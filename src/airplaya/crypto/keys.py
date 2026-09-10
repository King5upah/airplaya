"""Media key derivation.

Two steps sit between the FairPlay handshake and a decryptable video stream:

1. The AES key recovered from `ekey` is hashed together with the pairing shared
   secret. Clients that never paired have no secret, and for them the key is
   used as-is.
2. The mirroring stream derives its own key and IV from that AES key plus the
   `streamConnectionID` the client sends in SETUP. A new stream ID means a new
   keystream.

Every hash here is SHA-512 truncated to 16 bytes.
"""

from __future__ import annotations

import hashlib

AES_KEY_LEN = 16


def mix_with_shared_secret(aes_key: bytes, shared_secret: bytes) -> bytes:
    """Fold the pairing secret into the AES key."""
    if len(aes_key) != AES_KEY_LEN:
        raise ValueError(f"aes_key must be {AES_KEY_LEN} bytes")
    return hashlib.sha512(aes_key + shared_secret).digest()[:AES_KEY_LEN]


def mirror_key_and_iv(aes_key: bytes, stream_connection_id: int) -> tuple[bytes, bytes]:
    """Derive the AES-CTR key and IV for one mirroring stream.

    The label strings are exactly what the client hashes on its side, including
    the unsigned decimal stream ID appended with no separator.
    """
    if len(aes_key) != AES_KEY_LEN:
        raise ValueError(f"aes_key must be {AES_KEY_LEN} bytes")

    # The client formats the ID as a 64-bit unsigned value.
    stream_id = stream_connection_id & 0xFFFFFFFFFFFFFFFF
    key_label = f"AirPlayStreamKey{stream_id}".encode()
    iv_label = f"AirPlayStreamIV{stream_id}".encode()

    key = hashlib.sha512(key_label + aes_key).digest()[:AES_KEY_LEN]
    iv = hashlib.sha512(iv_label + aes_key).digest()[:AES_KEY_LEN]
    return key, iv
