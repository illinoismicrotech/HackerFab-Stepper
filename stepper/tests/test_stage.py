import support  # noqa: F401  (puts src on the path)
import unittest
from unittest.mock import Mock, patch

from stage_control.grbl_stage import GrblStage


class StageDirectionTests(unittest.TestCase):
    def stage(self, invert_z):
        serial = Mock()
        serial.read_until.return_value = b'<Idle|MPos:1.000,2.000,-3.000>\n'
        with patch('stage_control.grbl_stage.time.sleep'):
            stage = GrblStage(serial, False, invert_z=invert_z)
        serial.write.reset_mock()
        serial.read_until.return_value = b'ok\n'
        return stage, serial

    def test_reverses_absolute_and_relative_z_only(self):
        stage, serial = self.stage(True)
        stage.move_to({'x': 1000, 'y': -2000, 'z': 3000})
        self.assertEqual([call.args[0] for call in serial.write.call_args_list], [b'G90\n', b'G53 G0 X1.0000 Y-2.0000 Z-3.0000\n'])
        serial.write.reset_mock()
        stage.move_by({'z': -250})
        self.assertEqual([call.args[0] for call in serial.write.call_args_list], [b'G91\n', b'G0 Z0.2500\n'])

    def test_readback_matches_app_coordinates(self):
        stage, serial = self.stage(True)
        serial.read_until.return_value = b'<Idle|MPos:1.000,2.000,-3.000>\n'
        self.assertEqual(stage._query_state(), (True, (1., 2., 3.)))

    def test_can_disable_reversal(self):
        stage, serial = self.stage(False)
        stage.move_to({'z': 3000})
        self.assertEqual(serial.write.call_args.args[0], b'G53 G0 Z3.0000\n')
        serial.read_until.return_value = b'<Idle|MPos:1.000,2.000,-3.000>\n'
        self.assertEqual(stage._query_state(), (True, (1., 2., -3.)))

if __name__ == '__main__':
    unittest.main()


class GrblProtocolTests(unittest.TestCase):
    def make(self, replies):
        serial = Mock()
        serial.read_until.side_effect = list(replies)
        with patch('stage_control.grbl_stage.time.sleep'):
            return GrblStage(serial, True, invert_z=False), serial

    def test_work_coordinates_are_used_when_reported(self):
        stage, serial = self.make([b'<Idle|WPos:1.000,2.000,3.000>\n', b'ok\n', b'ok\n'])
        self.assertEqual(stage.frame, "work")
        serial.write.reset_mock()
        stage.move_to({'x': 500})
        self.assertEqual([c.args[0] for c in serial.write.call_args_list], [b'G90\n', b'G0 X0.5000\n'])

    def test_position_in_microns(self):
        stage, serial = self.make([b'<Idle|MPos:1.000,2.000,3.000>\n', b'<Idle|MPos:-1.5,0.25,0>\n'])
        self.assertEqual(stage.position(), (-1500.0, 250.0, 0.0))

    def test_silent_port_is_refused(self):
        serial = Mock()
        serial.read_until.return_value = b''
        with patch('stage_control.grbl_stage.time.sleep'), self.assertRaises(ConnectionError):
            GrblStage(serial, False)
        serial.close.assert_called_once()

    def test_homing_waits_for_the_cycle_and_eats_the_late_ok(self):
        stage, serial = self.make([b'<Idle|MPos:0,0,0>\n',
                                   b'<Home|MPos:0,0,0>\n',            # poll 1: homing
                                   b'ok\n', b'<Idle|MPos:-1,-2,-3>\n'])  # poll 2: done, 'ok' arrived first
        stage.begin_home()
        self.assertIn(b'$H\n', [c.args[0] for c in serial.write.call_args_list])
        self.assertFalse(stage.homing_finished())
        with patch('stage_control.grbl_stage.time.sleep'):
            self.assertTrue(stage.homing_finished())
        self.assertEqual(stage.last_position, (-1.0, -2.0, -3.0))

    def test_homing_from_a_startup_alarm_with_grbl_1_1(self):
        # GRBL 1.1 boots in Alarm when homing is enabled and does not answer '?' during $H:
        # silence (timeouts) until the cycle ends, then 'ok' and Idle.
        stage, serial = self.make([b'<Alarm|MPos:0,0,0>\n',
                                   b'',                                   # poll 1: no answer while homing
                                   b'',                                   # poll 2: still homing
                                   b'ok\n', b'<Idle|MPos:-1,-1,-1>\n'])  # poll 3: done
        self.assertEqual(stage.state_name(), "Alarm")
        stage.begin_home()
        self.assertFalse(stage.homing_finished())
        self.assertFalse(stage.homing_finished())
        self.assertTrue(stage.homing_finished())
        self.assertEqual(stage.state_name(), "Idle")

    def test_blank_lines_are_not_timeouts(self):
        stage, serial = self.make([b'<Idle|MPos:0,0,0>\n', b'\r\n', b'[MSG:hello]\n', b'ok\n', b'ok\n'])
        stage.move_by({'x': 10})   # G91 gets 'ok' after a blank line and a message; G0 gets 'ok'

    def test_homing_alarm_is_an_error(self):
        stage, serial = self.make([b'<Idle|MPos:0,0,0>\n', b'ALARM:9\n', b'<Alarm|MPos:0,0,0>\n'])
        stage.begin_home()
        with self.assertRaises(RuntimeError):
            stage.homing_finished()
