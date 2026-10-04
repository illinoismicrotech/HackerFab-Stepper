"""Stage problems seen on real hardware: USB dropouts, unsynced start position, runaway moves."""
import contextlib
import io
import tempfile
import tkinter
import unittest
from pathlib import Path
from unittest.mock import patch

import serial

from tk_runtime import enable_font_support
enable_font_support()

import ttkbootstrap as ttk

import gui
from stage_control.grbl_stage import GrblStage
from stage_control.stage_controller import StageController


class ReopenableSerial:
    """pyserial stand-in that can be closed and reopened, reporting a fixed position."""

    def __init__(self):
        self.timeout = None
        self.is_open = True
        self.pending = []
        self.opens = 0

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, data):
        if not self.is_open:
            raise serial.SerialException("port is closed")
        self.pending.append(b'<Idle|MPos:-1.000,2.500,0.250|FS:0,0>' if data == b'?' else b'ok')

    def read_until(self, _terminator):
        return (self.pending.pop(0) + b'\r\n') if self.pending else b''

    def open(self):
        self.is_open = True
        self.opens += 1

    def close(self):
        self.is_open = False


class UnpluggedStage(StageController):
    """Fails exactly like the SKR Pico did in HackerfabStepper.log when its USB link dropped."""

    def __init__(self):
        self.moves = []
        self.reconnected = False

    def move_to(self, amounts):
        if not self.reconnected:
            raise serial.SerialException("WriteFile failed (PermissionError(13, 'The device does not "
                                         "recognize the command.', None, 22))")
        self.moves.append(amounts)

    def reconnect(self):
        self.reconnected = True

    def position_um(self):
        return (-1000.0, 0.0, 0.0)


class RecordingStage(StageController):
    def __init__(self):
        self.moves = []

    def move_to(self, amounts):
        self.moves.append(amounts)


class GrblReconnectTests(unittest.TestCase):
    def test_reconnect_reopens_port_and_reads_position_in_microns(self):
        port = ReopenableSerial()
        with patch('stage_control.grbl_stage.time.sleep'):
            stage = GrblStage(port, False, invert_z=True)
            port.close()
            stage.reconnect()
        self.assertEqual(port.opens, 1)
        self.assertEqual(stage.position_um(), (-1000.0, 2500.0, -250.0))  # Z reversed like moves

    def test_only_link_failures_count_as_disconnects(self):
        self.assertTrue(gui.is_disconnect(serial.SerialException("WriteFile failed")))
        self.assertTrue(gui.is_disconnect(PermissionError(13, "device")))
        self.assertFalse(gui.is_disconnect(TimeoutError("slow reply")))
        self.assertFalse(gui.is_disconnect(RuntimeError("GRBL error: error:9")))


