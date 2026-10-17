"""Camera worker: mode choice (highest resolution, lowest frame rate), live controls,
full-resolution snapshots, frame details, error reporting and settings parsing."""
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import support  # noqa: F401

import cv2
import numpy as np
import toml

from camera import webcam
from camera.discovery import CameraDevice, CaptureMode, parse_modes
from camera.modes import Mode
from camera.webcam import (DEFAULT_EXPOSURE, DEFAULT_WHITE_BALANCE, Webcam, _capture, apply_controls, capture_backend,
                           exposure_mode, frame_problem, initial_controls, manual_exposure)
from settings import save_config


class FakeCap:
    """cv2.VideoCapture stand-in. Delivers frames at the requested size if it supports it,
    otherwise at its first supported size (like a real driver substituting a mode)."""

    def __init__(self, *args, sizes=((64, 48),), opened=True, green=False, accept_exposure=True):
        self.args, self.sizes, self.opened, self.green = args, list(sizes), opened, green
        self.accept_exposure = accept_exposure
        self.props = {cv2.CAP_PROP_EXPOSURE: -11.0}
        self.sets = []
        self.released = False

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.sets.append((prop, value))
        if prop == cv2.CAP_PROP_EXPOSURE and not self.accept_exposure:
            return True
        self.props[prop] = value
        return True

    def get(self, prop):
        return self.props.get(prop, 9.0 if prop == cv2.CAP_PROP_FPS else 0.0)

    def size(self):
        wanted = (int(self.props.get(cv2.CAP_PROP_FRAME_WIDTH, 0)), int(self.props.get(cv2.CAP_PROP_FRAME_HEIGHT, 0)))
        return wanted if wanted in self.sizes else self.sizes[0]

    def read(self):
        w, h = self.size()
        frame = np.zeros((h, w, 3), np.uint8)
        if self.green:
            frame[:, :, 1] = 128
        else:
            frame[:, :, 2] = 180          # BGR: red light
            frame[::4, :, 2] = 60         # some detail
        return True, frame

    def release(self):
        self.released = True


class Channel:
    """Collects worker messages; stops the worker after `frames` frames."""

    def __init__(self, stop, frames=3, wait_for=None):
        self.stop, self.frames, self.wait_for = stop, frames, wait_for
        self.events = []

    def put(self, item, timeout=0):
        self.events.append(item)
        frames = sum(1 for kind, _ in self.events if kind == "frame")
        if frames >= self.frames and (self.wait_for is None or any(k == self.wait_for for k, _ in self.events)):
            self.stop.set()

    def of(self, kind):
        return [value for k, value in self.events if k == kind]


def run_worker(settings, caps, modes, source="camera", frames=3, commands=None, wait_for=None):
    stop = threading.Event()
    channel = Channel(stop, frames, wait_for)
    made = iter(caps)
    with patch("camera.webcam.available_modes", return_value=(modes, source)), \
         patch("camera.webcam.cv2.VideoCapture", side_effect=lambda *a: next(made)), \
         patch("camera.webcam._read_ranges", return_value={}):
        _capture(dict({"device": "0", "backend": "dshow"}, **settings), channel, stop, commands)
    return channel


class ModeChoiceTests(unittest.TestCase):
    def test_highest_resolution_and_lowest_frame_rate_first(self):
        cap = FakeCap(sizes=[(640, 480), (320, 240)])
        modes = [Mode(320, 240, 30), Mode(640, 480, 30), Mode(640, 480, 9)]
        channel = run_worker({}, [cap], modes)
        ready = channel.of("ready")[0]
        self.assertEqual((ready["width"], ready["height"]), (640, 480))
        self.assertIn((cv2.CAP_PROP_FPS, 9), cap.sets)
        self.assertEqual(len(channel.of("modes")[0]["modes"]), 3)

    def test_preset_the_driver_does_not_support_is_skipped(self):
        first, second = FakeCap(sizes=[(640, 480)]), FakeCap(sizes=[(640, 480)])
        modes = [Mode(800, 600, 9, "YUY2", "IMX283"), Mode(640, 480, 10, "YUY2", "IMX283")]
        channel = run_worker({}, [first, second], modes, source="presets")
        self.assertTrue(first.released)
        self.assertTrue(any("instead" in s for s in channel.of("status")))
        self.assertEqual(channel.of("ready")[0]["width"], 640)

    def test_requested_resolution_is_not_replaced(self):
        cap = FakeCap(sizes=[(640, 480)])
        channel = run_worker({"mode": "manual", "width": 800, "height": 600, "fps": 9}, [cap], [Mode(640, 480, 9)])
        self.assertIn("did not accept 800 × 600", channel.of("error")[0])

    def test_green_frames_are_rejected_then_next_mode_used(self):
        green, good = FakeCap(sizes=[(640, 480)], green=True), FakeCap(sizes=[(320, 240)])
        channel = run_worker({}, [green, good], [Mode(640, 480, 9), Mode(320, 240, 9)])
        self.assertTrue(green.released and good.released)
        self.assertEqual(channel.of("ready")[0]["width"], 320)

    def test_busy_camera_reports_and_releases(self):
        cap = FakeCap(opened=False)
        channel = run_worker({}, [cap], [Mode(64, 48, 9)], frames=0)
        self.assertTrue(cap.released)
        self.assertIn("busy, unplugged or in use", channel.of("error")[0])

    def test_missing_device_reports_actionable_error(self):
        channel = queue.Queue()
        with patch("camera.webcam.discover_devices", return_value=[]):
            _capture({"device": "auto"}, channel, threading.Event())
        kind, message = channel.get_nowait()
        self.assertEqual(kind, "error")
        self.assertIn("No camera found", message)


