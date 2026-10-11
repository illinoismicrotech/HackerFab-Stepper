"""Switch the projector's LEDs on/off over USB (TI DLPDLCR471TPEVM, DLPC6540 controller).

Why: a DLP projector shows red, green and blue one after another. With a red-only image the
green and blue LEDs still run and some of their light leaks through. The camera turns that
into coloured stripes, and leaked blue (the exposure channel) can pre-expose resist while you
focus. Turning the unused LEDs off removes both problems.

Protocol from the original HackerFab dlpc.py and TI's DLPC6540 programmer's guide (DLPU110):
bulk transfers on the generic WinUSB interface, command 0x80 'illumination enable' with a
red/green/blue bitmask (bit 0 red, bit 1 green, bit 2 blue).
Everything here fails soft: if pyusb, libusb or the USB driver is missing, the app still runs
and the status explains what is needed.
"""
from __future__ import annotations

import platform

TI_VID = 0x0451
DEST_SYSTEM = 4
HDR_READ = 1 << 7
HDR_BUSY = 1 << 7
HDR_ERROR = 1 << 6
HDR_DEST_MASK = 0x07
OP_ILLUMINATION_ENABLE = 0x80

RED, GREEN, BLUE = 0b001, 0b010, 0b100
ALL = RED | GREEN | BLUE


def mask_for_channels(channels) -> int:
    """(red, green, blue) booleans -> LED bitmask."""
    r, g, b = channels
    return (RED if r else 0) | (GREEN if g else 0) | (BLUE if b else 0)


def describe_mask(mask) -> str:
    """'red only', 'red + blue', 'all on', 'all off', 'unknown'."""
    if mask is None:
        return "unknown"
    names = [name for bit, name in ((RED, "red"), (GREEN, "green"), (BLUE, "blue")) if mask & bit]
    if not names:
        return "all off"
    if len(names) == 3:
        return "all on"
    return names[0] + " only" if len(names) == 1 else " + ".join(names)


def _backend():
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:
        return None  # pyusb then searches the system for libusb


def _bulk_endpoints(intf):
    import usb.util
    def find(direction):
        return usb.util.find_descriptor(intf, custom_match=lambda e: (
            usb.util.endpoint_direction(e.bEndpointAddress) == direction
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK))
    return find(usb.util.ENDPOINT_OUT), find(usb.util.ENDPOINT_IN)


def _control_interface(dev):
    """(configuration, interface) of the DLPC's command interface: the one with a bulk IN and a
    bulk OUT endpoint. TI documents it as a generic WinUSB interface; usually interface 0."""
    import usb.core
    try:
        dev.set_configuration()
    except (usb.core.USBError, NotImplementedError):
        pass  # Windows: the configuration is already set and may not be changed by apps
    try:
        config = dev.get_active_configuration()
    except (usb.core.USBError, NotImplementedError):
        config = dev[0]
    candidates = sorted(config, key=lambda i: (i.bInterfaceNumber != 0, i.bInterfaceNumber, i.bAlternateSetting))
    for intf in candidates:
        ep_out, ep_in = _bulk_endpoints(intf)
        if ep_out is not None and ep_in is not None:
            return config, intf, ep_out, ep_in
    raise IOError("the projector's USB control interface (bulk endpoints) was not found")


