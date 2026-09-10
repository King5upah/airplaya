"""Unit tests for the pieces that can be checked without an iPhone."""

from __future__ import annotations

import hashlib

import pytest

from airplaya.crypto.aesctr import AesCtrStream
from airplaya.crypto.keys import mirror_key_and_iv, mix_with_shared_secret
from airplaya.stream import nal
from airplaya.stream.mirror import MirrorDecryptor

KEY = bytes(range(16))
IV = bytes(range(16, 32))


def test_ctr_stream_is_continuous():
    """Splitting a write must not change the keystream."""
    whole = AesCtrStream(KEY, IV).process(b"\x00" * 48)
    split = AesCtrStream(KEY, IV)
    piecewise = split.process(b"\x00" * 7) + split.process(b"\x00" * 41)
    assert whole == piecewise


def test_skip_to_block_boundary_discards_partial_block():
    stream = AesCtrStream(KEY, IV)
    stream.process(b"\x00" * 5)
    stream.skip_to_block_boundary()
    after_skip = stream.process(b"\x00" * 16)

    reference = AesCtrStream(KEY, IV).process(b"\x00" * 32)
    assert after_skip == reference[16:32]


def test_skip_to_block_boundary_is_a_noop_when_aligned():
    stream = AesCtrStream(KEY, IV)
    stream.process(b"\x00" * 16)
    stream.skip_to_block_boundary()
    assert stream.process(b"\x00" * 16) == AesCtrStream(KEY, IV).process(b"\x00" * 32)[16:]


def test_ctr_stream_rejects_wrong_key_size():
    with pytest.raises(ValueError):
        AesCtrStream(b"short", IV)


def _encrypt_like_sender(payload_sizes: list[int]) -> list[bytes]:
    """Model the sender: one keystream, re-aligned at each payload start.

    Each payload's trailing partial block consumes a whole block of keystream,
    so the next payload begins part-way into that block.
    """
    stream = AesCtrStream(KEY, IV)
    carry = b""
    out = []
    for size in payload_sizes:
        plain = bytes((i * 7 + size) & 0xFF for i in range(size))
        cipher = bytearray(size)
        consumed = 0
        if carry:
            head = min(len(carry), size)
            for i in range(head):
                cipher[i] = plain[i] ^ carry[i]
            carry = carry[head:]
            consumed = head
        remaining = size - consumed
        whole = (remaining // 16) * 16
        stream.skip_to_block_boundary()
        if whole:
            cipher[consumed : consumed + whole] = stream.process(
                plain[consumed : consumed + whole]
            )
        tail = remaining - whole
        if tail:
            start = consumed + whole
            block = stream.process(plain[start:] + bytes(16 - tail))
            cipher[start:] = block[:tail]
            carry = block[tail:]
        out.append((bytes(cipher), plain))
    return out


@pytest.mark.parametrize(
    "sizes",
    [
        [16, 16, 16],
        [17, 33, 5, 100],
        [1, 1, 1, 1],
        [255, 4096, 63],
    ],
)
def test_mirror_decryptor_round_trip(sizes):
    """Decrypting the modelled sender's output must recover the plaintext.

    This is the property the whole video path depends on: the keystream stays
    aligned across payload boundaries.
    """
    decryptor = MirrorDecryptor(KEY, IV)
    for cipher, plain in _encrypt_like_sender(sizes):
        assert bytes(decryptor.decrypt(cipher)) == plain


def test_mix_with_shared_secret_matches_the_derivation():
    key = bytes(16)
    secret = bytes(range(32))
    assert mix_with_shared_secret(key, secret) == hashlib.sha512(key + secret).digest()[:16]


def test_mirror_key_and_iv_depend_on_the_stream_id():
    key = bytes(range(16))
    first = mirror_key_and_iv(key, 1234)
    second = mirror_key_and_iv(key, 1235)
    assert first != second
    assert all(len(part) == 16 for part in first + second)


def test_mirror_key_and_iv_uses_the_expected_labels():
    key = bytes(range(16))
    derived_key, derived_iv = mirror_key_and_iv(key, 42)
    assert derived_key == hashlib.sha512(b"AirPlayStreamKey42" + key).digest()[:16]
    assert derived_iv == hashlib.sha512(b"AirPlayStreamIV42" + key).digest()[:16]


def test_annex_b_conversion():
    payload = bytearray(b"\x00\x00\x00\x03abc" + b"\x00\x00\x00\x02de")
    converted, count = nal.to_annex_b(payload)
    assert count == 2
    assert converted == b"\x00\x00\x00\x01abc\x00\x00\x00\x01de"


def test_annex_b_rejects_overrunning_length():
    """A bad key produces nonsense lengths; that must be detected, not decoded."""
    with pytest.raises(nal.NalError):
        nal.to_annex_b(bytearray(b"\xff\xff\xff\xffabc"))


def test_annex_b_rejects_trailing_garbage():
    with pytest.raises(nal.NalError):
        nal.to_annex_b(bytearray(b"\x00\x00\x00\x02ab\x00"))


def test_parameter_sets_h264():
    sps = b"\x67\x42\x00\x1f"
    pps = b"\x68\xce\x38\x80"
    payload = (
        b"\x01\x42\x00\x1f\xff\xe1"
        + len(sps).to_bytes(2, "big")
        + sps
        + b"\x01"
        + len(pps).to_bytes(2, "big")
        + pps
    )
    assert nal.parameter_sets_h264(payload) == (
        nal.START_CODE + sps + nal.START_CODE + pps
    )


def test_parameter_sets_h264_rejects_overrun():
    payload = b"\x01\x42\x00\x1f\xff\xe1" + b"\xff\xff" + b"junk"
    with pytest.raises(nal.NalError):
        nal.parameter_sets_h264(payload)
