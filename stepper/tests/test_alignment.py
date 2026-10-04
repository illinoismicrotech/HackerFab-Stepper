"""Alignment-marker detection without PyTorch, alignment maths, and tiling."""
import tempfile
import tkinter
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from tk_runtime import enable_font_support
enable_font_support()

import ttkbootstrap as ttk

import gui
from alignment_detector import OnnxMarkerDetector, load_marker_detector, marker_centers
from stage_control.stage_controller import StageController

STEPPER = Path(__file__).resolve().parents[1]
MODEL = STEPPER / "ckpts" / "best.onnx"


def alignment(**overrides):
    values = dict(enabled=True, model_path="", right_marker_x=1820, left_marker_x=280, top_marker_y=269,
                  bottom_marker_y=1075, x_scale_factor=-1100, y_scale_factor=800)
    values.update(overrides)
    return gui.AlignmentConfig(**values)


class FakeNet:
    """Stands in for the OpenCV network: returns a prepared YOLO output tensor."""

    def __init__(self, rows):
        # rows: (cx, cy, w, h, score) in 640x640 model coordinates
        self.output = np.array(rows, dtype=np.float32).T[np.newaxis]

    def setInput(self, _blob):
        pass

    def forward(self):
        return self.output


class DetectorTests(unittest.TestCase):
    def test_bundled_model_loads_on_opencv_and_finds_nothing_in_a_blank_frame(self):
        detector = OnnxMarkerDetector(MODEL)
        detector.net.setInput(np.zeros((1, 3, 640, 640), np.float32))
        self.assertEqual(detector.net.forward().shape, (1, 5, 8400))  # 4 box values + 1 class
        self.assertEqual(detector.detect(np.zeros((720, 1280, 3), np.uint8)), [])

    def test_boxes_are_scaled_back_to_the_camera_frame_and_duplicates_removed(self):
        detector = OnnxMarkerDetector.__new__(OnnxMarkerDetector)
        detector.net = FakeNet([
            (320, 320, 64, 64, 0.90),   # a mark in the middle
            (322, 321, 64, 64, 0.80),   # the same mark again: removed
            (40, 40, 20, 20, 0.10),     # below the confidence cut-off: ignored
        ])
        found = detector.detect(np.zeros((1080, 1920, 3), np.uint8))
        self.assertEqual(len(found), 1)
        ((x0, y0), (x1, y1)), score = found[0]
        self.assertAlmostEqual(score, 0.9, places=5)
        self.assertEqual(((x0, y0), (x1, y1)), ((864, 486), (1056, 594)))  # 640 -> 1920 x 1080

    def test_pt_path_uses_the_onnx_model_next_to_it(self):
        detector = load_marker_detector(STEPPER / "ckpts" / "best.pt")
        self.assertIsInstance(detector, OnnxMarkerDetector)

    def test_missing_model_disables_detection_instead_of_crashing(self):
        self.assertIsNone(load_marker_detector(Path(tempfile.mkdtemp()) / "nothing.onnx"))


class AlignmentMathsTests(unittest.TestCase):
    def test_marks_on_their_calibrated_positions_need_no_move_at_any_resolution(self):
        for width, height in ((1920, 1080), (1280, 720), (5472, 3078)):
            sx, sy = width / 1920, height / 1080
            boxes = [((int(x * sx) - 5, int(y * sy) - 5), (int(x * sx) + 5, int(y * sy) + 5))
                     for x, y in ((280, 269), (1820, 269), (280, 1075), (1820, 1075))]
            dx, dy, nx, ny = gui.alignment_correction(marker_centers(boxes, width, height), alignment())
            self.assertAlmostEqual(dx, 0, delta=6)
            self.assertAlmostEqual(dy, 0, delta=6)
            self.assertEqual((nx, ny), (4, 4))

    def test_correction_direction_and_size(self):
        # A mark 1 % of the frame right of its calibrated spot: move by -0.01 * x_scale_factor.
        x = 1820 / 1920 + 0.01
        dx, dy, _, _ = gui.alignment_correction([(x, 269 / 1080)], alignment())
        self.assertAlmostEqual(dx, 11.0, places=6)
        self.assertAlmostEqual(dy, 0.0, places=6)

    def test_tiling_uses_only_top_marks_for_y(self):
        dx, dy, nx, ny = gui.alignment_correction([(0.1, 0.9)], alignment(), tiling=True)
        self.assertEqual((nx, ny), (1, 0))
        self.assertEqual(dy, 0.0)

    def test_edge_filter(self):
        centers = [(0.1, 0.5), (0.9, 0.5), (0.5, 0.1)]
        self.assertEqual(gui.filter_edge(centers, "left"), [(0.1, 0.5)])
        self.assertEqual(gui.filter_edge(centers, "right"), [(0.9, 0.5)])
        self.assertEqual(gui.filter_edge(centers, "top"), [(0.5, 0.1)])


