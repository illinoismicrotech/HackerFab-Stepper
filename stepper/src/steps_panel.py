"""The three steps of an exposure, side by side with the camera: 1 Pattern, 2 Focus, 3 Expose."""
from __future__ import annotations

import math
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import ttkbootstrap as ttk

from dispatcher import MAX_BORDER, Event, MovementLock, RedFocusSource, ShownImage, load_pattern_image
from projector_panel import draw_projection
from stage_control.grbl_stage import GrblStage
from ui_kit import ScrollFrame, Segmented, ValueSlider, add_tooltip, big_button
from ui_theme import palette, px

EXPOSURE_PRESETS = (2, 5, 8, 10, 15, 30)


class WorkflowPanel(ttk.Frame):
    STEPS = (("pattern", "1  Pattern"), ("focus", "2  Focus"), ("expose", "3  Expose"))

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.step_var = tk.StringVar(value="focus")
        self.steps = Segmented(self, self.STEPS, self.step_var, command=self.show_step, font="StepperBold",
                               padx=8, pady=7)
        self.steps.grid(row=0, column=0, sticky="ew")
        self.scroll = ScrollFrame(self, padding=(0, px(12), px(4), px(4)))
        self.scroll.grid(row=1, column=0, sticky="nsew")
        self.scroll.body.columnconfigure(0, weight=1)
        self.footer = ttk.Frame(self, padding=(0, px(8), 0, 0))
        self.footer.grid(row=2, column=0, sticky="ew")
        self.footer.columnconfigure(0, weight=1)
        self.pages = {"pattern": PatternStep(self.scroll.body, app), "focus": FocusStep(self.scroll.body, app),
                      "expose": ExposeStep(self.scroll.body, app, self.footer)}
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()
        app.dispatcher.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, self._lock)
        self.show_step("focus", apply=False)

    def show_step(self, step: str, apply: bool = True):
        self.step_var.set(step)
        for name, page in self.pages.items():
            if name == step:
                page.grid()
            else:
                page.grid_remove()
        if step == "expose":
            self.footer.grid()   # Start / progress / Stop always visible, never scrolled away
        else:
            self.footer.grid_remove()
        self.scroll.canvas.yview_moveto(0)
        if apply:
            self.app.step_changed(step)

    def _lock(self):
        d = self.app.dispatcher
        self.steps.set_enabled(not (d.patterning_busy or d.autofocus_busy))


def section(parent, row, text):
    ttk.Label(parent, text=text.upper(), style="Caption.TLabel").grid(row=row, column=0, columnspan=3, sticky="w",
                                                                    pady=(px(14), px(4)))


