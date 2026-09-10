"""Handlers for the control channel.

The sequence a mirroring client actually walks:

```
GET  /info          capabilities, as a binary plist
POST /pair-setup    our Ed25519 public key
POST /pair-verify   X25519 exchange, twice
POST /fp-setup      FairPlay, twice
SETUP               keys and timing, then one call per stream
RECORD              begin
SET_PARAMETER       volume and similar, ignorable
TEARDOWN            end
```

Handlers mutate the `Session` and return nothing; the response object is
theirs to fill in.
"""

from __future__ import annotations

import plistlib
from typing import Protocol

from airplaya.crypto import keys
from airplaya.crypto.fairplay import FairPlayError
from airplaya.crypto.pairing import KEY_LEN, PairingError
from airplaya.discovery import (
    FEATURES_HIGH,
    FEATURES_LOW,
    PUBLIC_ID,
    airplay_txt,
    raop_txt,
    txt_record_bytes,
)
from airplaya.log import get_logger
from airplaya.net import format_hwaddr
from airplaya.rtsp.message import Request, Response
from airplaya.session import Session

log = get_logger(__name__)

BINARY_PLIST = "application/x-apple-binary-plist"
OCTET_STREAM = "application/octet-stream"

STREAM_TYPE_MIRROR = 110
STREAM_TYPE_AUDIO = 96

# A stable display UUID; iOS keys per-display settings off it.
_DISPLAY_UUID = "e0ff8a27-6738-3d56-8a16-cc53aacee925"


class ReceiverServices(Protocol):
    """What a handler needs from the receiver. Keeps the two testable apart."""

    def mirror_port(self) -> int: ...

    def set_mirror_keys(self, key: bytes, iv: bytes) -> None: ...

    def audio_ports(self) -> dict[str, int]: ...

    def timing_port(self) -> int: ...

    def start_timing(self, address: str, port: int) -> None: ...

    def teardown_streams(self) -> None: ...


def handle_info(session: Session, request: Request, response: Response, services, config, hw_addr) -> None:
    """Report capabilities.

    Two shapes of request arrive. A qualifier plist asks only for the TXT
    records; anything else wants the full capability dictionary.
    """
    body: dict[str, object] = {}
    device_id = format_hwaddr(hw_addr)
    public_key_hex = session.identity.public_key.hex()

    # The client's first request asks for a TXT record by name, either in a
    # plist qualifier or — during Bluetooth-assisted discovery, which sends no
    # CSeq — in the URL itself.
    qualifiers: list[str] = []
    if request.is_binary_plist and request.body:
        try:
            parsed = plistlib.loads(request.body)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            requested = parsed.get("qualifier")
            if isinstance(requested, list):
                qualifiers = [q for q in requested if isinstance(q, str)]
    if request.header("cseq") is None:
        qualifiers += [q for q in ("txtAirPlay", "txtRAOP") if q in request.url]

    if qualifiers:
        log.debug("/info qualifier request: %s", qualifiers)
        if "txtAirPlay" in qualifiers:
            body["txtAirPlay"] = txt_record_bytes(
                airplay_txt(device_id, config.model, public_key_hex, config.source_version)
            )
        if "txtRAOP" in qualifiers:
            body["txtRAOP"] = txt_record_bytes(
                raop_txt(config.model, public_key_hex, config.source_version)
            )
        response.set_body(plistlib.dumps(body, fmt=plistlib.FMT_BINARY), BINARY_PLIST)
        return

    body = {
        "deviceID": device_id,
        "macAddress": device_id,
        "pk": session.identity.public_key,
        "features": (FEATURES_HIGH << 32) | FEATURES_LOW,
        "name": config.name,
        "model": config.model,
        "pi": PUBLIC_ID,
        "vv": 2,
        "statusFlags": 68,
        "keepAliveLowPower": 1,
        "keepAliveSendStatsAsBody": True,
        "sourceVersion": config.source_version,
        "initialVolume": -20.0,
        "audioLatencies": [
            {
                "type": 100,
                "audioType": "default",
                "inputLatencyMicros": 0,
                "outputLatencyMicros": False,
            },
            {
                "type": 101,
                "audioType": "default",
                "inputLatencyMicros": 0,
                "outputLatencyMicros": False,
            },
        ],
        "audioFormats": [
            {"type": 100, "audioInputFormats": 0x3FFFFFC, "audioOutputFormats": 0x3FFFFFC},
            {"type": 101, "audioInputFormats": 0x3FFFFFC, "audioOutputFormats": 0x3FFFFFC},
        ],
        "displays": [
            {
                "uuid": _DISPLAY_UUID,
                "width": config.width,
                "height": config.height,
                "widthPixels": config.width,
                "heightPixels": config.height,
                "widthPhysical": 0,
                "heightPhysical": 0,
                "rotation": False,
                "refreshRate": 1.0 / config.refresh_rate,
                "maxFPS": config.max_fps,
                "overscanned": config.overscanned,
                "features": 14,
            }
        ],
    }
    response.set_body(plistlib.dumps(body, fmt=plistlib.FMT_BINARY), BINARY_PLIST)


