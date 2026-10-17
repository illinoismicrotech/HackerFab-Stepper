"""Live camera picture: the main view, a real fullscreen window, the toolbar under the view
and the camera adjustments window."""
from __future__ import annotations

import time
import tkinter as tk
from typing import Optional

import cv2
import numpy as np
import ttkbootstrap as ttk
from PIL import Image, ImageTk

from camera.auto_exposure import exposure_ms
from camera.dshow_controls import merged_ranges
from camera.modes import Mode, dedupe_resolutions, preset_modes
from camera.webcam import setting_flag, setting_int
from dispatcher import Event, ShownImage
from projector_window import list_displays
from ui_kit import Badge, Segmented, ValueSlider, add_tooltip
from ui_theme import palette, px


class FocusTracker:
    """Focus score with a slowly decaying peak, shared by every camera view."""

    def __init__(self):
        self.value: Optional[float] = None
        self.peak = 0.0

    def update(self, value: Optional[float]):
        self.value = value
        if value is not None:
            self.peak = max(float(value), self.peak * 0.997)

    def reset(self):
        self.peak = self.value or 0.0

    def fraction(self) -> float:
        if not self.value or self.peak <= 0:
            return 0.0
        return max(0.0, min(1.0, self.value / self.peak))


def fit_inside(width, height, box_w, box_h):
    scale = min(box_w / max(1, width), box_h / max(1, height))
    return max(1, int(width * scale)), max(1, int(height * scale))


