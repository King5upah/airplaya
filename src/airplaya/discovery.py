"""mDNS advertisement.

iOS finds a receiver through two Bonjour services on the same port:

* `_airplay._tcp` — the entry that appears in the Screen Mirroring picker.
* `_raop._tcp` — the audio half. Mirroring will not start without it, and its
  instance name must be `<HWADDR>@<name>` with the address in bare uppercase
  hex.

The TXT records are not decoration. `features` advertises what the receiver
can do, and bit 27 ("supports legacy pairing") is what makes the client run
`/pair-setup` — which is where the shared secret the media key depends on comes
from. Turning it off changes the key derivation.
"""

from __future__ import annotations

import re
import socket

from zeroconf import IPVersion, ServiceInfo, Zeroconf

from airplaya.log import get_logger
from airplaya.net import format_hwaddr

log = get_logger(__name__)

# First 32 bits of the features field, with bit 27 on.
FEATURES_LOW = 0x5A7FFEE6
FEATURES_HIGH = 0x0

# A fixed public identifier; clients only check that it stays put.
PUBLIC_ID = "2e388006-13ba-4041-9a67-25dd4a43d536"


def _features_string() -> str:
    return f"0x{FEATURES_LOW:X},0x{FEATURES_HIGH:X}"


def host_name() -> str:
    """The `.local.` name to use as the SRV target.

    This must be the name the machine's *existing* mDNS responder already
    claims. Windows ships Bonjour with iTunes and other Apple software, and on
    such a machine two responders end up on port 5353: ours, and Apple's. If we
    invent our own hostname, the other responder answers queries for it with a
    negative record, because as far as it knows the name does not exist. The
    client then sees the service in its picker, fails to resolve the target, and
    reports that it cannot connect — with no TCP connection ever attempted.

    Using the real hostname avoids the fight: whichever responder answers, the
    address is the same.
    """
    raw = socket.gethostname().split(".")[0]
    # mDNS labels allow letters, digits and hyphens.
    cleaned = re.sub(r"[^A-Za-z0-9-]", "-", raw).strip("-")
    return f"{cleaned or 'airplaya'}.local."


def airplay_txt(device_id: str, model: str, public_key_hex: str, source_version: str) -> dict:
    return {
        "deviceid": device_id,
        "features": _features_string(),
        "flags": "0x4",
        "model": model,
        "pk": public_key_hex,
        "pi": PUBLIC_ID,
        "srcvers": source_version,
        "vv": "2",
        "pw": "false",
    }


def raop_txt(model: str, public_key_hex: str, source_version: str) -> dict:
    return {
        "txtvers": "1",
        "ch": "2",  # stereo
        "cn": "0,1,2,3",  # PCM, ALAC, AAC, AAC-ELD
        "et": "0,3,5",  # none, FairPlay, FairPlay SAPv2.5
        "da": "true",
        "sr": "44100",
        "ss": "16",
        "sv": "false",
        "tp": "UDP",
        "md": "0,1,2",  # text, artwork, progress
        "vn": "65537",
        "vs": source_version,
        "am": model,
        "sf": "0x4",
        "ft": _features_string(),
        "rhd": "5.6.0.0",
        "vv": "2",
        "pk": public_key_hex,
        "pw": "false",
    }


def txt_record_bytes(properties: dict[str, str]) -> bytes:
    """Encode a TXT record the way DNS does: each entry a length-prefixed pair.

    The client asks for this over the control channel as well as over mDNS. Its
    first request is `GET /info` carrying a `qualifier` of `txtAirPlay`, and it
    expects the raw record back under that same key. An empty answer makes iOS
    abandon the session immediately, before it even tries to pair — which looks
    exactly like a network failure from the phone's side.
    """
    out = bytearray()
    for key, value in properties.items():
        entry = f"{key}={value}".encode("utf-8")
        if len(entry) > 255:
            raise ValueError(f"TXT entry {key!r} is too long for one record")
        out.append(len(entry))
        out += entry
    return bytes(out)


class Advertiser:
    """Registers both services for as long as the receiver runs."""

    def __init__(
        self,
        name: str,
        model: str,
        source_version: str,
        hw_addr: bytes,
        public_key: bytes,
        address: str,
        port: int,
    ) -> None:
        self._name = name
        self._address = address
        self._zeroconf: Zeroconf | None = None
        self._services: list[ServiceInfo] = []

        device_id = format_hwaddr(hw_addr)
        raop_instance = format_hwaddr(hw_addr, sep="") + "@" + name
        public_key_hex = public_key.hex()
        packed = self._packed_address(address)

        self._services.append(
            ServiceInfo(
                "_airplay._tcp.local.",
                f"{name}._airplay._tcp.local.",
                addresses=[packed],
                port=port,
                properties=airplay_txt(device_id, model, public_key_hex, source_version),
                server=host_name(),
            )
        )
        self._services.append(
            ServiceInfo(
                "_raop._tcp.local.",
                f"{raop_instance}._raop._tcp.local.",
                addresses=[packed],
                port=port,
                properties=raop_txt(model, public_key_hex, source_version),
                server=host_name(),
            )
        )

    @staticmethod
    def _packed_address(address: str) -> bytes:
        import socket

        return socket.inet_aton(address)

    def start(self) -> None:
        try:
            self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        except OSError as exc:
            raise RuntimeError(
                "could not open the mDNS socket on port 5353. Another responder "
                f"may be holding it exclusively: {exc}"
            ) from exc

        for service in self._services:
            self._zeroconf.register_service(service, allow_name_change=True)
        log.info(
            "advertising %r at %s as %s",
            self._name,
            self._address,
            ", ".join(s.type for s in self._services),
        )

    def stop(self) -> None:
        zeroconf, self._zeroconf = self._zeroconf, None
        if zeroconf is None:
            return
        for service in self._services:
            try:
                zeroconf.unregister_service(service)
            except Exception:
                log.debug("unregistering %s failed", service.name, exc_info=True)
        zeroconf.close()
        log.debug("mDNS advertisement withdrawn")
