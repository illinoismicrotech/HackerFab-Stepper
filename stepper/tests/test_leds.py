"""Projector LED control over USB (DLPC6540), with a simulated USB device."""
import sys
import unittest
from unittest.mock import patch

import support  # noqa: F401

import projector_leds
from projector_leds import ALL, BLUE, RED, LedController, describe_mask

try:
    import usb  # noqa: F401
    HAVE_PYUSB = True
except ImportError:
    HAVE_PYUSB = False


class Endpoint:
    def __init__(self, address, attributes, device):
        self.bEndpointAddress, self.bmAttributes, self.device = address, attributes, device

    def write(self, data, timeout=None):
        if self.device.fail_writes:
            raise OSError("pipe error")
        self.device.written.append(bytes(data))
        if data[0] & 0x80:  # read request: queue the reply
            self.device.replies.append(bytes([0x04, self.device.mask]))
        elif data[1] == 0x80:
            self.device.mask = data[2]
        return len(data)

    def read(self, size, timeout=None):
        return self.device.replies.pop(0) if self.device.replies else b""


class Interface:
    def __init__(self, number, endpoints):
        self.bInterfaceNumber, self.bAlternateSetting, self.endpoints = number, 0, endpoints

    def __iter__(self):
        return iter(self.endpoints)


class Device:
    def __init__(self, pid=0x6401, hid_first=False, no_driver=False):
        self.idProduct = pid
        self.mask = ALL
        self.written, self.replies = [], []
        self.fail_writes = False
        self.no_driver = no_driver
        bulk = Interface(1 if hid_first else 0, [Endpoint(0x01, 0x02, self), Endpoint(0x81, 0x02, self)])
        interfaces = [bulk]
        if hid_first:
            interfaces.insert(0, Interface(0, [Endpoint(0x82, 0x03, self)]))  # interrupt IN only (keyboard)
        self.config = interfaces

    def set_configuration(self):
        if self.no_driver:
            raise NotImplementedError("no backend driver")

    def get_active_configuration(self):
        return self.config

    def __getitem__(self, index):
        return self.config

    def is_kernel_driver_active(self, number):
        return False


@unittest.skipUnless(HAVE_PYUSB, "pyusb not installed")
class LedControllerTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch("usb.util.claim_interface"), patch("usb.util.release_interface"),
                        patch("usb.util.dispose_resources")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def connect(self, *devices):
        with patch("usb.core.find", return_value=list(devices)), patch("projector_leds._backend", return_value=None):
            return LedController.connect()

    def test_connect_reads_state_and_switches_red_only(self):
        dev = Device()
        controller, status = self.connect(dev)
        self.assertIsNotNone(controller)
        self.assertIn("connected", status)
        self.assertIn("all on", status)
        self.assertEqual(dev.written[0], bytes([0x84, 0x80]))  # read illumination enable
        self.assertTrue(controller.set_mask(RED))
        self.assertEqual(dev.written[-1], bytes([0x04, 0x80, 0x01]))
        writes = len(dev.written)
        controller.set_mask(RED)  # unchanged: no USB traffic
        self.assertEqual(len(dev.written), writes)

    def test_uses_the_bulk_interface_not_the_keyboard_one(self):
        controller, _ = self.connect(Device(hid_first=True))
        self.assertEqual(controller.interface, 1)

    def test_failed_write_is_reported_and_retried(self):
        dev = Device()
        controller, _ = self.connect(dev)
        dev.fail_writes = True
        self.assertFalse(controller.set_mask(RED | BLUE))
        self.assertIsNone(controller.mask)
        dev.fail_writes = False
        self.assertTrue(controller.set_mask(RED | BLUE))
        self.assertEqual(dev.mask, RED | BLUE)

    def test_close_turns_leds_off(self):
        dev = Device()
        controller, _ = self.connect(dev)
        controller.close()
        self.assertEqual(dev.mask, 0)
        controller.mask = 0
        controller.close(ALL)
        self.assertEqual(dev.mask, ALL)

    def test_messages_explain_what_to_do(self):
        _, status = self.connect()
        self.assertIn("USB port", status)
        with patch("usb.util.claim_interface", side_effect=NotImplementedError("not supported")):
            _, status = self.connect(Device())  # Windows without a WinUSB driver
        self.assertIn("Zadig", status)

    def test_prefers_the_known_projector_product(self):
        other, projector = Device(pid=0x1234), Device(pid=0x6401)
        controller, _ = self.connect(other, projector)
        self.assertIs(controller.dev, projector)


class LedTextTests(unittest.TestCase):
    def test_describe(self):
        self.assertEqual(describe_mask(RED), "red only")
        self.assertEqual(describe_mask(RED | BLUE), "red + blue")
        self.assertEqual(describe_mask(0), "all off")
        self.assertEqual(describe_mask(ALL), "all on")
        self.assertEqual(describe_mask(None), "unknown")

    def test_missing_pyusb_is_explained(self):
        with patch.dict(sys.modules, {"usb": None, "usb.core": None}):
            controller, status = projector_leds.LedController.connect()
        self.assertIsNone(controller)
        self.assertIn("pyusb", status)


if __name__ == "__main__":
    unittest.main()
