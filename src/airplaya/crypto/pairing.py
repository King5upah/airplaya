"""Legacy AirPlay pairing (`/pair-setup` and `/pair-verify`).

This is the pre-HomeKit scheme, and it authenticates nothing: the client never
checked our identity in a way we cannot satisfy, and we accept whoever calls.
Its one lasting effect is the X25519 shared secret, which is mixed into the AES
media key — so a receiver cannot skip it and still decrypt the stream.

The flow:

* `POST /pair-setup` — client sends 32 bytes we ignore, we return our long-term
  Ed25519 public key.
* `POST /pair-verify` with a leading `0x01` — client sends its ephemeral X25519
  public key and its Ed25519 public key. We answer with our ephemeral X25519
  public key plus an Ed25519 signature over both public keys, encrypted under
  keys derived from the shared secret.
* `POST /pair-verify` with a leading `0x00` — client returns the mirror-image
  signature, which we verify.

The encryption of both signatures runs on one continuous AES-CTR keystream: our
64-byte signature consumes the first 64 bytes, so the client's signature is
encrypted with bytes 64..128. Getting that offset wrong is the classic way this
step fails.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

from airplaya.crypto.aesctr import AesCtrStream
from airplaya.log import get_logger

log = get_logger(__name__)

KEY_LEN = 32
SIGNATURE_LEN = 64

_SALT_KEY = b"Pair-Verify-AES-Key"
_SALT_IV = b"Pair-Verify-AES-IV"


class PairingError(Exception):
    pass


def _raw(key: X25519PublicKey | Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


class DeviceIdentity:
    """The receiver's long-term Ed25519 key.

    iOS remembers the public key it saw, so regenerating it on every start makes
    the receiver look like a different device each time. We persist it.
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private = private_key

    @classmethod
    def load_or_create(cls, path: Path) -> "DeviceIdentity":
        if path.exists():
            raw = path.read_bytes()
            if len(raw) == KEY_LEN:
                log.debug("loaded device key from %s", path)
                return cls(Ed25519PrivateKey.from_private_bytes(raw))
            log.warning("%s is not a 32-byte key; generating a new one", path)

        private = Ed25519PrivateKey.generate()
        raw = private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        try:
            path.chmod(0o600)
        except OSError:
            # Windows ACLs do not map onto POSIX modes; the user profile
            # directory is already private.
            pass
        log.info("generated a new device key at %s", path)
        return cls(private)

    @property
    def public_key(self) -> bytes:
        return _raw(self._private.public_key())

    def sign(self, message: bytes) -> bytes:
        return self._private.sign(message)


class PairingSession:
    """One client's pairing state. Not reusable across connections."""

    def __init__(self, identity: DeviceIdentity) -> None:
        self._identity = identity
        self._ephemeral: X25519PrivateKey | None = None
        self._their_x25519: bytes | None = None
        self._their_ed25519: Ed25519PublicKey | None = None
        self._shared_secret: bytes | None = None
        self._verified = False

    @property
    def shared_secret(self) -> bytes | None:
        """The X25519 secret, once `/pair-verify` step 1 has run.

        `None` means the client skipped pairing, in which case the AES media key
        must *not* be hashed with a secret.
        """
        return self._shared_secret

    @property
    def verified(self) -> bool:
        return self._verified

    def setup(self) -> bytes:
        """`/pair-setup`: hand out our long-term public key."""
        return self._identity.public_key

    def verify_start(self, their_x25519: bytes, their_ed25519: bytes) -> bytes:
        """`/pair-verify` step 1: exchange keys and sign both halves."""
        if len(their_x25519) != KEY_LEN or len(their_ed25519) != KEY_LEN:
            raise PairingError("pair-verify step 1: malformed public keys")

        self._ephemeral = X25519PrivateKey.generate()
        self._their_x25519 = bytes(their_x25519)
        self._their_ed25519 = Ed25519PublicKey.from_public_bytes(bytes(their_ed25519))
        self._shared_secret = self._ephemeral.exchange(
            X25519PublicKey.from_public_bytes(self._their_x25519)
        )

        our_x25519 = _raw(self._ephemeral.public_key())
        signature = self._identity.sign(our_x25519 + self._their_x25519)
        encrypted = self._keystream().process(signature)
        log.debug("pair-verify step 1 complete")
        return our_x25519 + encrypted

    def verify_finish(self, encrypted_signature: bytes) -> None:
        """`/pair-verify` step 2: check the client's signature.

        Raises `PairingError` if it does not check out.
        """
        if self._shared_secret is None or self._ephemeral is None:
            raise PairingError("pair-verify step 2 arrived before step 1")
        if len(encrypted_signature) != SIGNATURE_LEN:
            raise PairingError("pair-verify step 2: malformed signature")

        stream = self._keystream()
        # Our own signature already consumed the first 64 keystream bytes.
        stream.process(bytes(SIGNATURE_LEN))
        signature = stream.process(encrypted_signature)

        assert self._their_ed25519 is not None and self._their_x25519 is not None
        message = self._their_x25519 + _raw(self._ephemeral.public_key())
        try:
            self._their_ed25519.verify(signature, message)
        except InvalidSignature as exc:
            raise PairingError("pair-verify signature did not verify") from exc

        self._verified = True
        log.debug("pair-verify step 2 complete: client signature verified")

    def _derive(self, salt: bytes) -> bytes:
        assert self._shared_secret is not None
        return hashlib.sha512(salt + self._shared_secret).digest()[:16]

    def _keystream(self) -> AesCtrStream:
        return AesCtrStream(self._derive(_SALT_KEY), self._derive(_SALT_IV))
