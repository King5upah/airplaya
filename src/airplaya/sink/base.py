"""The video sink interface.

A sink receives an Annex-B H.264 elementary stream: parameter sets and slices,
each prefixed with a 4-byte start code, in decode order. It gets one `start()`
per mirroring session and one `stop()` when the client disconnects.

Sinks must never raise from `write()` for a recoverable problem. A dropped
frame is not worth tearing down the session; the sink logs and moves on.
"""

from __future__ import annotations

import abc


class VideoSink(abc.ABC):
    @abc.abstractmethod
    def start(self, codec: str) -> None:
        """Prepare to receive a stream. `codec` is `"h264"` or `"h265"`."""

    @abc.abstractmethod
    def write(self, data: bytes) -> None:
        """Consume Annex-B data."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Release everything. Must be safe to call without a prior `start()`."""

    @property
    def alive(self) -> bool:
        """False once the sink can no longer accept data (e.g. window closed)."""
        return True
