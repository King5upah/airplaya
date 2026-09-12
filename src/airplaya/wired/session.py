"""The conversation with the phone once the AV endpoints are open.

The phone will not send a single frame until the handshake has run, and the
handshake is mostly about clocks: it hands us references to its own audio and
video clocks, asks us for ours, then asks what time it is on them. Those
answers are how it decides when to present each frame, so a receiver that
replies with nonsense gets a stream that stutters or stops.

```
phone                                   us
  PING                       ─────>
                             <─────     PING
  SYNC CWPA (audio clock)    ─────>
                             <─────     ASYN HPD1 (start video, screen size)
                             <─────     RPLY (our audio clock)
                             <─────     ASYN HPA1 (start audio, format)
  SYNC AFMT (audio format)   ─────>
                             <─────     RPLY (Error: 0)
  SYNC CVRP (video clock)    ─────>
                             <─────     ASYN NEED (send video)
                             <─────     RPLY (our video clock)
  SYNC CLOK / TIME / SKEW    ─────>     RPLY ...
  ASYN FEED (video sample)   ─────>     ASYN NEED after every one
  ASYN EAT! (audio sample)   ─────>
```

Video arrives as length-prefixed NAL units with the parameter sets in a format
description, which is the same shape the Wi-Fi path produces after decryption —
so from `to_annex_b` onwards the two paths share everything, including the
recorder tap. Audio arrives as plain 48 kHz stereo PCM, already decoded.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from airplaya.log import TRACE, get_logger
from airplaya.sink.base import VideoSink
from airplaya.stream import nal
from airplaya.stream.nal import START_CODE
from airplaya.wired import packets
from airplaya.wired.coremedia import CM_TIME_ROUNDED, NANOSECOND_SCALE, CMTime
from airplaya.wired.packets import PacketError

log = get_logger(__name__)

# How long to wait for the phone to acknowledge the shutdown before giving up
# and closing the endpoints anyway.
_RELEASE_TIMEOUT = 3.0


class _Clock:
    """A monotonic clock with a CoreMedia-shaped reading.

    Wall-clock time is wrong here: it can step backwards, and the phone uses
    the differences between readings to pace the stream.
    """

    def __init__(self, reference: int, scale: int = NANOSECOND_SCALE) -> None:
        self.reference = reference
        self.scale = scale
        self._started = time.monotonic_ns()

    def now(self) -> CMTime:
        elapsed = time.monotonic_ns() - self._started
        if self.scale != NANOSECOND_SCALE:
            elapsed = int(elapsed * self.scale / NANOSECOND_SCALE)
        return CMTime(value=elapsed, scale=self.scale, flags=CM_TIME_ROUNDED)


def _skew(start_local: CMTime, last_local: CMTime, start_device: CMTime, last_device: CMTime) -> float:
    """How fast our audio clock runs compared with the phone's.

    The answer is expressed in the phone's own timescale: equal rates give back
    its sample rate, a larger number means our clock is slower. The phone uses
    it to resample, so an honest measurement is better than a constant.
    """
    local_elapsed = last_local.value - start_local.value
    device_elapsed = last_device.value - start_device.value
    if device_elapsed <= 0 or local_elapsed <= 0 or not start_local.scale:
        return float(start_device.scale or 48000)
    scaled = local_elapsed * (start_device.scale / start_local.scale)
    return start_device.scale * scaled / device_elapsed


class WiredSession:
    """Drives one screen-mirroring session over the cable."""

    def __init__(
        self,
        write: Callable[[bytes], None],
        sink: VideoSink,
        audio=None,
        recorder=None,
        display_size: tuple[int, int] = (1920, 1080),
        name: str = "airplaya",
    ) -> None:
        self._write = write
        self._sink = sink
        self._audio = audio
        self._recorder = recorder
        self._display_size = display_size
        self._name = name

        self._lock = threading.Lock()
        self._released = threading.Semaphore(0)

        self._device_audio_clock = 0
        self._need_message: bytes | None = None
        self._clock: _Clock | None = None
        self._audio_clock: _Clock | None = None

        self._codec: str | None = None
        self._geometry: tuple[int, int] | None = None
        self._parameter_sets: bytes | None = None
        self._audio_started = False

        # Kept for the skew answer.
        self._audio_start_device: CMTime | None = None
        self._audio_start_local: CMTime | None = None
        self._audio_last_device = CMTime()
        self._audio_last_local = CMTime()

        self.video_samples = 0
        self.audio_samples = 0
        self._decode_failures = 0

    @property
    def codec(self) -> str | None:
        return self._codec

    @property
    def streaming(self) -> bool:
        return self._codec is not None

    def set_display_size(self, width: int, height: int) -> None:
        """The size to ask for next time video starts."""
        self._display_size = (width, height)

    # -- inbound ---------------------------------------------------------

    def handle(self, frame: bytes) -> None:
        """Handle one frame from the AV endpoint, length field already stripped."""
        try:
            magic = packets.kind(frame)
        except PacketError as exc:
            log.warning("%s", exc)
            return

        try:
            if magic == packets.PING:
                log.debug("ping")
                self._write(packets.ping())
            elif magic == packets.SYNC:
                self._on_sync(packets.parse_sync(frame))
            elif magic == packets.ASYN:
                self._on_asyn(packets.parse_asyn(frame))
            else:
                log.warning("unknown packet type %r", magic)
        except PacketError as exc:
            log.warning("%s", exc)

    def _on_sync(self, packet) -> None:
        sub = packet.subtype

        if sub == packets.CWPA:
            self._device_audio_clock = packet.device_clock_ref
            clock_ref = packets.audio_clock_ref(packet.device_clock_ref)
            self._audio_clock = _Clock(clock_ref)
            log.info("starting the mirror at %dx%d", *self._display_size)
            self._write(
                packets.start_video(*self._display_size, name=self._name)
            )
            self._write(packets.clock_reply(packet.correlation, clock_ref))
            self._write(packets.start_audio(packet.device_clock_ref, name=self._name))

        elif sub == packets.CVRP:
            self._need_message = packets.need(packet.device_clock_ref)
            self._write(self._need_message)
            self._write(
                packets.clock_reply(
                    packet.correlation, packets.video_clock_ref(packet.device_clock_ref)
                )
            )
            if packet.video_format is not None:
                log.debug(
                    "video format: %s %dx%d",
                    packet.video_format.codec_name,
                    packet.video_format.width,
                    packet.video_format.height,
                )

        elif sub == packets.CLOK:
            clock_ref = packets.derived_clock_ref(packet.clock_ref)
            self._clock = _Clock(clock_ref)
            self._write(packets.clock_reply(packet.correlation, clock_ref))

        elif sub == packets.TIME:
            clock = self._clock
            now = clock.now() if clock is not None else CMTime(flags=CM_TIME_ROUNDED)
            self._write(packets.time_reply(packet.correlation, now))

        elif sub == packets.AFMT:
            if packet.audio_format is not None:
                log.info("audio format: %s", packet.audio_format)
                self._start_audio(packet.audio_format)
            self._write(packets.audio_format_reply(packet.correlation))

        elif sub == packets.SKEW:
            value = 48000.0
            if self._audio_start_device is not None and self._audio_start_local is not None:
                value = _skew(
                    self._audio_start_local,
                    self._audio_last_local,
                    self._audio_start_device,
                    self._audio_last_device,
                )
            log.log(TRACE, "skew: %.3f", value)
            self._write(packets.skew_reply(packet.correlation, value))

        elif sub in (packets.STOP, packets.OG):
            self._write(packets.empty_reply(packet.correlation))

        else:
            log.debug("unhandled SYNC %s", sub.decode("ascii", "replace"))
            self._write(packets.empty_reply(packet.correlation))

    def _on_asyn(self, packet) -> None:
        sub = packet.subtype

        if sub == packets.FEED:
            self._on_video(packet.sample)
            if self._need_message is not None:
                self._write(self._need_message)
        elif sub == packets.EAT:
            self._on_audio(packet.sample)
        elif sub == packets.RELS:
            log.debug("clock %#x released", packet.clock_ref)
            self._released.release()
        elif sub in (packets.SPRP, packets.SRAT, packets.TBAS, packets.TJMP):
            log.log(TRACE, "%s (%d bytes)", sub.decode("ascii", "replace"), len(packet.body))
        else:
            log.debug("unhandled ASYN %s", sub.decode("ascii", "replace"))

    # -- video -----------------------------------------------------------

    def _on_video(self, sample) -> None:
        if sample is None:
            return

        prefix = None
        if sample.format is not None and sample.format.parameter_sets:
            prefix = b"".join(
                START_CODE + parameter_set for parameter_set in sample.format.parameter_sets
            )
            self._maybe_restart(sample.format, prefix)

        if not sample.has_data:
            # A format description with no payload is the phone telling us the
            # stream changed shape; the frames follow.
            return

        if self._codec is None:
            log.info("video arrived before any format description; assuming h264")
            self._start_sink("h264")

        try:
            annex_b, count = nal.to_annex_b(bytearray(sample.sample_data))
        except nal.NalError as exc:
            self._decode_failures += 1
            if self._decode_failures in (1, 50, 500):
                log.error("dropping a video sample (%d so far): %s", self._decode_failures, exc)
            return

        if prefix:
            annex_b = prefix + annex_b

        self.video_samples += 1
        log.log(TRACE, "video sample: %d NAL units, %d bytes", count, len(annex_b))
        self._sink.write(annex_b)

        if self._recorder is not None and self._recorder.recording:
            self._recorder.add_video(annex_b, has_keyframe=prefix is not None)

    def _maybe_restart(self, description, parameter_sets: bytes) -> None:
        """Start or restart the sink when the picture changes shape.

        Rotating the phone changes the frame size mid-stream, and a decoder
        that is already open cannot follow it.
        """
        codec = description.codec_name
        geometry: tuple[int, int] | None = (description.width, description.height)
        if not geometry[0] or not geometry[1]:
            geometry = None
        if geometry is None and codec == "h264":
            try:
                sps = description.parameter_sets[0]
                geometry = nal.h264_dimensions(sps)
            except (IndexError, nal.NalError):
                geometry = None

        changed = parameter_sets != self._parameter_sets
        if codec != self._codec or changed:
            if self._codec is not None and changed:
                previous = "x".join(str(n) for n in self._geometry or ()) or "unknown"
                if geometry and geometry != self._geometry:
                    log.info(
                        "picture size changed from %s to %dx%d; restarting the player",
                        previous,
                        *geometry,
                    )
                else:
                    log.info("stream parameters changed; restarting the player")
            elif geometry:
                log.info("picture size is %dx%d", *geometry)
            self._start_sink(codec)

        self._parameter_sets = parameter_sets
        self._geometry = geometry

    def _start_sink(self, codec: str) -> None:
        self._sink.stop()
        try:
            self._sink.start(codec)
        except RuntimeError as exc:
            log.error("%s", exc)
            return
        self._codec = codec

    # -- audio -----------------------------------------------------------

    def _start_audio(self, audio_format) -> None:
        player = self._audio
        if player is None or self._audio_started:
            return
        starter = getattr(player, "start_pcm", None)
        if starter is None:
            return
        try:
            starter(int(audio_format.sample_rate), audio_format.channels)
            self._audio_started = True
        except Exception as exc:  # noqa: BLE001 - audio is not worth the session
            log.warning("no audio: %s", exc)

    def _on_audio(self, sample) -> None:
        if sample is None:
            return

        # The phone's audio clock reading travels with every sample; it and our
        # own reading are what the skew answer is computed from.
        local = self._audio_clock.now() if self._audio_clock is not None else CMTime()
        if self._audio_start_device is None:
            self._audio_start_device = sample.output_timestamp
            self._audio_start_local = local
        self._audio_last_device = sample.output_timestamp
        self._audio_last_local = local

        if not sample.has_data:
            return

        self.audio_samples += 1
        player = self._audio
        if player is not None and self._audio_started:
            writer = getattr(player, "write_pcm", None)
            if writer is not None:
                writer(sample.sample_data)

        if self._recorder is not None and self._recorder.recording:
            self._recorder.add_audio(sample.sample_data)

    # -- shutdown --------------------------------------------------------

    def close(self) -> None:
        """Ask the phone to stop streaming, then let go.

        Without this the phone keeps the session open and refuses the next one
        until it is unplugged, so the acknowledgements are worth waiting for.
        """
        log.info("telling the phone to stop streaming")
        try:
            if self._device_audio_clock:
                self._write(packets.stop_audio(self._device_audio_clock))
            self._write(packets.stop_video())
        except OSError as exc:
            log.debug("could not send the stop messages: %s", exc)
            return

        deadline = time.monotonic() + _RELEASE_TIMEOUT
        for _ in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._released.acquire(timeout=remaining):
                log.debug("the phone did not acknowledge the stop")
                break

        try:
            self._write(packets.stop_video())
        except OSError:
            pass
