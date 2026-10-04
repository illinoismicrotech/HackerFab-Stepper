# HackerFab Stepper

Desktop control software for the HackerFab photolithography stepper: pattern selection, stage alignment, timed UV exposure, and a live camera preview.

## Download and run (easiest)

No Python, no installing, no command line. Go to the **[Releases page](../../releases)** and download the one file for your computer:

| Computer | Download | Then |
|---|---|---|
| Windows 10/11 | `HackerfabStepper-Windows.exe` | Put it in its own folder (e.g. `C:\HackerfabStepper\`) and double-click it. If Windows says "Windows protected your PC", click **More info → Run anyway**. |
| Mac, Apple silicon (M1–M4) | `HackerfabStepper-macOS-arm64.zip` | Double-click the zip, drag **HackerfabStepper.app** to Applications. First time: right-click it → **Open** → **Open** (or System Settings → Privacy & Security → **Open Anyway**). |
| Mac, Intel | `HackerfabStepper-macOS-x86_64.zip` | Same as above. |
| Linux (64-bit) | `HackerfabStepper-Linux-x86_64.tar.gz` | `tar xzf HackerfabStepper-Linux-x86_64.tar.gz && ./HackerfabStepper` |

The newest build of `main` is always under **Latest build**; numbered versions (e.g. `v1.0.0`) are the stable releases.

When it starts, click **Start** in the small setup window. The full step-by-step guide is built in: press **F1** or click **Help & guide** in the app.

> The download leaves out the YOLO alignment-marker detector (it needs PyTorch, 1–2 GB). Everything else works. For auto-alignment and tiling, run from source as below.

## Run from source (developers, or for auto-alignment)

Needs [Python 3.10–3.13](https://www.python.org/downloads/) (3.13 recommended). The launchers create a project-local environment and install everything on first run (about 1–2 GB, mostly PyTorch).

```bash
git clone https://github.com/audicakes/Hackerfab-Stepper.git
cd Hackerfab-Stepper/stepper
```

- **Windows:** double-click `run.bat` (or `.\run.ps1 -SetupOnly` in PowerShell to only install).
- **Linux / macOS:** `./run.sh` (run `chmod +x run.sh` first if needed).

See the [application guide](stepper/README.md) for GRBL setup, camera support, and configuration details.

## Before first use

- Connect the USB camera and the GRBL stage controller (e.g. an SKR Pico, shown as an RP2040 board).
- In the setup window, pick the stage controller, or **No stage** to try the software without hardware.
- Settings live in `default.toml` / `config.toml` (next to the app; on macOS in `Documents/HackerfabStepper`). The **Settings** page in the app edits them for you.
- If text is too small on a high-resolution display, start the app with the environment variable `HACKERFAB_UI_SCALE=1.6`.

## How the downloads are made

GitHub Actions ([`.github/workflows/build.yml`](.github/workflows/build.yml)) runs the tests, then builds and smoke-tests the app on Windows, macOS and Linux for every push. A push to `main` refreshes **Latest build** on the Releases page; pushing a tag publishes a numbered release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

To build it yourself, see [Standalone app](stepper/README.md#standalone-app-windows-macos-linux) in the application guide.
