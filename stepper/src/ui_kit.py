"""Small reusable widgets: cards, badges, segmented controls, hold-to-repeat buttons,
status dots, a focus meter and a scrollable frame."""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Iterable, Optional

import ttkbootstrap as ttk

from ui_theme import palette, px

try:
    from ttkbootstrap.widgets import ToolTip as _ToolTip
except ImportError:  # older ttkbootstrap
    from ttkbootstrap.tooltip import ToolTip as _ToolTip


def add_tooltip(widget, text):
    """Hover help. A tooltip problem must never break the window."""
    try:
        return _ToolTip(widget, text=text, wraplength=px(340), delay=450)
    except Exception as exc:
        print(f"Tooltip unavailable: {exc}")
        return None


def big_button(parent, text, command, bootstyle="success", **kwargs):
    """A large call-to-action ttk button (bigger font and padding)."""
    button = ttk.Button(parent, text=text, command=command, bootstyle=bootstyle, **kwargs)
    try:
        # Raw Tcl call: ttkbootstrap's configure() would reinterpret the style name.
        button.tk.call(button._w, "configure", "-style", f"Big.{bootstyle}.TButton")
    except tk.TclError:
        pass
    return button


def set_enabled(widgets: Iterable, enabled: bool):
    for widget in widgets:
        try:
            if hasattr(widget, "set_enabled"):
                widget.set_enabled(enabled)
            else:
                widget.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass


class Card(tk.Frame):
    """A bordered panel with an optional caption header. Put content in .body."""

    def __init__(self, parent, title: str = "", padding: int = 12, right=None):
        c = palette()
        super().__init__(parent, background=c["bg"], highlightthickness=1,
                         highlightbackground=c["border"], highlightcolor=c["border"], bd=0)
        self.columnconfigure(0, weight=1)
        row = 0
        if title:
            self.header = tk.Frame(self, background=c["bg"])
            self.header.grid(row=0, column=0, sticky="ew", padx=px(padding), pady=(px(padding - 2), 0))
            self.header.columnconfigure(0, weight=1)
            self.title_label = tk.Label(self.header, text=title.upper(), font="StepperCaption",
                                        foreground=c["muted"], background=c["bg"], anchor="w")
            self.title_label.grid(row=0, column=0, sticky="w")
            row = 1
        self.body = ttk.Frame(self, padding=(px(padding), px(6 if title else padding), px(padding), px(padding)))
        self.body.grid(row=row, column=0, sticky="nsew")
        self.rowconfigure(row, weight=1)
        self.body.columnconfigure(0, weight=1)


class Badge(tk.Label):
    """Coloured pill: Badge.set('RED · SAFE', 'red')."""

    def __init__(self, parent, text="", kind="off", **kwargs):
        c = palette()
        super().__init__(parent, text=text, font="StepperCaption", padx=px(8), pady=px(2),
                         background=c.get(f"{kind}_bg", c["off_bg"]), foreground=c.get(kind, c["off"]), **kwargs)
        self.kind = kind

    def set(self, text: str, kind: str = None):
        c = palette()
        kind = kind or self.kind
        self.kind = kind
        self.configure(text=text, background=c.get(f"{kind}_bg", c["off_bg"]), foreground=c.get(kind, c["off"]))


class StatusDot(tk.Frame):
    """● Camera  — coloured dot plus a short label, for the top bar."""

    def __init__(self, parent, label: str):
        c = palette()
        super().__init__(parent, background=c["bg"])
        size = px(10)
        self.canvas = tk.Canvas(self, width=size, height=size, highlightthickness=0, background=c["bg"])
        self.canvas.grid(row=0, column=0, padx=(0, px(5)))
        self.dot = self.canvas.create_oval(1, 1, size - 1, size - 1, fill=c["off"], outline="")
        self.label = tk.Label(self, text=label, background=c["bg"], foreground=c["muted"], font="StepperSmall")
        self.label.grid(row=0, column=1)
        self.kind = "off"

    def set(self, kind: str, text: Optional[str] = None):
        c = palette()
        self.kind = kind
        self.canvas.itemconfigure(self.dot, fill=c.get(kind, c["off"]))
        if text is not None:
            self.label.configure(text=text)
        self.label.configure(foreground=c["text"] if kind in ("ok", "warn", "bad", "uv") else c["muted"])


