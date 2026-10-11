"""The Help page: search, the live "Right now" checks and the buttons that go to the right place."""
import tempfile
import tkinter
import unittest
from pathlib import Path
from unittest.mock import patch

import support
from support import DialogsAnswered, RecordingStage, pump

from PIL import Image

import help_page
from dispatcher import load_pattern_image
from projector_window import Display

MAIN = Display(1, 0, 0, 1600, 1000, primary=True)
PROJECTOR = Display(2, 1600, 0, 640, 360)
KNOWN_ACTIONS = {"guide", "camera_adjust", "camera_reconnect", "stage_reconnect", "open_snapshots", "open_home",
                 "open_log", "copy_report"}


class HelpPageTests(DialogsAnswered):
    @classmethod
    def setUpClass(cls):
        import gui
        cls.root = support.new_root()
        cls.patches = [patch("gui.list_displays", return_value=[MAIN, PROJECTOR]),
                       patch("gui.LedController.connect", return_value=(None, "No TI projector found (test).")),
                       patch("help_page.open_path")]
        for p in cls.patches:
            p.start()
        cls.folder = Path(tempfile.mkdtemp())
        cls.stage = RecordingStage()
        lconf = gui.LithographerConfig(cls.stage, None, 1.0, 4167, 25000,
                                       gui.AlignmentConfig(False, "", 1820, 280, 269, 1075, -1100, 800))
        cls.app = gui.LithographerGui(lconf, cls.root, {"camera": {"type": "none"}}, cls.folder / "config.toml")
        cls.help = cls.app.help_page
        cls.root.geometry("1400x900+0+0")
        pump(cls.root, 0.2)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.cleanup()
        except tkinter.TclError:
            pass
        for p in cls.patches:
            p.stop()

    def setUp(self):
        self.help.query.set("")
        self.app.show_page("operate")
        pump(self.root, 0.05)

    def topics(self):
        return [w for _text, w in self.help.entries if isinstance(w, help_page.Collapsible)]

    def test_f1_opens_the_help_page_with_the_search_box_ready(self):
        self.root.event_generate("<F1>")
        self.app.open_help()
        pump(self.root, 0.1)
        self.assertEqual(self.app.current_page, "help")
        self.assertEqual(self.root.focus_get(), self.help.search)

    def test_search_opens_matching_topics_and_hides_the_rest(self):
        self.help.query.set("stripes")
        pump(self.root, 0.05)
        shown = [t for t in self.topics() if t.winfo_manager()]
        self.assertTrue(shown)
        self.assertTrue(all(t.is_open for t in shown))
        self.assertTrue(any("stripes" in t.topic.title for t in shown))
        self.assertLess(len(shown), len(self.topics()))
        self.help.query.set("zzzz nothing")
        pump(self.root, 0.05)
        self.assertFalse(any(t.winfo_manager() for t in self.topics()))
        self.assertTrue(self.help.empty.winfo_manager())
        self.help.query.set("")
        pump(self.root, 0.05)
        self.assertTrue(all(t.winfo_manager() and not t.is_open for t in self.topics()))
        self.assertFalse(self.help.empty.winfo_manager())

    def test_every_topic_button_goes_somewhere_real(self):
        for topic in help_page.HOW_TO + help_page.TROUBLE:
            for _text, name in topic.actions:
                kind = name.split(":", 1)[0]
                self.assertTrue(name in KNOWN_ACTIONS or kind in ("page", "step", "search"), name)
                if kind == "page":
                    self.assertIn(name.split(":", 1)[1], self.app.pages)
                if kind == "step":
                    self.assertIn(name.split(":", 1)[1], self.app.workflow.pages)

    def test_buttons_go_to_the_page_and_step(self):
        self.help.run("step:expose")
        self.assertEqual(self.app.current_page, "operate")
        self.assertEqual(self.app.workflow.step_var.get(), "expose")
        self.help.run("page:chip")
        self.assertEqual(self.app.current_page, "chip")
        self.help.run("search:LED")
        self.assertEqual(self.help.query.get(), "LED")

    def test_right_now_says_what_is_missing_and_how_to_fix_it(self):
        d = self.app.dispatcher
        d.pattern_image_path = ""
        titles = [row[1] for row in self.help.checks()]
        self.assertIn("No pattern chosen yet", titles)
        self.assertIn("Camera is off", titles)
        self.assertIn("Stage: simulation", titles)
        path = self.folder / "p.png"
        Image.new("RGB", (32, 18), "white").save(path)
        d.set_pattern_image(load_pattern_image(path), str(path))
        d.set_stage_connected(False)
        try:
            rows = {row[1]: row for row in self.help.checks()}
            self.assertIn("Pattern chosen", rows)
            self.assertEqual(rows["Stage is disconnected"][3], ("Reconnect stage", "stage_reconnect"))
            self.help.refresh()
            labels = [r[2].cget("text") for r in self.help.now_rows if r[2].winfo_manager()]
            self.assertIn("Stage is disconnected", labels)
        finally:
            d.set_stage_connected(True)

    def test_problem_report_and_folders(self):
        report = self.help.problem_report()
        self.assertIn("HackerFab Stepper problem report", report)
        self.assertIn("System:", report)
        self.help.run("open_home")
        help_page.open_path.assert_called()
        self.help.run("copy_report")
        self.assertIn("problem report", self.root.clipboard_get())


if __name__ == "__main__":
    unittest.main()