def handle_pair_setup(session: Session, request: Request, response: Response) -> None:
    response.set_body(session.pairing.setup(), OCTET_STREAM)


def handle_pair_verify(session: Session, request: Request, response: Response) -> None:
    data = request.body
    if len(data) < 4:
        raise PairingError(f"pair-verify body is only {len(data)} bytes")

    step = data[0]
    payload = data[4:]
    if step == 1:
        if len(payload) != 2 * KEY_LEN:
            raise PairingError("pair-verify step 1 body has the wrong length")
        reply = session.pairing.verify_start(payload[:KEY_LEN], payload[KEY_LEN:])
        response.set_body(reply, OCTET_STREAM)
    elif step == 0:
        session.pairing.verify_finish(payload)
        response.add_header("Content-Type", OCTET_STREAM)
    else:
        raise PairingError(f"unknown pair-verify step {step}")


def handle_fp_setup(session: Session, request: Request, response: Response) -> None:
    data = request.body
    if len(data) == 16:
        response.set_body(session.fairplay.setup(data), OCTET_STREAM)
    elif len(data) == 164:
        response.set_body(session.fairplay.handshake(data), OCTET_STREAM)
    else:
        raise FairPlayError(f"unexpected fp-setup body of {len(data)} bytes")


def handle_options(session: Session, request: Request, response: Response) -> None:
    response.add_header(
        "Public",
        "SETUP, RECORD, FLUSH, TEARDOWN, OPTIONS, GET_PARAMETER, SET_PARAMETER",
    )


