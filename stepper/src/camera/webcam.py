"""USB camera capture in an isolated process.

The worker process opens the camera at its highest resolution (lowest frame rate), keeps the
latest full-resolution frame, and sends the GUI a light preview (≤1600 px) with focus scores
and brightness statistics. Full-resolution snapshots are written by the worker. Camera
controls (exposure, gain, brightness, ...) change live through a command queue.

A blocked camera driver can never freeze the window: the GUI only talks to queues and a
watchdog kills a silent worker.
"""
from __future__ import annotations

from collections import deque
import math
import multiprocessing as mp
import os
import platform
import queue
import threading
import time
from pathlib import Path

# Must be set before cameras are opened: Media Foundation's hardware transforms can make
# opening a camera slower than the capture watchdog allows.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2
import numpy as np

from camera.camera_module import CameraModule
from camera.discovery import discover_devices, parse_modes, v4l_info
from camera.modes import (Mode, best_mode, dshow_modes, modes_from_v4l2, ordered_candidates,
                          preset_modes, requested_mode)
from camera.processing import FrameAverager, downscale, focus_measurements, image_stats
from camera.stripes import StripeTracker, apply_gains

PREVIEW_MAX = (1600, 1200)      # preview frames sent to the GUI
DEFAULT_EXPOSURE = -4           # 62.5 ms = 15 periods of a 240 Hz DLP: no projector stripes
DEFAULT_WHITE_BALANCE = 4500    # kelvin; fixed so a red-only picture stays red
WATCHDOG_SECONDS = 15.0
STARTUP_SECONDS = 45.0

CONTROL_PROPS = {
    "gain": cv2.CAP_PROP_GAIN,
    "brightness": cv2.CAP_PROP_BRIGHTNESS,
    "contrast": cv2.CAP_PROP_CONTRAST,
    "saturation": cv2.CAP_PROP_SATURATION,
}

BACKENDS = {"msmf": cv2.CAP_MSMF, "dshow": cv2.CAP_DSHOW, "v4l2": cv2.CAP_V4L2,
            "avfoundation": cv2.CAP_AVFOUNDATION, "any": cv2.CAP_ANY}


# --------------------------------------------------------------------------- settings
def exposure_ms(value) -> float:
    """DirectShow exposure units are log2 seconds: -4 = 62.5 ms."""
    return 1000.0 * 2.0 ** value


def exposure_mode(settings: dict) -> str:
    """'auto' (this app's anti-stripe auto exposure), 'camera' (camera's own auto) or 'manual'."""
    value = str(settings.get("exposure", "auto")).strip().lower()
    if value in ("", "auto", "app"):
        return "auto"
    if value in ("camera", "default", "camera-auto"):
        return "camera"
    try:
        int(round(float(value)))
        return "manual"
    except ValueError:
        return "auto"


def manual_exposure(settings: dict):
    """The fixed exposure the camera should start at, in DirectShow units, or None to leave it alone."""
    mode = exposure_mode(settings)
    if mode == "camera":
        return None
    if mode == "auto":
        found = settings.get("stripe-free-exposure")
        try:
            return max(-13, min(-1, int(found))) if found is not None else DEFAULT_EXPOSURE
        except (TypeError, ValueError):
            return DEFAULT_EXPOSURE
    return max(-13, min(-1, int(round(float(settings["exposure"])))))


def initial_controls(settings: dict) -> dict:
    """Control values to apply when the camera opens. Missing/'auto'/'default' means leave alone."""
    controls = {}
    exposure = manual_exposure(settings)
    if exposure is not None:
        controls["exposure"] = exposure
    for name in CONTROL_PROPS:
        value = settings.get(name.replace("_", "-"), settings.get(name))
        if value is None or str(value).strip().lower() in ("", "auto", "default"):
            continue
        try:
            controls[name] = int(round(float(value)))
        except (TypeError, ValueError):
            pass
    # A fixed white balance by default: the camera's automatic white balance sees a red-only
    # picture and boosts green and blue, which turns faint projector leakage into bold bands.
    white_balance = str(settings.get("white-balance", DEFAULT_WHITE_BALANCE)).strip().lower()
    if white_balance == "auto":
        controls["white_balance_auto"] = True
    elif white_balance not in ("camera", "default"):
        try:
            controls["white_balance"] = int(float(white_balance))
        except (TypeError, ValueError):
            controls["white_balance"] = DEFAULT_WHITE_BALANCE
    return controls


