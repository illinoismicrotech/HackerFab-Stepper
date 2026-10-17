"""Help page: answers inside the app, without leaving it.

* "Right now" checks the live state of the stepper (camera, stage, projector, LEDs, pattern,
  chip record) and says what to do about anything that is not ready, with a button that
  goes straight there.
* A search box filters every topic below as you type ("stripes", "won't move", "LED" ...).
* "How do I ..." and "Fix a problem" are short, numbered answers written for the operating
  system the app is running on, each with a button to the place in the app it talks about.
* Shortcuts, the status lights and the words used in the app are explained at the bottom.

The illustrated HOW-TO-GUIDE.html is one click away for the long version.
"""
from __future__ import annotations

import platform
import sys
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import ttkbootstrap as ttk

from dispatcher import ShownImage
from platform_text import EXTEND_DISPLAYS, FILE_MANAGER, OS_NAME, open_path
from stage_control.grbl_stage import GrblStage
from ui_kit import Card, add_tooltip
from ui_theme import palette, px

LOG_NAME = "HackerfabStepper.log"

if sys.platform == "win32":
    CAMERA_PRIVACY = ("Windows Settings → Privacy & security → Camera: turn on “Let desktop apps access your "
                      "camera”.")
    SERIAL_PORTS = ("Open Device Manager → Ports (COM & LPT). A warning icon means a driver is missing "
                    "(boards with a CH340 chip often need one).")
    FIRST_START = ("“Windows protected your PC”: click More info → Run anyway (the app is not code-signed). If the "
                   "app vanishes, your antivirus quarantined it: restore it and add an exception for its folder.")
    USB_LEDS = ("Windows normally installs the projector board as a WinUSB device by itself. If the app says there "
                "is no usable driver: run Zadig (zadig.akeo.ie) → Options → List All Devices → pick the Texas "
                "Instruments / DLPC entry (Interface 0, not the camera or keyboard) → driver WinUSB → Install "
                "Driver. Then click Reconnect LEDs.")
    PORT_EXAMPLE = "COM5"
elif sys.platform == "darwin":
    CAMERA_PRIVACY = ("System Settings → Privacy & Security → Camera: allow HackerFab Stepper (or the Terminal "
                      "you started it from). Then quit and restart the app.")
    SERIAL_PORTS = ("macOS lists controllers as /dev/cu.usbmodem… or /dev/cu.usbserial…. Boards with a CH340 chip "
                    "may need the WCH driver; after installing it, allow it under System Settings → Privacy & "
                    "Security.")
    FIRST_START = ("“HackerFab Stepper can’t be opened because it is from an unidentified developer”: right-click "
                   "(or Control-click) the app → Open → Open. You only need to do this once.")
    USB_LEDS = ("macOS needs no driver for the projector board. If LED control still fails, close TI’s DLP EVM "
                "GUI and any other program using the board, replug its USB cable and click Reconnect LEDs.")
    PORT_EXAMPLE = "/dev/cu.usbmodem1101"
else:
    CAMERA_PRIVACY = ("Your user must be allowed to use cameras: run  groups  in a terminal and check it lists "
                      "“video”. If not: sudo usermod -aG video $USER, then log out and back in.")
    SERIAL_PORTS = ("Linux lists controllers as /dev/ttyACM… or /dev/ttyUSB…. Your user needs access to them: "
                    "sudo usermod -aG dialout $USER (on Arch: uucp), then log out and back in.")
    FIRST_START = ("If double-clicking does nothing, make the program executable once: chmod +x HackerfabStepper, "
                   "then start it from a terminal with ./HackerfabStepper to see any message.")
    USB_LEDS = ("Allow your user to open the projector board once with a udev rule: create "
                "/etc/udev/rules.d/70-hackerfab-dlp.rules containing\n"
                "SUBSYSTEM==\"usb\", ATTR{idVendor}==\"0451\", MODE=\"0666\"\n"
                "then run  sudo udevadm control --reload  , replug the projector's USB cable and click Reconnect LEDs.")
    PORT_EXAMPLE = "/dev/ttyACM0"


@dataclass
class Topic:
    title: str
    steps: list[str]
    keywords: str = ""
    actions: list[tuple[str, str]] = field(default_factory=list)   # (button text, action name)
    numbered: bool = True


