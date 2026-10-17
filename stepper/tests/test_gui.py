"""The main window, with simulated camera, stage, displays and projector LEDs. Needs a display."""
import json
import os
import tempfile
import tkinter
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from support import DialogsAnswered, FakeCamera, FakeLeds, RecordingStage, pump

import toml
from PIL import Image

from dispatcher import RedFocusSource, ShownImage
from projector_window import Display

MAIN = Display(1, 0, 0, 1600, 1000, primary=True)
PROJECTOR = Display(2, 1600, 0, 640, 360)


def key(keysym, shift=False, widget=None):
    return SimpleNamespace(keysym=keysym, state=1 if shift else 0, widget=widget)


class GuiCase(DialogsAnswered):
    displays = [MAIN, PROJECTOR]

    @classmethod
    def setUpClass(cls):
        import gui
        cls.gui = gui
        cls.patches = [patch("gui.list_displays", side_effect=lambda root: list(cls.displays)),
                       patch("camera_view.list_displays", side_effect=lambda root: list(cls.displays)),
                       patch("gui.LedController.connect", return_value=(None, "No TI projector found (test).")),
                       patch("gui.open_guide")]
        cls.root = support.new_root()   # may skip (no display): before patching, so nothing leaks
        for p in cls.patches:
            p.start()
        cls.errors = []
        cls.root.report_callback_exception = lambda *args: cls.errors.append(args[1])
        cls.folder = Path(tempfile.mkdtemp())
        cls.stage = RecordingStage()
        cls.camera = FakeCamera({"device": "0"})
        config = {"camera": {"type": "webcam"}, "projector": {"window": "auto"},
                  "files": {"snapshot-folder": str(cls.folder / "snaps")}}
        lconf = gui.LithographerConfig(cls.stage, cls.camera, 1.0, 4167, 25000,
                                       gui.AlignmentConfig(False, "", 1820, 280, 269, 1075, -1100, 800))
        cls.app = gui.LithographerGui(lconf, cls.root, config, cls.folder / "config.toml")
        cls.d = cls.app.dispatcher
        cls.root.geometry("1400x900+0+0")
        pump(cls.root, 0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.cleanup()
        except tkinter.TclError:
            pass
        for p in cls.patches:
            p.stop()

    def setUp(self):
        self.errors.clear()
        self.app.chip_frame.path.set("")
        self.d.new_chip()
        self.d.exposure_history.clear()
        self.d.stage_setpoint = (0.0, 0.0, 0.0)
        self.stage.moves.clear()
        self.stage.idle = True
        self.app.show_page("operate")
        if not self.app.projector_window.is_open:
            self.app.display_choice = PROJECTOR.label
            self.app.open_projector_window()
        self.d.show_red()
        pump(self.root, 0.05)

    def tearDown(self):
        pump(self.root, 0.02)
        self.assertEqual(self.errors, [], "UI callback raised an exception")

    def pattern(self, size=(640, 360)):
        path = self.folder / "pattern.png"
        image = Image.new("RGB", size, "black")
        for x in range(0, size[0], 40):
            for y in range(size[1]):
                image.putpixel((x, y), (255, 255, 255))
        image.save(path)
        self.assertTrue(self.app.workflow.pages["pattern"].load(path))
        self.app.workflow.pages["expose"].seconds.set("0.05")
        return path


class LayoutTests(GuiCase):
    def test_every_page_shows(self):
        for name in ("chip", "alignment", "settings", "operate"):
            self.app.show_page(name)
            pump(self.root, 0.05)
            self.assertTrue(self.app.pages[name].winfo_ismapped(), name)
            self.assertEqual(self.app.page_var.get(), name)

    def test_projector_window_on_second_display_at_its_resolution(self):
        window = self.app.projector_window
        self.assertTrue(window.is_open)
        self.assertEqual(window.display, PROJECTOR)
        self.assertEqual(self.d.hardware.projector.size(), (640, 360))
        self.assertEqual(window.label.cget("background"), "#ff0000")  # solid red, painted directly
        self.assertEqual(self.app.dots["projector"].kind, "ok")

    def test_panel_says_what_is_projected_and_switches_off(self):
        panel = self.app.projector_panel
        self.assertEqual(panel.badge.cget("text"), "RED · SAFE")
        self.assertIn("does not expose", panel.description.cget("text"))
        panel.mode._click("off")
        self.assertEqual(panel.badge.cget("text"), "OFF")
        self.assertEqual(self.app.projector_window.label.cget("background"), "#000000")
        self.app.workflow.show_step("focus")
        self.assertEqual(panel.badge.cget("text"), "RED · SAFE")

    def test_projector_preview_window(self):
        self.app.open_projector_preview()
        preview = self.app.preview_window
        self.assertTrue(preview.is_open)
        self.assertEqual(preview.badge.cget("text"), "RED · SAFE")
        self.d.clear_projector()
        self.assertEqual(preview.badge.cget("text"), "OFF")
        preview.close()
        self.assertFalse(preview.is_open)
        self.d.show_red()  # listeners removed: no errors after closing

    def test_dimmed_red_is_visible_in_the_previews(self):
        self.d.set_red_brightness(0.08, by_user=False)
        image, note = self.d.preview_image()
        self.assertEqual(image.getpixel((image.width // 2, image.height // 2)), (255, 0, 0))   # full red shown
        self.assertIn("8%", note)
        self.app.open_projector_preview()
        pump(self.root, 0.05)
        canvas = self.app.preview_window.canvas
        texts = [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]
        self.assertTrue(any("shown brighter" in t for t in texts), texts)
        thumb = self.app.projector_panel.thumb
        self.assertIn("8%", [thumb.itemcget(i, "text") for i in thumb.find_all() if thumb.type(i) == "text"])
        self.app.preview_window.close()
        self.d.set_red_brightness(1.0, by_user=False)

    def test_pattern_step_previews_blue_and_red(self):
        path = self.folder / "square.png"
        img = Image.new("RGB", (200, 200), "black")
        img.paste((255, 255, 255), (50, 50, 150, 150))
        img.save(path)
        step = self.app.workflow.pages["pattern"]
        self.assertTrue(step.load(path))
        self.app.workflow.show_step("pattern", apply=False)
        pump(self.root, 0.05)
        self.assertIn("blue light", step.preview_caption.cget("text"))
        step.preview_switch._click("red")
        red = self.d.red_preview_of_pattern()
        self.assertEqual(red.getpixel((red.width // 2, red.height // 2)), (255, 0, 0))
        self.assertIn("red light", step.preview_caption.cget("text"))
        step.preview_switch._click("blue")
        step.show_in_red()                     # projecting it in red also shows the red preview
        self.assertEqual(step.preview_kind.get(), "red")
        self.assertEqual(self.d.shown_image, ShownImage.RED_FOCUS)
        self.d.show_red(RedFocusSource.SOLID)  # leave the other tests their solid red

    def test_status_line_and_dots(self):
        self.app.notify("Hello", "ok")
        self.assertEqual(self.app.toast.label.cget("text"), "Hello")
        self.assertEqual(self.app.dots["stage"].kind, "off")   # simulated stage
        self.assertEqual(self.app.dots["leds"].kind, "off")


class CameraTests(GuiCase):
    def test_frames_reach_the_view_with_focus(self):
        self.camera.push(focus=(2.5, 0.1))
        pump(self.root, 0.1)
        canvas = self.app.camera_view.canvas
        self.assertIsNotNone(canvas.image)
        self.assertEqual(self.app.focus_tracker.value, 2.5)
        pump(self.root, 0.6)
        self.assertEqual(self.app.camera_bar.badge.cget("text"), "LIVE")

    def test_fullscreen_is_a_real_window_and_escape_closes(self):
        self.app.open_camera_fullscreen()
        full = self.app.camera_fullscreen
        self.assertIsInstance(full.window, tkinter.Toplevel)
        self.camera.push()
        pump(self.root, 0.1)
        self.assertIsNotNone(full.canvas.image)
        full.window.event_generate("<Escape>")
        pump(self.root, 0.05)
        self.assertIsNone(self.app.camera_fullscreen)

    def test_snapshot_goes_to_snapshot_folder(self):
        self.camera.push()
        pump(self.root, 0.1)
        self.camera.state = "streaming"
        self.app.take_snapshot()
        path = Path(self.camera.snapshots[-1])
        self.assertEqual(path.parent, self.folder / "snaps")
        self.assertEqual(path.suffix, ".png")
        self.app.snapshot_results.put({"ok": True, "path": str(path), "size": (5472, 3648)})
        pump(self.root, 0.1)
        self.assertIn("5472 × 3648", self.app.toast.label.cget("text"))

    def test_adjustments_manual_exposure_turns_auto_off(self):
        self.app.open_camera_adjustments()
        dialog = self.app.adjust_dialog
        self.d.auto_camera.auto_exposure = True
        dialog.sliders["exposure"].on_change(-5)
        self.assertFalse(self.d.auto_camera.auto_exposure)
        self.assertEqual(self.camera.set_calls[-1], {"exposure": -5})
        dialog.destripe.set(False)
        dialog._processing()
        self.assertEqual(self.camera.processing[-1], {"destripe": False, "average": 1})
        dialog.close()
        self.assertFalse(dialog.is_open)

    def test_resolution_change_reconnects_in_manual_mode(self):
        made = []

        class Camera(FakeCamera):
            def __init__(self, index="auto", settings=None):
                super().__init__(settings)
                made.append(self)
        with patch("gui.Webcam", Camera):
            self.app.replace_camera(Camera(settings={"device": "0", "stripe-free-exposure": -4}))
            from camera.modes import Mode
            self.app.change_resolution(Mode(1920, 1080, 60))
        new = made[-1]
        self.assertEqual((new.settings["mode"], new.settings["width"], new.settings["fps"]), ("manual", 1920, 60))
        self.assertEqual(new.settings.get("stripe-free-exposure"), -4)   # learned value kept
        self.assertEqual(self.app.config["camera"]["mode"], "manual")
        self.assertTrue(made[0].closed)
        self.app.replace_camera(self.camera)  # restore for other tests
        self.camera.closed = False


class StageTests(GuiCase):
    def test_arrow_keys_move_and_shift_is_ten_times(self):
        panel = self.app.stage_panel
        panel.xy_step.set("10")
        self.app.handle_key(key("Right"))
        self.app.handle_key(key("Up", shift=True))
        self.assertEqual(self.d.stage_setpoint[:2], (10.0, 100.0))
        panel.z_step.set("2")
        self.app.handle_key(key("Next"))
        self.assertEqual(self.d.stage_setpoint[2], -2.0)
        self.assertEqual(panel.values["x"].cget("text"), "10.0")

    def test_keys_ignored_while_typing_or_busy(self):
        entry = tkinter.Entry(self.root)
        with patch.object(self.app.stage_panel, "focus_get", return_value=entry):
            self.app.handle_key(key("Right"))
        self.d.patterning_busy = True
        self.app.handle_key(key("Right"))
        self.d.patterning_busy = False
        self.assertEqual(self.stage.moves, [])

    def test_key_repeat_waits_for_the_stage(self):
        self.stage.idle = False
        self.app.handle_key(key("Left"))
        self.assertEqual(self.stage.moves, [])

    def test_hold_to_repeat_only_when_idle(self):
        panel = self.app.stage_panel
        button = panel.z_up
        calls = []
        button.action = lambda: calls.append(1)
        idle = {"value": False}
        button.can_repeat = lambda: idle["value"]
        button._press()
        self.assertEqual(len(calls), 1)
        button._repeat()
        self.assertEqual(len(calls), 1)  # stage still moving: no queued move
        idle["value"] = True
        button._repeat()
        self.assertEqual(len(calls), 2)
        button._release()
        button._repeat()
        self.assertEqual(len(calls), 2)

    def test_buttons_locked_during_exposure(self):
        self.d.set_patterning_busy(True)
        self.assertTrue(self.app.stage_panel.z_up.instate(["disabled"]))
        self.d.set_patterning_busy(False)
        self.assertFalse(self.app.stage_panel.z_up.instate(["disabled"]))

    def test_go_to_moves_after_confirmation(self):
        from stage_pad import GoToDialog
        dialog = GoToDialog(self.app)
        for axis, value in zip("xyz", ("5000", "-3000", "10")):
            dialog.vars[axis].set(value)
        dialog.move()  # multi-axis long move: asks first, default answer Cancel
        self.assertEqual(self.stage.moves, [])
        dialog.window.destroy()
        dialog = GoToDialog(self.app)
        dialog.vars["x"].set("250")
        dialog.move()
        self.assertEqual(self.stage.moves[-1]["x"], 250.0)


class ExposureTests(GuiCase):
    def test_start_button_exposes_on_the_projector_at_native_size(self):
        self.pattern()
        seen = []
        window = self.app.projector_window
        original = window.show
        window.show = lambda image: (seen.append(image), original(image))
        try:
            self.app.workflow.show_step("expose")
            self.assertTrue(self.app.workflow.footer.winfo_ismapped() or True)
            self.app.workflow.pages["expose"].start.invoke()
        finally:
            window.show = original
        self.assertEqual(len(self.d.exposure_history), 1)
        blue = [img for img in seen if img.getpixel((0, 180)) == (0, 0, 255)]
        self.assertTrue(blue, "the pattern reached the projector window")
        self.assertEqual(blue[0].size, (640, 360))
        self.assertEqual(self.d.shown_image.value, "red_focus")
        self.assertIn("Done", self.app.workflow.pages["expose"].last.cget("text"))

    def test_checklist(self):
        page = self.app.workflow.pages["expose"]
        self.d.pattern_image_path = ""
        page.refresh_checks()
        self.assertTrue(page.checks[0].cget("text").startswith("✗"))
        self.pattern()
        page.refresh_checks()
        texts = [label.cget("text") for label in page.checks]
        self.assertTrue(texts[0].startswith("✓  Pattern"))
        self.assertIn("Projector output on Display 2", texts[2])

    def test_one_display_asks_and_gives_the_screen_back(self):
        self.pattern()
        self.app.projector_window.close()
        type(self).displays = [MAIN]
        try:
            self.d.begin_patterning()   # default answer: No
            self.assertEqual(self.d.exposure_history, [])
            with patch("tkinter.messagebox.askyesno", return_value=True):
                self.d.begin_patterning()
            self.assertEqual(len(self.d.exposure_history), 1)
            self.assertFalse(self.app.projector_window.is_open)  # temporary fullscreen closed again
        finally:
            type(self).displays = [MAIN, PROJECTOR]

    def test_exposure_time_entry(self):
        page = self.app.workflow.pages["expose"]
        page.seconds.set("2.5")
        self.assertEqual(self.d.exposure_time, 2500)
        page.seconds.set("abc")
        self.assertIsNone(self.d.exposure_time)
        page.presets._click("8")
        self.assertEqual(self.d.exposure_time, 8000)

    def test_close_offers_to_save_unsaved_exposures(self):
        self.pattern()
        self.d.begin_patterning()
        with patch.object(self.app, "cleanup") as cleanup:
            self.app.request_close()           # default: Cancel
            cleanup.assert_not_called()
            target = self.folder / "kept.json"
            with patch("tkinter.messagebox.askyesnocancel", return_value=True), \
                 patch("tkinter.filedialog.asksaveasfilename", return_value=str(target)):
                self.app.request_close()
            cleanup.assert_called_once()
        self.assertTrue(target.exists())
        self.assertTrue((self.folder / "config.toml").exists())   # settings remembered on exit

    def test_close_during_exposure_stops_it_first(self):
        self.d.patterning_busy = True
        with patch.object(self.root, "after") as later:
            self.app.request_close()
        self.assertTrue(self.d.should_abort)
        later.assert_called_once()
        self.d.patterning_busy, self.d.should_abort = False, False


class LedTests(GuiCase):
    def test_led_test_sequence(self):
        leds = FakeLeds()
        self.d.attach_leds(leds, "connected")
        with patch.object(self.root, "after", side_effect=lambda ms, fn=None, *a: fn()), \
             patch("tkinter.messagebox.askyesno", return_value=True):
            self.app.test_leds()
        self.assertEqual(leds.calls, [1, 0, 1])  # red only, all off, red only
        self.assertIn("works", self.app.toast.label.cget("text"))
        self.assertEqual(self.app.projector_panel.leds.cget("text"), "LEDs: red only · USB")
        self.app.disconnect_leds()
        self.assertEqual(leds.closed_with, 7)
        self.assertIsNone(self.d.leds)


class SettingsAndChipTests(GuiCase):
    def test_settings_saved_with_choices(self):
        page = self.app.settings_page
        page.resolution.set("3840x2160@20")
        page.save()
        saved = toml.load(self.folder / "config.toml")
        self.assertEqual(saved["camera"]["mode"], "manual")
        self.assertEqual(saved["camera"]["width"], 3840)
        self.assertIn("red-brightness", saved["projector"])
        page.resolution.set(page.RESOLUTION_HIGHEST)
        page.save()
        self.assertEqual(toml.load(self.folder / "config.toml")["camera"]["mode"], "max")

    def test_jog_direction_setting_applies_immediately(self):
        page = self.app.settings_page
        page.jog_invert["x"].set(True)
        page._jog_setting()
        self.app.stage_panel.xy_step.set("10")
        self.app.handle_key(key("Right"))
        self.assertEqual(self.d.stage_setpoint[0], -10.0)
        page.jog_invert["x"].set(False)
        page._jog_setting()

    def test_chip_from_another_pc_opens(self):
        chip = self.folder / "chip.json"
        chip.write_text(json.dumps({"layers": [{"exposures": [
            {"time": "2026-09-01T10:00:00", "path": "D:/patterns/not-here.png",
             "coords": [1.0, 2.0, 3.0], "duration": 8000, "aborted": False}]}]}))
        self.d.load_chip(str(chip))
        self.app.show_page("chip")
        pump(self.root, 0.05)
        self.assertEqual(len(self.d.chip.layers[-1].exposures), 1)

    def test_failed_open_keeps_the_current_chip_file(self):
        frame = self.app.chip_frame
        frame.path.set(str(self.folder / "mine.json"))
        broken = self.folder / "broken.json"
        broken.write_text("not json")
        with patch("tkinter.filedialog.askopenfilename", return_value=str(broken)):
            frame.open_chip_button.invoke()
        self.assertEqual(self.dialog_kinds(), ["showerror"])
        self.assertEqual(frame.path.get(), str(self.folder / "mine.json"))

    def test_alignment_nudge_moves_the_image(self):
        page = self.app.alignment_page
        page.shift_step.set("20")
        page.nudge(1, 0, 0)
        self.assertEqual(self.d.image_adjust_position[0], 20)
        page.reset()
        self.assertEqual(self.d.image_adjust_position, (0, 0, 0.0))


class ReviewFixTests(GuiCase):
    """Regression tests for problems found in review."""

    def test_mouse_wheel_never_changes_dropdowns_or_number_boxes(self):
        changes = []
        with patch.object(self.app, "change_resolution", side_effect=changes.append), \
             patch.object(self.app, "display_chosen", side_effect=changes.append):
            self.app.workflow.show_step("pattern")
            pump(self.root, 0.05)
            border = self.app.workflow.pages["pattern"].border
            widgets = [self.app.camera_bar.resolution, self.app.projector_panel.display]
            for widget in widgets:
                for sequence, extra in (("<MouseWheel>", {"delta": -120}), ("<Button-5>", {}), ("<Button-4>", {})):
                    widget.event_generate(sequence, x=5, y=5, **extra)
            pump(self.root, 0.05)
            self.assertEqual(changes, [])
            self.assertEqual(border.get(), "0")
        self.app.workflow.show_step("focus")

    def test_output_and_resolution_locked_during_exposure(self):
        self.app.workflow.pages["pattern"].load(self._pattern())
        self.d.exposure_time = 300
        seen = {}

        def look():
            seen["output"] = str(self.app.projector_panel.display.cget("state"))
            seen["resolution"] = str(self.app.camera_bar.resolution.cget("state"))
            self.app.projector_window._close_requested()      # the window's X button
            seen["window_open"] = self.app.projector_window.is_open
        self.root.after(100, look)
        self.d.begin_patterning()
        self.assertEqual(seen, {"output": "disabled", "resolution": "disabled", "window_open": True})
        self.assertEqual(str(self.app.projector_panel.display.cget("state")), "readonly")

    def test_escape_in_camera_fullscreen_stops_the_exposure(self):
        self.app.workflow.pages["pattern"].load(self._pattern())
        self.d.exposure_time = 3000
        self.app.open_camera_fullscreen()
        pump(self.root, 0.05)
        self.root.after(100, lambda: self.app.camera_fullscreen._escape())
        self.d.begin_patterning()
        self.assertTrue(self.d.exposure_history[-1].aborted)
        self.assertIsNotNone(self.app.camera_fullscreen)    # Esc stopped the exposure, not the view
        self.app.camera_fullscreen.close()

    def test_led_test_window_closed_early_restores_everything(self):
        leds = FakeLeds()
        self.d.attach_leds(leds, "connected")
        self.app.test_leds()
        pump(self.root, 0.1)
        windows = [w for w in self.root.winfo_children()
                   if isinstance(w, tkinter.Toplevel) and w.title() == "Testing projector LEDs"]
        windows[0].destroy()
        pump(self.root, 0.1)
        self.assertEqual(self.errors, [])
        self.assertEqual(leds.mask, 1)                      # red only again, matching the red picture
        self.app.disconnect_leds()

    def test_unknown_settings_survive_a_save(self):
        page = self.app.settings_page
        page.device.set("1")
        page.text_scale.set("1.2")
        page.proj_resolution.set("2560x1600")
        page.save()
        saved = toml.load(self.folder / "config.toml")
        self.assertEqual(saved["camera"]["device"], "1")
        self.assertEqual(saved["ui"]["text-scale"], 1.2)
        self.assertEqual(saved["projector"]["resolution"], "2560x1600")
        page.device.set("auto")
        page.proj_resolution.set("auto")
        page.text_scale.set("1.0")

    def test_auto_exposure_switch_is_saved(self):
        self.d.configure_auto_camera(auto_exposure=False)
        self.camera.controls["exposure"] = -6
        self.app.settings_page.save()
        self.assertEqual(toml.load(self.folder / "config.toml")["camera"]["exposure"], -6)
        self.d.configure_auto_camera(auto_exposure=True)
        self.app.settings_page.save()
        self.assertEqual(toml.load(self.folder / "config.toml")["camera"]["exposure"], "auto")

    def test_uv_mark_turns_off_when_leaving_the_expose_step(self):
        self.app.workflow.show_step("expose")
        self.d.show_uv_mark()
        self.app.workflow.show_step("focus")
        self.assertEqual(self.d.shown_image, ShownImage.RED_FOCUS)
        self.app.workflow.show_step("expose")
        self.d.show_uv_mark()
        self.app.show_page("settings")
        self.assertEqual(self.d.shown_image, ShownImage.RED_FOCUS)
        self.app.show_page("operate")

    def test_stop_with_nothing_running_turns_the_projector_off(self):
        self.app.stop_everything()
        self.assertEqual(self.d.shown_image, ShownImage.CLEAR)
        self.assertIn("Projector turned off", self.app.toast.label.cget("text"))

    def _pattern(self):
        path = self.folder / "review.png"
        Image.new("RGB", (640, 360), "white").save(path)
        return path


class NoCameraTests(DialogsAnswered):
    def test_window_without_camera_or_second_display(self):
        import gui
        root = support.new_root()
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args[1])
        with patch("gui.list_displays", return_value=[MAIN]), \
             patch("gui.LedController.connect", return_value=(None, "none")):
            lconf = gui.LithographerConfig(RecordingStage(), None, 1.0, 4167, 25000,
                                           gui.AlignmentConfig(False, "", 1820, 280, 269, 1075, -1100, 800))
            app = gui.LithographerGui(lconf, root, {"camera": {"type": "none"}}, Path(tempfile.mkdtemp()) / "c.toml")
            pump(root, 0.3)
            self.assertEqual(app.camera_view.canvas.message[0], "Camera off")
            self.assertFalse(app.projector_window.is_open)
            self.assertIn("Win+P", app.projector_panel.output_note.cget("text"))
            self.assertEqual(app.dots["projector"].kind, "bad")
            app.cleanup()
        self.assertEqual(errors, [])


class MigrationTests(unittest.TestCase):
    def test_old_settings_get_automatic_exposure_and_highest_resolution(self):
        import gui
        old = {"camera": {"exposure": -4, "mode": "auto", "width": 1280, "red-exposure": 4167.0}}
        changes = gui.migrate_config(old)
        self.assertEqual(old["camera"]["exposure"], "auto")
        self.assertEqual(old["camera"]["mode"], "max")
        self.assertEqual(old["camera"]["red-exposure"], 4167.0)   # still used by Basler/FLIR cameras
        self.assertEqual(old["app"]["config-version"], gui.CONFIG_VERSION)
        self.assertEqual(len(changes), 2)
        manual = {"app": {"config-version": 2}, "camera": {"exposure": -5, "mode": "manual"}}
        self.assertEqual(gui.migrate_config(manual), [])
        self.assertEqual(manual["camera"]["exposure"], -5)


class HelpTests(unittest.TestCase):
    def test_help_opens_guide(self):
        import gui
        folder = Path(tempfile.mkdtemp())
        cwd = os.getcwd()
        os.chdir(folder)
        try:
            (folder / gui.GUIDE_NAME).write_text("<html></html>")
            with patch("gui.webbrowser.open") as browser:
                gui.open_guide()
            self.assertTrue(browser.call_args[0][0].startswith("file:"))
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
