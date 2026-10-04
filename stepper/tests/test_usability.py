"""Tests for the second audit: stage handling, autofocus, snapshots, pattern loading, usability."""
import os
import tempfile
import tkinter
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tk_runtime import enable_font_support
enable_font_support()

import ttkbootstrap as ttk
from PIL import Image

import gui
from stage_control.grbl_stage import GrblStage, _HOMING_TIMEOUT, _SERIAL_TIMEOUT
from stage_control.stage_controller import StageController


# --------------------------------------------------------------------------- stage driver
class FakeSerial:
    """Minimal pyserial stand-in that answers GRBL commands from a script."""

    def __init__(self, state='Idle'):
        self.timeout = None
        self.is_open = True
        self.state = state
        self.pending = []
        self.timeouts_at_write = []

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, data):
        self.timeouts_at_write.append((data, self.timeout))
        if data == b'?':
            self.pending.append(f'<{self.state}|MPos:1.000,2.000,3.000|FS:0,0>'.encode())
        else:
            self.pending.append(b'ok')

    def read_until(self, _terminator):
        return (self.pending.pop(0) + b'\r\n') if self.pending else b''

    def close(self):
        self.is_open = False


class GrblStageTests(unittest.TestCase):
    def make(self, state='Idle', homing=False):
        with patch('stage_control.grbl_stage.time.sleep'):
            return GrblStage(FakeSerial(state), homing)

    def test_homing_waits_longer_than_normal_replies(self):
        stage = self.make(homing=True)
        stage.home()
        self.assertIn((b'$H\n', _HOMING_TIMEOUT), stage.controller_target.timeouts_at_write)
        self.assertEqual(stage.controller_target.timeout, _SERIAL_TIMEOUT)

    def test_wait_for_idle_gives_up(self):
        stage = self.make(state='Run')
        with patch('stage_control.grbl_stage.time.sleep'):
            self.assertFalse(stage.wait_for_idle(timeout=0.05))
        self.assertEqual(stage.state_name(), 'Run')

    def test_alarm_state_is_reported(self):
        stage = self.make(state='Alarm')
        self.assertFalse(stage.is_idle())
        self.assertEqual(stage.state_name(), 'Alarm')

    def test_alarm_explanation_mentions_unlocking(self):
        text = gui.describe_stage_error(RuntimeError('GRBL error: error:9'))
        self.assertIn('ALARM', text)
        self.assertIn('$X', text)


# --------------------------------------------------------------------------- pure helpers
class ImageHelperTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def test_transparent_white_becomes_black(self):
        image = Image.new('RGBA', (4, 1), (255, 255, 255, 0))  # "empty" pixels stored as white
        image.putpixel((0, 0), (255, 255, 255, 255))
        image.save(self.folder / 'shape.png')
        pixels = list(gui.load_pattern_image(self.folder / 'shape.png').getdata())
        self.assertEqual(pixels[0], (255, 255, 255))
        self.assertEqual(pixels[1:], [(0, 0, 0)] * 3)

    def test_sixteen_bit_is_scaled_not_clipped(self):
        values = np.array([[0, 32768, 65535]], dtype=np.uint16)
        Image.fromarray(values).save(self.folder / 'deep.png')
        grey = [p[0] for p in gui.load_pattern_image(self.folder / 'deep.png').getdata()]
        self.assertEqual(grey[0], 0)
        self.assertAlmostEqual(grey[1], 127, delta=1)
        self.assertEqual(grey[2], 255)

    def test_snapshot_writes_to_non_ascii_folder(self):
        target = self.folder / 'Überprüfung' / 'bilder' / 'snap'
        saved = gui.write_rgb_image(target, np.zeros((8, 8, 3), np.uint8))
        self.assertEqual(saved.suffix, '.png')
        self.assertTrue(saved.exists() and saved.stat().st_size > 0)

    def test_config_reader_reports_typos(self):
        reader = gui.ConfigReader({'camera': {'gui-scale': 'big'}, 'stage': {'homing': 'yes', 'invert-z': 'maybe'}})
        self.assertEqual(reader.number('camera', 'gui-scale', 0.25, positive=True), 0.25)
        self.assertTrue(reader.flag('stage', 'homing', False))
        self.assertTrue(reader.flag('stage', 'invert-z', True))
        self.assertEqual(len(reader.problems), 2)


# --------------------------------------------------------------------------- GUI
class FailingStage(StageController):
    def move_to(self, amounts):
        raise RuntimeError('GRBL error: error:9')

    def state_name(self):
        return 'Alarm'


