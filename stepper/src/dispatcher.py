"""Stepper state and actions, independent of the window layout.

EventDispatcher owns what the projector shows, the stage position, exposures, autofocus,
the projector LEDs and the camera's automatic settings. Widgets call its methods and
listen to its events.
"""
from __future__ import annotations

import json
import math
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from tkinter import Tk, messagebox
from typing import Callable, List, Optional

import cv2
import numpy as np
from PIL import Image, ImageOps

def load_yolo():
    """Alignment marker detection needs ultralytics + PyTorch (~1-2 GB). Imported only when a
    model file is present, so normal starts (and the camera worker process, which re-imports
    the main script on Windows) stay fast. The standalone exe ships without it."""
    try:
        from ultralytics import YOLO
        return YOLO
    except ImportError:
        return None


from camera.auto_exposure import CLEAN as CLEAN_LINES, AutoCamera, exposure_ms, refresh_hint
from camera.camera_module import CameraModule
from camera.processing import compute_focus_score
from hardware import ImageProcessSettings, Lithographer, ProcessedImage
from projector import TkProjector
from projector_leds import BLUE, RED, describe_mask
from stage_control.stage_controller import StageController

LED_OFF, LED_FOCUS, LED_EXPOSE = 0, RED, RED | BLUE
MAX_BORDER = 90.0   # percent; more leaves (almost) no picture
# Exposures switch on red *and* blue. Red light does not expose resist and the pattern has no
# red in it, and this mask means "illumination on" even if the projector firmware treats the
# value as a single on/off flag, so an exposure can never be silently dark.


def detect_alignment_markers(model, image, draw_rectangle=False):
    detections = []
    display_image = image.copy()
    try:
        image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        original_height, original_width = image_rgb.shape[:2]
        resized = cv2.resize(image_rgb, (640, 640))
        results = model(resized)
        for box in results[0].boxes:
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            x1 = int(x1 * original_width / 640)
            x2 = int(x2 * original_width / 640)
            y1 = int(y1 * original_height / 640)
            y2 = int(y2 * original_height / 640)
            detections.append(((x1, y1), (x2, y2)))
            if draw_rectangle:
                cv2.rectangle(display_image, (x1, y1), (x2, y2), (0, 255, 0), 5)
    except Exception as e:
        print(f"Detection failed: {e}")
    return detections, display_image


class StrAutoEnum(str, Enum):
    """Base class for string-valued enums that use auto()"""

    def _generate_next_value_(name, *_):
        return name.lower()


class ShownImage(StrAutoEnum):
    """What the projector is showing"""
    CLEAR = auto()
    PATTERN = auto()
    FLATFIELD = auto()
    RED_FOCUS = auto()
    UV_FOCUS = auto()


class PatterningStatus(StrAutoEnum):
    IDLE = auto()
    PATTERNING = auto()
    ABORTING = auto()


class Event(StrAutoEnum):
    """Events that can be dispatched to listeners"""
    SNAPSHOT = auto()
    SHOWN_IMAGE_CHANGED = auto()
    STAGE_POSITION_CHANGED = auto()
    IMAGE_ADJUST_CHANGED = auto()
    PATTERN_IMAGE_CHANGED = auto()
    MOVEMENT_LOCK_CHANGED = auto()
    EXPOSURE_PATTERN_PROGRESS_CHANGED = auto()
    PATTERNING_BUSY_CHANGED = auto()
    PATTERNING_FINISHED = auto()
    CHIP_CHANGED = auto()
    RED_BRIGHTNESS_CHANGED = auto()
    RED_SOURCE_CHANGED = auto()
    CAMERA_AUTO_CHANGED = auto()
    AUTOFOCUS_FINISHED = auto()
    LEDS_CHANGED = auto()
    PROJECTOR_SIZE_CHANGED = auto()
    NOTIFY = auto()            # (text, kind) for the status line


class MovementLock(StrAutoEnum):
    UNLOCKED = auto()
    XY_LOCKED = auto()
    LOCKED = auto()


class RedFocusSource(StrAutoEnum):
    """What the red (focus) light looks like"""
    IMAGE = auto()        # a focusing image chosen by the user
    SOLID = auto()        # even red over the whole field
    PATTERN = auto()      # the pattern, in red
    INV_PATTERN = auto()  # the pattern with light and dark swapped, in red


RED_SOURCE_TEXT = {
    RedFocusSource.SOLID: "Solid red",
    RedFocusSource.PATTERN: "Your pattern in red",
    RedFocusSource.INV_PATTERN: "Your pattern inverted, in red",
    RedFocusSource.IMAGE: "Your focusing image in red",
}


@dataclass
class AlignmentConfig:
    enabled: bool
    model_path: str
    right_marker_x: float
    left_marker_x: float
    top_marker_y: float
    bottom_marker_y: float
    x_scale_factor: float
    y_scale_factor: float


@dataclass
class LithographerConfig:
    stage: StageController
    camera: CameraModule
    camera_scale: float
    red_exposure: float
    uv_exposure: float
    alignment: AlignmentConfig


@dataclass
class ExposureLog:
    time: datetime
    path: str
    coords: tuple[float, float, float]
    duration: float  # ms
    aborted: bool

    def to_disk(self):
        return {"time": str(self.time), "path": self.path, "coords": self.coords,
                "duration": self.duration, "aborted": self.aborted}

    @classmethod
    def from_disk(cls, d):
        return cls(datetime.fromisoformat(d["time"]), d["path"], d["coords"], d["duration"], d["aborted"])


@dataclass
class ChipLayer:
    exposures: List[ExposureLog]

    def to_disk(self):
        return {"exposures": [ex.to_disk() for ex in self.exposures]}

    @classmethod
    def from_disk(cls, d):
        return cls([ExposureLog.from_disk(ex) for ex in d["exposures"]])


@dataclass
class Chip:
    layers: List[ChipLayer]

    def to_disk(self):
        return {"layers": [layer.to_disk() for layer in self.layers]}

    @classmethod
    def from_disk(cls, d):
        return cls([ChipLayer.from_disk(layer) for layer in d["layers"]])


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str) -> str:
    return _INVALID_FILENAME_CHARS.sub("-", name).strip() or fallback


