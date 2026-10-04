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

Everything is included, including the alignment-marker detector used by **Auto-align** and **tiling**.

## Run from source (developers)

Needs [Python 3.10–3.13](https://www.python.org/downloads/) (3.13 recommended). The launchers create a project-local environment and install everything on first run (about 150 MB).

```bash
git clone https://github.com/illinoismicrotech/Hackerfab-Stepper.git
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

## Alignment and tiling

- **Auto-align** (Image alignment page) finds the alignment marks in the camera view and moves the stage so they line up with the calibrated positions under `[alignment]`. Turn on **Detect alignment markers in real time** first to check the marks are found.
- **Tiling** (Chip records & tiling page) exposes a pattern bigger than one projector field: **Split pattern into tiles**, then **Start tiling**. Each tile after the first is corrected using the previous tile's marks. **Stop tiling** stops the run.
- The marker positions in `default.toml` were calibrated on one station's camera. Check them on yours before relying on Auto-align or tiling: see [`stepper/docs/guide.md`](stepper/docs/guide.md#calibrating-alignment-parameters).
- The detector is a small YOLO model, `stepper/ckpts/best.onnx`, run by OpenCV (no PyTorch). To retrain it, see [`stepper/docs/model.md`](stepper/docs/model.md).

## How the downloads are made

GitHub Actions ([`.github/workflows/build.yml`](.github/workflows/build.yml)) runs the tests, then builds and smoke-tests the app on Windows, macOS and Linux for every push. A push to `main` refreshes **Latest build** on the Releases page; pushing a tag publishes a numbered release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

To build it yourself, see [Standalone app](stepper/README.md#standalone-app-windows-macos-linux) in the application guide.
