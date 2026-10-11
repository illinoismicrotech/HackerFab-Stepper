# PyInstaller build definition for HackerfabStepper.exe
# Build on Windows from the stepper/ folder:   .\packaging\build_exe.ps1
import os

root = os.path.abspath(os.path.join(SPECPATH, ".."))
src = os.path.join(root, "src")

a = Analysis(
    [os.path.join(src, "stepper_app.py")],
    pathex=[src],
    datas=[
        (os.path.join(root, "default.toml"), "."),
        (os.path.join(src, "uvFocusImage", "plus.png"), os.path.join("src", "uvFocusImage")),
        (os.path.join(root, "docs", "HOW-TO-GUIDE.html"), "."),  # opened by Help & guide / F1
    ],
    hiddenimports=["gui", "settings", "camera.webcam", "camera.discovery", "serial.tools.list_ports", "projector_window", "pygrabber.dshow_graph"],
    excludes=["ultralytics", "torch", "torchvision", "matplotlib", "pandas", "scipy", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="HackerfabStepper",
    console=False,      # windowed app; output goes to HackerfabStepper.log next to the exe
    upx=False,          # UPX-packed exes trigger more antivirus false positives
    strip=False,
    debug=False,
)
