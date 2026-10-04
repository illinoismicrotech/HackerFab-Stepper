"""Tests for the dedicated projector window (display detection, placement, exposure routing)."""
import tempfile
import tkinter
import unittest
from pathlib import Path
from unittest.mock import patch

from tk_runtime import enable_font_support
enable_font_support()

import ttkbootstrap as ttk
from PIL import Image

import gui
from projector_window import Display, ProjectorWindow, choose_projector_display, list_displays, parse_xrandr_monitors
from stage_control.stage_controller import StageController

MAIN = Display(1, 0, 0, 800, 600, primary=True)
PROJECTOR = Display(2, 800, 0, 640, 360)


class DisplayHelperTests(unittest.TestCase):
    def test_parse_xrandr_two_monitors(self):
        text = ("Monitors: 2\n"
                " 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1\n"
                " 1: +HDMI-1 1280/700x720/390+1920+0  HDMI-1\n")
        self.assertEqual(parse_xrandr_monitors(text), [Display(1, 0, 0, 1920, 1080, True),
                                                       Display(2, 1920, 0, 1280, 720, False)])

    def test_choose_prefers_saved_display_then_first_secondary(self):
        third = Display(3, -1280, 0, 1280, 720)
        self.assertEqual(choose_projector_display([MAIN, PROJECTOR, third], 3), third)
        self.assertEqual(choose_projector_display([MAIN, PROJECTOR, third], "auto"), PROJECTOR)
        self.assertEqual(choose_projector_display([MAIN, PROJECTOR], 1), PROJECTOR)  # never the main screen
        self.assertIsNone(choose_projector_display([MAIN], "auto"))

    def test_list_displays_always_has_one_main_screen(self):
        try:
            root = tkinter.Tk()
        except tkinter.TclError as exc:
            self.skipTest(f"Display unavailable: {exc}")
        try:
            displays = list_displays(root)
            self.assertGreaterEqual(len(displays), 1)
            self.assertTrue(displays[0].primary)
            self.assertEqual(sum(d.primary for d in displays), 1)
        finally:
            root.destroy()


class ProjectorGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ttkbootstrap.style
        ttkbootstrap.style.Style.instance = None
        cls.displays_patch = patch("gui.list_displays", return_value=[MAIN, PROJECTOR])
        cls.displays_patch.start()
        try:
            cls.root = ttk.Window(themename="darkly")
        except tkinter.TclError as exc:
            cls.displays_patch.stop()
            raise unittest.SkipTest(f"Display unavailable: {exc}")
        config = gui.LithographerConfig(StageController(), None, .25, 4167, 25000,
                                        gui.AlignmentConfig(False, "", 1820, 280, 269, 1075, -1100, 800))
        cls.app = gui.LithographerGui(config, cls.root, {"camera": {"type": "none"}, "projector": {"window": "auto"}},
                                      Path(tempfile.mkdtemp()) / "config.toml")
        cls.d = cls.app.event_dispatcher
        for _ in range(5):
            cls.root.update()

    @classmethod
    def tearDownClass(cls):
        cls.app.cleanup()
        cls.displays_patch.stop()

    def run(self, result=None):
        self.dialogs = []
        def record(kind, answer):
            def fn(*args, **kwargs):
                self.dialogs.append((kind, args, kwargs))
                return answer
            return fn
        with patch("gui.messagebox.showinfo", record("showinfo", None)), \
             patch("gui.messagebox.showerror", record("showerror", None)), \
             patch("gui.messagebox.showwarning", record("showwarning", None)), \
             patch("gui.messagebox.askyesno", record("askyesno", False)), \
             patch("gui.messagebox.askokcancel", record("askokcancel", False)), \
             patch("gui.messagebox.askyesnocancel", record("askyesnocancel", None)):
            return super().run(result)

    def setUp(self):
        self.app.projector_choice.set(PROJECTOR.label)
        if not self.app.projector_window.is_open:
            self.app.open_projector_window()
        self.root.update()

    def _pattern(self, ms=300):
        path = Path(tempfile.mkdtemp()) / "pattern.png"
        Image.new("RGB", (1280, 720), "white").save(path)
        self.d.set_pattern_image(Image.open(path).convert("RGB"), str(path))
        self.d.exposure_time = ms

    def test_opens_automatically_on_the_second_display(self):
        window = self.app.projector_window
        self.assertTrue(window.is_open)
        self.assertEqual(window.display, PROJECTOR)
        self.assertEqual(window.window.winfo_width(), PROJECTOR.width)
        self.assertEqual(window.window.winfo_height(), PROJECTOR.height)
        self.assertEqual(str(window.window.cget("cursor")), "none")  # no pointer projected onto resist
        self.assertIn("Display 2", self.app.projector_sidebar.get())

    def test_exposure_goes_to_projector_window_not_main_window(self):
        self._pattern()
        shown, kinds = [], []
        window = self.app.projector_window
        original = window.show
        def spy(image):
            shown.append(image.getextrema())
            kinds.append(self.app.fullscreen.kind)
            original(image)
        window.show = spy
        try:
            self.d.begin_patterning()
        finally:
            window.show = original
        lit = [e for e in shown if e != ((0, 0), (0, 0), (0, 0))]
        self.assertTrue(lit, "pattern never reached the projector window")
        self.assertEqual(shown[-1], ((0, 0), (0, 0), (0, 0)))  # cleared afterwards
        self.assertEqual(set(kinds), {None})                   # main window never went fullscreen
        self.assertFalse(bool(self.root.attributes("-fullscreen")))
        self.assertFalse(self.d.chip.layers[-1].exposures[-1].aborted)

    def test_escape_in_main_window_aborts_exposure(self):
        self._pattern(ms=3000)
        self.root.after(300, lambda: self.root.event_generate("<Escape>"))
        self.d.begin_patterning()
        self.assertTrue(self.d.chip.layers[-1].exposures[-1].aborted)
        self.assertFalse(self.d.patterning_busy)

    def test_closing_preview_does_not_clear_projector_window(self):
        self.d.enter_red_mode(mode_switch_autofocus=False)
        self.app.fullscreen.open("projector", lambda: self.d.hardware.projector.current_image)
        self.app.fullscreen.close()
        self.assertEqual(self.d.shown_image, gui.ShownImage.RED_FOCUS)

    def test_checklist_reports_projector_window(self):
        checklist = self.app.mode_select_frame.uv_mode_frame.checklist
        checklist.refresh()
        self.assertIn("✓ Projector window on Display 2", checklist.lines[3].cget("text"))
        self.app.toggle_projector_window()
        checklist.refresh()
        self.assertIn("No projector window", checklist.lines[3].cget("text"))
        self.assertIn("in this window", self.app.projector_sidebar.get())

    def test_main_screen_needs_confirmation(self):
        self.app.projector_window.close()
        self.app.projector_choice.set(MAIN.label)
        self.app.open_projector_window()  # default answer: No
        self.assertFalse(self.app.projector_window.is_open)
        self.assertEqual(self.dialogs[-1][0], "askyesno")

    def test_choice_is_remembered_for_save_settings(self):
        self.app.projector_choice.set(PROJECTOR.label)
        self.app._on_display_chosen()
        self.assertEqual(self.app.settings_page.config["projector"]["display"], 2)

    def test_cannot_close_window_mid_exposure(self):
        self.d.patterning_busy = True
        try:
            self.app.toggle_projector_window()
        finally:
            self.d.patterning_busy = False
        self.assertTrue(self.app.projector_window.is_open)
        self.assertEqual(self.dialogs[-1][0], "showinfo")

    def test_red_brightness_dims_red_image_and_is_remembered(self):
        self.d.enter_red_mode(mode_switch_autofocus=False)
        red = self.app.mode_select_frame.red_mode_frame
        red.red_brightness_var.set(25)
        self.root.update()
        peak = self.d.current_image.getchannel("R").getextrema()[1]
        self.assertAlmostEqual(peak, 64, delta=1)
        self.assertEqual(self.app.projector_window.image.getchannel("R").getextrema()[1], peak)
        self.assertEqual(self.app.settings_page.config["projector"]["red-brightness"], 25)
        red.red_brightness_var.set(100)
        self.root.update()
        self.assertEqual(self.d.current_image.getchannel("R").getextrema()[1], 255)

    def test_settings_page_saves_exposure(self):
        page = self.app.settings_page
        page.vars["exposure"].set("-4")
        self.assertEqual(page.validated()["camera"]["exposure"], -4)
        page.vars["exposure"].set("auto")
        self.assertEqual(page.validated()["camera"]["exposure"], "auto")
        self.assertIn("-4 · 62.5 ms", page.widgets["exposure"].cget("values"))

    def test_mouse_wheel_scrolls_when_main_window_has_focus(self):
        self.app.show_page("Operate")
        canvas = self.app.pages["Operate"].canvas
        canvas.yview_moveto(0)
        self.root.update()
        self.root.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertGreater(canvas.yview()[0], 0.0)
        canvas.yview_moveto(0)

    def test_off_setting_and_single_display(self):
        self.app.projector_window.close()
        self.app.projector_settings = {"window": "off"}
        self.app.auto_open_projector_window()
        self.assertFalse(self.app.projector_window.is_open)
        self.app.projector_settings = {"window": "auto"}
        with patch("gui.list_displays", return_value=[MAIN]):
            self.app._refresh_display_choices()
            self.app._projector_window_changed()
            self.assertIn(gui.EXTEND_DISPLAYS, self.app.projector_note.cget("text"))
            self.app.auto_open_projector_window()
            self.assertFalse(self.app.projector_window.is_open)
        self.app._refresh_display_choices()


class ProjectorWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tkinter.Tk()
        except tkinter.TclError as exc:
            self.skipTest(f"Display unavailable: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_fits_image_and_keeps_native_size_unscaled(self):
        window = ProjectorWindow(self.root)
        window.open(Display(2, 0, 0, 320, 240))
        window.show(Image.new("RGB", (1280, 720), "white"))
        self.assertEqual((window.photo.width(), window.photo.height()), (320, 180))
        window.open(Display(2, 0, 0, 1280, 720))
        window.show(Image.new("RGB", (1280, 720), "white"))
        self.assertEqual((window.photo.width(), window.photo.height()), (1280, 720))
        window.close()
        self.assertFalse(window.is_open)

    def test_movable_window_reports_close(self):
        closed = []
        window = ProjectorWindow(self.root, on_close=lambda: closed.append(True))
        window.open(None, Image.new("RGB", (64, 36), "red"))
        self.root.update()
        self.assertFalse(bool(window.window.overrideredirect()))
        window.close()
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