HOW_TO = [
    Topic("Do a complete exposure", [
        "Check the top bar: the Camera, Stage and Projector dots should be green (LEDs too, if the projector's "
        "USB cable is connected).",
        "On the Chip & map page, open or create a chip record, so every exposure is saved to a file.",
        "Operate → 1 Pattern: Choose pattern… and check the preview (bright = light).",
        "Operate → 2 Focus: in red light (safe for resist), move to the spot and focus until the focus bar is "
        "as long as it gets, or press Autofocus.",
        "Operate → 3 Expose: set the time, read the checklist (fix any red ✗), keep everyone from looking into the "
        "projector, and press ▶ Start exposure.",
        "To stop early press Esc or the red STOP. The exposure is recorded either way.",
    ], "first start begin quick start session expose exposure workflow steps",
        [("Go to 1 Pattern", "step:pattern"), ("Go to 2 Focus", "step:focus"), ("Go to 3 Expose", "step:expose")]),
    Topic("Find the chip and focus", [
        "Operate → 2 Focus. Under RED LIGHT SHOWS pick Solid (best for finding the chip).",
        "Move with the arrow pad or the arrow keys. Start with a large X/Y step (500 µm), then go smaller.",
        "Focus with Z + / Z − or Page Up / Page Down until the focus bar is at its longest.",
        "Or press Autofocus (about 30 s). It needs some detail in the picture and a start close to focus; it never "
        "moves more than 150 µm and goes back to where it started if it fails.",
    ], "focus blurry sharp autofocus z red find chip navigate move", [("Go to 2 Focus", "step:focus")]),
    Topic("Get a clean camera picture (no stripes)", [
        "Red light must be on: the projector badge says RED · SAFE.",
        "Keep Auto exposure on in 2 Focus and wait until it says “Auto: settled” (about 10 s the first time, a few "
        "seconds later on, when it only checks the remembered exposure).",
        "Keep Red brightness at 100 % (Auto on): dimmed red is drawn with uneven flashes and brings lines back.",
        "Connect the projector board's USB cable: the app then turns the green and blue LEDs off while you focus, "
        "which removes the teal/pink bands completely.",
        "Still stripy? Camera Adjust… → Find stripe-free exposure, and turn on Stripe filter or Frame averaging.",
    ], "stripes bands lines flicker camera picture image quality exposure white balance",
        [("Go to 2 Focus", "step:focus"), ("Camera adjustments…", "camera_adjust")]),
    Topic("Choose and check a pattern", [
        "Operate → 1 Pattern → Choose pattern… (PNG, JPEG, BMP or TIFF). Bright pixels get light; transparent "
        "pixels count as black.",
        "For pixel-exact results make the pattern exactly the projector's resolution; the details line says if "
        "it is scaled.",
        "Show it in red projects it in safe red light so you can see where it lands on the chip.",
        "Border adds a dark margin; Pure black & white turns grey pixels fully on or off.",
    ], "pattern image mask file png load choose open preview border posterize black white", [("Go to 1 Pattern", "step:pattern")]),
    Topic("Stop everything right now", [
        "Press Esc, or the red ■ STOP at the top right. It works on every page and in fullscreen.",
        "It stops an exposure, autofocus, homing or tiling and turns the projector off (black, LEDs off).",
        "Click Red light · safe on the Projector panel to see the chip again.",
    ], "stop abort cancel emergency halt escape esc off"),
    Topic("Line up a new layer with the one already on the chip", [
        "Operate → 1 Pattern: choose the new layer's pattern, then Show it in red.",
        "Alignment page: shift or rotate the projected image in projector pixels with the NUDGE arrows until it "
        "sits on the existing features. Reset returns to 0.",
        "If the chip has alignment marks: Alignment → Auto-align stage moves the stage so the marks land on their "
        "calibrated positions. Click it again to refine.",
        "Turn on “Show detected markers” to see what the model finds (green boxes on the camera picture).",
    ], "align alignment layer overlay registration marker marks auto-align rotate shift nudge", [("Open Alignment", "page:alignment")]),
    Topic("Move the stage to exact coordinates", [
        "Click Go to… under the position readout and type X, Y and Z in µm (or pick a saved position).",
        "Moves of several axes over long distances ask for confirmation first. Keep hands clear.",
        "On the Chip & map page, double-click a past exposure to return to it.",
    ], "coordinates position go to move absolute preset saved origin home", [("Go to Operate", "page:operate")]),
    Topic("Save a camera photo, and find saved files", [
        "Click Snapshot under the camera picture. The photo is full resolution; the status line shows where it went.",
        "A photo is also saved automatically just before every exposure (Settings → Files to change this).",
        f"Settings → Files → Open shows the folder in {FILE_MANAGER}.",
    ], "snapshot photo picture save capture image file folder where", [("Open snapshot folder", "open_snapshots")]),
    Topic("Keep a record of what was exposed where", [
        "Chip & map → Chip record → New (or Open). With a file chosen, every exposure is saved immediately.",
        "Each exposure lists its time, position, duration and pattern. Double-click one to move the stage back there.",
        "Finish Layer starts the next layer; the previous layer is shown alongside for reference.",
    ], "chip record layer history log json map exposures", [("Open Chip & map", "page:chip")]),
    Topic("Expose a pattern larger than one field (tiling)", [
        "Choose the big pattern in 1 Pattern.",
        "Chip & map → Tiling → 1 Split the pattern into tiles (projector-sized tiles with shared alignment marks).",
        "Place and focus the first tile in red, then 2 ▶ Begin tiling. The stage moves, focuses, aligns to the "
        "previous tile's marks and exposes, tile by tile.",
        "STOP or Esc ends the run after the current step. The status line reports each tile.",
    ], "tiling tiles large big stitch segment split mosaic", [("Open Chip & map", "page:chip")]),
    Topic("Use the projector as its own screen", [
        f"The projector must be a separate display, not a mirror of this screen: {EXTEND_DISPLAYS}.",
        "The output window then opens on the projector by itself; the Projector dot turns green.",
        "To change it, pick the display under OUTPUT on the Projector panel and click Open.",
    ], "projector display screen hdmi extend mirror duplicate output second monitor window"),
    Topic("Switch the projector LEDs over USB", [
        "Connect the projector board's USB port to this computer (HDMI alone carries no commands).",
        "Close TI's DLP EVM GUI: only one program can use the board at a time.",
        USB_LEDS,
        "Check it once: Settings → Projector → Test LEDs. The projector should go dark during step 2.",
    ], "led leds usb dlp ti projector board driver zadig winusb udev blue red", [("Open Settings", "page:settings")]),
    Topic("Change a setting, and where settings are kept", [
        "Settings page; most changes apply at once, a few say “after restart”. Save settings writes them to "
        "config.toml (the app also saves when you close it).",
        "default.toml lists every setting with an explanation. A typo never stops the app: it tells you at start "
        "which values it could not use.",
        "Open settings folder (below) shows config.toml, the log and default.toml.",
    ], "settings config toml preferences save options folder", [("Open Settings", "page:settings"),
                                                               ("Open settings folder", "open_home")]),
    Topic("Update the app", [
        "Settings → Updates → Check for updates. If there is a newer version, Install and restart downloads it, "
        "checks it and restarts the app with it. Settings, logs and snapshots stay.",
        "The app also checks once a day when it starts and says so in the status line.",
        "It waits until no exposure, autofocus or homing is running.",
        "Run from source (run.sh / run.bat), it runs git pull instead; start the app again with the launcher.",
    ], "update upgrade new version release download install latest", [("Open Settings", "page:settings")]),
]

