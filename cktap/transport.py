# (c) Copyright 2021 by Coinkite Inc. This file is covered by license found in COPYING-CC.
#
# transport.py
#
# Implement the desktop to card connection for our cards, both TAPSIGNER and SATSCARD.
#
#
import sys, os, cbor2
from binascii import b2a_hex, a2b_hex
from hashlib import sha256
from .utils import *
from .constants import *

# single-shot SHA256
sha256s = lambda msg: sha256(msg).digest()

# Correct response from all commands: 90 00 
SW_OKAY = 0x9000

# Change this to see traffic
VERBOSE = False

def find_cards():
    #
    # Search all connected card readers, and find all cards that are present.
    #
    from smartcard.System import readers as get_readers
    from smartcard.Exceptions import CardConnectionException, NoCardException

    # emulation running on a Unix socket
    sim = CKEmulatedCard.find_simulator()
    if sim:
        yield sim

    readers = get_readers()
    if not readers:
        raise RuntimeError("No USB card readers found. Need at least one.")

    # search for our card
    for r in readers:
        try:
            conn = r.createConnection()
        except:
            continue
        
        try:
            conn.connect()
            atr = conn.getATR()
        except (CardConnectionException, NoCardException):
            #print(f"Empty reader: {r}")
            continue

        if atr == CARD_ATR:
            yield CKTapCard(conn)

class CKTapDeviceBase:
    #
    # Abstract base class
    #
    def first_look(self):
        # Call this at end of __init__ to load up details from card

        st = self.send('status')
        assert 'error' not in st, 'Early failure: ' + repr(st)
        assert st['proto'] == 1, "Unknown card protocol version"

        self.pubkey = st['pubkey']
        self.applet_version = st['ver']
        self.birth_height = st.get('birth', None)
        self.is_testnet = st.get('testnet', False)
        self.active_slot, self.num_slots = st['slots']
        assert self.card_nonce      # self.send() will have captured from first status req
        self._certs_checked = False

    def __repr__(self):
        kk = b2a_hex(self.pubkey).decode('ascii')[-8:] if hasattr(self, 'pubkey') else '???'
        return '<%s: card_pubkey=...%s> ' % (self.__class__.__name__, kk)

    def _send_recv(self, msg):
        # do CBOR encoding and round-trip the request + response
        raise NotImplementedError

    def nfc_read(self):
        # return the contents of the NFC tag (NDEF file)
        raise NotImplementedError

    def get_ATR(self):
        # ATR = Answer To Reset
        raise NotImplementedError

    def send_auth(self, cmd, cvc, **args):
        # clean up, do crypto and provide the CVC in encrypted form
        # - returns session key and usual results
        # - skip if CVC is none, just do normal stuff

        if cvc:
            cvc = cvc[0:0].join(d for d in cvc if d.isdigit())
            session_key, auth_args = calc_xcvc(self.card_nonce, self.pubkey, cvc)
            args.update(auth_args)
        else:
            session_key = None

        # One single command takes an encrypted argument (most are returning encrypted
        # results) and the caller didn't know the session key yet. So xor it for them.
        if cmd == 'blind':
            args['digest'] = xor_bytes(args['digest'], session_key)

        return session_key, self.send(cmd, **args)

    def close(self):
        # release resources
        pass

    def send(self, cmd, raise_on_error=True, _retries=5, **args):
        # Serialize command, send it as ADPU, get response and decode

        args = dict(args)
        args['cmd'] = cmd
        msg = cbor2.dumps(args)

        for retry in range(_retries):
            # Send and wait for reply
            stat_word, resp = self._send_recv(msg)

            try:
                resp = cbor2.loads(resp) if resp else {}
            except:
                print("Bad CBOR rx'd from card:\n{B2A(resp)}")
                raise RuntimeError('Bad CBOR from card')

            if stat_word != SW_OKAY:
                # Assume error if ANY bad SW value seen; promote for debug purposes
                if 'error' not in resp:
                    resp['error'] = "Got error SW value: 0x%04x" % stat_word
                resp['stat_word'] = stat_word

            if VERBOSE:
                if 'error' in resp:
                    print(f"Command '{cmd}' => " + ', '.join(resp.keys()))
                else:
                    print(f"Command '{cmd}' => " + pformat(resp))

            # just simple bad luck can fail a command, so retry for another
            # number; exactly same request data is used.
            if 'error' in resp and resp.get('code', 0) == 205:
                continue
            break

        if 'card_nonce' in resp:
            # many responses provide an updated card_nonce need for
            # the *next* comand. Track it
            # - only changes when "consumed" by commands that need CVC
            self.card_nonce = resp['card_nonce']

        if raise_on_error and 'error' in resp:
            msg = resp.pop('error')
            raise CardRuntimeError(msg, resp)

        return resp

    #
    # Wrappers and Helpers
    #
    def address(self, faster=False, incl_pubkey=False, slot=None):
        # Get current payment address for card
        # - does 100% full verification by default
        # - returns a bech32 address as a string

        # check certificate chain
        if not self._certs_checked and not faster:
            self.certificate_check()

        st = self.send('status')
        cur_slot = st['slots'][0]
        if slot is None:
            slot = cur_slot

        if ('addr' not in st) and (cur_slot == slot):
            #raise ValueError("Current slot is not yet setup.")

            return None

        if self.pubkey.hex().endswith('a0b0d81a93117e442ae9ddce82c44d4268'):
            # XXX debug card, before API change
            if slot == 0:
                return 'tb1q39xdpaq5utt9f6pn7zvw5qwf6hweukwqm4avgp'

        if slot != cur_slot:
            # Use the unauthenticated "dump" command.
            rr = self.send('dump', slot=slot)

            assert not incl_pubkey, 'can only get pubkey on current slot'

            return rr['addr']
        
        # Use special-purpose "read" command
        n = pick_nonce()
        rr = self.send('read', nonce=n)
        
        pubkey, addr = recover_address(st, rr, n)

        if not faster:
            # additional check: did card include chain_code in generated private key?
            my_nonce = pick_nonce()
            card_nonce = self.card_nonce
            rr = self.send('derive', nonce=my_nonce)
            master_pub = verify_master_pubkey(rr['master_pubkey'], rr['sig'],
                                                rr['chain_code'], my_nonce, card_nonce)
            derived_addr,_ = verify_derive_address(rr['chain_code'], master_pub,
                                                        testnet=self.is_testnet)
            if derived_addr != addr:
                raise ValueError("card did not derive address as expected")

        if incl_pubkey:
            return pubkey, addr

        return addr

    def certificate_check(self):
        # Verify the certificate chain and the public key of the card
        # - assures this card was produced in Coinkite factory
        # - does not relate to payment addresses or slot usage
        # - raises on errors/failed validation
        st = self.send('status')
        certs = self.send('certs')

        n = pick_nonce()
        check = self.send('check', nonce=n)

        verify_certs(st, check, certs, n)
        self._certs_checked = True

    # TODO
    # - get chain_code (derive cmd w/ nonce) and/or get "xpub"

