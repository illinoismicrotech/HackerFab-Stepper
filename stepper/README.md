# HackerFab Stepper: station setup and development

This folder holds The Hacker Fab's open source stepper software: pattern selection, stage
alignment, timed exposure and a live camera preview. For the hardware and the Hacker Fab in
general, see the [Gitbook](https://hacker-fab.gitbook.io/hacker-fab-space/fab-toolkit/patterning/lithography-stepper-v2-build-work-in-progress)
and the [website](https://hackerfab.ece.cmu.edu/).

![stepper-gui](https://github.com/user-attachments/assets/3687a777-6f2b-4d9b-b7dc-8fc08cd7d4bf) ![stepper-assembly](https://github.com/user-attachments/assets/6211e7e7-3368-4a26-bbe2-425e88622b5c)

Operators: press **F1** in the app, or read the illustrated [how-to guide](docs/HOW-TO-GUIDE.html).
This page is for whoever sets up a station or works on the code.

- [Running the app](#running-the-app)
- [Stage controller (GRBL)](#stage-controller-grbl)
- [Cameras](#cameras)
- [Projector](#projector)
- [Configuration](#configuration)
- [How it behaves](#how-it-behaves)
- [Troubleshooting notes](#troubleshooting-notes)
- [Development](#development)

## Running the app

### Standalone app (no Python)

Install it with the one-line install command, or download it from the repository's **Releases**
page (both in the [top-level README](../README.md#install)). It includes everything,
alignment-marker detection too, and updates itself (see [Updates](#updates)).

| | Windows | macOS | Linux |
|---|---|---|---|
| File | `HackerfabStepper-Windows.exe` | `HackerfabStepper-macOS-<arm64\|x86_64>.zip` | `HackerfabStepper-Linux-x86_64.tar.gz` |
| First start | SmartScreen: **More info → Run anyway** | right-click the app → **Open** | `chmod +x HackerfabStepper` if needed; add your user to `video` and `dialout` (Arch: `uucp`) |
| Settings, log, snapshots | the exe's folder (`%LOCALAPPDATA%\HackerfabStepper` if read-only) | `~/Documents/HackerfabStepper` | the program's folder (`~/.local/share/HackerfabStepper` if read-only) |

`HACKERFAB_HOME=/some/folder` puts settings, log and snapshots somewhere else on any system.

### Updates

`src/updater.py` updates the standalone app from the GitHub releases (standard library only):

1. **Which release.** `packaging/build.py` bundles `build_info.json` with the version, commit and
   channel. Builds of `main` (channel `latest`) move to the **Latest build** pre-release when its
   commit differs; tag builds (channel `stable`) move to the highest newer `v*` tag (pre-releases
   only if the installed version is one, e.g. `v0.x`).
2. **Download and verify.** The asset for this OS and CPU (the same names `build.py` produces;
   Apple-silicon Macs get the arm64 build even when an Intel copy runs under Rosetta) is
   downloaded next to the program and checked against the SHA-256 digest GitHub publishes.
   Without a digest, nothing is installed.
3. **Swap.** The running program is renamed (`HackerfabStepper.old.exe`, `HackerfabStepper.old`,
   `HackerfabStepper.app.old`), the new one moved into place, and the original put back if
   anything fails. Camera worker processes of the still-running app keep using the old copy.
   The app then closes normally (settings saved, LEDs off) and starts the new version with
   `--after-update`, the settings file and the stage port it was using, so the setup window is
   skipped. Until the new version's window is up (a one-file app unpacks itself first, 10-30 s),
   the old process shows a small "Updating…" window; the new version closes it by writing
   `.update-ready` in the home folder. The next start deletes the `.old` copy.

The automatic daily check runs only in the standalone app (never in tests, the smoke test, or a
source checkout) and can be turned off in **Settings → Updates** (`[app] check-for-updates`).
`HackerfabStepper --update [--check]` does the same from a terminal. A source checkout is updated
with `git pull --ff-only` from the same tab.

### From source

Python 3.10–3.13 (3.13 recommended; 3.14 is not supported yet because Pillow has no wheel for
it). The launchers create a versioned `.venv-<python version>` here and install
`requirements.txt` with plain `pip`:

- **Windows:** double-click `run.bat` (it bypasses the PowerShell execution policy for this
  launch only). `.\run.ps1 -SetupOnly` installs without starting the app.
- **Linux / macOS:** `./run.sh` (`chmod +x run.sh` first if needed).

To do it by hand:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python src/gui.py
```

## Stage controller (GRBL)

The app drives the stage through a board running [GRBL](https://github.com/gnea/grbl) or grblHAL
(a BTT SKR Pico, or an Arduino) at 115200 baud, optionally with limit switches for homing.

### Axes

Axes are named from the **point of view of the projector/camera**. If a direction below is
wrong, swap or reverse the stepper connections.

- X and Y pan the camera's view. Their polarity does not matter to the app; arrow directions can
  be reversed in **Settings → Stage**.
- Z focuses. +Z should bring the camera and the chip's surface closer together. The app reverses
  Z by default (`invert-z = true`), in moves and in the position readout alike; change it in
  **Settings → Stage → Reverse Z direction** (after restart). This does not change the
  controller's own settings, so check the direction with a small jog before using saved
  positions.

### Firmware build (limit switches only)

If each axis has a limit switch or proximity sensor, edit GRBL's `config.h` before flashing.
Replace any existing `HOMING_CYCLE_0` / `HOMING_CYCLE_1` defines with:

```c
#define HOMING_CYCLE_0 ((1 << X_AXIS)|(1<<Y_AXIS))
#define HOMING_CYCLE_1 (1 << Z_AXIS)
```

and comment out `VARIABLE_SPINDLE` so the Z limit switch can be used:

```c
//#define VARIABLE_SPINDLE
```

### Controller settings

Set these from a serial terminal (e.g. the Arduino IDE's serial monitor). Details are on
[GRBL's wiki](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Configuration).

| Setting | Value (CMU's stage) | Meaning |
|---|---|---|
| `$22` | `1` | Homing on (only with limit switches) |
| `$23` | `7` | Homing direction invert mask: bit 0/1/2 = X/Y/Z, set when the switch is reached moving negative |
| `$24`, `$25` | `10`, `50` | Homing feed and seek rate |
| `$27` | `0.5` | Homing pull-off in mm (raise it if the sensors have hysteresis) |
| `$100`–`$102` | `3200` | Steps per mm: 8× microstepping × 200 steps/rev ÷ 0.5 mm/rev |
| `$110`–`$112` | `120.000` | Max rate, mm/min (empirical) |
| `$120`–`$122` | `5.000` | Max acceleration, mm/s² (empirical) |
| `$130`–`$132` | `15.000` | Max travel in mm: measure it and back off 0.1 mm |

With switches, `$H` should now home the stage. Then set `homing = true` under `[stage]` in the
app's settings; the app homes at startup and shows a **Home** button.

Every command the app sends is written to `HackerfabStepper.log` (e.g. `GRBL > G0 Y0.0100  ok`),
which makes wiring and steps-per-mm problems easy to spot.

## Cameras

| Camera | `[camera] type` | Notes |
|---|---|---|
| Any USB (UVC) camera, e.g. Arducam B0477 | `"webcam"` | Default. `device = "auto"` prefers the Arducam. Windows reads it through DirectShow (the only method through which the Arducam accepts exposure changes), Linux through V4L2, macOS through AVFoundation. |
| Basler | `"basler"` | Install [pylon](https://www.baslerweb.com/en-us/downloads/software/) (the "Software Suite"; restart when the installer asks, it continues afterwards) and `pip install pypylon`. Runs from source only. |
| FLIR | `"flir"` | Ask in the Hacker Fab Discord for access to `flir-private`, then `git submodule update --init` (or clone it into `src/camera/flir`). Needs Python 3.10 or older. Runs from source only. |
| None | `"none"` | No preview; everything else works. |

By default the camera runs at its highest resolution and lowest frame rate (B0477: 5472 × 3648 at
9 fps; on USB 2 only 1280 × 720 at 10 fps). The preview is reduced in the capture process; focus
is measured on the middle half of each full-resolution frame at six scales (full, 1/2, 1/4, …)
and snapshots are saved at full resolution. Capture runs
in its own process with a watchdog, so a stuck driver cannot freeze the window.

**Stripes.** A DLP projector flashes its light, so a rolling-shutter camera sees evenly spaced
lines unless each exposure spans whole light cycles. While red light is on, the app tries 16, 31,
62.5 and 125 ms, measures the lines (`camera/stripes.py` finds them by their spacing, so the chip
edge and dark margins do not count) and keeps every line-free exposure. The best is remembered as
`stripe-free-exposure` and checked first next time; the full search runs only if it has lines
again. If none is line-free and the projector display runs at 60 Hz (read on Windows, and on Linux
through xrandr), the result suggests 48 Hz or 24 Hz.

**Brightness.** Red light stays at 100 % (dimmed red is drawn with uneven mirror flashes, which
brings the lines back). Too dark: a longer line-free exposure, then gain (up to 4× the minimum).
Too bright: less gain, a shorter line-free exposure, and only then dimmer red. It reacts only
after brightness has left a wide band for a few frames, so it does not hunt while the stage moves.
White balance is fixed at 4500 K so faint green/blue leakage does not become coloured bands.

## Projector

The TI DLPDLCR471TPEVM is used as a second display over HDMI. The desktop must be **extended,
not mirrored** (Windows: Win+P → Extend; macOS: System Settings → Displays, no mirroring; Linux:
your desktop's display settings). The output window opens on it at startup, at its own
resolution, so patterns are projected pixel for pixel.

**LED control over USB (optional, recommended).** With the projector board's USB port connected,
the app switches its LEDs through the DLPC6540 command set: red only while focusing, red + blue
for exposures and the blue focus mark, all off when idle and when the app closes. It refuses to
start an exposure if the blue LED cannot be switched on. Check it once with **Settings →
Projector → Test LEDs**. Close TI's DLP EVM GUI while the app runs.

- **Windows:** the board normally installs as WinUSB by itself; if the app reports no usable
  driver, install WinUSB for its Interface 0 with [Zadig](https://zadig.akeo.ie/).
- **Linux:** allow your user to open it with a udev rule in
  `/etc/udev/rules.d/70-hackerfab-dlp.rules`:
  `SUBSYSTEM=="usb", ATTR{idVendor}=="0451", MODE="0666"`, then
  `sudo udevadm control --reload` and replug the board.
- **macOS:** nothing to install.

`[projector] led-control = "off"` turns it off.

## Configuration

Settings live in `config.toml` (written by the app: **Settings → Save settings**, and on exit;
the previous five versions are kept as `.bak`). `default.toml` holds the factory defaults with a
comment for every key; read it for the full list. A mistyped value never stops the app: at
startup it lists what it could not use.

```toml
[camera]
type = "webcam"            # "basler", "flir", "none"
device = "auto"            # or the camera's number
mode = "max"               # or "manual" with width, height, fps
exposure = "auto"          # stripe-free search; or a number in log2 seconds (-4 = 62.5 ms)
white-balance = 4500

[stage]
enabled = true
port = "auto"              # or "COM5", "/dev/ttyACM0", "/dev/cu.usbmodem1101"
homing = false             # true only with limit switches and GRBL homing set up
invert-z = true
# x-limits = [-15000, 0]   # optional travel limits in µm (also y-, z-)

[projector]
window = "auto"            # open the output window on the projector at startup
resolution = "auto"        # the projector display's own resolution
led-control = "auto"       # "off" leaves the LEDs alone

[exposure]
seconds = 8

[alignment]
enabled = false            # show detected marks on the camera picture
model_path = "ckpts/best.onnx"
reference-width = 1920     # camera pixels the marker positions below were measured at
reference-height = 1080
right_marker_x = 1820.0
left_marker_x = 280.0
top_marker_y = 269.0
bottom_marker_y = 1075.0
x_scale_factor = -1100     # µm of stage movement per frame width
y_scale_factor = 800

[app]
check-for-updates = true   # standalone app: look for a newer version at start, once a day
```

Calibrating the `[alignment]` values is explained in [docs/guide.md](docs/guide.md).

## How it behaves

- **Exposure.** The pattern is drawn first, then the blue LED switches on and the timer starts.
  A separate timer switches the light off on time even if a dialog is open; the chip record shows
  how long the light was really on. From **Start** until the end, the stage, projector output,
  camera resolution, LEDs and a second Start are locked; Esc and **STOP** still work.
- **STOP / Esc** ends any exposure, autofocus, homing or tiling and leaves the projector off.
- **Stage position** is read from the controller when the app connects and after homing. Jogs
  are relative moves (G91); absolute moves (Go to…, saved positions, the chip record) use the
  coordinates the controller reports (G53 machine coordinates on a standard setup). If the USB
  link drops, moves and exposures are refused and a **Reconnect** button appears.
- **Homing** runs without freezing the window; STOP cancels it with a soft reset.
- **Autofocus** judges sharpness at several image scales and uses the one where focus changes the
  picture most compared with frame-to-frame noise. It scans in one direction (stage backlash),
  skips frames exposed while the stage moved, interpolates between 2 µm steps and approaches the
  result from below. It never moves more than ±150 µm from where it started and returns there if
  it fails.

## Troubleshooting notes

The Help page (F1) and the [how-to guide](docs/HOW-TO-GUIDE.html) cover everyday problems. A few
station-level notes:

- **Green or flickering video:** image data is being lost on the USB link or the decoder.
  Close other camera programs; use a USB 3 port, the original cable and no hub. Frames that are
  mostly solid green are skipped (turn that off only if the specimen really is green). See
  [docs/arducam-usb2-diagnostics.md](docs/arducam-usb2-diagnostics.md) for a measured USB 2 case.
- **Camera problems:** **Settings → Camera → Copy camera diagnostics** lists the device, the
  cameras found and every capture attempt.
- **Tiny text on Linux:** some standalone Python builds ship Tk without Xft, so every font
  collapses to one small bitmap font. `./tools/build_tk.sh` builds a project-local Tcl/Tk 9.0.4
  with Xft under `.runtime/` (git-ignored; needs a C compiler, make, curl, pkg-config and the
  X11/Xft/fontconfig headers), which the app then uses automatically. **Settings → Appearance**
  also has text sizes from 90 % to 130 %, and `HACKERFAB_UI_SCALE=1.6` scales everything.

## Development

### Code map

| Path | Contents |
|---|---|
| `src/gui.py` | Main window, start window and `main()` |
| `src/stepper_app.py` | Standalone app entry point: working folder per OS, log, `--smoke-test` |
| `src/dispatcher.py` | State and actions: projection, LEDs, exposures, autofocus, stage, alignment maths |
| `src/camera/` | Capture process (`webcam.py`), automatic exposure, projector-line analysis (`stripes.py`), image processing, camera modes |
| `src/camera_view.py`, `stage_pad.py`, `projector_panel.py`, `steps_panel.py` | The Operate page |
| `src/settings.py`, `alignment_page.py`, `chip_pages.py`, `help_page.py` | The other pages |
| `src/alignment_detector.py` | Alignment-marker model on OpenCV DNN |
| `src/projector_leds.py`, `projector_window.py` | LED control over USB, the output window |
| `src/updater.py`, `update_panel.py` | Over-the-air updates from the GitHub releases, and Settings → Updates |
| `src/platform_text.py` | Wording and helpers that differ between Windows, macOS and Linux |
| `src/ui_theme.py`, `ui_kit.py` | Colours, fonts, DPI awareness, shared widgets |
| `packaging/` | PyInstaller spec, build script (writes `build_info.json`), build requirements |
| `../install.sh`, `../install.ps1` | One-line installers / updaters for macOS + Linux and Windows |
| `docs/guide_template.html`, `docs/guide_images/`, `tools/build_guide.py` | Source of the how-to guide |

### Tests

```bash
./run_tests.sh                                   # Linux: GUI tests run on a virtual screen (Xvfb)
PYTHONPATH=src python -m unittest discover -s tests
```

GUI tests need a display (they are skipped without one) and answer every dialog automatically.

### Building the standalone app

GitHub Actions builds and smoke-tests all platforms on every push (`.github/workflows/build.yml`).
To build locally, on the platform you want the app for:

```bash
python -m venv .venv-build
.venv-build/bin/pip install -r packaging/requirements-build.txt    # Windows: .venv-build\Scripts\pip
.venv-build/bin/python packaging/build.py
```

The result, smoke-tested, is in `dist/release/`. On a Linux machine without a screen, run the
last command under `xvfb-run`. A local build updates like a build of `main`; set
`APP_VERSION=1.2.0` for a release build, or `APP_CHANNEL` to override the channel.

### Editing the how-to guide

Edit `docs/guide_template.html` (screenshots: `docs/guide_images/<name>.png`, referenced as
`IMG:<name>|caption`), then run `python tools/build_guide.py` to rebuild `docs/HOW-TO-GUIDE.html`.
The Help page's answers are plain lists at the top of `src/help_page.py`.

### Reporting bugs

The software is in active development. Open an issue with what you did, what happened, and the
**Help → Copy problem report** output.