TROUBLE = [
    Topic("The camera picture is black", [
        "Check the projector shows red light (badge RED · SAFE) and that it reaches the chip.",
        "Camera Adjust… → Reset. Then pick a lower resolution under the picture to see if it comes back.",
        "The app writes a line “Camera picture is black” to the log with the camera's settings: include it when "
        "you ask for help.",
    ], "black dark nothing camera picture image empty", [("Camera adjustments…", "camera_adjust"), ("Open log", "open_log")],
        numbered=False),
    Topic("“NO CAMERA”, or the camera stays CONNECTING", [
        "Close every other program that might use the camera (camera apps, video calls, OBS, the Arducam viewer).",
        "Unplug the camera, wait five seconds, plug it into a USB 3 port directly (no hub).",
        "Click Reconnect under the picture.",
        CAMERA_PRIVACY,
        "Still failing: Copy problem report (above) and send it.",
    ], "no camera connecting not found missing webcam arducam privacy permission", [("Reconnect camera", "camera_reconnect")]),
    Topic("The laptop's own camera is shown instead of the microscope camera", [
        "Settings → Camera: Refresh, choose the Arducam by name, Apply and reconnect camera, then Save settings.",
    ], "wrong camera laptop webcam built-in selection device", [("Open Settings", "page:settings")], numbered=False),
    Topic("The picture is solid red, pink or white", [
        "Turn Auto on next to Red brightness (2 Focus), or lower the slider.",
        "Camera Adjust… → Reset returns the camera to its defaults.",
    ], "overexposed white pink red bright washed out saturated", [("Go to 2 Focus", "step:focus")], numbered=False),
    Topic("Only 1280 × 720 is available, or green blocks / flicker", [
        "The camera is on a USB 2 link or losing data. Use a USB 3 port (blue, or marked SS), the original cable "
        "and no hub; try flipping the USB-C plug at the camera.",
    ], "1280 720 usb2 resolution low slow green blocks flicker corrupted", numbered=False),
    Topic("The projector mirrors this screen, or shows nothing", [
        f"Mirroring: {EXTEND_DISPLAYS}, then pick the projector under OUTPUT and click Open.",
        "Nothing shown: a red Projector dot means no output window (click Open on the Projector panel); badge OFF "
        "means black is projected (click Red light · safe).",
        "Wrong size or place: Settings → Projector → Image resolution “same as the projector display”, Border 0, "
        "and Alignment → Reset.",
    ], "projector mirror duplicate nothing black blank wrong display size place"),
    Topic("“Exposure not started”", [
        "The message says why: no pattern, no exposure time, the stage did not stop moving within 20 s, the stage "
        "is disconnected, or the blue LED could not be switched on over USB.",
        "Fix that one thing and press Start again. Nothing was exposed.",
    ], "exposure not started refused cannot start expose button", numbered=False),
    Topic("“Stage did not move”", [
        "ALARM: the controller locked itself (limit switch, or power-up with homing on). Turn homing on and restart, "
        "or unlock it with $X from a G-code terminal.",
        "Outside the range: the target is past the stage's travel (or the x/y/z-limits in the settings file).",
        "Disconnected: the USB link dropped. Check the cable and power, then click Reconnect under the stage "
        "controls. If it happens while the motors run, use a shorter cable, another port or a powered hub.",
    ], "stage move moving not alarm limit disconnected reconnect usb grbl error", [("Reconnect stage", "stage_reconnect")],
        numbered=False),
    Topic("One axis does not move, the others do", [
        "Every command sent to the controller is in the log, e.g. “GRBL > G0 Y0.0100  ok”.",
        "If that axis's commands are there with ok, the problem is the controller's settings or wiring: check its "
        "steps per mm ($100–$102), driver and cable. Try a large step (100 µm) to see it clearly.",
    ], "axis x y z one not moving steps wiring driver", [("Open log", "open_log")], numbered=False),
    Topic("No stage controller in the start window", [
        "Check the controller has power and its USB cable carries data (not charge-only), then click ↺ to rescan.",
        SERIAL_PORTS,
        f"If you know the port, type it in Settings → Stage (for example {PORT_EXAMPLE}).",
        "“Stage: simulation” although you picked a port: another program has it open (Arduino IDE serial monitor, a "
        "G-code sender). Close it and restart.",
    ], "port serial com tty no ports controller missing simulation start window", numbered=False),
    Topic("Arrow keys do nothing", [
        "A text field has the keyboard: click the camera picture and try again.",
        "Keys are also ignored while an exposure, autofocus, homing or tiling runs.",
    ], "keys keyboard arrows page up down not working", numbered=False),
    Topic("Autofocus stops with a message", [
        "It needs a live picture with detail and a start reasonably close to focus. Focus roughly by hand, show "
        "solid red or a pattern with sharp edges, and try again.",
        "“No sharpest point within ±150 µm”: focus is further away than that; get close by hand first. It always "
        "returns to the Z it started from.",
    ], "autofocus focus failed stops sharpest", numbered=False),
    Topic("The app does not start, or closes straight away", [
        FIRST_START,
        "Read the last lines of HackerfabStepper.log in the settings folder.",
        "If it mentions the settings file, rename config.toml to config.old.toml and start again with the defaults.",
    ], "start launch crash close open error windows protected unidentified developer gatekeeper smartscreen",
        [("Open log", "open_log")], numbered=False),
]

