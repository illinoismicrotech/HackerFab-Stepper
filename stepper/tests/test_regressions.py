"""Regression tests for bugs found while auditing the Windows exe. Needs a display for the GUI cases."""
import json
import os
import sys
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
from camera import discovery
from stage_control.stage_controller import StageController


class GuiRegressionTests(unittest.TestCase):
    # ttkbootstrap allows one main window per process, so the tests share one app.
    @classmethod
    def setUpClass(cls):
        # Another test module may already have created and destroyed ttkbootstrap's single
        # window; forget its stale Style so a new window can be made in this process.
        import ttkbootstrap.style
        ttkbootstrap.style.Style.instance = None
        try:
            cls.root = ttk.Window(themename='darkly')
        except tkinter.TclError as exc:
            raise unittest.SkipTest(f'Display unavailable: {exc}')
        cls.errors = []
        cls.root.report_callback_exception = lambda *args: cls.errors.append(args[1])
        config = gui.LithographerConfig(StageController(), None, .25, 4167, 25000,
                                        gui.AlignmentConfig(False, '', 1820, 280, 269, 1075, -1100, 800))
        cls.app = gui.LithographerGui(config, cls.root, {'camera': {'type': 'none'}},
                                      Path(tempfile.mkdtemp()) / 'config.toml')
        cls.d = cls.app.event_dispatcher

    def run(self, result=None):
        # Every dialog gets a safe automatic answer so a new prompt can never hang the suite.
        # Individual tests patch specific dialogs again when they need a different answer.
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

    @classmethod
    def tearDownClass(cls):
        cls.app.cleanup()

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.errors.clear()
        self.app.chip_frame.path.set('')
        self.d.new_chip()
        self.d.exposure_history.clear()
        self.root.update()

    def _pattern(self, name='pattern.png'):
        path = self.folder / name
        Image.new('RGB', (64, 64), 'white').save(path)
        self.d.set_pattern_image(Image.open(path).convert('RGB'), str(path))
        self.d.exposure_time = 30
        return path

    def test_missing_pattern_file_does_not_lock_the_app(self):
        os.remove(self._pattern())  # e.g. chosen from a USB stick that was then unplugged
        self.d.begin_patterning()
        self.assertFalse(self.d.patterning_busy)
        self.assertEqual(self.d.movement_lock, gui.MovementLock.UNLOCKED)
        self.assertEqual(self.d.shown_image, gui.ShownImage.CLEAR)
        self.assertEqual(len(self.d.exposure_history), 1)

    def test_chip_from_another_pc_opens(self):
        chip = self.folder / 'chip.json'
        chip.write_text(json.dumps({'layers': [{'exposures': [
            {'time': '2026-09-01T10:00:00', 'path': 'D:/patterns/not-here.png',
             'coords': [1.0, 2.0, 3.0], 'duration': 8000, 'aborted': False}]}]}))
        self.d.load_chip(str(chip))
        self.assertEqual(len(self.d.chip.layers[-1].exposures), 1)

    def test_cancelled_chip_dialogs_keep_the_record(self):
        self._pattern()
        self.d.begin_patterning()
        frame = self.app.chip_frame
        with patch('gui.filedialog.asksaveasfilename', return_value=''):
            frame.new_chip_button.invoke()
        with patch('gui.filedialog.askopenfilename', return_value=''):
            frame.open_chip_button.invoke()
        self.root.update()
        self.assertEqual(len(self.d.chip.layers[-1].exposures), 1)
        self.assertEqual(self.errors, [])

    def test_failed_open_keeps_the_current_chip_file(self):
        frame = self.app.chip_frame
        frame.path.set(str(self.folder / 'mine.json'))
        broken = self.folder / 'broken.json'
        broken.write_text('not json')
        with patch('gui.filedialog.askopenfilename', return_value=str(broken)):
            frame.open_chip_button.invoke()
        self.assertEqual([k for k, *_ in self.dialogs], ['showerror'])
        self.assertEqual(frame.path.get(), str(self.folder / 'mine.json'))

    def test_finish_layer_without_a_chip_file(self):
        self._pattern()
        self.d.begin_patterning()
        self.app.chip_frame.finish_layer_button.invoke()
        self.root.update()
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.d.chip.layers), 2)

    def test_fullscreen_opens_when_focus_is_in_a_dropdown(self):
        # Tkinter raises KeyError('popdown') from focus_get() when focus is in a combobox list.
        with patch.object(self.root, 'focus_get', side_effect=KeyError('popdown')):
            self.app.fullscreen.open('camera', lambda: None)
        self.assertEqual(self.app.fullscreen.kind, 'camera')
        self.app.fullscreen._escape()
        self.assertIsNone(self.app.fullscreen.kind)

    def test_failure_while_showing_pattern_clears_projector_and_unlocks(self):
        self._pattern()
        projector = self.d.hardware.projector
        original = projector.on_show
        projector.on_show = lambda: (_ for _ in ()).throw(RuntimeError('display failed'))
        try:
            with self.assertRaises(RuntimeError):
                self.d.begin_patterning()
        finally:
            projector.on_show = original
        self.assertFalse(self.d.patterning_busy)
        self.assertEqual(self.d.shown_image, gui.ShownImage.CLEAR)

    def test_red_focus_radio_matches_active_source(self):
        red = self.app.mode_select_frame.red_mode_frame
        self.assertEqual(red.red_select_var.get(), self.d.red_focus_source.value)


