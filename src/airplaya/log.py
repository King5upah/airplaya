"""Logging setup.

One root logger named `airplaya`; every module uses `get_logger(__name__)` so
that `-v` can turn on protocol tracing without drowning the console in
per-packet noise by default.
"""

from __future__ import annotations

import logging
import sys

_ROOT = "airplaya"

# Per-packet detail lives below DEBUG so `-v` stays readable and `-vv` shows
# the full stream trace.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def get_logger(name: str) -> logging.Logger:
    if name == _ROOT or name.startswith(_ROOT + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT}.{name}")


def configure(verbosity: int = 0) -> None:
    level = {0: logging.INFO, 1: logging.DEBUG}.get(verbosity, TRACE)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger(_ROOT)
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def hexdump(data: bytes, limit: int = 64) -> str:
    """Short hex preview for trace logs."""
    head = data[:limit]
    text = head.hex(" ")
    return text + (f" ... (+{len(data) - limit} bytes)" if len(data) > limit else "")
