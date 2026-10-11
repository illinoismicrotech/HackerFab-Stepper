"""Stage controls: a direction pad with hold-to-repeat, focus buttons, step sizes, position
readout, keyboard shortcuts and a Go to… dialog."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox

import ttkbootstrap as ttk

from camera.webcam import setting_flag
from dispatcher import Event, MovementLock
from stage_control.grbl_stage import GrblStage
from ui_kit import HoldButton, Segmented, add_tooltip
from ui_theme import palette, px

XY_STEPS = (1, 10, 50, 100, 500, 1000)
Z_STEPS = (1, 2, 5, 10, 50)
DEFAULT_PRESETS = {
    "Chip origin": (-14500.0, -13500.0, -13844.0),
    "Load / unload": (-14500.0, -14500.0, -14500.0),
}
ENTRY_CLASSES = ("Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Text", "Listbox", "TScale", "Scale",
                 "Treeview", "TNotebook")


def presets_from_config(config: dict) -> dict:
    presets = dict(DEFAULT_PRESETS)
    raw = (config.get("stage", {}) or {}).get("presets", {}) or {}
    for name, value in raw.items():
        try:
            x, y, z = (float(v) for v in value)
            presets[str(name)] = (x, y, z)
        except (TypeError, ValueError):
            print(f"Ignoring stage preset {name!r}: expected [x, y, z]")
    return presets


class StagePanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.d = app.dispatcher
        stage_cfg = app.config.get("stage", {}) or {}
        self.invert = {axis: setting_flag(stage_cfg, f"jog-invert-{axis}", False) for axis in "xyz"}
        self.xy_step = tk.StringVar(value=str(stage_cfg.get("xy-step", 10)))
        self.z_step = tk.StringVar(value=str(stage_cfg.get("z-step", 2)))
        if self.xy_step.get() not in [str(s) for s in XY_STEPS]:
            self.xy_step.set("10")
        if self.z_step.get() not in [str(s) for s in Z_STEPS]:
            self.z_step.set("2")
        self._watch_job = None
        self.controls = []

        # Direction pad
        pad = ttk.Frame(self)
        pad.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, px(14)))
        def make(text, axis, sign, row, column, tip):
            button = HoldButton(pad, action=lambda a=axis, s=sign: self.move(a, s), can_repeat=self.d.stage_is_idle,
                                text=text, bootstyle="dark", width=3)
            button.grid(row=row, column=column, padx=px(2), pady=px(2), ipady=px(4))
            add_tooltip(button, tip)
            self.controls.append(button)
            return button
        make("▲", "y", +1, 0, 1, "Move +Y (↑ key). Hold to keep moving. Shift = 10× step.")
        make("◀", "x", -1, 1, 0, "Move −X (← key). Hold to keep moving.")
        make("▶", "x", +1, 1, 2, "Move +X (→ key). Hold to keep moving.")
        make("▼", "y", -1, 2, 1, "Move −Y (↓ key). Hold to keep moving.")
        self.center = ttk.Label(pad, text="", style="Small.TLabel", anchor="center", width=6)
        self.center.grid(row=1, column=1)

        zpad = ttk.Frame(self)
        zpad.grid(row=0, column=1, rowspan=2, sticky="w", padx=(0, px(16)))
        ttk.Label(zpad, text="FOCUS", style="Caption.TLabel").grid(row=0, column=0, pady=(0, px(2)))
        self.z_up = HoldButton(zpad, action=lambda: self.move("z", +1), can_repeat=self.d.stage_is_idle,
                               text="Z +", bootstyle="dark", width=5)
        self.z_up.grid(row=1, column=0, pady=px(2), ipady=px(4))
        self.z_down = HoldButton(zpad, action=lambda: self.move("z", -1), can_repeat=self.d.stage_is_idle,
                                 text="Z −", bootstyle="dark", width=5)
        self.z_down.grid(row=2, column=0, pady=px(2), ipady=px(4))
        add_tooltip(self.z_up, "Focus +Z (Page Up). Hold to keep moving.")
        add_tooltip(self.z_down, "Focus −Z (Page Down). Hold to keep moving.")
        self.controls += [self.z_up, self.z_down]

        steps = ttk.Frame(self)
        steps.grid(row=0, column=2, rowspan=2, sticky="nw", padx=(0, px(16)))
        ttk.Label(steps, text="X/Y STEP · µm", style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        self.xy_choice = Segmented(steps, [(str(s), f"{s:g}") for s in XY_STEPS], self.xy_step,
                                   command=lambda _v: self._steps_changed(), padx=6, pady=3)
        self.xy_choice.grid(row=1, column=0, sticky="w", pady=(px(2), px(8)))
        ttk.Label(steps, text="Z STEP · µm", style="Caption.TLabel").grid(row=2, column=0, sticky="w")
        self.z_choice = Segmented(steps, [(str(s), f"{s:g}") for s in Z_STEPS], self.z_step,
                                  command=lambda _v: self._steps_changed(), padx=6, pady=3)
        self.z_choice.grid(row=3, column=0, sticky="w", pady=(px(2), 0))
        self.controls += [self.xy_choice, self.z_choice]

        readout = ttk.Frame(self)
        readout.grid(row=0, column=3, rowspan=2, sticky="nw")
        self.values = {}
        for row, axis in enumerate("XYZ"):
            ttk.Label(readout, text=axis, style="Caption.TLabel").grid(row=row, column=0, sticky="w", padx=(0, px(6)))
            value = ttk.Label(readout, text="0.0", font="StepperMono", anchor="e", width=10)
            value.grid(row=row, column=1, sticky="e")
            ttk.Label(readout, text="µm", style="Small.TLabel").grid(row=row, column=2, sticky="w", padx=(px(3), 0))
            self.values[axis.lower()] = value
        self.state_label = ttk.Label(readout, text="", style="Small.TLabel")
        self.state_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(px(2), 0))
        buttons = ttk.Frame(readout)
        buttons.grid(row=4, column=0, columnspan=3, sticky="w", pady=(px(4), 0))
        self.goto = ttk.Button(buttons, text="Go to…", bootstyle="dark", command=self.open_goto)
        self.goto.grid(row=0, column=0, sticky="w")
        add_tooltip(self.goto, "Move to typed X/Y/Z coordinates or a saved position.")
        self.home = ttk.Button(buttons, text="Home", bootstyle="dark", command=self.home_stage)
        self.home.grid(row=0, column=1, sticky="w", padx=(px(6), 0))
        add_tooltip(self.home, "Run the homing cycle (all axes go to their limit switches).")
        if not self.d.hardware.stage.has_homing():
            self.home.grid_remove()
        self.reconnect = ttk.Button(buttons, text="Reconnect", bootstyle="warning", command=self.d.reconnect_stage)
        self.reconnect.grid(row=0, column=2, sticky="w", padx=(px(6), 0))
        add_tooltip(self.reconnect, "The stage controller's USB link dropped. Check the cable and power, then click.")
        self.reconnect.grid_remove()
        self.controls += [self.goto, self.home]
        self.columnconfigure(5, weight=1)

        self.d.add_event_listener(Event.STAGE_POSITION_CHANGED, self.refresh_position)
        self.d.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, self.refresh_lock)
        self.d.add_event_listener(Event.STAGE_CONNECTION_CHANGED, self.refresh_connection)
        self._steps_changed(save=False)
        self.refresh_position()
        self.refresh_lock()

    # ---------------------------------------------------------------- moves
    def step(self, axis: str) -> float:
        return float(self.z_step.get() if axis == "z" else self.xy_step.get())

    def move(self, axis: str, sign: int, multiplier: float = 1.0) -> bool:
        amount = sign * self.step(axis) * multiplier * (-1 if self.invert.get(axis) else 1)
        if self.d.jog(axis, amount):
            self._watch()
            return True
        return False

    def handle_key(self, event) -> bool:
        """Arrow keys: X/Y. Page Up/Down: Z. Shift: 10× step. Ignored while typing in a field."""
        keys = {"Up": ("y", 1), "Down": ("y", -1), "Left": ("x", -1), "Right": ("x", 1),
                "Prior": ("z", 1), "Next": ("z", -1)}
        if event.keysym not in keys:
            return False
        try:
            focus = self.focus_get()
        except (KeyError, tk.TclError):
            return False  # e.g. an open drop-down list has the keyboard
        if focus is not None and focus.winfo_class() in ENTRY_CLASSES:
            return False
        if self.d.movement_lock != MovementLock.UNLOCKED:
            return True
        if not self.d.stage_is_idle():
            return True  # key auto-repeat while the last move runs: skip, don't queue
        axis, sign = keys[event.keysym]
        self.move(axis, sign, 10.0 if (event.state & 0x0001) else 1.0)
        return True

    def _watch(self):
        """Show Moving… until the controller reports idle."""
        if self._watch_job is not None:
            return
        stage = self.d.hardware.stage
        if not isinstance(stage, GrblStage):
            self.refresh_state()
            return
        def poll():
            self._watch_job = None
            if self.d.stage_is_idle():
                self.refresh_state()
            else:
                self.state_label.configure(text="● Moving…", style="Warn.TLabel")
                self._watch_job = self.after(120, poll)
        poll()

    def _steps_changed(self, save=True):
        self.center.configure(text=f"{float(self.xy_step.get()):g} µm")
        if save:
            stage = self.app.config.setdefault("stage", {})
            stage["xy-step"] = int(self.xy_step.get())
            stage["z-step"] = int(self.z_step.get())

    # ---------------------------------------------------------------- display
    def refresh_position(self):
        for axis, value in zip("xyz", self.d.stage_setpoint):
            self.values[axis].configure(text=f"{value:,.1f}")

    def refresh_connection(self):
        if self.d.stage_connected:
            self.reconnect.grid_remove()
        else:
            self.reconnect.grid()
        self.refresh_lock()

    def refresh_state(self):
        stage = self.d.hardware.stage
        if not self.d.stage_connected:
            self.state_label.configure(text="● Disconnected: check the USB cable, then Reconnect", style="Bad.TLabel")
        elif not isinstance(stage, GrblStage):
            self.state_label.configure(text="Simulation · no stage connected", style="Small.TLabel")
        elif self.d.movement_lock != MovementLock.UNLOCKED:
            self.state_label.configure(text=f"Locked: {self.d.busy_text() or 'busy'} in progress", style="Warn.TLabel")
        else:
            state = stage.state_name()
            ok = state.startswith("Idle") or state == "Unknown"
            self.state_label.configure(text=f"● {state}", style="Ok.TLabel" if ok else "Warn.TLabel")

    def refresh_lock(self):
        lock = self.d.movement_lock
        enabled = lock == MovementLock.UNLOCKED and self.d.stage_connected
        for widget in self.controls:
            if hasattr(widget, "set_enabled"):
                widget.set_enabled(enabled)
            else:
                widget.configure(state="normal" if enabled else "disabled")
        if lock == MovementLock.XY_LOCKED:
            for widget in (self.z_up, self.z_down):
                widget.configure(state="normal")
        self.refresh_state()

    # ---------------------------------------------------------------- dialogs
    def home_stage(self):
        if messagebox.askokcancel("Home the stage", "Run the homing cycle? Every axis moves to its limit switch.\n\n"
                                  "Keep hands and tools clear of the stage."):
            self.d.home_stage()

    def open_goto(self):
        GoToDialog(self.app)


class GoToDialog:
    def __init__(self, app):
        self.app = app
        d = app.dispatcher
        self.window = tk.Toplevel(app.root)
        self.window.title("Go to position")
        self.window.transient(app.root)
        self.window.resizable(False, False)
        body = ttk.Frame(self.window, padding=px(16))
        body.grid()
        ttk.Label(body, text="Move the stage to", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.vars = {}
        for i, axis in enumerate("xyz"):
            ttk.Label(body, text=f"{axis.upper()} · µm").grid(row=1, column=i, sticky="w", padx=(0 if i == 0 else px(8), 0),
                                                              pady=(px(10), px(2)))
            var = tk.StringVar(value=f"{d.stage_setpoint[i]:.1f}")
            entry = ttk.Entry(body, textvariable=var, width=12)
            entry.grid(row=2, column=i, sticky="ew", padx=(0 if i == 0 else px(8), 0))
            entry.bind("<Return>", lambda _e: self.move())
            self.vars[axis] = var
        self.error = ttk.Label(body, text="", style="Bad.TLabel")
        self.error.grid(row=3, column=0, columnspan=3, sticky="w", pady=(px(4), 0))
        presets = presets_from_config(app.config)
        homing = d.hardware.stage.has_homing()
        ttk.Label(body, text="Saved positions", style="Caption.TLabel").grid(row=4, column=0, columnspan=3, sticky="w",
                                                                            pady=(px(10), px(4)))
        box = ttk.Frame(body)
        box.grid(row=5, column=0, columnspan=3, sticky="w")
        for column, (name, coords) in enumerate(presets.items()):
            button = ttk.Button(box, text=name, bootstyle="dark", state="normal" if homing else "disabled",
                                command=lambda c=coords: self.fill(c))
            button.grid(row=0, column=column, padx=(0, px(6)))
        if not homing:
            ttk.Label(body, text="Saved positions need a homed stage (homing = true in the config).",
                      style="Small.TLabel", wraplength=px(360)).grid(row=6, column=0, columnspan=3, sticky="w", pady=(px(4), 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=7, column=0, columnspan=3, sticky="e", pady=(px(16), 0))
        ttk.Button(buttons, text="Cancel", bootstyle="dark", command=self.window.destroy).pack(side="right")
        ttk.Button(buttons, text="Move", bootstyle="primary", command=self.move).pack(side="right", padx=(0, px(8)))
        self.window.bind("<Escape>", lambda _e: self.window.destroy())
        self.window.update_idletasks()
        try:
            self.window.grab_set()
        except tk.TclError:
            pass

    def fill(self, coords):
        for axis, value in zip("xyz", coords):
            self.vars[axis].set(f"{value:.1f}")

    def move(self):
        try:
            target = {axis: float(var.get().replace(",", "")) for axis, var in self.vars.items()}
            if not all(math.isfinite(v) for v in target.values()):
                raise ValueError
        except ValueError:
            self.error.configure(text="Enter a number for each axis.")
            return
        d = self.app.dispatcher
        if d.movement_lock != MovementLock.UNLOCKED:
            self.error.configure(text="Wait for the exposure or autofocus to finish.")
            return
        changed = [a for a, v, cur in zip("xyz", target.values(), d.stage_setpoint) if abs(v - cur) > 1e-6]
        far = max((abs(v - cur) for v, cur in zip(target.values(), d.stage_setpoint)), default=0)
        if len(changed) > 1 and far > 1000:
            where = ", ".join(f"{a.upper()} {v:,.0f}" for a, v in target.items())
            if not messagebox.askokcancel("Move the stage", f"Move to {where} µm?\n\nThis moves several axes at once. "
                                          "Keep hands and tools clear of the stage.", parent=self.window):
                return
        self.window.destroy()
        if not changed:
            return
        try:
            d.move_absolute({axis: target[axis] for axis in changed})
        except Exception:
            return
        self.app.stage_panel._watch()