class Segmented(tk.Frame):
    """A row of mutually exclusive choices drawn as one control.

    options: [(value, label)] or [(value, label, kind)] where kind picks the selected colour
    (e.g. 'red', 'uv', 'off'); default is the accent colour.
    """

    def __init__(self, parent, options, variable: tk.Variable = None, command: Callable = None,
                 font="TkDefaultFont", padx=10, pady=5, equal=True):
        c = palette()
        super().__init__(parent, background=c["border"], bd=0, highlightthickness=0)
        self.variable = variable or tk.StringVar(value=str(options[0][0]))
        self.command = command
        self.buttons: dict = {}
        self.kinds: dict = {}
        self.enabled: dict = {}
        self.all_enabled = True
        for column, option in enumerate(options):
            value, text = str(option[0]), option[1]
            kind = option[2] if len(option) > 2 else "accent"
            label = tk.Label(self, text=text, font=font, padx=px(padx), pady=px(pady), cursor="hand2",
                             background=c["seg_bg"], foreground=c["text"])
            label.grid(row=0, column=column, sticky="nsew", padx=(1 if column else 1, 1), pady=1)
            if equal:
                self.columnconfigure(column, weight=1, uniform="seg")
            label.bind("<Button-1>", lambda _e, v=value: self._click(v))
            label.bind("<Enter>", lambda _e, v=value: self._hover(v, True))
            label.bind("<Leave>", lambda _e, v=value: self._hover(v, False))
            self.buttons[value] = label
            self.kinds[value] = kind
            self.enabled[value] = True
        self.variable.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def get(self) -> str:
        return str(self.variable.get())

    def set(self, value, notify=False):
        self.variable.set(str(value))
        if notify and self.command:
            self.command(str(value))

    def _click(self, value):
        if not (self.all_enabled and self.enabled.get(value, True)):
            return
        changed = self.get() != value
        self.variable.set(value)
        if self.command and (changed or getattr(self, "notify_same", False)):
            self.command(value)

    def _hover(self, value, inside):
        c = palette()
        label = self.buttons[value]
        if value == self.get() or not (self.all_enabled and self.enabled.get(value, True)):
            return
        label.configure(background=c["hover"] if inside else c["seg_bg"])

    def set_enabled(self, enabled: bool, value=None):
        if value is None:
            self.all_enabled = enabled
        else:
            self.enabled[str(value)] = enabled
        self.refresh()

    def refresh(self):
        c = palette()
        current = self.get()
        for value, label in self.buttons.items():
            usable = self.all_enabled and self.enabled.get(value, True)
            if value == current:
                kind = self.kinds[value]
                if kind == "accent":
                    bg, fg = c["seg_on"], "#ffffff"
                else:
                    bg, fg = c.get(f"{kind}_bg", c["seg_on"]), c.get(kind, "#ffffff")
                label.configure(background=bg, foreground=fg, cursor="hand2" if usable else "arrow")
            else:
                label.configure(background=c["seg_bg"], foreground=c["text"] if usable else c["faint"],
                                cursor="hand2" if usable else "arrow")


class HoldButton(ttk.Button):
    """Acts once on press and keeps repeating while held (like a keyboard key).

    can_repeat() is asked before each repeat, e.g. 'has the stage finished the last move?',
    so holding the button never queues up a backlog of moves.
    """

    def __init__(self, parent, action: Callable, can_repeat: Callable[[], bool] = lambda: True,
                 delay_ms=450, interval_ms=90, **kwargs):
        kwargs.pop("command", None)
        super().__init__(parent, **kwargs)
        self.action, self.can_repeat = action, can_repeat
        self.delay_ms, self.interval_ms = delay_ms, interval_ms
        self._job = None
        self._held = False
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        self.bind("<Leave>", self._release, add="+")

    def _press(self, _event=None):
        if self.instate(["disabled"]):
            return
        self._held = True
        self._fire()
        self._job = self.after(self.delay_ms, self._repeat)

    def _fire(self):
        try:
            result = self.action()
        except Exception as exc:  # a failed move already told the user; stop repeating
            print(f"Button action failed: {exc}")
            result = False
        if result is False:       # refused or failed: don't keep trying while the button is held
            self._release()

    def _repeat(self):
        self._job = None
        if not self._held or self.instate(["disabled"]):
            return
        if self.can_repeat():
            self._fire()
        if self._held:
            self._job = self.after(self.interval_ms, self._repeat)

    def _release(self, _event=None):
        self._held = False
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None