def capture_backend(settings: dict):
    """[camera] backend = "auto" | "dshow" | "msmf" | "v4l2" | "any". On Windows "auto" is
    DirectShow, the only API through which the Arducam accepts exposure changes."""
    name = str(settings.get("backend", "auto")).strip().lower()
    if name == "msmf":
        # The Windows build of this app includes DirectShow only (the Arducam ignores exposure
        # changes through Media Foundation anyway).
        try:
            if cv2.videoio_registry.hasBackend(cv2.CAP_MSMF):
                return cv2.CAP_MSMF
        except Exception:
            pass
        print("Media Foundation is not available in this build; using DirectShow.")
        return cv2.CAP_DSHOW
    if name in BACKENDS:
        return BACKENDS[name]
    return {"Linux": cv2.CAP_V4L2, "Windows": cv2.CAP_DSHOW,
            "Darwin": cv2.CAP_AVFOUNDATION}.get(platform.system(), cv2.CAP_ANY)


def setting_flag(settings: dict, key: str, default: bool) -> bool:
    """true/false from the settings file, also when written as a string ("false", "no", "0")."""
    value = settings.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "off", "0", "")
    return bool(value)


def setting_int(settings: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(round(float(settings.get(key, default) or default)))))
    except (TypeError, ValueError):
        return default


def frame_problem(frame, reject_green=True):
    if frame is None or frame.ndim != 3 or frame.shape[2] != 3 or not frame.size:
        return "No decoded color frame"
    # Conservative heuristic for the solid green corruption seen on unstable USB streams.
    sample = frame[::8, ::8].astype(np.int16)
    green = (sample[:, :, 1] > 70) & (sample[:, :, 0] < 25) & (sample[:, :, 2] < 25)
    if reject_green and green.mean() > .65:
        return "Suspected green-frame corruption"
    return ""


# --------------------------------------------------------------------------- worker side
def _send(channel, kind, value):
    try:
        channel.put((kind, value), timeout=0 if kind == "frame" else 1)
    except queue.Full:
        pass


def apply_controls(cap, backend, controls: dict) -> dict:
    """Apply control values; returns what the camera reports afterwards."""
    readback = {}
    for name, value in controls.items():
        try:
            if name == "exposure":
                if backend == cv2.CAP_DSHOW:
                    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0)   # DirectShow: 0 = manual, 1 = auto
                    cap.set(cv2.CAP_PROP_EXPOSURE, value)
                    readback["exposure"] = cap.get(cv2.CAP_PROP_EXPOSURE)
                elif backend == cv2.CAP_V4L2:
                    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)   # V4L2: 1 = manual
                    cap.set(cv2.CAP_PROP_EXPOSURE, round(exposure_ms(value) * 10))  # units of 100 µs
                    # V4L2 reports 100 µs units; convert back to the log2-seconds units requested,
                    # or a working exposure would look "not accepted".
                    reported = cap.get(cv2.CAP_PROP_EXPOSURE)
                    readback["exposure"] = math.log2(reported / 10000.0) if reported > 0 else None
                else:
                    cap.set(cv2.CAP_PROP_EXPOSURE, value)
                    readback["exposure"] = cap.get(cv2.CAP_PROP_EXPOSURE)
            elif name == "camera_auto_exposure":
                cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1 if backend == cv2.CAP_DSHOW else 3)
            elif name == "white_balance_auto":
                cap.set(cv2.CAP_PROP_AUTO_WB, 1 if value else 0)
            elif name == "white_balance":
                cap.set(cv2.CAP_PROP_AUTO_WB, 0)
                cap.set(cv2.CAP_PROP_WB_TEMPERATURE, value)
                readback["white_balance"] = cap.get(cv2.CAP_PROP_WB_TEMPERATURE)
            elif name in CONTROL_PROPS:
                cap.set(CONTROL_PROPS[name], value)
                readback[name] = cap.get(CONTROL_PROPS[name])
        except cv2.error:
            pass
    return readback


