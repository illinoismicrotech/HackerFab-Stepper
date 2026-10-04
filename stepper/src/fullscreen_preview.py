"""Full-size camera / projector view in its own borderless fullscreen window.

A separate top-level window always covers the whole screen, on every OS. (An overlay
inside the main window could leave parts of the GUI showing through on Windows.)
"""
import tkinter as tk

from PIL import Image, ImageTk


class FullscreenPreview:
    def __init__(self, root, on_close=None):
        self.root = root
        self.on_close = on_close
        self.kind = None
        self.provider = None
        self.timer = None
        self.photo = None
        self.previous_focus = None
        self.window = tk.Toplevel(root, background='black')
        self.window.title('HackerFab Stepper – Full size view')
        self.window.withdraw()
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.frame = tk.Frame(self.window, background='black')
        self.frame.pack(fill='both', expand=True)
        self.label = tk.Label(self.frame, background='black', foreground='white', font='TkDefaultFont')
        self.label.pack(fill='both', expand=True)
        self.title = tk.Label(self.frame, background='#252525', foreground='white', font=('sans', 14), padx=14, pady=8)
        self.title.place(x=16, y=16, anchor='nw')
        self.close_button = tk.Button(self.frame, text='×  Close (Esc)', font=('sans', 16), command=self.close,
                                      background='#252525', foreground='white', borderwidth=0,
                                      activebackground='#444444', activeforeground='white', cursor='hand2')
        self.close_button.place(relx=1, x=-16, y=16, anchor='ne', width=190, height=52)
        self.window.bind('<Escape>', self._escape)

    def open(self, kind, provider):
        if self.kind is None:
            try:
                self.previous_focus = self.root.focus_get()
            except KeyError:
                # Tkinter bug: focus inside a combobox dropdown ('popdown') has no Python widget.
                self.previous_focus = None
        self.kind, self.provider = kind, provider
        # The theme repaints plain Tk widgets in its own colours. Anything that is not pure
        # black here would shine onto the resist when this view is the projector output.
        for widget in (self.window, self.frame, self.label):
            widget.configure(background='black')
        self.title.configure(text='Camera (live)' if kind == 'camera' else 'What the projector shows')
        # Cover the monitor the main window is on. The explicit size also works where the
        # fullscreen request is ignored (e.g. no window manager).
        x, y, width, height = self._monitor_of_main_window()
        self.window.geometry(f'{width}x{height}+{x}+{y}')
        self.window.deiconify()
        self.window.attributes('-fullscreen', True)
        self.window.lift()
        try:
            self.window.attributes('-topmost', True)
        except tk.TclError:
            pass
        self.window.focus_force()
        self.window.update_idletasks()
        self.refresh()
        if self.timer is None:
            self.timer = self.root.after(66, self._tick)

    def _monitor_of_main_window(self):
        from projector_window import list_displays
        cx = self.root.winfo_rootx() + self.root.winfo_width() // 2
        cy = self.root.winfo_rooty() + self.root.winfo_height() // 2
        displays = list_displays(self.root)
        display = next((d for d in displays if d.x <= cx < d.x + d.width and d.y <= cy < d.y + d.height), displays[0])
        return display.x, display.y, display.width, display.height

    def refresh(self):
        if self.kind is None:
            return
        picture = self.provider()
        if picture is None:
            self.photo = None
            self.label.configure(image='', text='No live camera image' if self.kind == 'camera' else 'No projector output')
            return
        image = picture.copy()
        width, height = self.window.winfo_width(), self.window.winfo_height()
        if width <= 1 or height <= 1:
            width, height = self.window.winfo_screenwidth(), self.window.winfo_screenheight()
        # Fit the whole image; never crop the pattern or change its aspect ratio.
        ratio = min(width / image.width, height / image.height)
        size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        image = image.resize(size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image, master=self.root)
        self.label.configure(image=self.photo, text='')

    def _tick(self):
        self.timer = None
        if self.kind is not None:
            self.refresh()
            self.timer = self.root.after(66, self._tick)

    def _escape(self, _event=None):
        if self.kind is not None:
            self.close()
            return 'break'

    def close(self):
        kind = self.kind
        if kind is None:
            return
        self.kind = None
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.window.attributes('-fullscreen', False)
        self.window.withdraw()
        self.label.configure(image='', text='')
        self.photo = None
        # Give the keyboard back to the main window, so Esc there still stops an exposure.
        if self.previous_focus is not None and self.previous_focus.winfo_exists():
            self.previous_focus.focus_force()
        else:
            self.root.focus_force()
        if self.on_close:
            self.on_close(kind)

    def cleanup(self):
        self.close()
        self.window.destroy()