class CameraCanvas(tk.Canvas):
    """Draws a camera frame scaled to fit, plus overlays (status chip, focus bar, crosshair)."""

    def __init__(self, parent, app, big=False):
        c = palette()
        super().__init__(parent, background="#000000" if big else c["canvas"], highlightthickness=0, bd=0)
        self.app = app
        self.big = big
        self.image = None
        self.info: dict = {}
        self.photo = None
        self.message: Optional[tuple[str, str]] = ("Starting camera…", "")
        self.crosshair = True
        self._pending = False
        self.image_item = self.create_image(0, 0, anchor="nw")
        self.bind("<Configure>", lambda _e: self.render())

    def set_frame(self, image, info):
        self.image, self.info, self.message = image, info or {}, None
        if not self._pending:
            self._pending = True
            self.after_idle(self._render_pending)

    def _render_pending(self):
        self._pending = False
        self.render()

    def set_message(self, title: str, detail: str = ""):
        self.image, self.message = None, (title, detail)
        self.render()

    def render(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        self.delete("overlay")
        c = palette()
        if self.image is None:
            self.itemconfigure(self.image_item, image="")
            self.photo = None
            title, detail = self.message or ("No camera picture", "")
            self.create_text(w // 2, h // 2 - px(14), text=title, fill=c["text"], font="StepperBig",
                             tags="overlay", width=max(100, w - px(60)), justify="center")
            if detail:
                self.create_text(w // 2, h // 2 + px(16), text=detail, fill=c["muted"], font="StepperSmall",
                                 tags="overlay", width=max(100, w - px(80)), justify="center", anchor="n")
            return
        ih, iw = self.image.shape[:2]
        tw, th = fit_inside(iw, ih, w, h)
        interpolation = cv2.INTER_AREA if tw < iw else cv2.INTER_LINEAR
        frame = cv2.resize(self.image, (tw, th), interpolation=interpolation) if (tw, th) != (iw, ih) else self.image
        self.photo = ImageTk.PhotoImage(Image.fromarray(np.ascontiguousarray(frame)), master=self)
        x0, y0 = (w - tw) // 2, (h - th) // 2
        self.coords(self.image_item, x0, y0)
        self.itemconfigure(self.image_item, image=self.photo)
        self._draw_overlays(x0, y0, tw, th)

    def _chip(self, x, y, text, anchor="nw", color=None, font="StepperSmall"):
        c = palette()
        item = self.create_text(x, y, text=text, fill=color or c["text"], font=font, anchor=anchor, tags="overlay")
        x1, y1, x2, y2 = self.bbox(item)
        pad = px(6)
        rect = self.create_rectangle(x1 - pad, y1 - px(3), x2 + pad, y2 + px(3), fill="#000000", outline="",
                                     tags="overlay")
        self.tag_lower(rect, item)
        return x1 - pad, y1 - px(3), x2 + pad, y2 + px(3)

    def _draw_overlays(self, x0, y0, w, h):
        c = palette()
        info = self.info
        margin = px(10)
        full = info.get("full_size")
        fps = info.get("fps") or 0.0
        left = "● LIVE"
        if full:
            left += f"   {full[0]} × {full[1]}"
        if fps:
            left += f"  ·  {fps:.1f} fps"
        self._chip(x0 + margin, y0 + margin, left, color=c["ok"])
        # Focus bar, top right
        tracker = self.app.focus_tracker
        bar_w, bar_h, gap, pad = px(140 if self.big else 90), px(8), px(8), px(6)
        right = x0 + w - margin
        label = "FOCUS" if tracker.value is None else f"FOCUS  {tracker.value:.3g}"
        text = self.create_text(right - pad - bar_w - gap, y0 + margin, text=label, fill=c["text"],
                                font="StepperSmall", anchor="ne", tags="overlay")
        x1, y1, x2, y2 = self.bbox(text)
        rect = self.create_rectangle(x1 - pad, y1 - px(3), right, y2 + px(3), fill="#000000", outline="",
                                     tags="overlay")
        self.tag_lower(rect, text)
        bx1, bx2 = x2 + gap, right - pad
        by1 = (y1 + y2) // 2 - bar_h // 2
        frac = tracker.fraction()
        color = c["ok"] if frac > 0.9 else (c["warn"] if frac > 0.6 else c["bad"])
        self.create_rectangle(bx1, by1, bx2, by1 + bar_h, fill="#30363d", outline="", tags="overlay")
        if frac > 0:
            self.create_rectangle(bx1, by1, bx1 + (bx2 - bx1) * frac, by1 + bar_h, fill=color, outline="",
                                  tags="overlay")
        if self.crosshair:
            cx, cy = x0 + w // 2, y0 + h // 2
            gap, arm = px(8), max(px(30), min(w, h) // 6)
            for coords in ((cx - arm, cy, cx - gap, cy), (cx + gap, cy, cx + arm, cy),
                           (cx, cy - arm, cx, cy - gap), (cx, cy + gap, cx, cy + arm)):
                self.create_line(*coords, fill="#00e0c6", width=max(1, px(1)), tags="overlay")
        light = self.app.dispatcher.shown_image
        if light == ShownImage.PATTERN and self.app.dispatcher.patterning_busy:
            self._chip(x0 + margin, y0 + h - margin, "EXPOSING", anchor="sw", color=c["uv"], font="StepperBold")


class CameraView(ttk.Frame):
    """The camera picture on the Operate page. Double-click for fullscreen."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas = CameraCanvas(self, app)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Double-Button-1>", lambda _e: app.open_camera_fullscreen())
        # No hover tooltip here: it would cover the picture you are looking at. The keys are in
        # the status line at the bottom of the window.


class CameraFullscreen:
    """A real fullscreen window with the camera picture (covers the screen the app is on)."""

    def __init__(self, app):
        self.app = app
        root = app.root
        self.window = tk.Toplevel(root, background="black")
        self.window.title("Camera · fullscreen")
        display = self._display_of(root)
        if display is not None:
            self.window.geometry(f"{display.width}x{display.height}+{display.x}+{display.y}")
        self.window.update_idletasks()
        try:
            self.window.attributes("-fullscreen", True)
        except tk.TclError:
            pass
        try:
            self.window.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.canvas = CameraCanvas(self.window, app, big=True)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.crosshair = app.camera_view.canvas.crosshair
        self.canvas.image, self.canvas.info = app.camera_view.canvas.image, app.camera_view.canvas.info
        self.canvas.message = app.camera_view.canvas.message
        c = palette()
        self.exit_button = tk.Label(self.window, text="✕  Exit fullscreen  (Esc)", font="StepperBold",
                                    background="#000000", foreground=c["text"], padx=px(12), pady=px(6),
                                    cursor="hand2")
        self.exit_button.place(relx=0.5, y=px(10), anchor="n")
        self.exit_button.bind("<Button-1>", lambda _e: self.close())
        self.hint = tk.Label(self.window, text="Arrows: move X/Y   ·   Page Up / Page Down: focus   ·   "
                                               "Shift: 10× steps   ·   Esc: stop / exit   ·   double-click: exit",
                             font="StepperSmall", background="#000000", foreground=c["muted"], padx=px(10), pady=px(4))
        self.hint.place(relx=0.5, rely=1.0, y=-px(10), anchor="s")
        self.window.bind("<Escape>", self._escape)
        self.canvas.bind("<Double-Button-1>", lambda _e: self.close())
        self.window.bind("<KeyPress>", app.handle_key)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.after(50, self._focus)
        self.canvas.render()

    def _escape(self, _event=None):
        # Esc means STOP while something runs (the STOP button is hidden behind this window).
        if self.app.dispatcher.busy_text():
            self.app.stop_everything()
        else:
            self.close()

    @staticmethod
    def _display_of(root):
        try:
            displays = list_displays(root)
            x = root.winfo_rootx() + root.winfo_width() // 2
            y = root.winfo_rooty() + root.winfo_height() // 2
            for display in displays:
                if display.x <= x < display.x + display.width and display.y <= y < display.y + display.height:
                    return display
            return displays[0] if displays else None
        except Exception:
            return None

    def _focus(self):
        try:
            self.window.lift()
            self.window.focus_force()
        except tk.TclError:
            pass

    @property
    def is_open(self):
        return self.window is not None

    def set_frame(self, image, info):
        if self.window is not None:
            self.canvas.set_frame(image, info)

    def set_message(self, title, detail=""):
        if self.window is not None:
            self.canvas.set_message(title, detail)

    def close(self):
        if self.window is None:
            return
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.window = None
        self.app.camera_fullscreen_closed()


class CameraBar(ttk.Frame):
    """Toolbar under the camera picture."""

    HIGHEST = "Highest resolution"

    def __init__(self, parent, app):
        super().__init__(parent, padding=(0, px(6), 0, 0))
        self.app = app
        self.columnconfigure(1, weight=1)
        self.badge = Badge(self, "CAMERA", "off")
        self.badge.grid(row=0, column=0, padx=(0, px(8)))
        self.status = ttk.Label(self, text="", style="Small.TLabel", anchor="w")
        self.status.grid(row=0, column=1, sticky="ew")
        self.resolution_var = tk.StringVar(value=self.HIGHEST)
        self.resolution = ttk.Combobox(self, textvariable=self.resolution_var, state="readonly", width=24,
                                       postcommand=self._fill_resolutions)
        self.resolution.grid(row=0, column=2, padx=(px(8), 0))
        self.resolution.bind("<<ComboboxSelected>>", self._resolution_chosen)
        self.cross_var = tk.BooleanVar(value=True)
        self.cross = ttk.Checkbutton(self, text="Crosshair", variable=self.cross_var, bootstyle="round-toggle",
                                     command=self._crosshair)
        self.cross.grid(row=0, column=3, padx=(px(10), 0))
        self.fullscreen = ttk.Button(self, text="⛶ Fullscreen", bootstyle="dark", command=app.open_camera_fullscreen)
        self.fullscreen.grid(row=0, column=4, padx=(px(8), 0))
        self.snapshot = ttk.Button(self, text="Snapshot", bootstyle="dark", command=app.take_snapshot)
        self.snapshot.grid(row=0, column=5, padx=(px(6), 0))
        self.adjust = ttk.Button(self, text="Adjust…", bootstyle="dark", command=app.open_camera_adjustments)
        self.adjust.grid(row=0, column=6, padx=(px(6), 0))
        self.reconnect = ttk.Button(self, text="Reconnect", bootstyle="warning", command=app.reconnect_camera)
        self.reconnect.grid(row=0, column=7, padx=(px(6), 0))
        self.reconnect.grid_remove()
        self._modes: list = []
        add_tooltip(self.resolution, "Camera resolution. Highest gives the most detail; the camera then runs at "
                                     "its lowest frame rate, which also helps remove projector stripes.")
        add_tooltip(self.fullscreen, "Show the camera picture on the whole screen (Esc to return). "
                                     "Double-clicking the picture does the same.")
        add_tooltip(self.snapshot, "Save the current camera picture at full resolution to the snapshot folder.")
        add_tooltip(self.adjust, "Exposure, gain, white balance, stripe filter and the automatic settings.")
        add_tooltip(self.reconnect, "Start the camera again. Close other camera apps first.")

    def _crosshair(self):
        self.app.set_crosshair(self.cross_var.get())

    def set_locked(self, locked: bool):
        """No resolution change or reconnect while an exposure, autofocus or homing runs."""
        state = "disabled" if locked else "readonly"
        self.resolution.configure(state=state)
        self.reconnect.configure(state="disabled" if locked else "normal")

    def _fill_resolutions(self):
        camera = self.app.dispatcher.camera
        modes = list(getattr(camera, "modes", []) or []) or preset_modes()
        self._modes = dedupe_resolutions(modes)
        self.resolution.configure(values=[self.HIGHEST] + [m.label() for m in self._modes])

    def _resolution_chosen(self, _event=None):
        choice = self.resolution_var.get()
        mode = None if choice == self.HIGHEST else next((m for m in self._modes if m.label() == choice), None)
        self.app.change_resolution(mode)

    def show_choice(self, settings: dict):
        if str(settings.get("mode", "max")).lower() in ("manual", "fixed"):
            try:
                mode = Mode(int(settings["width"]), int(settings["height"]), float(settings["fps"]))
                self.resolution_var.set(mode.label())
                return
            except (KeyError, TypeError, ValueError):
                pass
        self.resolution_var.set(self.HIGHEST)

    def update_status(self, state: str, text: str, live: bool):
        kinds = {"streaming": ("LIVE", "ok"), "connecting": ("CONNECTING", "warn"), "error": ("NO CAMERA", "bad"),
                 "disconnected": ("OFF", "off"), "disabled": ("OFF", "off")}
        label, kind = kinds.get(state, ("CAMERA", "off"))
        if state == "streaming" and not live:
            label, kind = "WAITING", "warn"
        self.badge.set(label, kind)
        if state == "streaming" and live:
            # Resolution and frame rate are on the picture itself; keep this line short.
            text = text.split(" · ", 3)[-1] if text.startswith("Connected") else text
        lines = (text or "").splitlines()
        # One line here; the full explanation is shown on the camera picture.
        self.status.configure(text=lines[0] if lines else "")
        needs = state == "error" or (state == "streaming" and not live)
        if needs and not self.reconnect.winfo_ismapped():
            self.reconnect.grid()
        elif not needs and self.reconnect.winfo_ismapped() and state != "disabled":
            self.reconnect.grid_remove()
        self.snapshot.configure(state="normal" if live else "disabled")


def exposure_label(value: float) -> str:
    return f"{int(value)}  ·  {exposure_ms(value):.3g} ms"


class CameraAdjustDialog:
    """Camera settings that change live. Manual changes switch the automatic setting off."""

    def __init__(self, app):
        self.app = app
        d = app.dispatcher
        self.window = tk.Toplevel(app.root)
        self.window.title("Camera adjustments")
        self.window.transient(app.root)
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _e: self.close())
        body = ttk.Frame(self.window, padding=px(16))
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)
        camera = d.camera
        self.ranges = merged_ranges(getattr(camera, "ranges", {}) or {})
        controls = dict(getattr(camera, "controls", {}) or {})
        settings = getattr(camera, "settings", {}) or {}
        row = 0
        ttk.Label(body, text="Automatic", style="Section.TLabel").grid(row=row, column=0, sticky="w")
        row += 1
        self.auto_exposure = tk.BooleanVar(value=d.auto_camera.auto_exposure)
        self.auto_light = tk.BooleanVar(value=d.auto_camera.auto_light)
        ttk.Checkbutton(body, text="Auto exposure and gain (finds the stripe-free exposure)", bootstyle="round-toggle",
                        variable=self.auto_exposure, command=self._auto_changed).grid(row=row, column=0, sticky="w", pady=(px(6), 0))
        row += 1
        ttk.Checkbutton(body, text="Auto red brightness (red stays at 100 %; dimmed only if still too bright)",
                        bootstyle="round-toggle", variable=self.auto_light,
                        command=self._auto_changed).grid(row=row, column=0, sticky="w", pady=(px(6), 0))
        row += 1
        self.status = ttk.Label(body, text="", style="Small.TLabel", wraplength=px(430), justify="left")
        self.status.grid(row=row, column=0, sticky="w", pady=(px(6), 0))
        row += 1
        self.search = ttk.Button(body, text="Find stripe-free exposure", bootstyle="primary-outline",
                                 command=d.start_stripe_search)
        self.search.grid(row=row, column=0, sticky="w", pady=(px(8), px(4)))
        add_tooltip(self.search, "Tries exposures from 16 to 125 ms with red light on, measures the projector lines "
                                 "in each and keeps every exposure that is line-free. Takes about 10 seconds.")
        row += 1
        ttk.Separator(body).grid(row=row, column=0, sticky="ew", pady=px(12))
        row += 1
        ttk.Label(body, text="Manual", style="Section.TLabel").grid(row=row, column=0, sticky="w")
        row += 1
        ttk.Label(body, text="Moving exposure or gain turns auto exposure off.", style="Small.TLabel").grid(
            row=row, column=0, sticky="w")
        row += 1
        self.sliders = {}

        def add(name, label, fmt, step=1, default=None):
            nonlocal row
            info = self.ranges.get(name, {})
            low, high = info.get("min", 0), info.get("max", 100)
            value = controls.get(name, info.get("value", info.get("default", default if default is not None else low)))
            slider = ValueSlider(body, label, low, high, float(value), fmt=fmt, step=step,
                                 on_change=lambda v, n=name: self._manual(n, v))
            slider.grid(row=row, column=0, sticky="ew", pady=(px(8), 0))
            if camera is None or not hasattr(camera, "set_controls"):
                slider.set_enabled(False)
            self.sliders[name] = slider
            row += 1

        add("exposure", "Exposure", exposure_label, default=-4)
        add("gain", "Gain", lambda v: f"{int(v)}")
        add("brightness", "Brightness", lambda v: f"{int(v)}")
        add("contrast", "Contrast", lambda v: f"{int(v)}")
        add("saturation", "Saturation", lambda v: f"{int(v)}")
        add("white_balance", "White balance", lambda v: f"{int(v)} K", step=50, default=4500)
        ttk.Label(body, text="White balance stays fixed so red light looks red (automatic white balance "
                             "turns faint projector light into coloured bands).", style="Small.TLabel",
                  wraplength=px(430), justify="left").grid(row=row, column=0, sticky="w", pady=(px(4), 0))
        row += 1
        ttk.Separator(body).grid(row=row, column=0, sticky="ew", pady=px(12))
        row += 1
        ttk.Label(body, text="Picture", style="Section.TLabel").grid(row=row, column=0, sticky="w")
        row += 1
        self.destripe = tk.BooleanVar(value=setting_flag(settings, "stripe-filter", True))
        ttk.Checkbutton(body, text="Stripe filter (removes leftover projector lines from the live picture)",
                        bootstyle="round-toggle", variable=self.destripe,
                        command=self._processing).grid(row=row, column=0, sticky="w", pady=(px(6), 0))
        row += 1
        avg = ttk.Frame(body)
        avg.grid(row=row, column=0, sticky="ew", pady=(px(8), 0))
        ttk.Label(avg, text="Frame averaging").grid(row=0, column=0, sticky="w", padx=(0, px(12)))
        self.average = Segmented(avg, [("1", "Off"), ("2", "2"), ("4", "4"), ("8", "8")],
                                 tk.StringVar(value=str(setting_int(settings, "average-frames", 1, 1, 16))),
                                 command=lambda _v: self._processing(), padx=12, pady=3)
        self.average.grid(row=0, column=1, sticky="w")
        add_tooltip(self.average, "Average several frames: less noise and flicker, but the picture reacts more slowly.")
        row += 1
        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, sticky="ew", pady=(px(16), 0))
        ttk.Button(buttons, text="Reset", bootstyle="dark", command=self.reset).pack(side="left")
        ttk.Button(buttons, text="Save as default", bootstyle="primary", command=self.save).pack(side="left", padx=px(8))
        ttk.Button(buttons, text="Close", bootstyle="dark", command=self.close).pack(side="right")
        self.listener = lambda: self.refresh()
        d.add_event_listener(Event.CAMERA_AUTO_CHANGED, self.listener)
        self.refresh()
        self.window.update_idletasks()
        x = app.root.winfo_rootx() + app.root.winfo_width() - self.window.winfo_reqwidth() - px(40)
        y = app.root.winfo_rooty() + px(80)
        self.window.geometry(f"+{max(0, x)}+{max(0, y)}")

    @property
    def is_open(self):
        return self.window is not None

    def lift(self):
        if self.window is not None:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()

    def refresh(self):
        if self.window is None:
            return
        d = self.app.dispatcher
        auto = d.auto_camera
        self.auto_exposure.set(auto.auto_exposure)
        self.auto_light.set(auto.auto_light)
        status = auto.status
        if auto.summary:
            status += "\n" + auto.summary
        self.status.configure(text=status)
        controls = getattr(d.camera, "controls", {}) or {}
        for name, slider in self.sliders.items():
            if name in controls:
                try:
                    slider.set(float(controls[name]))
                except (TypeError, ValueError):
                    pass

    def _auto_changed(self):
        self.app.dispatcher.configure_auto_camera(self.auto_exposure.get(), self.auto_light.get())

    def _manual(self, name, value):
        d = self.app.dispatcher
        camera = d.camera
        if camera is None or not hasattr(camera, "set_controls"):
            return
        if name in ("exposure", "gain") and d.auto_camera.auto_exposure:
            d.configure_auto_camera(auto_exposure=False)
        camera.set_controls({name: int(round(value))})
        if hasattr(camera, "settings"):
            key = name.replace("_", "-")
            camera.settings[key] = int(round(value))

    def _processing(self):
        camera = self.app.dispatcher.camera
        if camera is None or not hasattr(camera, "set_processing"):
            return
        average = int(self.average.get())
        camera.set_processing(destripe=self.destripe.get(), average=average)
        camera.settings["stripe-filter"] = self.destripe.get()
        camera.settings["average-frames"] = average

    def reset(self):
        d = self.app.dispatcher
        camera = d.camera
        defaults = {"exposure": -4, "gain": self.ranges.get("gain", {}).get("min", 100),
                    "brightness": self.ranges.get("brightness", {}).get("default", 0),
                    "contrast": self.ranges.get("contrast", {}).get("default", 10),
                    "saturation": self.ranges.get("saturation", {}).get("default", 10), "white_balance": 4500}
        if camera is not None and hasattr(camera, "set_controls"):
            camera.set_controls(defaults)
        self.destripe.set(True)
        self.average.set("1")
        self._processing()
        d.configure_auto_camera(True, True)
        self.refresh()

    def save(self):
        d = self.app.dispatcher
        cam = self.app.config.setdefault("camera", {})
        controls = getattr(d.camera, "controls", {}) or {}
        cam["exposure"] = "auto" if d.auto_camera.auto_exposure else int(round(controls.get("exposure", -4)))
        for name in ("gain", "brightness", "contrast", "saturation"):
            if name in controls and not (name == "gain" and d.auto_camera.auto_exposure):
                cam[name] = int(round(controls[name]))
        if "white_balance" in controls:
            cam["white-balance"] = int(round(controls["white_balance"]))
        cam["stripe-filter"] = self.destripe.get()
        cam["average-frames"] = int(self.average.get())
        cam["auto-light"] = d.auto_camera.auto_light
        found = (getattr(d.camera, "settings", {}) or {}).get("stripe-free-exposure")
        if found is not None:
            cam["stripe-free-exposure"] = int(found)
        self.app.save_settings("Camera settings saved as the default.")

    def close(self):
        if self.window is None:
            return
        listeners = self.app.dispatcher.listeners.get(Event.CAMERA_AUTO_CHANGED, [])
        if self.listener in listeners:
            listeners.remove(self.listener)
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.window = None