def _read_ranges(device, backend) -> dict:
    if backend != cv2.CAP_DSHOW or not str(device).isdecimal():
        return {}
    try:
        from camera.dshow_controls import read_controls
        return read_controls(int(device))
    except Exception as exc:
        print(f"Could not read camera control ranges: {exc}")
        return {}


def available_modes(device, backend, settings) -> tuple[list[Mode], str]:
    """(modes to offer, where they came from). Highest resolution first."""
    modes, source = [], "presets"
    try:
        if backend == cv2.CAP_DSHOW and str(device).isdecimal():
            modes, source = dshow_modes(int(device)), "camera"
        elif backend == cv2.CAP_V4L2:
            modes, source = modes_from_v4l2(parse_modes(v4l_info(device, "--list-formats-ext"))), "camera"
    except Exception as exc:
        print(f"Could not list camera modes: {exc}")
        modes = []
    if not modes:
        return preset_modes(), "presets"
    return modes, source


def _modes_to_try(device, backend, settings) -> tuple[list[Mode], list[Mode], str]:
    offered, source = available_modes(device, backend, settings)
    wanted = requested_mode(settings)
    if wanted is not None:
        return [wanted], offered, source
    return ordered_candidates(offered), offered, source


def _open(device, backend, mode: Mode, controls: dict):
    cap = cv2.VideoCapture(int(device) if str(device).isdecimal() else device, backend)
    if not cap.isOpened():
        return cap, None
    if backend == cv2.CAP_DSHOW:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUY2"))
    elif backend == cv2.CAP_V4L2:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*("YUYV" if mode.fourcc in ("YUY2", "YUYV") else mode.fourcc)))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode.height)
    cap.set(cv2.CAP_PROP_FPS, mode.fps)
    return cap, apply_controls(cap, backend, controls)


def _first_frames(cap, stop, reject_green, needed=3, attempts=14, max_misses=3):
    """Read until `needed` good frames in a row. Gives up quickly when the camera sends nothing
    (an unsupported mode), so trying several modes stays fast."""
    good, misses, frame, problem = 0, 0, None, "No frames received"
    for _ in range(attempts):
        if stop.is_set():
            return None, "stopped"
        ok, frame = cap.read()
        if not ok:
            misses += 1
            problem = "No frames received"
            if misses >= max_misses and good == 0:
                break
            continue
        problem = frame_problem(frame, reject_green)
        good = good + 1 if not problem else 0
        if good >= needed:
            return frame, ""
    return None, problem


class _Snapshotter:
    """Encodes full-resolution snapshots on a background thread so the preview keeps running."""

    def __init__(self, channel):
        self.channel = channel

    def save(self, frame_bgr, path):
        def work():
            try:
                target = Path(path)
                if not target.suffix:
                    target = target.with_suffix(".png")
                target.parent.mkdir(parents=True, exist_ok=True)
                params = [cv2.IMWRITE_PNG_COMPRESSION, 1] if target.suffix.lower() == ".png" else []
                ok, data = cv2.imencode(target.suffix, frame_bgr, params)
                if not ok:
                    raise OSError(f"cannot save '{target.suffix}' images; use .png, .jpg, .bmp or .tif")
                target.write_bytes(data.tobytes())  # works with non-ASCII folders on Windows
                _send(self.channel, "snapshot", {"ok": True, "path": str(target),
                                                 "size": (frame_bgr.shape[1], frame_bgr.shape[0])})
            except Exception as exc:
                _send(self.channel, "snapshot", {"ok": False, "path": str(path), "error": str(exc)})
        threading.Thread(target=work, daemon=True).start()


