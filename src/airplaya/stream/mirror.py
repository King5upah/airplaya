"""The mirroring video stream (TCP, port 7100).

Wire format: a 128-byte header followed by `payload_size` bytes.

```
offset  size  meaning
0       4     payload size, little-endian
4       1     payload type
5       1     (part of the type field; always 0 in practice)
6       2     payload option / flags
8       8     NTP timestamp, when the type carries one
16      112   type-specific extras (image geometry on parameter sets)
```

Payload types we act on:

* `0x00` — encrypted video: length-prefixed NAL units, AES-CTR encrypted.
* `0x01` — unencrypted parameter sets (SPS/PPS, or VPS/SPS/PPS for HEVC).
  These must be prepended to the *next* video payload rather than sent on their
  own, because a decoder that receives parameter sets separated from the IDR
  they describe may discard them.
* `0x02` — a once-per-second heartbeat from older clients; no payload.
* `0x05` — a performance-report plist from the client.

The decryption is the subtle part. One AES-CTR keystream spans the whole
session, but each payload starts on a fresh 16-byte block, and payloads are not
block-aligned. So a payload's trailing partial block is decrypted as a full
block: the leftover keystream bytes are kept and applied to the first bytes of
the *next* payload, which begins mid-block.
"""

from __future__ import annotations

import plistlib
import socket
import struct
import threading

from airplaya.crypto.aesctr import BLOCK, AesCtrStream
from airplaya.log import TRACE, get_logger
from airplaya.net import bind_exclusive
from airplaya.sink.base import VideoSink
from airplaya.stream import nal

log = get_logger(__name__)

HEADER_LEN = 128
MAX_PAYLOAD = 32 * 1024 * 1024

TYPE_VIDEO = 0x00
TYPE_PARAMETER_SETS = 0x01
TYPE_HEARTBEAT = 0x02
TYPE_PERFORMANCE = 0x05

_HEVC_MARKER = b"hvc1"


