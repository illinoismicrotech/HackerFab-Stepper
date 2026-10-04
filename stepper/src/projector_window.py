"""Dedicated projector output window.

The pattern gets its own borderless window that exactly covers the projector's display,
so the controls stay on the main screen. The OS must extend (not mirror) the desktop for the
projector to appear as a separate display; in *Duplicate* mode there is only one display
and the projector just mirrors the laptop screen.
"""
from __future__ import annotations

import platform
import re
import subprocess
import tkinter as tk
from dataclasses import dataclass, replace
from typing import Callable, Optional

from PIL import Image, ImageTk


@dataclass(frozen=True)
class Display:
    number: int          # Windows display number (\\.\DISPLAYn), or position on other systems
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    @property
    def label(self) -> str:
        text = f"Display {self.number} · {self.width} × {self.height}"
        return text + " (main screen)" if self.primary else text


def _windows_displays() -> list[Display]:
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                       ctypes.POINTER(RECT), wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    found: list[Display] = []

    def visit(monitor, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcMonitor
            match = re.search(r"(\d+)$", info.szDevice or "")
            number = int(match.group(1)) if match else len(found) + 1
            found.append(Display(number, r.left, r.top, r.right - r.left, r.bottom - r.top,
                                 bool(info.dwFlags & 1)))  # MONITORINFOF_PRIMARY
        return True

    user32.EnumDisplayMonitors(None, None, callback_type(visit), 0)
    return found


def parse_xrandr_monitors(text: str) -> list[Display]:
    """Parse `xrandr --listmonitors`, e.g. ' 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1'."""
    found = []
    for line in text.splitlines():
        match = re.search(r"^\s*(\d+):\s*\+?(\*?)\S*\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)", line)
        if match:
            index, star, width, height, x, y = match.groups()
            found.append(Display(int(index) + 1, int(x), int(y), int(width), int(height), star == "*"))
    return found


def list_displays(root: tk.Misc) -> list[Display]:
    """All connected displays, main screen first. Never raises; falls back to Tk's screen size."""
    displays: list[Display] = []
    try:
        if platform.system() == "Windows":
            displays = _windows_displays()
        elif platform.system() == "Linux":
            out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True, timeout=3).stdout
            displays = parse_xrandr_monitors(out)
    except Exception as exc:
        print(f"Could not list displays: {exc}")
    if not displays:
        displays = [Display(1, 0, 0, root.winfo_screenwidth(), root.winfo_screenheight(), True)]
    if not any(d.primary for d in displays):
        displays[0] = replace(displays[0], primary=True)
    return sorted(displays, key=lambda d: (not d.primary, d.number))


def choose_projector_display(displays: list[Display], preferred=None) -> Optional[Display]:
    """The saved display if connected, else the first display that is not the main screen."""
    try:
        wanted = int(preferred)
    except (TypeError, ValueError):
        wanted = None
    for display in displays:
        if wanted is not None and display.number == wanted and not display.primary:
            return display
    return next((d for d in displays if not d.primary), None)


class ProjectorWindow:
    """Shows the projector image in its own window.

    On a display: borderless, topmost, exactly covering it, mouse pointer hidden (a visible
    pointer would be projected onto the resist). Without a display: an ordinary movable
    window; double-click it to toggle fullscreen on whichever display it is on.
    """

    def __init__(self, root: tk.Misc, on_escape: Callable[[], None] | None = None,
                 on_close: Callable[[], None] | None = None):
        self.root = root
        self.on_escape = on_escape
        self.on_close = on_close
        self.window: Optional[tk.Toplevel] = None
        self.label: Optional[tk.Label] = None
        self.display: Optional[Display] = None
        self.image: Optional[Image.Image] = None
        self.photo = None

    @property
    def is_open(self) -> bool:
        return self.window is not None

    def open(self, display: Optional[Display], image: Optional[Image.Image] = None):
        self.close(notify=False)
        window = tk.Toplevel(self.root, background="black")
        window.title("HackerFab Stepper – Projector")
        self.label = tk.Label(window, background="black", borderwidth=0, highlightthickness=0)
        self.label.pack(fill="both", expand=True)
        if display is not None:
            window.overrideredirect(True)
            window.geometry(f"{display.width}x{display.height}+{display.x}+{display.y}")
            window.attributes("-topmost", True)
            window.configure(cursor="none")
            self.label.configure(cursor="none")
        else:
            window.geometry("640x360")
            window.protocol("WM_DELETE_WINDOW", self.close)
            window.bind("<Double-Button-1>", self._toggle_fullscreen)
        window.bind("<Escape>", self._escape)
        window.bind("<Configure>", lambda _event: self._render())
        self.window, self.display = window, display
        if image is not None:
            self.image = image
        window.update_idletasks()
        self._render()
        try:
            self.root.focus_force()  # keep keyboard focus (and Esc) on the controls
        except tk.TclError:
            pass

    def show(self, image: Image.Image):
        self.image = image
        if self.window is not None:
            self._render()
            self.window.update_idletasks()  # draw now: the exposure timer starts when show() returns

    def _size(self) -> tuple[int, int]:
        if self.display is not None:
            return self.display.width, self.display.height
        return max(1, self.window.winfo_width()), max(1, self.window.winfo_height())

    def _render(self):
        if self.window is None or self.image is None:
            return
        width, height = self._size()
        image = self.image
        if image.size != (width, height):
            # Fit the whole image without cropping or changing its shape (same as the in-app view).
            ratio = min(width / image.width, height / image.height)
            size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
            image = image.resize(size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image, master=self.window)
        # The theme repaints plain Tk widgets in its own (grey) colours, at creation and on
        # every theme change. Any area the pattern does not fill must stay pure black, or it
        # shines onto the resist.
        self.window.configure(background="black")
        self.label.configure(image=self.photo, background="black")

    def _toggle_fullscreen(self, _event=None):
        if self.window is not None:
            self.window.attributes("-fullscreen", not bool(self.window.attributes("-fullscreen")))

    def _escape(self, _event=None):
        if self.on_escape:
            self.on_escape()
        return "break"

    def close(self, notify: bool = True):
        if self.window is None:
            return
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.window = self.label = self.display = None
        self.photo = None
        if notify and self.on_close:
            self.on_close()