SHORTCUTS = [
    ("Arrow keys", "Move X/Y by the step (hold to repeat)"),
    ("Page Up / Page Down", "Focus: Z + / Z −"),
    ("Shift + those keys", "10× the step"),
    ("Esc", "STOP: ends an exposure, autofocus, homing or tiling; leaves fullscreen"),
    ("F1", "This Help page"),
    ("Double-click the camera picture", "Camera fullscreen (double-click again to return)"),
    ("Click the projector thumbnail", "Larger preview of what the projector shows"),
    ("Double-click an exposure (Chip & map)", "Move the stage back to it (asks first)"),
    ("Hover over any control", "A short explanation"),
]

LIGHTS = [
    ("ok", "Green", "Working."),
    ("warn", "Amber", "Needs attention (connecting, movable window, homing)."),
    ("bad", "Red", "Not working: read the Right now list above for the fix."),
    ("off", "Grey", "Turned off or not used (e.g. simulation, LED control off)."),
    ("uv", "Purple", "BLUE ON: exposing light is on. Do not look into the projector."),
    ("red", "RED · SAFE", "Red light on the chip. Safe for resist: focus for as long as you like."),
]

GLOSSARY = [
    ("Stage", "The motorized platform under the optics. X/Y move it sideways, Z focuses. Distances are in µm "
              "(1 µm = 0.001 mm)."),
    ("Red light", "Patterns shown in red only. Photoresist does not react to red: use it to find, align and focus."),
    ("Blue light / UV", "The exposing light. The pattern and the blue focus mark are shown in it."),
    ("Exposure", "Showing the pattern in blue light for exactly the exposure time."),
    ("Output window", "The borderless window on the projector's display. Whatever it shows lands on the chip."),
    ("Stripes", "Coloured bands in the camera picture from the projector flashing its colours; the app removes them "
                "with a stripe-free camera exposure."),
    ("Focus bar", "The meter at the top right of the camera picture. Longer means sharper."),
    ("Chip record", "A file listing every exposure on a chip: when, where, how long and which pattern."),
    ("Tiling", "Exposing a pattern larger than one field as several tiles, moving the stage between them."),
    ("Homing", "Driving every axis to its limit switch so positions are referenced to the machine (needs switches)."),
]


