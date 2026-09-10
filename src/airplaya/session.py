"""Per-connection session state.

One iOS device, one TCP connection to the control port, one `Session`. It owns
the handshake state and the media key; the streams themselves are owned by the
`Receiver`, because their sockets outlive individual control connections.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from airplaya.crypto.fairplay import FairPlaySession
from airplaya.crypto.pairing import DeviceIdentity, PairingSession


@dataclass
class Session:
    identity: DeviceIdentity
    client_address: str
    pairing: PairingSession = field(init=False)
    fairplay: FairPlaySession = field(default_factory=FairPlaySession)

    # Filled in by the first SETUP.
    aes_key: bytes | None = None
    aes_iv: bytes | None = None
    client_name: str | None = None
    client_model: str | None = None
    client_device_id: str | None = None

    def __post_init__(self) -> None:
        self.pairing = PairingSession(self.identity)

    @property
    def keys_ready(self) -> bool:
        return self.aes_key is not None