def handle_setup(
    session: Session,
    request: Request,
    response: Response,
    services: ReceiverServices,
) -> None:
    """Install keys, then answer one entry per requested stream.

    SETUP arrives at least twice: once carrying `ekey`/`eiv` and the timing
    port, then once per media stream.
    """
    try:
        body = plistlib.loads(request.body) if request.body else {}
    except Exception as exc:
        raise ValueError(f"SETUP body is not a plist: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("SETUP body is not a dictionary")

    reply: dict[str, object] = {}

    if "ekey" in body and "eiv" in body:
        _setup_keys(session, body, request, response, services, reply)

    streams = body.get("streams")
    if isinstance(streams, list):
        reply["streams"] = [
            _setup_stream(session, stream, services)
            for stream in streams
            if isinstance(stream, dict)
        ]

    response.set_body(plistlib.dumps(reply, fmt=plistlib.FMT_BINARY), BINARY_PLIST)


def _setup_keys(
    session: Session,
    body: dict,
    request: Request,
    response: Response,
    services: ReceiverServices,
    reply: dict[str, object],
) -> None:
    session.client_device_id = body.get("deviceID")
    session.client_model = body.get("model")
    session.client_name = body.get("name")
    log.info(
        "client: %s (%s), deviceID %s",
        session.client_name,
        session.client_model,
        session.client_device_id,
    )

    eiv = bytes(body["eiv"])
    ekey = bytes(body["ekey"])
    if len(eiv) < 16 or len(ekey) < 72:
        raise ValueError(f"SETUP key material is too short: eiv {len(eiv)}, ekey {len(ekey)}")

    aes_key = session.fairplay.decrypt_session_key(ekey[:72])

    shared_secret = session.pairing.shared_secret
    if shared_secret is not None:
        # Pairing happened, so the client hashed the key with the shared secret
        # and we must do the same. Clients that skip pairing use the key as-is.
        aes_key = keys.mix_with_shared_secret(aes_key, shared_secret)
        log.debug("mixed the AES key with the pairing shared secret")
    else:
        log.debug("no pairing secret; using the AES key unmodified")

    session.aes_key = aes_key
    session.aes_iv = eiv[:16]

    timing_protocol = body.get("timingProtocol")
    if timing_protocol not in (None, "NTP"):
        # "None" means the AirPlay 2 remote-control protocol, which this
        # receiver does not speak. Mirroring usually still works.
        log.warning("client asked for timingProtocol=%r; only NTP is supported", timing_protocol)

    timing_port = int(body.get("timingPort") or 0)
    if timing_port:
        services.start_timing(session.client_address, timing_port)
    else:
        log.warning("client sent no timingPort; clock sync is disabled")

    reply["timingPort"] = services.timing_port()
    # The event channel is unused for mirroring; reporting 0 keeps the client
    # from opening a connection nothing would answer.
    reply["eventPort"] = 0


def _setup_stream(session: Session, stream: dict, services: ReceiverServices) -> dict:
    stream_type = int(stream.get("type") or 0)

    if stream_type == STREAM_TYPE_MIRROR:
        if not session.keys_ready:
            raise ValueError("mirror SETUP arrived before the key exchange")
        stream_id = int(stream.get("streamConnectionID") or 0)
        assert session.aes_key is not None
        key, iv = keys.mirror_key_and_iv(session.aes_key, stream_id)
        services.set_mirror_keys(key, iv)
        log.info("mirror stream ready (streamConnectionID %d)", stream_id)
        return {"type": STREAM_TYPE_MIRROR, "dataPort": services.mirror_port()}

    if stream_type == STREAM_TYPE_AUDIO:
        ports = services.audio_ports()
        log.info("audio stream requested (ct=%s); packets will be discarded", stream.get("ct"))
        return {
            "type": STREAM_TYPE_AUDIO,
            "dataPort": ports["data"],
            "controlPort": ports["control"],
        }

    log.warning("ignoring unsupported stream type %d", stream_type)
    return {"type": stream_type}


def handle_get_parameter(session: Session, request: Request, response: Response) -> None:
    """Answer parameter queries. Only volume is ever asked for."""
    body = request.body.decode("utf-8", "replace")
    if "volume" in body:
        response.set_body(b"volume: 0.000000\r\n", "text/parameters")
    else:
        response.set_body(b"", "text/parameters")


def handle_set_parameter(session: Session, request: Request, response: Response) -> None:
    if request.content_type.startswith("text/parameters"):
        log.debug("SET_PARAMETER: %s", request.body.decode("utf-8", "replace").strip())


def handle_record(session: Session, request: Request, response: Response) -> None:
    log.info("client sent RECORD; mirroring is live")
    response.add_header("Audio-Latency", "11025")
    response.add_header("Audio-Jack-Status", "connected; type=analog")


def handle_teardown(
    session: Session,
    request: Request,
    response: Response,
    services: ReceiverServices,
) -> None:
    """End the session.

    TEARDOWN also arrives per-stream mid-session, but tearing everything down
    on any TEARDOWN is fine here: this receiver serves a single client, and the
    client reconnects with a fresh SETUP.
    """
    log.info("client sent TEARDOWN")
    services.teardown_streams()
    response.close_connection = True


def handle_feedback(session: Session, request: Request, response: Response) -> None:
    """A keep-alive. An empty 200 is the whole contract."""
