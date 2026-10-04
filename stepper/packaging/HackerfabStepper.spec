# PyInstaller build definition for the standalone HackerFab Stepper app.
# Works on Windows, macOS and Linux. Build with:   python packaging/build.py
import os
import sys

root = os.path.abspath(os.path.join(SPECPATH, ".."))
src = os.path.join(root, "src")
name = "HackerfabStepper"

hiddenimports = ["gui", "settings", "camera.webcam", "camera.discovery", "serial.tools.list_ports",
                 "projector_window", "fullscreen_preview", "alignment_detector",
                 "PIL._tkinter_finder", "PIL.ImageTk"]  # ttkbootstrap draws its widgets with Pillow
if sys.platform == "win32":
    hiddenimports.append("pygrabber.dshow_graph")

a = Analysis(
    [os.path.join(src, "stepper_app.py")],
    pathex=[src],
    datas=[
        (os.path.join(root, "default.toml"), "."),
        (os.path.join(src, "uvFocusImage", "plus.png"), os.path.join("src", "uvFocusImage")),
        (os.path.join(root, "docs", "HOW-TO-GUIDE.html"), "."),  # opened by Help & guide / F1
        (os.path.join(root, "ckpts", "best.onnx"), "ckpts"),     # alignment-marker model (runs on OpenCV)
    ],
    hiddenimports=hiddenimports,
    excludes=["ultralytics", "torch", "torchvision", "matplotlib", "pandas", "scipy", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    # macOS: a normal double-clickable .app bundle.
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, console=False,
              upx=False, strip=False, debug=False, argv_emulation=False)
    coll = COLLECT(exe, a.binaries, a.datas, name=name, upx=False, strip=False)
    app = BUNDLE(
        coll,
        name=f"{name}.app",
        bundle_identifier="edu.hackerfab.stepper",
        info_plist={
            "CFBundleDisplayName": "HackerFab Stepper",
            "CFBundleShortVersionString": os.environ.get("APP_VERSION", "0.0.0"),
            "NSHighResolutionCapable": True,
            # Without this macOS refuses camera access instead of asking the user.
            "NSCameraUsageDescription": "HackerFab Stepper shows the stepper's microscope camera.",
        },
    )
else:
    # Windows and Linux: one self-contained file.
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas,
        name=name,
        console=False,      # windowed app; output goes to HackerfabStepper.log next to the app
        upx=False,          # UPX-packed exes trigger more antivirus false positives
        strip=False,
        debug=False,
    )
