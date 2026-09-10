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


class _BitReader:
    """Bit reader with exp-Golomb support, for reading an SPS."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def bit(self) -> int:
        index, offset = divmod(self._pos, 8)
        if index >= len(self._data):
            raise NalError("ran off the end of the SPS")
        self._pos += 1
        return (self._data[index] >> (7 - offset)) & 1

    def bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self) -> int:
        """Unsigned exp-Golomb."""
        leading = 0
        while self.bit() == 0:
            leading += 1
            if leading > 32:
                raise NalError("malformed exp-Golomb value in the SPS")
        if leading == 0:
            return 0
        return (1 << leading) - 1 + self.bits(leading)

    def se(self) -> int:
        """Signed exp-Golomb."""
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)


def _remove_emulation_prevention(data: bytes) -> bytes:
    """Strip the `00 00 03` escape bytes an encoder inserts."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0x00 else 0
    return bytes(out)


def h264_dimensions(sps: bytes) -> tuple[int, int]:
    """Read the frame size out of an H.264 SPS.

    Used to notice when the phone rotates: the picture size changes, and a
    decoder already running cannot follow that, so the player has to be
    restarted. Parsing is cheaper and more reliable than waiting for the
    player to fall over.
    """
    if len(sps) < 4:
        raise NalError(f"SPS is only {len(sps)} bytes")

    # Skip the NAL header byte, and undo the encoder's escaping.
    reader = _BitReader(_remove_emulation_prevention(sps[1:]))

    profile_idc = reader.bits(8)
    reader.bits(8)  # constraint flags and reserved bits
    reader.bits(8)  # level_idc
    reader.ue()  # seq_parameter_set_id

    chroma_format_idc = 1
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format_idc = reader.ue()
        if chroma_format_idc == 3:
            reader.bit()  # separate_colour_plane_flag
        reader.ue()  # bit_depth_luma_minus8
        reader.ue()  # bit_depth_chroma_minus8
        reader.bit()  # qpprime_y_zero_transform_bypass_flag
        if reader.bit():  # seq_scaling_matrix_present_flag
            count = 8 if chroma_format_idc != 3 else 12
            for i in range(count):
                if reader.bit():  # seq_scaling_list_present_flag[i]
                    size = 16 if i < 6 else 64
                    last = next_scale = 8
                    for _ in range(size):
                        if next_scale != 0:
                            next_scale = (last + reader.se() + 256) % 256
                        last = next_scale or last

    reader.ue()  # log2_max_frame_num_minus4
    pic_order_cnt_type = reader.ue()
    if pic_order_cnt_type == 0:
        reader.ue()  # log2_max_pic_order_cnt_lsb_minus4
    elif pic_order_cnt_type == 1:
        reader.bit()  # delta_pic_order_always_zero_flag
        reader.se()  # offset_for_non_ref_pic
        reader.se()  # offset_for_top_to_bottom_field
        for _ in range(reader.ue()):
            reader.se()

    reader.ue()  # max_num_ref_frames
    reader.bit()  # gaps_in_frame_num_value_allowed_flag
    width_in_mbs = reader.ue() + 1
    height_in_map_units = reader.ue() + 1
    frame_mbs_only_flag = reader.bit()
    if not frame_mbs_only_flag:
        reader.bit()  # mb_adaptive_frame_field_flag
    reader.bit()  # direct_8x8_inference_flag

    crop_left = crop_right = crop_top = crop_bottom = 0
    if reader.bit():  # frame_cropping_flag
        crop_left = reader.ue()
        crop_right = reader.ue()
        crop_top = reader.ue()
        crop_bottom = reader.ue()

    width = width_in_mbs * 16
    height = (2 - frame_mbs_only_flag) * height_in_map_units * 16

    # Cropping is counted in chroma samples, which are subsampled except in
    # 4:4:4.
    sub_width = 1 if chroma_format_idc == 3 else 2
    sub_height = (1 if chroma_format_idc == 3 else 2) * (2 - frame_mbs_only_flag)
    width -= (crop_left + crop_right) * sub_width
    height -= (crop_top + crop_bottom) * sub_height
    return width, height


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