class PatternStep(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        d = self.d = app.dispatcher
        self.columnconfigure(0, weight=1)
        self.last_folder = None
        ttk.Label(self, text="Choose the image to expose. Bright areas get light.", style="Muted.TLabel",
                  wraplength=px(330), justify="left").grid(row=0, column=0, sticky="w")
        buttons = ttk.Frame(self)
        buttons.grid(row=1, column=0, sticky="ew", pady=(px(10), 0))
        self.choose = ttk.Button(buttons, text="Choose pattern…", bootstyle="primary", command=self.choose_image)
        self.choose.pack(side="left")
        self.in_red = ttk.Button(buttons, text="Show it in red", bootstyle="dark", command=self.show_in_red)
        self.in_red.pack(side="left", padx=(px(8), 0))
        add_tooltip(self.in_red, "Project the pattern in red (safe) so you can see where it will land on the chip.")
        self.name = ttk.Label(self, text="No pattern chosen", font="StepperBold", wraplength=px(330))
        self.name.grid(row=2, column=0, sticky="w", pady=(px(12), 0))
        self.details = ttk.Label(self, text="", style="Small.TLabel", wraplength=px(330), justify="left")
        self.details.grid(row=3, column=0, sticky="w")
        self.preview = tk.Canvas(self, height=px(200), highlightthickness=0, background="#000000")
        self.preview.grid(row=4, column=0, sticky="ew", pady=(px(8), 0))
        self.preview.bind("<Configure>", lambda _e: self.refresh_preview())
        ttk.Label(self, text="Preview of the exposure: the pattern in blue light, exactly as it will be projected.",
                  style="Small.TLabel", wraplength=px(330)).grid(row=5, column=0, sticky="w", pady=(px(4), 0))
        self._holder = {}
        section(self, 6, "Pattern options")
        options = ttk.Frame(self)
        options.grid(row=7, column=0, sticky="ew")
        ttk.Label(options, text="Border").grid(row=0, column=0, sticky="w")
        self.border = tk.StringVar(value=f"{d.border_size:g}")
        border = ttk.Spinbox(options, from_=0, to=50, increment=1, textvariable=self.border, width=5,
                             command=self.apply_border)
        border.grid(row=0, column=1, sticky="w", padx=(px(8), px(4)))
        border.bind("<Return>", lambda _e: self.apply_border())
        border.bind("<FocusOut>", lambda _e: self.apply_border())
        ttk.Label(options, text="% dark margin", style="Small.TLabel").grid(
            row=0, column=2, sticky="w")
        self.posterize = tk.BooleanVar(value=d.posterize_strength is not None)
        ttk.Checkbutton(options, text="Pure black & white, cut-off", variable=self.posterize, bootstyle="round-toggle",
                        command=self.apply_posterize).grid(row=1, column=0, columnspan=2, sticky="w", pady=(px(8), 0))
        self.cutoff = tk.StringVar(value=str(d.posterize_strength or 50))
        cutoff = ttk.Spinbox(options, from_=0, to=100, increment=5, textvariable=self.cutoff, width=5,
                             command=self.apply_posterize)
        cutoff.grid(row=1, column=2, sticky="w", pady=(px(8), 0))
        cutoff.bind("<Return>", lambda _e: self.apply_posterize())
        cutoff.bind("<FocusOut>", lambda _e: self.apply_posterize())
        add_tooltip(cutoff, "Brightness (percent) above which a pixel becomes fully lit.")
        ttk.Label(self, text="To shift or rotate the pattern on the projector, use the Alignment page.",
                  style="Small.TLabel", wraplength=px(330)).grid(row=8, column=0, sticky="w", pady=(px(10), 0))
        d.add_event_listener(Event.PATTERN_IMAGE_CHANGED, self.refresh)
        d.add_event_listener(Event.PATTERNING_BUSY_CHANGED,
                             lambda: self.choose.configure(state="disabled" if d.patterning_busy else "normal"))
        self.refresh()

    def choose_image(self):
        if self.d.patterning_busy:
            return
        path = filedialog.askopenfilename(title="Choose a pattern image", initialdir=self.last_folder,
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                                                     ("All files", "*.*")])
        if not path:
            return
        self.last_folder = str(Path(path).parent)
        self.load(path)

    def load(self, path):
        try:
            image = load_pattern_image(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not open image", f"{path}\n\n{exc}")
            return False
        self.d.set_pattern_image(image, str(path))
        self.app.notify(f"Pattern loaded: {Path(path).name}", "ok")
        return True

    def show_in_red(self):
        if not self.d.pattern_image_path:
            messagebox.showinfo("Choose a pattern", "Choose a pattern first.")
            return
        self.d.show_red(RedFocusSource.PATTERN)
        self.app.workflow.show_step("focus", apply=False)

    def apply_border(self):
        try:
            value = float(self.border.get())
            if not math.isfinite(value) or not 0 <= value <= MAX_BORDER:
                raise ValueError
        except ValueError:
            self.border.set(f"{self.d.border_size:g}")
            return
        if abs(value - self.d.border_size) > 1e-9:
            self.d.set_border_size(value)

    def apply_posterize(self):
        strength = None
        if self.posterize.get():
            try:
                strength = max(0, min(100, int(float(self.cutoff.get()))))
            except ValueError:
                strength = 50
            self.cutoff.set(str(strength))
        if strength != self.d.posterize_strength:
            self.d.set_posterize_strength(strength)

    def refresh(self):
        d = self.d
        if d.pattern_image_path:
            path = Path(d.pattern_image_path)
            w, h = d.pattern_image.size
            pw, ph = d.hardware.projector.size()
            self.name.configure(text=path.name)
            fit = "fills the projector exactly" if (w, h) == (pw, ph) else f"scaled to fit the {pw} × {ph} projector"
            self.details.configure(text=f"{w} × {h} pixels · {fit}\n{path.parent}")
            self.in_red.configure(state="normal")
        else:
            self.name.configure(text="No pattern chosen")
            self.details.configure(text="")
            self.in_red.configure(state="disabled")
        self.refresh_preview()

    def refresh_preview(self):
        image = self.d.pattern.processed() if self.d.pattern_image_path else None
        draw_projection(self.preview, image, self._holder)
        if image is None:
            c = palette()
            self.preview.create_text(self.preview.winfo_width() // 2, self.preview.winfo_height() // 2,
                                     text="No pattern", fill=c["muted"], font="StepperBold")


class FocusStep(ttk.Frame):
    SOURCES = (("solid", "Solid"), ("pattern", "Pattern"), ("inv_pattern", "Inverted"), ("image", "Image…"))

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        d = self.d = app.dispatcher
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text="Red light is safe: it does not expose resist. Use it to find the chip, line up "
                             "the pattern and focus.", style="Muted.TLabel", wraplength=px(330), justify="left").grid(
            row=0, column=0, sticky="w")
        section(self, 1, "Red light shows")
        self.source_var = tk.StringVar(value=d.red_focus_source.value)
        self.source = Segmented(self, self.SOURCES, self.source_var, command=self.source_chosen, padx=6, pady=4)
        self.source.notify_same = True   # clicking Image… again picks a different image
        self.source.grid(row=2, column=0, sticky="ew")
        add_tooltip(self.source, "Solid: even red everywhere. Pattern: your pattern drawn in red, to see where it "
                                 "will land. Inverted: light and dark swapped. Image: your own focusing picture.")
        self.auto_light = tk.BooleanVar(value=d.auto_camera.auto_light)
        self.brightness = ValueSlider(self, "Red brightness", 2, 100, d.red_brightness * 100,
                                      fmt=lambda v: f"{v:.0f}%", on_change=lambda v: d.set_red_brightness(v / 100),
                                      auto_var=self.auto_light,
                                      on_auto=lambda on: d.configure_auto_camera(auto_light=on))
        self.brightness.grid(row=3, column=0, sticky="ew", pady=(px(10), 0))
        add_tooltip(self.brightness.scale, "Dims the red light. Auto keeps the camera picture bright but not clipped.")
        section(self, 8, "Camera")
        cam = ttk.Frame(self)
        cam.grid(row=9, column=0, sticky="ew")
        cam.columnconfigure(0, weight=1)
        self.auto_exposure = tk.BooleanVar(value=d.auto_camera.auto_exposure)
        ttk.Checkbutton(cam, text="Auto exposure (removes projector stripes)", variable=self.auto_exposure,
                        bootstyle="round-toggle",
                        command=lambda: d.configure_auto_camera(auto_exposure=self.auto_exposure.get())).grid(
            row=0, column=0, sticky="w")
        self.auto_status = ttk.Label(cam, text="", style="Small.TLabel", wraplength=px(330), justify="left")
        self.auto_status.grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        row = ttk.Frame(cam)
        row.grid(row=2, column=0, sticky="w", pady=(px(6), 0))
        self.search = ttk.Button(row, text="Find stripe-free exposure", bootstyle="dark", command=d.start_stripe_search)
        self.search.pack(side="left")
        add_tooltip(self.search, "Tries several camera exposures under red light and keeps the one with the fewest "
                                 "stripes. About 10 seconds. The result is remembered when you save settings.")
        ttk.Button(row, text="Adjust…", bootstyle="dark", command=app.open_camera_adjustments).pack(side="left",
                                                                                                  padx=(px(6), 0))
        section(self, 4, "Focus")
        af = ttk.Frame(self)
        af.grid(row=5, column=0, sticky="ew")
        self.autofocus = ttk.Button(af, text="Autofocus", bootstyle="primary", command=self.run_autofocus)
        self.autofocus.pack(side="left")
        add_tooltip(self.autofocus, "Finds the sharpest Z near the current one (about 20 s). Focus roughly by hand first.")
        self.stop = ttk.Button(af, text="Stop", bootstyle="danger-outline", command=self.stop_autofocus)
        self.stop.pack(side="left", padx=(px(6), 0))
        self.af_result = ttk.Label(self, text="", style="Small.TLabel", wraplength=px(330), justify="left")
        self.af_result.grid(row=6, column=0, sticky="w", pady=(px(4), 0))
        ttk.Label(self, text="By hand: Z + / Z − (or Page Up / Page Down) while watching the FOCUS bar on the "
                             "camera picture. Longest bar = sharpest.", style="Small.TLabel",
                  wraplength=px(330), justify="left").grid(row=7, column=0, sticky="w", pady=(px(6), 0))
        d.add_event_listener(Event.CAMERA_AUTO_CHANGED, self.refresh_auto)
        d.add_event_listener(Event.RED_BRIGHTNESS_CHANGED, lambda: self.brightness.set(d.red_brightness * 100))
        d.add_event_listener(Event.RED_SOURCE_CHANGED, lambda: self.source_var.set(d.red_focus_source.value))
        d.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, self.refresh_lock)
        d.add_event_listener(Event.AUTOFOCUS_FINISHED, lambda: self.af_result.configure(text=d.last_autofocus))
        self.refresh_auto()
        self.refresh_lock()

    def source_chosen(self, value):
        d = self.d
        source = RedFocusSource(value)
        if source in (RedFocusSource.PATTERN, RedFocusSource.INV_PATTERN) and not d.pattern_image_path:
            messagebox.showinfo("Choose a pattern", "Choose a pattern in step 1 first.")
            self.source_var.set(d.red_focus_source.value)
            return
        if source == RedFocusSource.IMAGE:
            if self.d.patterning_busy:
                return
            path = filedialog.askopenfilename(title="Choose a focusing image",
                                              filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                                                         ("All files", "*.*")])
            if not path:
                self.source_var.set(d.red_focus_source.value)
                return
            try:
                d.set_red_focus_image(load_pattern_image(path), path)
            except (OSError, ValueError) as exc:
                messagebox.showerror("Could not open image", str(exc))
                self.source_var.set(d.red_focus_source.value)
                return
        d.show_red(source)

    def refresh_auto(self):
        auto = self.d.auto_camera
        self.auto_exposure.set(auto.auto_exposure)
        self.auto_light.set(auto.auto_light)
        supported = self.d.camera_supports_controls()
        text = auto.status if supported else "Automatic settings need a USB camera (webcam type)."
        problem = getattr(self.d.camera, "exposure_problem", "")
        if problem:
            text = problem + "\n" + text
        if auto.summary and supported:
            text += "\n" + auto.summary
        self.auto_status.configure(text=text)
        self.search.configure(state="normal" if supported and not self.d.patterning_busy else "disabled")

    def refresh_lock(self):
        d = self.d
        busy = d.patterning_busy or d.autofocus_busy
        self.autofocus.configure(state="disabled" if busy else "normal")
        self.stop.configure(state="normal" if d.autofocus_busy else "disabled")
        self.source.set_enabled(not d.patterning_busy)
        if d.autofocus_busy:
            self.af_result.configure(text="Autofocus running… (Stop or Esc to cancel)")

    def run_autofocus(self):
        d = self.d
        if d.shown_image not in (ShownImage.RED_FOCUS,):
            d.show_red()
        d.autofocus(blue_only=False, interactive=True)

    def stop_autofocus(self):
        self.d.autofocus_abort = True