class MirrorDecryptor:
    """AES-CTR over the payload sequence, preserving cross-payload alignment."""

    def __init__(self, key: bytes, iv: bytes) -> None:
        self._stream = AesCtrStream(key, iv)
        self._carry = b""  # keystream bytes left over from the previous payload

    def decrypt(self, payload: bytes) -> bytearray:
        out = bytearray(len(payload))
        consumed = 0

        # Finish the block the previous payload left half-used.
        if self._carry:
            head = min(len(self._carry), len(payload))
            for i in range(head):
                out[i] = payload[i] ^ self._carry[i]
            self._carry = self._carry[head:]
            consumed = head
            if self._carry:
                # This payload was shorter than the leftover keystream.
                return out

        # Whole blocks decrypt directly, starting on a block boundary.
        remaining = len(payload) - consumed
        whole = (remaining // BLOCK) * BLOCK
        self._stream.skip_to_block_boundary()
        if whole:
            out[consumed : consumed + whole] = self._stream.process(
                payload[consumed : consumed + whole]
            )

        # The trailing partial block is decrypted as a full block; what we do
        # not need becomes the carry for the next payload.
        tail = remaining - whole
        if tail:
            start = consumed + whole
            padded = payload[start:] + bytes(BLOCK - tail)
            block = self._stream.process(padded)
            out[start:] = block[:tail]
            self._carry = block[tail:]

        return out


class MirrorStream:
    """Accepts one client at a time on the mirroring port and feeds the sink."""

    def __init__(self, host: str, port: int, sink: VideoSink) -> None:
        self._host = host
        self._port = port
        self._sink = sink
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._decryptor: MirrorDecryptor | None = None
        self._codec: str | None = None
        self._pending_parameter_sets: bytes | None = None

    # -- lifecycle -------------------------------------------------------

    def set_keys(self, key: bytes, iv: bytes) -> None:
        """Install the stream keys. A re-key resets the keystream."""
        with self._lock:
            self._decryptor = MirrorDecryptor(key, iv)
            self._pending_parameter_sets = None

    def start(self) -> int:
        """Bind and start accepting. Returns the bound port."""
        if self._server is not None:
            return self._server.getsockname()[1]

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bind_exclusive(server)
        server.bind((self._host, self._port))
        server.listen(1)
        server.settimeout(0.5)
        self._server = server

        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="mirror", daemon=True)
        self._thread.start()

        port = server.getsockname()[1]
        log.info("mirror stream listening on port %d", port)
        return port

    def stop(self) -> None:
        self._stop.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._sink.stop()
        self._codec = None

    # -- accept loop -----------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                return
            try:
                connection, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            log.info("mirror client connected from %s", address[0])
            try:
                self._read_stream(connection)
            except Exception:
                log.exception("mirror stream failed")
            finally:
                try:
                    connection.close()
                except OSError:
                    pass
                log.info("mirror client disconnected")
                self._sink.stop()
                self._codec = None

    def _read_stream(self, connection: socket.socket) -> None:
        connection.settimeout(0.5)
        while not self._stop.is_set():
            header = self._read_exactly(connection, HEADER_LEN)
            if header is None:
                return

            (payload_size,) = struct.unpack_from("<I", header, 0)
            payload_type = header[4]
            option = header[6]
            if payload_size > MAX_PAYLOAD:
                raise ValueError(f"refusing a {payload_size}-byte payload")

            log.debug(
                "mirror packet: type %#04x option %#04x payload %d bytes",
                payload_type,
                option,
                payload_size,
            )

            payload = b""
            if payload_size:
                chunk = self._read_exactly(connection, payload_size)
                if chunk is None:
                    return
                payload = chunk

            self._dispatch(payload_type, option, payload)

    def _read_exactly(self, connection: socket.socket, size: int) -> bytes | None:
        """Read exactly `size` bytes. `None` means the peer went away."""
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            try:
                chunk = connection.recv(remaining)
            except socket.timeout:
                if self._stop.is_set():
                    return None
                continue
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # -- payload handling ------------------------------------------------

    def _dispatch(self, payload_type: int, option: int, payload: bytes) -> None:
        if payload_type == TYPE_VIDEO:
            self._on_video(payload)
        elif payload_type == TYPE_PARAMETER_SETS:
            self._on_parameter_sets(option, payload)
        elif payload_type == TYPE_HEARTBEAT:
            log.log(TRACE, "heartbeat")
        elif payload_type == TYPE_PERFORMANCE:
            self._on_performance(payload)
        else:
            log.debug("ignoring payload type %#02x (%d bytes)", payload_type, len(payload))

    def _on_video(self, payload: bytes) -> None:
        with self._lock:
            decryptor = self._decryptor
            prefix = self._pending_parameter_sets
            self._pending_parameter_sets = None

        if decryptor is None:
            log.warning("video arrived before SETUP installed the stream keys")
            return

        # Parameter sets normally arrive first and are what starts the sink, but
        # the order is not guaranteed: a client that reconnects mid-stream sends
        # video straight away. Without this the sink stays closed and every
        # frame is discarded silently — no window, no error.
        if self._codec is None:
            log.info("video arrived before any parameter sets; assuming h264")
            self._start_sink("h264")

        decrypted = decryptor.decrypt(payload)
        try:
            annex_b, count = nal.to_annex_b(decrypted)
        except nal.NalError as exc:
            # Almost always a key or alignment problem, and the stream will not
            # recover on its own, so say so loudly but keep reading.
            log.error("dropping payload: %s", exc)
            return

        log.log(TRACE, "video payload: %d NAL units, %d bytes", count, len(annex_b))
        if prefix:
            annex_b = prefix + annex_b
        self._sink.write(annex_b)

    def _on_parameter_sets(self, option: int, payload: bytes) -> None:
        if not payload:
            log.error("parameter-set payload was empty")
            return

        is_hevc = payload[4:8] == _HEVC_MARKER
        codec = "h265" if is_hevc else "h264"
        try:
            sets = (
                nal.parameter_sets_h265(payload)
                if is_hevc
                else nal.parameter_sets_h264(payload)
            )
        except nal.NalError as exc:
            log.error("could not parse parameter sets: %s", exc)
            return

        if codec != self._codec:
            self._start_sink(codec)

        with self._lock:
            self._pending_parameter_sets = sets
        log.debug("held %d bytes of %s parameter sets for the next payload", len(sets), codec)

    def _start_sink(self, codec: str) -> None:
        log.info("video codec is %s; (re)starting the sink", codec)
        self._sink.stop()
        try:
            self._sink.start(codec)
        except RuntimeError as exc:
            log.error("%s", exc)
            return
        self._codec = codec

    def _on_performance(self, payload: bytes) -> None:
        if log.getEffectiveLevel() > TRACE:
            return
        try:
            report = plistlib.loads(payload)
        except Exception:
            log.log(TRACE, "performance packet (%d bytes, unparsed)", len(payload))
            return
        log.log(TRACE, "performance report: %s", report)