def _process(frame_bgr, state, index, started, captured=None):
    """Build the message for one frame: preview, focus scores, statistics."""
    # Large frames are halved once (fast) and that is shared by the preview and the focus scales.
    half = cv2.pyrDown(frame_bgr) if frame_bgr.shape[1] >= 2 * state["preview_max"][0] else None
    preview = downscale(half if half is not None else frame_bgr, *state["preview_max"])
    # Exposure statistics and stripes are measured on this frame alone: averaging several
    # frames would hide the stripes the automatic exposure is trying to find.
    stats = image_stats(preview)
    fit = state["stripes"].analyse(preview)
    stats["stripes"] = fit.strength
    stats["stripe_period"] = round(fit.periods[0], 1) if fit.periods else None
    if state["destripe"]:
        preview = apply_gains(preview, fit.gains)   # each frame with its own stripes, before averaging
    preview = state["averager"].add(preview)
    focus = focus_measurements(frame_bgr, half=half)
    default = focus["default"]
    return {
        "image": cv2.cvtColor(preview, cv2.COLOR_BGR2RGB),
        "index": index,
        "full_size": (frame_bgr.shape[1], frame_bgr.shape[0]),
        "focus": (focus["red"][default], focus["blue"][default]),
        "focus_levels": {"red": focus["red"], "blue": focus["blue"], "crop": focus["crop"], "default": default},
        "stats": stats,
        "time": time.monotonic() - started,
        "captured": captured if captured is not None else time.monotonic(),
    }


def _capture(settings, channel, stop, commands=None):
    commands = commands if commands is not None else queue.Queue()
    backend = capture_backend(settings)
    requested = str(settings.get("device", settings.get("index", "auto")))
    api = {cv2.CAP_MSMF: "msmf", cv2.CAP_DSHOW: "dshow"}.get(backend)
    named = api is not None or platform.system() != "Windows"
    if requested == "auto":
        found = discover_devices(named=named, api=api or "dshow")
        # When an Arducam is connected, use only it: if it is busy, say so instead of quietly
        # switching to the laptop's own camera (autofocus would then measure the wrong picture).
        preferred = [d for d in found if "arducam" in d.name.lower()]
        devices = [d.device for d in (preferred or found)]
    else:
        devices = [requested]
    if not devices:
        _send(channel, "error", "No camera found. Connect the camera, then click Reconnect camera.")
        return
    reject_green = setting_flag(settings, "reject-green", True)
    controls = initial_controls(settings)
    state = {"destripe": setting_flag(settings, "stripe-filter", True),
             "averager": FrameAverager(setting_int(settings, "average-frames", 1, 1, 16)),
             "stripes": StripeTracker(), "preview_max": PREVIEW_MAX, "snapshots": []}
    failures = []
    for device in devices:
        to_try, offered, source = _modes_to_try(device, backend, settings)
        for mode in to_try:
            if stop.is_set():
                return
            _send(channel, "status", f"Opening camera {device} at {mode.label()}…")
            cap, readback = _open(device, backend, mode, controls)
            try:
                if readback is None:
                    failures.append(f"{device}: could not open the camera (busy, unplugged or in use by another program)")
                    _send(channel, "status", failures[-1])
                    break
                frame, problem = _first_frames(cap, stop, reject_green)
                if frame is None:
                    if problem == "stopped":
                        return
                    failures.append(f"{device} · {mode.label()}: {problem}")
                    _send(channel, "status", f"Skipped {failures[-1]}")
                    continue
                height, width = frame.shape[:2]
                if (width, height) != (mode.width, mode.height):
                    if requested_mode(settings) is not None:
                        _send(channel, "error", f"The camera did not accept {mode.width} × {mode.height}; it sent "
                                                f"{width} × {height}. Choose another resolution.")
                        return
                    if source == "presets" and mode is not to_try[-1]:
                        # The driver substituted another size: this preset isn't supported. Try the next.
                        failures.append(f"{device} · {mode.label()}: camera sent {width} × {height} instead")
                        _send(channel, "status", f"Skipped {failures[-1]}")
                        continue
                ranges = _read_ranges(device, backend)
                _send(channel, "modes", {"modes": [m.__dict__ for m in offered], "source": source})
                _send(channel, "ready", {"device": device, "width": width, "height": height,
                                         "fps": float(cap.get(cv2.CAP_PROP_FPS) or mode.fps),
                                         "backend": {cv2.CAP_DSHOW: "DirectShow", cv2.CAP_MSMF: "Media Foundation",
                                                     cv2.CAP_V4L2: "V4L2"}.get(backend, str(backend)),
                                         "controls": {**controls, **readback}, "requested": controls,
                                         "readback": readback, "ranges": ranges})
                _stream(cap, backend, channel, commands, stop, state, reject_green)
                return
            finally:
                cap.release()
    detail = "\n".join(failures[-3:]) or "No usable camera mode."
    _send(channel, "error", "The camera could not be started.\n" + detail +
          "\nClose other camera programs (Zoom, Teams, the Camera app, the Arducam viewer), then Reconnect.")


