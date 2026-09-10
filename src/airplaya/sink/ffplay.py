"""Play the mirrored stream in an ffplay window.

ffplay is a pragmatic choice rather than an elegant one: it decodes and
displays H.264 with no extra dependency beyond an ffmpeg build. The flags below
all serve latency — without them ffplay buffers enough to put the mirror a
second or more behind the phone.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from airplaya.log import get_logger
from airplaya.sink.base import VideoSink

log = get_logger(__name__)

# `-probesize`/`-analyzeduration` stop ffplay from waiting to inspect the
# stream; `nobuffer`/`low_delay`/`-framedrop` keep it from queueing frames.
_LOW_LATENCY_ARGS = [
    "-fflags",
    "nobuffer",
    "-flags",
    "low_delay",
    "-framedrop",
    "-probesize",
    "32",
    "-analyzeduration",
    "0",
    "-sync",
    "video",
    "-loglevel",
    "warning",
]


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

    def start(self, codec: str) -> None:
        self.stop()
        command = [
            self._binary,
            *_LOW_LATENCY_ARGS,
            "-window_title",
            self._window_title,
            "-f",
            codec,
            "-i",
            "-",
            *self._extra_args,
        ]
        log.info("starting sink: %s", " ".join(command))
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"could not run {self._binary!r}. Install ffmpeg, or pass "
                "--sink file to write the stream instead."
            ) from exc

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def write(self, data: bytes) -> None:
        process = self._process
        if process is None or process.stdin is None:
            return
        if process.poll() is not None:
            # The user closed the window. Drop data rather than crashing the
            # stream thread; the receiver notices via `alive`.
            return
        try:
            process.stdin.write(data)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            log.info("sink closed the pipe (%s); dropping video", exc)

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        # Closing stdin is the polite exit: ffplay sees end of stream.
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
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
