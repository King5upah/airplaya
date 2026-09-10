"""Clock sync on the timing port.

In AirPlay v1 the receiver is the one that polls: it sends a 32-byte request to
the client's `timingPort` and the client replies with its clock. The exchange
drives audio/video sync, which this receiver does not yet do — but the client
notices a receiver that never asks, so we keep the conversation going and bind
the port we advertised.

Packet layout, 32 bytes:

```
0   0x80        RTP marker
1   0xd2        payload type (request; replies come back as 0xd3)
2-3 0x0007      sequence
4-7 zero
8-15  the client's reference time, echoed from the last reply
16-23 our receive time for that reply
24-31 our send time
```

Times are 64-bit NTP timestamps: seconds in the high 32 bits, fractional in the
low 32.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

from airplaya.log import TRACE, get_logger
from airplaya.net import bind_exclusive

log = get_logger(__name__)

_REQUEST_HEADER = bytes([0x80, 0xD2, 0x00, 0x07, 0x00, 0x00, 0x00, 0x00])
_POLL_INTERVAL = 3.0
_NTP_EPOCH_OFFSET = 0x83AA7E80  # seconds between 1900-01-01 and 1970-01-01


def ntp_now() -> int:
    """The current time as a 64-bit NTP timestamp."""
    now = time.time()
    seconds = int(now) + _NTP_EPOCH_OFFSET
    fraction = int((now % 1.0) * (1 << 32))
    return (seconds << 32) | fraction


class TimingClient:
    """Polls the client's timing port from the port we advertise."""

    def __init__(self, host: str, local_port: int) -> None:
        self._host = host
        self._local_port = local_port
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._remote: tuple[str, int] | None = None
        self._client_reference = 0
        self._receive_time = 0

    def start(self) -> int:
        """Bind the timing port. Returns the bound port."""
        if self._socket is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            bind_exclusive(sock)
            sock.bind((self._host, self._local_port))
            sock.settimeout(0.5)
            self._socket = sock
            log.debug("timing socket bound to port %d", sock.getsockname()[1])
        return self._socket.getsockname()[1]

    def set_peer(self, address: str, port: int) -> None:
        """Point the poller at the client, and start it."""
        self._remote = (address, port)
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="timing", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if thread is not None:
            thread.join(timeout=2)

    def _loop(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            sock, remote = self._socket, self._remote
            if sock is None or remote is None:
                return

            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + _POLL_INTERVAL
                self._send_request(sock, remote)

            try:
                data, _ = sock.recvfrom(128)
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                return
            self._on_reply(data)

    def _send_request(self, sock: socket.socket, remote: tuple[str, int]) -> None:
        packet = bytearray(_REQUEST_HEADER + bytes(24))
        struct.pack_into(">Q", packet, 8, self._client_reference)
        struct.pack_into(">Q", packet, 16, self._receive_time)
        struct.pack_into(">Q", packet, 24, ntp_now())
        try:
            sock.sendto(bytes(packet), remote)
        except OSError as exc:
            log.debug("timing request to %s failed: %s", remote, exc)

    def _on_reply(self, data: bytes) -> None:
        if len(data) < 32:
            log.log(TRACE, "short timing packet: %d bytes", len(data))
            return
        # Offset 24 holds the client's transmit time, which becomes the
        # reference we echo on the next request.
        (self._client_reference,) = struct.unpack_from(">Q", data, 24)
        self._receive_time = ntp_now()
        log.log(TRACE, "timing reply, type %#02x", data[1] & ~0x80)