def _stream(cap, backend, channel, commands, stop, state, reject_green):
    snapshotter = _Snapshotter(channel)
    started = time.monotonic()
    index, failures, count, window_start = 0, 0, 0, time.monotonic()
    fps = 0.0
    last_good = None
    while not stop.is_set():
        try:
            while True:
                kind, value = commands.get_nowait()
                if kind == "set":
                    _send(channel, "controls", {"requested": value, "readback": apply_controls(cap, backend, value)})
                    if "exposure" in value:
                        state["stripes"].invalidate()   # look for the stripe spacing again at once
                elif kind == "process":
                    if "destripe" in value:
                        state["destripe"] = bool(value["destripe"])
                    if "average" in value:
                        state["averager"].set_count(int(value["average"]))
                elif kind == "snapshot":
                    if last_good is not None:
                        snapshotter.save(last_good, value)   # the picture as it was when asked
                    else:
                        state["snapshots"].append(value)
        except queue.Empty:
            pass
        ok, frame = cap.read()
        captured = time.monotonic()   # same clock in every process: autofocus compares it with stage moves
        problem = frame_problem(frame, reject_green) if ok else "Camera disconnected or capture failed"
        if problem:
            failures += 1
            if failures >= 8:
                _send(channel, "error", problem + ". Check the USB cable, close other camera programs, then Reconnect.")
                return
            continue
        failures = 0
        index += 1
        count += 1
        last_good = frame   # cap.read() returns a new array every time; nothing modifies it
        while state["snapshots"]:
            snapshotter.save(frame, state["snapshots"].pop(0))
        info = _process(frame, state, index, started, captured)
        elapsed = time.monotonic() - window_start
        if elapsed >= 2.0:
            fps, count, window_start = count / elapsed, 0, time.monotonic()
        info["fps"] = fps
        _send(channel, "frame", info)


def _capture_worker(settings, channel, stop, commands=None):
    try:
        _capture(settings, channel, stop, commands)
    except Exception as exc:
        _send(channel, "error", f"Capture failed: {exc}. Check the camera and click Reconnect camera.")


