"""Write the stream to a file, or discard it.

The file sink is the debugging workhorse: an `.h264` Annex-B dump plays in VLC
and can be re-fed to ffmpeg, which separates "decryption is broken" from "the
player is unhappy".
"""

from __future__ import annotations

from pathlib import Path

from airplaya.log import get_logger
from airplaya.sink.base import VideoSink

log = get_logger(__name__)


class FileSink(VideoSink):
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._handle = None

    def start(self, codec: str) -> None:
        self.stop()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("wb")
        log.info("writing %s stream to %s", codec, self._path)

    def write(self, data: bytes) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(data)
        except OSError as exc:
            log.error("write to %s failed: %s", self._path, exc)

    def stop(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


class NullSink(VideoSink):
    """Counts bytes and throws them away. Useful for protocol-only testing."""

    def __init__(self) -> None:
        self.byte_count = 0

    def start(self, codec: str) -> None:
        self.byte_count = 0
        log.info("null sink: discarding the %s stream", codec)

    def write(self, data: bytes) -> None:
        self.byte_count += len(data)

    def stop(self) -> None:
        log.info("null sink: discarded %d bytes", self.byte_count)
