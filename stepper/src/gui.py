from tk_runtime import enable_font_support, safe_console
enable_font_support()
safe_console()

import os
import queue
import threading
import time
import tkinter
import webbrowser
from pathlib import Path
from tkinter import StringVar, filedialog, messagebox
from typing import Optional

import cv2
import numpy as np
import serial
import toml
import ttkbootstrap as ttk
from PIL import Image

from alignment_page import AlignmentPage
from camera.camera_module import CameraModule
from camera.processing import compute_focus_score
from camera.webcam import Webcam, setting_flag
from camera_view import CameraAdjustDialog, CameraBar, CameraFullscreen, CameraView, FocusTracker
from chip_pages import ChipFrame, MapFrame, TilingFrame, confirm_stage_move
from dispatcher import (AlignmentConfig, AutofocusStopped, Chip, ChipLayer, ConfigReader, Event,
                        EventDispatcher, ExposureLog, LED_FOCUS, LithographerConfig, MovementLock, RedFocusSource,
                        ShownImage, StageError, describe_stage_error, detect_alignment_markers, load_pattern_image,
                        write_rgb_image)
from projector import TkProjector
from projector_leds import ALL, LedController
from projector_panel import ProjectorPanel, ProjectorPreviewWindow
from projector_window import ProjectorWindow, choose_projector_display, list_displays
from settings import SettingsPage
from stage_control.grbl_stage import GrblStage
from stage_control.stage_controller import StageController
from stage_pad import StagePanel
from ui_kit import Card, ScrollFrame, Segmented, StatusDot, Toast, add_tooltip, install_wheel_router
from ui_theme import configure_theme, enable_dpi_awareness, palette, prepare_dropdowns, px
from steps_panel import WorkflowPanel

# The values set here are defaults; the config file overrides them
DEFAULT_RED_EXPOSURE: float = 4167.0
DEFAULT_UV_EXPOSURE: float = 25000.0
GUIDE_NAME = "HOW-TO-GUIDE.html"
GUIDE_URL = "https://github.com/illinoismicrotech/Hackerfab-Stepper"
PAGES = (("operate", "Operate"), ("chip", "Chip & map"), ("alignment", "Alignment"), ("settings", "Settings"))


def configure_ui_scale(root: tkinter.Tk) -> None:
    """Optional extra scaling: set HACKERFAB_UI_SCALE (e.g. 1.6) to make everything larger.
    Normally the app follows the Windows display scaling (it runs DPI-aware)."""
    try:
        requested = float(os.environ.get("HACKERFAB_UI_SCALE", "0"))
    except ValueError:
        return
    if requested > 0:
        root.tk.call("tk", "scaling", requested)


def find_guide() -> Optional[Path]:
    for candidate in (Path(GUIDE_NAME), Path("docs") / GUIDE_NAME):
        if candidate.exists():
            return candidate.resolve()
    return None


def open_guide():
    guide = find_guide()
    if guide is not None:
        webbrowser.open(guide.as_uri())
    else:
        messagebox.showinfo("Guide not found", f"{GUIDE_NAME} was not found next to the app.\n\n"
                            f"Put it in the same folder as the app, or see {GUIDE_URL}")


def parse_size(text) -> Optional[tuple[int, int]]:
    try:
        w, h = str(text).lower().replace("×", "x").split("x")
        size = (int(w), int(h))
        return size if size[0] > 0 and size[1] > 0 else None
    except (ValueError, AttributeError):
        return None


CONFIG_VERSION = 2


def migrate_config(config: dict) -> list[str]:
    """Bring a settings file saved by an older version up to date. Returns what changed."""
    app = config.setdefault("app", {})
    try:
        version = int(app.get("config-version", 1))
    except (TypeError, ValueError):
        version = 1
    changes = []
    if version < 2:
        cam = config.setdefault("camera", {})
        if str(cam.get("exposure", "auto")).strip().lower() not in ("auto", "camera"):
            # The old manual exposure was a workaround for the stripes; the new automatic
            # exposure finds the stripe-free value itself.
            changes.append(f"camera exposure {cam.get('exposure')} -> automatic")
            cam["exposure"] = "auto"
        if str(cam.get("mode", "auto")).lower() == "auto":
            cam["mode"] = "max"
            changes.append("camera resolution -> highest available")
    app["config-version"] = CONFIG_VERSION
    return changes