# --------------------------------------------------------------------------- GUI side
class Webcam(CameraModule):
    """GUI-side handle for the capture process."""

    def __init__(self, index="auto", settings=None):
        self.settings = dict(settings or {})
        self.settings.setdefault("device", str(index))
        self.status = "Not connected"
        self.state = "disconnected"
        self.diagnostics = deque(maxlen=80)
        self.actual = {}
        self.modes: list[Mode] = []
        self.modes_source = ""
        self.ranges: dict = {}
        self.controls: dict = {}
        self.measured_fps = 0.0
        self.latest_info: dict = {}
        self.capture_thread = None
        self.process = None
        self.commands = None
        self.should_stop = threading.Event()
        self.last_frame_at = 0.0
        self.snapshot_listener = None
        self.exposure_problem = ""
        self.__active__ = False

    # ---- control API used by the GUI
    def set_controls(self, values: dict):
        self.controls.update(values)
        self._command("set", dict(values))

    def set_processing(self, **values):
        self._command("process", values)

    def snapshot(self, path):
        self._command("snapshot", str(path))

    def _command(self, kind, value):
        if self.commands is not None:
            try:
                self.commands.put_nowait((kind, value))
            except Exception:
                pass

    def setExposureTime(self, value):
        # Exposure is managed through set_controls (DirectShow units), not microseconds.
        return False

    def open(self):
        self.status = "Searching for the camera…"
        self.state = "connecting"
        return True  # The worker performs and reports the actual device open.

    def startStreamCapture(self):
        if self.process is not None:
            return False
        self.should_stop.clear()
        context = mp.get_context("spawn")
        self.channel = context.Queue(maxsize=3)
        self.commands = context.Queue()
        self.stop = context.Event()
        self.process = context.Process(target=_capture_worker,
                                       args=(self.settings, self.channel, self.stop, self.commands), daemon=True)
        self.process.start()
        self.capture_thread = threading.Thread(target=self._monitor, daemon=True)
        self.capture_thread.start()
        return True

    def _monitor(self):
        last_message = time.monotonic()
        heard = False   # the first message can take longer (process start, driver, camera search)
        while not self.should_stop.is_set():
            try:
                kind, value = self.channel.get(timeout=.25)
            except queue.Empty:
                if not self.process.is_alive():
                    if self.__active__ or self.process.exitcode not in (None, 0):
                        self.status = "Camera stopped. Click Reconnect camera."
                    self.__active__ = False
                    self.state = "error"
                    return
                if time.monotonic() - last_message > (WATCHDOG_SECONDS if heard else STARTUP_SECONDS):
                    self.status = "Camera stopped responding. Close other camera apps, check USB, then Reconnect."
                    self.__active__ = False
                    self.state = "error"
                    self.diagnostics.append(self.status)
                    self.process.terminate()
                    return
                continue
            except (EOFError, OSError, ValueError):
                return
            last_message = time.monotonic()
            heard = True
            self.handle_message(kind, value)

    def handle_message(self, kind, value):
        if kind == "frame":
            self.last_frame_at = time.monotonic()
            self.measured_fps = value.get("fps", self.measured_fps)
            self.latest_info = value
            image = value["image"]
            if self.__streamCaptureCallback__:
                self.__streamCaptureCallback__(image, (image.shape[1], image.shape[0]), "RGB888")
        elif kind == "ready":
            self.actual = value
            self.ranges = value.get("ranges", {})
            self.controls.update(value.get("controls", {}))
            self.controls.update(value.get("requested", {}))  # what we asked for drives the auto settings
            self._check_exposure(value.get("requested", {}), value.get("readback", {}))
            self.state = "streaming"
            self.__active__ = True
            self.status = (f"Connected · {value['width']} × {value['height']} · {value['fps']:g} fps · "
                           f"{value.get('backend', '')} · camera {value['device']}")
        elif kind == "modes":
            self.modes = [Mode(**m) for m in value.get("modes", [])]
            self.modes_source = value.get("source", "")
        elif kind == "controls":
            if "requested" in value:
                self._check_exposure(value["requested"], value.get("readback", {}))
            else:
                self.controls.update(value)
        elif kind == "snapshot":
            if self.snapshot_listener:
                self.snapshot_listener(value)
        else:
            self.status = value
            self.diagnostics.append(value)
            if kind == "error":
                self.state = "error"
                self.__active__ = False

    def _check_exposure(self, requested: dict, readback: dict):
        """Notice a camera that ignores exposure changes (e.g. the Arducam through Media Foundation)."""
        if "exposure" not in requested or readback.get("exposure") is None:
            return
        try:
            wanted, got = float(requested["exposure"]), float(readback["exposure"])
        except (TypeError, ValueError):
            return
        if abs(wanted - got) > 0.5:
            self.exposure_problem = (f"The camera did not accept exposure {wanted:g} (it reports {got:g}), so "
                                     "automatic exposure cannot remove the stripes. Close other camera apps and "
                                     "reconnect the camera.")
        else:
            self.exposure_problem = ""

    def close(self):
        self.should_stop.set()
        if self.process is not None:
            self.stop.set()
            self.process.join(.8)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(1)
            if self.capture_thread:
                self.capture_thread.join(1)
            for q in (self.channel, self.commands):
                try:
                    q.close()
                except Exception:
                    pass
            self.process = None
        self.capture_thread = None
        self.commands = None
        self.state = "disconnected"
        self.__active__ = False
        return True

    def isOpen(self):
        return self.__active__


def describe_mode(info: dict) -> str:
    if not info:
        return ""
    return f"{info.get('width')} × {info.get('height')} · {info.get('fps', 0):g} fps"


def best_offered(modes: list[Mode]) -> Mode | None:
    return best_mode(modes)
