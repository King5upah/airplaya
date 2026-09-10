"""Audio playback: decode in-process, play on a chosen output device.

    AAC frames ──> PyAV (aac decoder + resampler) ──> s16 PCM ──> PortAudio

PyAV rather than an ffmpeg subprocess because the decoder needs its
AudioSpecificConfig passed as extradata, and the ffmpeg command line has no way
to supply that. PortAudio rather than ffplay because it can be pointed at a
specific Windows output device, which is the whole point of letting the user
choose one.

Both dependencies are optional. Without them audio is skipped with an
explanation, and mirroring carries on.
"""

from __future__ import annotations

import threading

from airplaya.log import get_logger

log = get_logger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 2
_BLOCK_FRAMES = 512

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

    def __init__(self, device: str | int | None = None) -> None:
        self._device = device
        self._decoder = None
        self._resampler = None
        self._stream = None
        self._lock = threading.Lock()
        self._decode_errors = 0

    @property
    def running(self) -> bool:
        return self._stream is not None

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

            index = resolve_device(self._device)
            try:
                self._stream = sounddevice.RawOutputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    device=index,
                    blocksize=_BLOCK_FRAMES,
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
            log.info("audio output: %s", name)
            self._decode_errors = 0

    def write(self, frame: bytes) -> None:
        """Decode one AAC access unit and play it."""
        with self._lock:
            decoder, resampler, stream = self._decoder, self._resampler, self._stream
            if decoder is None or resampler is None or stream is None:
                return

            try:
                packet = av.Packet(frame)
                decoded = decoder.decode(packet)
            except Exception as exc:  # noqa: BLE001
                # A few bad frames at stream start are normal; a steady stream
                # of them means the format is wrong and the log should say so
                # once rather than per packet.
                self._decode_errors += 1
                if self._decode_errors in (1, 50, 500):
                    log.warning("audio decode error (%d so far): %s", self._decode_errors, exc)
                return

            for audio_frame in decoded:
                for resampled in resampler.resample(audio_frame):
                    try:
                        stream.write(bytes(resampled.planes[0]))
                    except Exception as exc:  # noqa: BLE001
                        if "underflow" in str(exc).lower():
                            continue
                        log.error("audio playback stopped: %s", exc)
                        self._close_locked()
                        return

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
