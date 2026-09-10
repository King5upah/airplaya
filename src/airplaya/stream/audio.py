"""Audio stream sockets.

Mirroring sets up an audio stream alongside the video one, and iOS expects the
ports it was given in the SETUP response to exist. It does not require anything
to be done with the packets: if nothing listens, the client retries and logs,
and some clients give up on the whole session.

So this binds the data and control ports and drains them. Decoding is not
implemented — the payloads are AES-CBC encrypted AAC-ELD or ALAC frames, which
is a separate piece of work from mirroring. The counters exist so `-vv` can
confirm audio is arriving when that work starts.
"""

from __future__ import annotations

import socket
import threading

from airplaya.log import TRACE, get_logger
from airplaya.net import bind_exclusive

log = get_logger(__name__)

_MAX_DATAGRAM = 4096


class AudioDrain:
    """Binds the audio data and control ports and discards what arrives."""

    def __init__(self, host: str, data_port: int, control_port: int) -> None:
        self._host = host
        self._requested = {"data": data_port, "control": control_port}
        self._sockets: dict[str, socket.socket] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.packet_count = 0
        self.byte_count = 0

    def start(self) -> dict[str, int]:
        """Bind both ports. Returns the bound port for each role."""
        if self._sockets:
            return {role: sock.getsockname()[1] for role, sock in self._sockets.items()}

        for role, port in self._requested.items():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            bind_exclusive(sock)
            sock.bind((self._host, port))
            sock.settimeout(0.5)
            self._sockets[role] = sock

        self._stop.clear()
        self._thread = threading.Thread(target=self._drain, name="audio", daemon=True)
        self._thread.start()

        ports = {role: sock.getsockname()[1] for role, sock in self._sockets.items()}
        log.info("audio sockets open on %s (packets are discarded)", ports)
        return ports

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        for sock in self._sockets.values():
            try:
                sock.close()
            except OSError:
                pass
        self._sockets.clear()
        if thread is not None:
            thread.join(timeout=2)
        if self.packet_count:
            log.debug("discarded %d audio packets (%d bytes)", self.packet_count, self.byte_count)

    def _drain(self) -> None:
        while not self._stop.is_set():
            for role, sock in list(self._sockets.items()):
                try:
                    data, _ = sock.recvfrom(_MAX_DATAGRAM)
                except (socket.timeout, BlockingIOError):
                    continue
                except OSError:
                    return
                self.packet_count += 1
                self.byte_count += len(data)
                log.log(TRACE, "audio %s packet: %d bytes", role, len(data))
