"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from airplaya import __version__
from airplaya.config import Config
from airplaya.log import configure, get_logger
from airplaya.receiver import Receiver
from airplaya.sink.ffplay import default_binary

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airplaya",
        description="Receive iPhone screen mirroring over AirPlay.",
    )
    parser.add_argument("--version", action="version", version=f"airplaya {__version__}")
    parser.add_argument(
        "-n", "--name", default="airplaya", help="name shown in the iOS picker"
    )
    parser.add_argument(
        "--sink",
        choices=("ffplay", "file", "null"),
        default="ffplay",
        help="where the video goes (default: an ffplay window)",
    )
    parser.add_argument(
        "-o", "--sink-path", help="output file for --sink file (raw Annex-B H.264)"
    )
    parser.add_argument(
        "--ffplay", default=default_binary(), help="path to the ffplay binary"
    )
    parser.add_argument("--bind", default="0.0.0.0", help="local address to bind")
    parser.add_argument(
        "--ip", help="address to advertise over mDNS (default: autodetect)"
    )
    parser.add_argument("--rtsp-port", type=int, default=7000)
    parser.add_argument("--mirror-port", type=int, default=7100)
    parser.add_argument(
        "--resolution",
        default="1920x1080",
        help="resolution to advertise, WIDTHxHEIGHT (default: 1920x1080)",
    )
    parser.add_argument(
        "--max-fps", type=int, default=30, help="frame rate to advertise"
    )
    parser.add_argument("--state-dir", help="where the device key is stored")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="-v for protocol detail, -vv for per-packet tracing",
    )
    return parser


def parse_resolution(text: str) -> tuple[int, int]:
    try:
        width, _, height = text.lower().partition("x")
        return int(width), int(height)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--resolution wants WIDTHxHEIGHT, got {text!r}"
        ) from exc


def config_from_args(args: argparse.Namespace) -> Config:
    width, height = parse_resolution(args.resolution)
    return Config(
        name=args.name,
        bind_host=args.bind,
        advertise_ip=args.ip,
        rtsp_port=args.rtsp_port,
        mirror_port=args.mirror_port,
        width=width,
        height=height,
        max_fps=args.max_fps,
        sink=args.sink,
        sink_path=args.sink_path,
        ffplay_binary=args.ffplay,
        state_dir=args.state_dir,
        verbosity=args.verbose,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure(args.verbose)

    try:
        config = config_from_args(args)
    except argparse.ArgumentTypeError as exc:
        print(f"airplaya: {exc}", file=sys.stderr)
        return 2

    try:
        Receiver(config).serve_forever()
    except OSError as exc:
        # Almost always a port already in use, or a firewall block.
        log.error("network setup failed: %s", exc)
        return 1
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
