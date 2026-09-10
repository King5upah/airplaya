"""Tests for the audio path."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from airplaya.stream.asc import (
    AOT_AAC_ELD,
    AOT_AAC_LC,
    audio_specific_config,
    sample_rate_index,
)
from airplaya.stream.audio import RTP_HEADER_LEN, decrypt_packet

KEY = bytes(range(16))
IV = bytes(range(16, 32))


def test_eld_config_matches_the_expected_bits():
    """AAC-ELD, 44.1 kHz, stereo, 480-sample frames.

    Bit by bit: object type escape (31) + 7 for type 39, sample rate index 4,
    two channels, the 480-sample flag, three resilience flags off, no SBR, and
    ELDEXT_TERM.
    """
    assert audio_specific_config(AOT_AAC_ELD, 44100, 2, 480) == bytes.fromhex("f8e85000")


def test_eld_frame_length_changes_the_config():
    assert audio_specific_config(AOT_AAC_ELD, 44100, 2, 512) != audio_specific_config(
        AOT_AAC_ELD, 44100, 2, 480
    )


def test_eld_rejects_an_impossible_frame_length():
    with pytest.raises(ValueError):
        audio_specific_config(AOT_AAC_ELD, 44100, 2, 1024)


def test_lc_config_is_two_bytes():
    # 5 + 4 + 4 + 3 bits of GASpecificConfig fits in two bytes.
    assert audio_specific_config(AOT_AAC_LC, 44100, 2) == bytes.fromhex("1210")


def test_sample_rate_index():
    assert sample_rate_index(44100) == 4
    assert sample_rate_index(48000) == 3
    with pytest.raises(ValueError):
        sample_rate_index(44000)


def _encrypt_like_sender(plaintext: bytes) -> bytes:
    """Encrypt whole blocks only, leaving any tail in the clear."""
    whole = len(plaintext) // 16 * 16
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return encryptor.update(plaintext[:whole]) + encryptor.finalize() + plaintext[whole:]


@pytest.mark.parametrize("size", [16, 32, 100, 480, 7])
def test_audio_decrypt_round_trip(size):
    plaintext = bytes((i * 11 + size) & 0xFF for i in range(size))
    assert decrypt_packet(KEY, IV, _encrypt_like_sender(plaintext)) == plaintext


def test_audio_decrypt_restarts_per_packet():
    """Each packet starts from the session IV, so identical packets decrypt alike."""
    plaintext = bytes(range(32))
    cipher = _encrypt_like_sender(plaintext)
    assert decrypt_packet(KEY, IV, cipher) == plaintext
    assert decrypt_packet(KEY, IV, cipher) == plaintext


def test_audio_decrypt_passes_short_payloads_through():
    assert decrypt_packet(KEY, IV, b"abc") == b"abc"


def test_rtp_header_length():
    # The payload starts after a fixed 12-byte RTP header.
    assert RTP_HEADER_LEN == 12
