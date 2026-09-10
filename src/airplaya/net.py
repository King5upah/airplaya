"""Host address and hardware-address helpers."""

from __future__ import annotations

import hashlib
import socket
import sys
import uuid


def bind_exclusive(sock: socket.socket) -> None:
    """Claim a port so that no second process can share it.

    On Windows `SO_REUSEADDR` does not mean what it means elsewhere: a second
    process can bind a port that is already listening, and the OS then hands
    incoming connections to only one of them. A leftover receiver from an
    earlier run therefore keeps serving while a fresh one starts up, reports
    success, and logs nothing — which is a genuinely baffling failure to debug.

    `SO_EXCLUSIVEADDRUSE` makes the second bind fail loudly instead. On other
    platforms `SO_REUSEADDR` is the right call, since it only affects sockets
    in TIME_WAIT.
    """
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def primary_ipv4() -> str:
    """Best guess at the LAN address the iPhone will connect to.

    Opening a UDP socket toward a public address makes the OS pick the
    interface it would actually route through, without sending a packet.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def hardware_address() -> bytes:
    """A stable 6-byte device ID.

    AirPlay wants a MAC-shaped `deviceID` that does not change between runs, or
    iOS treats the receiver as a new device each time. `uuid.getnode()` gives us
    the real adapter address when it can; when it falls back to a random value
    it sets the multicast bit, and we replace that with a hash of the hostname
    so the ID at least stays stable on this machine.
    """
    node = uuid.getnode()
    raw = node.to_bytes(6, "big")
    if raw[0] & 0x01:
        digest = hashlib.sha256(socket.gethostname().encode()).digest()
        raw = bytes([digest[0] & 0xFE]) + digest[1:6]
    return raw


def format_hwaddr(raw: bytes, sep: str = ":") -> str:
    return sep.join(f"{b:02X}" for b in raw)