class HelpPage(ttk.Frame):
    """Searchable, task-based help with a live readiness check."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.columnconfigure(0, weight=1)
        self.entries: list[tuple[str, tk.Widget]] = []        # (search text, widget) for everything searchable
        self.sections: list[tuple[tk.Widget, list["Collapsible"]]] = []
        self.titles: dict[tk.Widget, str] = {}                # topic -> its title and keywords
        self._job = None

        ttk.Label(self, text="Help", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="Type what you need, or check “Right now” to see what is not ready yet.",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(px(2), px(10)))

        # ---- search and quick actions
        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="SEARCH", style="Caption.TLabel").grid(row=0, column=0, sticky="w", padx=(0, px(8)))
        self.query = tk.StringVar()
        self.search = ttk.Entry(bar, textvariable=self.query)
        self.search.grid(row=0, column=1, sticky="ew")
        self.search.bind("<Escape>", lambda _e: (self.query.set(""), "break")[1])
        add_tooltip(self.search, "For example: stripes, focus, won't move, LED, black picture, tiling.")
        ttk.Button(bar, text="Clear", bootstyle="dark", command=lambda: self.query.set("")).grid(
            row=0, column=2, padx=(px(6), 0))
        self.match_note = ttk.Label(bar, text="", style="Small.TLabel")
        self.match_note.grid(row=1, column=1, sticky="w", pady=(px(4), 0))
        self.query.trace_add("write", lambda *_: self.apply_filter())

        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="w", pady=(px(10), px(14)))
        for column, (text, name, tip) in enumerate((
                ("Illustrated guide", "guide", "The full how-to guide with screenshots, in your web browser."),
                ("Open log", "open_log", "HackerfabStepper.log: status messages and errors, newest at the bottom."),
                ("Open settings folder", "open_home", "Where config.toml, default.toml and the log are kept."),
                ("Copy problem report", "copy_report", "Copies your system, the equipment status and the last "
                                                       "lines of the log, ready to paste into a message."))):
            button = ttk.Button(actions, text=text, bootstyle="primary" if column == 0 else "dark",
                                command=lambda n=name: self.run(n))
            button.grid(row=0, column=column, padx=(0, px(6)))
            add_tooltip(button, tip)

        # ---- right now
        self.now_card = now = Card(self, "Right now")
        now.grid(row=4, column=0, sticky="ew")
        self.now_body = now.body
        self.now_body.columnconfigure(0, weight=0)
        self.now_body.columnconfigure(1, weight=1)
        self.now_rows = []

        row = 5
        row = self._section(row, "How do I …", HOW_TO)
        row = self._section(row, f"Fix a problem ({OS_NAME})", TROUBLE)

        # ---- reference
        ref = Card(self, "Keyboard, mouse and lights")
        ref.grid(row=row, column=0, sticky="ew", pady=(px(14), 0))
        self._reference(ref.body)
        self.entries.append(("keyboard mouse shortcut keys esc f1 arrows lights dots status colours colors badge "
                             + " ".join(f"{k} {w}" for k, w in SHORTCUTS).lower(), ref))
        row += 1
        words = Card(self, "Words used in this app")
        words.grid(row=row, column=0, sticky="ew", pady=(px(14), 0))
        words.body.columnconfigure(0, weight=0)
        words.body.columnconfigure(1, weight=1)
        for i, (term, meaning) in enumerate(GLOSSARY):
            ttk.Label(words.body, text=term, style="Section.TLabel").grid(row=i, column=0, sticky="nw",
                                                                         padx=(0, px(14)), pady=px(3))
            ttk.Label(words.body, text=meaning, wraplength=px(640), justify="left").grid(row=i, column=1, sticky="w",
                                                                                        pady=px(3))
        self.entries.append(("words glossary meaning terms " + " ".join(f"{t} {m}" for t, m in GLOSSARY).lower(),
                             words))
        row += 1
        self.empty = ttk.Label(self, text="", style="Warn.TLabel", wraplength=px(700), justify="left")
        self.empty.grid(row=row, column=0, sticky="w", pady=(px(14), 0))
        self.empty.grid_remove()
        self.refresh()

    # ------------------------------------------------------------------ building
    def _section(self, row, title, topics):
        heading = ttk.Label(self, text=title.upper(), style="Caption.TLabel")
        heading.grid(row=row, column=0, sticky="w", pady=(px(18), px(6)))
        row += 1
        items = []
        for topic in topics:
            item = Collapsible(self, topic, self.run)
            item.grid(row=row, column=0, sticky="ew", pady=(0, px(6)))
            items.append(item)
            self.titles[item] = f"{topic.title} {topic.keywords}".lower()
            self.entries.append((" ".join([topic.title, topic.keywords] + topic.steps).lower(), item))
            row += 1
        self.sections.append((heading, items))
        return row

    def _reference(self, body):
        c = palette()
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        for i, (keys, what) in enumerate(SHORTCUTS):
            ttk.Label(body, text=keys, font="StepperBold").grid(row=i, column=0, sticky="w", padx=(0, px(16)), pady=px(2))
            ttk.Label(body, text=what).grid(row=i, column=1, sticky="w", pady=px(2))
        start = len(SHORTCUTS)
        ttk.Separator(body).grid(row=start, column=0, columnspan=2, sticky="ew", pady=px(10))
        for i, (kind, name, meaning) in enumerate(LIGHTS, start=start + 1):
            swatch = tk.Frame(body, background=c["bg"])
            swatch.grid(row=i, column=0, sticky="w", pady=px(2))
            dot = tk.Canvas(swatch, width=px(12), height=px(12), highlightthickness=0, background=c["bg"])
            dot.create_oval(1, 1, px(12) - 1, px(12) - 1, fill=c.get(kind, c["off"]), outline="")
            dot.pack(side="left", padx=(0, px(6)))
            tk.Label(swatch, text=name, background=c["bg"], foreground=c["text"], font="StepperBold").pack(side="left")
            ttk.Label(body, text=meaning, wraplength=px(620), justify="left").grid(row=i, column=1, sticky="w", pady=px(2))

    # ------------------------------------------------------------------ search
    def apply_filter(self):
        words = [w for w in self.query.get().lower().replace("’", "'").split() if w]
        matches = 0
        # Topics whose title or keywords match come first: when there are any, topics that only
        # mention the words somewhere in their steps are left out.
        strong = {w for w in self.titles if words and all(word in self.titles[w] for word in words)}
        for text, widget in self.entries:
            hit = all(w in text for w in words)
            if strong and isinstance(widget, Collapsible):
                hit = widget in strong
            if hit:
                widget.grid()
                if isinstance(widget, Collapsible):
                    matches += 1
                    widget.set_open(bool(words))   # open what matched; a cleared search folds everything
            else:
                widget.grid_remove()
        for heading, items in self.sections:
            if not words or any(item.winfo_manager() for item in items):
                heading.grid()
            else:
                heading.grid_remove()
        if words:
            self.now_card.grid_remove()   # results right under the search box
        else:
            self.now_card.grid()
        if not words:
            self.match_note.configure(text="")
            self.empty.grid_remove()
        elif matches:
            self.match_note.configure(text=f"{matches} matching topic{'s' if matches != 1 else ''}, opened below.")
            self.empty.grid_remove()
        else:
            self.match_note.configure(text="Nothing matched.")
            self.empty.configure(text="No topic matches that. Try a shorter word (e.g. “stage”, “camera”, “LED”), "
                                      "or open the Illustrated guide.")
            self.empty.grid()
        page = getattr(self.app, "pages", {}).get("help")
        if page is not None and hasattr(page, "canvas"):
            page.canvas.yview_moveto(0)

    # ------------------------------------------------------------------ right now
    def checks(self):
        """(kind, title, detail, action) rows describing the stepper's state right now."""
        app, d = self.app, self.app.dispatcher
        rows = []
        camera = d.camera
        if camera is None:
            rows.append(("off", "Camera is off", "Choose a camera in Settings → Camera.", ("Open Settings", "page:settings")))
        elif d.camera_image is not None:
            rows.append(("ok", "Camera is live", getattr(camera, "status", "") or "Pictures are arriving.", None))
        elif getattr(camera, "state", "") == "error":
            rows.append(("bad", "No camera picture", (getattr(camera, "status", "") or "The camera stopped.") +
                         " Close other camera programs, then Reconnect.", ("Reconnect camera", "camera_reconnect")))
        else:
            rows.append(("warn", "Camera is connecting", "This can take 10–20 seconds at the highest resolution.", None))

        stage = d.hardware.stage
        if not getattr(d, "stage_connected", True):
            rows.append(("bad", "Stage is disconnected", "Check its USB cable and power, then Reconnect.",
                         ("Reconnect stage", "stage_reconnect")))
        elif not isinstance(stage, GrblStage):
            rows.append(("off", "Stage: simulation", "Nothing moves. Pick the controller in the start window next time "
                         "(restart the app).", None))
        elif stage.state_name().startswith("Alarm"):
            rows.append(("bad", "Stage is in ALARM", "It refuses to move: turn homing on and restart, or unlock it "
                         "with $X.", ("Open Settings", "page:settings")))
        else:
            rows.append(("ok", "Stage is connected", f"State: {stage.state_name()}.", None))

        window = app.projector_window
        if d.in_uv():
            rows.append(("uv", "Blue light is ON", "Exposing light: do not look into the projector.", None))
        if window.is_open and window.display is not None and not window.display.primary:
            rows.append(("ok", "Projector output is on the projector", window.display.label, None))
        elif window.is_open:
            rows.append(("warn", "Projector output is not on its own display",
                         f"Drag it onto the projector, or {EXTEND_DISPLAYS} and pick the projector under OUTPUT.", None))
        elif len(app.displays) < 2:
            rows.append(("bad", "Only one display found", "The projector is mirroring this screen (or is not "
                         f"connected). To fix it, {EXTEND_DISPLAYS}.", None))
        else:
            rows.append(("warn", "Projector output window is closed", "Click Open on the Projector panel (it also "
                         "opens by itself when you start an exposure).", ("Go to Operate", "page:operate")))

        kind, text = d.led_summary()
        if d.leds is None:
            rows.append(("off", "Projector LEDs: not controlled", d.led_status, ("How to connect", "search:LED")))
        else:
            rows.append((kind, "Projector LEDs", text, None))

        if d.pattern_image_path:
            rows.append(("ok", "Pattern chosen", Path(d.pattern_image_path).name, None))
        else:
            rows.append(("warn", "No pattern chosen yet", "Operate → 1 Pattern → Choose pattern…",
                         ("Go to 1 Pattern", "step:pattern")))

        chip = getattr(app, "chip_frame", None)
        if chip is not None and not chip.path.get():
            rows.append(("warn", "Exposures are not being saved to a file",
                         "Chip & map → Chip record → New or Open, to keep a record.", ("Open Chip & map", "page:chip")))
        return rows

    def refresh(self):
        self._job = None
        try:
            rows = self.checks()
        except Exception as exc:   # help must never break the window
            rows = [("warn", "Status unavailable", str(exc), None)]
        c = palette()
        while len(self.now_rows) < len(rows):
            i = len(self.now_rows)
            dot = tk.Canvas(self.now_body, width=px(12), height=px(12), highlightthickness=0, background=c["bg"])
            oval = dot.create_oval(1, 1, px(12) - 1, px(12) - 1, outline="")
            title = ttk.Label(self.now_body, font="StepperBold")
            detail = ttk.Label(self.now_body, style="Small.TLabel", wraplength=px(520), justify="left")
            button = ttk.Button(self.now_body, bootstyle="dark")
            dot.grid(row=i * 2, column=0, sticky="w", padx=(0, px(8)), pady=(px(4), 0))
            title.grid(row=i * 2, column=1, sticky="w", pady=(px(4), 0))
            detail.grid(row=i * 2 + 1, column=1, sticky="w")
            button.grid(row=i * 2, column=2, rowspan=2, sticky="e", padx=(px(8), 0))
            self.now_rows.append((dot, oval, title, detail, button))
        for i, widgets in enumerate(self.now_rows):
            dot, oval, title, detail, button = widgets
            if i >= len(rows):
                for w in (dot, title, detail, button):
                    w.grid_remove()
                continue
            kind, head, text, action = rows[i]
            for w in (dot, title, detail):
                w.grid()
            dot.itemconfigure(oval, fill=c.get(kind, c["off"]))
            title.configure(text=head)
            detail.configure(text=text)
            if action:
                button.configure(text=action[0], command=lambda n=action[1]: self.run(n))
                button.grid()
            else:
                button.grid_remove()
        if self.winfo_exists():
            self._job = self.after(1000, self.refresh)

    # ------------------------------------------------------------------ actions
    def run(self, name: str):
        app = self.app
        try:
            if name == "guide":
                from gui import open_guide
                open_guide()
            elif name.startswith("page:"):
                app.show_page(name.split(":", 1)[1])
            elif name.startswith("step:"):
                app.show_page("operate")
                app.workflow.show_step(name.split(":", 1)[1])
            elif name.startswith("search:"):
                self.query.set(name.split(":", 1)[1])
            elif name == "camera_adjust":
                app.show_page("operate")
                app.open_camera_adjustments()
            elif name == "camera_reconnect":
                app.reconnect_camera()
            elif name == "stage_reconnect":
                app.dispatcher.reconnect_stage()
            elif name == "open_snapshots":
                folder = Path(app.dispatcher.snapshot_directory)
                folder.mkdir(parents=True, exist_ok=True)
                open_path(folder)
            elif name == "open_home":
                open_path(Path.cwd())
            elif name == "open_log":
                log = Path.cwd() / LOG_NAME
                if log.exists():
                    open_path(log)
                else:
                    app.notify("No log file: when started from a terminal, messages appear in the terminal.", "warn")
            elif name == "copy_report":
                self.clipboard_clear()
                self.clipboard_append(self.problem_report())
                app.notify("Problem report copied. Paste it into your message, with what you did and what happened.",
                           "ok")
        except Exception as exc:
            app.notify(f"That did not work: {exc}", "bad")

    def problem_report(self) -> str:
        d = self.app.dispatcher
        lines = ["HackerFab Stepper problem report",
                 f"System: {platform.platform()} · Python {platform.python_version()} · "
                 f"{'standalone app' if getattr(sys, 'frozen', False) else 'from source'}",
                 f"Folder: {Path.cwd()}", ""]
        for kind, head, text, _action in self.checks():
            lines.append(f"[{kind}] {head}: {text}")
        camera = d.camera
        if camera is not None:
            lines.append(f"Camera status: {getattr(camera, 'status', '')}")
            lines += [f"  {line}" for line in list(getattr(camera, "diagnostics", []))[-8:]]
        lines.append(f"Projected: {d.shown_image.value} · last exposure: {d.last_exposure or 'none'}")
        log = Path.cwd() / LOG_NAME
        if log.exists():
            try:
                tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
                lines += ["", f"Last {len(tail)} lines of {LOG_NAME}:"] + tail
            except OSError:
                pass
        return "\n".join(lines)