class CKTapCard(CKTapDeviceBase):
    #
    # For talking to a real card over USB to a reader.
    #
    @classmethod
    def find_first(cls):
        # operate on the first card we can find
        for c in find_cards():
            if isinstance(c, cls):
                return c
        return None

    def __init__(self, card_conn):
        # Check connection they gave us
        # - if you don't have that, use find_cards instead
        atr = card_conn.getATR()
        assert atr == CARD_ATR, "wrong ATR from card"

        self._conn = card_conn

        # Perform "ISO Select" to pick our app
        # - 00 a4 04 00 (APPID)
        # - probably optional
        sw, resp = self._apdu(0x00, 0xa4, APP_ID, p1=4)
        assert sw == SW_OKAY, "ISO app select failed"

        self.first_look()

    def close(self):
        # release resources
        self._conn.disconnect()
        del self._conn

    def get_ATR(self):
        return self._conn.getATR()

    def _apdu(self, cls, ins, data, p1=0, p2=0):
        # send APDU to card
        lst = [ cls, ins, p1, p2, len(data)] + list(data)
        resp, sw1, sw2 = self._conn.transmit(lst)
        resp = bytes(resp)
        return ((sw1 << 8) | sw2), resp

    def _send_recv(self, msg):
        # send raw bytes (already CBOR encoded) and get response back
        assert len(msg) <= 255, "msg too long"
        return self._apdu(CBOR_CLA, CBOR_INS, msg)

class CKEmulatedCard(CKTapDeviceBase):
    #
    # Emulation running over a Unix socket.
    #

    @classmethod
    def find_simulator(cls):
        import os
        FN = '/tmp/ecard-pipe'
        if os.path.exists(FN):
            return cls(FN)
        return None

    def get_ATR(self):
        return CARD_ATR

    def __init__(self, pipename):
        import socket
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(pipename)
        self.first_look()
        self._certs_checked = True      # because it won't pass

    def _send_recv(self, msg):
        # send and receive response back
        self.sock.sendall(msg)
        resp = self.sock.recv(4096)

        if not resp:
            # closed socket causes this
            raise RuntimeError("Emu crashed?")

        return 0x9000, resp

    def nfc_read(self):
        # read the NFC data from card
        # - use special command
        return self.send('XXX_NFC')['nfc']

        

# EOF