class StreamTests(unittest.TestCase):
    def test_frame_details_preview_focus_and_stats(self):
        cap = FakeCap(sizes=[(2400, 1600)])
        channel = run_worker({}, [cap], [Mode(2400, 1600, 9)], frames=2)
        info = channel.of("frame")[-1]
        self.assertEqual(info["full_size"], (2400, 1600))
        self.assertLessEqual(info["image"].shape[1], webcam.PREVIEW_MAX[0])
        self.assertGreater(info["image"][..., 0].mean(), 50)  # RGB order: the red light is channel 0
        self.assertEqual(info["image"][..., 2].max(), 0)
        self.assertEqual(len(info["focus"]), 2)
        self.assertTrue({"p99", "mean", "saturated", "stripes"} <= set(info["stats"]))
        # Focus at every scale from the middle half of the full-resolution frame, and a capture time
        # autofocus can compare with stage moves.
        levels = info["focus_levels"]
        self.assertEqual(levels["crop"], (1200, 800))
        self.assertEqual(len(levels["red"]), len(levels["blue"]))
        self.assertEqual(info["focus"][0], levels["red"][levels["default"]])
        self.assertLessEqual(info["captured"], time.monotonic())

    def test_live_controls_report_requested_and_readback(self):
        commands = queue.Queue()
        commands.put(("set", {"exposure": -5, "gain": 300}))
        cap = FakeCap()
        channel = run_worker({}, [cap], [Mode(64, 48, 9)], commands=commands, wait_for="controls")
        reply = channel.of("controls")[0]
        self.assertEqual(reply["requested"], {"exposure": -5, "gain": 300})
        self.assertEqual(reply["readback"]["exposure"], -5)
        self.assertIn((cv2.CAP_PROP_GAIN, 300), cap.sets)

    def test_full_resolution_snapshot_in_non_ascii_folder(self):
        folder = Path(tempfile.mkdtemp()) / "Überprüfung"
        commands = queue.Queue()
        commands.put(("snapshot", str(folder / "shot.png")))
        stop = threading.Event()
        channel = Channel(stop, frames=10 ** 9)
        made = iter([FakeCap(sizes=[(1200, 900)])])
        result = {}

        def watch():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not channel.of("snapshot"):
                time.sleep(0.02)
            result.update(channel.of("snapshot")[0] if channel.of("snapshot") else {})
            stop.set()
        threading.Thread(target=watch, daemon=True).start()
        with patch("camera.webcam.available_modes", return_value=([Mode(1200, 900, 9)], "camera")), \
             patch("camera.webcam.cv2.VideoCapture", side_effect=lambda *a: next(made)), \
             patch("camera.webcam._read_ranges", return_value={}):
            _capture({"device": "0", "backend": "dshow"}, channel, stop, commands)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["size"], (1200, 900))
        saved = cv2.imdecode(np.frombuffer(Path(result["path"]).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(saved.shape[:2], (900, 1200))

    def test_processing_command_changes_filter_and_averaging(self):
        commands = queue.Queue()
        commands.put(("process", {"destripe": False, "average": 4}))
        state = {}
        original = webcam._process

        def spy(frame, st, index, started, captured=None):
            state.update(destripe=st["destripe"], average=st["averager"].count)
            return original(frame, st, index, started, captured)
        with patch("camera.webcam._process", side_effect=spy):
            run_worker({}, [FakeCap()], [Mode(64, 48, 9)], commands=commands)
        self.assertEqual(state, {"destripe": False, "average": 4})


class SettingsTests(unittest.TestCase):
    def test_defaults_fixed_white_balance_and_stripe_free_exposure(self):
        self.assertEqual(initial_controls({}), {"exposure": DEFAULT_EXPOSURE, "white_balance": DEFAULT_WHITE_BALANCE})
        self.assertTrue(initial_controls({"white-balance": "auto"})["white_balance_auto"])
        self.assertNotIn("exposure", initial_controls({"exposure": "camera"}))
        self.assertEqual(initial_controls({"exposure": -5, "gain": 300})["gain"], 300)
        self.assertEqual(initial_controls({"stripe-free-exposure": -3})["exposure"], -3)
        self.assertEqual(manual_exposure({"exposure": -40}), -13)
        self.assertEqual(exposure_mode({"exposure": "auto"}), "auto")
        self.assertEqual(exposure_mode({"exposure": "-4"}), "manual")

    def test_windows_uses_directshow(self):
        with patch("camera.webcam.platform.system", return_value="Windows"):
            self.assertEqual(capture_backend({}), cv2.CAP_DSHOW)
            with patch("camera.webcam.cv2.videoio_registry.hasBackend", return_value=True):
                self.assertEqual(capture_backend({"backend": "msmf"}), cv2.CAP_MSMF)
            with patch("camera.webcam.cv2.videoio_registry.hasBackend", return_value=False):
                self.assertEqual(capture_backend({"backend": "msmf"}), cv2.CAP_DSHOW)   # minimal build
        with patch("camera.webcam.platform.system", return_value="Linux"):
            self.assertEqual(capture_backend({"backend": "auto"}), cv2.CAP_V4L2)

    def test_directshow_switches_to_manual_then_sets_exposure(self):
        cap = FakeCap()
        readback = apply_controls(cap, cv2.CAP_DSHOW, {"exposure": -5, "white_balance": 4500})
        self.assertEqual(cap.sets[:2], [(cv2.CAP_PROP_AUTO_EXPOSURE, 0), (cv2.CAP_PROP_EXPOSURE, -5)])
        self.assertIn((cv2.CAP_PROP_AUTO_WB, 0), cap.sets)
        self.assertEqual(readback["exposure"], -5)

    def test_v4l2_exposure_in_100_microsecond_units(self):
        cap = FakeCap()
        apply_controls(cap, cv2.CAP_V4L2, {"exposure": -5})
        self.assertIn((cv2.CAP_PROP_EXPOSURE, 312), cap.sets)

    def test_camera_that_ignores_exposure_is_reported(self):
        camera = Webcam(settings={})
        camera.handle_message("ready", {"device": "0", "width": 64, "height": 48, "fps": 9.0, "backend": "DirectShow",
                                        "controls": {"exposure": -11.0}, "requested": {"exposure": -4},
                                        "readback": {"exposure": -11.0}, "ranges": {}})
        self.assertEqual(camera.controls["exposure"], -4)
        self.assertIn("did not accept exposure -4", camera.exposure_problem)
        camera.handle_message("controls", {"requested": {"exposure": -5}, "readback": {"exposure": -5.0}})
        self.assertEqual(camera.exposure_problem, "")

    def test_green_corruption_check(self):
        green = np.zeros((64, 64, 3), np.uint8)
        green[:, :, 1] = 128
        self.assertIn("green", frame_problem(green))
        self.assertEqual(frame_problem(green, False), "")
        self.assertEqual(frame_problem(np.zeros_like(green)), "")
        self.assertTrue(frame_problem(None))

    def test_watchdog_terminates_a_silent_worker(self):
        camera = Webcam()
        camera.channel = Mock()
        camera.channel.get.side_effect = queue.Empty
        camera.process = Mock()
        camera.process.is_alive.return_value = True
        with patch("camera.webcam.time.monotonic", side_effect=[0, webcam.STARTUP_SECONDS + 1]):
            camera._monitor()
        camera.process.terminate.assert_called_once()
        self.assertIn("stopped responding", camera.status)
        self.assertFalse(camera.isOpen())

    def test_advertised_formats_parse(self):
        modes = parse_modes("""
[0]: 'MJPG' (Motion-JPEG)
    Size: Discrete 1280x720
        Interval: Discrete 0.033s (30.000 fps)
[1]: 'YUYV' (YUYV)
    Size: Discrete 640x480
        Interval: Discrete 0.100s (10.000 fps)
""")
        self.assertEqual(modes, [CaptureMode("MJPG", 1280, 720, 30), CaptureMode("YUYV", 640, 480, 10)])

    def test_b0477_usb2_hint(self):
        camera = CameraDevice("/dev/video4", "Arducam B0477 (USB3 20MP)", "480")
        self.assertIn("YUYV, 10 fps", camera.connection_hint())

    def test_arducam_listed_first(self):
        from camera import discovery
        names = ["Integrated Camera", "USB Video Device", "Arducam USB Camera"]
        with patch("camera.discovery.platform.system", return_value="Windows"), \
             patch("camera.discovery._dshow_device_names", return_value=names):
            order = [d.name for d in discovery.discover_devices(api="dshow")]
        self.assertEqual(order, ["Arducam USB Camera", "USB Video Device", "Integrated Camera"])

    def test_save_config_backs_up_and_keeps_five(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[alignment]\nenabled = false\n")
            for i in range(7):
                save_config(path, {"camera": {"device": str(i)}})
                time.sleep(0.002)
            self.assertEqual(toml.load(path), {"camera": {"device": "6"}})
            self.assertEqual(len(list(Path(directory).glob("*.bak"))), 5)

    def test_msmf_hardware_transforms_disabled(self):
        import os
        self.assertEqual(os.environ.get("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"), "0")



class ReviewFixTests(unittest.TestCase):
    def test_settings_written_as_strings(self):
        self.assertEqual(webcam.setting_int({"average-frames": "2.0"}, "average-frames", 1, 1, 16), 2)
        self.assertEqual(webcam.setting_int({"average-frames": "lots"}, "average-frames", 1, 1, 16), 1)
        self.assertFalse(webcam.setting_flag({"stripe-filter": "false"}, "stripe-filter", True))
        self.assertTrue(webcam.setting_flag({}, "stripe-filter", True))
        self.assertTrue(webcam.setting_flag({"reject-green": True}, "reject-green", False))

    def test_busy_arducam_is_reported_not_swapped_for_the_laptop_camera(self):
        devices = [CameraDevice("0", "Integrated Webcam"), CameraDevice("1", "Arducam B0477")]
        stop = threading.Event()
        channel = Channel(stop, frames=1)
        opened = []

        def make(device, backend):
            opened.append(device)
            return FakeCap(opened=False)
        with patch("camera.webcam.discover_devices", return_value=devices), \
             patch("camera.webcam.available_modes", return_value=([Mode(64, 48, 9)], "camera")), \
             patch("camera.webcam.cv2.VideoCapture", side_effect=make), \
             patch("camera.webcam._read_ranges", return_value={}):
            _capture({"device": "auto", "backend": "dshow"}, channel, stop)
        self.assertEqual(opened, [1])
        self.assertTrue(channel.of("error"))

    def test_snapshot_saves_the_picture_from_when_it_was_asked(self):
        folder = Path(tempfile.mkdtemp())
        commands = queue.Queue()
        stop = threading.Event()
        channel = Channel(stop, frames=10 ** 9)
        reads = {"count": 0}

        class CountingCap(FakeCap):
            def read(self):
                reads["count"] += 1
                ok, frame = super().read()
                frame[0, 0, 0] = min(255, reads["count"])     # frame number in the first pixel
                if reads["count"] == 5:
                    commands.put(("snapshot", str(folder / "shot.png")))
                return ok, frame
        result = {}

        def watch():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not channel.of("snapshot"):
                time.sleep(0.02)
            result.update(channel.of("snapshot")[0] if channel.of("snapshot") else {})
            stop.set()
        threading.Thread(target=watch, daemon=True).start()
        with patch("camera.webcam.available_modes", return_value=([Mode(64, 48, 9)], "camera")), \
             patch("camera.webcam.cv2.VideoCapture", side_effect=lambda *a: CountingCap()), \
             patch("camera.webcam._read_ranges", return_value={}):
            _capture({"device": "0", "backend": "dshow"}, channel, stop, commands)
        self.assertTrue(result.get("ok"), result)
        saved = cv2.imdecode(np.frombuffer(Path(result["path"]).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(int(saved[0, 0, 0]), 5)   # the frame read before the command, not a later one


if __name__ == "__main__":
    unittest.main()