class BusyStage(StageController):
    def is_idle(self):
        return False

    def state_name(self):
        return 'Run'


class HomingStage(StageController):
    def __init__(self):
        self.moves = []

    def has_homing(self):
        return True

    def move_to(self, amounts):
        self.moves.append(dict(amounts))


class FakeWebcam:
    made = 0

    def __init__(self, index='auto', settings=None):
        FakeWebcam.made += 1
        self.settings = dict(settings or {})
        self.state, self.status = 'error', 'No usable camera mode.'

    def setStreamCaptureCallback(self, callback):
        pass

    def open(self):
        return True

    def startStreamCapture(self):
        return True

    def close(self):
        return True


class UsabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ttkbootstrap.style
        ttkbootstrap.style.Style.instance = None
        try:
            cls.root = ttk.Window(themename='darkly')
        except tkinter.TclError as exc:
            raise unittest.SkipTest(f'Display unavailable: {exc}')
        config = gui.LithographerConfig(StageController(), None, .25, 4167, 25000,
                                        gui.AlignmentConfig(False, '', 1820, 280, 269, 1075, -1100, 800))
        cls.app = gui.LithographerGui(config, cls.root, {'camera': {'type': 'none'}},
                                      Path(tempfile.mkdtemp()) / 'config.toml')
        cls.d = cls.app.event_dispatcher
        cls.stage = cls.d.hardware.stage

    @classmethod
    def tearDownClass(cls):
        cls.app.cleanup()

    def run(self, result=None):
        self.dialogs = []
        def record(kind, answer):
            def fn(*args, **kwargs):
                self.dialogs.append((kind, args, kwargs))
                return answer
            return fn
        with patch('gui.messagebox.showinfo', record('showinfo', None)), \
             patch('gui.messagebox.showerror', record('showerror', None)), \
             patch('gui.messagebox.showwarning', record('showwarning', None)), \
             patch('gui.messagebox.askyesno', record('askyesno', False)), \
             patch('gui.messagebox.askokcancel', record('askokcancel', False)), \
             patch('gui.messagebox.askyesnocancel', record('askyesnocancel', None)):
            return super().run(result)

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.d.hardware.stage = self.stage
        self.d.stage_setpoint = (0.0, 0.0, 0.0)
        self.d._last_stage_error_at = 0.0
        self.app.chip_frame.path.set('')
        self.d.new_chip()
        self.root.update()

    def _pattern(self):
        path = self.folder / 'pattern.png'
        Image.new('RGB', (64, 64), 'white').save(path)
        self.d.set_pattern_image(Image.open(path).convert('RGB'), str(path))
        self.d.exposure_time = 30

    # -- stage -----------------------------------------------------------------
    def test_failed_move_is_explained_and_position_kept(self):
        self.d.hardware.stage = FailingStage()
        with self.assertRaises(gui.StageError):
            self.d.move_relative({'x': 100})
        self.assertEqual(self.d.stage_setpoint, (0.0, 0.0, 0.0))
        self.assertEqual(self.dialogs[0][0], 'showerror')
        self.assertIn('ALARM', self.dialogs[0][1][1])

    def test_no_exposure_while_stage_is_moving(self):
        self._pattern()
        self.d.hardware.stage = BusyStage()
        self.d.stage_idle_timeout = 0.2
        try:
            self.d.begin_patterning()
        finally:
            self.d.stage_idle_timeout = 20.0
        self.assertEqual(self.d.chip.layers[-1].exposures, [])  # nothing was exposed or logged
        self.assertIn("'Run'", self.dialogs[-1][1][1])
        self.assertFalse(self.d.patterning_busy)

    def test_shortcut_asks_before_moving_all_axes(self):
        stage = HomingStage()
        self.d.hardware.stage = stage
        self.d.on_event(gui.Event.MOVEMENT_LOCK_CHANGED)
        button = self.app.mode_select_frame.red_mode_frame.stage_position_frame.shortcuts[0]
        button.invoke()
        self.assertEqual(stage.moves, [])  # default answer: Cancel
        with patch('gui.messagebox.askokcancel', return_value=True):
            button.invoke()
        self.assertEqual(len(stage.moves), 1)

    # -- autofocus -------------------------------------------------------------
    def _autofocus_to(self, peak, interactive=False):
        self.d.camera, self.d.camera_image = object(), np.zeros((4, 4, 3), np.uint8)
        curve = lambda _image, blue_only=False: -(self.d.stage_setpoint[2] - peak) ** 2
        try:
            with patch('gui.compute_focus_score', side_effect=curve), \
                 patch.object(self.d, 'non_blocking_delay', lambda _t: None):
                self.d.autofocus(blue_only=False, interactive=interactive)
        finally:
            self.d.camera = self.d.camera_image = None
        return self.d.stage_setpoint[2]

    def test_autofocus_reaches_focus_in_either_direction(self):
        for peak in (-37.0, 43.0, 3.0, -9.0):
            self.d.stage_setpoint = (0.0, 0.0, 0.0)
            self.assertLessEqual(abs(self._autofocus_to(peak) - peak), 1.0, f'peak at {peak}')

    def test_autofocus_explains_when_it_cannot_run(self):
        self.d.camera = self.d.camera_image = None
        self.d.autofocus(blue_only=False, interactive=True)
        self.assertIn('live camera', self.dialogs[-1][1][1])

    # -- snapshots, checklist, defaults ------------------------------------------
    def test_snapshot_goes_to_snapshot_folder_with_safe_name(self):
        frame = self.app.camera.snapshot
        frame.name_var.set('run 1: best?.png')
        name = Path(frame._next_filename())
        self.assertEqual(name.parent, self.d.snapshot_directory)
        self.assertEqual(name.name, 'run 1- best-.png')

    def test_red_mode_shows_solid_red_at_launch(self):
        self.assertEqual(self.d.red_focus_source, gui.RedFocusSource.SOLID)
        red = self.app.mode_select_frame.red_mode_frame
        self.assertEqual(red.red_select_var.get(), gui.RedFocusSource.SOLID.value)

    def test_expose_checklist_tracks_readiness(self):
        checklist = self.app.mode_select_frame.uv_mode_frame.checklist
        self.d.pattern_image_path, self.d.exposure_time = '', None
        checklist.refresh()
        texts = [label.cget('text') for label in checklist.lines]
        self.assertTrue(texts[0].startswith('✗') and texts[1].startswith('✗'))
        self._pattern()
        self.d.exposure_time = 8000
        checklist.refresh()
        texts = [label.cget('text') for label in checklist.lines]
        self.assertTrue(texts[0].startswith('✓ Pattern') and '8.00 s' in texts[1])

    # -- closing, saving, help ----------------------------------------------------------
    def test_close_offers_to_save_unsaved_exposures(self):
        self._pattern()
        self.d.begin_patterning()
        with patch.object(self.app, 'cleanup') as cleanup:
            self.app.request_close()  # default answer: Cancel
            cleanup.assert_not_called()
            target = self.folder / 'kept.json'
            with patch('gui.messagebox.askyesnocancel', return_value=True), \
                 patch('gui.filedialog.asksaveasfilename', return_value=str(target)):
                self.app.request_close()
            cleanup.assert_called_once()
        self.assertTrue(target.exists())

    def test_close_during_exposure_stops_it_first(self):
        self.d.patterning_busy = True
        with patch.object(self.root, 'after') as later:
            self.app.request_close()
        self.assertTrue(self.d.should_abort)
        later.assert_called_once()
        self.d.patterning_busy, self.d.should_abort = False, False

    def test_help_opens_guide(self):
        cwd = os.getcwd()
        os.chdir(self.folder)
        try:
            (self.folder / gui.GUIDE_NAME).write_text('<html></html>')
            with patch('gui.webbrowser.open') as browser:
                gui.open_guide()
            browser.assert_called_once()
            self.assertTrue(browser.call_args[0][0].startswith('file:'))
            os.remove(self.folder / gui.GUIDE_NAME)
            gui.open_guide()
            self.assertEqual(self.dialogs[-1][0], 'showinfo')
        finally:
            os.chdir(cwd)

    def test_reconnect_button_appears_and_retries(self):
        frame = self.app.camera
        with patch('gui.Webcam', FakeWebcam):
            frame.replace(FakeWebcam(settings={'device': '1'}))
            self.root.update()
            frame._on_new_frame()
            self.root.update()
            self.assertTrue(frame.reconnect_button.winfo_ismapped())
            before = FakeWebcam.made
            frame.reconnect_button.invoke()
            self.assertEqual(FakeWebcam.made, before + 1)
            self.assertEqual(frame.camera.settings, {'device': '1'})
            frame.replace(None)
            frame._on_new_frame()
            self.root.update()
            self.assertFalse(frame.reconnect_button.winfo_ismapped())


if __name__ == '__main__':
    unittest.main()
