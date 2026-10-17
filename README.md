# HackerFab Stepper

Desktop control software for the HackerFab maskless photolithography stepper: a DLP projector
shows the pattern in red light (safe for resist) to focus and align, then in blue light to expose
it, while a USB microscope camera watches the chip and a GRBL stage moves it.

It runs on **Windows, macOS and Linux**, either as a ready-made download or from source.

## Install

One command downloads the right app for your computer from the releases, checks its SHA-256 and
installs it with a menu entry. Run it again any time to update.

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/illinoismicrotech/HackerFab-Stepper/main/install.ps1 | iex
```

**macOS / Linux** (terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/illinoismicrotech/HackerFab-Stepper/main/install.sh | sh
```

| | Installs to | Start it from |
|---|---|---|
| Windows | `%LOCALAPPDATA%\Programs\HackerfabStepper` | Start menu / desktop shortcut |
| macOS | `/Applications` (or `~/Applications`) | Launchpad / Applications |
| Linux | `~/.local/share/HackerfabStepper` | App menu, or `hackerfab-stepper` |

Add `-Release` (Windows, when run as a saved file) or `sh -s -- --release` (macOS/Linux) to install
the newest numbered release instead of the Latest build of `main`.

### Or download it by hand

No Python, no command line. Open the **[Releases page](../../releases)** and download the file for
your computer from **Latest build** (the newest version of `main`):

| Computer | Download | First start |
|---|---|---|
| Windows 10/11 | `HackerfabStepper-Windows.exe` | Put it in its own folder (e.g. `C:\HackerfabStepper\`) and double-click it. "Windows protected your PC" → **More info → Run anyway**. |
| Mac, Apple silicon (M1–M4) | `HackerfabStepper-macOS-arm64.zip` | Unzip, drag **HackerfabStepper.app** to Applications. First time: right-click it → **Open** → **Open**. |
| Mac, Intel | `HackerfabStepper-macOS-x86_64.zip` | Same as above. |
| Linux (x86-64) | `HackerfabStepper-Linux-x86_64.tar.gz` | `tar xzf HackerfabStepper-Linux-x86_64.tar.gz && ./HackerfabStepper`. Once: `sudo usermod -aG video,dialout $USER` (Arch: `uucp` instead of `dialout`), then log out and in. |

Numbered versions (`v1.0.0`) are stable releases; **Pre-release** versions (`v0.x`, or names with
a dash such as `v1.1.0-beta`) are test builds.

When it starts, pick the stage controller (or **No stage**) in the setup window and click
**Launch**.

## Updates

The app updates itself over the air. Once a day at start-up it asks GitHub whether there is a
newer version and says so in the status line; **Settings → Updates** shows the version you have,
checks on demand and **Install and restart** downloads the file for your computer, verifies its
SHA-256 against the checksum GitHub publishes, swaps the program and restarts it with the same
settings and stage port, without the setup window. Settings, logs,
chip records and snapshots are never touched, and nothing is installed while an exposure,
autofocus or homing is running.

- A copy from **Latest build** follows every new build of `main`; a copy from a numbered release
  (`v1.2.0`) follows numbered releases.
- `HackerfabStepper --update` updates from a terminal; `--update --check` only reports.
- Re-running the install command above does the same from outside the app.
- Run from source, **Settings → Updates** runs `git pull` instead.

## Getting help

Press **F1** (or **Help** in the top bar). The Help page:

- checks the camera, stage, projector, LEDs, pattern and chip record **right now**, and gives a
  one-click fix for anything that is not ready;
- answers "how do I …" and "what's wrong?" questions as you type (try *stripes*, *focus*,
  *won't move*, *LED*), with steps written for your operating system;
- opens the illustrated **[how-to guide](stepper/docs/HOW-TO-GUIDE.html)**, the log and the settings
  folder, and copies a problem report you can paste into a message.

## What it does

- **One screen to operate:** live camera with focus meter, stage pad, what the projector is
  showing, and the three steps of an exposure (1 Pattern, 2 Focus, 3 Expose). Esc or **STOP**
  ends anything that is running and turns the projector off.
- **A clean camera picture:** automatic stripe-free exposure, brightness control and fixed white
  balance remove the bands a DLP projector causes in a rolling-shutter camera.
- **Projector LEDs over USB** (optional): red only while focusing, blue for exposures, all off
  when idle.
- **Alignment:** a small YOLO model (`stepper/ckpts/best.onnx`, run by OpenCV, no PyTorch) finds
  the chip's alignment marks. **Auto-align stage** moves the stage onto them.
- **Tiling:** patterns larger than one projector field are split into tiles and exposed one
  after another, each aligned to the previous one.
- **Chip record:** every exposure is logged with its time, position, duration and pattern.

## Run from source

Needs [Python 3.10–3.13](https://www.python.org/downloads/). The launchers create a project-local
environment and install everything on the first run.

```bash
git clone https://github.com/illinoismicrotech/Hackerfab-Stepper.git
cd Hackerfab-Stepper/stepper
./run.sh          # Linux / macOS
```

On Windows, double-click `stepper\run.bat`.

## Documentation

| Document | For |
|---|---|
| [`stepper/docs/HOW-TO-GUIDE.html`](stepper/docs/HOW-TO-GUIDE.html) | Operators: install, every part of the window, a full exposure, troubleshooting (also in the app). |
| [`stepper/README.md`](stepper/README.md) | Setting up a station: stage controller (GRBL), cameras, projector LEDs, configuration, development. |
| [`stepper/docs/guide.md`](stepper/docs/guide.md) | Alignment-marker detection and calibrating Auto-align. |
| [`stepper/docs/model.md`](stepper/docs/model.md) | Training a new alignment-marker model. |

## How the downloads are made

GitHub Actions ([`.github/workflows/build.yml`](.github/workflows/build.yml)) runs the tests, then
builds and smoke-tests the app on Windows, macOS (Apple silicon and Intel) and Linux for every
push and pull request. Each build records its version, commit and update channel
(`build_info.json`), which is how an installed copy knows what to update to. A push to `main`
refreshes **Latest build** (and then checks that the install scripts work on all three systems);
a tag publishes a release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

To build it yourself, see [Development](stepper/README.md#development).

MIT License. Copyright (c) 2024 hacker-fab.
