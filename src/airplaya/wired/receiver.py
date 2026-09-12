"""The cable receiver: same sinks, same recorder, no network.

Everything downstream of the frames is shared with the Wi-Fi path — the video
sink, the audio device, the clip recorder, the control channel the desktop app
talks to. What differs is where the frames come from and that nothing is
advertised or encrypted.

The device is watched rather than opened once: unplugging the phone ends the
session and the loop goes back to waiting, so plugging it in again starts
mirroring without restarting anything.
"""

from __future__ import annotations

import threading
import time

from airplaya.config import Config
from airplaya.control import ControlServer
from airplaya.log import get_logger
from airplaya.record import ClipRecorder, RecordingError, default_clip_path
from airplaya.sink import build_sink
from airplaya.sink.audio import AudioPlayer
from airplaya.wired import usb
from airplaya.wired.session import WiredSession
from airplaya.wired.usb import UsbError, UsbLink, UsbUnavailable

log = get_logger(__name__)

# How long to wait before looking for the phone again.
_RETRY_DELAY = 2.0
# The audio the cable carries. Confirmed by the AFMT packet at session start,
# which is when the output device is actually opened.
_AUDIO_RATE = 48000
_AUDIO_CHANNELS = 2


class WiredReceiver:
    """Mirrors the phone over USB until stopped."""

    def __init__(self, config: Config, serial: str | None = None) -> None:
        self.config = config
        self._serial = serial

        self._sink = build_sink(config)
        self._recorder = ClipRecorder()
        self._audio: AudioPlayer | None = None
        if config.audio_enabled:
            self._audio = AudioPlayer(
                device=config.audio_device, pcm_tap=self._on_pcm
            )
        self._control = ControlServer(self.handle_command)

        self._session: WiredSession | None = None
        self._link: UsbLink | None = None
        self._stopped = threading.Event()
        self._announced = False

    # -- control commands ------------------------------------------------

    def handle_command(self, message: dict) -> dict:
        """Run one command from the front end. Raises on a bad request."""
        command = message.get("command")
        session = self._session

        if command == "record":
            path = message.get("path") or default_clip_path(self.config.clips_dir)
            codec = (session.codec if session else None) or "h264"
            target = self._recorder.start(
                path,
                codec=codec,
                audio_rate=_AUDIO_RATE,
                audio_channels=_AUDIO_CHANNELS,
            )
            return {"path": str(target)}
        if command == "stop_record":
            clip = self._recorder.stop()
            return {
                "clip": {
                    "path": str(clip.path),
                    "durationSeconds": round(clip.duration_seconds, 2),
                    "sizeBytes": clip.size_bytes,
                }
            }
        if command == "video_size":
            width = int(message.get("width") or 0)
            height = int(message.get("height") or 0)
            if width <= 0 or height <= 0:
                raise ValueError("video_size needs a positive width and height")
            setter = getattr(self._sink, "set_display_size", None)
            if setter is None:
                return {"applied": False, "reason": "this sink scales nothing"}
            setter(width, height)
            return {"applied": True, "width": width, "height": height}
        if command == "status":
            return {
                "source": "usb",
                "recording": self._recorder.recording,
                "recordingPath": str(self._recorder.path) if self._recorder.path else None,
                "elapsedSeconds": round(self._recorder.elapsed_seconds, 1),
                "mirroring": bool(session and session.streaming),
                "videoSamples": session.video_samples if session else 0,
                "audioSamples": session.audio_samples if session else 0,
            }
        raise ValueError(f"unknown command {command!r}")

    def _on_pcm(self, pcm: bytes) -> None:
        if self._recorder.recording:
            self._recorder.add_audio(pcm)

    # -- lifecycle -------------------------------------------------------

    def serve_forever(self) -> None:
        """Wait for the phone, mirror it, and keep waiting after it leaves."""
        self._control.start()

        try:
            while not self._stopped.is_set():
                try:
                    self._run_once()
                except UsbUnavailable as exc:
                    # A missing dependency will not fix itself; stop rather
                    # than retry for ever.
                    log.error("%s", exc)
                    return
                except UsbError as exc:
                    log.info("%s", exc)
                except Exception:  # noqa: BLE001
                    log.exception("the cable session failed")
                if not self._stopped.is_set():
                    self._stopped.wait(_RETRY_DELAY)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.stop()

    def _run_once(self) -> None:
        """One attempt: find the phone, mirror it until it goes away."""
        if not self._announced:
            log.info(
                "ready. Waiting for an iPhone on USB — unlock it and answer "
                "'Trust this computer' if asked"
            )
            self._announced = True

        device = usb.activate(self._serial)
        link = UsbLink(self._serial)
        opened = link.open()
        self._link = link

        audio = self._audio
        session = WiredSession(
            write=link.write,
            sink=self._sink,
            audio=audio,
            recorder=self._recorder,
            display_size=self.config.advertised_size(),
            name=self.config.name,
        )
        self._session = session

        log.info("mirror client connected over the cable")
        log.info("client: %s (cable)", opened.product or device.product)

        try:
            for frame in link.frames():
                session.handle(frame)
                if self._stopped.is_set():
                    break
        finally:
            self._end_session(session, link)

    def _end_session(self, session: WiredSession, link: UsbLink) -> None:
        if self._recorder.recording:
            try:
                self._recorder.stop()
            except RecordingError as exc:
                log.warning("could not finish the clip: %s", exc)

        try:
            session.close()
        except Exception:  # noqa: BLE001 - the phone may already be gone
            log.debug("could not close the session cleanly", exc_info=True)

        link.close()
        self._link = None
        self._session = None
        self._sink.stop()
        if self._audio is not None:
            self._audio.stop()
        log.info(
            "mirror client disconnected (%d video samples, %d audio samples)",
            session.video_samples,
            session.audio_samples,
        )
        # Leaving the AV configuration selected upsets iTunes and Finder, so
        # hand the device back the way it was found.
        usb.deactivate(self._serial)
        self._announced = False

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        log.info("shutting down")

        if self._recorder.recording:
            try:
                self._recorder.stop()
            except RecordingError as exc:
                log.warning("could not finish the clip: %s", exc)

        self._control.stop()
        link = self._link
        if link is not None:
            link.close()
        if self._audio is not None:
            self._audio.stop()
        self._sink.stop()


def list_devices_text() -> str:
    """A human-readable list of what is plugged in."""
    devices = usb.find_devices()
    if not devices:
        return "No iPhone or iPad is connected over USB."
    lines = ["Devices on USB:"]
    for device in devices:
        lines.append(f"  {device}")
    return "\n".join(lines)


def wait_for_device(timeout: float = 10.0) -> bool:
    """Poll until a device shows up. Used by the front end's preflight."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if usb.find_devices():
            return True
        time.sleep(0.5)
    return False
