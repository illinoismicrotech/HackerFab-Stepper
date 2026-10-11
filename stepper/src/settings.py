"""Settings page and atomic saving of the settings file."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import os
import platform
import queue
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import cv2
import toml
import ttkbootstrap as ttk

from camera.discovery import discover_devices
from camera.modes import Mode, dedupe_resolutions, preset_modes
from camera.webcam import Webcam, capture_backend, setting_flag
from platform_text import DISPLAY_SCALING, EXTEND_DISPLAYS, open_path
from ui_kit import ScrollFrame, add_tooltip
from ui_theme import px


def save_config(path, config):
    path = Path(path)
    content = toml.dumps(config)
    if path.exists():
        backup = path.with_name(path.name + datetime.now().strftime('.%Y%m%d-%H%M%S-%f.bak'))
        shutil.copy2(path, backup)
        # keep the five newest backups
        backups = sorted(path.parent.glob(path.name + '.*.bak'))
        for old in backups[:-5]:
            try:
                old.unlink()
            except OSError:
                pass
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(content, encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Choice:
    """A read-only combobox that shows friendly labels but stores a value."""

    def __init__(self, parent, options, value, width=34, command=None):
        self.options = list(options)  # [(value, label)]
        self.var = tk.StringVar()
        self.widget = ttk.Combobox(parent, textvariable=self.var, state='readonly', width=width,
                                   values=[label for _, label in self.options])
        self.command = command
        self.widget.bind('<<ComboboxSelected>>', lambda _e: command(self.get()) if command else None)
        self.set(value)

    def set_options(self, options, keep=True):
        current = self.get() if keep else None
        self.options = list(options)
        self.widget.configure(values=[label for _, label in self.options])
        if current is not None:
            self.set(current)

    def set(self, value):
        for key, label in self.options:
            if str(key) == str(value):
                self.var.set(label)
                return
        if value is None or str(value) == '':
            if self.options:
                self.var.set(self.options[0][1])
            return
        # A value from the settings file that is not in the list: keep it (never silently replace it).
        label = f'{value} (from the settings file)'
        self.options.append((value, label))
        self.widget.configure(values=[text for _, text in self.options])
        self.var.set(label)

    def get(self):
        text = self.var.get()
        return next((key for key, label in self.options if label == text), self.options[0][0] if self.options else None)


def form_row(parent, row, label, widget, hint=None):
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, px(18)), pady=px(6))
    widget.grid(row=row, column=1, sticky='w', pady=px(6))
    if hint:
        ttk.Label(parent, text=hint, style='Small.TLabel', wraplength=px(560), justify='left').grid(
            row=row + 1, column=1, sticky='w', pady=(0, px(6)))


class SettingsPage(ttk.Frame):
    RESOLUTION_HIGHEST = 'max'

    def __init__(self, parent, app, config, path):
        super().__init__(parent)
        self.app, self.config, self.path = app, config, Path(path)
        self.results = queue.Queue()
        self.devices = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        ttk.Label(self, text='Settings', style='Title.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(self, text='Settings are saved when you close the app (or now, with Save settings). '
                             'Changes marked "after restart" take effect the next time the app starts.',
                  style='Muted.TLabel').grid(
            row=1, column=0, sticky='w', pady=(px(2), px(12)))
        notebook = ttk.Notebook(self)
        notebook.grid(row=2, column=0, sticky='nsew')
        self.notebook = notebook
        pages = {}
        for title in ('Camera', 'Projector', 'Stage', 'Files', 'Appearance'):
            scroll = ScrollFrame(notebook, padding=px(18))
            notebook.add(scroll, text=title)
            scroll.body.columnconfigure(1, weight=1)
            pages[title] = scroll.body
        self._camera_page(pages['Camera'])
        self._projector_page(pages['Projector'])
        self._stage_page(pages['Stage'])
        self._files_page(pages['Files'])
        self._appearance_page(pages['Appearance'])
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky='ew', pady=(px(12), 0))
        ttk.Button(actions, text='Save settings', bootstyle='primary', command=self.save).pack(side='left')
        self.notice = tk.StringVar(value=f'Settings file: {self.path}')
        ttk.Label(actions, textvariable=self.notice, style='Small.TLabel', wraplength=px(700)).pack(
            side='left', padx=px(14))
        self.scan()
        self.tick()

    # ------------------------------------------------------------------ pages
    def _camera_page(self, page):
        cam = self.config.setdefault('camera', {})
        row = 0
        ttk.Label(page, text='Camera', style='Section.TLabel').grid(row=row, column=0, columnspan=2, sticky='w')
        row += 1
        self.camera_type = Choice(page, [('webcam', 'USB camera (Arducam and other webcams)'), ('basler', 'Basler'),
                                         ('flir', 'FLIR'), ('none', 'No camera')], cam.get('type', 'webcam'), width=44)
        form_row(page, row, 'Camera type', self.camera_type.widget)
        row += 1
        device_row = ttk.Frame(page)
        device_value = str(cam.get('device', cam.get('index', 'auto')))
        self.device = Choice(device_row, [('auto', 'Automatic (prefers the Arducam)')], device_value, width=44)
        self.device.widget.pack(side='left')
        ttk.Button(device_row, text='Refresh', bootstyle='dark', command=self.scan).pack(side='left', padx=(px(6), 0))
        self.connection_hint = tk.StringVar(value='')
        form_row(page, row, 'Device', device_row)
        row += 1
        ttk.Label(page, textvariable=self.connection_hint, style='Small.TLabel', wraplength=px(560)).grid(
            row=row, column=1, sticky='w')
        row += 1
        self.resolution = Choice(page, self._resolution_options(), self._current_resolution(cam), width=44)
        self.resolution.widget.configure(postcommand=lambda: self.resolution.set_options(self._resolution_options()))
        form_row(page, row, 'Resolution', self.resolution.widget,
                 'Highest gives the most detail at the lowest frame rate (B0477: 5472 × 3648 at 9 fps). '
                 'The list shows what your camera reports once it has connected.')
        row += 2
        self.backend = None   # Windows always uses DirectShow: the only way the Arducam accepts exposure changes
        self.reject = tk.BooleanVar(value=cam.get('reject-green', True))
        ttk.Checkbutton(page, text='Skip corrupted (solid green) frames', variable=self.reject,
                        bootstyle='round-toggle').grid(row=row, column=1, sticky='w', pady=px(6))
        row += 1
        buttons = ttk.Frame(page)
        buttons.grid(row=row, column=1, sticky='w', pady=(px(10), 0))
        ttk.Button(buttons, text='Apply and reconnect camera', bootstyle='primary', command=self.apply_camera).pack(side='left')
        ttk.Button(buttons, text='Camera adjustments…', bootstyle='dark',
                   command=self.app.open_camera_adjustments).pack(side='left', padx=(px(8), 0))
        row += 1
        self.camera_status = tk.StringVar(value='')
        ttk.Label(page, textvariable=self.camera_status, style='Small.TLabel', wraplength=px(620), justify='left').grid(
            row=row, column=1, sticky='w', pady=(px(10), 0))
        row += 1
        ttk.Button(page, text='Copy camera diagnostics', bootstyle='dark', command=self.copy_diagnostics).grid(
            row=row, column=1, sticky='w', pady=(px(10), 0))

    def _projector_page(self, page):
        proj = self.config.setdefault('projector', {})
        row = 0
        ttk.Label(page, text='Projector output', style='Section.TLabel').grid(row=row, column=0, columnspan=2, sticky='w')
        row += 1
        self.open_at_start = tk.BooleanVar(value=str(proj.get('window', 'auto')).lower() == 'auto')
        ttk.Checkbutton(page, text='Open the projector window on the projector display when the app starts',
                        variable=self.open_at_start, bootstyle='round-toggle',
                        command=self._projector_window_setting).grid(row=row, column=1, sticky='w', pady=px(6))
        row += 1
        ttk.Label(page, text='Choose the display on the Projector panel (Operate page). The projector must be its own '
                             f'display, not a mirror of this screen: {EXTEND_DISPLAYS}.', style='Small.TLabel',
                  wraplength=px(560)).grid(
            row=row, column=1, sticky='w')
        row += 1
        self.proj_resolution = Choice(page, [('auto', 'Same as the projector display (recommended)'),
                                             ('1920x1080', '1920 × 1080'), ('3840x2160', '3840 × 2160'),
                                             ('1280x720', '1280 × 720')], proj.get('resolution', 'auto'), width=46,
                                      command=self.app.apply_projector_resolution)
        form_row(page, row, 'Image resolution', self.proj_resolution.widget,
                 'Patterns are prepared at exactly this size so the projector shows them pixel for pixel.')
        row += 2
        ttk.Separator(page).grid(row=row, column=0, columnspan=2, sticky='ew', pady=px(14))
        row += 1
        ttk.Label(page, text='Projector LEDs (USB)', style='Section.TLabel').grid(row=row, column=0, columnspan=2, sticky='w')
        row += 1
        self.led_control = tk.BooleanVar(value=str(proj.get('led-control', 'auto')).lower() not in ('off', 'false', 'no'))
        ttk.Checkbutton(page, text='Control the projector LEDs over USB (red only while focusing, blue on for '
                                   'exposures, all off when the projector is clear)', variable=self.led_control,
                        bootstyle='round-toggle', command=self._led_setting).grid(row=row, column=1, sticky='w', pady=px(6))
        row += 1
        ttk.Label(page, text='Needs the projector board\'s USB port connected to this computer. Removes the coloured '
                             'bands caused by the unused LEDs and keeps stray blue light off the resist. Close TI\'s '
                             'DLP EVM GUI while this app runs.', style='Small.TLabel', wraplength=px(560),
                  justify='left').grid(row=row, column=1, sticky='w')
        row += 1
        self.led_status = tk.StringVar(value='')
        ttk.Label(page, textvariable=self.led_status, wraplength=px(560), justify='left').grid(
            row=row, column=1, sticky='w', pady=(px(8), 0))
        row += 1
        buttons = ttk.Frame(page)
        buttons.grid(row=row, column=1, sticky='w', pady=(px(8), 0))
        ttk.Button(buttons, text='Reconnect LEDs', bootstyle='dark', command=self.app.connect_leds).pack(side='left')
        ttk.Button(buttons, text='Test LEDs', bootstyle='dark', command=self.app.test_leds).pack(side='left', padx=(px(8), 0))

    def _stage_page(self, page):
        stage = self.config.setdefault('stage', {})
        row = 0
        ttk.Label(page, text='Stage', style='Section.TLabel').grid(row=row, column=0, columnspan=2, sticky='w')
        row += 1
        self.stage_enabled = tk.BooleanVar(value=stage.get('enabled', True))
        ttk.Checkbutton(page, text='Use the motion stage (after restart)', variable=self.stage_enabled,
                        bootstyle='round-toggle').grid(row=row, column=1, sticky='w', pady=px(6))
        row += 1
        self.stage_port = tk.StringVar(value=str(stage.get('port', 'auto')))
        form_row(page, row, 'Serial port', ttk.Entry(page, textvariable=self.stage_port, width=16),
                 '"auto" finds the controller (the startup window also lets you pick). After restart.')
        row += 2
        self.homing = tk.BooleanVar(value=stage.get('homing', False))
        ttk.Checkbutton(page, text='Stage has limit switches: home at startup (after restart)', variable=self.homing,
                        bootstyle='round-toggle').grid(row=row, column=1, sticky='w', pady=px(6))
        row += 1
        self.invert_z = tk.BooleanVar(value=stage.get('invert-z', True))
        ttk.Checkbutton(page, text='Reverse the Z axis (after restart)', variable=self.invert_z,
                        bootstyle='round-toggle').grid(row=row, column=1, sticky='w', pady=px(6))
        row += 1
        ttk.Separator(page).grid(row=row, column=0, columnspan=2, sticky='ew', pady=px(14))
        row += 1
        ttk.Label(page, text='Arrow buttons and keys', style='Section.TLabel').grid(row=row, column=0, columnspan=2, sticky='w')
        row += 1
        self.jog_invert = {}
        for axis in 'xyz':
            var = tk.BooleanVar(value=setting_flag(stage, f'jog-invert-{axis}', False))
            self.jog_invert[axis] = var
            ttk.Checkbutton(page, text=f'Reverse the {axis.upper()} direction of the arrows', variable=var,
                            bootstyle='round-toggle', command=self._jog_setting).grid(row=row, column=1, sticky='w', pady=px(4))
            row += 1
        ttk.Label(page, text='Use these if pressing ▲ moves the camera picture the "wrong" way. Applies immediately.',
                  style='Small.TLabel', wraplength=px(560)).grid(row=row, column=1, sticky='w')

    def _files_page(self, page):
        files = self.config.setdefault('files', {})
        row = 0
        ttk.Label(page, text='Snapshots', style='Section.TLabel').grid(row=row, column=0, columnspan=2, sticky='w')
        row += 1
        folder_row = ttk.Frame(page)
        self.folder = tk.StringVar(value=str(self.app.dispatcher.snapshot_directory.resolve()))
        ttk.Entry(folder_row, textvariable=self.folder, width=48, state='readonly').pack(side='left')
        ttk.Button(folder_row, text='Choose…', bootstyle='dark', command=self.choose_folder).pack(side='left', padx=(px(6), 0))
        ttk.Button(folder_row, text='Open', bootstyle='dark', command=self.open_folder).pack(side='left', padx=(px(6), 0))
        form_row(page, row, 'Folder', folder_row)
        row += 1
        self.snapshot_format = Choice(page, [('png', 'PNG · lossless (about 20-40 MB at 20 MP)'),
                                             ('jpg', 'JPEG · smaller (about 3-6 MB)')], files.get('snapshot-format', 'png'),
                                      width=40, command=lambda v: files.__setitem__('snapshot-format', v))
        form_row(page, row, 'Snapshot format', self.snapshot_format.widget)
        row += 1
        self.auto_snapshot = tk.BooleanVar(value=self.app.dispatcher.auto_snapshot_on_uv)
        ttk.Checkbutton(page, text='Save a camera snapshot (JPEG) just before every exposure', variable=self.auto_snapshot,
                        bootstyle='round-toggle', command=self._auto_snapshot).grid(row=row, column=1, sticky='w', pady=px(6))

    def _appearance_page(self, page):
        ui = self.config.setdefault('ui', {})
        row = 0
        ttk.Label(page, text='Appearance (after restart)', style='Section.TLabel').grid(row=row, column=0, columnspan=2, sticky='w')
        row += 1
        self.theme = Choice(page, [('studio-dark', 'Dark'), ('studio', 'Light')], ui.get('theme', 'studio-dark'), width=20)
        form_row(page, row, 'Colours', self.theme.widget)
        row += 1
        try:
            scale = str(float(ui.get('text-scale', 1.0)))
        except (TypeError, ValueError):
            scale = '1.0'
        self.text_scale = Choice(page, [('0.9', 'Compact · 90%'), ('1.0', 'Normal · 100%'), ('1.15', 'Large · 115%'),
                                        ('1.3', 'Extra large · 130%')], scale, width=24)
        form_row(page, row, 'Text size', self.text_scale.widget,
                 f'The app follows {DISPLAY_SCALING}; this makes text larger or smaller on top of that.')

    # ------------------------------------------------------------------ camera
    def _resolution_options(self):
        camera = self.app.dispatcher.camera
        modes = list(getattr(camera, 'modes', []) or []) or preset_modes()
        return [(self.RESOLUTION_HIGHEST, 'Highest available (recommended)')] + \
               [(m.key(), m.label()) for m in dedupe_resolutions(modes)]

    def _current_resolution(self, cam):
        if str(cam.get('mode', 'max')).lower() in ('manual', 'fixed'):
            try:
                return Mode(int(cam['width']), int(cam['height']), float(cam['fps'])).key()
            except (KeyError, TypeError, ValueError):
                pass
        return self.RESOLUTION_HIGHEST

    def camera_settings(self) -> dict:
        cam = dict(self.config.get('camera', {}))
        cam['type'] = self.camera_type.get()
        cam['device'] = self.device.get() or 'auto'
        cam['reject-green'] = self.reject.get()
        choice = self.resolution.get()
        if choice == self.RESOLUTION_HIGHEST:
            cam['mode'] = 'max'
        else:
            w_h, fps = choice.split('@')
            w, h = w_h.split('x')
            cam.update(mode='manual', width=int(w), height=int(h), fps=float(fps), fourcc='YUY2')
        return cam

    def apply_camera(self):
        d = self.app.dispatcher
        if d.patterning_busy or d.autofocus_busy:
            messagebox.showinfo('Camera busy', 'Wait for the exposure or autofocus to finish first.')
            return
        cam = self.camera_settings()
        self.config['camera'] = cam
        self.app.reconnect_camera(settings=cam)
        self.notice.set('Camera reconnecting with the new settings. Click Save settings to keep them.')

    def show_resolution(self, cam):
        self.resolution.set(self._current_resolution(cam))

    def copy_diagnostics(self):
        camera = self.app.dispatcher.camera
        lines = [f'Camera type: {self.camera_type.get()}', f'Selected device: {self.device.get()}',
                 f'Status: {getattr(camera, "status", "Camera off")}',
                 f'Modes: {", ".join(m.key() for m in getattr(camera, "modes", []) or [])}',
                 f'Controls: {getattr(camera, "controls", {})}', 'Devices found:']
        lines += [d.label() for d in self.devices]
        lines.append('Capture attempts:')
        lines += list(getattr(camera, 'diagnostics', []))
        lines.append(f'LEDs: {self.app.dispatcher.led_status}')
        info = cv2.getBuildInformation()
        video = info[info.find('Video I/O'):].split('\n\n')[0] if 'Video I/O' in info else ''
        lines.append(f'OpenCV {cv2.__version__}: ' + ' | '.join(line.strip() for line in video.splitlines()[1:4]))
        self.clipboard_clear()
        self.clipboard_append('\n'.join(lines))
        self.notice.set('Camera diagnostics copied to the clipboard.')

    def scan(self):
        backend = capture_backend(self.config.get('camera', {}))
        named = backend == cv2.CAP_DSHOW or platform.system() != 'Windows'
        threading.Thread(target=lambda: self.results.put(('devices', discover_devices(named=named, api='dshow'))),
                         daemon=True).start()

    # ------------------------------------------------------------------ projector / stage / files
    def _projector_window_setting(self):
        self.config.setdefault('projector', {})['window'] = 'auto' if self.open_at_start.get() else 'off'

    def _led_setting(self):
        proj = self.config.setdefault('projector', {})
        proj['led-control'] = 'auto' if self.led_control.get() else 'off'
        if self.led_control.get():
            self.app.connect_leds()
        else:
            self.app.disconnect_leds()

    def _jog_setting(self):
        stage = self.config.setdefault('stage', {})
        for axis, var in self.jog_invert.items():
            stage[f'jog-invert-{axis}'] = var.get()
            self.app.stage_panel.invert[axis] = var.get()

    def _auto_snapshot(self):
        self.app.dispatcher.auto_snapshot_on_uv = self.auto_snapshot.get()
        self.config.setdefault('files', {})['auto-snapshot'] = self.auto_snapshot.get()

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder.get(), title='Folder for snapshots')
        if folder:
            try:
                self.app.dispatcher.set_snapshot_directory(Path(folder))
            except OSError as exc:
                messagebox.showerror('Snapshot folder', str(exc))
                return
            self.folder.set(folder)
            self.config.setdefault('files', {})['snapshot-folder'] = folder

    def open_folder(self):
        folder = Path(self.folder.get())
        try:
            folder.mkdir(parents=True, exist_ok=True)
            open_path(folder)
        except Exception as exc:
            messagebox.showerror('Open folder', str(exc))

    # ------------------------------------------------------------------ save
    def collect(self) -> dict:
        config = self.config
        config['camera'] = {**config.get('camera', {}), **self.camera_settings()}
        camera = self.app.dispatcher.camera
        found = (getattr(camera, 'settings', {}) or {}).get('stripe-free-exposure')
        if found is not None:
            config['camera']['stripe-free-exposure'] = int(found)
        stage = config.setdefault('stage', {})
        stage['enabled'] = self.stage_enabled.get()
        stage['port'] = self.stage_port.get().strip() or 'auto'
        stage['homing'] = self.homing.get()
        stage['invert-z'] = self.invert_z.get()
        proj = config.setdefault('projector', {})
        proj['window'] = 'auto' if self.open_at_start.get() else 'off'
        proj['resolution'] = self.proj_resolution.get()
        proj['led-control'] = 'auto' if self.led_control.get() else 'off'
        proj['red-brightness'] = round(self.app.dispatcher.red_brightness * 100)
        ui = config.setdefault('ui', {})
        ui['theme'] = self.theme.get()
        try:
            ui['text-scale'] = float(self.text_scale.get())
        except (TypeError, ValueError):
            ui['text-scale'] = 1.0
        files = config.setdefault('files', {})
        files['snapshot-folder'] = str(self.app.dispatcher.snapshot_directory)
        files['snapshot-format'] = self.snapshot_format.get()
        files['auto-snapshot'] = self.auto_snapshot.get()
        auto = self.app.dispatcher.auto_camera
        config['camera']['auto-light'] = auto.auto_light
        if auto.auto_exposure:
            config['camera']['exposure'] = 'auto'
        elif str(config['camera'].get('exposure', 'auto')).strip().lower() in ('auto', 'app', ''):
            # Auto exposure was switched off: keep the exposure the camera is using now.
            current = (getattr(camera, 'controls', {}) or {}).get('exposure')
            config['camera']['exposure'] = int(round(current)) if current is not None else 'camera'
        return config

    def save(self, message=None):
        try:
            save_config(self.path, deepcopy(self.collect()))
            self.notice.set(message or f'Saved to {self.path}.')
            self.app.notify(message or 'Settings saved.', 'ok')
            return True
        except Exception as exc:
            messagebox.showerror('Settings could not be saved', str(exc))
            return False

    def tick(self):
        try:
            while True:
                kind, value = self.results.get_nowait()
                if kind == 'devices':
                    self.devices = value
                    self.device.set_options([('auto', 'Automatic (prefers the Arducam)')] +
                                            [(d.device, d.label()) for d in value])
        except queue.Empty:
            pass
        device = self.device.get()
        hints = [d.connection_hint() for d in self.devices if device == 'auto' or d.device == device]
        self.connection_hint.set(next((h for h in hints if h), ''))
        camera = self.app.dispatcher.camera
        status = getattr(camera, 'status', 'Camera off' if camera is None else 'Vendor camera')
        if isinstance(camera, Webcam) and camera.measured_fps:
            status += f'\nMeasured: {camera.measured_fps:.1f} fps'
        self.camera_status.set(status)
        self.led_status.set(self.app.dispatcher.led_status)
        self.after(700, self.tick)
