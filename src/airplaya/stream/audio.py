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

import socket
import struct
import threading

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
    ) -> None:
        self._host = host
        self._requested = {"data": data_port, "control": control_port}
        self._sockets: dict[str, socket.socket] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._enabled = enabled
        self._device = device
        self._player: AudioPlayer | None = None
        self._playing = False
        self._key: bytes | None = None
        self._iv: bytes | None = None
        self._ct: int | None = None
        self._warned_unsupported = False

        self.packet_count = 0
        self.byte_count = 0

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
                self._player = AudioPlayer(device=self._device)
            try:
                self._player.start(extradata)
                self._playing = True
            except AudioUnavailable as exc:
                log.error("%s", exc)
                self._playing = False

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
                if role == "data":
                    self._on_packet(data)
                else:
                    log.log(TRACE, "audio control packet: %d bytes", len(data))

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
        player.write(frame)
