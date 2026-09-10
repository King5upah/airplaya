"""Play the mirrored stream in an ffplay window.

ffplay is a pragmatic choice rather than an elegant one: it decodes and
displays H.264 with no dependency beyond an ffmpeg build.

The stream reaches it over a loopback TCP connection, not over stdin. That is
not a style preference. Given `-i -` on Windows, ffplay consumes the stream and
decodes it happily but never creates its window, so mirroring appears to do
nothing at all; the same stream from a file or a socket opens a window
immediately. `tcp://…?listen=1` is a URL like any other to ffmpeg, so it takes
that path instead.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from airplaya.log import get_logger
from airplaya.sink.base import VideoSink

log = get_logger(__name__)

# `-flags low_delay` and `-framedrop` keep latency down; `-framerate` saves
# ffplay from having to estimate one.
#
# Two latency options are deliberately absent, because each one costs the
# window entirely — ffplay decodes the stream and never displays it:
#
# * `-fflags nobuffer`. Measured: with it, ffplay runs with no window at all;
#   without it, the window appears immediately. Same stream either way.
# * `-probesize 32 -analyzeduration 0`. Too little data to estimate a frame
#   rate ("not enough frames to estimate rate"), so the decoder never finishes
#   opening.
#
# If you are tempted to add either one back for latency, check that a window
# still appears.
_LOW_LATENCY_ARGS = [
    "-flags",
    "low_delay",
    # A raw H.264 stream carries no timestamps, so ffplay synthesises them from
    # `-framerate`. Paced against its own video clock, any mismatch between
    # that rate and what the phone actually sends accumulates as delay. Running
    # against the external (wall) clock with `-framedrop` instead means late
    # frames are discarded rather than queued, so the picture stays current.
    "-sync",
    "ext",
    "-framedrop",
    # Deliberately higher than the phone will send: ffplay then never waits on
    # a timestamp, it just displays what has arrived.
    "-framerate",
    "120",
    "-flags2",
    "fast",
    # No title bar: a borderless window is what makes this look like a screen
    # rather than a media player with a stream in it.
    "-noborder",
    "-loglevel",
    "warning",
]

_CONNECT_TIMEOUT = 6.0


class FfplaySink(VideoSink):
    def __init__(
        self,
        binary: str = "ffplay",
        window_title: str = "airplaya",
        extra_args: Sequence[str] = (),
    ) -> None:
        self._binary = binary
        self._window_title = window_title
        self._extra_args = list(extra_args)
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None

    def start(self, codec: str) -> None:
        self.stop()

        port = _free_port()
        # ffplay listens; we connect. The listen timeout is ffplay's own, so a
        # crash on our side does not leave it waiting forever.
        url = f"tcp://127.0.0.1:{port}?listen=1&listen_timeout=8000"
        command = [
            self._binary,
            *_LOW_LATENCY_ARGS,
            "-window_title",
            self._window_title,
            "-f",
            codec,
            "-i",
            url,
            *self._extra_args,
        ]
        log.info("starting sink: %s", " ".join(command))
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"could not run {self._binary!r}. Install ffmpeg, or pass "
                "--sink file to write the stream instead."
            ) from exc

        self._socket = self._connect(port)
        if self._socket is None:
            self.stop()
            raise RuntimeError("ffplay did not accept the video connection")

    def _connect(self, port: int) -> socket.socket | None:
        """Wait for ffplay's listener to come up, then connect."""
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            process = self._process
            if process is not None and process.poll() is not None:
                log.error("ffplay exited before accepting the stream")
                return None
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=1.0)
            except OSError:
                time.sleep(0.05)
                continue
            # Drop the connect timeout: it would otherwise apply to every
            # send, and a full player buffer is normal back-pressure, not a
            # failure. With it left in place the stream dies after a few
            # seconds with "timed out".
            connection.settimeout(None)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            log.debug("video connected to ffplay on port %d", port)
            return connection
        log.error("timed out waiting for ffplay to listen on port %d", port)
        return None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def write(self, data: bytes) -> None:
        connection = self._socket
        if connection is None:
            return
        try:
            connection.sendall(data)
        except OSError as exc:
            # The window was closed. Drop the data rather than killing the
            # stream thread; the receiver notices through `alive`.
            log.info("sink closed the connection (%s); dropping video", exc)
            self._socket = None

    def stop(self) -> None:
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

        process = self._process
        self._process = None
        if process is None:
            return

        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass

        log.debug("sink did not exit on its own; terminating")
        process.terminate()
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        process.kill()

        # A launcher shim (Chocolatey installs one for ffplay) spawns the real
        # binary as a child, and killing the shim leaves that child running
        # with a window nobody owns. Take the whole tree down.
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )


def _free_port() -> int:
    """Ask the OS for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def default_binary() -> str:
    """Locate ffplay, preferring the real binary over any launcher shim.

    Chocolatey puts a shim in `PATH` that re-launches the real executable as a
    child process. Running the real one directly means the process we hold is
    the process that owns the window.
    """
    name = "ffplay.exe" if sys.platform == "win32" else "ffplay"
    found = shutil.which(name)
    if found is None:
        return name

    path = Path(found)
    if sys.platform == "win32" and "chocolatey" in str(path).casefold():
        for candidate in Path("C:/ProgramData/chocolatey/lib").glob(
            "ffmpeg*/tools/**/bin/ffplay.exe"
        ):
            return str(candidate)
    return str(path)