class FocusMeter(tk.Canvas):
    """Horizontal bar: current focus score relative to the best seen recently (peak hold)."""

    def __init__(self, parent, width=180, height=14, background=None):
        c = palette()
        super().__init__(parent, width=px(width), height=px(height), highlightthickness=0,
                         background=background or c["bg"], cursor="hand2")
        self.peak = 0.0
        self.value = 0.0
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", lambda _e: self.reset())
        add_tooltip(self, "Focus sharpness relative to the sharpest seen recently. Turn Z until the bar is "
                          "as long as it gets. Click to reset the peak.")

    def reset(self):
        self.peak = self.value
        self._draw()

    def update_value(self, value: Optional[float]):
        if value is None:
            self.value = 0.0
        else:
            self.value = max(0.0, float(value))
            self.peak = max(self.value, self.peak * 0.997)  # slowly forget an old peak
        self._draw()

    def fraction(self) -> float:
        return 0.0 if self.peak <= 0 else min(1.0, self.value / self.peak)

    def _draw(self):
        c = palette()
        self.delete("all")
        w, h = max(1, self.winfo_width()), max(1, self.winfo_height())
        self.create_rectangle(0, 0, w, h, fill=c["seg_bg"], outline="")
        f = self.fraction()
        color = c["ok"] if f > 0.9 else (c["warn"] if f > 0.6 else c["bad"])
        if self.value > 0:
            self.create_rectangle(0, 0, max(2, w * f), h, fill=color, outline="")
            self.create_line(w - 1, 0, w - 1, h, fill=c["text"])


