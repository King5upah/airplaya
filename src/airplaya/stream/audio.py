"""The audio stream (UDP, ports 6000 and 6001).

Audio arrives as RTP: a 12-byte header, then the payload. Encryption is
AES-128-CBC with the session key and IV, re-initialised for every packet, and
covering only the whole 16-byte blocks — a trailing partial block is sent in the
clear.

What is inside depends on the compression type the client announced in SETUP:

* `ct = 8` — AAC-ELD, 480 samples per frame. This is what screen mirroring
  uses. The frames go through the LATM packetizer to the decoder.
* `ct = 2` — ALAC. Playing it needs the codec's magic cookie, which the client
  sends in a 44-byte format packet we do not parse yet, so ALAC is announced and
  then dropped rather than played badly.

Before the first real frame, an AAC-ELD stream sends packets whose entire
payload is the four bytes `00 68 34 00`. They are placeholders and must not
reach the decoder.
"""

from __future__ import annotations

import queue
import selectors
import socket
import struct
import threading
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from airplaya.log import TRACE, get_logger
from airplaya.net import bind_exclusive
from airplaya.sink.audio import AudioPlayer, AudioUnavailable
from airplaya.stream.asc import AOT_AAC_ELD, audio_specific_config

log = get_logger(__name__)

RTP_HEADER_LEN = 12
_MAX_DATAGRAM = 8192
_NO_DATA_MARKER = b"\x00\x68\x34\x00"

CT_ALAC = 2
CT_AAC_MAIN = 4
CT_AAC_ELD = 8

_CT_NAMES = {CT_ALAC: "ALAC", CT_AAC_MAIN: "AAC", CT_AAC_ELD: "AAC-ELD"}

# ALAC and AAC-ELD each have a fixed frame length in an AirPlay stream.
_DEFAULT_FRAME_LENGTH = {CT_AAC_ELD: 480, CT_ALAC: 352}


_SEQ_MODULO = 1 << 16
_SEQ_HALF = _SEQ_MODULO // 2


def _is_ahead(a: int, b: int) -> bool:
    """True when sequence `a` is later than `b`, allowing for wraparound."""
    return ((a - b) % _SEQ_MODULO) < _SEQ_HALF


class JitterBuffer:
    """Puts RTP frames back in order, and gives up on ones that never arrive.

    UDP over Wi-Fi reorders and loses packets. Handing those straight to an
    AAC-ELD decoder is heard as crackling: a frame decoded out of order is
    noise, and a missing frame leaves the decoder mid-stream.

    So frames wait here briefly. `depth` frames of slack is enough to absorb
    normal reordering — at 480 samples each, that is about 11 ms per frame — and
    once `max_depth` is exceeded the gap is declared lost and the buffer skips
    ahead rather than stalling the stream indefinitely.
    """

    def __init__(self, depth: int = 3, max_depth: int = 24) -> None:
        self._depth = depth
        self._max_depth = max_depth
        self._frames: dict[int, bytes] = {}
        self._expected: int | None = None
        self.lost = 0
        self.duplicates = 0
        self.late = 0

    def push(self, sequence: int, frame: bytes) -> list[bytes]:
        """Add a frame; return whichever frames are now ready, in order."""
        if self._expected is None:
            self._expected = sequence

        if sequence in self._frames:
            self.duplicates += 1
            return []
        if sequence != self._expected and _is_ahead(self._expected, sequence):
            # Older than what we have already released: too late to use.
            self.late += 1
            return []

        self._frames[sequence] = frame

        # Release only what keeps `depth` frames of slack in hand, so a frame
        # that arrives out of order still has time to land.
        ready: list[bytes] = []
        while len(self._frames) > self._depth:
            held = self._frames.pop(self._expected, None)
            if held is not None:
                ready.append(held)
                self._expected = (self._expected + 1) % _SEQ_MODULO
                continue
            if len(self._frames) < self._max_depth:
                break
            # The buffer is full and the frame we want has not arrived. Treat
            # the gap as lost and resume from the oldest frame we do have.
            oldest = min(self._frames, key=lambda s: (s - self._expected) % _SEQ_MODULO)
            self.lost += (oldest - self._expected) % _SEQ_MODULO
            self._expected = oldest

        return ready

    def reset(self) -> None:
        self._frames.clear()
        self._expected = None


