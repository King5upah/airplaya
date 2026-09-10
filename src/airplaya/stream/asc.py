"""AudioSpecificConfig, the four bytes a decoder needs before it can decode.

The phone sends bare AAC access units. Nothing in them says which AAC flavour,
sample rate or channel count applies — that lives in the AudioSpecificConfig,
which AirPlay never transmits, because both ends already know the format from
the SETUP handshake. So we build it from the `ct` and `spf` values the client
announced and hand it to the decoder as extradata.

For AAC-LC this configuration could travel in an ADTS header instead. Mirroring
uses AAC-ELD, which ADTS cannot describe, so extradata is the only route.

Reference: ISO/IEC 14496-3, §1.6.2.1 (AudioSpecificConfig) and §4.6.20
(ELDSpecificConfig).
"""

from __future__ import annotations

# Sampling frequency index table, ISO/IEC 14496-3 Table 1.18.
_SAMPLE_RATES = [
    96000, 88200, 64000, 48000, 44100, 32000,
    24000, 22050, 16000, 12000, 11025, 8000, 7350,
]

AOT_AAC_LC = 2
AOT_AAC_ELD = 39

# ELD frame lengths, selected by a single flag.
_ELD_FRAME_LENGTHS = {512: 0, 480: 1}


class _BitWriter:
    def __init__(self) -> None:
        self._bits: list[int] = []

    def write(self, value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            self._bits.append((value >> shift) & 1)

    @property
    def bit_count(self) -> int:
        return len(self._bits)

    def to_bytes(self) -> bytes:
        bits = self._bits + [0] * (-len(self._bits) % 8)
        out = bytearray(len(bits) // 8)
        for index, bit in enumerate(bits):
            if bit:
                out[index // 8] |= 0x80 >> (index % 8)
        return bytes(out)


def sample_rate_index(sample_rate: int) -> int:
    try:
        return _SAMPLE_RATES.index(sample_rate)
    except ValueError as exc:
        raise ValueError(f"{sample_rate} Hz has no AAC sampling frequency index") from exc


def audio_specific_config(
    object_type: int = AOT_AAC_ELD,
    sample_rate: int = 44100,
    channels: int = 2,
    frame_length: int = 480,
) -> bytes:
    """Build the AudioSpecificConfig for one AAC format."""
    writer = _BitWriter()

    if object_type >= 31:
        # Object types past 30 use an escape value plus a 6-bit extension.
        writer.write(31, 5)
        writer.write(object_type - 32, 6)
    else:
        writer.write(object_type, 5)

    writer.write(sample_rate_index(sample_rate), 4)
    writer.write(channels, 4)

    if object_type == AOT_AAC_ELD:
        try:
            frame_length_flag = _ELD_FRAME_LENGTHS[frame_length]
        except KeyError as exc:
            raise ValueError(
                f"AAC-ELD frames are 480 or 512 samples, not {frame_length}"
            ) from exc
        writer.write(frame_length_flag, 1)
        # No error resilience and no low-delay SBR layer in an AirPlay stream.
        writer.write(0, 1)  # aacSectionDataResilienceFlag
        writer.write(0, 1)  # aacScalefactorDataResilienceFlag
        writer.write(0, 1)  # aacSpectralDataResilienceFlag
        writer.write(0, 1)  # ldSbrPresentFlag
        writer.write(0, 4)  # ELDEXT_TERM
    else:
        # GASpecificConfig, for the plain AAC object types.
        writer.write(0, 1)  # frameLengthFlag: 1024 samples
        writer.write(0, 1)  # dependsOnCoreCoder
        writer.write(0, 1)  # extensionFlag

    return writer.to_bytes()
