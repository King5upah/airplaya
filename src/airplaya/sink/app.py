"""Send decoded frames to the desktop app, so it can draw the video itself.

The alternative is handing the stream to a player like ffplay, which works but
puts the picture in a window we do not control — no overlay controls, no
styling, and a title bar we do not want. Decoding here and letting the app draw
means the window is ours.

    H.264 Annex-B ──> PyAV decode ──> scale to RGBA ──> loopback TCP ──> app

The app listens and we connect, because the app is the longer-lived process; it
passes its port in with `--video-port`.

Wire format, little-endian, one frame after another:

```
magic   4 bytes  "AVF1"
width   uint32
height  uint32
length  uint32   bytes of RGBA that follow (width * height * 4)
pixels  length bytes
```

Only the newest frame matters for a live mirror, so a frame that arrives while
the previous one is still being written is dropped rather than queued. Latency
is worth more than completeness here.
"""

from __future__ import annotations

import socket
import struct
import threading

from airplaya.log import TRACE, get_logger
from airplaya.sink.base import VideoSink

log = get_logger(__name__)

MAGIC = b"AVF1"
_HEADER = struct.Struct("<4sIII")

try:  # pragma: no cover - depends on the environment
    import av
    import numpy
except Exception:  # noqa: BLE001
    av = None
    numpy = None


def scaled_size(width: int, height: int, max_side: int) -> tuple[int, int]:
    """Fit within `max_side` on the long edge, keeping the aspect ratio.

    A phone screen is around 1170x2532; sending that at 30 fps is 350 MB/s of
    pixels, which is a waste when the window showing it is smaller. Dimensions
    come back even, which every scaler prefers.
    """
    longest = max(width, height)
    if longest <= max_side:
        scale = 1.0
    else:
        scale = max_side / longest
    new_width = max(2, int(width * scale) & ~1)
    new_height = max(2, int(height * scale) & ~1)
    return new_width, new_height


class AppSink(VideoSink):
    def __init__(self, port: int, max_side: int = 1920) -> None:
        self._port = port
        self._max_side = max_side
        self._requested_side: int | None = None
        self._decoder = None
        self._socket: socket.socket | None = None
        self._lock = threading.Lock()

        # Single-slot handoff: the newest frame replaces whatever has not been
        # sent yet.
        self._pending: bytes | None = None
        self._pending_lock = threading.Condition()
        self._sender: threading.Thread | None = None
        self._stop = threading.Event()

        self.frames_sent = 0
        self.frames_dropped = 0
        self._decode_errors = 0

    def start(self, codec: str) -> None:
        if av is None or numpy is None:
            raise RuntimeError(
                "in-app video needs the av and numpy packages: pip install av numpy"
            )

        self.stop()
        try:
            self._socket = socket.create_connection(("127.0.0.1", self._port), timeout=3)
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            raise RuntimeError(
                f"could not reach the app's video port {self._port}: {exc}"
            ) from exc

        decoder = av.codec.CodecContext.create(codec, "r")
        try:
            decoder.open()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"could not open the {codec} decoder: {exc}") from exc
        self._decoder = decoder

        self._stop.clear()
        self._sender = threading.Thread(target=self._send_loop, name="video-send", daemon=True)
        self._sender.start()
        log.info("decoding %s in-process; frames go to the app on port %d", codec, self._port)

    @property
    def alive(self) -> bool:
        return self._socket is not None

    def write(self, data: bytes) -> None:
        with self._lock:
            decoder = self._decoder
            if decoder is None or self._socket is None:
                return
            try:
                frames = decoder.decode(av.Packet(data))
            except Exception as exc:  # noqa: BLE001
                # Mirroring starts mid-stream, so the first packets often fail
                # to decode; say so once rather than per frame.
                self._decode_errors += 1
                if self._decode_errors in (1, 50, 500):
                    log.warning("video decode error (%d so far): %s", self._decode_errors, exc)
                return

        for frame in frames:
            self._queue(frame)

    def set_display_size(self, width: int, height: int) -> None:
        """Tell the sink how large the client is drawing the picture.

        Scaling to the window means the pixels sent are the pixels shown.
        Sending less than that is visibly soft; sending more wastes bandwidth
        and conversion time on detail that is thrown away on screen.
        """
        requested = max(2, max(width, height))
        if requested != self._requested_side:
            self._requested_side = requested
            log.info("client is drawing at %dx%d; scaling to fit", width, height)

    def _target_side(self) -> int:
        """Never scale above the configured ceiling, or below 480."""
        if self._requested_side is None:
            return self._max_side
        return max(480, min(self._max_side, self._requested_side))

    def _queue(self, frame) -> None:
        width, height = scaled_size(frame.width, frame.height, self._target_side())
        try:
            rgba = frame.reformat(width=width, height=height, format="rgba")
            pixels = rgba.to_ndarray(format="rgba").tobytes()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not convert a frame: %s", exc)
            return

        message = _HEADER.pack(MAGIC, width, height, len(pixels)) + pixels
        with self._pending_lock:
            if self._pending is not None:
                self.frames_dropped += 1
            self._pending = message
            self._pending_lock.notify()

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            with self._pending_lock:
                if self._pending is None:
                    self._pending_lock.wait(timeout=0.25)
                    if self._pending is None:
                        continue
                message, self._pending = self._pending, None

            connection = self._socket
            if connection is None:
                return
            try:
                connection.sendall(message)
            except OSError as exc:
                log.info("the app closed the video connection (%s)", exc)
                self._socket = None
                return
            self.frames_sent += 1
            if self.frames_sent % 300 == 1:
                log.log(
                    TRACE,
                    "video: %d frames sent, %d dropped",
                    self.frames_sent,
                    self.frames_dropped,
                )

    def stop(self) -> None:
        self._stop.set()
        with self._pending_lock:
            self._pending = None
            self._pending_lock.notify_all()

        sender, self._sender = self._sender, None
        if sender is not None and sender is not threading.current_thread():
            sender.join(timeout=2)

        with self._lock:
            connection, self._socket = self._socket, None
            self._decoder = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if self.frames_sent:
            log.debug(
                "video sink closed: %d frames sent, %d dropped",
                self.frames_sent,
                self.frames_dropped,
            )
