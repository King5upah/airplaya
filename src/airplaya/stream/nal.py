"""Length-prefixed NAL units to Annex-B.

The mirroring payload holds one or more NAL units, each prefixed with a 4-byte
big-endian length. Decoders want Annex-B, where the prefix is the start code
`00 00 00 01` — same width, so the conversion is in place.

A length that runs past the end of the payload means the decryption is wrong
(bad key, or a keystream that has drifted out of alignment). That is worth
detecting: feeding a decoder garbage produces confusing artefacts instead of a
clear error.
"""

from __future__ import annotations

import struct

START_CODE = b"\x00\x00\x00\x01"
PREFIX_LEN = 4

_H264_TYPE_NAMES = {1: "slice", 5: "IDR", 6: "SEI", 7: "SPS", 8: "PPS"}


class NalError(Exception):
    """The payload is not a well-formed run of length-prefixed NAL units."""


def to_annex_b(payload: bytearray) -> tuple[bytes, int]:
    """Rewrite length prefixes as start codes.

    Returns the converted buffer and the NAL unit count. Raises `NalError` if
    the lengths do not tile the payload exactly.
    """
    total = len(payload)
    offset = 0
    count = 0
    while offset < total:
        if offset + PREFIX_LEN > total:
            raise NalError(f"truncated length prefix at offset {offset} of {total}")
        (length,) = struct.unpack_from(">I", payload, offset)
        if length == 0 or length > total - offset - PREFIX_LEN:
            raise NalError(
                f"NAL length {length} at offset {offset} does not fit in {total} bytes"
            )
        payload[offset : offset + PREFIX_LEN] = START_CODE
        offset += PREFIX_LEN + length
        count += 1

    if offset != total:
        raise NalError(f"NAL units cover {offset} bytes of a {total}-byte payload")
    return bytes(payload), count


def describe_h264(nal_header: int) -> str:
    nal_type = nal_header & 0x1F
    return _H264_TYPE_NAMES.get(nal_type, f"type {nal_type}")


def parameter_sets_h264(payload: bytes) -> bytes:
    """Extract SPS and PPS from an unencrypted type-0x01 payload as Annex-B.

    Layout: 6 bytes of avcC-style header, a 2-byte SPS length, the SPS, one
    byte of PPS count, a 2-byte PPS length, then the PPS.
    """
    if len(payload) < 8:
        raise NalError(f"parameter-set payload is only {len(payload)} bytes")

    (sps_len,) = struct.unpack_from(">H", payload, 6)
    sps_start = 8
    sps_end = sps_start + sps_len
    if sps_end + 3 > len(payload):
        raise NalError(f"SPS length {sps_len} overruns the payload")

    (pps_len,) = struct.unpack_from(">H", payload, sps_end + 1)
    pps_start = sps_end + 3
    pps_end = pps_start + pps_len
    if pps_end > len(payload):
        raise NalError(f"PPS length {pps_len} overruns the payload")

    return (
        START_CODE
        + payload[sps_start:sps_end]
        + START_CODE
        + payload[pps_start:pps_end]
    )


def parameter_sets_h265(payload: bytes) -> bytes:
    """Extract VPS, SPS and PPS from an HEVC type-0x01 payload as Annex-B.

    The three sets start at offset 0x75, each introduced by a 4-byte marker
    whose last two bytes are unused and whose length sits at offset 3.
    """
    markers = (b"\xa0\x00\x01", b"\xa1\x00\x01", b"\xa2\x00\x01")
    offset = 0x75
    out = bytearray()
    for index, marker in enumerate(markers):
        if payload[offset : offset + 3] != marker:
            raise NalError(f"HEVC parameter set {index} has no marker at {offset:#x}")
        (length,) = struct.unpack_from(">H", payload, offset + 3)
        start = offset + 5
        end = start + length
        if end > len(payload):
            raise NalError(f"HEVC parameter set {index} overruns the payload")
        out += START_CODE + payload[start:end]
        offset = end
    return bytes(out)
