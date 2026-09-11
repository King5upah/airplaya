"""The receiver: owns the sockets, the advertisement, and the streams.

Lifetime differences are the reason this class exists. The control connection
comes and goes — iOS opens several during one mirroring session — while the
mirror, audio and timing sockets must stay bound across all of them, and the
mDNS advertisement must stay up the whole time. So handlers reach back here
through the `ReceiverServices` protocol instead of owning any of it.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from airplaya.config import Config
from airplaya.control import ControlServer
from airplaya.crypto.pairing import DeviceIdentity
from airplaya.discovery import Advertiser
from airplaya.log import get_logger
from airplaya.net import format_hwaddr, hardware_address, primary_ipv4
from airplaya.record import ClipRecorder, RecordingError, default_clip_path
from airplaya.rtsp.server import RtspServer
from airplaya.sink import build_sink
from airplaya.stream.audio import AudioStream
from airplaya.stream.mirror import MirrorStream
from airplaya.stream.timing import TimingClient

log = get_logger(__name__)


def default_state_dir() -> Path:
    """Where the persistent device key lives."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "airplaya"
    return Path.home() / ".airplaya"


class Receiver:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.hw_addr = hardware_address()

        state_dir = Path(config.state_dir) if config.state_dir else default_state_dir()
        self.identity = DeviceIdentity.load_or_create(state_dir / "device_key")

        self._address = config.advertise_ip or primary_ipv4()
        self._sink = build_sink(config)
        self._recorder = ClipRecorder()
        self._mirror = MirrorStream(
            config.bind_host, config.mirror_port, self._sink, recorder=self._recorder
        )
        self._audio = AudioStream(
            config.bind_host,
            config.audio_port,
            config.audio_control_port,
            device=config.audio_device,
            enabled=config.audio_enabled,
            recorder=self._recorder,
        )
        self._control = ControlServer(self.handle_command)
        self._timing = TimingClient(config.bind_host, config.timing_port)

        self._advertiser: Advertiser | None = None
        self._rtsp: RtspServer | None = None
        self._audio_ports: dict[str, int] = {}
        self._timing_port = 0
        self._mirror_port = 0
        self._stopped = threading.Event()

    # -- ReceiverServices ------------------------------------------------

    def mirror_port(self) -> int:
        return self._mirror_port

    def set_mirror_keys(self, key: bytes, iv: bytes) -> None:
        self._mirror.set_keys(key, iv)

    def audio_ports(self) -> dict[str, int]:
        return dict(self._audio_ports)

    def set_audio_keys(self, key: bytes, iv: bytes) -> None:
        self._audio.set_keys(key, iv)

    def configure_audio(self, ct: int, frame_length: int | None) -> None:
        self._audio.configure_format(ct, frame_length)

    def timing_port(self) -> int:
        return self._timing_port

    def start_timing(self, address: str, port: int) -> None:
        self._timing.set_peer(address, port)

    def teardown_streams(self) -> None:
        """Drop the current session's media state but keep listening."""
        # Finish any clip first: the stream it was recording is about to end,
        # and an unfinalised MP4 is not playable.
        if self._recorder.recording:
            try:
                self._recorder.stop()
            except RecordingError as exc:
                log.warning("could not finish the clip: %s", exc)
        self._sink.stop()
        self._audio.end_session()

    # -- control commands ------------------------------------------------

    def handle_command(self, message: dict) -> dict:
        """Run one command from the front end. Raises on a bad request."""
        command = message.get("command")
        if command == "record":
            path = message.get("path") or default_clip_path(self.config.clips_dir)
            target = self._recorder.start(path, codec=self._mirror.codec or "h264")
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
            # The front end knows how large it is drawing the picture; scaling
            # to that keeps the image sharp instead of upscaling a smaller one.
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
                "recording": self._recorder.recording,
                "recordingPath": str(self._recorder.path) if self._recorder.path else None,
                "elapsedSeconds": round(self._recorder.elapsed_seconds, 1),
                "mirroring": self._mirror.codec is not None,
            }
        raise ValueError(f"unknown command {command!r}")

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._mirror_port = self._mirror.start()
        self._audio_ports = self._audio.start()
        self._timing_port = self._timing.start()
        self._control.start()

        self._rtsp = RtspServer((self.config.bind_host, self.config.rtsp_port), self)
        threading.Thread(
            target=self._rtsp.serve_forever, name="rtsp-accept", daemon=True
        ).start()
        log.info("control channel listening on port %d", self.config.rtsp_port)

        self._advertiser = Advertiser(
            name=self.config.name,
            model=self.config.model,
            source_version=self.config.source_version,
            hw_addr=self.hw_addr,
            public_key=self.identity.public_key,
            address=self._address,
            port=self.config.rtsp_port,
        )
        self._advertiser.start()

        log.info(
            "ready. On the iPhone: Control Centre -> Screen Mirroring -> %r "
            "(device %s at %s)",
            self.config.name,
            format_hwaddr(self.hw_addr),
            self._address,
        )

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
        if self._advertiser is not None:
            self._advertiser.stop()
        if self._rtsp is not None:
            self._rtsp.shutdown()
            self._rtsp.server_close()
        self._timing.stop()
        self._audio.stop()
        self._mirror.stop()
        self._sink.stop()

    def serve_forever(self) -> None:
        """Run until interrupted."""
        self.start()
        try:
            while not self._stopped.wait(0.5):
                # A closed player window means the user is done; exiting is
                # friendlier than mirroring into a void.
                if not self._sink.alive and self._sink_was_started():
                    log.info("the player window closed")
                    break
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.stop()

    def _sink_was_started(self) -> bool:
        # `alive` is only meaningful once a stream has begun; before that a
        # sink with no process is not a failure.
        return self._mirror_port != 0 and getattr(self._sink, "_process", None) is not None
