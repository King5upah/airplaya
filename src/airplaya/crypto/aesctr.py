"""AES-128-CTR as a resumable keystream.

The mirroring stream is not a sequence of independent ciphertexts: one long
keystream runs across every video packet of a session, and the receiver has to
stay byte-aligned with it. That rules out a one-shot `encrypt()` helper, so we
keep the counter and the offset inside the current block explicitly.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK = 16


class AesCtrStream:
    """A stateful AES-128-CTR keystream positioned at an arbitrary byte offset."""

    def __init__(self, key: bytes, iv: bytes) -> None:
        if len(key) != BLOCK or len(iv) != BLOCK:
            raise ValueError("AES-128-CTR needs a 16-byte key and a 16-byte IV")
        self._cipher = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
        self._block_offset = 0

    def process(self, data: bytes) -> bytes:
        """XOR `data` with the next `len(data)` keystream bytes.

        CTR is symmetric, so this both encrypts and decrypts.
        """
        out = self._cipher.update(data)
        self._block_offset = (self._block_offset + len(data)) % BLOCK
        return out

    def skip_to_block_boundary(self) -> None:
        """Discard the rest of the current block so the next byte starts fresh.

        The sender re-aligns to a block boundary at the start of every video
        packet. Reproducing that discard is what keeps our keystream in step.
        """
        if self._block_offset == 0:
            return
        self.process(bytes(BLOCK - self._block_offset))
