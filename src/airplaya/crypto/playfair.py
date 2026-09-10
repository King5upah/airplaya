"""Binding to the native `playfair` library.

`playfair_decrypt` recovers the 16-byte AES session key from the 72-byte `ekey`
blob in the first SETUP request. The algorithm is a white-box cipher with about
half a megabyte of key tables; it exists only as C (from the `playfair` project,
via RPiPlay/UxPlay), so we call it rather than reimplement it.

Build the shared library with `python scripts/build_playfair.py`.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

KEY_MESSAGE_LEN = 164  # the fp-setup stage-2 request, verbatim
CIPHERTEXT_LEN = 72  # the `ekey` blob from SETUP
KEY_LEN = 16


class PlayfairUnavailable(RuntimeError):
    """The native library is missing or could not be loaded."""


def _library_names() -> tuple[str, ...]:
    if sys.platform == "win32":
        return ("playfair.dll",)
    if sys.platform == "darwin":
        return ("libplayfair.dylib",)
    return ("libplayfair.so",)


def _candidate_paths() -> list[Path]:
    here = Path(__file__).resolve().parent
    roots = [here, here.parent.parent.parent.parent / "build"]
    return [root / name for root in roots for name in _library_names()]


class Playfair:
    """Lazily-loaded handle on the native library."""

    def __init__(self, library_path: str | Path | None = None) -> None:
        self._path = Path(library_path) if library_path else self._find()
        try:
            self._lib = ctypes.CDLL(str(self._path))
        except OSError as exc:
            raise PlayfairUnavailable(f"could not load {self._path}: {exc}") from exc

        try:
            self._decrypt = self._lib.playfair_decrypt
        except AttributeError as exc:
            raise PlayfairUnavailable(
                f"{self._path} does not export playfair_decrypt"
            ) from exc
        self._decrypt.restype = None
        self._decrypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]

    @staticmethod
    def _find() -> Path:
        for candidate in _candidate_paths():
            if candidate.exists():
                return candidate
        searched = "\n  ".join(str(p) for p in _candidate_paths())
        raise PlayfairUnavailable(
            "native playfair library not found. Build it with "
            "`python scripts/build_playfair.py`. Searched:\n  " + searched
        )

    @property
    def path(self) -> Path:
        return self._path

    def decrypt_key(self, key_message: bytes, ciphertext: bytes) -> bytes:
        """Recover the AES session key.

        `key_message` is the 164-byte fp-setup stage-2 request; `ciphertext` is
        the 72-byte `ekey`.
        """
        if len(key_message) != KEY_MESSAGE_LEN:
            raise ValueError(f"key_message must be {KEY_MESSAGE_LEN} bytes")
        if len(ciphertext) != CIPHERTEXT_LEN:
            raise ValueError(f"ciphertext must be {CIPHERTEXT_LEN} bytes")

        # The C signature is non-const on all three arguments, so pass mutable
        # buffers even for the inputs.
        msg = ctypes.create_string_buffer(key_message, KEY_MESSAGE_LEN)
        cipher = ctypes.create_string_buffer(ciphertext, CIPHERTEXT_LEN)
        out = ctypes.create_string_buffer(KEY_LEN)
        self._decrypt(msg, cipher, out)
        return out.raw[:KEY_LEN]


_INSTANCE: Playfair | None = None


def load() -> Playfair:
    """Process-wide singleton; the native library holds no per-session state."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Playfair()
    return _INSTANCE