class StageConnectionGuiTests(unittest.TestCase):
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

    @classmethod
    def tearDownClass(cls):
        cls.app.cleanup()

    def run(self, result=None):
        self.dialogs = []
        self.confirm = False
        def record(kind):
            def fn(*args, **kwargs):
                self.dialogs.append((kind, args))
                return self.confirm if kind == 'askokcancel' else None
            return fn
        with patch('gui.messagebox.showinfo', record('showinfo')), \
             patch('gui.messagebox.showerror', record('showerror')), \
             patch('gui.messagebox.askokcancel', record('askokcancel')):
            return super().run(result)

    def setUp(self):
        self.d.stage_setpoint = (0.0, 0.0, 0.0)
        self.d._last_stage_error_at = 0.0
        self.d.stage_limits = {}
        self.d.set_stage_connected(True)
        self.root.update()

    def jog_button(self, text):
        frame = self.app.mode_select_frame.red_mode_frame.stage_position_frame
        return next(b for b in frame.xy_widgets if isinstance(b, ttk.Button) and b.cget('text') == text)

    def test_usb_dropout_marks_stage_disconnected_and_stops_retrying(self):
        stage = UnpluggedStage()
        self.d.hardware.stage = stage
        with self.assertRaises(gui.StageError):
            self.d.move_relative({'x': 250})
        self.assertFalse(self.d.stage_connected)
        self.assertIn('Reconnect stage', self.dialogs[0][1][1])
        self.assertEqual(self.d.stage_setpoint, (0.0, 0.0, 0.0))
        # Further clicks are refused without touching the dead port, and without a dialog storm.
        for _ in range(5):
            with self.assertRaises(gui.StageError):
                self.d.move_relative({'x': 250})
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(str(self.jog_button('X+').cget('state')), 'disabled')

    def test_reconnect_restores_moves_and_syncs_position(self):
        stage = UnpluggedStage()
        self.d.hardware.stage = stage
        with self.assertRaises(gui.StageError):
            self.d.move_relative({'x': 250})
        self.root.update()
        self.assertEqual(self.app.reconnect_stage_button.winfo_manager(), 'pack')
        self.app.reconnect_stage_button.invoke()
        self.root.update()
        self.assertTrue(self.d.stage_connected)
        self.assertEqual(self.d.stage_setpoint, (-1000.0, 0.0, 0.0))
        self.assertEqual(self.app.reconnect_stage_button.winfo_manager(), '')
        self.d.move_relative({'x': 250})
        self.assertEqual(stage.moves, [{'x': -750.0, 'y': 0.0, 'z': 0.0}])

    def test_stage_errors_from_buttons_do_not_print_tracebacks(self):
        self.d.hardware.stage = UnpluggedStage()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.jog_button('X+').invoke()
            self.root.update()
        self.assertNotIn('Traceback', stderr.getvalue())
        self.assertEqual(self.dialogs[0][0], 'showerror')

    def test_moves_outside_limits_are_refused(self):
        stage = RecordingStage()
        self.d.hardware.stage = stage
        self.d.stage_limits = {'y': (-15000.0, 0.0)}
        with self.assertRaises(gui.StageError):
            self.d.move_absolute({'x': 0.0, 'y': 100000.0, 'z': 0.0})
        self.assertEqual(stage.moves, [])
        self.assertIn('outside the allowed range', self.dialogs[0][1][1])
        self.d.move_absolute({'x': 0.0, 'y': -500.0, 'z': 0.0})
        self.assertEqual(len(stage.moves), 1)

    def test_large_jog_asks_first(self):
        stage = RecordingStage()
        self.d.hardware.stage = stage
        frame = self.app.mode_select_frame.red_mode_frame.stage_position_frame
        frame.step_size.set('5000')
        try:
            self.jog_button('X+').invoke()  # answer: Cancel
            self.assertEqual(stage.moves, [])
            self.confirm = True
            self.jog_button('X+').invoke()
            self.assertEqual(len(stage.moves), 1)
        finally:
            frame.step_size.set('10')

    def test_projection_surroundings_are_pure_black(self):
        # The theme paints plain Tk widgets grey; on the DLP that grey would expose resist.
        from projector_window import Display
        from PIL import Image
        self.app.fullscreen.open('projector', lambda: Image.new('RGB', (1280, 720), 'red'))
        try:
            for widget in (self.app.fullscreen.window, self.app.fullscreen.frame, self.app.fullscreen.label):
                self.assertEqual(widget.cget('background'), 'black')
        finally:
            self.app.fullscreen.close()
        window = self.app.projector_window
        window.open(Display(2, 0, 0, 800, 600, False), Image.new('RGB', (1280, 720), 'red'))
        try:
            self.root.update()
            self.assertEqual(window.label.cget('background'), 'black')
            self.assertEqual(window.window.cget('background'), 'black')
        finally:
            window.close()

    def test_limits_are_read_from_config(self):
        reader = gui.ConfigReader({'stage': {'x-limits': [-15000, 0], 'y-limits': 'big'}})
        self.assertEqual(reader.limits('stage', 'x-limits'), (-15000.0, 0.0))
        self.assertIsNone(reader.limits('stage', 'y-limits'))
        self.assertIsNone(reader.limits('stage', 'z-limits'))
        self.assertEqual(len(reader.problems), 1)


if __name__ == '__main__':
    unittest.main()
