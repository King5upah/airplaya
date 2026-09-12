"""The packet layer: PING, SYNC/RPLY, and ASYN.

Every frame on the AV endpoints is a length-prefixed block whose magic says how
to read the rest:

```
PING   a keepalive; answered with an identical one
SYNC   a request that must be answered by an RPLY carrying the same
       correlation id — this is how clocks are handed out
ASYN   an unsolicited message in either direction, including the media itself
```

A SYNC or ASYN packet names the clock it concerns with an 8-byte reference.
Those references are opaque handles that CoreMedia hands out on the phone; what
matters is quoting the right one back, because the phone routes on it. The
values we invent for our own clocks are derived from the phone's by adding a
constant — arbitrary, but it keeps them distinct and makes a packet capture
readable, which is why those constants are the ones a Mac uses.

The reader strips the 4-byte length before handing a frame over, so offsets
here start at the magic.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from airplaya.wired.coremedia import (
    AudioFormat,
    CMTime,
    CoreMediaError,
    FormatDescription,
    Number,
    SampleBuffer,
    decode_dict,
    decode_format_description,
    decode_sample_buffer,
    encode_dict,
)

PING = b"ping"
SYNC = b"sync"
ASYN = b"asyn"
RPLY = b"rply"

# SYNC subtypes
CWPA = b"cwpa"  # the phone's audio clock, and a request for ours
CVRP = b"cvrp"  # the phone's video clock, with the video format
CLOK = b"clok"  # make us a clock
TIME = b"time"  # what time is it on that clock
SKEW = b"skew"  # how far apart are our audio clocks running
AFMT = b"afmt"  # the audio format it intends to send
STOP = b"stop"  # stop that clock
OG = b"go! "  # purpose unknown; it wants an empty reply

# ASYN subtypes
FEED = b"feed"  # a video sample
EAT = b"eat!"  # an audio sample
SPRP = b"sprp"  # set property
SRAT = b"srat"  # set rate and anchor time
TBAS = b"tbas"  # set time base
TJMP = b"tjmp"  # the time base jumped
RELS = b"rels"  # that clock is released
NEED = b"need"  # we want more video
HPD1 = b"hpd1"  # start video, here is the screen we have
HPA1 = b"hpa1"  # start audio, here is the format we want
HPD0 = b"hpd0"  # stop video
HPA0 = b"hpa0"  # stop audio

# The reference used when a packet concerns no clock yet.
EMPTY_CLOCK = 0x1

# Offsets from these constants keep our clock references distinct from the
# phone's without any allocation scheme.
_AUDIO_CLOCK_OFFSET = 1000
_VIDEO_CLOCK_OFFSET = 0x1000AF
_CLOCK_OFFSET = 0x10000

_HEADER = struct.Struct("<4sQ4s")  # magic, clock reference, subtype


class PacketError(ValueError):
    """A frame is too short, or is not the packet type it claims to be."""


def kind(frame: bytes) -> bytes:
    """The packet magic of a frame, spelled in reading order."""
    if len(frame) < 4:
        raise PacketError(f"a frame needs 4 bytes of magic, got {len(frame)}")
    return frame[:4][::-1]


def subtype(frame: bytes) -> bytes:
    """The subtype of a SYNC or ASYN frame, spelled in reading order."""
    if len(frame) < 16:
        raise PacketError(f"a typed frame needs 16 bytes of header, got {len(frame)}")
    return frame[12:16][::-1]


def _parse_header(frame: bytes, expected_magic: bytes) -> tuple[int, bytes]:
    magic, clock_ref, sub = _HEADER.unpack_from(frame, 0)
    if magic != expected_magic[::-1]:
        raise PacketError(
            f"expected {expected_magic.decode()}, found {magic[::-1].decode('ascii', 'replace')}"
        )
    return clock_ref, sub[::-1]


def _frame(magic: bytes, clock_ref: int, sub: bytes, body: bytes = b"") -> bytes:
    """Build an outgoing packet, length field included."""
    payload = _HEADER.pack(magic[::-1], clock_ref, sub[::-1]) + body
    return struct.pack("<I", len(payload) + 4) + payload


# -- PING ----------------------------------------------------------------

# The phone's ping carries 0x0000000100000000 after the magic and wants the
# same back; the value never varies, so it is a constant rather than an echo.
_PING_BODY = struct.pack("<Q", 0x0000000100000000)


def ping() -> bytes:
    return struct.pack("<I4s", 16, PING[::-1]) + _PING_BODY


# -- SYNC ----------------------------------------------------------------


@dataclass
class SyncPacket:
    """A request from the phone. `correlation` must come back in the reply."""

    subtype: bytes
    clock_ref: int
    correlation: int
    body: bytes

    # Set for the subtypes that carry one.
    device_clock_ref: int = 0
    payload: dict | None = None
    audio_format: AudioFormat | None = None
    video_format: FormatDescription | None = None
    unknown: int = 0


def parse_sync(frame: bytes) -> SyncPacket:
    if len(frame) < 24:
        raise PacketError(f"a SYNC frame needs 24 bytes, got {len(frame)}")
    clock_ref, sub = _parse_header(frame, SYNC)
    (correlation,) = struct.unpack_from("<Q", frame, 16)
    body = frame[24:]
    packet = SyncPacket(
        subtype=sub, clock_ref=clock_ref, correlation=correlation, body=body
    )

    try:
        if sub in (CWPA, CVRP):
            if len(body) < 8:
                raise PacketError(f"{sub.decode()} carries no clock reference")
            (packet.device_clock_ref,) = struct.unpack_from("<Q", body, 0)
            if sub == CVRP and len(body) > 8:
                packet.payload = decode_dict(body[8:])
                packet.video_format = _video_format(packet.payload)
        elif sub == AFMT:
            packet.audio_format = AudioFormat.decode(body)
        elif sub == OG and len(body) >= 4:
            (packet.unknown,) = struct.unpack_from("<I", body, 0)
    except CoreMediaError as exc:
        raise PacketError(f"could not parse the {sub.decode()} payload: {exc}") from exc
    return packet


def _video_format(payload: dict) -> FormatDescription | None:
    """The video format description CVRP hides inside its dictionary."""
    for value in payload.values():
        if isinstance(value, FormatDescription):
            return value
        if isinstance(value, dict):
            found = _video_format(value)
            if found is not None:
                return found
        if isinstance(value, (bytes, bytearray)) and len(value) > 8:
            try:
                return decode_format_description(bytes(value))
            except CoreMediaError:
                continue
    return None


def _reply(correlation: int, body: bytes = b"") -> bytes:
    payload = struct.pack("<4sQI", RPLY[::-1], correlation, 0) + body
    return struct.pack("<I", len(payload) + 4) + payload


def clock_reply(correlation: int, clock_ref: int) -> bytes:
    """The answer to CWPA, CVRP and CLOK: here is a clock of ours."""
    return _reply(correlation, struct.pack("<Q", clock_ref))


def time_reply(correlation: int, time: CMTime) -> bytes:
    return _reply(correlation, time.encode())


def skew_reply(correlation: int, skew: float) -> bytes:
    return _reply(correlation, struct.pack("<d", skew))


def empty_reply(correlation: int) -> bytes:
    """The answer to OG and STOP: acknowledged, nothing to say."""
    return _reply(correlation, struct.pack("<I", 0))


def audio_format_reply(correlation: int) -> bytes:
    """The answer to AFMT: a dictionary holding an error code of zero."""
    return _reply(correlation, encode_dict({"Error": Number.int32(0)}))


def audio_clock_ref(device_clock_ref: int) -> int:
    return device_clock_ref + _AUDIO_CLOCK_OFFSET


def video_clock_ref(device_clock_ref: int) -> int:
    return device_clock_ref + _VIDEO_CLOCK_OFFSET


def derived_clock_ref(clock_ref: int) -> int:
    return clock_ref + _CLOCK_OFFSET


# -- ASYN ----------------------------------------------------------------


@dataclass
class AsynPacket:
    subtype: bytes
    clock_ref: int
    body: bytes
    sample: SampleBuffer | None = None


def parse_asyn(frame: bytes) -> AsynPacket:
    if len(frame) < 16:
        raise PacketError(f"an ASYN frame needs 16 bytes, got {len(frame)}")
    clock_ref, sub = _parse_header(frame, ASYN)
    body = frame[16:]
    packet = AsynPacket(subtype=sub, clock_ref=clock_ref, body=body)
    if sub in (FEED, EAT):
        try:
            packet.sample = decode_sample_buffer(body)
        except CoreMediaError as exc:
            raise PacketError(f"could not parse the {sub.decode()} sample: {exc}") from exc
    return packet


def need(device_video_clock_ref: int) -> bytes:
    """Ask for more video. The phone sends nothing until it sees this."""
    return _frame(ASYN, device_video_clock_ref, NEED)


def start_video(width: int, height: int, name: str = "airplaya") -> bytes:
    """HPD1: start mirroring, and here is the screen it is going to.

    The phone renders to the size given here, so this is the one lever on
    quality the wired path has.
    """
    return _frame(
        ASYN,
        EMPTY_CLOCK,
        HPD1,
        encode_dict(
            {
                name: True,
                "HEVCDecoderSupports444": True,
                "DisplaySize": {
                    "Width": Number.float64(width),
                    "Height": Number.float64(height),
                },
            }
        ),
    )


def start_audio(
    device_audio_clock_ref: int,
    audio_format: AudioFormat | None = None,
    name: str = "airplaya",
) -> bytes:
    """HPA1: start the audio, in this format, with this much buffer."""
    fmt = audio_format or AudioFormat()
    return _frame(
        ASYN,
        device_audio_clock_ref,
        HPA1,
        encode_dict(
            {
                "BufferAheadInterval": Number.float64(0.07300000000000001),
                "deviceUID": name,
                "ScreenLatency": Number.float64(0.04),
                "formats": fmt.encode(),
                "EDIDAC3Support": Number.int32(0),
                "deviceName": name,
            }
        ),
    )


def stop_video() -> bytes:
    return _frame(ASYN, EMPTY_CLOCK, HPD0)


def stop_audio(device_audio_clock_ref: int) -> bytes:
    return _frame(ASYN, device_audio_clock_ref, HPA0)
