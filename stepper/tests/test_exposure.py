"""Tests for the camera exposure setting (DirectShow units) used to remove projector banding."""
import queue
import threading
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from camera import webcam
from camera.discovery import CameraDevice
from camera.webcam import Webcam, _apply_exposure, capture_backend, manual_exposure


class FakeCap:
    def __init__(self, *args, accept=True):
        self.args, self.props, self.accept = args, {}, accept
        self.sets = []

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.sets.append((prop, value))
        if self.accept or prop != cv2.CAP_PROP_EXPOSURE:
            self.props[prop] = value
        return True

    def get(self, prop):
        return self.props.get(prop, -11 if prop == cv2.CAP_PROP_EXPOSURE else 30)

    def read(self):
        frame = np.full((48, 64, 3), 120, np.uint8)
        frame[::2] = 60  # some detail, not green
        return True, frame

    def release(self):
        pass


class ExposureSettingTests(unittest.TestCase):
    def test_parse(self):
        self.assertIsNone(manual_exposure({}))
        self.assertIsNone(manual_exposure({'exposure': 'auto'}))
        self.assertIsNone(manual_exposure({'exposure': 'bright'}))
        self.assertEqual(manual_exposure({'exposure': -5}), -5)
        self.assertEqual(manual_exposure({'exposure': '-4'}), -4)
        self.assertEqual(manual_exposure({'exposure': -40}), -13)

    def test_windows_uses_directshow_when_exposure_is_set(self):
        with patch('camera.webcam.platform.system', return_value='Windows'):
            self.assertEqual(capture_backend({'exposure': -5}), cv2.CAP_DSHOW)
            self.assertEqual(capture_backend({'exposure': 'auto'}), cv2.CAP_MSMF)
            self.assertEqual(capture_backend({'exposure': -5, 'backend': 'msmf'}), cv2.CAP_MSMF)
        with patch('camera.webcam.platform.system', return_value='Linux'):
            self.assertEqual(capture_backend({'exposure': -5}), cv2.CAP_V4L2)

    def test_directshow_switches_to_manual_then_sets_value(self):
        cap = FakeCap()
        self.assertEqual(_apply_exposure(cap, cv2.CAP_DSHOW, -5), -5)
        self.assertEqual(cap.sets, [(cv2.CAP_PROP_AUTO_EXPOSURE, 0), (cv2.CAP_PROP_EXPOSURE, -5)])
        self.assertIsNone(_apply_exposure(FakeCap(), cv2.CAP_DSHOW, None))

    def test_v4l2_converts_to_100_microsecond_units(self):
        cap = FakeCap()
        _apply_exposure(cap, cv2.CAP_V4L2, -5)
        self.assertIn((cv2.CAP_PROP_EXPOSURE, 312), cap.sets)  # 31.25 ms

    def test_worker_reports_exposure_and_uses_directshow_names(self):
        channel, stop, made = queue.Queue(), threading.Event(), []
        def make(*args):
            made.append(FakeCap(*args))
            return made[-1]
        settings = {'device': 'auto', 'exposure': -4, 'mode': 'manual', 'width': 64, 'height': 48, 'fps': 30, 'fourcc': 'YUYV'}
        with patch('camera.webcam.platform.system', return_value='Windows'), \
             patch('camera.webcam.discover_devices', return_value=[CameraDevice('1', 'Arducam')]) as found, \
             patch('camera.webcam.cv2.VideoCapture', side_effect=make):
            threading.Timer(0.5, stop.set).start()
            webcam._capture(settings, channel, stop)
        self.assertEqual(found.call_args.kwargs.get('api'), 'dshow')
        self.assertEqual(made[0].args[1], cv2.CAP_DSHOW)
        messages = []
        while not channel.empty():
            messages.append(channel.get())
        ready = [v for k, v in messages if k == 'ready']
        self.assertTrue(ready)
        self.assertEqual((ready[0]['exposure'], ready[0]['exposure_readback']), (-4, -4))

    def _status_for(self, ready):
        cam = Webcam(settings={})
        cam.channel = queue.Queue()
        cam.channel.put(('ready', ready))
        class Alive:
            def is_alive(self): return True
            exitcode = None
        cam.process = Alive()
        worker = threading.Thread(target=cam._monitor, daemon=True)
        worker.start()
        time.sleep(0.4)
        cam.should_stop.set()
        worker.join(2)
        return cam.status

    def test_status_says_whether_exposure_was_applied(self):
        base = {'width': 1920, 'height': 1080, 'fps': 30.0, 'fourcc': 'YUY2', 'device': '1'}
        self.assertIn('exposure -5 (31.25 ms)', self._status_for({**base, 'exposure': -5, 'exposure_readback': -5.0}))
        self.assertIn('NOT applied', self._status_for({**base, 'exposure': -5, 'exposure_readback': -11.0}))
        self.assertNotIn('exposure', self._status_for({**base, 'exposure': None, 'exposure_readback': None}))


if __name__ == '__main__':
    unittest.main()