class LithographerGui:
    MOVABLE_WINDOW = "Movable window"

    def __init__(self, config: LithographerConfig, root: tkinter.Tk, settings=None, config_path="config.toml"):
        self.root = root
        self.config = settings if settings is not None else {}
        self.config_path = config_path
        ui = self.config.get("ui", {})
        configure_theme(root, ui.get("theme", "studio-dark"), ui.get("text-scale", 1.0))
        root.title("HackerFab Stepper")
        self._place_window()

        proj_cfg = self.config.get("projector", {})
        size = parse_size(proj_cfg.get("resolution", "auto")) or (1920, 1080)
        self.dispatcher = EventDispatcher(config.stage, TkProjector(root, size=size), root, config.camera,
                                          config.red_exposure, config.uv_exposure)
        d = self.dispatcher
        d.initialize_alignment(config)
        self._apply_saved_settings()
        d.ensure_output = self.ensure_output
        d.snapshot_requester = lambda path: self.request_snapshot(path, auto=True)
        d.add_event_listener(Event.NOTIFY, lambda text, kind="muted": self.toast.show(text, kind))

        self.focus_tracker = FocusTracker()
        self.camera_generation = 0
        self._pending_frame = None
        self.last_frame_at = 0.0
        self.snapshot_results = queue.Queue()
        self.camera_fullscreen: Optional[CameraFullscreen] = None
        self.adjust_dialog: Optional[CameraAdjustDialog] = None
        self.preview_window: Optional[ProjectorPreviewWindow] = None
        self._temporary_output = False
        self.projector_window = ProjectorWindow(root, on_escape=self._escape, on_close=self._projector_window_changed,
                                                can_close=self._projector_window_may_close)
        self.displays = list_displays(root)
        self.display_choice = ""

        self._build()
        d.hardware.projector.on_show = self._projector_updated
        install_wheel_router(root)
        prepare_dropdowns(root)
        root.bind("<F1>", lambda _e: open_guide())
        root.bind("<Escape>", lambda _e: self._escape(), add="+")
        root.bind("<KeyPress>", self.handle_key, add="+")
        root.bind_all("<Button-1>", self._click_focus, add="+")
        root.protocol("WM_DELETE_WINDOW", self.request_close)
        d.add_event_listener(Event.PATTERNING_BUSY_CHANGED, self._patterning_changed)
        d.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, self._lock_changed)
        # Autofocus may switch the focus bar to another image scale: start its "best so far" afresh.
        d.add_event_listener(Event.AUTOFOCUS_FINISHED, lambda: self.focus_tracker.reset())
        self.show_page("operate")
        self.refresh_display_choices()
        root.after(0, self._on_start)

    # ================================================================== setup
    def _place_window(self):
        root = self.root
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(px(1440), sw - px(40)), min(px(920), sh - px(90))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.minsize(min(px(980), sw - px(20)), min(px(620), sh - px(60)))

    def maximize(self):
        """A single laptop screen: use all of it."""
        if os.name == "nt":
            try:
                self.root.state("zoomed")
            except tkinter.TclError:
                pass

    def _apply_saved_settings(self):
        d, cfg = self.dispatcher, self.config
        cam = cfg.get("camera", {})
        proj = cfg.get("projector", {})
        files = cfg.get("files", {})
        try:
            d.red_brightness = max(0.02, min(1.0, float(proj.get("red-brightness", 100)) / 100))
        except (TypeError, ValueError):
            pass
        try:
            seconds = float(cfg.get("exposure", {}).get("seconds", 8))
            if 0 < seconds <= 3600:
                d.exposure_time = int(round(seconds * 1000))
        except (TypeError, ValueError):
            pass
        exposure = str(cam.get("exposure", "auto")).strip().lower()
        d.auto_camera.auto_exposure = exposure in ("auto", "app", "")
        d.auto_camera.auto_light = setting_flag(cam, "auto-light", True)
        d.auto_snapshot_on_uv = setting_flag(files, "auto-snapshot", True)
        folder = files.get("snapshot-folder")
        if folder:
            try:
                d.set_snapshot_directory(Path(folder))
            except OSError as exc:
                print(f"Snapshot folder {folder!r} unavailable ({exc}); using {d.snapshot_directory}")
        plus = Path("src/uvFocusImage/plus.png")
        if plus.exists():
            try:
                d.uv_focus_image = load_pattern_image(plus)
            except (OSError, ValueError):
                pass
        d._refresh_red_focus()
        d._refresh_uv_focus()

    def _build(self):
        root = self.root
        c = palette()
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        # ---- top bar
        top = ttk.Frame(root, padding=(px(14), px(8), px(14), px(8)))
        top.grid(row=0, column=0, sticky="ew")
        # The status column is the one that gives way on a narrow window, so STOP always stays visible.
        top.columnconfigure(2, weight=1)
        brand = ttk.Frame(top)
        brand.grid(row=0, column=0, sticky="w", padx=(0, px(18)))
        ttk.Label(brand, text="HackerFab", style="Caption.TLabel").pack(anchor="w")
        ttk.Label(brand, text="Stepper", font="StepperBrand").pack(anchor="w")
        self.page_var = StringVar(value="operate")
        self.nav = Segmented(top, PAGES, self.page_var, command=self.show_page, font="StepperBold", padx=14, pady=6,
                             equal=False)
        self.nav.grid(row=0, column=1, sticky="w")
        status = ttk.Frame(top)
        status.grid(row=0, column=2, sticky="e", padx=(px(12), px(12)))
        self.dots = {}
        for key, label in (("camera", "Camera"), ("stage", "Stage"), ("projector", "Projector"), ("leds", "LEDs")):
            dot = StatusDot(status, label)
            dot.pack(side="left", padx=(px(10), 0))
            self.dots[key] = dot
        self.help_button = ttk.Button(top, text="Help  F1", bootstyle="dark", command=open_guide)
        self.help_button.grid(row=0, column=4, padx=(0, px(8)))
        self.stop_button = ttk.Button(top, text="■  STOP", bootstyle="danger", command=self.stop_everything)
        self.stop_button.grid(row=0, column=5)
        add_tooltip(self.help_button, "Open the illustrated how-to guide (F1).")
        add_tooltip(self.stop_button, "Stops whatever is running (exposure, autofocus, homing, tiling) and turns "
                                      "the projector off. Esc does the same.")
        ttk.Separator(root).grid(row=1, column=0, sticky="ew")
        # ---- pages
        host = ttk.Frame(root)
        host.grid(row=2, column=0, sticky="nsew")
        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        self.pages = {}
        self.pages["operate"] = self._build_operate(host)
        chip = ScrollFrame(host, padding=px(18), allow_wider=True)
        self._build_chip_page(chip.body)
        self.pages["chip"] = chip
        alignment = ScrollFrame(host, padding=px(18), max_width=1100)
        self.alignment_page = AlignmentPage(alignment.body, self)
        self.alignment_page.grid(row=0, column=0, sticky="nsew")
        alignment.body.columnconfigure(0, weight=1)
        self.pages["alignment"] = alignment
        settings = ttk.Frame(host, padding=px(18))
        settings.columnconfigure(0, weight=1)
        settings.rowconfigure(0, weight=1)
        self.settings_page = SettingsPage(settings, self, self.config, self.config_path)
        self.settings_page.grid(row=0, column=0, sticky="nsew")
        self.pages["settings"] = settings
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()
        # ---- status line
        ttk.Separator(root).grid(row=3, column=0, sticky="ew")
        bottom = ttk.Frame(root, padding=(px(14), px(4)))
        bottom.grid(row=4, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        message = tkinter.Label(bottom, text="", anchor="w", font="StepperSmall", background=c["bg"], foreground=c["faint"])
        message.grid(row=0, column=0, sticky="ew")
        self.toast = Toast(message, "Ready.")
        self.toast.clear()
        ttk.Label(bottom, text="Arrows: move X/Y   PgUp/PgDn: focus   Shift: 10× step   Esc: STOP   F1: help",
                  style="Small.TLabel").grid(row=0, column=1, sticky="e")

    def _build_operate(self, host):
        page = ttk.Frame(host, padding=(px(12), px(10), px(12), px(10)))
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        left = ttk.Frame(page)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, px(12)))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.camera_view = CameraView(left, self)
        self.camera_view.grid(row=0, column=0, sticky="nsew")
        self.camera_bar = CameraBar(left, self)
        self.camera_bar.grid(row=1, column=0, sticky="ew")
        self.camera_bar.show_choice(self.config.get("camera", {}))
        stage_card = Card(left, "Stage", padding=10)
        stage_card.grid(row=2, column=0, sticky="ew", pady=(px(10), 0))
        self.stage_panel = StagePanel(stage_card.body, self)
        self.stage_panel.grid(row=0, column=0, sticky="ew")
        right = ttk.Frame(page, width=px(390))
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_propagate(False)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.projector_panel = ProjectorPanel(right, self)
        self.projector_panel.grid(row=0, column=0, sticky="ew")
        workflow_card = Card(right, "", padding=10)
        workflow_card.grid(row=1, column=0, sticky="nsew", pady=(px(10), 0))
        workflow_card.body.rowconfigure(0, weight=1)
        self.workflow = WorkflowPanel(workflow_card.body, self)
        self.workflow.grid(row=0, column=0, sticky="nsew")
        return page

    def _build_chip_page(self, body):
        d = self.dispatcher
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="Chip & map", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body, text="Where each pattern was exposed on this chip, layer by layer. Every exposure is added "
                             "automatically.", style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w",
                                                                        pady=(px(2), px(14)))
        map_card = Card(body, "Exposure map")
        map_card.grid(row=2, column=0, sticky="nw", padx=(0, px(14)))
        self.map = MapFrame(map_card.body, d)
        self.map.frame.grid(row=0, column=0)
        chip_card = Card(body, "Chip record")
        chip_card.grid(row=2, column=1, sticky="nsew")
        self.chip_frame = ChipFrame(chip_card.body, d)
        self.chip_frame.frame.grid(row=0, column=0, sticky="nsew")
        tiling_card = Card(body, "Tiling: large patterns over several fields (advanced)")
        tiling_card.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(px(14), 0))
        self.tiling_frame = TilingFrame(tiling_card.body, d)
        self.tiling_frame.frame.grid(row=0, column=0, sticky="w")

    # ================================================================== start / stop
    def _on_start(self):
        self.start_camera()
        self.connect_leds(quiet=True)
        self.dispatcher.show_red()
        self.auto_open_projector_window()
        self._poll_frames()
        self._status_tick()
        if self.dispatcher.hardware.stage.has_homing():
            self.dispatcher.home_stage()

    def notify(self, text, kind="muted"):
        self.toast.show(text, kind)

    def show_page(self, name):
        if name != "operate" and self.dispatcher.shown_image == ShownImage.UV_FOCUS:
            self.dispatcher.show_red()   # the blue focus mark exposes resist: don't leave it on unseen
        self.current_page = name
        self.page_var.set(name)
        for key, page in self.pages.items():
            if key == name:
                page.grid()
            else:
                page.grid_remove()

    def step_changed(self, step):
        d = self.dispatcher
        if d.patterning_busy:
            return
        if d.shown_image == ShownImage.UV_FOCUS and step != "expose":
            d.show_red()   # the blue focus mark exposes resist: only on the Expose step
        elif step == "focus" and d.shown_image == ShownImage.CLEAR:
            d.show_red()

    def handle_key(self, event):
        if self.camera_fullscreen is None and getattr(self, "current_page", "operate") != "operate":
            return None
        if self.stage_panel.handle_key(event):
            return "break"
        return None

    def _click_focus(self, event):
        """Clicking anywhere outside a text field gives the keyboard back to the stage shortcuts."""
        widget = event.widget
        try:
            if isinstance(widget, str) or widget.winfo_toplevel() is not self.root:
                return
            if widget.winfo_class() in ("Entry", "TEntry", "TSpinbox", "Spinbox", "TCombobox", "Text"):
                return
            self.root.focus_set()
        except (tkinter.TclError, KeyError, AttributeError):
            pass

    def _escape(self):
        d = self.dispatcher
        if d.busy_text():
            self.stop_everything()
        elif self._temporary_output and self.projector_window.is_open:
            self.projector_window.close()

    def stop_everything(self):
        d = self.dispatcher
        was = d.busy_text()
        d.stop_all()
        if was == "exposure":
            self.notify("Stopping the exposure… the projector turns off.", "warn")
        elif was:
            self.notify(f"Stopping the {was}…", "warn")
        else:
            self.notify("Projector turned off (black, LEDs off). Click Red light to see the chip again.", "warn")

    def _patterning_changed(self):
        busy = self.dispatcher.patterning_busy
        if not busy and self._temporary_output and self.projector_window.is_open:
            self.projector_window.close()   # it covered the controls; give the screen back
        self._lock_changed()

    def _lock_changed(self):
        """Controls that must not change while an exposure, autofocus, homing or tiling runs."""
        d = self.dispatcher
        busy = bool(d.busy_text())
        self.nav.set_enabled(not busy)
        self.camera_bar.set_locked(busy)
        self.projector_panel.set_locked(d.patterning_busy)

    def _refuse_while_busy(self, what: str) -> bool:
        """True (and a message in the status line) if something is running."""
        running = self.dispatcher.busy_text()
        if running:
            self.notify(f"{what} is not possible while the {running} runs. Press STOP (Esc) first.", "warn")
            return True
        return False

    def _projector_window_may_close(self) -> bool:
        if self.dispatcher.patterning_busy:
            self.notify("The projector window stays open during an exposure. Press STOP (Esc) to end it.", "warn")
            return False
        return True

    def request_close(self):
        d = self.dispatcher
        if d.patterning_busy:
            d.abort_patterning()
            self.root.after(200, self.request_close)  # let the exposure loop finish first
            return
        running = d.busy_text()
        if running:
            if not messagebox.askyesno("Still running", f"The {running} is still running. Stop it and close?"):
                return
            d.stop_all()
            self.root.after(300, self.request_close)
            return
        if not self.chip_frame.offer_to_save("Close HackerFab Stepper"):
            return
        try:
            self.settings_page.save("Settings saved.")  # remember display, brightness, steps … for next time
        except Exception as exc:
            print(f"Settings not saved on exit: {exc}")
        self.cleanup()

    def cleanup(self):
        if self.toast._job is not None:
            try:
                self.toast.label.after_cancel(self.toast._job)
            except tkinter.TclError:
                pass
        self.projector_window.close(notify=False)
        for window in (self.camera_fullscreen, self.adjust_dialog, self.preview_window):
            if window is not None:
                try:
                    window.close()
                except Exception:
                    pass
        self.camera_generation += 1
        camera = self.dispatcher.camera
        if camera:
            try:
                camera.close()
            except Exception as exc:
                print(f"Camera close failed: {exc}")
        if self.dispatcher.leds is not None:
            try:
                self.dispatcher.leds.close(0)   # LEDs off: nothing gets projected onto resist after exit
            except Exception:
                pass
        try:
            self.dispatcher.hardware.stage.close()
        except Exception:
            pass
        try:  # pending timers (pollers, tooltips) must not fire after the window is gone
            for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.tk.call("after", "cancel", job)  # raw cancel: widgets delete their own callbacks
        except tkinter.TclError:
            pass
        self.root.destroy()

    # ================================================================== camera
    def start_camera(self):
        camera = self.dispatcher.camera
        if camera is None:
            self.camera_view.canvas.set_message("Camera off", "Choose a camera in Settings → Camera.")
            return
        generation = self.camera_generation

        def on_frame(image, dimensions, fmt):
            if generation == self.camera_generation:
                self._pending_frame = (image, dict(getattr(camera, "latest_info", {}) or {}))

        camera.setStreamCaptureCallback(on_frame)
        if hasattr(camera, "snapshot_listener"):
            camera.snapshot_listener = lambda result: self.snapshot_results.put(result)
        self.camera_view.canvas.set_message("Connecting to the camera…", "Searching for the highest resolution it supports.")
        try:
            if not camera.open() or not camera.startStreamCapture():
                self.camera_view.canvas.set_message("Camera failed to start", "Click Reconnect, or choose a camera in Settings.")
        except Exception as exc:
            self.camera_view.canvas.set_message("Camera failed to start", str(exc))

    def _poll_frames(self):
        d = self.dispatcher
        try:
            packet, self._pending_frame = self._pending_frame, None
            if packet is not None:
                image, info = packet
                self.last_frame_at = time.monotonic()
                d.on_camera_frame(image, info)
                self._check_black_picture(info)
                self.focus_tracker.update(d.current_focus(blue_only=d.in_uv()))
                shown = image
                if d.model is not None and d.realtime_detection:
                    _, shown = detect_alignment_markers(d.model, image, draw_rectangle=True)
                if self.camera_fullscreen is not None:
                    self.camera_fullscreen.set_frame(shown, info)
                elif self.current_page == "operate":
                    self.camera_view.canvas.set_frame(shown, info)
                else:
                    self.camera_view.canvas.image, self.camera_view.canvas.info = shown, info
            else:
                camera = d.camera
                stale = self.last_frame_at and time.monotonic() - self.last_frame_at > 3.0
                if camera is not None and (getattr(camera, "state", "") == "error" or stale) and d.camera_image is not None:
                    d.camera_lost()
                    self.focus_tracker.update(None)
                if camera is not None and d.camera_image is None:
                    title = {"error": "No camera picture", "connecting": "Connecting to the camera…"}.get(
                        getattr(camera, "state", ""), "Waiting for the camera…")
                    detail = getattr(camera, "status", "")
                    if self.camera_view.canvas.message != (title, detail):
                        self.camera_view.canvas.set_message(title, detail)
                        if self.camera_fullscreen is not None:
                            self.camera_fullscreen.set_message(title, detail)
            while True:
                result = self.snapshot_results.get_nowait()
                if result.get("ok"):
                    w, h = result.get("size", (0, 0))
                    self.notify(f"Snapshot saved ({w} × {h}): {result['path']}", "ok")
                else:
                    self.notify(f"Snapshot failed: {result.get('error')}", "bad")
        except queue.Empty:
            pass
        except Exception as exc:
            print(f"Frame update failed: {exc}")
        self.root.after(30, self._poll_frames)

    def _check_black_picture(self, info):
        """Frames arrive but are black: say so (once per connection) and log the camera's settings."""
        stats = info.get("stats") or {}
        p99 = stats.get("p99")
        if p99 is None:
            return
        now = time.monotonic()
        if p99 >= 8:
            self._black_since = None
            return
        if getattr(self, "_black_since", None) is None:
            self._black_since = now
        if now - self._black_since < 4.0 or getattr(self, "_black_reported", -1) == self.camera_generation:
            return
        self._black_reported = self.camera_generation
        camera = self.dispatcher.camera
        print(f"Camera picture is black (brightest pixels {p99:.0f}/255). Status: {getattr(camera, 'status', '')}; "
              f"controls: {getattr(camera, 'controls', {})}; ranges: {getattr(camera, 'ranges', {})}; "
              f"actual: {getattr(camera, 'actual', {})}; red {self.dispatcher.red_brightness:.0%}; "
              f"projector: {self.dispatcher.shown_image.value}; auto: {self.dispatcher.auto_camera.status}")
        self.notify("The camera picture is black. Check that red light is on, then Adjust… → Reset. "
                    "Details are in HackerfabStepper.log.", "warn")

    def _status_tick(self):
        d = self.dispatcher
        camera = d.camera
        live = d.camera_image is not None
        if camera is None:
            self.camera_bar.update_status("disabled", "Camera off · Settings → Camera", False)
            self.dots["camera"].set("off", "Camera off")
        else:
            state = getattr(camera, "state", "streaming" if live else "connecting")
            text = getattr(camera, "status", "Vendor camera")
            self.camera_bar.update_status(state, text, live)
            self.dots["camera"].set("ok" if live else ("bad" if state == "error" else "warn"), "Camera")
        stage = d.hardware.stage
        if not isinstance(stage, GrblStage):
            self.dots["stage"].set("off", "Stage: simulation")
        elif stage.state_name().startswith("Alarm"):
            self.dots["stage"].set("bad", "Stage: ALARM")
        elif d.homing_busy:
            self.dots["stage"].set("warn", "Stage: homing")
        else:
            self.dots["stage"].set("ok", "Stage")
        window = self.projector_window
        if d.in_uv():
            self.dots["projector"].set("uv", "Projector: BLUE ON")
        elif window.is_open and window.display is not None and not window.display.primary:
            self.dots["projector"].set("ok", "Projector")
        elif window.is_open:
            self.dots["projector"].set("warn", "Projector: movable window")
        else:
            self.dots["projector"].set("bad", "Projector: no output")
        kind, _text = d.led_summary()
        self.dots["leds"].set(kind if kind != "off" else "off", "LEDs" if d.leds else "LEDs: off")
        self.root.after(500, self._status_tick)

    def set_crosshair(self, on: bool):
        self.camera_view.canvas.crosshair = on
        self.camera_view.canvas.render()
        if self.camera_fullscreen is not None:
            self.camera_fullscreen.canvas.crosshair = on

    def open_camera_fullscreen(self):
        if self.camera_fullscreen is None:
            self.camera_fullscreen = CameraFullscreen(self)

    def camera_fullscreen_closed(self):
        self.camera_fullscreen = None
        self.camera_view.canvas.render()

    def open_camera_adjustments(self):
        if self.adjust_dialog is not None and self.adjust_dialog.is_open:
            self.adjust_dialog.lift()
            return
        self.adjust_dialog = CameraAdjustDialog(self)

    def request_snapshot(self, path, auto=False):
        d = self.dispatcher
        camera = d.camera
        if camera is not None and hasattr(camera, "snapshot") and getattr(camera, "state", "") == "streaming":
            camera.snapshot(str(path))
            if not auto:
                self.notify("Saving snapshot…")
            return True
        if d.camera_image is not None:
            try:
                saved = write_rgb_image(path, d.camera_image)
                self.notify(f"Snapshot saved: {saved}", "ok")
                return True
            except (OSError, cv2.error) as exc:
                self.notify(f"Snapshot failed: {exc}", "bad")
        return False

    def take_snapshot(self):
        fmt = str(self.config.get("files", {}).get("snapshot-format", "png")).lower()
        fmt = fmt if fmt in ("png", "jpg", "tif", "bmp") else "png"
        if not self.request_snapshot(self.dispatcher.snapshot_path("snapshot", fmt)):
            self.notify("No camera picture to save.", "warn")

    def reconnect_camera(self, settings=None):
        if self._refuse_while_busy("Reconnecting the camera"):
            return
        d = self.dispatcher
        old = d.camera
        cam = dict(settings if settings is not None else self.config.get("camera", {}))
        learned = dict(getattr(old, "settings", {}) or {}) if isinstance(old, Webcam) else {}
        for key in ("stripe-free-exposure", "stripe-filter", "average-frames", "white-balance"):
            if key in learned and key not in (settings or {}):
                cam[key] = learned[key]
        if isinstance(old, Webcam) and settings is None:
            cam = {**learned, **{k: v for k, v in cam.items() if k in ("mode", "width", "height", "fps", "device", "backend")}}
        kind = cam.get("type", "webcam")
        try:
            if kind == "webcam":
                camera = Webcam(settings=cam)
            elif kind in ("basler", "pylon"):
                from camera.pylon import BaslerPylon
                camera = BaslerPylon(int(cam.get("index", 0)))
            elif kind == "flir":
                from camera.flir.flir_camera import FlirCamera
                camera = FlirCamera()
            else:
                camera = None
        except Exception as exc:
            messagebox.showerror("Camera", f"The camera could not be set up: {exc}")
            return
        self.replace_camera(camera)

    def replace_camera(self, camera):
        d = self.dispatcher
        self.camera_generation += 1
        old = d.camera
        if old is not None:
            try:
                old.close()
            except Exception as exc:
                print(f"Camera close failed: {exc}")
        d.camera = camera
        if d.config is not None:
            d.config.camera = camera
        d.camera_lost()
        d.reset_auto_camera()
        self._pending_frame = None
        self.last_frame_at = 0.0
        self.focus_tracker = FocusTracker()
        self.camera_view.canvas.image = None
        self.start_camera()
        if self.adjust_dialog is not None and self.adjust_dialog.is_open:
            self.adjust_dialog.close()

    def change_resolution(self, mode):
        if self._refuse_while_busy("Changing the camera resolution"):
            self.camera_bar.show_choice(self.config.get("camera", {}))
            return
        cam = dict(self.config.get("camera", {}))
        if mode is None:
            cam["mode"] = "max"
        else:
            cam.update(mode="manual", width=mode.width, height=mode.height, fps=mode.fps, fourcc="YUY2")
        self.config["camera"] = cam
        self.settings_page.show_resolution(cam)
        self.camera_bar.show_choice(cam)
        old = self.dispatcher.camera
        if isinstance(old, Webcam):
            merged = {**old.settings, **{k: cam[k] for k in ("mode", "width", "height", "fps", "fourcc") if k in cam}}
            self.reconnect_camera(merged)
        else:
            self.reconnect_camera(cam)
        self.notify("Reconnecting the camera at the new resolution…")

    def save_settings(self, message=None):
        return self.settings_page.save(message)

    # ================================================================== projector output
    def refresh_display_choices(self):
        self.displays = list_displays(self.root)
        choices = [d.label for d in self.displays] + [self.MOVABLE_WINDOW]
        if self.projector_window.is_open:
            current = self.projector_window.display.label if self.projector_window.display else self.MOVABLE_WINDOW
        elif self.display_choice in choices:
            current = self.display_choice
        else:
            target = choose_projector_display(self.displays, self.config.get("projector", {}).get("display", "auto"))
            current = target.label if target else self.MOVABLE_WINDOW
        self.display_choice = current
        window = self.projector_window
        problem = not (window.is_open and window.display is not None and not window.display.primary)
        self.projector_panel.show_output(choices, current, window.is_open, self._output_note(), problem)

    def _output_note(self):
        window = self.projector_window
        if window.is_open and window.display is not None:
            if window.display.primary:
                return "Output covers this screen (press Esc to close it when no exposure runs)."
            rate = f", {window.display.refresh} Hz" if getattr(window.display, "refresh", 0) else ""
            return (f"Projecting full screen on display {window.display.number} "
                    f"({window.display.width} × {window.display.height}{rate}).")
        if window.is_open:
            return "Movable window: drag it onto the projector, then double-click it for fullscreen."
        if len(self.displays) < 2:
            return "Only one display: Windows is mirroring the projector. Press Win+P → Extend, then pick it here."
        return "Output window closed. Starting an exposure opens it again."

    def _chosen_display(self):
        return next((d for d in self.displays if d.label == self.display_choice), None)

    def display_chosen(self, label):
        if self.dispatcher.patterning_busy:
            self.notify("The projector output cannot change during an exposure.", "warn")
            self.refresh_display_choices()
            return
        self.display_choice = label
        display = self._chosen_display()
        if display is not None and not display.primary:
            self.config.setdefault("projector", {})["display"] = display.number
        if self.projector_window.is_open:
            self.open_projector_window()
        else:
            self.refresh_display_choices()

    def open_projector_window(self, confirm_primary=True):
        self.displays = list_displays(self.root)   # displays may have changed (Win+P, cables)
        display = self._chosen_display()
        if display is not None and display.primary and confirm_primary:
            if not messagebox.askyesno("Projector output", "That is your main screen, so the output window would cover "
                                       "these controls (Esc closes it).\n\nTo use the projector as its own screen, "
                                       "press Win+P and choose Extend.\n\nOpen it on the main screen anyway?"):
                return False
        self._temporary_output = bool(display is not None and display.primary)
        if display is not None:
            self._match_projector_resolution(display)
        self.dispatcher.projector_refresh_hz = getattr(display, "refresh", 0) if display is not None else 0
        self.projector_window.open(display, self._projection_image())
        self._projector_window_changed()
        return True

    def _match_projector_resolution(self, display):
        setting = str(self.config.get("projector", {}).get("resolution", "auto")).lower()
        if setting == "auto":
            self.dispatcher.set_projector_size((display.width, display.height))

    def apply_projector_resolution(self, value):
        if self.dispatcher.patterning_busy:
            self.notify("The image resolution cannot change during an exposure.", "warn")
            return
        size = parse_size(value)
        if size is not None:
            self.dispatcher.set_projector_size(size)
        elif self.projector_window.is_open and self.projector_window.display is not None:
            self.dispatcher.set_projector_size((self.projector_window.display.width, self.projector_window.display.height))
        self.config.setdefault("projector", {})["resolution"] = value
        w, h = self.dispatcher.hardware.projector.size()
        self.notify(f"Projector images are now prepared at {w} × {h}.", "ok")

    def toggle_projector_window(self):
        if self.projector_window.is_open:
            if not self._projector_window_may_close():
                return
            self.projector_window.close()
        else:
            self.open_projector_window()

    def auto_open_projector_window(self):
        if str(self.config.get("projector", {}).get("window", "auto")).lower() != "auto":
            return
        target = choose_projector_display(self.displays, self.config.get("projector", {}).get("display", "auto"))
        if target is not None:
            self.display_choice = target.label
            self.open_projector_window()

    def ensure_output(self) -> bool:
        """Before an exposure: make sure the pattern will actually reach the projector."""
        if self.projector_window.is_open:
            return True
        self.displays = list_displays(self.root)
        chosen = self._chosen_display()
        if chosen is None or chosen.primary:
            chosen = choose_projector_display(self.displays, self.config.get("projector", {}).get("display", "auto"))
        if chosen is not None and not chosen.primary:
            self.display_choice = chosen.label
            return self.open_projector_window()
        if not messagebox.askyesno("No projector display", "Windows shows only one display, so the projector is "
                                   "mirroring this screen (or is not connected).\n\nShow the pattern fullscreen on this "
                                   "screen for the exposure? It closes again when the exposure ends.\n\n(Better: press "
                                   "Win+P, choose Extend, and pick the projector on the Projector panel.)"):
            return False
        main = next((d for d in self.displays if d.primary), None)
        if main is not None:
            self.display_choice = main.label
        return self.open_projector_window(confirm_primary=False)

    def output_check(self):
        window = self.projector_window
        if window.is_open and window.display is not None and not window.display.primary:
            return "Ok.TLabel", f"✓  Projector output on {window.display.label}"
        if window.is_open:
            return "Warn.TLabel", "•  Output is a movable window: make sure it is fullscreen on the projector"
        if any(not d.primary for d in self.displays):
            return "Small.TLabel", "•  Projector output opens automatically when you start"
        return "Warn.TLabel", "•  Only one display: the pattern will fill this screen during the exposure"

    def _projection_image(self):
        return getattr(self.dispatcher.hardware.projector, "current_image", None)

    def _projector_updated(self):
        if self.projector_window.is_open:
            self.projector_window.show(self._projection_image())

    def _projector_window_changed(self):
        if not self.projector_window.is_open:
            self._temporary_output = False
        self.refresh_display_choices()

    def open_projector_preview(self):
        if self.preview_window is not None and self.preview_window.is_open:
            self.preview_window.lift()
            return
        self.preview_window = ProjectorPreviewWindow(self)

    # ================================================================== LEDs
    def connect_leds(self, quiet=False):
        d = self.dispatcher
        if d.patterning_busy:
            self.notify("LED control cannot change during an exposure.", "warn")
            return
        proj = self.config.get("projector", {})
        if str(proj.get("led-control", "auto")).lower() in ("off", "false", "no"):
            d.attach_leds(None, "Projector LED control is off (Settings → Projector).")
            return
        self.disconnect_leds(restore=False)
        pid = proj.get("led-pid")
        try:
            pid = int(str(pid), 0) if pid not in (None, "", "auto") else None
        except ValueError:
            pid = None
        controller, status = LedController.connect(pid)
        d.attach_leds(controller, status)
        if not quiet or controller is not None:
            self.notify(status, "ok" if controller is not None else "warn")

    def disconnect_leds(self, restore=True):
        d = self.dispatcher
        if d.patterning_busy:
            self.notify("LED control cannot change during an exposure.", "warn")
            return
        if d.leds is not None:
            try:
                d.leds.close(ALL if restore else None)
            except Exception:
                pass
        d.attach_leds(None, "Projector LED control is off (Settings → Projector).")

    def test_leds(self):
        d = self.dispatcher
        if d.leds is None:
            messagebox.showinfo("Test LEDs", d.led_status)
            return
        if self._refuse_while_busy("Testing the LEDs"):
            return
        previous_image, previous_source = d.shown_image, d.red_focus_source
        d.show_red(RedFocusSource.SOLID)
        window = tkinter.Toplevel(self.root)
        window.title("Testing projector LEDs")
        window.transient(self.root)
        window.resizable(False, False)
        label = ttk.Label(window, text="", font="StepperBig", padding=px(20), wraplength=px(420))
        label.pack()
        steps = [("Red LED on: the projector shows red.", LED_FOCUS),
                 ("All LEDs off: the projector should go dark now.", 0),
                 ("Red LED on again.", LED_FOCUS)]
        state = {"job": None, "done": False}

        def restore():
            if state["done"]:
                return
            state["done"] = True
            if state["job"] is not None:
                try:
                    self.root.after_cancel(state["job"])
                except tkinter.TclError:
                    pass
            try:
                window.grab_release()
                window.destroy()
            except tkinter.TclError:
                pass
            if previous_source != RedFocusSource.SOLID:
                d.set_red_focus_source(previous_source)
            d.set_shown_image(previous_image)
            d.set_leds(d.led_mask_for(d.shown_image))   # the LEDs match the picture again

        def run(index=0):
            state["job"] = None
            if state["done"]:
                return
            if index < len(steps):
                text, mask = steps[index]
                try:
                    label.configure(text=f"Step {index + 1} of {len(steps)}\n\n{text}")
                except tkinter.TclError:   # the window was closed
                    restore()
                    return
                if not d.set_leds(mask):
                    label.configure(text="The projector did not accept the USB command.")
                    state["job"] = self.root.after(2500, restore)
                    return
                state["job"] = self.root.after(2500, lambda: run(index + 1))
                return
            restore()
            if messagebox.askyesno("Test LEDs", "Did the projector go dark during step 2?"):
                self.notify("Projector LED control works.", "ok")
            else:
                messagebox.showinfo("Test LEDs", "The projector accepted the commands but nothing changed. Its "
                                    "firmware may not support switching LEDs this way. Turn LED control off in "
                                    "Settings → Projector; everything else still works.")

        window.protocol("WM_DELETE_WINDOW", restore)
        window.bind("<Escape>", lambda _e: restore())
        window.bind("<Destroy>", lambda e: restore() if e.widget is window else None)
        window.update_idletasks()
        try:
            window.grab_set()   # nothing else can change the projector during the test
        except tkinter.TclError:
            pass
        run()

    # ================================================================== keyboard focus helpers
    def focus_widget_is_text(self):
        widget = self.root.focus_get()
        return widget is not None and widget.winfo_class() in ("Entry", "TEntry", "TSpinbox", "TCombobox", "Text")


