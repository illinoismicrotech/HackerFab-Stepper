"""Read a DirectShow camera's control ranges (exposure, gain, brightness, ...) on Windows.

OpenCV can set these controls but cannot report their ranges, which the auto exposure and
the sliders need. This opens the same camera through DirectShow's device enumerator (no
streaming) and asks IAMCameraControl / IAMVideoProcAmp. Everything fails soft: on other
systems or errors it returns {} and callers fall back to DEFAULT_RANGES.
"""
from __future__ import annotations

import platform

AUTO, MANUAL = 1, 2

# name -> (interface, property id)
CONTROLS = {
    "brightness": ("procamp", 0), "contrast": ("procamp", 1), "saturation": ("procamp", 3),
    "sharpness": ("procamp", 4), "gamma": ("procamp", 5), "white_balance": ("procamp", 7),
    "gain": ("procamp", 9), "exposure": ("camera", 4), "focus": ("camera", 6),
}

# Arducam IMX283 (B0477) values read from a real camera; used when ranges can't be read.
DEFAULT_RANGES = {
    "exposure": {"min": -11, "max": -3, "default": -11, "step": 1, "auto": True},
    "gain": {"min": 100, "max": 2239, "default": 100, "step": 1, "auto": False},
    "brightness": {"min": -64, "max": 64, "default": 0, "step": 1, "auto": False},
    "contrast": {"min": 0, "max": 100, "default": 10, "step": 1, "auto": False},
    "saturation": {"min": 0, "max": 100, "default": 10, "step": 1, "auto": False},
    "white_balance": {"min": 2300, "max": 6500, "default": 4500, "step": 1, "auto": True},
}


def _interfaces():
    from ctypes import POINTER, c_long
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    def methods():
        return [
            COMMETHOD([], HRESULT, "GetRange", (["in"], c_long, "Property"),
                      (["out"], POINTER(c_long), "pMin"), (["out"], POINTER(c_long), "pMax"),
                      (["out"], POINTER(c_long), "pSteppingDelta"), (["out"], POINTER(c_long), "pDefault"),
                      (["out"], POINTER(c_long), "pCapsFlags")),
            COMMETHOD([], HRESULT, "Set", (["in"], c_long, "Property"), (["in"], c_long, "lValue"),
                      (["in"], c_long, "Flags")),
            COMMETHOD([], HRESULT, "Get", (["in"], c_long, "Property"),
                      (["out"], POINTER(c_long), "lValue"), (["out"], POINTER(c_long), "Flags")),
        ]

    class IAMVideoProcAmp(IUnknown):
        _iid_ = GUID("{C6E13360-30AC-11d0-A18C-00A0C9118956}")
        _methods_ = methods()

    class IAMCameraControl(IUnknown):
        _iid_ = GUID("{C6E13370-30AC-11d0-A18C-00A0C9118956}")
        _methods_ = methods()

    return {"procamp": IAMVideoProcAmp, "camera": IAMCameraControl}


def read_controls(index: int) -> dict:
    """{name: {min, max, default, step, auto(bool: supports auto), value, is_auto}} for supported controls."""
    if platform.system() != "Windows":
        return {}
    try:
        from pygrabber.dshow_graph import SystemDeviceEnum
        from pygrabber.dshow_ids import DeviceCategories
        flt, _name = SystemDeviceEnum().get_filter_by_index(DeviceCategories.VideoInputDevice, int(index))
        interfaces = _interfaces()
    except Exception as exc:
        print(f"Camera control ranges unavailable: {exc}")
        return {}
    found = {}
    for name, (kind, prop) in CONTROLS.items():
        try:
            ctl = flt.QueryInterface(interfaces[kind])
            low, high, step, default, caps = ctl.GetRange(prop)
            value, flags = ctl.Get(prop)
        except Exception:
            continue  # not supported by this camera
        found[name] = {"min": low, "max": high, "default": default, "step": max(1, step),
                       "auto": bool(caps & AUTO), "value": value, "is_auto": bool(flags & AUTO)}
    return found


def merged_ranges(read: dict) -> dict:
    """Read ranges where available, defaults otherwise."""
    ranges = {k: dict(v) for k, v in DEFAULT_RANGES.items()}
    for name, info in (read or {}).items():
        ranges[name] = {**ranges.get(name, {}), **info}
    return ranges
