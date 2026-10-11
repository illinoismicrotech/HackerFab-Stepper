"""Alignment page: shift/rotate the projected image, and alignment-marker detection."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox

import ttkbootstrap as ttk

from dispatcher import Event, MovementLock, detect_alignment_markers
from ui_kit import Card, Segmented, add_tooltip
from ui_theme import px

SHIFT_STEPS = ("1", "5", "20", "100")
ANGLE_STEPS = ("0.05", "0.2", "1")


class AlignmentPage(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        d = self.d = app.dispatcher
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text="Alignment", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="Line the projected image up with features already on the chip.", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(px(2), px(14)))

        card = Card(self, "Image position on the projector")
        card.grid(row=2, column=0, sticky="ew")
        body = card.body
        body.columnconfigure(0, weight=0)
        body.columnconfigure(8, weight=1)
        ttk.Label(body, text="Moves the pattern, the red light and the UV mark together. Units are projector "
                             "pixels and degrees; + rotation is counter-clockwise.", style="Small.TLabel",
                  wraplength=px(640)).grid(
            row=0, column=0, columnspan=8, sticky="w")
        self.vars = {}
        for column, (key, label, unit) in enumerate((("x", "X shift", "px"), ("y", "Y shift", "px"),
                                                     ("t", "Rotation", "°"))):
            ttk.Label(body, text=label).grid(row=1, column=column * 2, sticky="w", pady=(px(10), 0),
                                             padx=(0 if column == 0 else px(16), px(6)))
            var = tk.StringVar(value="0")
            entry = ttk.Entry(body, textvariable=var, width=8, justify="right")
            entry.grid(row=1, column=column * 2 + 1, sticky="w", pady=(px(10), 0))
            entry.bind("<Return>", lambda _e: self.apply())
            self.vars[key] = var
        self.apply_button = ttk.Button(body, text="Apply", bootstyle="primary", command=self.apply)
        self.apply_button.grid(row=1, column=6, padx=(px(16), 0), pady=(px(10), 0))
        self.reset_button = ttk.Button(body, text="Reset", bootstyle="dark", command=self.reset)
        self.reset_button.grid(row=1, column=7, padx=(px(6), 0), pady=(px(10), 0))
        nudge = ttk.Frame(body)
        nudge.grid(row=2, column=0, columnspan=8, sticky="w", pady=(px(14), 0))
        ttk.Label(nudge, text="NUDGE", style="Caption.TLabel").grid(row=0, column=0, sticky="w", padx=(0, px(10)))
        self.shift_step = tk.StringVar(value="5")
        Segmented(nudge, [(s, f"{s} px") for s in SHIFT_STEPS], self.shift_step, padx=6, pady=3).grid(row=0, column=1)
        self.buttons = []
        for column, (text, dx, dy) in enumerate((("◀", -1, 0), ("▶", 1, 0), ("▲", 0, -1), ("▼", 0, 1)), start=2):
            button = ttk.Button(nudge, text=text, width=3, bootstyle="dark",
                                command=lambda a=dx, b=dy: self.nudge(a, b, 0))
            button.grid(row=0, column=column, padx=(px(6) if column == 2 else px(2), 0))
            self.buttons.append(button)
        self.angle_step = tk.StringVar(value="0.2")
        Segmented(nudge, [(s, f"{s}°") for s in ANGLE_STEPS], self.angle_step, padx=6, pady=3).grid(
            row=0, column=6, padx=(px(18), 0))
        # Positive angles turn the image counter-clockwise (as it appears in the projector output).
        for column, (text, sign, tip) in enumerate((("⟲", 1, "Rotate counter-clockwise"),
                                                    ("⟳", -1, "Rotate clockwise")), start=7):
            button = ttk.Button(nudge, text=text, width=3, bootstyle="dark",
                                command=lambda s=sign: self.nudge(0, 0, s))
            button.grid(row=0, column=column, padx=(px(6) if column == 7 else px(2), 0))
            add_tooltip(button, tip + " (as seen in the projector output).")
            self.buttons.append(button)
        self.buttons += [self.apply_button, self.reset_button]

        markers = Card(self, "Alignment markers")
        markers.grid(row=3, column=0, sticky="ew", pady=(px(14), 0))
        mbody = markers.body
        if d.model is None:
            ttk.Label(mbody, text="Automatic marker detection is not available in this build (it needs the "
                                  "ultralytics/PyTorch packages and a trained model). Align by eye with the red "
                                  "pattern preview instead.", style="Small.TLabel", wraplength=px(640),
                      justify="left").grid(row=0, column=0, sticky="w")
        else:
            self.realtime = tk.BooleanVar(value=d.realtime_detection)
            ttk.Checkbutton(mbody, text="Show detected markers on the camera picture", variable=self.realtime,
                            bootstyle="round-toggle",
                            command=lambda: setattr(d, "realtime_detection", self.realtime.get())).grid(
                row=0, column=0, sticky="w")
            self.align_button = ttk.Button(mbody, text="Auto-align stage", bootstyle="primary", command=self.auto_align)
            self.align_button.grid(row=1, column=0, sticky="w", pady=(px(10), 0))
            self.buttons.append(self.align_button)

        options = Card(self, "Automatic focus")
        options.grid(row=4, column=0, sticky="ew", pady=(px(14), 0))
        self.af_switch = tk.BooleanVar(value=d.autofocus_on_mode_switch)
        ttk.Checkbutton(options.body, text="Autofocus automatically when tiling switches between red and exposure",
                        variable=self.af_switch, bootstyle="round-toggle",
                        command=lambda: setattr(d, "autofocus_on_mode_switch", self.af_switch.get())).grid(
            row=0, column=0, sticky="w")
        d.add_event_listener(Event.IMAGE_ADJUST_CHANGED, self.refresh)
        d.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, self.refresh_lock)
        self.refresh()

    def _values(self):
        x, y, t = self.d.image_adjust_position
        return {"x": x, "y": y, "t": t}

    def refresh(self):
        for key, value in self._values().items():
            self.vars[key].set(f"{value:g}")

    def refresh_lock(self):
        enabled = self.d.movement_lock == MovementLock.UNLOCKED
        for button in self.buttons:
            button.configure(state="normal" if enabled else "disabled")

    def apply(self):
        try:
            values = {k: float(v.get().replace(",", ".")) for k, v in self.vars.items()}
            if not all(math.isfinite(v) for v in values.values()):
                raise ValueError
        except ValueError:
            messagebox.showerror("Image position", "Enter a number for X, Y and rotation.")
            return
        self.d.set_image_position(int(round(values["x"])), int(round(values["y"])), values["t"])
        self.app.notify("Image position updated.", "ok")

    def reset(self):
        self.d.set_image_position(0, 0, 0.0)

    def nudge(self, dx, dy, dt):
        x, y, t = self.d.image_adjust_position
        shift = float(self.shift_step.get())
        angle = float(self.angle_step.get())
        self.d.set_image_position(int(round(x + dx * shift)), int(round(y + dy * shift)), round(t + dt * angle, 4))

    def auto_align(self):
        d = self.d
        if d.camera_image is None or d.model is None:
            messagebox.showinfo("Alignment unavailable", "Connect a live camera and load an alignment model first.")
            return
        h, w, _ = d.camera_image.shape
        markers, _ = detect_alignment_markers(d.model, d.camera_image)
        if not markers:
            self.app.notify("No alignment markers found in the camera picture.", "warn")
            return
        alignment = d.config.alignment
        dx = dy = 0.0
        for (x0, y0), (x1, y1) in markers:
            x = (x0 + x1) / 2 / w
            y = (y0 + y1) / 2 / h
            dx += alignment.x_scale_factor * ((alignment.right_marker_x if x > 0.5 else alignment.left_marker_x) / w - x)
            dy += alignment.y_scale_factor * ((alignment.bottom_marker_y if y > 0.5 else alignment.top_marker_y) / h - y)
        dx /= len(markers)
        dy /= len(markers)
        d.move_relative({"x": dx, "y": dy})
        self.app.notify(f"Auto-align moved the stage by X {dx:.1f}, Y {dy:.1f} µm ({len(markers)} markers).", "ok")
