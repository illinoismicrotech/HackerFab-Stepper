"""What the projector is showing: a badge, a plain-language sentence, an exact thumbnail,
the LED state, and where the projector window is. Plus a resizable preview window."""
from __future__ import annotations

import time
import tkinter as tk

import ttkbootstrap as ttk
from PIL import Image, ImageTk

from dispatcher import Event, ShownImage
from platform_text import EXTEND_DISPLAYS
from projector_window import solid_color
from ui_kit import Badge, Card, Segmented, add_tooltip
from ui_theme import palette, px


def scaled_copy(image: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Shrink for display, averaging pixels so thin pattern lines stay visible."""
    w, h = image.size
    scale = min(box_w / w, box_h / h)
    size = (max(1, int(w * scale)), max(1, int(h * scale)))
    if scale < 0.5:
        factor = int(1 / scale)
        if factor >= 2:
            image = image.reduce(factor)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image.resize(size, Image.Resampling.BOX)


def draw_projection(canvas: tk.Canvas, image, holder: dict, outline=None):
    """Draw the projector image letterboxed in `canvas` (black = no light)."""
    c = palette()
    canvas.delete("all")
    w, h = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
    canvas.create_rectangle(0, 0, w, h, fill="#000000", outline="")
    if image is None:
        holder["photo"] = None
    else:
        iw, ih = image.size
        scale = min(w / iw, h / ih)
        tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
        x0, y0 = (w - tw) // 2, (h - th) // 2
        color = solid_color(image)
        if color is not None:
            canvas.create_rectangle(x0, y0, x0 + tw, y0 + th, fill="#%02x%02x%02x" % color, outline="")
            holder["photo"] = None
        else:
            holder["photo"] = ImageTk.PhotoImage(scaled_copy(image, tw, th), master=canvas)
            canvas.create_image(x0, y0, image=holder["photo"], anchor="nw")
    canvas.create_rectangle(0, 0, w - 1, h - 1, outline=outline or c["border"])


class ProjectorPanel(Card):
    def __init__(self, parent, app):
        super().__init__(parent, "Projector")
        self.app = app
        d = self.d = app.dispatcher
        c = palette()
        self.badge = Badge(self.header, "OFF", "off")
        self.badge.grid(row=0, column=1, sticky="e")
        body = self.body
        body.columnconfigure(1, weight=1)
        self.thumb = tk.Canvas(body, width=px(128), height=px(72), highlightthickness=0, background="#000000",
                               cursor="hand2")
        self.thumb.grid(row=0, column=0, sticky="nw", padx=(0, px(10)))
        self.thumb.bind("<Button-1>", lambda _e: app.open_projector_preview())
        self.thumb.bind("<Configure>", lambda _e: self.refresh_image())
        add_tooltip(self.thumb, "Exactly what the projector shows right now. Click to enlarge.")
        self._holder = {}
        text = ttk.Frame(body)
        text.grid(row=0, column=1, sticky="nsew")
        self.description = ttk.Label(text, text="", wraplength=px(222), justify="left")
        self.description.grid(row=0, column=0, sticky="nw")
        self.leds = ttk.Label(text, text="", style="Small.TLabel", wraplength=px(222), justify="left")
        self.leds.grid(row=1, column=0, sticky="nw", pady=(px(3), 0))
        self.led_tip = add_tooltip(self.leds, d.led_status)
        controls = ttk.Frame(body)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        controls.columnconfigure(0, weight=1)
        self.mode_var = tk.StringVar(value="off")
        self.mode = Segmented(controls, [("off", "Off", "off"), ("red", "Red light · safe", "red")],
                              self.mode_var, command=self._mode_chosen, padx=8, pady=4)
        self.mode.grid(row=0, column=0, sticky="ew")
        add_tooltip(self.mode, "Off = black, LEDs off. Red light lets you see and focus without exposing resist.")
        self.enlarge = ttk.Button(controls, text="Enlarge", bootstyle="dark", command=app.open_projector_preview)
        self.enlarge.grid(row=0, column=1, padx=(px(6), 0))
        add_tooltip(self.enlarge, "Open a larger preview of what the projector shows (a preview only, not the output).")
        output = ttk.Frame(body)
        output.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(6), 0))
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="OUTPUT", style="Caption.TLabel").grid(row=0, column=0, sticky="w", padx=(0, px(8)))
        self.display_var = tk.StringVar()
        self.display = ttk.Combobox(output, textvariable=self.display_var, state="readonly", width=22,
                                    postcommand=app.refresh_display_choices)
        self.display.grid(row=0, column=1, sticky="ew")
        self.display.bind("<<ComboboxSelected>>", lambda _e: app.display_chosen(self.display_var.get()))
        self.window_button = ttk.Button(output, text="Open", bootstyle="success", width=7,
                                        command=app.toggle_projector_window)
        self.window_button.grid(row=0, column=2, padx=(px(6), 0))
        self.display_tip = add_tooltip(self.display, "The display the projector is on. If it is missing, " + EXTEND_DISPLAYS + ".")
        add_tooltip(self.window_button, "Open or close the projector output window on that display.")
        self.output_note = ttk.Label(body, text="", style="Warn.TLabel", wraplength=px(370), justify="left")
        self.output_note.grid(row=3, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        self._last_text = 0.0
        for event in (Event.SHOWN_IMAGE_CHANGED, Event.PATTERN_IMAGE_CHANGED, Event.RED_BRIGHTNESS_CHANGED,
                      Event.RED_SOURCE_CHANGED, Event.LEDS_CHANGED, Event.PATTERNING_BUSY_CHANGED,
                      Event.IMAGE_ADJUST_CHANGED, Event.PROJECTOR_SIZE_CHANGED):
            d.add_event_listener(event, self.refresh)
        d.add_event_listener(Event.EXPOSURE_PATTERN_PROGRESS_CHANGED, self._progress)
        self.refresh()

    def _mode_chosen(self, value):
        if value == "red":
            self.d.show_red()
        else:
            self.d.clear_projector()

    def _progress(self):
        if time.monotonic() - self._last_text > 0.1:
            self._last_text = time.monotonic()
            self.refresh_text()

    def refresh(self):
        self.refresh_text()
        self.refresh_image()

    def refresh_text(self):
        d = self.d
        badge, kind, sentence = d.describe_projection()
        self.badge.set(badge, kind)
        self.description.configure(text=sentence)
        led_kind, led_text = d.led_summary()
        self.leds.configure(text=led_text, style={"ok": "Ok.TLabel", "warn": "Warn.TLabel"}.get(led_kind, "Small.TLabel"))
        if self.led_tip is not None:
            self.led_tip.text = d.led_status
        shown = d.shown_image
        self.mode_var.set("red" if shown == ShownImage.RED_FOCUS else ("off" if shown == ShownImage.CLEAR else ""))
        self.mode.set_enabled(not d.patterning_busy)
        self.enlarge.configure(state="normal")

    def refresh_image(self):
        image = getattr(self.d.hardware.projector, "current_image", None) if self.d.shown_image != ShownImage.CLEAR else None
        _b, kind, _s = self.d.describe_projection()
        c = palette()
        draw_projection(self.thumb, image, self._holder, outline=c.get(kind, c["border"]) if image is not None else None)

    def set_locked(self, locked: bool):
        """The output cannot change during an exposure."""
        self.display.configure(state="disabled" if locked else "readonly")
        self.window_button.configure(state="disabled" if locked else "normal")

    def show_output(self, choices, current, is_open, note, problem=False):
        self.display.configure(values=choices)
        if current is not None:
            self.display_var.set(current)
        self.window_button.configure(text="Close" if is_open else "Open", bootstyle="dark" if is_open else "success")
        if self.display_tip is not None:
            self.display_tip.text = note
        # Only take up room when something needs attention.
        self.output_note.configure(text=note if problem else "")
        if problem:
            self.output_note.grid()
        else:
            self.output_note.grid_remove()


class ProjectorPreviewWindow:
    """A resizable window showing what the projector shows. A preview, never the output."""

    def __init__(self, app):
        self.app = app
        d = app.dispatcher
        self.window = tk.Toplevel(app.root)
        self.window.title("Projector preview: what the projector is showing")
        self.window.geometry(f"{px(800)}x{px(520)}")
        self.window.minsize(px(320), px(220))
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _e: self.close())
        top = ttk.Frame(self.window, padding=(px(12), px(10)))
        top.pack(fill="x")
        self.badge = Badge(top, "OFF", "off")
        self.badge.pack(side="left")
        self.text = ttk.Label(top, text="", wraplength=px(640))
        self.text.pack(side="left", padx=(px(10), 0), fill="x", expand=True)
        self.canvas = tk.Canvas(self.window, background="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=px(12), pady=(0, px(6)))
        self.canvas.bind("<Configure>", lambda _e: self.refresh())
        ttk.Label(self.window, text="This window is only a preview. The projector output window is separate "
                                    "(see Output on the Projector panel).", style="Small.TLabel").pack(
            anchor="w", padx=px(12), pady=(0, px(10)))
        self._holder = {}
        self._last = 0.0
        self.listeners = []
        for event in (Event.SHOWN_IMAGE_CHANGED, Event.PATTERN_IMAGE_CHANGED, Event.RED_BRIGHTNESS_CHANGED,
                      Event.IMAGE_ADJUST_CHANGED, Event.PATTERNING_BUSY_CHANGED, Event.LEDS_CHANGED):
            d.add_event_listener(event, self.refresh)
            self.listeners.append((event, self.refresh))
        d.add_event_listener(Event.EXPOSURE_PATTERN_PROGRESS_CHANGED, self._progress)
        self.listeners.append((Event.EXPOSURE_PATTERN_PROGRESS_CHANGED, self._progress))
        self.refresh()

    @property
    def is_open(self):
        return self.window is not None

    def lift(self):
        if self.window is not None:
            self.window.deiconify()
            self.window.lift()

    def _progress(self):
        if time.monotonic() - self._last > 0.15:
            self._last = time.monotonic()
            badge, kind, sentence = self.app.dispatcher.describe_projection()
            self.badge.set(badge, kind)
            self.text.configure(text=sentence)

    def refresh(self):
        if self.window is None:
            return
        d = self.app.dispatcher
        badge, kind, sentence = d.describe_projection()
        self.badge.set(badge, kind)
        self.text.configure(text=sentence)
        image = getattr(d.hardware.projector, "current_image", None) if d.shown_image != ShownImage.CLEAR else None
        draw_projection(self.canvas, image, self._holder)

    def close(self):
        if self.window is None:
            return
        for event, listener in self.listeners:
            listeners = self.app.dispatcher.listeners.get(event, [])
            if listener in listeners:
                listeners.remove(listener)
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.window = None
