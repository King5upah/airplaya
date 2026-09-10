"""Tests for reading the picture size out of an H.264 SPS.

The size is how a rotation is detected: it changes, and the player has to be
restarted around the new geometry.
"""

from __future__ import annotations

import pytest

from airplaya.config import Config
from airplaya.stream import nal


# Real x264 output at known sizes, captured from ffmpeg.
_SPS_SAMPLES = {
    (1920, 1080): "67f40028919b280f0044fc4e0220000003002000000781e30632c0",
    # Portrait, and not a multiple of 16 in either direction: the size comes
    # from the cropping fields. This is the shape an iPhone sends.
    (1170, 2532): "67f40032919b28094027fc7c6e0220000003002000000781e30632c0",
    (640, 360): "67f4001e919b281405ff138088000003000800000301e078b16cb0",
}


@pytest.mark.parametrize(("size", "sps_hex"), list(_SPS_SAMPLES.items()))
def test_dimensions_of_real_sps(size, sps_hex):
    assert nal.h264_dimensions(bytes.fromhex(sps_hex)) == size


def test_rotation_is_visible_as_a_size_change():
    """The check the mirror stream relies on to restart the player."""
    portrait = nal.h264_dimensions(bytes.fromhex(_SPS_SAMPLES[(1170, 2532)]))
    landscape = nal.h264_dimensions(bytes.fromhex(_SPS_SAMPLES[(1920, 1080)]))
    assert portrait != landscape


def test_rejects_a_short_sps():
    with pytest.raises(nal.NalError):
        nal.h264_dimensions(b"\x67")


def test_emulation_prevention_bytes_are_removed():
    # `00 00 03` inside the payload is an escape, not data.
    assert nal._remove_emulation_prevention(b"\x00\x00\x03\x01") == b"\x00\x00\x01"
    assert nal._remove_emulation_prevention(b"\x00\x00\x03\x03") == b"\x00\x00\x03"
    assert nal._remove_emulation_prevention(b"\x01\x02\x03") == b"\x01\x02\x03"


def test_orientation_swaps_the_advertised_size():
    portrait = Config(width=1920, height=1080, orientation="portrait")
    assert portrait.advertised_size() == (1080, 1920)

    landscape = Config(width=1080, height=1920, orientation="landscape")
    assert landscape.advertised_size() == (1920, 1080)


def test_auto_orientation_keeps_the_configured_size():
    config = Config(width=1234, height=567, orientation="auto")
    assert config.advertised_size() == (1234, 567)