class _StartupDialog:
    """Pre-launch dialog: config file selection + GRBL board detection.

    Runs its own Tk() event loop.  On a successful launch, self.config and
    self.selected_port are populated before the window is destroyed.
    """

    _GRBL_VIDS = {
        0x2341,  # Arduino LLC
        0x2A03,  # Arduino.org
        0x239A,  # Adafruit
        0x1B4F,  # SparkFun
        0x1A86,  # CH340 (ubiquitous clone chip)
        0x0403,  # FTDI
        0x10C4,  # Silicon Labs CP210x
        0x067B,  # Prolific PL2303
        0x2E8A,  # Raspberry Pi RP2040 / BTT SKR Pico
    }

    def __init__(self, master: tkinter.Tk):
        self.win = tkinter.Toplevel(master)
        self.win.title("HackerFab Stepper – Setup")
        self.win.resizable(False, False)
        self.win.grab_set()  # modal: blocks interaction with the hidden root window

        self.config: dict = {}
        self.selected_port: Optional[str] = None   # None → no stage
        self.launched: bool = False
        self._port_devices: list[str] = []

        self._build_ui()
        prepare_dropdowns(self.win)
        self._center()
        self.win.after(50, self._scan_ports)        # scan after window renders

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        f = ttk.Frame(self.win, padding=16)
        f.grid(sticky="nsew")

        ttk.Label(f, text="HackerFab Stepper",
                  font="StepperTitle").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Photolithography stepper control",
                  style="Muted.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Separator(f, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        # Config file
        ttk.Label(f, text="Config file:").grid(
            row=3, column=0, sticky="e", padx=(0, 8), pady=4)
        self._config_var = StringVar(value="config.toml" if Path("config.toml").exists() else "default.toml")
        ttk.Entry(f, textvariable=self._config_var, width=40).grid(
            row=3, column=1, sticky="ew", pady=4)
        ttk.Button(f, text="Browse…", command=self._browse,
                   bootstyle="dark").grid(
            row=3, column=2, padx=(8, 0), pady=4)

        ttk.Separator(f, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=10)

        # Stage port
        ttk.Label(f, text="Stage port:").grid(
            row=5, column=0, sticky="e", padx=(0, 8), pady=4)
        self._port_var = StringVar(value="Scanning…")
        self._combo = ttk.Combobox(f, textvariable=self._port_var,
                                   state="readonly", width=40)
        self._combo.grid(row=5, column=1, sticky="ew", pady=4)
        ttk.Button(f, text="↺", width=3, command=self._scan_ports,
                   bootstyle="dark").grid(
            row=5, column=2, padx=(8, 0), pady=4)

        self._status_var = StringVar()
        self._status_lbl = ttk.Label(f, textvariable=self._status_var, style="Small.TLabel", wraplength=px(460))
        self._status_lbl.grid(row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Separator(f, orient="horizontal").grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=10)

        btns = ttk.Frame(f)
        btns.grid(row=8, column=0, columnspan=3, sticky="e")
        ttk.Button(btns, text="Cancel", command=self.win.destroy,
                   bootstyle="dark").pack(side="left", padx=4)
        ttk.Button(btns, text="Launch  →", command=self._launch,
                   bootstyle="success").pack(side="left", padx=4)

        f.columnconfigure(1, weight=1)

    def _center(self) -> None:
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    # ── Actions ──────────────────────────────────────────────────────────

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.win,
            title="Select config file",
            filetypes=[("TOML files", "*.toml"), ("All files", "*.*")],
        )
        if path:
            self._config_var.set(path)
            self._scan_ports()

    def _scan_ports(self) -> None:
        from serial.tools import list_ports
        ports = list_ports.comports()
        grbl  = [p for p in ports if (p.vid or 0) in self._GRBL_VIDS]
        other = [p for p in ports if (p.vid or 0) not in self._GRBL_VIDS]

        entries: list[tuple[str, str]] = []
        for p in grbl:
            entries.append((p.device,
                            f"{p.device}  –  {p.description}  [Arduino / GRBL]"))
        for p in other:
            entries.append((p.device, f"{p.device}  –  {p.description}"))
        entries.append(("none", "No stage  (run without hardware)"))

        self._port_devices    = [e[0] for e in entries]
        self._combo["values"] = [e[1] for e in entries]

        try:
            stage_settings = toml.load(self._config_var.get()).get("stage", {})
        except (OSError, ValueError):
            stage_settings = {}
        saved_port = str(stage_settings.get("port", "auto"))
        if not stage_settings.get("enabled", True):
            saved_port = "none"
        if saved_port != "auto":
            if saved_port not in self._port_devices:
                self._port_devices.insert(0, saved_port)
                self._combo["values"] = [f"{saved_port} (saved; unavailable)"] + [e[1] for e in entries]
            self._combo.current(self._port_devices.index(saved_port))
            self._status(f"Saved stage selection: {saved_port}")
            return

        if grbl:
            self._combo.current(0)
            if len(grbl) == 1:
                self._status(f"✓  Auto-selected: {grbl[0].description}", "green")
            else:
                self._status(
                    f"{len(grbl)} GRBL boards found – please select one", "dark orange")
        elif ports:
            if len(other) == 1:
                self._combo.current(0)
                self._status(
                    f"Unrecognized serial device auto-selected: {other[0].description}",
                    "dark orange",
                )
            else:
                self._combo.current(len(entries) - 1)
                self._status(
                    "No Arduino / GRBL board detected – select the controller from the list", "#cc5500")
        else:
            self._combo.current(len(entries) - 1)
            self._status(
                "No serial ports found – connect the board then press  ↺", "#cc5500")

    def _status(self, msg: str, color: str = "gray") -> None:
        kind = {"green": "ok", "dark orange": "warn", "#cc5500": "bad"}.get(color, "muted")
        self._status_var.set(msg)
        self._status_lbl.configure(foreground=palette()[kind])

    def _launch(self) -> None:
        path = self._config_var.get().strip()
        try:
            with open(path, "r") as fh:
                self.config = toml.load(fh)
        except FileNotFoundError:
            messagebox.showerror(
                "Config not found", f"File not found:\n{path}", parent=self.win)
            return
        except Exception as e:
            messagebox.showerror("Config error", str(e), parent=self.win)
            return

        idx = self._combo.current()
        self.selected_port = self._port_devices[idx] if idx >= 0 else None
        self.launched = True
        self.win.destroy()

    # ── Entry point ──────────────────────────────────────────────────────

    def run(self) -> bool:
        """Block until the dialog is closed; return True if the user clicked Launch."""
        self.win.wait_window()
        return self.launched