class Collapsible(ttk.Frame):
    """A topic: a clickable title (mouse, or Tab + Enter/Space) that opens to short steps and buttons."""

    def __init__(self, parent, topic: Topic, run: Callable[[str], None]):
        super().__init__(parent)
        self.topic = topic
        self.columnconfigure(0, weight=1)
        c = palette()
        self.header = tk.Label(self, text="", anchor="w", font="StepperBold", cursor="hand2", takefocus=1,
                               background=c["surface"], foreground=c["text"], padx=px(12), pady=px(7),
                               highlightthickness=1, highlightbackground=c["border"], highlightcolor=c["accent"])
        self.header.grid(row=0, column=0, sticky="ew")
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            self.header.bind(sequence, lambda _e: self.toggle())
        self.header.bind("<Enter>", lambda _e: self.header.configure(background=c["hover"]))
        self.header.bind("<Leave>", lambda _e: self.header.configure(background=c["surface"]))
        self.body = ttk.Frame(self, padding=(px(16), px(8), px(8), px(4)))
        self.body.columnconfigure(1, weight=1)
        for i, step in enumerate(topic.steps):
            mark = f"{i + 1}." if topic.numbered else "•"
            ttk.Label(self.body, text=mark, font="StepperBold").grid(row=i, column=0, sticky="nw", padx=(0, px(8)),
                                                                    pady=px(2))
            ttk.Label(self.body, text=step, wraplength=px(680), justify="left").grid(row=i, column=1, sticky="w",
                                                                                    pady=px(2))
        if topic.actions:
            buttons = ttk.Frame(self.body)
            buttons.grid(row=len(topic.steps), column=0, columnspan=2, sticky="w", pady=(px(6), px(2)))
            for text, name in topic.actions:
                ttk.Button(buttons, text=text + "  →", bootstyle="primary-outline",
                           command=lambda n=name: run(n)).pack(side="left", padx=(0, px(6)))
        self.is_open = False
        self.set_open(False)

    def set_open(self, is_open: bool):
        self.is_open = is_open
        self.header.configure(text=("▾   " if is_open else "▸   ") + self.topic.title)
        if is_open:
            self.body.grid(row=1, column=0, sticky="ew")
        else:
            self.body.grid_remove()

    def toggle(self):
        self.set_open(not self.is_open)
        return "break"
