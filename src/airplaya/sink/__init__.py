"""Video sinks: where the decrypted H.264 elementary stream goes."""

from airplaya.sink.base import VideoSink
from airplaya.sink.factory import build_sink

__all__ = ["VideoSink", "build_sink"]
