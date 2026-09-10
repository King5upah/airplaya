"""Receiver configuration.

Port numbers are fixed rather than ephemeral: the iOS client is told which
ports to use in the SETUP response, so any free port works, but fixed ones are
far easier to punch through a firewall and to reason about in a packet capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Config:
    # --- identity, as advertised over mDNS ---
    name: str = "airplaya"
    # `AppleTV3,2` is the model an AirPlay v1 mirroring session expects; iOS
    # gates protocol behaviour on it, so this is not cosmetic.
    model: str = "AppleTV3,2"
    source_version: str = "220.68"

    # --- network ---
    bind_host: str = "0.0.0.0"
    rtsp_port: int = 7000
    mirror_port: int = 7100
    audio_port: int = 6000
    audio_control_port: int = 6001
    timing_port: int = 7010
    advertise_ip: str | None = None  # None => autodetect the primary LAN address

    # --- advertised display ---
    width: int = 1920
    height: int = 1080
    refresh_rate: int = 60
    max_fps: int = 30
    overscanned: bool = False

    # --- audio ---
    audio_enabled: bool = True
    # A PortAudio device index, or part of a device name. None means the
    # system default output.
    audio_device: str | int | None = None

    # --- output ---
    sink: str = "ffplay"  # ffplay | file | null
    sink_path: str | None = None  # target file when sink == "file"
    ffplay_binary: str = "ffplay"
    ffplay_extra_args: list[str] = field(default_factory=list)

    # --- behaviour ---
    # A stale mDNS record makes the receiver appear in the iOS picker but fail
    # to connect, which is confusing; re-register on every start.
    state_dir: str | None = None  # where the persistent Ed25519 key is kept
    verbosity: int = 0
