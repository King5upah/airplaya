"""Mirroring over the USB cable, using the QuickTime screen-capture protocol.

Over Wi-Fi the phone is the client: it finds us through mDNS and connects. Over
the cable that is inverted — the phone exposes a hidden USB configuration with
two extra bulk endpoints, and the host drives the conversation. There is no
pairing, no FairPlay, and no encryption: the frames arrive in the clear, at the
full frame rate, with no network in the middle.

    iPhone ──USB bulk──> frames ──> CMSampleBuffer ──> H.264 ──> the same sink

The protocol was reverse engineered by Daniel Paulus for `quicktime_video_hack`
(MIT); `docs/airplay.md` records what we learned from it and where this
implementation differs.
"""

from airplaya.wired.receiver import WiredReceiver
from airplaya.wired.usb import UsbUnavailable, WiredDevice, find_devices

__all__ = ["WiredReceiver", "WiredDevice", "UsbUnavailable", "find_devices"]