def write_rgb_image(path, image_rgb) -> Path:
    """Save an RGB array. Works with non-ASCII folders on Windows, where cv2.imwrite fails silently."""
    path = Path(path)
    if not path.suffix:
        path = path.with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, data = cv2.imencode(path.suffix, cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise OSError(f"cannot save '{path.suffix}' images; use .png, .jpg, .bmp or .tif")
    path.write_bytes(data.tobytes())
    return path


def load_pattern_image(path) -> Image.Image:
    """Open a pattern as 8-bit RGB where bright means light.

    Transparent pixels become black (no light): many editors store transparent pixels as
    white, which a plain RGB conversion would turn into full exposure. 16-bit images are
    scaled down instead of clipped to white.
    """
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        image.load()
    if image.mode.startswith("I;16") or image.mode in ("I", "F"):
        values = np.asarray(image, dtype=np.float64)
        peak = 65535.0 if image.mode.startswith("I;16") or values.max() > 255 else 255.0
        image = Image.fromarray(np.clip(values * (255.0 / peak), 0, 255).astype(np.uint8))
    if image.mode == "P":
        image = image.convert("RGBA")
    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        image = Image.alpha_composite(Image.new("RGBA", rgba.size, (0, 0, 0, 255)), rgba)
    return image.convert("RGB")


class ConfigReader:
    """Reads values from a loaded config, collecting friendly messages instead of crashing on typos."""

    def __init__(self, config: dict):
        self.config = config
        self.problems: list[str] = []

    def number(self, section, key, default, positive=False):
        value = self.config.get(section, {}).get(key, default)
        try:
            result = float(value)
            if positive and result <= 0:
                raise ValueError
            return result
        except (TypeError, ValueError):
            self.problems.append(f"[{section}] {key} = {value!r} is not a valid number; using {default}.")
            return float(default)

    def flag(self, section, key, default):
        value = self.config.get(section, {}).get(key, default)
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0"):
            return False
        self.problems.append(f"[{section}] {key} = {value!r} should be true or false; using {str(default).lower()}.")
        return default


class StageError(RuntimeError):
    """A stage move failed; the user has already been told why."""


class AutofocusStopped(RuntimeError):
    pass


def describe_stage_error(exc: Exception, state: str = "") -> str:
    """Plain-language explanation of a stage failure, with what to do next."""
    text = str(exc)
    if "error:9" in text or state.startswith("Alarm"):
        return ("The stage controller is locked in ALARM mode, so it refuses to move. This happens after a "
                "limit switch is hit, or at power-up when homing is enabled.\n\n"
                "Fix: turn on homing (homing = true in the config) and restart, or unlock the controller "
                "with $X from a G-code terminal. Then restart this app.")
    if "error:15" in text:
        return "That move goes past the stage's travel limits. Choose a position inside the stage's range."
    if isinstance(exc, (TimeoutError, OSError)) or "timeout" in text.lower():
        return ("Lost contact with the stage controller. Check its USB cable and power, "
                "close any other program using the port, then restart this app.")
    return f"The stage controller reported: {text}"


class EventDispatcher:
    def __init__(self, stage: StageController, proj: TkProjector, root: Tk, camera: Optional[CameraModule],
                 red_exposure: float = 4167.0, uv_exposure: float = 25000.0):
        self.hardware = Lithographer(stage, proj)
        self.camera = camera
        self.camera_image = None          # latest preview frame (RGB numpy array)
        self.camera_info: dict = {}       # latest frame details from the camera worker
        self.frame_index = 0              # counts frames shown, for "wait for a fresh frame"
        self.focus_scores = (None, None)  # (red, uv) from the camera worker, full resolution
        self.focus_level: Optional[int] = None   # image scale the last autofocus judged focus at
        self.root = root
        self.config = None
        self.model = None

        self.red_focus = ProcessedImage()
        self.uv_focus = ProcessedImage()
        self.pattern = ProcessedImage()
        self.pattern_image = Image.new("RGB", (1, 1), "black")
        self.pattern_image_path = ""
        self.red_focus_image = Image.new("RGB", (1, 1), "black")
        self.red_focus_image_path = ""
        self.uv_focus_image = Image.new("RGB", (1, 1), "black")
        self.solid_red_image = Image.new("RGB", (1, 1), "red")

        self.image_adjust_position = (0.0, 0.0, 0.0)
        self.border_size = 0.0
        self.posterize_strength = None
        self.red_focus_source = RedFocusSource.SOLID
        self.red_brightness = 1.0   # 0.02-1.0: dims the red light so the camera does not wash out
        self._dimmed_cache = None
        self._last_stage_error_at = 0.0
        self.stage_idle_timeout = 20.0
        self.stage_setpoint = (0.0, 0.0, 0.0)

        self.shown_image = ShownImage.CLEAR
        self.autofocus_busy = False
        self.autofocus_abort = False
        self.autofocus_max_travel = 150.0  # µm either side of the starting Z
        self.last_autofocus = ""
        self.patterning_busy = False
        self.exposure_phase = ""           # "preparing" (waiting for the stage) or "exposing"
        self.homing_busy = False
        self.homing_abort = False
        self.sequence_busy = False         # tiling / capture & stitch: manual moves locked
        self.sequence_abort = False
        self.autofocus_on_mode_switch = False
        self.realtime_detection = False
        self.should_abort = False
        self._light_on_at = None
        self._light_off_at = None
        self._light_timer = None
        self._leds_ok = True
        self._projector_error = None

        self.exposure_time = 8000          # ms
        self.patterning_progress = 0.0
        self.exposure_progress = 0.0
        self.exposure_end = 0.0
        self.last_exposure = ""
        self.uv_z_offset = 0.0             # µm moved in Z for the exposure and back afterwards
        self.red_exposure_time = red_exposure
        self.uv_exposure_time = uv_exposure

        self.exposure_history = []
        self.chip = Chip([ChipLayer([])])

        self.auto_snapshot_on_uv = True    # camera snapshot when an exposure starts
        self.snapshot_directory = Path("stepper_captures")
        try:
            self.snapshot_directory.mkdir(exist_ok=True)
        except OSError:
            pass
        self.snapshot_requester: Optional[Callable[[str], None]] = None   # set by the window
        self.ensure_output: Optional[Callable[[], bool]] = None          # opens the projector window

        # Projector LEDs over USB (optional)
        self.leds = None
        self.led_status = "Projector LED control is off."

        # Automatic camera exposure / red brightness
        self.auto_camera = AutoCamera()
        self.auto_camera_wanted = True     # run the stripe search when red light is first shown
        self.projector_refresh_hz = 0      # refresh rate of the projector display (0 = unknown)
        self._auto_started = False

        self.listeners = dict()
        self.add_event_listener(Event.SHOWN_IMAGE_CHANGED, lambda: self._update_projector())
        self.sync_stage_position()   # start from where the stage really is

    # ------------------------------------------------------------------ chip record
    def load_chip(self, path: str):
        with open(path, "r") as f:
            d = json.load(f)
        self.chip = Chip.from_disk(d)
        self.on_event(Event.CHIP_CHANGED)

    def new_chip(self):
        self.chip = Chip([ChipLayer([])])
        self.on_event(Event.CHIP_CHANGED)

    def add_chip_layer(self):
        self.chip.layers.append(ChipLayer([]))
        self.on_event(Event.CHIP_CHANGED)

    def save_chip(self, path: str):
        with open(path, "w") as f:
            json.dump(self.chip.to_disk(), f)

    def delete_chip_exposure(self, layer: int, ex: int):
        self.chip.layers[layer].exposures.pop(ex)
        self.on_event(Event.CHIP_CHANGED)

    # ------------------------------------------------------------------ projector image
    @property
    def current_image(self) -> Optional[Image.Image]:
        match self.shown_image:
            case ShownImage.CLEAR:
                return None
            case ShownImage.RED_FOCUS:
                image = self.red_focus.processed()
                if self.red_brightness < 0.999:
                    key = (id(image), round(self.red_brightness, 4))
                    if self._dimmed_cache is None or self._dimmed_cache[0] != key:
                        scale = self.red_brightness
                        self._dimmed_cache = (key, image.point(lambda v: int(v * scale + 0.5)))
                    return self._dimmed_cache[1]
                return image
            case ShownImage.UV_FOCUS:
                return self.uv_focus.processed()
            case ShownImage.PATTERN:
                return self.pattern.processed()
        return None

    def preview_image(self) -> tuple[Optional[Image.Image], str]:
        """(image for the on-screen previews, note). Red light is often dimmed to a few percent for
        the camera, which would make the preview look black; the previews show its shape at full
        brightness and say how bright the projector really is."""
        if self.shown_image == ShownImage.CLEAR:
            return None, ""
        if self.shown_image == ShownImage.RED_FOCUS and self.red_brightness < 0.999:
            return self.red_focus.processed(), f"red at {self.red_brightness:.0%}: shown brighter here"
        return getattr(self.hardware.projector, "current_image", None), ""

    def red_preview_of_pattern(self) -> Optional[Image.Image]:
        """The pattern as 'Show it in red' projects it (red light, same size and position)."""
        if not self.pattern_image_path:
            return None
        image = self.pattern.processed()
        key = id(image)
        if getattr(self, "_red_pattern_cache", (None, None))[0] != key:
            rgb = image.convert("RGB")
            blank = Image.new("L", rgb.size, 0)
            self._red_pattern_cache = (key, Image.merge("RGB", (rgb.getchannel("B"), blank, blank)))
        return self._red_pattern_cache[1]

    @staticmethod
    def led_mask_for(shown: ShownImage) -> int:
        if shown == ShownImage.RED_FOCUS:
            return LED_FOCUS
        if shown in (ShownImage.UV_FOCUS, ShownImage.PATTERN):
            return LED_EXPOSE
        return LED_OFF

    def set_leds(self, mask: int, force: bool = False) -> bool:
        if self.leds is None:
            return True
        if self.patterning_busy and not force:
            print("LED change ignored: an exposure is running.")
            return False
        if getattr(self.leds, "failures", 0) >= 3:
            # Unplugged or crashed: stop retrying (each try can block) until the user reconnects.
            self.led_status = ("Projector LED control stopped after repeated USB errors. Check the projector's USB "
                               "cable, then Settings → Projector → Reconnect LEDs (or turn LED control off).")
            self.on_event(Event.LEDS_CHANGED)
            return False
        ok = self.leds.set_mask(mask)
        if ok:
            self.led_status = f"Projector LEDs: {describe_mask(mask)} (controlled over USB)."
        else:
            self.led_status = ("Projector LEDs: a USB command failed. Check the projector's USB cable; "
                               "the app will retry on the next change.")
        self.on_event(Event.LEDS_CHANGED)
        return ok

    def attach_leds(self, controller, status: str):
        self.leds = controller
        self.led_status = status
        if controller is not None:
            self.set_leds(self.led_mask_for(self.shown_image))
        self.on_event(Event.LEDS_CHANGED)

    def _update_projector(self):
        mask = self.led_mask_for(self.shown_image)
        self._projector_error = None
        try:
            img = self.current_image
            if mask & BLUE:
                # Light that exposes: the picture first (dark while only the red LED is on), then
                # the LEDs. If drawing fails, the blue LED is never switched on.
                self.hardware.projector.show(img)
                self._leds_ok = self.set_leds(mask, force=True)
            else:
                # Red or off: switch blue off first, then change the picture.
                self._leds_ok = self.set_leds(mask, force=True)
                if img is None:
                    self.hardware.projector.clear()
                else:
                    self.hardware.projector.show(img)
        except Exception as exc:
            self._projector_error = exc
            raise
        finally:
            self._vendor_camera_exposure()
            self._sync_auto_camera()

    def _vendor_camera_exposure(self):
        """Basler/FLIR cameras: their red/UV exposure times from the config (µs)."""
        camera = self.camera
        if camera is None or hasattr(camera, "set_controls") or not hasattr(camera, "setExposureTime"):
            return
        wanted = self.uv_exposure_time if self.in_uv() else self.red_exposure_time
        if getattr(self, "_vendor_exposure", None) != wanted:
            self._vendor_exposure = wanted
            try:
                camera.setExposureTime(wanted)
            except Exception as exc:
                print(f"Camera exposure not set: {exc}")

    def describe_projection(self) -> tuple[str, str, str]:
        """(badge text, colour kind, plain-language sentence) for what the projector shows now."""
        name = Path(self.pattern_image_path).name if self.pattern_image_path else "the pattern"
        if self.patterning_busy and self.exposure_phase == "preparing":
            return ("STARTING", "warn", "Getting ready to expose: waiting for the stage to stop. Nothing is exposed yet. "
                                        "STOP or Esc cancels.")
        if self.shown_image == ShownImage.PATTERN:
            if self.patterning_busy:
                left = max(0.0, self.exposure_end - time.monotonic())
                return "EXPOSING", "uv", f"Exposing {name} in blue: {left:.1f} s left."
            return "PATTERN · BLUE", "uv", f"{name} in blue. Blue light exposes resist."
        if self.shown_image == ShownImage.RED_FOCUS:
            text = RED_SOURCE_TEXT.get(self.red_focus_source, "Red light")
            return "RED · SAFE", "red", f"{text} at {self.red_brightness:.0%}. Red light does not expose resist."
        if self.shown_image == ShownImage.UV_FOCUS:
            return "BLUE MARK", "uv", "Blue focus mark (a plus sign). It exposes resist where it lands."
        if self.shown_image == ShownImage.FLATFIELD:
            return "FLATFIELD", "uv", "Flat-field calibration image."
        return "OFF", "off", "Nothing is projected" + (" (LEDs off)." if self.leds else ".")

    def led_summary(self) -> tuple[str, str]:
        """(kind, short text) for the LED status line; led_status has the full explanation."""
        if self.leds is None:
            return "off", "LED control off (Settings → Projector)"
        if self.leds.mask is None:
            return "warn", "LEDs: USB command failed"
        return "ok", f"LEDs: {describe_mask(self.leds.mask)} · USB"

    def set_projector_size(self, size: tuple[int, int]):
        size = (int(size[0]), int(size[1]))
        projector = self.hardware.projector
        if not hasattr(projector, "set_size") or projector.size() == size:
            return
        projector.set_size(size)
        self._dimmed_cache = None
        self._refresh_red_focus()
        self._refresh_uv_focus()
        self._refresh_pattern()
        self.on_event(Event.PROJECTOR_SIZE_CHANGED)
        self.on_event(Event.SHOWN_IMAGE_CHANGED)

    def _settings(self, channels, border):
        return ImageProcessSettings(posterization=self.posterize_strength, color_channels=channels, flatfield=None,
                                    size=self.hardware.projector.size(), image_adjust=self.image_adjust_position,
                                    border_size=border)

    def _refresh_pattern(self):
        self.pattern.update(image=self.pattern_image, settings=self._settings((False, False, True), self.border_size))
        if self.red_focus_source in (RedFocusSource.PATTERN, RedFocusSource.INV_PATTERN):
            self._refresh_red_focus()
        self.on_event(Event.PATTERN_IMAGE_CHANGED)

    def set_red_brightness(self, fraction: float, by_user: bool = True):
        fraction = max(0.02, min(1.0, float(fraction)))
        if by_user and self.auto_camera.auto_light:
            self.auto_camera.auto_light = False  # the user took over
            self.on_event(Event.CAMERA_AUTO_CHANGED)
        if abs(fraction - self.red_brightness) < 1e-4:
            return
        self.red_brightness = fraction
        if self.shown_image == ShownImage.RED_FOCUS:
            self.on_event(Event.SHOWN_IMAGE_CHANGED)
        self.on_event(Event.RED_BRIGHTNESS_CHANGED)

    def set_red_focus_source(self, source: RedFocusSource):
        self.red_focus_source = source
        self._refresh_red_focus()
        self.on_event(Event.RED_SOURCE_CHANGED)

    def _red_focus_source(self) -> Image.Image:
        match self.red_focus_source:
            case RedFocusSource.IMAGE:
                return self.red_focus_image
            case RedFocusSource.PATTERN:
                return self.pattern_image.getchannel("B").convert("RGBA")
            case RedFocusSource.INV_PATTERN:
                return ImageOps.invert(self.pattern_image.getchannel("B")).convert("RGBA")
        return self.solid_red_image

    def _refresh_red_focus(self):
        if self.hardware.projector.size() != self.solid_red_image.size:
            self.solid_red_image = Image.new("RGB", self.hardware.projector.size(), "red")
        self.red_focus.update(image=self._red_focus_source(), settings=self._settings((True, False, False), self.border_size))
        self._dimmed_cache = None
        if self.shown_image == ShownImage.RED_FOCUS:
            self.on_event(Event.SHOWN_IMAGE_CHANGED)

    def _refresh_uv_focus(self):
        self.uv_focus.update(image=self.uv_focus_image, settings=self._settings((False, False, True), 0.0))
        if self.shown_image == ShownImage.UV_FOCUS:
            self.on_event(Event.SHOWN_IMAGE_CHANGED)

    def set_posterize_strength(self, strength: Optional[int]):
        self.posterize_strength = strength
        self._refresh_red_focus()
        self._refresh_uv_focus()
        self._refresh_pattern()

    def set_border_size(self, border_size: float):
        # A border of (nearly) 100 % leaves no picture at all.
        self.border_size = max(0.0, min(MAX_BORDER, float(border_size)))
        self._refresh_red_focus()
        self._refresh_uv_focus()
        self._refresh_pattern()

    def set_shown_image(self, shown_image: ShownImage, force: bool = False):
        if self.patterning_busy and not force and shown_image != self.shown_image:
            print(f"Projector change to {shown_image.value} ignored: an exposure is running.")
            return
        self.shown_image = shown_image
        self.on_event(Event.SHOWN_IMAGE_CHANGED)

    def show_red(self, source: Optional[RedFocusSource] = None):
        if self.patterning_busy:
            return
        if source is not None and source != self.red_focus_source:
            self.red_focus_source = source
            self._refresh_red_focus()
            self.on_event(Event.RED_SOURCE_CHANGED)
        self.set_shown_image(ShownImage.RED_FOCUS)

    def enter_red_mode(self, mode_switch_autofocus=True):
        """Red light on (used by tiling). Optionally autofocus in red."""
        self.show_red()
        if mode_switch_autofocus and self.autofocus_on_mode_switch:
            self.autofocus(blue_only=False)
        self.on_event(Event.MOVEMENT_LOCK_CHANGED)

    def enter_uv_mode(self, mode_switch_autofocus=True):
        """Projector dark, ready to expose (used by tiling)."""
        if self.patterning_busy:
            return
        self.set_shown_image(ShownImage.CLEAR)
        if mode_switch_autofocus and self.autofocus_on_mode_switch:
            self.non_blocking_delay(1.0)
            self.autofocus(blue_only=True)
        self.on_event(Event.MOVEMENT_LOCK_CHANGED)

    def show_uv_mark(self):
        if not self.patterning_busy:
            self.set_shown_image(ShownImage.UV_FOCUS)

    def clear_projector(self):
        if self.patterning_busy:
            self.abort_patterning()
        self.set_shown_image(ShownImage.CLEAR)

    def set_pattern_image(self, img: Image.Image, path: str):
        self.pattern_image = img
        self.pattern_image_path = path
        self._refresh_pattern()

    def set_red_focus_image(self, img: Image.Image, path: str = ""):
        self.red_focus_image = img
        self.red_focus_image_path = path
        self._refresh_red_focus()

    def set_uv_focus_image(self, img: Image.Image):
        self.uv_focus_image = img
        self._refresh_uv_focus()

    def set_image_position(self, x, y, t):
        self.image_adjust_position = (x, y, t)
        self._refresh_red_focus()
        self._refresh_uv_focus()
        self._refresh_pattern()
        self.on_event(Event.IMAGE_ADJUST_CHANGED)

    @property
    def image_position(self):
        return self.image_adjust_position

    # ------------------------------------------------------------------ stage
    def _stage_call(self, method: str, coords: dict[str, float]):
        stage = self.hardware.stage
        try:
            getattr(stage, method)(coords)
        except Exception as exc:
            state = ""
            try:
                stage.is_idle()              # refresh the controller state (e.g. Alarm)
                state = stage.state_name()
            except Exception:
                pass
            print(f"Stage move failed: {exc}")
            # One dialog per burst: autofocus, hold-to-repeat and tiling issue many moves in a row.
            if time.monotonic() - self._last_stage_error_at > 3.0:
                messagebox.showerror("Stage did not move", describe_stage_error(exc, state))
            self._last_stage_error_at = time.monotonic()
            raise StageError(str(exc)) from exc

    def sync_stage_position(self) -> bool:
        """Take the position from the controller (after connecting and after homing), so the
        readout and absolute moves match where the stage really is."""
        try:
            pos = self.hardware.stage.position()
        except Exception as exc:
            print(f"Could not read the stage position: {exc}")
            pos = None
        if pos is None:
            return False
        self.stage_setpoint = tuple(float(v) for v in pos)
        self.on_event(Event.STAGE_POSITION_CHANGED)
        return True

    def move_absolute(self, coords: dict[str, float]):
        target = {axis: float(coords[axis]) for axis in "xyz" if axis in coords}
        if not target:
            return
        self._stage_call("move_to", target)
        self.stage_setpoint = tuple(target.get(axis, self.stage_setpoint[i]) for i, axis in enumerate("xyz"))
        self.on_event(Event.STAGE_POSITION_CHANGED)

    def move_relative(self, coords: dict[str, float]):
        """A relative move (G91 on GRBL): only the given axes move, by exactly these amounts."""
        deltas = {axis: float(coords[axis]) for axis in "xyz" if axis in coords and float(coords[axis]) != 0.0}
        if not deltas:
            return
        self._stage_call("move_by", deltas)
        self.stage_setpoint = tuple(self.stage_setpoint[i] + deltas.get(axis, 0.0) for i, axis in enumerate("xyz"))
        self.on_event(Event.STAGE_POSITION_CHANGED)

    def jog(self, axis: str, amount: float) -> bool:
        """A manual move from the stage controls; refused while an exposure/autofocus runs."""
        lock = self.movement_lock
        if lock == MovementLock.LOCKED or (lock == MovementLock.XY_LOCKED and axis != "z"):
            return False
        try:
            self.move_relative({axis: amount})
            return True
        except StageError:
            return False

    def stage_is_idle(self) -> bool:
        try:
            return bool(self.hardware.stage.is_idle())
        except Exception:
            return True

    def _any_abort(self) -> bool:
        return self.should_abort or self.autofocus_abort or self.homing_abort or self.sequence_abort

    def wait_for_stage_idle(self, timeout: float = 20.0) -> bool:
        """Wait (keeping the window responsive) until the stage reports it has stopped.
        Returns False on timeout, or early when STOP was pressed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.hardware.stage.is_idle():
                return True
            if self._any_abort():
                return False
            self.non_blocking_delay(0.05)
        return False

    def home_stage(self) -> bool:
        """Run the homing cycle without freezing the window. STOP aborts it."""
        stage = self.hardware.stage
        if self.homing_busy or self.patterning_busy or self.autofocus_busy:
            return False
        self.homing_busy, self.homing_abort = True, False
        self.on_event(Event.MOVEMENT_LOCK_CHANGED)
        self.notify("Homing the stage: every axis moves to its limit switch. Keep hands clear (STOP cancels).", "warn")
        try:
            if hasattr(stage, "begin_home"):
                stage.begin_home()
                deadline = time.monotonic() + 120.0
                self.non_blocking_delay(1.0)
                while True:
                    if self.homing_abort:
                        stage.stop()
                        raise RuntimeError("stopped with STOP. The controller now needs homing again "
                                           "(click Home) before it moves")
                    if stage.homing_finished():
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError("homing did not finish within 2 minutes")
                    self.non_blocking_delay(0.3)
            else:
                stage.home()
        except Exception as exc:
            print(f"Homing failed: {exc}")
            if self.homing_abort:
                self.notify(f"Homing {exc}.", "warn")
            else:
                messagebox.showerror("Homing failed", "The stage could not complete homing, so positions are not "
                                     "referenced to the limit switches.\n\n" + describe_stage_error(exc))
            return False
        finally:
            self.homing_busy = self.homing_abort = False
            self.on_event(Event.MOVEMENT_LOCK_CHANGED)
        self.sync_stage_position()
        self.notify("Stage homed.", "ok")
        return True

    @property
    def movement_lock(self):
        if self.patterning_busy or self.autofocus_busy or self.homing_busy or self.sequence_busy:
            return MovementLock.LOCKED
        return MovementLock.UNLOCKED

    def set_sequence_busy(self, busy: bool):
        """Tiling or capture & stitch is moving the stage: lock the manual controls."""
        self.sequence_busy = busy
        self.sequence_abort = False   # a STOP belongs to the run it stopped
        self.on_event(Event.MOVEMENT_LOCK_CHANGED)

    # ------------------------------------------------------------------ events
    def on_event(self, event: Event, *args, **kwargs):
        for listener in list(self.listeners.get(event, [])):
            try:
                listener(*args, **kwargs)
            except Exception:
                # A broken display update must never stop the stepper logic (e.g. an exposure
                # ending): report it and carry on with the other listeners.
                traceback.print_exc()

    def on_event_cb(self, event: Event, *args, **kwargs):
        return lambda: self.on_event(event, *args, **kwargs)

    def add_event_listener(self, event: Event, listener: Callable):
        self.listeners.setdefault(event, []).append(listener)

    def notify(self, text: str, kind: str = "muted"):
        try:
            print(text)
        except (UnicodeError, OSError, ValueError):
            pass  # a console that cannot show the text must not stop the app
        self.on_event(Event.NOTIFY, text, kind)

    def non_blocking_delay(self, t: float):
        end = time.monotonic() + t
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.004)

    # ------------------------------------------------------------------ camera frames
    def set_latest_image(self, camera_image):
        self.camera_image = camera_image

    def on_camera_frame(self, image, info: Optional[dict] = None):
        """Called by the window for every new preview frame."""
        info = info or {}
        self.camera_image = image
        self.camera_info = info
        self.frame_index += 1
        self.focus_scores = tuple(info.get("focus", (None, None)))
        self._step_auto_camera(info)

    def camera_lost(self):
        self.camera_image = None
        self.camera_info = {}
        self.focus_scores = (None, None)

    def current_focus(self, blue_only: bool) -> Optional[float]:
        """Focus score for the live focus bar: at the scale the last autofocus found best, else the
        camera's default scale (about 700 px across the centre of the picture)."""
        levels = ((self.camera_info or {}).get("focus_levels") or {}).get("blue" if blue_only else "red")
        if levels and self.focus_level is not None and self.focus_level < len(levels) \
                and levels[self.focus_level] is not None:
            return float(levels[self.focus_level])
        score = self.focus_scores[1 if blue_only else 0] if self.focus_scores else None
        if score is not None:
            return float(score)
        if self.camera_image is not None:
            return compute_focus_score(self.camera_image, blue_only=blue_only)
        return None

    def wait_for_frames(self, count: int = 1, timeout: float = 3.0) -> bool:
        """Wait until `count` new frames have arrived (frames that started after now)."""
        target = self.frame_index + count
        deadline = time.monotonic() + timeout
        while self.frame_index < target:
            if time.monotonic() > deadline or self.camera_image is None:
                return False
            if self.autofocus_abort:
                raise AutofocusStopped("stopped")
            self.non_blocking_delay(0.01)
        return True

    # ------------------------------------------------------------------ automatic camera settings
    def camera_supports_controls(self) -> bool:
        return self.camera is not None and hasattr(self.camera, "set_controls")

    def configure_auto_camera(self, auto_exposure: bool = None, auto_light: bool = None):
        if auto_exposure is not None:
            self.auto_camera.auto_exposure = bool(auto_exposure)
            if not auto_exposure:
                self.auto_camera.cancel_search()   # keep the exposure the user chose
        if auto_light is not None:
            self.auto_camera.auto_light = bool(auto_light)
        if self.auto_camera.phase == "idle" and (self.auto_camera.auto_exposure or self.auto_camera.auto_light):
            self._auto_started = False
            self._sync_auto_camera()
        self.on_event(Event.CAMERA_AUTO_CHANGED)

    def reset_auto_camera(self):
        """A different camera (or a reconnect): forget what the automatic settings learned."""
        auto = self.auto_camera
        auto.phase, auto.status, auto.summary = "idle", "Auto: waiting for the camera…", ""
        auto.results, auto.best_exposure = {}, None
        auto._last_index = -1
        auto.ranges = {}
        self._auto_started = False
        self.focus_level = None   # another camera or resolution: the scales differ
        self.on_event(Event.CAMERA_AUTO_CHANGED)

    def start_stripe_search(self):
        if not self.camera_supports_controls():
            self.notify("Stripe search needs a USB camera with adjustable exposure.", "warn")
            return
        self.auto_camera.auto_exposure = True
        self.auto_camera.ranges = getattr(self.camera, "ranges", {}) or {}
        self.auto_camera.start_search(self.frame_index)
        self._auto_started = True
        if self.shown_image != ShownImage.RED_FOCUS:
            self.show_red()
        self.on_event(Event.CAMERA_AUTO_CHANGED)

    def _sync_auto_camera(self):
        auto = self.auto_camera
        active = (self.shown_image == ShownImage.RED_FOCUS and not self.patterning_busy and not self.autofocus_busy
                  and self.camera_supports_controls() and getattr(self.camera, "state", "") == "streaming")
        if not (auto.auto_exposure or auto.auto_light):
            if auto.phase != "idle":
                auto.phase, auto.status = "idle", "Auto off"
                self.on_event(Event.CAMERA_AUTO_CHANGED)
            return
        if active:
            if auto.phase == "paused":
                auto.resume(self.frame_index)
            elif auto.phase == "idle" and not self._auto_started:
                auto.ranges = getattr(self.camera, "ranges", {}) or {}
                self._auto_started = True
                found = getattr(self.camera, "settings", {}).get("stripe-free-exposure")
                if auto.auto_exposure and self.auto_camera_wanted:
                    # A remembered exposure is checked first (a few seconds) and kept if it is still
                    # line-free; otherwise every candidate is tried. A stale value (another resolution,
                    # another projector setting) used to bring the lines back for the whole session.
                    auto.start_search(self.frame_index, remembered=found)
                else:
                    auto.phase, auto.status = "track", "Auto: adjusting…"
                    auto.best_exposure = found
        elif auto.phase in ("search", "track"):
            auto.pause("Auto paused: it works while the projector shows red light.")
        self.on_event(Event.CAMERA_AUTO_CHANGED)

    def _step_auto_camera(self, info: dict):
        auto = self.auto_camera
        if auto.phase in ("idle", "paused") or not info.get("stats"):
            if auto.phase == "idle" and (auto.auto_exposure or auto.auto_light) and not self._auto_started:
                self._sync_auto_camera()
            return
        if self.shown_image != ShownImage.RED_FOCUS or self.patterning_busy or self.autofocus_busy:
            self._sync_auto_camera()
            return
        controls = getattr(self.camera, "controls", {}) or {}
        exposure = int(round(controls.get("exposure", -4)))
        gain = int(round(controls.get("gain", (auto.ranges.get("gain") or {}).get("min", 100))))
        before = (auto.phase, auto.status)
        actions = auto.update(self.frame_index, info["stats"], self.red_brightness, exposure, gain)
        for action in actions:
            if action.kind == "red":
                self.set_red_brightness(action.value, by_user=False)
            elif action.kind == "camera":
                self.camera.set_controls(dict(action.value))
        if before != (auto.phase, auto.status) or actions:
            if before[0] == "search" and auto.phase == "track" and auto.best_exposure is not None:
                settings = getattr(self.camera, "settings", None)
                if isinstance(settings, dict):
                    settings["stripe-free-exposure"] = auto.best_exposure
                measured = [v for v in auto.results.values() if v is not None]
                if measured and min(measured) > 2 * CLEAN_LINES:
                    auto.summary += refresh_hint(self.projector_refresh_hz)
                self.notify(f"Stripe-free exposure found: {auto.best_exposure} "
                            f"({exposure_ms(auto.best_exposure):g} ms).", "ok")
            self.on_event(Event.CAMERA_AUTO_CHANGED)

    # ------------------------------------------------------------------ snapshots
    def snapshot_path(self, prefix="snapshot", extension="png") -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = self.snapshot_directory / f"{safe_filename(prefix, 'snapshot')}_{stamp}.{extension}"
        counter = 2
        while path.exists():
            path = self.snapshot_directory / f"{safe_filename(prefix, 'snapshot')}_{stamp}_{counter}.{extension}"
            counter += 1
        return path

    def set_snapshot_directory(self, directory: Path):
        self.snapshot_directory = Path(directory)
        self.snapshot_directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ exposure
    def set_patterning_busy(self, busy: bool):
        self.patterning_busy = busy
        self.on_event(Event.MOVEMENT_LOCK_CHANGED)
        self.on_event(Event.PATTERNING_BUSY_CHANGED)

    def set_progress(self, pattern_progress: float, exposure_progress: float):
        self.patterning_progress = pattern_progress
        self.exposure_progress = exposure_progress
        self.on_event(Event.EXPOSURE_PATTERN_PROGRESS_CHANGED)

    def set_autofocus_busy(self, busy):
        self.autofocus_busy = busy
        self.on_event(Event.MOVEMENT_LOCK_CHANGED)
        self._sync_auto_camera()

    def abort_patterning(self):
        if self.patterning_busy:
            self.should_abort = True

    def stop_all(self):
        """The STOP button: end an exposure, autofocus, homing or tiling, and turn the projector off."""
        if self.patterning_busy:
            self.should_abort = True
        if self.autofocus_busy:
            self.autofocus_abort = True
        if self.homing_busy:
            self.homing_abort = True
        if self.sequence_busy:
            self.sequence_abort = True
        if not self.patterning_busy:
            self.set_shown_image(ShownImage.CLEAR)

    def busy_text(self) -> str:
        """What is running (for messages), or '' when idle."""
        if self.patterning_busy:
            return "exposure"
        if self.autofocus_busy:
            return "autofocus"
        if self.homing_busy:
            return "homing"
        if self.sequence_busy:
            return "tiling"
        return ""

    def in_uv(self):
        return self.shown_image in (ShownImage.PATTERN, ShownImage.UV_FOCUS)

    def exposure_problem(self) -> str:
        if not self.pattern_image_path:
            return "Choose a pattern first (step 1)."
        if not isinstance(self.exposure_time, (int, float)) or not math.isfinite(self.exposure_time) or self.exposure_time <= 0:
            return "Enter an exposure time greater than 0 seconds."
        return ""

    def _light_off(self):
        """End the exposure light now: LEDs off, picture black."""
        if self._light_on_at is not None and self._light_off_at is None:
            self._light_off_at = time.monotonic()
        self.set_shown_image(ShownImage.CLEAR, force=True)
        if self._projector_error is not None or not self._leds_ok:
            print(f"Clearing the projector reported a problem: {self._projector_error}")
            self.shown_image = ShownImage.CLEAR
            try:
                self.set_leds(LED_OFF, force=True)
            except Exception:
                pass

    def _exposure_time_up(self):
        """Safety timer: ends the light on time even if the window is stuck in a dialog."""
        self._light_timer = None
        if self.patterning_busy and self.shown_image == ShownImage.PATTERN:
            self._light_off()

    def begin_patterning(self) -> bool:
        """Expose the pattern for exposure_time ms. Returns True if light was projected."""
        if self.patterning_busy or self.autofocus_busy or self.homing_busy:
            return False
        problem = self.exposure_problem()
        if problem:
            messagebox.showinfo("Exposure not started", problem)
            return False

        duration = float(self.exposure_time)
        previous = self.shown_image
        z_offset = float(self.uv_z_offset or 0.0)
        moved_z = False
        exposed = False
        error = None
        self.should_abort = False
        self._light_on_at = self._light_off_at = None
        self.exposure_phase = "preparing"
        self.set_patterning_busy(True)   # Start disabled, stage locked, STOP/Esc cancel from here on
        try:
            if self.ensure_output is not None and not self.ensure_output():
                return False
            if not self.wait_for_stage_idle(self.stage_idle_timeout):
                if not self.should_abort:
                    self._stage_not_ready()
                return False
            if z_offset:
                self.move_relative({"z": z_offset})
                moved_z = True
                if not self.wait_for_stage_idle(self.stage_idle_timeout):
                    if not self.should_abort:
                        self._stage_not_ready()
                    return False
            if self.should_abort:
                return False
            if self.auto_snapshot_on_uv and self.snapshot_requester and self.camera_image is not None:
                self.snapshot_requester(str(self.snapshot_path("before_exposure", "jpg")))

            self.exposure_phase = "exposing"
            self.set_progress(0.0, 0.0)
            self.exposure_end = time.monotonic() + duration / 1000.0
            self.set_shown_image(ShownImage.PATTERN, force=True)   # picture, then blue LED
            start = time.monotonic()
            self._light_on_at = start
            self._exposure_coords = self.stage_setpoint   # where the light landed (incl. Z offset)
            exposed = True
            if self._projector_error is not None or (self.leds is not None and not self._leds_ok):
                exposed = False
                self._light_off()
                if self._projector_error is not None:
                    detail = f"The pattern could not be shown on the projector ({self._projector_error})."
                else:
                    detail = ("The projector's blue LED could not be switched on over USB, so the exposure would be "
                              "dark. Check the projector's USB cable, then click Settings → Projector → Reconnect "
                              "LEDs (or turn LED control off) and try again.")
                messagebox.showerror("Exposure not started", detail)
                exposed = False
                return False
            end = start + duration / 1000.0
            self.exposure_end = end
            self._light_timer = self.root.after(max(1, int(round(duration))), self._exposure_time_up)
            while True:
                now = time.monotonic()
                if now >= end or self.should_abort or self._light_off_at is not None:
                    break
                self.set_progress(0.0, (now - start) / (end - start))
                self.root.update()
                time.sleep(0.002)
        except StageError:
            pass  # already explained in a dialog
        except Exception as exc:  # e.g. the stage's USB cable came out: report it once the light is off
            traceback.print_exc()
            error = exc
        finally:
            # 1. Never leave the exposure light on, whatever happened above.
            if self._light_timer is not None:
                try:
                    self.root.after_cancel(self._light_timer)
                except Exception:
                    pass
                self._light_timer = None
            aborted = self.should_abort
            try:
                if exposed or aborted or self.shown_image in (ShownImage.PATTERN, ShownImage.UV_FOCUS):
                    self._light_off()
            except Exception as exc:
                print(f"Clearing the projector failed: {exc}")
            # 2. Undo the Z offset before anything else can fail.
            if moved_z:
                try:
                    self.move_relative({"z": -z_offset})
                except Exception as exc:
                    print(f"Z offset not undone: {exc}")
            # 3. Unlock the controls.
            self.exposure_phase = ""
            self.should_abort = False
            self.set_patterning_busy(False)
            # 4. Record what happened.
            if exposed:
                try:
                    self._record_exposure(duration, aborted)
                except Exception as exc:
                    print(f"Exposure not recorded: {exc}")
            # 5. Back to the safe red light you were using (not after STOP: STOP means projector off).
            if previous == ShownImage.RED_FOCUS and not aborted and error is None:
                self.set_shown_image(ShownImage.RED_FOCUS)
        if error is not None:
            messagebox.showerror("Exposure stopped", "Something went wrong, so the exposure was stopped and the "
                                 "projector turned off.\n\n" + (describe_stage_error(error)
                                                                   if isinstance(error, OSError) or "GRBL" in str(error)
                                                                   else f"Details: {error}"))
        return exposed

    def _stage_not_ready(self):
        state = self.hardware.stage.state_name()
        if state.startswith("Alarm"):
            detail = describe_stage_error(RuntimeError(state), state)
        else:
            detail = (f"The controller still reports '{state}' after {self.stage_idle_timeout:g} seconds. Wait for "
                      "the stage to settle and try again. If it stays like this, check the controller.")
        messagebox.showerror("Exposure not started", "The stage has not stopped moving, so the exposure "
                             "was not started.\n\n" + detail)

    def _record_exposure(self, planned_ms: float, aborted: bool):
        on, off = self._light_on_at, self._light_off_at or time.monotonic()
        actual = (off - on) * 1000.0 if on is not None else 0.0
        self.set_progress(1.0, 1.0)
        coords = getattr(self, "_exposure_coords", None) or self.stage_setpoint
        log = ExposureLog(datetime.now(), self.pattern_image_path, coords, round(actual), aborted)
        self.exposure_history.append(log)
        if not self.chip.layers:
            self.chip.layers.append(ChipLayer([]))
        self.chip.layers[-1].exposures.append(log)
        late = actual - planned_ms
        timing = f"{actual / 1000:.2f} s" if abs(late) > 50 or aborted else f"{planned_ms / 1000:g} s"
        self.last_exposure = (("Stopped" if aborted else "Done") + f" · {timing} · "
                              f"{Path(self.pattern_image_path).name} · {datetime.now():%H:%M:%S}")
        self.on_event(Event.CHIP_CHANGED)
        self.on_event(Event.PATTERNING_FINISHED)
        if aborted:
            self.notify("Exposure stopped early. " + self.last_exposure, "warn")
        elif late > 50:
            self.notify(f"Exposure finished {late / 1000:.2f} s late (the window was busy). " + self.last_exposure, "warn")
        else:
            self.notify("Exposure finished. " + self.last_exposure, "ok")

    # ------------------------------------------------------------------ autofocus
    def current_focus_levels(self, blue_only: bool) -> list:
        """Focus scores of the latest frame at every scale (full resolution, 1/2, 1/4, ...).
        A camera without per-scale scores gives a single value."""
        levels = (self.camera_info or {}).get("focus_levels") or {}
        values = levels.get("blue" if blue_only else "red")
        if values:
            return list(values)
        return [self.current_focus(blue_only)]

    def _focus_settle_seconds(self) -> float:
        """How long after the stage stops a frame can still contain light from while it moved:
        the exposure, plus one frame time for the rolling shutter's readout, plus a margin."""
        controls = getattr(self.camera, "controls", {}) or {}
        try:
            exposure = exposure_ms(float(controls.get("exposure", -4))) / 1000.0
        except (TypeError, ValueError):
            exposure = 0.0625
        fps = float((self.camera_info or {}).get("fps") or getattr(self.camera, "measured_fps", 0) or 5.0)
        return min(1.5, exposure + 1.0 / max(fps, 1.0) + 0.05)

    def _measure_focus(self, blue_only: bool, frames: int = 3) -> tuple[list, list]:
        """(median score per scale, frame-to-frame spread per scale) over `frames` fresh frames.

        Frames carry the time they were captured; any that could have been exposed while the stage
        was still moving are skipped. Without capture times, the first frame is skipped instead."""
        settled_at = getattr(self, "_focus_settled_at", 0.0)
        collected, skipped, skip_first = [], 0, True
        while len(collected) < frames:
            if not self.wait_for_frames(1, timeout=4.0):
                raise RuntimeError("the camera stopped sending pictures")
            captured = (self.camera_info or {}).get("captured")
            if captured is not None:
                if captured < settled_at and skipped < 12:
                    skipped += 1
                    continue
            elif skip_first:
                skip_first = False
                continue
            levels = self.current_focus_levels(blue_only)
            if not levels or all(v is None for v in levels):
                raise RuntimeError("no focus measurement from the camera")
            collected.append(levels)
        count = min(len(levels) for levels in collected)
        medians, spreads = [], []
        for level in range(count):
            values = [levels[level] for levels in collected]
            if any(v is None for v in values):
                medians.append(None)
                spreads.append(None)
                continue
            values = sorted(float(v) for v in values)
            medians.append(values[len(values) // 2])
            spreads.append(values[-1] - values[0])
        return medians, spreads

    @staticmethod
    def choose_focus_level(entries: list) -> int:
        """The scale at which focus is easiest to judge: the one where the score changes the most
        across the measured heights, compared with how much it jitters from frame to frame.
        `entries` are (medians, spreads) pairs. On a near tie the finer scale wins (sharper peak)."""
        if not entries:
            return 0
        count = min(len(m) for m, _s in entries)
        best_level, best_snr = 0, -1.0
        for level in range(count):
            values = [m[level] for m, _s in entries]
            spreads = [sp[level] for _m, sp in entries]
            if any(v is None for v in values) or any(v is None for v in spreads):
                continue
            span = max(values) - min(values)
            noise = sorted(spreads)[len(spreads) // 2]
            noise = max(noise, 1e-9 * max(abs(v) for v in values), 1e-12)
            snr = span / noise
            if snr > best_snr * 1.25:
                best_level, best_snr = level, snr
        return best_level

    @staticmethod
    def _peak_of(zs: list, values: list) -> float:
        """Height of the sharpest point: a parabola through the best measurement and its
        neighbours (up to two each side), so the result can fall between the 2 µm steps."""
        i = max(range(len(values)), key=values.__getitem__)
        lo, hi = max(0, i - 2), min(len(values), i + 3)
        if hi - lo < 3:
            return zs[i]
        z = np.array(zs[lo:hi], float)
        v = np.array(values[lo:hi], float)
        a, b, _c = np.polyfit(z - zs[i], v, 2)
        if a >= 0:
            return zs[i]
        step = abs(zs[1] - zs[0]) if len(zs) > 1 else 1.0
        return zs[i] + float(np.clip(-b / (2 * a), -step, step))

    def focus_level_text(self, level: int) -> str:
        crop = ((self.camera_info or {}).get("focus_levels") or {}).get("crop")
        scale = "full resolution" if level == 0 else f"1/{2 ** level} resolution"
        if crop:
            return f"{scale} ({max(1, crop[0] >> level)} × {max(1, crop[1] >> level)} px of the picture's centre)"
        return scale

    def autofocus(self, blue_only, log=False, interactive=False) -> bool:
        """Find the sharpest Z near the current position and move there.

        1. Coarse: the score at the start and 20 µm either side picks the direction, then 10 µm
           steps climb until the score has dropped twice.
        2. Fine: a scan in 2 µm steps over ±12 µm around the best coarse height (widened if the best
           point is at an end), always moving the same way so stage backlash cannot skew it.
        3. The score is measured at every image scale; the scale where focus changes the score the
           most compared with frame-to-frame noise is used (a 20 MP picture at full resolution is
           mostly noise; a heavily shrunk one barely changes with focus).
        4. The peak is interpolated between steps and approached from below, like the scan.
        Never travels more than autofocus_max_travel from the start; if it finds no peak, or is
        stopped, it goes back to the starting Z.
        """
        def tell(message, kind="warn"):
            self.last_autofocus = message
            self.notify(f"Autofocus: {message}", kind)
            if interactive and kind not in ("ok", "muted"):
                messagebox.showinfo("Autofocus", message)

        if not self.camera or self.camera_image is None:
            tell("Autofocus needs a live camera picture. Connect the camera first.")
            return False
        if self.autofocus_busy or self.patterning_busy or self.homing_busy:
            return False
        limit = float(self.autofocus_max_travel)
        coarse: dict[float, tuple] = {}

        def settle_mark():
            self._focus_settled_at = time.monotonic() + self._focus_settle_seconds()

        def move_to(z):
            if self.autofocus_abort:
                raise AutofocusStopped("stopped")
            if abs(z - start_z) > limit + 1e-6:
                raise _OutOfRange()
            self.move_relative({"z": z - self.stage_setpoint[2]})
            self.wait_for_stage_idle(10.0)
            self.non_blocking_delay(0.1)
            settle_mark()

        def measure(store):
            entry = self._measure_focus(blue_only)
            store[round(self.stage_setpoint[2], 3)] = entry
            return entry

        def value(entry, level):
            v = entry[0][level] if level < len(entry[0]) else None
            return float("-inf") if v is None else v

        class _OutOfRange(Exception):
            pass

        self.autofocus_abort = False
        self.set_autofocus_busy(True)
        start_z = self.stage_setpoint[2]
        success = False
        try:
            settle_mark()
            measure(coarse)
            move_to(start_z - 20.0)
            measure(coarse)
            move_to(start_z + 20.0)
            measure(coarse)
            level = self.choose_focus_level(list(coarse.values()))
            mid = value(coarse[round(start_z, 3)], level)
            neg = value(coarse[round(start_z - 20.0, 3)], level)
            pos = value(coarse[round(start_z + 20.0, 3)], level)
            if neg < mid < pos:
                direction = 1.0
            elif neg > mid > pos:
                direction = -1.0
            elif neg < mid and pos < mid:
                direction = 0.0
            else:
                tell("could not tell which way is sharper. Focus roughly by hand first, and show solid red or a "
                     "pattern with sharp edges so the camera has detail to measure.")
                return False
            if direction:
                z, best, last, drops = start_z, mid, mid, 0
                while drops < 2:
                    z += 10.0 * direction
                    move_to(z)          # past the travel limit: _OutOfRange, handled below
                    score = value(measure(coarse), level)
                    drops = drops + 1 if score < last else 0
                    best, last = max(best, score), score
            else:
                for dz in (-10.0, 10.0):
                    move_to(start_z + dz)
                    measure(coarse)
            level = self.choose_focus_level(list(coarse.values()))
            centre = max(coarse, key=lambda key: value(coarse[key], level))

            # Fine scan, always moving up (+Z) so every measurement sees the same backlash.
            fine: dict[float, tuple] = {}
            low = max(centre - 12.0, start_z - limit)
            high = min(centre + 12.0, start_z + limit)

            def scan(z_from, z_to):
                move_to(max(start_z - limit, z_from - 6.0))
                z = z_from
                while z <= z_to + 1e-6:
                    move_to(z)
                    measure(fine)
                    z += 2.0
            scan(low, high)
            for _ in range(2):   # widen when the best point is at an end of the scan
                level = self.choose_focus_level(list(fine.values()))
                zs = sorted(fine)
                top = max(zs, key=lambda key: value(fine[key], level))
                if top >= zs[-1] - 1e-6 and zs[-1] + 2.0 <= start_z + limit:
                    scan(zs[-1] + 2.0, min(zs[-1] + 10.0, start_z + limit))
                elif top <= zs[0] + 1e-6 and zs[0] - 2.0 >= start_z - limit:
                    scan(max(zs[0] - 10.0, start_z - limit), zs[0] - 2.0)
                else:
                    break
            level = self.choose_focus_level(list(fine.values()))
            zs = sorted(fine)
            best_z = self._peak_of(zs, [value(fine[z], level) for z in zs])
            # Approach the result from below, the same way the scan moved.
            move_to(max(start_z - limit, best_z - 6.0))
            move_to(best_z)
            self.focus_level = level
            success = True
            tell(f"done · Z {start_z:,.1f} → {best_z:,.1f} µm · judged at {self.focus_level_text(level)}", "ok")
            return True
        except _OutOfRange:
            tell(f"no sharpest point within ±{limit:g} µm, so the stage went back to the start. "
                 "Focus roughly by hand first.")
        except StageError:
            self.last_autofocus = "stopped: the stage did not move"
        except AutofocusStopped:
            tell("stopped; the stage went back to the starting Z.", "muted")
        except RuntimeError as exc:
            tell(f"stopped: {exc}. The stage went back to the starting Z.")
        finally:
            if not success and abs(self.stage_setpoint[2] - start_z) > 1e-6:
                try:
                    self.move_relative({"z": start_z - self.stage_setpoint[2]})
                except Exception as exc:
                    print(f"Autofocus could not return to the starting Z: {exc}")
            self.autofocus_abort = False
            self.set_autofocus_busy(False)
            self.on_event(Event.AUTOFOCUS_FINISHED)
        return False

    # ------------------------------------------------------------------ alignment
    def initialize_alignment(self, config: LithographerConfig):
        self.config = config
        self.realtime_detection = config.alignment.enabled
        if not Path(config.alignment.model_path).exists():
            if config.alignment.enabled:
                print(f"Alignment detection unavailable: model file {config.alignment.model_path!r} not found.")
            return
        yolo = load_yolo()
        if yolo is None:
            print("Alignment detection unavailable: the 'ultralytics' package is not installed in this build.")
            return
        try:
            self.model = yolo(config.alignment.model_path)
        except Exception as e:
            print(f"Failed to load YOLO model: {e}")
