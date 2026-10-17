"""Projector output window: display detection, placement, pixel-exact images, previews."""
import tkinter
import unittest

import support  # noqa: F401

from PIL import Image

from projector_panel import scaled_copy
from projector_window import (Display, ProjectorWindow, choose_projector_display, list_displays,
                              parse_xrandr_monitors, parse_xrandr_refresh, solid_color)

MAIN = Display(1, 0, 0, 800, 600, primary=True)
PROJECTOR = Display(2, 800, 0, 640, 360)


class DisplayHelperTests(unittest.TestCase):
    def test_parse_xrandr_two_monitors(self):
        text = ("Monitors: 2\n"
                " 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1\n"
                " 1: +HDMI-1 1280/700x720/390+1920+0  HDMI-1\n")
        self.assertEqual(parse_xrandr_monitors(text), [Display(1, 0, 0, 1920, 1080, True),
                                                       Display(2, 1920, 0, 1280, 720, False)])

    def test_parse_xrandr_current_refresh_rates(self):
        text = ("Screen 0: minimum 320 x 200, current 3200 x 1080, maximum 16384 x 16384\n"
                "eDP-1 connected primary 1920x1080+0+0 (normal left inverted right) 344mm x 194mm\n"
                "   1920x1080     60.01*+  59.97    48.00\n"
                "   1680x1050     59.95\n"
                "HDMI-1 connected 1280x720+1920+0 (normal left inverted right) 700mm x 390mm\n"
                "   1920x1080     60.00 +  50.00    59.94\n"
                "   1280x720      60.00    50.00    47.95*\n"
                "DP-1 disconnected (normal left inverted right x axis y axis)\n"
                "   1024x768      60.00*\n")
        self.assertEqual(parse_xrandr_refresh(text), {"eDP-1": 60, "HDMI-1": 48})

    def test_choose_prefers_saved_display_then_first_secondary(self):
        third = Display(3, -1280, 0, 1280, 720)
        self.assertEqual(choose_projector_display([MAIN, PROJECTOR, third], 3), third)
        self.assertEqual(choose_projector_display([MAIN, PROJECTOR, third], "auto"), PROJECTOR)
        self.assertEqual(choose_projector_display([MAIN, PROJECTOR], 1), PROJECTOR)  # never the main screen
        self.assertIsNone(choose_projector_display([MAIN], "auto"))

    def test_solid_colour_detection(self):
        self.assertEqual(solid_color(Image.new("RGB", (64, 36), (200, 0, 0))), (200, 0, 0))
        self.assertEqual(solid_color(Image.new("L", (8, 8), 0)), (0, 0, 0))
        patterned = Image.new("RGB", (64, 36))
        patterned.putpixel((3, 3), (0, 0, 255))
        self.assertIsNone(solid_color(patterned))

    def test_thumbnail_keeps_thin_lines_visible(self):
        image = Image.new("RGB", (3840, 2160))
        for x in range(0, 3840, 64):
            for y in range(2160):
                image.putpixel((x, y), (0, 0, 255))
        small = scaled_copy(image, 192, 108)
        self.assertEqual(small.size, (192, 108))
        self.assertGreater(max(p[2] for p in small.getdata()), 0)


class ProjectorWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tkinter.Tk()
        except tkinter.TclError as exc:
            self.skipTest(f"Display unavailable: {exc}")

    def tearDown(self):
        self.root.destroy()

    def test_list_displays_always_has_one_main_screen(self):
        displays = list_displays(self.root)
        self.assertGreaterEqual(len(displays), 1)
        self.assertTrue(displays[0].primary)
        self.assertEqual(sum(d.primary for d in displays), 1)

    def test_native_size_is_shown_unscaled_and_others_fit(self):
        window = ProjectorWindow(self.root)
        pattern = Image.new("RGB", (1280, 720))
        pattern.putpixel((10, 10), (0, 0, 255))
        window.open(Display(2, 0, 0, 320, 240))
        window.show(pattern)
        self.assertEqual((window.photo.width(), window.photo.height()), (320, 180))
        window.open(Display(2, 0, 0, 1280, 720))
        window.show(pattern)
        self.assertEqual((window.photo.width(), window.photo.height()), (1280, 720))
        window.close()
        self.assertFalse(window.is_open)

    def test_solid_images_paint_the_window(self):
        window = ProjectorWindow(self.root)
        window.open(Display(2, 0, 0, 320, 180))
        window.show(Image.new("RGB", (320, 180), (128, 0, 0)))
        self.assertIsNone(window.photo)
        self.assertEqual(window.label.cget("background"), "#800000")
        window.show(Image.new("RGB", (320, 180), (0, 0, 0)))
        self.assertEqual(window.label.cget("background"), "#000000")
        window.close()

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