class LedController:
    """USB link to the DLPC6540. Use LedController.connect(); never raises after that."""

    PREFERRED_PID = 0x6401

    def __init__(self, dev, timeout_ms: int = 1000):
        import usb.core
        import usb.util
        self._usb_util = usb.util
        self.dev = dev
        self.timeout_ms = timeout_ms
        self.mask = None
        self.original_mask = None
        self.failures = 0
        config, intf, self.ep_out, self.ep_in = _control_interface(dev)
        self.interface = intf.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(self.interface):
                dev.detach_kernel_driver(self.interface)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            usb.util.claim_interface(dev, self.interface)
        except usb.core.USBError as exc:
            raise IOError(f"the projector's USB interface is in use by another program ({exc})") from exc
        try:
            self.original_mask = self.get_mask()
            self.mask = self.original_mask
        except Exception as exc:
            print(f"Projector LED state could not be read ({exc}); continuing with writes only.")
            self.original_mask = None
            self.mask = None

    @classmethod
    def connect(cls, pid: int | None = None) -> tuple["LedController | None", str]:
        """(controller or None, plain-language status)."""
        try:
            import usb.core
        except ImportError:
            return None, "Projector LED control unavailable: the USB library (pyusb) is not installed."
        try:
            kwargs = {"idVendor": TI_VID, "find_all": True, "backend": _backend()}
            if pid is not None:
                kwargs["idProduct"] = int(pid)
            devices = list(usb.core.find(**kwargs))
        except Exception as exc:
            return None, f"Projector LED control unavailable: USB support could not start ({exc})."
        if not devices:
            return None, ("Projector LED control: no TI projector found on USB. Connect the projector board's "
                          "USB port to this computer (the HDMI cable alone is not enough), then click Reconnect.")
        devices.sort(key=lambda d: d.idProduct != cls.PREFERRED_PID)
        errors = []
        for dev in devices:
            try:
                controller = cls(dev)
                state = f" (LEDs now: {describe_mask(controller.mask)})" if controller.mask is not None else ""
                return controller, f"Projector LED control connected{state}."
            except NotImplementedError:
                errors.append("no-driver")
            except Exception as exc:
                errors.append(str(exc))
        text = "; ".join(errors)
        if "no-driver" in errors:
            return None, ("Projector LED control: Windows has no usable driver for the projector's USB control "
                          "interface. Install WinUSB for it once with Zadig (see the guide), then click Reconnect.")
        if platform.system() == "Windows" and any(k in text for k in ("Access denied", "in use", "Entity not found",
                                                                     "not supported", "Errno 13")):
            return None, ("Projector LED control: the projector's USB interface is busy or has the wrong driver. "
                          "Close TI's DLP EVM GUI and click Reconnect; if it still fails, see the guide (Zadig).")
        if platform.system() == "Linux" and any(k in text for k in ("Access denied", "Errno 13", "insufficient permissions")):
            return None, ("Projector LED control: Linux does not let this user open the projector's USB port. Allow it "
                          "once with a udev rule (see the guide, LEDs on Linux), replug the projector's USB cable, "
                          "then click Reconnect.")
        return None, f"Projector LED control could not connect: {text}"

    # ---- protocol (DLPC6540 programmer's guide, chapter 16: header, opcode, data)
    def _write(self, opcode: int, data: bytes = b""):
        self.ep_out.write(bytes([DEST_SYSTEM & HDR_DEST_MASK, opcode]) + data, timeout=self.timeout_ms)

    def _read(self, opcode: int, length: int) -> bytes:
        self.ep_out.write(bytes([HDR_READ | (DEST_SYSTEM & HDR_DEST_MASK), opcode]), timeout=self.timeout_ms)
        resp = bytes(self.ep_in.read(max(length + 1, 64), timeout=self.timeout_ms))
        if not resp:
            raise IOError("empty response from the projector controller")
        if resp[0] & HDR_ERROR:
            raise RuntimeError(f"projector controller error {resp[1] if len(resp) > 1 else '?'}")
        if resp[0] & HDR_BUSY:
            raise RuntimeError("projector controller busy")
        return resp[1:length + 1]

    def get_mask(self) -> int:
        return self._read(OP_ILLUMINATION_ENABLE, 1)[0] & ALL

    def set_mask(self, mask: int) -> bool:
        """Switch LEDs to `mask`. Returns False (and keeps running) if the projector didn't take it."""
        if not 0 <= mask <= ALL:
            raise ValueError(f"LED mask must be 0-7, got {mask}")
        if mask == self.mask:
            return True
        try:
            self._write(OP_ILLUMINATION_ENABLE, bytes([mask]))
            self.mask = mask
            self.failures = 0
            return True
        except Exception as exc:
            print(f"Projector LED switch failed: {exc}")
            self.mask = None  # unknown; the next call will resend
            self.failures += 1
            return False

    def close(self, final_mask=0):
        """Set the LEDs to final_mask (default: all off, so the desktop is not projected onto
        resist after the app closes; None leaves them as they are) and release the USB interface."""
        try:
            if final_mask is not None:
                self.set_mask(int(final_mask))
        finally:
            try:
                self._usb_util.release_interface(self.dev, self.interface)
            except Exception:
                pass
            try:
                self._usb_util.dispose_resources(self.dev)
            except Exception:
                pass
