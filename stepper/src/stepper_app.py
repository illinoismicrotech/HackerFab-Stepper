"""Entry point for the standalone app (Windows .exe, macOS .app, Linux program).

The GUI resolves files such as default.toml and src/uvFocusImage/plus.png relative to
the working directory, exactly as when started by run.bat / run.sh from the stepper folder.
The app therefore works inside its "home" folder and, on first run, copies the bundled
default files there so they can be edited in any text editor:

    Windows / Linux   the folder the app is in (or a per-user folder if that is read-only)
    macOS             ~/Documents/HackerfabStepper (an .app bundle must not be modified)
    any OS            the folder named by the HACKERFAB_HOME environment variable, if set

    HackerfabStepper                 normal start
    HackerfabStepper --smoke-test    build the UI with no hardware, then exit (CI check)
"""
import multiprocessing
import os
import sys
import time
import traceback
from pathlib import Path

# Media Foundation's hardware transforms can make opening a camera take 10+ seconds,
# longer than the capture watchdog allows. OpenCV reads this on every camera open, and
# the camera worker processes inherit it.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

BUNDLED_FILES = ("default.toml", "src/uvFocusImage/plus.png")  # user-editable: copied only if missing
REFRESHED_FILES = ("HOW-TO-GUIDE.html",)  # documentation: replaced when this exe ships a different copy
LOG_NAME = "HackerfabStepper.log"
LOG_MAX_BYTES = 2_000_000


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _per_user_folder() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HackerfabStepper"
    if sys.platform == "darwin":
        return Path.home() / "Documents" / "HackerfabStepper"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "HackerfabStepper"


def _home() -> Path:
    if os.environ.get("HACKERFAB_HOME"):
        home = Path(os.environ["HACKERFAB_HOME"]).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True)
        return home
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[1]  # the stepper/ folder, like run.bat
    home = Path(sys.executable).resolve().parent
    if sys.platform != "darwin" and _writable(home):
        return home
    # macOS app bundles, or e.g. C:\Program Files: keep settings, logs and snapshots per user instead.
    fallback = _per_user_folder()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _prepare_working_directory(home: Path) -> None:
    # Bundled copies inside a frozen app; the stepper/ folder (and its docs/) when run from source.
    source_root = Path(__file__).resolve().parents[1]
    bundle = Path(getattr(sys, "_MEIPASS", source_root))
    for relative in BUNDLED_FILES + REFRESHED_FILES:
        target, source = home / relative, bundle / relative
        if not source.exists() and not hasattr(sys, "_MEIPASS"):
            source = bundle / "docs" / relative
        if not source.exists() or source.resolve() == target.resolve():
            continue
        data = source.read_bytes()
        if not target.exists() or (relative in REFRESHED_FILES and target.read_bytes() != data):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    os.chdir(home)


def _redirect_output(home: Path) -> None:
    # A double-clicked app has no console to show messages; keep the GUI's status
    # messages and any errors in a log file in the app's home folder instead.
    if getattr(sys, "frozen", False) or sys.stdout is None or sys.stderr is None:
        path = home / LOG_NAME
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                path.replace(path.with_name(LOG_NAME + ".old"))  # keep one previous log
        except OSError:
            pass
        log = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        log.write(f"\n===== HackerfabStepper started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        sys.stdout = log
        sys.stderr = log


def _child_process_check(queue):
    queue.put("ok")


def _process_start_works() -> bool:
    """The camera runs in a child process; prove the frozen app can start one."""
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_child_process_check, args=(queue,), daemon=True)
    process.start()
    try:
        return queue.get(timeout=60) == "ok"
    except Exception:
        return False
    finally:
        process.join(5)


def _smoke_test() -> int:
    """Build the full UI without hardware and prove camera worker processes can start."""
    import toml
    import ttkbootstrap as ttk

    import gui
    from camera.webcam import Webcam
    from stage_control.stage_controller import StageController

    root = ttk.Window(themename="darkly")
    gui.configure_ui_scale(root)
    gui.configure_theme(root)
    errors = []
    root.report_callback_exception = lambda *args: errors.append("".join(traceback.format_exception(*args)))
    config = toml.load("default.toml")
    config.setdefault("camera", {})["type"] = "none"
    lithographer_config = gui.LithographerConfig(
        StageController(), None, 0.25, 4167, 25000,
        gui.AlignmentConfig(False, "ckpts/best.onnx", 1820, 280, 269, 1075, -1100, 800))
    app = gui.LithographerGui(lithographer_config, root, config, str(Path.cwd() / "smoke-test-config.toml"))
    root.update()

    # The alignment model must be bundled and must run on OpenCV.
    detector = app.event_dispatcher.model
    detector_ok = False
    if detector is not None:
        try:
            import numpy as np
            detector.detect(np.zeros((720, 1280, 3), np.uint8))
            detector_ok = True
        except Exception as exc:
            print(f"Alignment model failed to run: {exc}")
    print(f"Alignment model: {'ok' if detector_ok else 'MISSING OR BROKEN'} ({getattr(detector, 'path', None)})")

    processes_ok = _process_start_works()
    print(f"Child process check: {'ok' if processes_ok else 'FAILED'}")
    if sys.platform == "darwin":
        # Opening any camera on macOS triggers a permission prompt, which a CI machine cannot answer.
        root.destroy()
        ok = not errors and processes_ok and detector_ok
        if errors:
            print("UI callback errors:\n" + "\n".join(errors))
        print("SMOKE TEST " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1

    # The camera runs in a separate process; in a frozen app that only works when
    # multiprocessing.freeze_support() is wired up correctly. Ask for a camera index
    # that does not exist and wait for the worker process to report back.
    camera = Webcam(settings={"device": "97"})
    camera.startStreamCapture()
    deadline = time.monotonic() + 30
    while camera.state not in ("error", "streaming") and time.monotonic() < deadline:
        root.update()
        time.sleep(0.1)
    worker_state, worker_status = camera.state, camera.status
    camera.close()
    root.destroy()

    print(f"Camera worker state: {worker_state} ({worker_status})")
    if errors:
        print("UI callback errors:\n" + "\n".join(errors))
    # The worker must answer from its own process ("No usable camera mode" / "No camera found");
    # a timeout or a crashed worker means multiprocessing is broken in this build.
    reported = any(text in worker_status for text in ("No usable camera mode", "No camera found"))
    ok = not errors and processes_ok and detector_ok and worker_state == "error" and reported
    print("SMOKE TEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    home = _home()
    try:
        _redirect_output(home)
    except OSError:
        pass  # no log file; the app still runs
    try:
        _prepare_working_directory(home)
        if getattr(sys, "frozen", False) and home != Path(sys.executable).resolve().parent:
            print(f"Using {home} for settings, logs and snapshots.")
        if "--smoke-test" in sys.argv:
            return _smoke_test()
        import gui
        gui.main()
        return 0
    except SystemExit:
        raise
    except Exception:
        details = traceback.format_exc()
        print(details)
        if "--smoke-test" in sys.argv:
            return 1  # never block an automated check on a dialog
        try:
            from tkinter import Tk, messagebox
            window = Tk()
            window.withdraw()
            messagebox.showerror("HackerFab Stepper",
                                 f"The application stopped because of an error:\n\n{details[-1500:]}\n"
                                 f"Full details are in {home / LOG_NAME}")
            window.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()  # must run first: camera worker processes re-launch this app
    sys.exit(main())
