"""The serialisation CoreMedia uses on the wire, in both directions.

Everything in this protocol is a length-prefixed, magic-tagged block:

```
length  uint32   bytes of this block, including these 8
magic   uint32   four ASCII characters, little-endian ("dict" reads as "tcid")
body    length-8 bytes
```

Blocks nest, which is all a dictionary is: a `dict` block holding `keyv` blocks,
each holding a key block and a value block. Numbers are `NSNumber`s with a
one-byte type tag, times are 24-byte `CMTime` structs, and a media sample is an
`sbuf` block with the encoded frame in its `sdat` child.

Byte order is little-endian throughout, including the magics — which is why
every constant here is written as the ASCII it actually spells.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

# -- block magics --------------------------------------------------------

DICT = b"dict"
KEY_VALUE = b"keyv"
STRING_KEY = b"strk"
INDEX_KEY = b"idxk"
BOOL_VALUE = b"bulv"
STRING_VALUE = b"strv"
DATA_VALUE = b"datv"
NUMBER_VALUE = b"nmbv"

FORMAT_DESCRIPTION = b"fdsc"
MEDIA_TYPE = b"mdia"
VIDEO_DIMENSION = b"vdim"
CODEC = b"codc"
EXTENSIONS = b"extn"
AUDIO_DESCRIPTION = b"asbd"

MEDIA_VIDEO = b"vide"
MEDIA_SOUND = b"soun"

SAMPLE_BUFFER = b"sbuf"
OUTPUT_TIMESTAMP = b"opts"
TIMING_INFO = b"stia"
SAMPLE_DATA = b"sdat"
SAMPLE_COUNT = b"nsmp"
SAMPLE_SIZES = b"ssiz"
ATTACHMENTS = b"satt"
SAMPLE_ARRAY = b"sary"

CODEC_AVC1 = b"avc1"
CODEC_HVC1 = b"hvc1"

CM_TIME_SIZE = 24
NANOSECOND_SCALE = 1_000_000_000
# The flag iOS sets on the times it sends, and the one it expects back.
CM_TIME_ROUNDED = 0x1


class CoreMediaError(ValueError):
    """A block is malformed, or is not the block we were expecting."""


def _magic_text(raw: bytes) -> str:
    text = raw[::-1].decode("ascii", "replace")
    return text


def read_header(data: bytes, expected: bytes | None = None) -> tuple[int, bytes]:
    """Read a block header. Returns its length and its body.

    `expected` is the magic spelled in reading order (`b"dict"`); on the wire it
    is reversed, and the mismatch message says which one actually turned up,
    because that is the only useful thing to know when a parse goes wrong.
    """
    if len(data) < 8:
        raise CoreMediaError(f"a block needs 8 bytes of header, got {len(data)}")
    length, magic = struct.unpack_from("<I4s", data, 0)
    if length < 8 or length > len(data):
        raise CoreMediaError(
            f"block claims {length} bytes but {len(data)} are available"
        )
    if expected is not None and magic != expected[::-1]:
        raise CoreMediaError(
            f"expected a {expected.decode()} block, found {_magic_text(magic)}"
        )
    return length, data[8:length]


def peek_magic(data: bytes) -> bytes:
    """The magic of the block at `data`, spelled in reading order."""
    if len(data) < 8:
        raise CoreMediaError(f"a block needs 8 bytes of header, got {len(data)}")
    return data[4:8][::-1]


def block(magic: bytes, body: bytes = b"") -> bytes:
    """Wrap `body` in a length-prefixed block tagged with `magic`."""
    return struct.pack("<I4s", len(body) + 8, magic[::-1]) + body


# -- NSNumber ------------------------------------------------------------

_INT32 = 3
_INT64 = 4
_FLOAT64 = 6


@dataclass(frozen=True)
class Number:
    """An NSNumber: a type tag and a value. Type 5 turns up too, as an int32."""

    value: int | float
    tag: int = _INT32

    @staticmethod
    def int32(value: int) -> "Number":
        return Number(int(value), _INT32)

    @staticmethod
    def int64(value: int) -> "Number":
        return Number(int(value), _INT64)

    @staticmethod
    def float64(value: float) -> "Number":
        return Number(float(value), _FLOAT64)

    def encode(self) -> bytes:
        if self.tag == _FLOAT64:
            return struct.pack("<Bd", self.tag, float(self.value))
        if self.tag == _INT64:
            return struct.pack("<BQ", self.tag, int(self.value))
        return struct.pack("<BI", self.tag, int(self.value))

    @staticmethod
    def decode(body: bytes) -> "Number":
        if not body:
            raise CoreMediaError("an NSNumber needs at least a type tag")
        tag = body[0]
        if tag in (_INT32, 5) and len(body) >= 5:
            return Number(struct.unpack_from("<I", body, 1)[0], tag)
        if tag == _INT64 and len(body) >= 9:
            return Number(struct.unpack_from("<Q", body, 1)[0], tag)
        if tag == _FLOAT64 and len(body) >= 9:
            return Number(struct.unpack_from("<d", body, 1)[0], tag)
        raise CoreMediaError(f"NSNumber type {tag} with {len(body)} bytes of body")


# -- CMTime --------------------------------------------------------------


@dataclass(frozen=True)
class CMTime:
    value: int = 0
    scale: int = NANOSECOND_SCALE
    flags: int = 0
    epoch: int = 0

    @property
    def seconds(self) -> float:
        return self.value / self.scale if self.scale else 0.0

    def encode(self) -> bytes:
        return struct.pack(
            "<QIIQ", self.value & 0xFFFFFFFFFFFFFFFF, self.scale, self.flags, self.epoch
        )

    @staticmethod
    def decode(data: bytes) -> "CMTime":
        if len(data) < CM_TIME_SIZE:
            raise CoreMediaError(f"a CMTime is {CM_TIME_SIZE} bytes, got {len(data)}")
        value, scale, flags, epoch = struct.unpack_from("<QIIQ", data, 0)
        return CMTime(value, scale, flags, epoch)


# -- dictionaries --------------------------------------------------------


def encode_dict(entries: dict[str, Any]) -> bytes:
    """Serialise a string-keyed dictionary.

    Values may be `bool`, `str`, `bytes`, `Number`, or a nested dict. Anything
    else is a programming error rather than bad input, so it raises.
    """
    body = bytearray()
    for key, value in entries.items():
        pair = block(STRING_KEY, key.encode("utf-8")) + _encode_value(value)
        body += block(KEY_VALUE, pair)
    return block(DICT, bytes(body))


def _encode_value(value: Any) -> bytes:
    if isinstance(value, bool):
        # One byte of payload, not four: the length field says 9.
        return block(BOOL_VALUE, b"\x01" if value else b"\x00")
    if isinstance(value, Number):
        return block(NUMBER_VALUE, value.encode())
    if isinstance(value, str):
        return block(STRING_VALUE, value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return block(DATA_VALUE, bytes(value))
    if isinstance(value, dict):
        return encode_dict(value)
    raise TypeError(f"cannot serialise {type(value).__name__} into a CoreMedia dict")


def decode_dict(data: bytes, magic: bytes = DICT) -> dict[Any, Any]:
    """Parse a dictionary. Keys come back as `str` or `int` as tagged."""
    _, body = read_header(data, magic)
    entries: dict[Any, Any] = {}
    offset = 0
    while offset < len(body):
        pair_length, pair = read_header(body[offset:], KEY_VALUE)
        key, value = _decode_pair(pair)
        entries[key] = value
        offset += pair_length
    return entries


def _decode_pair(pair: bytes) -> tuple[Any, Any]:
    key_length, key_body = read_header(pair)
    kind = peek_magic(pair)
    if kind == STRING_KEY:
        key: Any = key_body.decode("utf-8", "replace")
    elif kind == INDEX_KEY:
        if len(key_body) < 2:
            raise CoreMediaError("an index key needs 2 bytes")
        key = struct.unpack_from("<H", key_body, 0)[0]
    else:
        raise CoreMediaError(f"unknown key type {kind.decode()}")
    return key, decode_value(pair[key_length:])


def decode_value(data: bytes) -> Any:
    """Parse one value block."""
    magic = peek_magic(data)
    _, body = read_header(data)
    if magic == STRING_VALUE:
        return body.decode("utf-8", "replace")
    if magic == DATA_VALUE:
        return body
    if magic == BOOL_VALUE:
        return bool(body and body[0] == 1)
    if magic == NUMBER_VALUE:
        return Number.decode(body)
    if magic == DICT:
        return decode_dict(data)
    if magic == FORMAT_DESCRIPTION:
        return decode_format_description(data)
    # Unknown values are kept as raw bytes rather than failing the whole
    # dictionary: the parts we do understand are still worth having.
    return body


# -- format description --------------------------------------------------


@dataclass
class AudioFormat:
    """An AudioStreamBasicDescription, as iOS sends it for the mirrored audio."""

    sample_rate: float = 48000.0
    format_id: bytes = b"lpcm"
    format_flags: int = 12
    bytes_per_packet: int = 4
    frames_per_packet: int = 1
    bytes_per_frame: int = 4
    channels: int = 2
    bits_per_channel: int = 16
    reserved: int = 0

    _STRUCT = struct.Struct("<d4sIIIIIII")

    @staticmethod
    def decode(data: bytes) -> "AudioFormat":
        if len(data) < AudioFormat._STRUCT.size:
            raise CoreMediaError(
                f"an audio description is {AudioFormat._STRUCT.size} bytes, got {len(data)}"
            )
        fields = AudioFormat._STRUCT.unpack_from(data, 0)
        return AudioFormat(
            sample_rate=fields[0],
            format_id=fields[1][::-1],
            format_flags=fields[2],
            bytes_per_packet=fields[3],
            frames_per_packet=fields[4],
            bytes_per_frame=fields[5],
            channels=fields[6],
            bits_per_channel=fields[7],
            reserved=fields[8],
        )

    def encode(self) -> bytes:
        """The 56-byte form the phone expects in an HPA1 dictionary.

        The description itself is 40 bytes; iOS sends the sample rate twice
        more after it, and rejects the packet without them.
        """
        head = self._STRUCT.pack(
            self.sample_rate,
            self.format_id[::-1],
            self.format_flags,
            self.bytes_per_packet,
            self.frames_per_packet,
            self.bytes_per_frame,
            self.channels,
            self.bits_per_channel,
            self.reserved,
        )
        return head + struct.pack("<dd", self.sample_rate, self.sample_rate)

    def __str__(self) -> str:
        return (
            f"{self.format_id.decode('ascii', 'replace')} "
            f"{self.sample_rate:.0f} Hz, {self.channels} ch, "
            f"{self.bits_per_channel}-bit"
        )


@dataclass
class FormatDescription:
    media_type: bytes = MEDIA_VIDEO
    width: int = 0
    height: int = 0
    codec: bytes = CODEC_AVC1
    parameter_sets: list[bytes] = field(default_factory=list)
    audio: AudioFormat | None = None

    @property
    def codec_name(self) -> str:
        """The codec as PyAV and ffmpeg name it."""
        return "hevc" if self.codec == CODEC_HVC1 else "h264"


def decode_format_description(data: bytes) -> FormatDescription:
    """Parse an `fdsc` block: dimensions, codec, and the parameter sets."""
    _, body = read_header(data, FORMAT_DESCRIPTION)

    length, media_body = read_header(body, MEDIA_TYPE)
    media_type = media_body[:4][::-1]
    rest = body[length:]

    if media_type == MEDIA_SOUND:
        _, audio_body = read_header(rest, AUDIO_DESCRIPTION)
        return FormatDescription(
            media_type=MEDIA_SOUND, audio=AudioFormat.decode(audio_body)
        )

    length, dimension = read_header(rest, VIDEO_DIMENSION)
    width, height = struct.unpack_from("<II", dimension, 0)
    rest = rest[length:]

    length, codec_body = read_header(rest, CODEC)
    codec = codec_body[:4][::-1]
    rest = rest[length:]

    extensions = decode_dict(rest, EXTENSIONS) if rest else {}
    return FormatDescription(
        media_type=MEDIA_VIDEO,
        width=width,
        height=height,
        codec=codec,
        parameter_sets=_parameter_sets(extensions),
    )


def _parameter_sets(extensions: dict[Any, Any]) -> list[bytes]:
    """Pull SPS and PPS out of the extensions, which hold an avcC record.

    The record lives two index-keyed dictionaries deep, under key 49 then key
    105. What it holds is the standard AVC decoder configuration, so it is
    parsed as one rather than at the fixed offsets that happen to work for a
    single-SPS stream.
    """
    nested = extensions.get(49)
    if not isinstance(nested, dict):
        return []
    record = nested.get(105)
    if not isinstance(record, (bytes, bytearray)):
        return []
    try:
        return decode_avcc(bytes(record))
    except CoreMediaError:
        return []


def decode_avcc(record: bytes) -> list[bytes]:
    """Read the SPS and PPS NAL units out of an avcC configuration record.

    Layout: one byte of version, three of profile/level, one byte whose low two
    bits give the NAL length size, then a count and a 16-bit length per
    parameter set — SPS first, then PPS.
    """
    if len(record) < 7:
        raise CoreMediaError(f"an avcC record needs 7 bytes, got {len(record)}")
    if record[0] != 1:
        raise CoreMediaError(f"avcC version {record[0]} is not supported")

    sets: list[bytes] = []
    offset = 5
    for _ in range(2):  # the SPS array, then the PPS array
        if offset >= len(record):
            break
        count = record[offset] & 0x1F
        offset += 1
        for _ in range(count):
            if offset + 2 > len(record):
                raise CoreMediaError("avcC parameter set length is truncated")
            (length,) = struct.unpack_from(">H", record, offset)
            offset += 2
            if offset + length > len(record):
                raise CoreMediaError("avcC parameter set overruns the record")
            sets.append(record[offset : offset + length])
            offset += length
    return sets


# -- sample buffers ------------------------------------------------------


@dataclass
class SampleBuffer:
    """One media sample: the encoded bytes, when it was captured, and format."""

    output_timestamp: CMTime = field(default_factory=CMTime)
    presentation_timestamp: CMTime | None = None
    sample_data: bytes = b""
    sample_count: int = 0
    sample_sizes: list[int] = field(default_factory=list)
    format: FormatDescription | None = None
    attachments: dict[Any, Any] = field(default_factory=dict)

    @property
    def has_data(self) -> bool:
        return bool(self.sample_data)


def decode_sample_buffer(data: bytes) -> SampleBuffer:
    """Parse an `sbuf` block.

    The children arrive in no guaranteed order and any of them may be absent —
    a buffer with a format description and no data is how the phone announces a
    format change, and an empty buffer is how it marks a gap.
    """
    _, body = read_header(data, SAMPLE_BUFFER)
    buffer = SampleBuffer()

    offset = 0
    while offset < len(body):
        magic = peek_magic(body[offset:])
        length, child = read_header(body[offset:])

        if magic == OUTPUT_TIMESTAMP:
            buffer.output_timestamp = CMTime.decode(child)
        elif magic == TIMING_INFO:
            # Duration, presentation and decode times per sample; the
            # presentation time of the first is the one worth keeping.
            if len(child) >= 2 * CM_TIME_SIZE:
                buffer.presentation_timestamp = CMTime.decode(child[CM_TIME_SIZE:])
        elif magic == SAMPLE_DATA:
            buffer.sample_data = child
        elif magic == SAMPLE_COUNT:
            if len(child) >= 4:
                buffer.sample_count = struct.unpack_from("<I", child, 0)[0]
        elif magic == SAMPLE_SIZES:
            buffer.sample_sizes = [
                size for (size,) in struct.iter_unpack("<I", child[: len(child) // 4 * 4])
            ]
        elif magic == FORMAT_DESCRIPTION:
            buffer.format = decode_format_description(body[offset : offset + length])
        elif magic == ATTACHMENTS:
            buffer.attachments = decode_dict(body[offset : offset + length], ATTACHMENTS)
        elif magic == SAMPLE_ARRAY:
            pass  # a flag array we do not act on
        # Unknown children are skipped: the length field makes that safe.

        offset += length
    return buffer
