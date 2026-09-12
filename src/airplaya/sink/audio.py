"""Audio playback: decode in-process, play on a chosen output device.

    AAC frames ──> PyAV (aac decoder + resampler) ──> s16 PCM ──> PortAudio
    PCM (cable) ─────────────────────────────────────────────────┘

PyAV rather than an ffmpeg subprocess because the decoder needs its
AudioSpecificConfig passed as extradata, and the ffmpeg command line has no way
to supply that. PortAudio rather than ffplay because it can be pointed at a
specific Windows output device, which is the whole point of letting the user
choose one.

Over the cable the phone sends plain PCM, so there is nothing to decode; the
buffering and the device handling are worth sharing, which is why the same
player has a `start_pcm` entry point.

Both dependencies are optional. Without them audio is skipped with an
explanation, and mirroring carries on.
"""

from __future__ import annotations

import threading
from typing import Callable

from airplaya.log import get_logger

log = get_logger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 2
_BYTES_PER_FRAME = CHANNELS * 2  # stereo, s16
_BYTES_PER_SAMPLE = 2

# The output runs on PortAudio's own callback thread, fed by a small buffer the
# network thread fills. Writing to the device directly from the network thread
# is what makes mirrored audio sound broken: every decode and every device write
# stalls packet reception, so packets arrive late and the device underruns
# between them.
#
# The buffer is a latency budget. Too small and normal network jitter is
# audible; too large and the audio drifts behind the picture. 120 ms of slack
# with a 40 ms prefill holds up on Wi-Fi without a noticeable offset.
_MAX_BUFFER_MS = 300
_PREFILL_MS = 90


def _ms_to_bytes(milliseconds: int) -> int:
    return int(SAMPLE_RATE * milliseconds / 1000) * _BYTES_PER_FRAME

try:  # pragma: no cover - depends on the environment
    import av
except Exception:  # noqa: BLE001
    av = None

try:  # pragma: no cover - depends on the environment
    import sounddevice
except Exception:  # noqa: BLE001
    sounddevice = None


class AudioUnavailable(RuntimeError):
    """Audio cannot be played, with a message explaining what is missing."""


class AudioDeviceInfo:
    def __init__(self, index: int, name: str, channels: int, host_api: str) -> None:
        self.index = index
        self.name = name
        self.channels = channels
        self.host_api = host_api

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "channels": self.channels,
            "hostApi": self.host_api,
        }


def list_output_devices() -> list[AudioDeviceInfo]:
    """Every output device PortAudio can see.

    An empty list means PortAudio is unavailable, which callers present as
    "system default only" rather than as an error.
    """
    if sounddevice is None:
        return []
    try:
        host_apis = sounddevice.query_hostapis()
        devices = sounddevice.query_devices()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not query audio devices: %s", exc)
        return []

    out = []
    for index, device in enumerate(devices):
        if device.get("max_output_channels", 0) <= 0:
            continue
        api_index = device.get("hostapi", 0)
        api_name = ""
        if 0 <= api_index < len(host_apis):
            api_name = host_apis[api_index].get("name", "")
        out.append(
            AudioDeviceInfo(
                index=index,
                name=str(device.get("name", f"device {index}")),
                channels=int(device.get("max_output_channels", 0)),
                host_api=api_name,
            )
        )
    return out


def resolve_device(selector: str | int | None) -> int | None:
    """Turn an index or a name fragment into a PortAudio device index.

    `None` means "use the default". An unmatched selector also falls back to
    the default with a warning, because a stale device name should not cost the
    user their audio mid-session.
    """
    if selector is None or selector == "":
        return None
    devices = list_output_devices()
    if not devices:
        return None

    if isinstance(selector, int) or str(selector).isdigit():
        index = int(selector)
        if any(device.index == index for device in devices):
            return index
        log.warning("no output device with index %d; using the default", index)
        return None

    needle = str(selector).casefold()
    for device in devices:
        if needle in device.name.casefold():
            return device.index
    log.warning("no output device matching %r; using the default", selector)
    return None