class ScrollFrame(ttk.Frame):
    """Vertical scrolling container; the scrollbar appears only when needed. Content goes in .body."""

    def __init__(self, parent, padding=0, max_width=None, center=False, allow_wider=False):
        super().__init__(parent)
        c = palette()
        self.canvas = tk.Canvas(self, highlightthickness=0, background=c["bg"], bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.allow_wider = allow_wider
        if allow_wider:  # wide content (tables): scroll sideways instead of squeezing it
            self.xscrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
            self.canvas.configure(xscrollcommand=self._on_xscroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.body = ttk.Frame(self.canvas, padding=padding)
        self.item = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.max_width = max_width
        self.center = center
        self.body.bind("<Configure>", lambda _e: self._update_region())
        self.canvas.bind("<Configure>", self._fit)
        self.canvas._stepper_scroll = self  # found by the global mouse-wheel router

    def _on_scroll(self, first, last):
        self.scrollbar.set(first, last)
        needed = float(first) > 0.0 or float(last) < 1.0
        if needed and not self.scrollbar.winfo_ismapped():
            self.scrollbar.grid(row=0, column=1, sticky="ns")
        elif not needed and self.scrollbar.winfo_ismapped():
            self.scrollbar.grid_remove()

    def _on_xscroll(self, first, last):
        self.xscrollbar.set(first, last)
        needed = float(first) > 0.0 or float(last) < 1.0
        if needed and not self.xscrollbar.winfo_ismapped():
            self.xscrollbar.grid(row=1, column=0, sticky="ew")
        elif not needed and self.xscrollbar.winfo_ismapped():
            self.xscrollbar.grid_remove()

    def _update_region(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _fit(self, event):
        width = event.width
        if self.max_width:
            width = min(width, px(self.max_width))
        # Never wider than the visible area (that would clip the right edge); wide content wraps or
        # shrinks, unless the frame scrolls sideways.
        if self.allow_wider:
            width = max(width, self.body.winfo_reqwidth())
        self.canvas.itemconfigure(self.item, width=width)
        x = max(0, (event.width - width) // 2) if self.center else 0
        self.canvas.coords(self.item, x, 0)
        self._update_region()

    def scroll(self, units: int):
        first, last = self.canvas.yview()
        if first > 0.0 or last < 1.0:
            self.canvas.yview_scroll(units, "units")


def install_wheel_router(root):
    """Mouse wheel scrolls the ScrollFrame under the pointer (not the focused widget, as
    Windows would), and never fights widgets that scroll themselves."""
    def route(event):
        try:
            widget = root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        if widget is None:
            return
        if widget.winfo_class() in ("Listbox", "Treeview", "Text", "TCombobox", "TScale", "Scale", "TSpinbox"):
            return
        direction = -1 if getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0 else 1
        while widget is not None:
            target = getattr(widget, "_stepper_scroll", None)
            if target is not None:
                target.scroll(direction * 3)
                return "break"
            handler = getattr(widget, "_stepper_wheel", None)
            if handler is not None:
                return handler(direction)
            widget = getattr(widget, "master", None)
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(sequence, route)

    def scroll_page(event):
        # Drop-downs and number boxes would change their value under a scrolling mouse (easy to
        # do by accident: camera resolution, projector output, border). Scroll the page instead.
        widget = event.widget
        direction = -1 if getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0 else 1
        while widget is not None:
            target = getattr(widget, "_stepper_scroll", None)
            if target is not None:
                target.scroll(direction * 3)
                break
            widget = getattr(widget, "master", None)
        return "break"
    for widget_class in ("TCombobox", "TSpinbox"):
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            root.bind_class(widget_class, sequence, scroll_page)


class ValueSlider(ttk.Frame):
    """Label · value text · slider, with an optional 'Auto' checkbox.

    on_change(value) is called after the slider rests for a moment (and on release), so
    dragging does not flood the camera with commands.
    """

    def __init__(self, parent, label: str, low: float, high: float, value: float,
                 fmt: Callable[[float], str] = lambda v: f"{v:g}", on_change: Callable = None,
                 step: float = 1.0, auto_var: tk.BooleanVar = None, on_auto: Callable = None, delay_ms=120):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.low, self.high, self.step, self.fmt = low, high, step, fmt
        self.on_change = on_change
        self.delay_ms = delay_ms
        self._job = None
        self._quiet = False
        self.label = ttk.Label(self, text=label)
        self.label.grid(row=0, column=0, sticky="w")
        self.value_text = ttk.Label(self, text="", style="Muted.TLabel")
        self.value_text.grid(row=0, column=1, sticky="e")
        self.auto = None
        if auto_var is not None:
            self.auto = ttk.Checkbutton(self, text="Auto", variable=auto_var, bootstyle="round-toggle",
                                        command=(lambda: on_auto(auto_var.get())) if on_auto else None)
            self.auto.grid(row=0, column=2, sticky="e", padx=(px(10), 0))
        self.var = tk.DoubleVar(value=value)
        self.scale = ttk.Scale(self, from_=low, to=high, variable=self.var, command=self._moved)
        self.scale.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(px(2), 0))
        self.scale.bind("<ButtonRelease-1>", lambda _e: self._commit())
        self._show()

    def _snapped(self) -> float:
        value = round(self.var.get() / self.step) * self.step
        return max(self.low, min(self.high, value))

    def _show(self):
        self.value_text.configure(text=self.fmt(self._snapped()))

    def _moved(self, _value=None):
        self._show()
        if self._quiet:
            return
        if self._job is not None:
            self.after_cancel(self._job)
        self._job = self.after(self.delay_ms, self._commit)

    def _commit(self):
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        if self.on_change and not self._quiet:
            self.on_change(self._snapped())

    def get(self) -> float:
        return self._snapped()

    def set(self, value: float):
        """Show a value without reporting it as a user change."""
        self._quiet = True
        try:
            self.var.set(max(self.low, min(self.high, value)))
            self._show()
        finally:
            self._quiet = False

    def set_range(self, low, high):
        self.low, self.high = low, high
        self.scale.configure(from_=low, to=high)
        self._show()

    def set_enabled(self, enabled: bool):
        self.scale.configure(state="normal" if enabled else "disabled")


class Toast:
    """One-line status message at the bottom of the window that fades back to idle text."""

    def __init__(self, label: tk.Label, idle_text=""):
        self.label = label
        self.idle_text = idle_text
        self._job = None

    def show(self, text: str, kind: str = "muted", seconds: float = 6.0):
        c = palette()
        self.label.configure(text=text, foreground=c.get(kind, c["muted"]))
        if self._job is not None:
            self.label.after_cancel(self._job)
        self._job = self.label.after(int(seconds * 1000), self.clear)

    def clear(self):
        c = palette()
        self._job = None
        self.label.configure(text=self.idle_text, foreground=c["faint"])
