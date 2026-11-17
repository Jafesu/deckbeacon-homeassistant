"""Noise Protocol Framework (rev. 34) subset for the DeckBeacon device channel.

Implements exactly what ADR-0002 needs, on top of ``cryptography``:

* ``Noise_XX_25519_ChaChaPoly_SHA256`` (pairing)
* ``Noise_IK_25519_ChaChaPoly_SHA256`` (sessions)

Interoperability with the Go agent (``flynn/noise``) is proven byte-for-byte by
``packages/protocol/vectors/noise.json`` (see tests/test_noise.py).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from collections.abc import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat

DHLEN = 32
HASHLEN = 32
MAX_MESSAGE = 65535
MAX_PLAINTEXT = MAX_MESSAGE - 16
PROLOGUE_PAIR = b"deckbeacon/1 pair"
PROLOGUE_SESSION = b"deckbeacon/1 session"

_PATTERNS: dict[str, tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]] = {
    # (responder pre-message tokens, message patterns)
    "XX": ((), (("e",), ("e", "ee", "s", "es"), ("s", "se"))),
    "IK": (("s",), (("e", "es", "s", "ss"), ("e", "ee", "se"))),
}


class NoiseError(Exception):
    """Handshake or transport failure. Always fatal for the connection."""


class KeyPair:
    """X25519 key pair (raw 32-byte encodings)."""

    def __init__(self, private: bytes) -> None:
        if len(private) != DHLEN:
            raise ValueError("X25519 private key must be 32 bytes")
        self._key = X25519PrivateKey.from_private_bytes(private)
        self.private = private
        self.public = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @classmethod
    def generate(cls) -> KeyPair:
        key = X25519PrivateKey.generate()
        return cls(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, _no_encryption()))

    def dh(self, public: bytes) -> bytes:
        if len(public) != DHLEN:
            raise NoiseError("bad public key length")
        try:
            return self._key.exchange(X25519PublicKey.from_public_bytes(public))
        except ValueError as err:  # low-order point
            raise NoiseError("invalid DH") from err


def _no_encryption():
    from cryptography.hazmat.primitives.serialization import NoEncryption

    return NoEncryption()


def _hkdf(chaining_key: bytes, ikm: bytes, outputs: int) -> tuple[bytes, ...]:
    temp = hmac.new(chaining_key, ikm, hashlib.sha256).digest()
    out1 = hmac.new(temp, b"\x01", hashlib.sha256).digest()
    out2 = hmac.new(temp, out1 + b"\x02", hashlib.sha256).digest()
    if outputs == 2:
        return out1, out2
    out3 = hmac.new(temp, out2 + b"\x03", hashlib.sha256).digest()
    return out1, out2, out3


class CipherState:
    """Noise CipherState with ChaChaPoly (nonce = 4 zero bytes || LE64(n))."""

    def __init__(self, key: bytes | None = None) -> None:
        self._aead = ChaCha20Poly1305(key) if key is not None else None
        self.n = 0

    @property
    def has_key(self) -> bool:
        return self._aead is not None

    def _nonce(self) -> bytes:
        if self.n >= 2**64 - 1:
            raise NoiseError("nonce exhausted")
        return b"\x00\x00\x00\x00" + struct.pack("<Q", self.n)

    def encrypt(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._aead is None:
            return plaintext
        ct = self._aead.encrypt(self._nonce(), plaintext, ad)
        self.n += 1
        return ct

    def decrypt(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._aead is None:
            return ciphertext
        try:
            pt = self._aead.decrypt(self._nonce(), ciphertext, ad)
        except InvalidTag as err:
            raise NoiseError("authentication failed") from err  # n is not incremented
        self.n += 1
        return pt


class SymmetricState:
    def __init__(self, protocol_name: bytes) -> None:
        if len(protocol_name) <= HASHLEN:
            self.h = protocol_name.ljust(HASHLEN, b"\x00")
        else:
            self.h = hashlib.sha256(protocol_name).digest()
        self.ck = self.h
        self.cipher = CipherState()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, temp_k = _hkdf(self.ck, ikm, 2)
        self.cipher = CipherState(temp_k)

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ct = self.cipher.encrypt(self.h, plaintext)
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        pt = self.cipher.decrypt(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return pt

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = _hkdf(self.ck, b"", 2)
        return CipherState(k1), CipherState(k2)


class Session:
    """Established transport. Seal/Open are each single-threaded."""

    def __init__(self, send: CipherState, recv: CipherState, handshake_hash: bytes, remote_static: bytes):
        self._send = send
        self._recv = recv
        self.handshake_hash = handshake_hash
        self.remote_static = remote_static

    def seal(self, plaintext: bytes) -> bytes:
        if len(plaintext) > MAX_PLAINTEXT:
            raise NoiseError("plaintext too large")
        return self._send.encrypt(b"", plaintext)

    def open(self, ciphertext: bytes) -> bytes:
        if len(ciphertext) > MAX_MESSAGE:
            raise NoiseError("message too large")
        return self._recv.decrypt(b"", ciphertext)


class Handshake:
    """Drive one handshake, one message at a time."""

    def __init__(
        self,
        pattern: str,
        initiator: bool,
        prologue: bytes,
        static: KeyPair,
        remote_static: bytes | None = None,
        ephemeral_factory: Callable[[], KeyPair] = KeyPair.generate,
    ) -> None:
        if pattern not in _PATTERNS:
            raise ValueError(f"unsupported pattern {pattern}")
        pre, self._messages = _PATTERNS[pattern]
        self.initiator = initiator
        self.s = static
        self.rs = remote_static
        self.e: KeyPair | None = None
        self.re: bytes | None = None
        self._new_ephemeral = ephemeral_factory
        self._step = 0
        self._session: Session | None = None
        self.ss = SymmetricState(f"Noise_{pattern}_25519_ChaChaPoly_SHA256".encode())
        self.ss.mix_hash(prologue)
        if "s" in pre:  # responder static is a pre-message
            if initiator:
                if remote_static is None or len(remote_static) != DHLEN:
                    raise ValueError("IK initiator requires the responder static key")
                self.ss.mix_hash(remote_static)
            else:
                self.ss.mix_hash(static.public)

    @property
    def done(self) -> bool:
        return self._session is not None

    @property
    def my_turn(self) -> bool:
        return (self._step % 2 == 0) == self.initiator

    def _dh(self, token: str) -> bytes:
        # token "xy": x is the initiator's key, y the responder's.
        first, second = token
        if self.initiator:
            mine = self.e if first == "e" else self.s
            theirs = self.re if second == "e" else self.rs
        else:
            mine = self.e if second == "e" else self.s
            theirs = self.re if first == "e" else self.rs
        if mine is None or theirs is None:
            raise NoiseError("missing key for DH")
        return mine.dh(theirs)

    def write_message(self, payload: bytes = b"") -> bytes:
        if self.done or not self.my_turn:
            raise NoiseError("not our turn to write")
        out = bytearray()
        for token in self._messages[self._step]:
            if token == "e":
                self.e = self._new_ephemeral()
                out += self.e.public
                self.ss.mix_hash(self.e.public)
            elif token == "s":
                out += self.ss.encrypt_and_hash(self.s.public)
            else:
                self.ss.mix_key(self._dh(token))
        out += self.ss.encrypt_and_hash(payload)
        self._advance()
        return bytes(out)

    def read_message(self, message: bytes) -> bytes:
        if self.done or self.my_turn:
            raise NoiseError("not our turn to read")
        if len(message) > MAX_MESSAGE:
            raise NoiseError("handshake message too large")
        buf = memoryview(message)
        for token in self._messages[self._step]:
            if token == "e":
                if len(buf) < DHLEN:
                    raise NoiseError("short message")
                self.re = bytes(buf[:DHLEN])
                buf = buf[DHLEN:]
                self.ss.mix_hash(self.re)
            elif token == "s":
                n = DHLEN + (16 if self.ss.cipher.has_key else 0)
                if len(buf) < n:
                    raise NoiseError("short message")
                self.rs = self.ss.decrypt_and_hash(bytes(buf[:n]))
                buf = buf[n:]
            else:
                self.ss.mix_key(self._dh(token))
        payload = self.ss.decrypt_and_hash(bytes(buf))
        self._advance()
        return payload

    def _advance(self) -> None:
        self._step += 1
        if self._step < len(self._messages):
            return
        c1, c2 = self.ss.split()
        send, recv = (c1, c2) if self.initiator else (c2, c1)
        assert self.rs is not None
        self._session = Session(send, recv, self.ss.h, self.rs)

    def session(self) -> Session:
        if self._session is None:
            raise NoiseError("handshake incomplete")
        return self._session


def sas(handshake_hash: bytes) -> str:
    """6-digit short authentication string (must match the Go implementation)."""
    digest = hashlib.sha256(b"deckbeacon-sas-v1" + handshake_hash).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


def random_bytes(n: int) -> bytes:
    return os.urandom(n)