class ExposeStep(ttk.Frame):
    def __init__(self, parent, app, footer=None):
        super().__init__(parent)
        self.app = app
        d = self.d = app.dispatcher
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text="Blue light exposes the resist. Check the list, then start.", style="Muted.TLabel",
                  wraplength=px(330), justify="left").grid(row=0, column=0, sticky="w")
        section(self, 1, "Exposure time")
        time_row = ttk.Frame(self)
        time_row.grid(row=2, column=0, sticky="ew")
        self.seconds = tk.StringVar(value=f"{d.exposure_time / 1000:g}")
        entry = ttk.Entry(time_row, textvariable=self.seconds, width=7, justify="right", font="StepperMonoBig")
        entry.grid(row=0, column=0, sticky="w")
        ttk.Label(time_row, text="seconds", style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=(px(6), 0))
        self.ms_label = ttk.Label(time_row, text="", style="Small.TLabel")
        self.ms_label.grid(row=0, column=2, sticky="w", padx=(px(10), 0))
        self.seconds.trace_add("write", lambda *_: self.apply_time())
        self.preset_var = tk.StringVar(value="")
        self.presets = Segmented(self, [(str(s), f"{s} s") for s in EXPOSURE_PRESETS], self.preset_var,
                                 command=lambda v: self.seconds.set(v), padx=4, pady=3)
        self.presets.grid(row=3, column=0, sticky="ew", pady=(px(6), 0))
        self.time_entry = entry
        section(self, 4, "Before you expose")
        self.checks = []
        for row in range(6):
            label = ttk.Label(self, text="", wraplength=px(330), justify="left")
            label.grid(row=5 + row, column=0, sticky="w", pady=1)
            self.checks.append(label)
        section(self, 11, "Optional: blue focus check")
        mark = ttk.Frame(self)
        mark.grid(row=12, column=0, sticky="ew")
        self.mark = ttk.Button(mark, text="Show blue focus mark", bootstyle="warning-outline", command=self.toggle_mark)
        self.mark.pack(side="left")
        ttk.Label(mark, text="exposes resist where it lands", style="Small.TLabel").pack(side="left", padx=(px(8), 0))
        add_tooltip(self.mark, "Projects a small plus sign in blue to check focus under blue light. It exposes the "
                               "resist where it lands: use a spare area of the chip.")
        offset = ttk.Frame(self)
        offset.grid(row=13, column=0, sticky="ew", pady=(px(8), 0))
        ttk.Label(offset, text="Z offset during exposure").pack(side="left")
        self.offset = tk.StringVar(value=f"{d.uv_z_offset:g}")
        off_entry = ttk.Entry(offset, textvariable=self.offset, width=6, justify="right")
        off_entry.pack(side="left", padx=(px(8), px(4)))
        ttk.Label(offset, text="µm", style="Small.TLabel").pack(side="left")
        self.offset_error = ttk.Label(offset, text="", style="Bad.TLabel")
        self.offset_error.pack(side="left", padx=(px(8), 0))
        self.offset.trace_add("write", lambda *_: self.apply_offset())
        add_tooltip(off_entry, "Blue light focuses at a slightly different height than red. This Z move is made just "
                               "before the exposure and undone afterwards. 0 = no move.")
        # ---- footer: always visible, never scrolled away
        footer = footer if footer is not None else self
        footer.columnconfigure(0, weight=1)
        self.ready = ttk.Label(footer, text="", wraplength=px(340), justify="left")
        self.ready.grid(row=0, column=0, sticky="w", pady=(0, px(4)))
        self.start = big_button(footer, "▶  Start exposure", d.begin_patterning, "success")
        self.start.grid(row=1, column=0, sticky="ew")
        add_tooltip(self.start, "Project the pattern in blue for the exposure time. Waits for the stage to stop first. "
                                "STOP or Esc ends it early.")
        self.stop = big_button(footer, "■  Stop exposure  (Esc)", d.abort_patterning, "danger")
        self.stop.grid(row=1, column=0, sticky="ew")
        self.progress_row = ttk.Frame(footer)
        self.progress_row.grid(row=2, column=0, sticky="ew", pady=(px(8), 0))
        self.progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(self.progress_row, maximum=1000, bootstyle="info")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.countdown = ttk.Label(self.progress_row, text="", font="StepperMono", width=8, anchor="e")
        self.countdown.grid(row=0, column=1, padx=(px(8), 0))
        self.last = ttk.Label(footer, text="", style="Small.TLabel", wraplength=px(340))
        self.last.grid(row=3, column=0, sticky="w", pady=(px(6), 0))
        self.last.grid_remove()
        self._last_update = 0.0
        d.add_event_listener(Event.PATTERNING_BUSY_CHANGED, self.refresh)
        d.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, self.refresh)
        d.add_event_listener(Event.SHOWN_IMAGE_CHANGED, self.refresh_mark)
        d.add_event_listener(Event.EXPOSURE_PATTERN_PROGRESS_CHANGED, self.refresh_progress)
        d.add_event_listener(Event.PATTERNING_FINISHED, self.show_last)
        self.apply_time()
        self.refresh()
        self._tick()

    def show_last(self):
        self.last.configure(text=f"Last exposure: {self.d.last_exposure}")
        self.last.grid()

    def apply_time(self):
        text = self.seconds.get().strip().replace(",", ".")
        try:
            seconds = float(text)
            if not math.isfinite(seconds) or seconds <= 0 or seconds > 3600:
                raise ValueError
            self.d.exposure_time = int(round(seconds * 1000))
            self.ms_label.configure(text=f"= {self.d.exposure_time:,} ms", style="Small.TLabel")
            self.app.config.setdefault("exposure", {})["seconds"] = seconds
        except ValueError:
            self.d.exposure_time = None
            self.ms_label.configure(text="enter seconds (up to 3600)", style="Bad.TLabel")
        self.preset_var.set(text if text in [str(s) for s in EXPOSURE_PRESETS] else "")
        self.refresh_checks()

    def apply_offset(self):
        try:
            value = float(self.offset.get().replace(",", ".") or 0)
            if not math.isfinite(value) or abs(value) > 500:
                raise ValueError
            self.d.uv_z_offset = value
            self.offset_error.configure(text="")
        except ValueError:
            self.d.uv_z_offset = 0.0
            self.offset_error.configure(text="not used: enter −500 to 500")
        self.refresh_checks()

    def toggle_mark(self):
        d = self.d
        if d.shown_image == ShownImage.UV_FOCUS:
            d.show_red()
        else:
            d.show_uv_mark()

    def refresh_mark(self):
        on = self.d.shown_image == ShownImage.UV_FOCUS
        self.mark.configure(text="Hide blue focus mark" if on else "Show blue focus mark",
                            bootstyle="warning" if on else "warning-outline")
        self.refresh_checks()

    def check_items(self):
        d, app = self.d, self.app
        ok, bad, info = "Ok.TLabel", "Bad.TLabel", "Small.TLabel"
        if d.pattern_image_path:
            w, h = d.pattern_image.size
            yield ok, f"✓  Pattern: {Path(d.pattern_image_path).name} ({w} × {h})"
        else:
            yield bad, "✗  No pattern: choose one in step 1."
        if isinstance(d.exposure_time, (int, float)) and d.exposure_time and d.exposure_time > 0:
            yield ok, f"✓  Exposure: {d.exposure_time / 1000:g} s"
        else:
            yield bad, "✗  Enter an exposure time."
        yield app.output_check()
        if isinstance(d.hardware.stage, GrblStage):
            yield ok, "✓  Stage connected"
        else:
            yield info, "•  Stage in simulation: it will not move"
        if d.leds is not None:
            yield ok, "✓  Projector LEDs controlled over USB"
        else:
            yield info, "•  LED control off: the projector's LEDs stay as they are"
        if d.uv_z_offset:
            yield info, f"•  Z moves {d.uv_z_offset:+g} µm for the exposure and back afterwards"
        else:
            yield info, ""

    def refresh_checks(self):
        items = list(self.check_items())
        for label, (style, text) in zip(self.checks, items):
            label.configure(text=text, style=style)
            if text:
                label.grid()
            else:
                label.grid_remove()
        # One line above the Start button, so the verdict is visible without scrolling.
        problems = [text for style, text in items if style == "Bad.TLabel"]
        d = self.d
        warnings = [text.lstrip("•✓✗ ").strip() for style, text in items if style == "Warn.TLabel"]
        if d.patterning_busy and d.exposure_phase == "preparing":
            self.ready.configure(text="Starting: waiting for the stage to stop. Nothing is exposed yet.",
                                 style="Warn.TLabel")
        elif d.patterning_busy:
            self.ready.configure(text="Exposing: blue light is on. STOP or Esc ends it.", style="Uv.TLabel")
        elif problems:
            self.ready.configure(text=problems[0], style="Bad.TLabel")
        elif warnings:
            self.ready.configure(text=f"Ready. Note: {warnings[0]}", style="Warn.TLabel")
        else:
            name = Path(d.pattern_image_path).name
            self.ready.configure(text=f"Ready: {name} for {d.exposure_time / 1000:g} s.", style="Ok.TLabel")

    def refresh(self):
        d = self.d
        busy = d.patterning_busy
        other = not busy and d.movement_lock != MovementLock.UNLOCKED   # autofocus, homing, tiling
        self.start.configure(state="disabled" if other else "normal")
        self.stop.configure(state="normal" if busy else "disabled")
        # One big button at a time: Start, or Stop while exposing. Progress only while exposing.
        if busy:
            self.start.grid_remove()
            self.stop.grid()
            self.progress_row.grid()
        else:
            self.stop.grid_remove()
            self.start.grid()
            self.progress_row.grid_remove()
        self.time_entry.configure(state="disabled" if busy else "normal")
        self.presets.set_enabled(not busy)
        self.mark.configure(state="disabled" if busy else "normal")
        if not busy:
            self.countdown.configure(text="")
        self.refresh_checks()

    def refresh_progress(self):
        if time.monotonic() - self._last_update < 0.05 and self.d.exposure_progress < 1.0:
            return
        self._last_update = time.monotonic()
        self.progress.configure(value=self.d.exposure_progress * 1000)
        if self.d.patterning_busy:
            self.countdown.configure(text=f"{max(0.0, self.d.exposure_end - time.monotonic()):.1f} s")

    def _tick(self):
        """The output and stage lines can change without an event (windows, displays)."""
        if not self.d.patterning_busy:
            self.refresh_checks()
        self.after(1000, self._tick)
