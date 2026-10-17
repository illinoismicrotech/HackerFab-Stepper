# PyInstaller build definition for the standalone HackerFab Stepper app.
# Works on Windows, macOS and Linux. Build with:   python packaging/build.py
#
# Size: the app only carries what it uses. OpenCV's FFmpeg library (30 MB, for video files; the
# camera does not need it) and other unused libraries are left out below.
import os
import re
import sys

from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter

root = os.path.abspath(os.path.join(SPECPATH, ".."))
src = os.path.join(root, "src")
name = "HackerfabStepper"

# Maximum zlib compression for the Python modules too (default 6); costs only build time.
CArchiveWriter._COMPRESSION_LEVEL = ZlibArchiveWriter._COMPRESSION_LEVEL = 9

hiddenimports = ["gui", "settings", "dispatcher", "ui_kit", "ui_theme", "camera_view", "stage_pad",
                 "projector_panel", "steps_panel", "alignment_page", "chip_pages", "projector_leds", "help_page",
                 "platform_text", "alignment_detector", "projector_window", "updater", "update_panel",
                 "camera.webcam", "camera.discovery", "camera.modes", "camera.processing", "camera.auto_exposure",
                 "camera.stripes", "camera.dshow_controls", "serial.tools.list_ports",
                 "usb", "usb.core", "usb.util", "usb.backend.libusb1", "libusb_package",
                 "PIL._tkinter_finder", "PIL.ImageTk"]  # ttkbootstrap draws its widgets with Pillow
if sys.platform == "win32":
    hiddenimports += ["pygrabber.dshow_graph", "comtypes"]

a = Analysis(
    [os.path.join(src, "stepper_app.py")],
    pathex=[src],
    datas=[
        (os.path.join(root, "default.toml"), "."),
        (os.path.join(src, "uvFocusImage", "plus.png"), os.path.join("src", "uvFocusImage")),
        (os.path.join(root, "docs", "HOW-TO-GUIDE.html"), "."),  # the illustrated guide (Help page)
        (os.path.join(root, "ckpts", "best.onnx"), "ckpts"),     # alignment-marker model (runs on OpenCV)
        (os.path.join(root, "build", "build_info.json"), "."),   # version and update channel (packaging/build.py)
    ],
    hiddenimports=hiddenimports,
    excludes=["ultralytics", "torch", "torchvision", "matplotlib", "pandas", "scipy", "pytest", "IPython",
              # never used at run time
              "setuptools", "pkg_resources", "_distutils_hack", "distutils", "lib2to3", "pydoc", "pydoc_data",
              "doctest", "pdb", "unittest", "numpy.f2py", "numpy.testing", "numpy.distutils",
              "ftplib", "http.server", "xmlrpc", "tkinter.tix", "turtle", "turtledemo",
              "idlelib", "sqlite3", "_sqlite3"],
    noarchive=False,
)

# Binaries the app never loads: FFmpeg (video files), image formats it never reads (AVIF, WebP)
# and colour management.
_drop_binaries = re.compile(r"(opencv_videoio_ffmpeg|PIL[\\/]_avif|PIL[\\/]_webp|PIL[\\/]_imagingcms|sqlite3)",
                            re.IGNORECASE)
a.binaries = [b for b in a.binaries if not _drop_binaries.search(b[0])]
# Tcl's time-zone database (600 files, unpacked at every start) and Tk's demo images.
_drop_data = re.compile(r"^(_tcl_data[\\/]tzdata|_tk_data[\\/]demos)[\\/]", re.IGNORECASE)
a.datas = [d for d in a.datas if not _drop_data.search(d[0])]

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
