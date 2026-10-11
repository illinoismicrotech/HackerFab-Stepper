"""Stage problems seen on real hardware: USB dropouts and moves outside the stage's range."""
import tempfile
import tkinter
import unittest
from pathlib import Path
from unittest.mock import patch

import support
from support import DialogsAnswered, RecordingStage, pump

import serial

from dispatcher import ConfigReader, Event, EventDispatcher, StageError, is_disconnect, read_stage_limits
from projector import TkProjector
from projector_window import Display
from stage_control.grbl_stage import GrblStage

MAIN = Display(1, 0, 0, 1600, 1000, primary=True)


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
        self.pending.append(b"<Idle|MPos:-1.000,2.500,0.250|FS:0,0>" if data == b"?" else b"ok")

    def read_until(self, _terminator):
        return (self.pending.pop(0) + b"\r\n") if self.pending else b""

    def open(self):
        self.is_open = True
        self.opens += 1

    def close(self):
        self.is_open = False


class UnpluggedStage(RecordingStage):
    """Fails exactly like the SKR Pico did in HackerfabStepper.log when its USB link dropped."""

    def __init__(self):
        super().__init__()
        self.plugged = False

    def move_by(self, amounts):
        if not self.plugged:
            raise serial.SerialException("WriteFile failed (PermissionError(13, 'The device does not "
                                         "recognize the command.', None, 22))")
        super().move_by(amounts)

    def reconnect(self):
        self.plugged = True
        self.pos = [-1000.0, 0.0, 0.0]


class GrblReconnectTests(unittest.TestCase):
    def test_reconnect_reopens_port_and_reads_the_position(self):
        port = ReopenableSerial()
        with patch("stage_control.grbl_stage.time.sleep"):
            stage = GrblStage(port, False, invert_z=True)
            port.close()
            with self.assertRaises(serial.SerialException):
                stage.is_idle()
            stage.reconnect()
        self.assertEqual(port.opens, 1)
        self.assertEqual(stage.position(), (-1000.0, 2500.0, -250.0))  # µm, Z reversed like moves

    def test_only_link_failures_count_as_disconnects(self):
        self.assertTrue(is_disconnect(serial.SerialException("WriteFile failed")))
        self.assertTrue(is_disconnect(PermissionError(13, "device")))
        self.assertFalse(is_disconnect(TimeoutError("slow reply")))
        self.assertFalse(is_disconnect(RuntimeError("GRBL error: error:9")))


class LimitConfigTests(unittest.TestCase):
    def test_limits_are_read_and_typos_reported(self):
        reader = ConfigReader({"stage": {"x-limits": [-15000, 0], "y-limits": [5, 1], "z-limits": "oops"}})
        self.assertEqual(read_stage_limits(reader), {"x": (-15000.0, 0.0)})
        self.assertEqual(len(reader.problems), 2)
        self.assertIn("[lowest, highest]", reader.problems[0])


class DispatcherStageTests(DialogsAnswered):
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

    def make(self, stage):
        d = EventDispatcher(stage, TkProjector(size=(320, 180)), self.root, None)
        d.snapshot_directory = Path(tempfile.mkdtemp())
        return d

    def test_usb_dropout_marks_the_stage_disconnected_until_reconnected(self):
        stage = UnpluggedStage()
        d = self.make(stage)
        changes = []
        d.add_event_listener(Event.STAGE_CONNECTION_CHANGED, lambda: changes.append(d.stage_connected))
        with self.assertRaises(StageError):
            d.move_relative({"x": 10})
        self.assertFalse(d.stage_connected)
        self.assertIn("disconnected", " ".join(map(str, self.dialogs[-1][1])))
        self.assertIn("disconnected", d.exposure_problem())
        # While disconnected nothing is sent to the controller, and no new dialog pops up per click.
        dialogs = len(self.dialogs)
        with self.assertRaises(StageError):
            d.move_relative({"x": 10})
        self.assertEqual(len(self.dialogs), dialogs)
        self.assertTrue(d.reconnect_stage())
        self.assertEqual(changes, [False, True])
        self.assertEqual(d.stage_setpoint, (-1000.0, 0.0, 0.0))   # position read again after reconnecting
        d.move_relative({"x": 10})
        self.assertEqual(stage.relative, [{"x": 10.0}])

    def test_moves_outside_the_configured_range_are_refused(self):
        stage = RecordingStage()
        d = self.make(stage)
        d.stage_limits = {"x": (-100.0, 100.0)}
        d.move_relative({"x": 60})
        with self.assertRaises(StageError):
            d.move_relative({"x": 60})          # would end at 120
        with self.assertRaises(StageError):
            d.move_absolute({"x": -150, "y": 0})
        d.move_absolute({"x": -90, "y": 5000})  # y has no limit
        self.assertEqual(stage.relative, [{"x": 60.0}])
        self.assertEqual(d.stage_setpoint[:2], (-90.0, 5000.0))
        self.assertIn("outside the allowed range", " ".join(map(str, self.dialogs[0][1])))


class StagePanelTests(DialogsAnswered):
    def test_reconnect_button_and_red_dot_while_disconnected(self):
        import gui
        root = support.new_root()
        with patch("gui.list_displays", return_value=[MAIN]), \
             patch("gui.LedController.connect", return_value=(None, "none")):
            stage = UnpluggedStage()
            lconf = gui.LithographerConfig(stage, None, 1.0, 4167, 25000,
                                           gui.AlignmentConfig(False, "", 1820, 280, 269, 1075, -1100, 800))
            app = gui.LithographerGui(lconf, root, {"camera": {"type": "none"}}, Path(tempfile.mkdtemp()) / "c.toml")
            try:
                pump(root, 0.2)
                panel = app.stage_panel
                self.assertFalse(panel.reconnect.winfo_manager())
                self.assertFalse(panel.move("x", 1))
                pump(root, 0.6)
                self.assertTrue(panel.reconnect.winfo_manager())
                self.assertEqual(app.dots["stage"].kind, "bad")
                self.assertIn("Disconnected", panel.state_label.cget("text"))
                panel.reconnect.invoke()
                pump(root, 0.6)
                self.assertFalse(panel.reconnect.winfo_manager())
                self.assertTrue(panel.move("x", 1))
            finally:
                app.cleanup()


if __name__ == "__main__":
    unittest.main()
