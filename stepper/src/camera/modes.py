"""Capture modes: what the camera offers, known Arducam sensor modes, and the default choice.

The default is the camera's highest resolution at its lowest frame rate (more detail, less
USB bandwidth, and room for the long exposures that remove projector stripes).
"""
from __future__ import annotations

import platform
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Mode:
    width: int
    height: int
    fps: float
    fourcc: str = "YUY2"
    source: str = ""  # "camera" (advertised by the device) or a sensor name for presets

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def megapixels(self) -> float:
        return self.pixels / 1e6

    def label(self) -> str:
        mp = f"{self.megapixels:.1f} MP" if self.megapixels >= 1 else f"{self.megapixels:.2f} MP"
        return f"{self.width} × {self.height} · {self.fps:g} fps · {mp}"

    def key(self) -> str:
        return f"{self.width}x{self.height}@{self.fps:g}"


# Arducam USB 3 UVC modules (YUY2). B0477 = IMX283 20 MP; also the IMX477 12 MP and IMX519 16 MP boards.
SENSOR_PRESETS: dict[str, list[tuple[int, int, float]]] = {
    "IMX283": [(5472, 3648, 9), (3840, 2160, 20), (2720, 1536, 40), (1920, 1080, 60), (1280, 720, 120)],
    "IMX519": [(4656, 3496, 9), (3840, 2160, 20), (2320, 1748, 40), (1920, 1080, 60), (1280, 720, 120)],
    "IMX477": [(4056, 3040, 10), (3840, 2160, 20), (2016, 1520, 50), (1920, 1080, 60), (1280, 720, 120)],
}
USB2_MODE = Mode(1280, 720, 10, "YUY2", "USB 2")


def preset_modes() -> list[Mode]:
    """Every known sensor mode, highest resolution first, without duplicates."""
    seen, modes = set(), []
    for sensor, entries in SENSOR_PRESETS.items():
        for w, h, fps in entries:
            if (w, h, fps) not in seen:
                seen.add((w, h, fps))
                modes.append(Mode(w, h, fps, "YUY2", sensor))
    modes.append(USB2_MODE)
    return sorted(modes, key=lambda m: (-m.pixels, m.fps))


def best_mode(modes: list[Mode]) -> Mode | None:
    """Highest resolution, then the lowest frame rate offered at that resolution."""
    if not modes:
        return None
    return min(modes, key=lambda m: (-m.pixels, m.fps))


def ordered_candidates(modes: list[Mode]) -> list[Mode]:
    """Modes to try in order: highest resolution first, lowest fps first within a resolution."""
    unique = {(m.width, m.height, round(m.fps, 2)): m for m in modes}
    return sorted(unique.values(), key=lambda m: (-m.pixels, m.fps))


def dedupe_resolutions(modes: list[Mode]) -> list[Mode]:
    """One entry per resolution (its lowest frame rate), highest resolution first."""
    best: dict[tuple[int, int], Mode] = {}
    for mode in modes:
        current = best.get((mode.width, mode.height))
        if current is None or mode.fps < current.fps:
            best[(mode.width, mode.height)] = mode
    return sorted(best.values(), key=lambda m: -m.pixels)


def dshow_modes(index: int) -> list[Mode]:
    """Modes a DirectShow camera advertises. Each capability has a frame-rate range; both
    ends are listed (the low end is what 'lowest frame rate' uses)."""
    if platform.system() != "Windows":
        return []
    from pygrabber.dshow_graph import FilterGraph

    graph = FilterGraph()
    graph.add_video_input_device(int(index))
    modes = []
    for fmt in graph.get_input_device().get_formats():
        try:
            fourcc = str(fmt.get("media_type_str") or "YUY2").strip().upper()
        except Exception:
            fourcc = "YUY2"
        if fourcc in ("MJPG", "MJPEG"):
            continue  # this pipeline asks for uncompressed frames
        rates = sorted({round(float(fmt["min_framerate"]), 2), round(float(fmt["max_framerate"]), 2)})
        for fps in rates:
            if fps > 0:
                modes.append(Mode(int(fmt["width"]), abs(int(fmt["height"])), fps, "YUY2", "camera"))
    return ordered_candidates(modes)


def modes_from_v4l2(parsed) -> list[Mode]:
    """Convert discovery.parse_modes() output (CaptureMode) to Modes."""
    return ordered_candidates([Mode(m.width, m.height, m.fps, "YUYV" if m.fourcc == "YUYV" else m.fourcc, "camera")
                               for m in parsed if m and m.fourcc in ("YUYV", "YUY2", "MJPG")])


def requested_mode(settings: dict) -> Mode | None:
    """The mode the config asks for, or None for 'highest available'."""
    if str(settings.get("mode", "max")).lower() not in ("manual", "fixed"):
        return None
    try:
        return Mode(int(settings.get("width")), int(settings.get("height")), float(settings.get("fps")),
                    str(settings.get("fourcc", "YUY2")).upper(), "settings")
    except (TypeError, ValueError):
        return None