class TilingPlanTests(unittest.TestCase):
    def test_previous_tile_edge_follows_the_snake(self):
        # 3 columns x 2 rows: row 0 goes left->right, row 1 right->left.
        self.assertIsNone(gui.tiling_alignment_edge(0, 0, 3))
        self.assertEqual(gui.tiling_alignment_edge(1, 0, 3), "left")
        self.assertEqual(gui.tiling_alignment_edge(2, 0, 3), "left")
        self.assertEqual(gui.tiling_alignment_edge(2, 1, 3), "top")    # first tile of row 1 (under the last one)
        self.assertEqual(gui.tiling_alignment_edge(1, 1, 3), "right")
        self.assertEqual(gui.tiling_alignment_edge(0, 1, 3), "right")  # was wrongly 'top' before

    def test_tiles_are_evenly_spaced_and_cover_the_image(self):
        for length in (100, 3840, 3841, 7480, 7481, 12000):
            positions = gui.tile_positions(length, 3840, 200)
            self.assertEqual(positions, [i * 3640 for i in range(len(positions))])
            self.assertGreaterEqual(positions[-1] + 3840, length)
            if len(positions) > 1:
                self.assertLess(positions[-2] + 3840, length)  # no wasted extra tile


class RecordingStage(StageController):
    def __init__(self):
        self.moves = []

    def move_to(self, amounts):
        self.moves.append(dict(amounts))


class TilingGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ttkbootstrap.style
        ttkbootstrap.style.Style.instance = None
        try:
            cls.root = ttk.Window(themename='darkly')
        except tkinter.TclError as exc:
            raise unittest.SkipTest(f'Display unavailable: {exc}')
        config = gui.LithographerConfig(StageController(), None, .25, 4167, 25000, alignment(model_path=str(MODEL)))
        cls.app = gui.LithographerGui(config, cls.root, {'camera': {'type': 'none'}},
                                      Path(tempfile.mkdtemp()) / 'config.toml')
        cls.d = cls.app.event_dispatcher
        cls.tiling = cls.app.tiling_frame

    @classmethod
    def tearDownClass(cls):
        cls.app.cleanup()

    def run(self, result=None):
        self.dialogs = []
        def record(kind, answer):
            def fn(*args, **kwargs):
                self.dialogs.append((kind, args))
                return answer
            return fn
        with patch('gui.messagebox.showinfo', record('showinfo', None)), \
             patch('gui.messagebox.showwarning', record('showwarning', None)), \
             patch('gui.messagebox.showerror', record('showerror', None)), \
             patch('gui.messagebox.askokcancel', record('askokcancel', True)):
            return super().run(result)

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.tiling.tile_dir = self.folder / 'tiles'
        self.tiling.tile_size, self.tiling.tile_overlap = (40, 20), (10, 5)
        self.stage = RecordingStage()
        self.d.hardware.stage = self.stage
        self.d.stage_setpoint = (0.0, 0.0, 0.0)
        self.d.new_chip()
        self.d.exposure_time = 20
        pattern = self.folder / 'big.png'
        Image.new('RGB', (95, 50), 'white').save(pattern)  # 3 x 3 tiles of 40 x 20 with 10 x 5 overlap
        self.d.set_pattern_image(gui.load_pattern_image(pattern), str(pattern))

    def test_split_makes_an_even_grid_and_fills_in_the_counts(self):
        self.tiling.segment()
        self.assertEqual((self.tiling.x_settings.amount_var.get(), self.tiling.y_settings.amount_var.get()), ('3', '3'))
        self.assertEqual(len(list(self.tiling.tile_dir.glob('tile_*.png'))), 9)
        # The last column starts at 60 and reaches past the 95 px image: the overhang is black.
        corner = Image.open(self.tiling.tile_path(2, 0)).convert('RGB')
        self.assertEqual(corner.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(corner.getpixel((39, 0)), (0, 0, 0))

    def test_runs_every_tile_in_snake_order_on_an_even_grid(self):
        self.tiling.segment()
        self.tiling.x_settings.offset_var.set('100')
        self.tiling.y_settings.offset_var.set('50')
        self.tiling.on_begin()
        self.assertEqual(len(self.d.chip.layers[-1].exposures), 9)
        self.assertFalse(self.tiling.running)
        xy = [(m['x'], m['y']) for m in self.stage.moves if 'x' in m and 'z' not in m]
        self.assertEqual(xy, [(-100, 0), (-200, 0),               # row 1, left to right
                              (-200, -50), (-100, -50), (0, -50),  # row 2, right to left
                              (0, -100), (-100, -100), (-200, -100)])
        self.assertIn('finished', self.dialogs[-1][1][1])

    def test_aborted_exposure_stops_the_whole_run(self):
        self.tiling.segment()
        original = self.d.begin_patterning
        def abort_second():
            if len(self.d.chip.layers[-1].exposures) == 1:
                self.d.should_abort = True
            original()
        with patch.object(self.d, 'begin_patterning', side_effect=abort_second):
            self.tiling.on_begin()
        self.assertEqual(len(self.d.chip.layers[-1].exposures), 2)  # tile 1 done, tile 2 aborted, no more
        self.assertIn('stopped', self.dialogs[-1][1][1])
        self.assertEqual(str(self.tiling.abort_tiling_button.cget('state')), 'disabled')

    def test_start_before_splitting_explains_what_to_do(self):
        self.tiling.x_settings.amount_var.set('2')
        self.tiling.y_settings.amount_var.set('1')
        self.tiling.on_begin()
        self.assertEqual(self.d.chip.layers[-1].exposures, [])
        self.assertIn('Split pattern', self.dialogs[-1][1][1])


if __name__ == '__main__':
    unittest.main()
