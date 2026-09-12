"""The cable protocol, checked against frames captured from a real iPhone.

The fixtures in `data/wired` are whole frames including their length prefix, so
every one of them is sliced past the first four bytes — that prefix is the
framing the USB reader consumes. Where a fixture is a packet the phone was
*sent*, the test compares our serialiser's output byte for byte: the phone
either accepts a dictionary or ignores it entirely, so "close enough" is not a
useful state to be in.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from airplaya.wired import coremedia, packets
from airplaya.wired.coremedia import AudioFormat, CMTime, Number
from airplaya.wired.session import WiredSession

DATA = Path(__file__).parent / "data" / "wired"


_MAGICS = {packets.PING, packets.SYNC, packets.ASYN, packets.RPLY}


def frame(name: str) -> bytes:
    """A fixture as the reader hands it over: no length prefix.

    Some captures were saved with the prefix and some without, so the first
    four bytes are tested for a packet magic rather than trusted either way.
    """
    raw = (DATA / name).read_bytes()
    if raw[:4][::-1] in _MAGICS:
        return raw
    length = struct.unpack_from("<I", raw, 0)[0]
    assert length == len(raw), f"{name} claims {length} bytes but holds {len(raw)}"
    return raw[4:]


def whole(name: str) -> bytes:
    return (DATA / name).read_bytes()


# -- framing and headers -------------------------------------------------


def test_packet_kinds():
    assert packets.kind(frame("cwpa-request1")) == packets.SYNC
    assert packets.kind(frame("asyn-feed")) == packets.ASYN
    assert packets.subtype(frame("cwpa-request1")) == packets.CWPA
    assert packets.subtype(frame("asyn-feed")) == packets.FEED


def test_ping_is_the_expected_sixteen_bytes():
    assert packets.ping() == bytes.fromhex("10000000676e69700000000001000000")


# -- SYNC ----------------------------------------------------------------


def test_cwpa_carries_the_device_audio_clock():
    packet = packets.parse_sync(frame("cwpa-request1"))
    assert packet.clock_ref == packets.EMPTY_CLOCK
    assert packet.correlation == 0x113573DE0
    assert packet.device_clock_ref == 0x1135A74E0


def test_cwpa_reply_matches_the_captured_reply():
    packet = packets.parse_sync(frame("cwpa-request1"))
    reply = packets.clock_reply(packet.correlation, 0x00007FA66CE20CB0)
    assert reply == whole("cwpa-reply1")


def test_cvrp_carries_the_video_format():
    packet = packets.parse_sync(frame("cvrp-request"))
    assert packet.device_clock_ref != 0
    assert packet.video_format is not None
    assert packet.video_format.codec_name == "h264"
    # Two parameter sets, SPS first: an SPS starts with NAL type 7, a PPS with 8.
    sets = packet.video_format.parameter_sets
    assert len(sets) == 2
    assert sets[0][0] & 0x1F == 7
    assert sets[1][0] & 0x1F == 8


def test_cvrp_reply_matches_the_captured_reply():
    packet = packets.parse_sync(frame("cvrp-request"))
    expected = whole("cvrp-reply")
    # The capture came from a Mac, whose clock reference was a real CoreMedia
    # handle rather than one derived from the phone's, so it is read back out
    # of the reply instead of recomputed.
    clock_ref = struct.unpack_from("<Q", expected, 20)[0]
    assert packets.clock_reply(packet.correlation, clock_ref) == expected
    assert packets.video_clock_ref(packet.device_clock_ref) != packet.device_clock_ref


def test_afmt_describes_lpcm_audio():
    packet = packets.parse_sync(frame("afmt-request"))
    fmt = packet.audio_format
    assert fmt is not None
    assert fmt.sample_rate == 48000
    assert fmt.format_id == b"lpcm"
    assert fmt.channels == 2
    assert fmt.bits_per_channel == 16
    assert str(fmt) == "lpcm 48000 Hz, 2 ch, 16-bit"


def test_afmt_reply_matches_the_captured_reply():
    packet = packets.parse_sync(frame("afmt-request"))
    assert packets.audio_format_reply(packet.correlation) == whole("afmt-reply")


def test_clok_reply_matches_the_captured_reply():
    packet = packets.parse_sync(frame("clok-request"))
    expected = whole("clok-reply")
    # As with CVRP, the captured reply carries the Mac's own clock handle.
    clock_ref = struct.unpack_from("<Q", expected, 20)[0]
    assert packets.clock_reply(packet.correlation, clock_ref) == expected
    assert packets.derived_clock_ref(packet.clock_ref) != packet.clock_ref


def test_time_reply_matches_the_captured_reply():
    packet = packets.parse_sync(frame("time-request1"))
    expected = whole("time-reply1")
    time = CMTime.decode(expected[20:])
    assert packets.time_reply(packet.correlation, time) == expected


def test_skew_reply_matches_the_captured_reply():
    packet = packets.parse_sync(frame("skew-request"))
    expected = whole("skew-reply")
    (skew,) = struct.unpack_from("<d", expected, 20)
    assert packets.skew_reply(packet.correlation, skew) == expected


@pytest.mark.parametrize("name", ["og", "stop"])
def test_empty_replies_match_the_captured_replies(name):
    packet = packets.parse_sync(frame(f"{name}-request"))
    assert packets.empty_reply(packet.correlation) == whole(f"{name}-reply")


def test_a_sync_frame_with_the_wrong_magic_is_rejected():
    bad = bytearray(frame("cwpa-request1"))
    bad[0] = 0x50
    with pytest.raises(packets.PacketError):
        packets.parse_sync(bytes(bad))


def test_a_truncated_frame_is_rejected():
    with pytest.raises(packets.PacketError):
        packets.parse_sync(frame("cwpa-request1")[:12])


# -- ASYN ----------------------------------------------------------------


def test_feed_holds_a_video_sample_with_its_format():
    packet = packets.parse_asyn(frame("asyn-feed"))
    sample = packet.sample
    assert sample is not None
    assert sample.has_data
    assert sample.format is not None
    assert sample.format.width and sample.format.height
    assert len(sample.format.parameter_sets) == 2
    assert sample.output_timestamp.scale == coremedia.NANOSECOND_SCALE
    assert sample.presentation_timestamp is not None


def test_a_feed_sample_converts_to_annex_b():
    from airplaya.stream import nal

    packet = packets.parse_asyn(frame("asyn-feed"))
    annex_b, count = nal.to_annex_b(bytearray(packet.sample.sample_data))
    assert count >= 1
    assert annex_b.startswith(nal.START_CODE)


def test_the_feed_parameter_sets_give_the_picture_size():
    from airplaya.stream import nal

    packet = packets.parse_asyn(frame("asyn-feed"))
    description = packet.sample.format
    width, height = nal.h264_dimensions(description.parameter_sets[0])
    assert (width, height) == (description.width, description.height)


def test_eat_holds_audio_samples():
    packet = packets.parse_asyn(frame("asyn-eat"))
    sample = packet.sample
    assert sample is not None
    assert sample.has_data
    # 48 kHz stereo 16-bit: four bytes a frame, so the payload is a whole
    # number of frames.
    assert len(sample.sample_data) % 4 == 0


def test_need_matches_the_captured_packet():
    expected = whole("asyn-need")
    clock_ref = struct.unpack_from("<Q", expected, 8)[0]
    assert packets.need(clock_ref) == expected


def test_start_video_matches_the_captured_hpd1():
    # The capture claimed a 1920x1200 screen under the name Valeria.
    assert packets.start_video(1920, 1200, name="Valeria") == whole("asyn-hpd1")


def test_start_audio_matches_the_captured_hpa1():
    expected = whole("asyn-hpa1")
    clock_ref = struct.unpack_from("<Q", expected, 8)[0]
    assert packets.start_audio(clock_ref, name="Valeria") == expected


def test_stop_packets_are_twenty_bytes():
    assert len(packets.stop_video()) == 20
    assert len(packets.stop_audio(0x1135A74E0)) == 20
    assert packets.subtype(packets.stop_video()[4:]) == packets.HPD0
    assert packets.subtype(packets.stop_audio(1)[4:]) == packets.HPA0


def test_rels_names_the_released_clock():
    packet = packets.parse_asyn(frame("asyn-rels"))
    assert packet.subtype == packets.RELS
    assert packet.clock_ref != 0


def test_sprp_is_parsed_without_complaint():
    packet = packets.parse_asyn(frame("asyn-sprp"))
    assert packet.subtype == packets.SPRP


# -- serialisation -------------------------------------------------------


def test_dictionaries_round_trip():
    entries = {
        "flag": True,
        "off": False,
        "name": "airplaya",
        "count": Number.int32(7),
        "big": Number.int64(2**40),
        "ratio": Number.float64(0.073),
        "blob": b"\x01\x02\x03",
        "nested": {"inner": True},
    }
    decoded = coremedia.decode_dict(coremedia.encode_dict(entries))
    assert decoded["flag"] is True
    assert decoded["off"] is False
    assert decoded["name"] == "airplaya"
    assert decoded["count"].value == 7
    assert decoded["big"].value == 2**40
    assert decoded["ratio"].value == pytest.approx(0.073)
    assert decoded["blob"] == b"\x01\x02\x03"
    assert decoded["nested"]["inner"] is True


def test_a_block_longer_than_its_buffer_is_rejected():
    with pytest.raises(coremedia.CoreMediaError):
        coremedia.read_header(struct.pack("<I4s", 99, b"tcid"))


def test_a_block_with_the_wrong_magic_names_what_it_found():
    with pytest.raises(coremedia.CoreMediaError, match="found sbuf"):
        coremedia.read_header(coremedia.block(b"sbuf"), coremedia.DICT)


def test_audio_format_round_trips_through_its_wire_form():
    fmt = AudioFormat()
    encoded = fmt.encode()
    # 40 bytes of description, then the sample rate twice more.
    assert len(encoded) == 56
    assert AudioFormat.decode(encoded) == fmt


def test_cmtime_round_trips():
    time = CMTime(value=123456789, scale=1_000_000_000, flags=1, epoch=0)
    assert CMTime.decode(time.encode()) == time
    assert time.seconds == pytest.approx(0.123456789)


def test_avcc_records_yield_sps_then_pps():
    sps = b"\x67\x64\x00\x28"
    pps = b"\x68\xee\x3c\xb0"
    record = (
        bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1])
        + struct.pack(">H", len(sps))
        + sps
        + bytes([1])
        + struct.pack(">H", len(pps))
        + pps
    )
    assert coremedia.decode_avcc(record) == [sps, pps]


def test_an_avcc_record_that_overruns_is_rejected():
    record = bytes([1, 0x64, 0, 0x28, 0xFF, 0xE1]) + struct.pack(">H", 400)
    with pytest.raises(coremedia.CoreMediaError):
        coremedia.decode_avcc(record)


# -- the session ---------------------------------------------------------


class FakeSink:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.writes: list[bytes] = []
        self.stops = 0

    def start(self, codec: str) -> None:
        self.started.append(codec)

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def stop(self) -> None:
        self.stops += 1

    @property
    def alive(self) -> bool:
        return True


class FakeAudio:
    def __init__(self) -> None:
        self.opened: tuple[int, int] | None = None
        self.written = 0

    def start_pcm(self, rate: int, channels: int) -> None:
        self.opened = (rate, channels)

    def write_pcm(self, pcm: bytes) -> None:
        self.written += len(pcm)

    def stop(self) -> None:
        pass


def make_session(**kwargs):
    sent: list[bytes] = []
    sink = FakeSink()
    session = WiredSession(write=sent.append, sink=sink, **kwargs)
    return session, sink, sent


def sent_subtypes(sent: list[bytes]) -> list[bytes]:
    """The subtype of every ASYN packet that went out."""
    out = []
    for message in sent:
        body = message[4:]
        if len(body) >= 16 and packets.kind(body) == packets.ASYN:
            out.append(packets.subtype(body))
    return out


def test_a_ping_is_answered_with_a_ping():
    session, _, sent = make_session()
    session.handle(b"gnip" + struct.pack("<Q", 0x0000000100000000))
    assert sent == [packets.ping()]


def test_cwpa_starts_video_and_audio():
    session, _, sent = make_session(display_size=(2560, 1440), name="airplaya")
    session.handle(frame("cwpa-request1"))

    assert sent_subtypes(sent) == [packets.HPD1, packets.HPA1]
    # The middle message is the clock reply, which is not an ASYN packet.
    assert len(sent) == 3
    # The screen we asked for is the one that was configured.
    hpd1 = coremedia.decode_dict(sent[0][20:])
    assert hpd1["DisplaySize"]["Width"].value == 2560
    assert hpd1["DisplaySize"]["Height"].value == 1440


def test_cvrp_asks_for_video_and_answers_with_a_clock():
    session, _, sent = make_session()
    session.handle(frame("cvrp-request"))
    assert sent_subtypes(sent) == [packets.NEED]
    assert packets.kind(sent[1][4:]) == packets.RPLY


def test_afmt_opens_the_audio_device_once():
    audio = FakeAudio()
    session, _, sent = make_session(audio=audio)
    session.handle(frame("afmt-request"))
    session.handle(frame("afmt-request"))
    assert audio.opened == (48000, 2)


def test_a_feed_sample_reaches_the_sink_with_its_parameter_sets():
    session, sink, _ = make_session()
    session.handle(frame("asyn-feed"))

    assert sink.started == ["h264"]
    assert len(sink.writes) == 1
    # Parameter sets are prepended so the decoder can start from this frame:
    # a start code, then a NAL whose type is 7, the SPS.
    written = sink.writes[0]
    assert written.startswith(b"\x00\x00\x00\x01")
    assert written[4] & 0x1F == 7
    assert session.video_samples == 1


def test_every_feed_is_followed_by_a_need():
    session, _, sent = make_session()
    session.handle(frame("cvrp-request"))
    sent.clear()
    session.handle(frame("asyn-feed"))
    assert sent_subtypes(sent) == [packets.NEED]


def test_audio_samples_reach_the_player():
    audio = FakeAudio()
    session, _, _ = make_session(audio=audio)
    session.handle(frame("afmt-request"))
    session.handle(frame("asyn-eat"))
    assert session.audio_samples == 1
    assert audio.written > 0


def test_a_stop_request_is_acknowledged():
    session, _, sent = make_session()
    session.handle(frame("stop-request"))
    assert len(sent) == 1
    assert packets.kind(sent[0][4:]) == packets.RPLY


def test_closing_asks_the_phone_to_stop():
    session, _, sent = make_session()
    session.handle(frame("cwpa-request1"))
    sent.clear()
    session.close()
    # Audio off, video off, and video off again once the phone has answered or
    # the wait has run out.
    assert sent_subtypes(sent) == [packets.HPA0, packets.HPD0, packets.HPD0]


def test_a_rotation_restarts_the_sink():
    session, sink, _ = make_session()
    session.handle(frame("asyn-feed"))
    assert sink.started == ["h264"]

    # Same frame, but with the parameter sets altered so they look like a new
    # geometry: the sink has to be restarted for the new decoder state.
    session._parameter_sets = b"different"
    session.handle(frame("asyn-feed"))
    assert sink.started == ["h264", "h264"]
    assert sink.stops >= 2
