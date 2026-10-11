"""Stepper logic without the window: what is projected, LEDs, exposures, autofocus, stage."""
import tempfile
import time
import tkinter
import unittest
from pathlib import Path
from unittest.mock import patch

import support
from support import DialogsAnswered, FakeCamera, FakeLeds, RecordingStage

import numpy as np
from PIL import Image

from dispatcher import (LED_EXPOSE, LED_FOCUS, LED_OFF, AutofocusStopped, ConfigReader, Event, EventDispatcher,
                        MovementLock, RedFocusSource, ShownImage, StageError, describe_stage_error,
                        load_pattern_image, write_rgb_image)
from projector import TkProjector


class FailingStage(RecordingStage):
    def move_to(self, amounts):
        raise RuntimeError("GRBL error: error:9")

    def move_by(self, amounts):
        raise RuntimeError("GRBL error: error:9")

    def state_name(self):
        return "Alarm"


class DispatcherCase(DialogsAnswered):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tkinter.Tk()
        except tkinter.TclError as exc:
            raise unittest.SkipTest(f"Display unavailable: {exc}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.stage = RecordingStage()
        self.projector = TkProjector(size=(320, 180))
        self.shown = []
        self.projector.on_show = lambda: self.shown.append(self.projector.current_image)
        self.d = EventDispatcher(self.stage, self.projector, self.root, None)
        self.d.snapshot_directory = self.folder
        self.d._refresh_red_focus()

    def pattern(self, name="pattern.png", size=(320, 180)):
        path = self.folder / name
        Image.new("RGB", size, "white").save(path)
        self.d.set_pattern_image(load_pattern_image(path), str(path))
        self.d.exposure_time = 30
        return path


class ProjectionTests(DispatcherCase):
    def test_describes_what_is_projected(self):
        self.assertEqual(self.d.describe_projection()[:2], ("OFF", "off"))
        self.d.show_red()
        badge, kind, text = self.d.describe_projection()
        self.assertEqual((badge, kind), ("RED · SAFE", "red"))
        self.assertIn("Solid red", text)
        self.assertIn("does not expose", text)
        self.pattern()
        self.d.show_red(RedFocusSource.PATTERN)
        self.assertIn("pattern in red", self.d.describe_projection()[2])
        self.d.show_uv_mark()
        self.assertEqual(self.d.describe_projection()[:2], ("BLUE MARK", "uv"))
        self.assertIn("exposes resist", self.d.describe_projection()[2])

    def test_red_brightness_dims_projection_and_user_turns_auto_off(self):
        self.d.show_red()
        self.d.set_red_brightness(0.5, by_user=False)
        self.assertTrue(self.d.auto_camera.auto_light)
        red = self.projector.current_image.getpixel((5, 5))[0]
        self.assertAlmostEqual(red, 128, delta=1)
        self.d.set_red_brightness(0.25)
        self.assertFalse(self.d.auto_camera.auto_light)
        self.assertIn("25%", self.d.describe_projection()[2])

    def test_projector_size_follows_the_display(self):
        self.pattern(size=(640, 360))
        self.d.set_projector_size((1280, 720))
        self.assertEqual(self.d.pattern.processed().size, (1280, 720))
        self.assertEqual(self.d.red_focus.processed().size, (1280, 720))

    def test_stop_all_turns_projector_off(self):
        self.d.show_red()
        self.d.stop_all()
        self.assertEqual(self.d.shown_image, ShownImage.CLEAR)


class LedSequencingTests(DispatcherCase):
    def setUp(self):
        super().setUp()
        self.leds = FakeLeds()
        self.d.attach_leds(self.leds, "connected")

    def test_masks_follow_the_projection(self):
        self.d.show_red()
        self.assertEqual(self.leds.mask, LED_FOCUS)
        self.d.clear_projector()
        self.assertEqual(self.leds.mask, LED_OFF)
        self.d.show_uv_mark()
        self.assertEqual(self.leds.mask, LED_EXPOSE)

    def test_exposure_switches_blue_on_then_off_then_back_to_red(self):
        self.pattern()
        self.d.show_red()
        self.leds.calls.clear()
        self.d.begin_patterning()
        self.assertEqual(self.leds.calls, [LED_EXPOSE, LED_OFF, LED_FOCUS])
        self.assertEqual(len(self.d.exposure_history), 1)
        self.assertEqual(self.d.shown_image, ShownImage.RED_FOCUS)

    def test_exposure_refused_when_blue_cannot_be_switched_on(self):
        self.pattern()
        self.leds.fail = True
        self.d.begin_patterning()
        self.assertEqual(self.d.exposure_history, [])
        self.assertIn("blue LED", self.dialogs[-1][1][1])
        self.assertFalse(self.d.patterning_busy)

    def test_lost_usb_link_stops_retrying_and_blocks_exposure(self):
        self.pattern()
        self.leds.failures = 3
        self.leds.calls.clear()
        self.assertFalse(self.d.set_leds(LED_EXPOSE))
        self.d.begin_patterning()
        self.assertEqual(self.leds.calls, [])
        self.assertEqual(self.d.exposure_history, [])
        self.assertIn("Reconnect LEDs", self.d.led_status)

    def test_red_light_never_shown_with_blue_on(self):
        self.pattern()
        self.d.show_uv_mark()
        order = []
        self.projector.on_show = lambda: order.append(("image", self.d.shown_image))
        original = self.leds.set_mask
        self.leds.set_mask = lambda mask: (order.append(("leds", mask)), original(mask))[1]
        self.d.show_red()
        self.assertEqual(order[0], ("leds", LED_FOCUS))  # blue off before the red picture appears


class ExposureTests(DispatcherCase):
    def test_exposure_is_logged_and_projector_cleared(self):
        self.pattern()
        self.d.begin_patterning()
        log = self.d.exposure_history[-1]
        self.assertFalse(log.aborted)
        self.assertAlmostEqual(log.duration, 30, delta=20)   # the time the light was really on
        self.assertEqual(self.d.shown_image, ShownImage.CLEAR)
        self.assertFalse(self.d.patterning_busy)
        self.assertIn("Done", self.d.last_exposure)
        # what the projector showed: the pattern (blue only), then black
        pattern_frames = [img for img in self.shown if img.getpixel((160, 90)) == (0, 0, 255)]
        self.assertTrue(pattern_frames)
        self.assertEqual(self.shown[-1].getpixel((160, 90)), (0, 0, 0))

    def test_no_pattern_no_exposure(self):
        self.d.begin_patterning()
        self.assertEqual(self.d.exposure_history, [])
        self.assertIn("Choose a pattern", self.dialogs[-1][1][1])

    def test_no_exposure_while_stage_is_moving(self):
        self.pattern()
        self.stage.idle = False
        self.d.stage_idle_timeout = 0.2
        self.d.begin_patterning()
        self.assertEqual(self.d.exposure_history, [])
        self.assertIn("'Run'", self.dialogs[-1][1][1])
        self.assertFalse(self.d.patterning_busy)

    def test_output_check_can_cancel(self):
        self.pattern()
        self.d.ensure_output = lambda: False
        self.d.begin_patterning()
        self.assertEqual(self.d.exposure_history, [])

    def test_z_offset_applied_and_undone(self):
        self.pattern()
        self.d.uv_z_offset = -40
        self.d.begin_patterning()
        self.assertEqual([m["z"] for m in self.stage.moves], [-40.0, 0.0])
        self.assertEqual(self.d.exposure_history[-1].coords[2], -40.0)

    def test_stop_mid_exposure(self):
        self.pattern()
        self.d.exposure_time = 5000
        self.root.after(100, self.d.stop_all)
        self.d.begin_patterning()
        self.assertTrue(self.d.exposure_history[-1].aborted)
        self.assertLess(self.d.exposure_progress, 1.01)
        self.assertFalse(self.d.should_abort)

    def test_display_failure_unlocks_and_clears(self):
        self.pattern()
        self.projector.on_show = lambda: (_ for _ in ()).throw(RuntimeError("display failed"))
        self.assertFalse(self.d.begin_patterning())
        self.assertIn("could not be shown", self.dialogs[-1][1][1])
        self.assertEqual(self.d.exposure_history, [])
        self.assertFalse(self.d.patterning_busy)
        self.assertEqual(self.d.shown_image, ShownImage.CLEAR)
        self.assertEqual(self.d.movement_lock, MovementLock.UNLOCKED)

    def test_snapshot_requested_before_exposure(self):
        self.pattern()
        requested = []
        self.d.snapshot_requester = requested.append
        self.d.camera_image = np.zeros((4, 4, 3), np.uint8)
        self.d.begin_patterning()
        self.assertEqual(len(requested), 1)
        self.assertTrue(requested[0].endswith(".jpg"))
        self.assertIn("before_exposure", requested[0])


class StageTests(DispatcherCase):
    def test_failed_move_is_explained_and_position_kept(self):
        self.d.hardware.stage = FailingStage()
        with self.assertRaises(StageError):
            self.d.move_relative({"x": 100})
        self.assertEqual(self.d.stage_setpoint, (0.0, 0.0, 0.0))
        self.assertIn("ALARM", self.dialogs[0][1][1])

    def test_jog_refused_while_locked(self):
        self.assertTrue(self.d.jog("x", 10))
        self.d.patterning_busy = True
        self.assertFalse(self.d.jog("x", 10))
        self.d.patterning_busy = False
        self.assertEqual(self.d.stage_setpoint[0], 10)

    def test_alarm_explanation_mentions_unlocking(self):
        text = describe_stage_error(RuntimeError("GRBL error: error:9"))
        self.assertIn("$X", text)


class AutofocusTests(DispatcherCase):
    def run_autofocus(self, peak, start=0.0):
        self.d.stage_setpoint = (0.0, 0.0, start)
        self.d.camera, self.d.camera_image = object(), np.zeros((4, 4, 3), np.uint8)

        def fresh_frames(count=1, timeout=3.0):
            self.d.frame_index += count
            return True
        with patch.object(self.d, "wait_for_frames", side_effect=fresh_frames), \
             patch.object(self.d, "current_focus", side_effect=lambda blue_only: -(self.d.stage_setpoint[2] - peak) ** 2), \
             patch.object(self.d, "non_blocking_delay", lambda _t: None):
            self.d.autofocus(blue_only=False)
        return self.d.stage_setpoint[2]

    def test_reaches_focus_in_either_direction(self):
        for peak in (-37.0, 43.0, 3.0, -9.0):
            self.assertLessEqual(abs(self.run_autofocus(peak) - peak), 1.0, f"peak at {peak}")
        self.assertFalse(self.d.autofocus_busy)
        self.assertIn("done", self.d.last_autofocus)

    def test_explains_when_it_cannot_run(self):
        self.d.camera = self.d.camera_image = None
        self.d.autofocus(blue_only=False, interactive=True)
        self.assertIn("live camera", self.dialogs[-1][1][1])

    def test_can_be_stopped(self):
        self.d.camera, self.d.camera_image = object(), np.zeros((4, 4, 3), np.uint8)
        calls = []

        def frames(count=1, timeout=3.0):
            calls.append(1)
            if len(calls) > 5:
                raise AutofocusStopped("stopped")
            return True
        with patch.object(self.d, "wait_for_frames", side_effect=frames), \
             patch.object(self.d, "current_focus", return_value=1.0):
            self.d.autofocus(blue_only=False)
        self.assertFalse(self.d.autofocus_busy)
        self.assertIn("stopped", self.d.last_autofocus)


class AutoCameraHookTests(DispatcherCase):
    def test_runs_only_under_red_light(self):
        camera = FakeCamera()
        self.d.camera = camera
        self.d.show_red()
        camera.push()
        self.d.on_camera_frame(np.zeros((4, 4, 3), np.uint8), dict(camera.latest_info))
        self.assertEqual(self.d.auto_camera.phase, "search")
        for _ in range(8):
            self.d.on_camera_frame(np.zeros((4, 4, 3), np.uint8), dict(camera.latest_info))
        self.assertTrue(camera.set_calls)  # it tried the first candidate exposure
        self.d.clear_projector()
        self.assertEqual(self.d.auto_camera.phase, "paused")
        self.d.show_red()
        self.assertEqual(self.d.auto_camera.phase, "search")

    def test_remembered_exposure_skips_the_search(self):
        camera = FakeCamera({"stripe-free-exposure": -4})
        self.d.camera = camera
        self.d.show_red()
        self.d.on_camera_frame(np.zeros((4, 4, 3), np.uint8), dict(camera.latest_info or {"stats": {"p99": 200}}))
        self.assertEqual(self.d.auto_camera.phase, "track")


class HelperTests(unittest.TestCase):
    def test_transparent_white_becomes_black(self):
        folder = Path(tempfile.mkdtemp())
        image = Image.new("RGBA", (4, 1), (255, 255, 255, 0))
        image.putpixel((0, 0), (255, 255, 255, 255))
        image.save(folder / "shape.png")
        pixels = list(load_pattern_image(folder / "shape.png").getdata())
        self.assertEqual(pixels, [(255, 255, 255)] + [(0, 0, 0)] * 3)

    def test_sixteen_bit_is_scaled_not_clipped(self):
        folder = Path(tempfile.mkdtemp())
        Image.fromarray(np.array([[0, 32768, 65535]], dtype=np.uint16)).save(folder / "deep.png")
        grey = [p[0] for p in load_pattern_image(folder / "deep.png").getdata()]
        self.assertEqual((grey[0], grey[2]), (0, 255))
        self.assertAlmostEqual(grey[1], 127, delta=1)

    def test_snapshot_writes_to_non_ascii_folder(self):
        target = Path(tempfile.mkdtemp()) / "Überprüfung" / "snap"
        saved = write_rgb_image(target, np.zeros((8, 8, 3), np.uint8))
        self.assertTrue(saved.exists() and saved.suffix == ".png")

    def test_config_reader_reports_typos(self):
        reader = ConfigReader({"camera": {"gui-scale": "big"}, "stage": {"homing": "yes", "invert-z": "maybe"}})
        self.assertEqual(reader.number("camera", "gui-scale", 0.25, positive=True), 0.25)
        self.assertTrue(reader.flag("stage", "homing", False))
        self.assertTrue(reader.flag("stage", "invert-z", True))
        self.assertEqual(len(reader.problems), 2)


if __name__ == "__main__":
    unittest.main()


class SafetyTests(DispatcherCase):
    """Regression tests for the review findings: light, STOP, locks, stage position."""

    def test_stop_leaves_the_projector_off(self):
        self.pattern()
        self.d.exposure_time = 2000
        self.d.show_red()
        self.root.after(50, self.d.stop_all)
        self.assertTrue(self.d.begin_patterning())
        self.assertTrue(self.d.exposure_history[-1].aborted)
        self.assertEqual(self.d.shown_image, ShownImage.CLEAR)   # not back to red: STOP means off

    def test_finished_exposure_returns_to_red(self):
        self.pattern()
        self.d.show_red()
        self.d.begin_patterning()
        self.assertEqual(self.d.shown_image, ShownImage.RED_FOCUS)

    def test_stop_while_waiting_for_the_stage_exposes_nothing(self):
        self.pattern()
        self.stage.idle = False
        self.d.stage_idle_timeout = 5.0
        self.root.after(80, self.d.stop_all)
        started = time.monotonic()
        self.assertFalse(self.d.begin_patterning())
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.d.exposure_history, [])
        self.assertFalse(any(img.getpixel((160, 90))[2] for img in self.shown))   # never blue
        self.assertEqual(self.dialogs, [])                                        # no error dialog after STOP

    def test_second_start_while_preparing_is_ignored(self):
        self.pattern()
        self.stage.idle = False
        self.d.stage_idle_timeout = 0.5
        calls = []

        def press_start_again():
            calls.append(self.d.begin_patterning())
            self.stage.idle = True
        self.root.after(50, press_start_again)
        self.d.begin_patterning()
        self.assertEqual(calls, [False])
        self.assertEqual(len(self.d.exposure_history), 1)

    def test_light_ends_on_time_even_when_a_handler_blocks(self):
        # A dialog opened during the exposure runs its own event loop. The light must still end on time.
        self.pattern()
        self.d.exposure_time = 100
        blocked = {}

        def handler_with_its_own_loop():
            started = time.monotonic()
            while time.monotonic() - started < 0.6:   # like a message box waiting for a click
                self.root.update()
                time.sleep(0.005)
                if self.d.shown_image == ShownImage.CLEAR and "off" not in blocked:
                    blocked["off"] = time.monotonic() - started
        self.root.after(10, handler_with_its_own_loop)
        self.d.begin_patterning()
        self.assertLess(blocked.get("off", 9), 0.25)
        self.assertLess(self.d.exposure_history[-1].duration, 250)

    def test_light_cannot_be_changed_during_an_exposure(self):
        self.pattern()
        self.d.leds = FakeLeds()
        self.d.exposure_time = 100
        seen = []

        def meddle():
            self.d.set_leds(0)
            self.d.set_shown_image(ShownImage.RED_FOCUS)
            self.d.show_red()
            seen.append((self.d.shown_image, self.d.leds.mask))
        self.root.after(30, meddle)
        self.d.begin_patterning()
        self.assertEqual(seen, [(ShownImage.PATTERN, LED_EXPOSE)])

    def test_blue_led_switches_on_after_the_pattern_is_drawn(self):
        self.pattern()
        events = []
        self.d.leds = FakeLeds(mask=LED_FOCUS)
        original = self.d.leds.set_mask
        self.d.leds.set_mask = lambda mask: (events.append(("leds", mask)), original(mask))[1]
        self.projector.on_show = lambda: events.append(("image", self.projector.current_image.getpixel((160, 90))))
        self.d.show_red()
        events.clear()
        self.d.begin_patterning()
        first_blue_led = events.index(("leds", LED_EXPOSE))
        first_pattern = events.index(("image", (0, 0, 255)))
        self.assertLess(first_pattern, first_blue_led)

    def test_chip_listener_failure_still_undoes_z_and_unlocks(self):
        self.pattern()
        self.d.uv_z_offset = 50.0
        self.d.add_event_listener(Event.CHIP_CHANGED, lambda: (_ for _ in ()).throw(OSError("read-only folder")))
        self.d.begin_patterning()
        self.assertEqual(self.d.stage_setpoint[2], 0.0)
        self.assertFalse(self.d.patterning_busy)
        self.assertEqual(len(self.d.exposure_history), 1)

    def test_position_comes_from_the_controller(self):
        stage = RecordingStage()
        stage.pos = [-12500.0, -9000.0, 5000.0]
        d = EventDispatcher(stage, self.projector, self.root, None)
        self.assertEqual(d.stage_setpoint, (-12500.0, -9000.0, 5000.0))
        d.jog("z", 2.0)
        self.assertEqual(stage.relative, [{"z": 2.0}])          # a jog is a relative move
        self.assertEqual(d.stage_setpoint, (-12500.0, -9000.0, 5002.0))

    def test_homing_takes_the_controller_position_and_can_be_stopped(self):
        class HomingStage(RecordingStage):
            def __init__(self):
                super().__init__(homing=True)
                self.polls, self.stopped = 0, False

            def begin_home(self):
                self.polls = 0

            def homing_finished(self):
                self.polls += 1
                if self.polls >= 3:
                    self.pos = [-1000.0, -2000.0, -3000.0]
                    return True
                return False

            def stop(self):
                self.stopped = True
        stage = HomingStage()
        self.d.hardware.stage = stage
        with patch.object(self.d, "non_blocking_delay", lambda _t: None):
            self.assertTrue(self.d.home_stage())
        self.assertEqual(self.d.stage_setpoint, (-1000.0, -2000.0, -3000.0))
        self.assertFalse(self.d.homing_busy)
        stage.homing_finished = lambda: False
        with patch.object(self.d, "non_blocking_delay", lambda _t: setattr(self.d, "homing_abort", True)):
            self.assertFalse(self.d.home_stage())
        self.assertTrue(stage.stopped)
        self.assertEqual(self.d.movement_lock, MovementLock.UNLOCKED)

    def test_autofocus_travel_is_limited_and_returns_to_start(self):
        self.d.stage_setpoint = (0.0, 0.0, 100.0)
        self.d.camera, self.d.camera_image = object(), np.zeros((4, 4, 3), np.uint8)

        def fresh_frames(count=1, timeout=3.0):
            self.d.frame_index += count
            return True
        with patch.object(self.d, "wait_for_frames", side_effect=fresh_frames), \
             patch.object(self.d, "current_focus", side_effect=lambda blue_only: self.d.stage_setpoint[2]), \
             patch.object(self.d, "non_blocking_delay", lambda _t: None):
            self.assertFalse(self.d.autofocus(blue_only=False))
        self.assertEqual(self.d.stage_setpoint[2], 100.0)
        self.assertLessEqual(max(m.get("z", 100.0) for m in self.stage.moves), 100.0 + 150.0)
        self.assertIn("no sharpest point", self.d.last_autofocus)

    def test_turning_auto_exposure_off_ends_the_stripe_search(self):
        self.d.auto_camera.start_search(0)
        self.d.configure_auto_camera(auto_exposure=False)
        self.assertNotEqual(self.d.auto_camera.phase, "search")
        self.assertEqual(self.d.auto_camera.update(50, {"p99": 200, "stripes": 0.1}, 1.0, -8, 100), [])

    def test_lost_stage_link_during_exposure_turns_light_off_then_explains(self):
        self.pattern()
        self.d.show_red()
        self.d.exposure_time = 400
        calls = {"n": 0}

        def flaky_idle():
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("could not open port")
            return True
        self.stage.is_idle = flaky_idle
        self.d.uv_z_offset = 10.0          # the second idle check (after the Z move) fails
        self.assertFalse(self.d.begin_patterning())
        self.assertNotIn(self.d.shown_image, (ShownImage.PATTERN, ShownImage.UV_FOCUS))   # never exposed
        self.assertEqual(self.d.exposure_history, [])
        self.assertFalse(self.d.patterning_busy)
        self.assertIn("showerror", self.dialog_kinds())
        self.assertIn("Lost contact", " ".join(map(str, self.dialogs[-1][1])))

    def test_border_is_limited(self):
        self.pattern()
        self.d.set_border_size(99.99)
        self.assertLessEqual(self.d.border_size, 90.0)
        self.d.show_red(RedFocusSource.PATTERN)
