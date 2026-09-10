"""The FairPlay SAP handshake, as far as a receiver has to play along.

Three messages arrive on `POST /fp-setup`:

1. 16 bytes. The client asks which of four canned replies we speak; byte 14
   selects one. We echo the matching 142-byte constant.
2. 164 bytes. We answer with a fixed 12-byte header plus 20 bytes echoed from
   the request, and keep the whole request — it is the key material for step 3.
3. Not a request at all: the 72-byte `ekey` in the first SETUP, decrypted with
   the stage-2 message via the native playfair library.

None of this authenticates anybody. It is a ritual iOS insists on before it
will send a pixel.
"""

from __future__ import annotations

from airplaya.crypto import playfair
from airplaya.crypto.fp_replies import HANDSHAKE_HEADER, SETUP_REPLIES
from airplaya.log import get_logger

log = get_logger(__name__)

SUPPORTED_VERSION = 0x03
SETUP_REQUEST_LEN = 16
HANDSHAKE_REQUEST_LEN = 164
SETUP_REPLY_LEN = 142
HANDSHAKE_REPLY_LEN = 32


class FairPlayError(Exception):
    pass


class FairPlaySession:
    """Per-connection FairPlay state."""

    def __init__(self) -> None:
        self._key_message: bytes | None = None

    def setup(self, request: bytes) -> bytes:
        """Stage 1: pick a canned reply."""
        if len(request) != SETUP_REQUEST_LEN:
            raise FairPlayError(f"fp-setup stage 1: expected 16 bytes, got {len(request)}")
        if request[4] != SUPPORTED_VERSION:
            raise FairPlayError(f"unsupported FairPlay version {request[4]:#x}")

        mode = request[14]
        if mode >= len(SETUP_REPLIES):
            raise FairPlayError(f"unknown fp-setup mode {mode}")
        # A new stage 1 restarts the handshake, so any earlier key material is
        # stale and must not be reused.
        self._key_message = None
        log.debug("fp-setup stage 1, mode %d", mode)
        return SETUP_REPLIES[mode]

    def handshake(self, request: bytes) -> bytes:
        """Stage 2: acknowledge, and retain the request as key material."""
        if len(request) != HANDSHAKE_REQUEST_LEN:
            raise FairPlayError(f"fp-setup stage 2: expected 164 bytes, got {len(request)}")
        if request[4] != SUPPORTED_VERSION:
            raise FairPlayError(f"unsupported FairPlay version {request[4]:#x}")

        self._key_message = bytes(request)
        log.debug("fp-setup stage 2 complete")
        return HANDSHAKE_HEADER + request[144:164]

    def decrypt_session_key(self, ekey: bytes) -> bytes:
        """Turn the 72-byte `ekey` from SETUP into the 16-byte AES key."""
        if self._key_message is None:
            raise FairPlayError("SETUP arrived before the fp-setup handshake finished")
        key = playfair.load().decrypt_key(self._key_message, ekey)
        log.debug("recovered %d-byte AES session key", len(key))
        return key