class ExeStartupTests(unittest.TestCase):
    def test_read_only_exe_folder_falls_back_to_local_app_data(self):
        import stepper_app
        local = Path(tempfile.mkdtemp())
        exe_dir = Path(tempfile.mkdtemp())
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'platform', 'win32'), \
             patch.object(sys, 'executable', str(exe_dir / 'HackerfabStepper.exe')), \
             patch.dict(os.environ, {'LOCALAPPDATA': str(local)}), \
             patch('stepper_app._writable', side_effect=lambda folder: folder != exe_dir):
            os.environ.pop('HACKERFAB_HOME', None)
            self.assertEqual(stepper_app._home(), local / 'HackerfabStepper')

    def test_mac_app_keeps_files_in_documents_not_inside_the_bundle(self):
        import stepper_app
        user = Path(tempfile.mkdtemp())
        bundle = user / 'HackerfabStepper.app' / 'Contents' / 'MacOS'
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'platform', 'darwin'), \
             patch.object(sys, 'executable', str(bundle / 'HackerfabStepper')), \
             patch('stepper_app.Path.home', return_value=user), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HACKERFAB_HOME', None)
            self.assertEqual(stepper_app._home(), user / 'Documents' / 'HackerfabStepper')

    def test_home_folder_can_be_chosen_with_an_environment_variable(self):
        import stepper_app
        chosen = Path(tempfile.mkdtemp()) / 'station-1'
        with patch.dict(os.environ, {'HACKERFAB_HOME': str(chosen)}):
            self.assertEqual(stepper_app._home(), chosen.resolve())
        self.assertTrue(chosen.is_dir())

    def test_update_refreshes_guide_but_keeps_user_settings(self):
        import stepper_app
        bundle, home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (bundle / 'default.toml').write_text('new defaults')
        (bundle / 'HOW-TO-GUIDE.html').write_text('new guide')
        (home / 'default.toml').write_text('my edits')
        (home / 'HOW-TO-GUIDE.html').write_text('old guide')
        cwd = os.getcwd()
        try:
            with patch.object(sys, '_MEIPASS', str(bundle), create=True):
                stepper_app._prepare_working_directory(home)
        finally:
            os.chdir(cwd)
        self.assertEqual((home / 'default.toml').read_text(), 'my edits')
        self.assertEqual((home / 'HOW-TO-GUIDE.html').read_text(), 'new guide')

    def test_msmf_hardware_transforms_disabled_before_opening_cameras(self):
        import camera.webcam  # noqa: F401
        self.assertEqual(os.environ.get('OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS'), '0')


class CameraOrderTests(unittest.TestCase):
    def test_arducam_first_and_laptop_camera_last(self):
        names = ['Integrated Camera', 'USB Video Device', 'Arducam USB Camera']
        with patch('camera.discovery.platform.system', return_value='Windows'), \
             patch('camera.discovery._msmf_device_names', return_value=names):
            order = [d.name for d in discovery.discover_devices()]
        self.assertEqual(order, ['Arducam USB Camera', 'USB Video Device', 'Integrated Camera'])


if __name__ == '__main__':
    unittest.main()
