"""Turn a `Config` into a sink."""

from __future__ import annotations

from airplaya.config import Config
from airplaya.sink.app import AppSink
from airplaya.sink.base import VideoSink
from airplaya.sink.file import FileSink, NullSink
from airplaya.sink.ffplay import FfplaySink


def build_sink(config: Config) -> VideoSink:
    kind = config.sink
    if kind == "app":
        if not config.video_port:
            raise ValueError("--sink app needs --video-port")
        return AppSink(port=config.video_port, max_side=config.video_max_side)
    if kind == "ffplay":
        return FfplaySink(
            binary=config.ffplay_binary,
            # Plain ASCII: the title reaches ffplay through the console code
            # page, and anything else arrives mangled.
            window_title=f"airplaya - {config.name}",
            extra_args=config.ffplay_extra_args,
        )
    if kind == "file":
        if not config.sink_path:
            raise ValueError("--sink file needs --sink-path")
        return FileSink(config.sink_path)
    if kind == "null":
        return NullSink()
    raise ValueError(f"unknown sink {kind!r}")
