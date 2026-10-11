# HackerFab Stepper

Desktop control software for the HackerFab maskless photolithography stepper: a DLP projector
shows the pattern in red light (safe for resist) to focus and align, then in blue light to expose
it, while a USB microscope camera watches the chip and a GRBL stage moves it.

It runs on **Windows, macOS and Linux**, either as a ready-made download or from source.

## Download and run

No Python, no installing, no command line. Open the **[Releases page](../../releases)** and download
the file for your computer from **Latest build** (the newest version of `main`):

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
push and pull request. A push to `main` refreshes **Latest build**; a tag publishes a release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

To build it yourself, see [Development](stepper/README.md#development).

MIT License. Copyright (c) 2024 hacker-fab.