def main():
    enable_dpi_awareness()
    # One root window for the entire app lifetime — ttkbootstrap Style binds to it
    # and must never be destroyed and recreated.
    root = ttk.Window(themename="darkly")
    configure_ui_scale(root)
    configure_theme(root)
    root.withdraw()  # stay hidden until the main UI is ready

    dialog = _StartupDialog(root)
    if not dialog.run():
        root.destroy()
        return

    config = dialog.config
    for change in migrate_config(config):
        print(f"Settings updated for this version: {change}")
    reader = ConfigReader(config)
    number, flag, problems = reader.number, reader.flag, reader.problems

    # ── Stage ────────────────────────────────────────────────────────────
    port = dialog.selected_port
    stage = StageController()
    if port and port != "none":
        # Picking a port in the startup window means "use the stage", even if it was turned off.
        config.setdefault("stage", {})["enabled"] = True
        sp = None
        try:
            sp = serial.Serial(port, int(number("stage", "baud-rate", 115200, positive=True)))
            stage = GrblStage(sp, flag("stage", "homing", False), invert_z=flag("stage", "invert-z", True))
        except Exception as e:
            print(f"Stage connection failed ({port}): {e}")
            try:
                if sp is not None:
                    sp.close()
            except Exception:
                pass
            problems.append(f"Could not connect to the stage on {port}: {e}.\n"
                            "  The app is running in simulation, so the stage will not move. Close other "
                            "programs using that port, check the cable and power, then restart.")

    # ── Camera ───────────────────────────────────────────────────────────
    cam_cfg = config.setdefault("camera", {})
    cam_type = cam_cfg.get("type", "none")
    try:
        if cam_type == "webcam":
            camera = Webcam(cam_cfg.get("device", cam_cfg.get("index", "auto")), settings=cam_cfg)
        elif cam_type == "flir":
            import camera.flir.flir_camera as flir
            camera = flir.FlirCamera()
        elif cam_type in ("basler", "pylon"):
            from camera.pylon import BaslerPylon
            camera = BaslerPylon(int(cam_cfg.get("index", 0)))
        elif cam_type == "none":
            camera = None
        else:
            problems.append(f"[camera] type = {cam_type!r} is not recognized; the camera is disabled. "
                            "Use \"webcam\" for USB cameras.")
            camera = None
    except Exception as exc:
        print(f"Camera initialization failed: {exc}. Choose a camera in Settings.")
        problems.append(f"The camera could not be started ({exc}). Choose a camera in Settings.")
        camera = None

    camera_scale = number("camera", "gui-scale", 1.0, positive=True)
    red_exposure = number("camera", "red-exposure", DEFAULT_RED_EXPOSURE, positive=True)
    uv_exposure = number("camera", "uv-exposure", DEFAULT_UV_EXPOSURE, positive=True)

    # ── Alignment ────────────────────────────────────────────────────────
    ac = config.get("alignment", {})
    alignment_config = AlignmentConfig(
        enabled=flag("alignment", "enabled", False),
        model_path=ac.get("model_path", "ckpts/best.pt"),
        right_marker_x=number("alignment", "right_marker_x", 1820.0),
        left_marker_x=number("alignment", "left_marker_x", 280.0),
        top_marker_y=number("alignment", "top_marker_y", 269.0),
        bottom_marker_y=number("alignment", "bottom_marker_y", 1075.0),
        x_scale_factor=number("alignment", "x_scale_factor", -1100),
        y_scale_factor=number("alignment", "y_scale_factor", 800),
    )

    if problems:
        messagebox.showwarning("Check your settings", "\n\n".join(problems))

    chosen = Path(dialog._config_var.get())
    save_path = "config.toml" if chosen.resolve() == Path("default.toml").resolve() else str(chosen)
    app = LithographerGui(LithographerConfig(stage, camera, camera_scale, red_exposure, uv_exposure, alignment_config),
                          root, config, save_path)
    root.deiconify()  # show the main window now that it's fully built
    app.maximize()
    root.mainloop()


if __name__ == "__main__":
    main()
