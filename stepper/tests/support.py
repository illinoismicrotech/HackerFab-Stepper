"""Shared test helpers. Importing this puts stepper/src on the import path.

Run all tests from the stepper folder:   python -m unittest discover -s tests
"""
import pathlib
import sys
import tkinter
import unittest
from unittest.mock import patch

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tk_runtime import enable_font_support  # noqa: E402

enable_font_support()

import numpy as np  # noqa: E402

from stage_control.stage_controller import StageController  # noqa: E402


def new_root():
    """A fresh ttkbootstrap main window (ttkbootstrap allows one per process at a time)."""
    import ttkbootstrap as ttk
    import ttkbootstrap.style
    ttkbootstrap.style.Style.instance = None
    try:
        root = ttk.Window(themename="darkly")
    except tkinter.TclError as exc:
        raise unittest.SkipTest(f"Display unavailable: {exc}")
    return root


class DialogsAnswered(unittest.TestCase):
    """Every message box gets a safe automatic answer and is recorded in self.dialogs."""

    answers = {"showinfo": None, "showerror": None, "showwarning": None, "askyesno": False,
               "askokcancel": False, "askyesnocancel": None}

    def run(self, result=None):
        self.dialogs = []

        def record(kind, answer):
            def fn(*args, **kwargs):
                self.dialogs.append((kind, args, kwargs))
                return answer
            return fn
        patches = [patch(f"tkinter.messagebox.{kind}", record(kind, answer)) for kind, answer in self.answers.items()]
        for p in patches:
            p.start()
        try:
            return super().run(result)
        finally:
            for p in patches:
                p.stop()

    def dialog_kinds(self):
        return [kind for kind, *_ in self.dialogs]


class RecordingStage(StageController):
    """Simulated stage that records moves and can pretend to be busy or to have homing."""

    def __init__(self, homing=False):
        self.moves = []          # the absolute target of every move, axes that moved only
        self.relative = []       # relative moves as sent
        self.idle = True
        self.homing = homing
        self.pos = [0.0, 0.0, 0.0]

    def move_to(self, amounts):
        for i, axis in enumerate("xyz"):
            if axis in amounts:
                self.pos[i] = float(amounts[axis])
        self.moves.append(dict(amounts))

    def move_by(self, amounts):
        self.relative.append(dict(amounts))
        target = {}
        for i, axis in enumerate("xyz"):
            if axis in amounts:
                self.pos[i] += float(amounts[axis])
                target[axis] = self.pos[i]
        self.moves.append(target)

    def position(self):
        return tuple(self.pos)

    def is_idle(self):
        return self.idle

    def state_name(self):
        return "Idle" if self.idle else "Run"

    def has_homing(self):
        return self.homing


class FakeLeds:
    """Stands in for projector_leds.LedController."""

    def __init__(self, mask=7, fail=False):
        self.mask = mask
        self.original_mask = mask
        self.fail = fail
        self.calls = []
        self.closed_with = "open"

    def set_mask(self, mask):
        if mask == self.mask and not self.fail:
            return True  # like the real controller: no USB traffic when nothing changes
        self.calls.append(mask)
        if self.fail:
            self.mask = None
            return False
        self.mask = mask
        return True

    def close(self, final_mask=0):
        self.closed_with = final_mask


class FakeCamera:
    """A Webcam-like camera driven by the test: push() delivers one frame."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.state, self.status = "streaming", "Connected · 600 × 400 · 9 fps · Test · camera 0"
        self.controls = {"exposure": -4, "gain": 100}
        self.ranges = {"exposure": {"min": -11, "max": -3}, "gain": {"min": 100, "max": 2239}}
        self.modes = []
        self.latest_info = {}
        self.diagnostics = []
        self.measured_fps = 9.0
        self.set_calls = []
        self.snapshots = []
        self.processing = []
        self.callback = None
        self.closed = False
        self.snapshot_listener = None
        self.exposure_problem = ""

    def set_controls(self, values):
        self.set_calls.append(dict(values))
        self.controls.update(values)

    def set_processing(self, **values):
        self.processing.append(values)

    def snapshot(self, path):
        self.snapshots.append(path)

    def setStreamCaptureCallback(self, callback):
        self.callback = callback

    def open(self):
        return True

    def startStreamCapture(self):
        return True

    def close(self):
        self.closed = True
        return True

    def push(self, image=None, **info):
        image = image if image is not None else np.full((40, 60, 3), 90, np.uint8)
        self.latest_info = {"index": 1, "full_size": (5472, 3648), "fps": 9.0,
                            "stats": {"mean": 90, "p50": 90, "p99": 200, "saturated": 0.0, "stripes": 0.01},
                            "focus": (1.0, 0.5), **info}
        if self.callback:
            self.callback(image, (image.shape[1], image.shape[0]), "RGB888")


def pump(root, seconds=0.05):
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.005)
