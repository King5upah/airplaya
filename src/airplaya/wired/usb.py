"""Finding the phone on USB, and opening its hidden AV endpoints.

An iPhone normally shows one USB configuration: vendor class, subclass `0xFE`,
two bulk endpoints, which is the pipe `usbmuxd` uses for everything Xcode and
iTunes do. A vendor control request unlocks a second configuration, subclass
`0x2A`, with two more bulk endpoints that carry screen mirroring.

```
        control(0x40, 0x52, wValue=0, wIndex=2)
iPhone ──────────────────────────────────────> re-enumerates
        config 1: usbmux (0xFE)   config N: usbmux + AV (0x2A)
```

The device drops off the bus and comes back, so the handle has to be reopened
afterwards — which is also why activation is a separate step from streaming.

On Windows this needs a driver that permits selecting a non-first configuration.
WinUSB and libusbK cannot, which is why a filter driver is the prerequisite the
app checks for; `docs/wired.md` covers the details. Nothing here replaces
Apple's driver — the filter sits above it and the usbmux pipe keeps working.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from airplaya.log import get_logger

log = get_logger(__name__)

APPLE_VENDOR_ID = 0x05AC

_VENDOR_CLASS = 0xFF
_USBMUX_SUBCLASS = 0xFE
_QUICKTIME_SUBCLASS = 0x2A

_ENABLE_REQUEST_TYPE = 0x40  # host to device, vendor, device recipient
_ENABLE_REQUEST = 0x52
_ENABLE_INDEX = 0x02
_DISABLE_INDEX = 0x00

_CLEAR_FEATURE_TYPE = 0x02  # host to device, standard, endpoint recipient
_CLEAR_FEATURE = 0x01

# A single read has to be able to hold a whole bulk transfer; keyframes on a
# large screen run past 256 KB.
_READ_SIZE = 512 * 1024
_READ_TIMEOUT_MS = 1000
_WRITE_TIMEOUT_MS = 2000

# The device needs a moment to re-enumerate after the control request.
_ACTIVATION_ATTEMPTS = 20
_ACTIVATION_DELAY = 0.5

try:  # pragma: no cover - depends on the environment
    import usb.core
    import usb.util
except Exception:  # noqa: BLE001
    usb = None

try:  # pragma: no cover - depends on the environment
    import libusb_package
except Exception:  # noqa: BLE001
    libusb_package = None


class UsbUnavailable(RuntimeError):
    """USB cannot be used, with a message explaining what is missing."""


class UsbError(RuntimeError):
    """A USB operation failed in a way worth showing the user."""


def _require_usb():
    if usb is None:
        raise UsbUnavailable(
            "mirroring over the cable needs the pyusb package: pip install pyusb"
        )
    return usb


def _backend():
    """The libusb backend, preferring the one shipped with `libusb-package`.

    Without it pyusb hunts for a `libusb-1.0` DLL on PATH, which is not
    something a desktop app can rely on being there.
    """
    if libusb_package is None:
        return None
    try:
        return libusb_package.get_libusb1_backend()
    except Exception as exc:  # noqa: BLE001
        log.debug("libusb-package has no usable backend: %s", exc)
        return None


@dataclass
class WiredDevice:
    """An iPhone or iPad seen on USB."""

    serial: str
    product: str
    bus: int | None
    address: int | None
    usbmux_config: int | None
    quicktime_config: int | None

    @property
    def activated(self) -> bool:
        """True when the AV configuration is present and selectable."""
        return self.quicktime_config is not None

    @property
    def udid(self) -> str:
        """The serial in the form the rest of Apple's tooling uses.

        Most devices report a 40-character serial that is already the UDID.
        Some report 24 characters, and everything else inserts a dash after the
        eighth, so a serial copied from here matches what Xcode shows.
        """
        serial = self.serial.strip("\x00")
        if len(serial) == 24:
            return f"{serial[:8]}-{serial[8:]}"
        return serial

    def as_dict(self) -> dict:
        return {
            "serial": self.serial,
            "udid": self.udid,
            "name": self.product,
            "mirroringEnabled": self.activated,
        }

    def __str__(self) -> str:
        state = "ready" if self.activated else "not activated"
        return f"{self.product} ({self.udid}) — {state}"


def _describe(device) -> WiredDevice:
    usbmux_config = None
    quicktime_config = None

    try:
        configurations = list(device)
    except Exception as exc:  # noqa: BLE001
        # On Windows a driver that only exposes the active configuration lands
        # here; the caller turns that into advice about the filter driver.
        log.debug("could not read the configurations: %s", exc)
        configurations = []

    for configuration in configurations:
        subclasses = {
            interface.bInterfaceSubClass
            for interface in configuration
            if interface.bInterfaceClass == _VENDOR_CLASS
        }
        if _QUICKTIME_SUBCLASS in subclasses:
            quicktime_config = configuration.bConfigurationValue
        elif _USBMUX_SUBCLASS in subclasses:
            usbmux_config = configuration.bConfigurationValue

    return WiredDevice(
        serial=_string(device, "serial_number"),
        product=_string(device, "product") or "iOS device",
        bus=getattr(device, "bus", None),
        address=getattr(device, "address", None),
        usbmux_config=usbmux_config,
        quicktime_config=quicktime_config,
    )


def _string(device, attribute: str) -> str:
    try:
        return str(getattr(device, attribute) or "")
    except Exception:  # noqa: BLE001 - a descriptor read can fail mid-reset
        return ""


def _raw_devices() -> list:
    core = _require_usb().core
    try:
        return list(
            core.find(find_all=True, idVendor=APPLE_VENDOR_ID, backend=_backend())
        )
    except core.NoBackendError as exc:
        raise UsbUnavailable(
            "no libusb backend was found: pip install libusb-package"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise UsbError(f"could not enumerate USB devices: {exc}") from exc


def find_devices() -> list[WiredDevice]:
    """Every Apple device on USB that speaks the usbmux or AV protocol."""
    found = []
    for device in _raw_devices():
        described = _describe(device)
        if described.usbmux_config is None and described.quicktime_config is None:
            continue  # a keyboard, a hub, some other Apple peripheral
        found.append(described)
    return found


def _open(serial: str | None):
    """Find one raw device handle, by serial or the first one that qualifies."""
    candidates = []
    for device in _raw_devices():
        described = _describe(device)
        if described.usbmux_config is None and described.quicktime_config is None:
            continue
        if serial and described.serial != serial and described.udid != serial:
            continue
        candidates.append((device, described))

    if not candidates:
        if serial:
            raise UsbError(f"no device with serial {serial!r} is connected")
        raise UsbError("no iPhone or iPad is connected over USB")
    return candidates[0]


def activate(serial: str | None = None) -> WiredDevice:
    """Turn on the AV configuration, waiting for the device to come back.

    Safe to call when it is already on: the request is skipped and the device
    is returned as it is.
    """
    device, described = _open(serial)
    if described.activated:
        log.debug("%s already exposes the AV configuration", described.product)
        return described

    log.info("asking %s to expose its AV configuration", described.product)
    try:
        device.ctrl_transfer(
            _ENABLE_REQUEST_TYPE, _ENABLE_REQUEST, 0x00, _ENABLE_INDEX, None
        )
    except Exception as exc:  # noqa: BLE001
        # The device usually disconnects before it can answer, so a failure
        # here is normal and says nothing about whether it worked.
        log.debug("the activation request did not complete cleanly: %s", exc)
    finally:
        _dispose(device)

    target = described.serial
    for attempt in range(_ACTIVATION_ATTEMPTS):
        time.sleep(_ACTIVATION_DELAY)
        try:
            handle, described = _open(target or serial)
        except UsbError:
            continue
        _dispose(handle)
        if described.activated:
            log.info("the AV configuration is available")
            return described
        log.debug("waiting for the AV configuration (attempt %d)", attempt + 1)

    raise UsbError(
        "the phone did not expose its AV configuration. On Windows this needs "
        "the USB filter driver installed for the device; without it Windows "
        "cannot select the configuration mirroring lives on"
    )


def deactivate(serial: str | None = None) -> None:
    """Put the device back to the usbmux-only configuration.

    Left activated, the extra endpoints stay exposed and some versions of
    iTunes stop seeing the device, so this runs on the way out.
    """
    try:
        device, described = _open(serial)
    except (UsbError, UsbUnavailable):
        return
    try:
        device.ctrl_transfer(
            _ENABLE_REQUEST_TYPE, _ENABLE_REQUEST, 0x00, _DISABLE_INDEX, None
        )
        if described.usbmux_config is not None:
            try:
                device.set_configuration(described.usbmux_config)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.debug("could not restore the usbmux configuration: %s", exc)
    finally:
        _dispose(device)


_SET_CONFIGURATION = 0x09


def _select_configuration(device, value: int) -> None:
    """Make `value` the active configuration.

    Some Windows backends refuse the library call — or accept it and do
    nothing — while still passing a raw control request through to the device.
    So the library call comes first and the standard request is the fallback,
    with the active configuration read back to tell which of them worked.
    """
    try:
        device.set_configuration(value)
    except Exception as exc:  # noqa: BLE001
        log.debug("set_configuration(%d) was refused: %s", value, exc)

    try:
        if device.get_active_configuration().bConfigurationValue == value:
            return
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read back the active configuration: %s", exc)

    log.debug("asking for configuration %d with a raw control request", value)
    device.ctrl_transfer(0x00, _SET_CONFIGURATION, value, 0, None)
    active = device.get_active_configuration().bConfigurationValue
    if active != value:
        raise UsbError(
            f"the device stayed on configuration {active} instead of {value}"
        )


def _dispose(device) -> None:
    try:
        _require_usb().util.dispose_resources(device)
    except Exception:  # noqa: BLE001
        pass


class UsbLink:
    """The pair of bulk endpoints, framed.

    Frames are length-prefixed: four little-endian bytes counting themselves,
    then the payload. Bulk reads do not respect those boundaries — one read can
    return half a frame or three — so the reader buffers and hands out whole
    frames only.
    """

    def __init__(self, serial: str | None = None) -> None:
        self._serial = serial
        self._device = None
        self._interface = None
        self._in_endpoint = None
        self._out_endpoint = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self.device: WiredDevice | None = None

    def open(self) -> WiredDevice:
        """Claim the AV interface. The device must already be activated."""
        util = _require_usb().util
        device, described = _open(self._serial)
        if not described.activated:
            _dispose(device)
            raise UsbError("this device has not been activated for mirroring")

        try:
            _select_configuration(device, described.quicktime_config)
        except Exception as exc:  # noqa: BLE001
            _dispose(device)
            raise UsbError(
                f"could not select the AV configuration: {exc}. On Windows this "
                "needs the USB filter driver installed for the device"
            ) from exc

        configuration = device.get_active_configuration()
        interface = None
        for candidate in configuration:
            if (
                candidate.bInterfaceClass == _VENDOR_CLASS
                and candidate.bInterfaceSubClass == _QUICKTIME_SUBCLASS
            ):
                interface = candidate
                break
        if interface is None:
            _dispose(device)
            raise UsbError("the AV configuration has no mirroring interface")

        in_endpoint = _find_endpoint(util, interface, util.ENDPOINT_IN)
        out_endpoint = _find_endpoint(util, interface, util.ENDPOINT_OUT)
        if in_endpoint is None or out_endpoint is None:
            _dispose(device)
            raise UsbError("the mirroring interface is missing a bulk endpoint")

        try:
            util.claim_interface(device, interface.bInterfaceNumber)
        except Exception as exc:  # noqa: BLE001
            _dispose(device)
            raise UsbError(f"could not claim the mirroring interface: {exc}") from exc

        # A previous session can leave an endpoint halted, and a halted
        # endpoint reads nothing for ever rather than failing.
        for endpoint in (in_endpoint, out_endpoint):
            try:
                device.ctrl_transfer(
                    _CLEAR_FEATURE_TYPE,
                    _CLEAR_FEATURE,
                    0,
                    endpoint.bEndpointAddress,
                    None,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("clearing endpoint %#x failed: %s", endpoint.bEndpointAddress, exc)

        self._device = device
        self._interface = interface
        self._in_endpoint = in_endpoint
        self._out_endpoint = out_endpoint
        self.device = described
        self._stop.clear()
        log.info("USB link open to %s", described.product)
        return described

    def write(self, data: bytes) -> None:
        endpoint = self._out_endpoint
        if endpoint is None:
            raise UsbError("the USB link is not open")
        with self._write_lock:
            endpoint.write(data, _WRITE_TIMEOUT_MS)

    def frames(self) -> Iterator[bytes]:
        """Yield whole frames with the length prefix stripped."""
        core = _require_usb().core
        endpoint = self._in_endpoint
        if endpoint is None:
            raise UsbError("the USB link is not open")

        buffer = bytearray()
        while not self._stop.is_set():
            try:
                chunk = endpoint.read(_READ_SIZE, _READ_TIMEOUT_MS)
            except core.USBTimeoutError:
                continue
            except core.USBError as exc:
                if self._stop.is_set():
                    return
                raise UsbError(f"the USB link failed: {exc}") from exc

            buffer += bytes(chunk)
            while len(buffer) >= 4:
                length = int.from_bytes(buffer[:4], "little")
                if length < 4:
                    raise UsbError(f"a frame claims to be {length} bytes")
                if len(buffer) < length:
                    break
                yield bytes(buffer[4:length])
                del buffer[:length]

    def close(self) -> None:
        self._stop.set()
        util = _require_usb().util if usb is not None else None
        device, self._device = self._device, None
        interface, self._interface = self._interface, None
        self._in_endpoint = None
        self._out_endpoint = None
        if device is None or util is None:
            return
        try:
            if interface is not None:
                util.release_interface(device, interface.bInterfaceNumber)
        except Exception:  # noqa: BLE001
            pass
        _dispose(device)
        log.debug("USB link closed")


def _find_endpoint(util, interface, direction):
    for endpoint in interface:
        is_bulk = (
            util.endpoint_type(endpoint.bmAttributes) == util.ENDPOINT_TYPE_BULK
        )
        if is_bulk and util.endpoint_direction(endpoint.bEndpointAddress) == direction:
            return endpoint
    return None


def run_link(
    serial: str | None,
    on_frame: Callable[[bytes], None],
    stop: threading.Event,
) -> None:
    """Open the link and pump frames into `on_frame` until `stop` is set.

    Kept here rather than in the session so the session stays testable without
    any USB at all.
    """
    link = UsbLink(serial)
    link.open()
    try:
        for frame in link.frames():
            on_frame(frame)
            if stop.is_set():
                return
    finally:
        link.close()