class AudioPlayer:
    """Decodes AAC frames and plays them. Safe to start and stop repeatedly."""

    def __init__(
        self,
        device: str | int | None = None,
        pcm_tap: "Callable[[bytes], None] | None" = None,
    ) -> None:
        self._device = device
        # Recording taps the decoded PCM here rather than decoding a second
        # time. It is the same audio the speakers get.
        self._pcm_tap = pcm_tap
        self._decoder = None
        self._resampler = None
        self._stream = None
        self._lock = threading.Lock()
        self._decode_errors = 0

        # The format the device is open in. The AAC path always negotiates
        # 44.1 kHz stereo; the cable path is told what to use.
        self._rate = SAMPLE_RATE
        self._channels = CHANNELS
        self._frame_bytes = _BYTES_PER_FRAME

        # Shared with PortAudio's callback thread; guarded by _buffer_lock.
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._max_buffer = _ms_to_bytes(_MAX_BUFFER_MS)
        self._prefill = _ms_to_bytes(_PREFILL_MS)
        self._filling = True
        self._underruns = 0
        self._dropped = 0

    @property
    def running(self) -> bool:
        return self._stream is not None

    @property
    def buffered_ms(self) -> int:
        """How much audio is queued, in milliseconds. This is the latency."""
        with self._buffer_lock:
            frames = len(self._buffer) // self._frame_bytes
        return int(frames * 1000 / self._rate)

    @property
    def underruns(self) -> int:
        return self._underruns

    def start(self, extradata: bytes) -> None:
        """Open the decoder and the output device.

        `extradata` is the AudioSpecificConfig for the negotiated format.
        """
        if av is None:
            raise AudioUnavailable(
                "audio needs the av package: pip install av"
            )
        if sounddevice is None:
            raise AudioUnavailable(
                "audio needs the sounddevice package: pip install sounddevice"
            )

        with self._lock:
            self._close_locked()

            decoder = av.codec.CodecContext.create("aac", "r")
            decoder.extradata = extradata
            decoder.sample_rate = SAMPLE_RATE
            try:
                decoder.open()
            except Exception as exc:  # noqa: BLE001
                raise AudioUnavailable(f"could not open the AAC decoder: {exc}") from exc
            self._decoder = decoder

            # The decoder emits planar float; PortAudio wants interleaved s16.
            self._resampler = av.AudioResampler(
                format="s16", layout="stereo", rate=SAMPLE_RATE
            )
            self._open_device_locked(SAMPLE_RATE, CHANNELS)
            self._decode_errors = 0

    def start_pcm(self, sample_rate: int, channels: int = CHANNELS) -> None:
        """Open the output for PCM that arrives ready to play.

        The cable carries 48 kHz 16-bit stereo, so the device is opened in that
        format rather than resampling to the Wi-Fi path's 44.1 kHz: resampling
        would cost quality and latency for no reason.
        """
        if sounddevice is None:
            raise AudioUnavailable(
                "audio needs the sounddevice package: pip install sounddevice"
            )
        with self._lock:
            self._close_locked()
            self._open_device_locked(int(sample_rate), int(channels))

    def _open_device_locked(self, sample_rate: int, channels: int) -> None:
        """Open the output device. Called with `_lock` held."""
        self._rate = sample_rate
        self._channels = channels
        self._frame_bytes = channels * _BYTES_PER_SAMPLE
        self._max_buffer = self._budget(_MAX_BUFFER_MS)
        self._prefill = self._budget(_PREFILL_MS)

        with self._buffer_lock:
            self._buffer.clear()
            self._filling = True
        self._underruns = 0
        self._dropped = 0

        index = resolve_device(self._device)
        try:
            self._stream = sounddevice.RawOutputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
                device=index,
                # Let PortAudio pick the block size for the device, and ask
                # for its low-latency path.
                blocksize=0,
                latency="low",
                callback=self._on_device_ready,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            self._close_locked()
            raise AudioUnavailable(f"could not open the audio device: {exc}") from exc

        name = "system default"
        if index is not None:
            try:
                name = str(sounddevice.query_devices(index)["name"])
            except Exception:  # noqa: BLE001
                name = f"device {index}"
        log.info("audio output: %s (%d Hz, %d ch)", name, sample_rate, channels)

    def _budget(self, milliseconds: int) -> int:
        return int(self._rate * milliseconds / 1000) * self._frame_bytes

    def _on_device_ready(self, outdata, frames, time_info, status) -> None:
        """PortAudio callback: hand over whatever is buffered.

        Runs on a real-time thread, so it only moves bytes — no decoding, no
        locking beyond the buffer, no logging.
        """
        wanted = frames * self._frame_bytes
        with self._buffer_lock:
            # While filling, play silence rather than a fragment: starting on a
            # nearly empty buffer means underrunning immediately.
            available = 0 if self._filling else len(self._buffer)
            if available >= wanted:
                outdata[:wanted] = self._buffer[:wanted]
                del self._buffer[:wanted]
                return
            chunk = bytes(self._buffer[:available])
            del self._buffer[:available]
            if not self._filling:
                self._filling = True
                self._underruns += 1

        outdata[: len(chunk)] = chunk
        outdata[len(chunk) : wanted] = bytes(wanted - len(chunk))

    def write(self, frame: bytes) -> None:
        """Decode one AAC access unit and queue it for playback."""
        with self._lock:
            decoder, resampler = self._decoder, self._resampler
            if decoder is None or resampler is None or self._stream is None:
                return

            try:
                packet = av.Packet(frame)
                decoded = decoder.decode(packet)
            except Exception as exc:  # noqa: BLE001
                # A few bad frames at stream start are normal; a steady stream
                # of them means the format is wrong, and the log should say so
                # once rather than per packet.
                self._decode_errors += 1
                if self._decode_errors in (1, 50, 500):
                    log.warning("audio decode error (%d so far): %s", self._decode_errors, exc)
                return

            pcm = bytearray()
            for audio_frame in decoded:
                for resampled in resampler.resample(audio_frame):
                    # A plane's buffer is padded for alignment, so it is larger
                    # than the samples it holds. Copying the whole plane feeds
                    # that padding to the device as audio, which is heard as a
                    # metallic buzz on every frame. Take only the real samples.
                    wanted = resampled.samples * _BYTES_PER_FRAME
                    plane = memoryview(resampled.planes[0])
                    pcm += bytes(plane[:wanted])

        self._queue_pcm(bytes(pcm))

    def write_pcm(self, pcm: bytes) -> None:
        """Queue PCM that is already in the device's format."""
        if self._stream is None:
            return
        self._queue_pcm(pcm)

    def _queue_pcm(self, pcm: bytes) -> None:
        if not pcm:
            return

        tap = self._pcm_tap
        if tap is not None:
            try:
                tap(pcm)
            except Exception:  # noqa: BLE001 - recording must not break playback
                log.debug("the PCM tap raised", exc_info=True)

        with self._buffer_lock:
            self._buffer += pcm
            # Bound the buffer: if the source delivers faster than the device
            # consumes, the excess is latency, not audio worth keeping.
            excess = len(self._buffer) - self._max_buffer
            if excess > 0:
                # Drop whole frames: cutting mid-frame leaves half a sample
                # pair and shifts channel alignment, which clicks.
                excess += (-excess) % self._frame_bytes
                del self._buffer[:excess]
                self._dropped += excess
            if self._filling and len(self._buffer) >= self._prefill:
                self._filling = False

    def stop(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        stream, self._stream = self._stream, None
        self._decoder = None
        self._resampler = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        if self._underruns or self._dropped:
            log.debug(
                "audio buffer: %d underruns, %d bytes dropped to cap latency",
                self._underruns,
                self._dropped,
            )
        with self._buffer_lock:
            self._buffer.clear()
            self._filling = True