def decrypt_packet(key: bytes, iv: bytes, payload: bytes) -> bytes:
    """Decrypt one RTP audio payload.

    The cipher state does not carry between packets: each one starts from the
    session IV, and any bytes past the last whole block are already plaintext.
    """
    whole = len(payload) // 16 * 16
    if whole == 0:
        return payload
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    head = decryptor.update(payload[:whole]) + decryptor.finalize()
    return head + payload[whole:]


class AudioStream:
    """Receives, decrypts and plays the mirrored audio."""

    def __init__(
        self,
        host: str,
        data_port: int,
        control_port: int,
        device: str | int | None = None,
        enabled: bool = True,
        recorder=None,
    ) -> None:
        self._host = host
        self._requested = {"data": data_port, "control": control_port}
        self._sockets: dict[str, socket.socket] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._enabled = enabled
        self._device = device
        self._recorder = recorder
        self._player: AudioPlayer | None = None
        self._playing = False
        self._jitter = JitterBuffer()
        # Decoding happens off the receive thread: an AAC decode takes long
        # enough that doing it inline delays the next packet, and the delay is
        # audible.
        self._decode_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._decode_thread: threading.Thread | None = None
        self._last_stats = 0.0
        self._key: bytes | None = None
        self._iv: bytes | None = None
        self._ct: int | None = None
        self._warned_unsupported = False

        self.packet_count = 0
        self.byte_count = 0
        self.frame_count = 0
        self.data_packet_count = 0
        self.control_packet_count = 0
        self.decode_backlog_drops = 0

    # -- configuration ---------------------------------------------------

    def set_keys(self, key: bytes, iv: bytes) -> None:
        with self._lock:
            self._key = key
            self._iv = iv

    def set_device(self, device: str | int | None) -> None:
        """Choose the output device. Takes effect on the next stream."""
        self._device = device

    def configure_format(self, ct: int, frame_length: int | None = None) -> None:
        """Prepare for the format the client announced in SETUP."""
        with self._lock:
            self._ct = ct
            self._warned_unsupported = False
            name = _CT_NAMES.get(ct, f"ct={ct}")

            if not self._enabled:
                log.info("audio is disabled; %s packets will be discarded", name)
                return

            if ct != CT_AAC_ELD:
                log.warning(
                    "audio format %s is not supported yet; packets will be discarded",
                    name,
                )
                self._teardown_player()
                return

            samples = frame_length or _DEFAULT_FRAME_LENGTH[CT_AAC_ELD]
            log.info("audio format %s, %d samples per frame", name, samples)
            extradata = audio_specific_config(
                object_type=AOT_AAC_ELD, frame_length=samples
            )
            if self._player is None:
                self._player = AudioPlayer(device=self._device, pcm_tap=self._on_pcm)
            try:
                self._player.start(extradata)
                self._playing = True
            except AudioUnavailable as exc:
                log.error("%s", exc)
                self._playing = False
                return

            self._jitter.reset()
            if self._decode_thread is None:
                self._decode_thread = threading.Thread(
                    target=self._decode_loop, name="audio-decode", daemon=True
                )
                self._decode_thread.start()

    # -- lifecycle -------------------------------------------------------

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
        self._thread = threading.Thread(target=self._receive, name="audio", daemon=True)
        self._thread.start()

        ports = {role: sock.getsockname()[1] for role, sock in self._sockets.items()}
        log.info("audio sockets open on %s", ports)
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
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._teardown_player()

    def end_session(self) -> None:
        """Called on TEARDOWN: stop playing but keep the ports bound."""
        with self._lock:
            self._teardown_player()
            self._ct = None

    def _teardown_player(self) -> None:
        player, self._player = self._player, None
        self._playing = False
        if player is not None:
            player.stop()

    # -- receive loop ----------------------------------------------------

    def _receive(self) -> None:
        # One selector over both sockets rather than a blocking read on each in
        # turn: audio arrives about 90 times a second, and a half-second wait on
        # whichever socket happens to be idle would throttle the other to a
        # couple of packets a second.
        selector = selectors.DefaultSelector()
        for role, sock in self._sockets.items():
            sock.setblocking(False)
            selector.register(sock, selectors.EVENT_READ, role)

        try:
            self._select_loop(selector)
        finally:
            selector.close()

    def _select_loop(self, selector: selectors.BaseSelector) -> None:
        while not self._stop.is_set():
            try:
                ready = selector.select(timeout=0.5)
            except OSError:
                return
            for key, _ in ready:
                role = key.data
                sock = key.fileobj
                try:
                    data, _ = sock.recvfrom(_MAX_DATAGRAM)  # type: ignore[union-attr]
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    return
                self.packet_count += 1
                self.byte_count += len(data)
                if self.packet_count == 1:
                    # Worth an INFO line: "no audio at all" and "audio that is
                    # not coming out of the speakers" need completely different
                    # investigations, and this is the line that tells them
                    # apart.
                    log.info(
                        "first audio packet arrived on the %s port (%d bytes)",
                        role,
                        len(data),
                    )
                if role == "data":
                    self.data_packet_count += 1
                    if self.data_packet_count <= 3:
                        log.info(
                            "audio data packet %d: %d bytes, payload type %#04x",
                            self.data_packet_count,
                            len(data),
                            data[1] & 0x7F if len(data) > 1 else 0,
                        )
                    self._on_packet(data)
                else:
                    self.control_packet_count += 1
                    if self.control_packet_count <= 3:
                        log.info(
                            "audio control packet %d: %d bytes, payload type %#04x",
                            self.control_packet_count,
                            len(data),
                            data[1] & 0x7F if len(data) > 1 else 0,
                        )

    def _on_packet(self, packet: bytes) -> None:
        if len(packet) <= RTP_HEADER_LEN:
            return

        with self._lock:
            player = self._player
            playing = self._playing
            key, iv = self._key, self._iv

        if player is None or not playing:
            return
        if key is None or iv is None:
            log.warning("audio arrived before SETUP installed the keys")
            return

        payload = packet[RTP_HEADER_LEN:]
        frame = decrypt_packet(key, iv, payload)
        if frame[:4] == _NO_DATA_MARKER and len(frame) <= 8:
            log.log(TRACE, "audio placeholder packet")
            return

        sequence = struct.unpack_from(">H", packet, 2)[0]
        log.log(TRACE, "audio frame: seq %d, %d bytes", sequence, len(frame))

        for ordered in self._jitter.push(sequence, frame):
            self.frame_count += 1
            try:
                self._decode_queue.put_nowait(ordered)
            except queue.Full:
                # The decoder is behind. Dropping the newest frame keeps the
                # backlog from becoming latency.
                self.decode_backlog_drops += 1

        self._maybe_log_stats()

    def _on_pcm(self, pcm: bytes) -> None:
        recorder = self._recorder
        if recorder is not None and recorder.recording:
            recorder.add_audio(pcm)

    def _decode_loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._decode_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            with self._lock:
                player = self._player
            if player is None:
                continue
            player.write(frame)

    def _maybe_log_stats(self) -> None:
        """Report often enough to diagnose glitches, rarely enough to read."""
        now = time.monotonic()
        if now - self._last_stats < 5.0:
            return
        self._last_stats = now

        with self._lock:
            player = self._player
        buffered = player.buffered_ms if player is not None else 0
        underruns = player.underruns if player is not None else 0
        log.info(
            "audio: %d frames, %d lost, %d late, %d dup, %d backlog drops, "
            "%d underruns, %d ms buffered",
            self.frame_count,
            self._jitter.lost,
            self._jitter.late,
            self._jitter.duplicates,
            self.decode_backlog_drops,
            underruns,
            buffered,
        )
