# PyInstaller build definition for HackerfabStepper.exe
# Build on Windows from the stepper/ folder:   .\packaging\build_exe.ps1
#
# Size: the exe only carries what the app uses. OpenCV's FFmpeg DLL (30 MB, for video files; the
# camera uses DirectShow) and other unused libraries are left out below. The regular opencv-python
# cv2.pyd (75 MB) is still most of the exe. `build_exe.ps1 -SlimOpenCV` swaps in an experimental
# slim build (packaging/build_opencv_slim.sh) that is not used by default until it is confirmed on
# the stepper's camera.
import os
import re

from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter

root = os.path.abspath(os.path.join(SPECPATH, ".."))
src = os.path.join(root, "src")

# Maximum zlib compression for the Python modules too (default 6); costs only build time.
CArchiveWriter._COMPRESSION_LEVEL = ZlibArchiveWriter._COMPRESSION_LEVEL = 9

a = Analysis(
    [os.path.join(src, "stepper_app.py")],
    pathex=[src],
    datas=[
        (os.path.join(root, "default.toml"), "."),
        (os.path.join(src, "uvFocusImage", "plus.png"), os.path.join("src", "uvFocusImage")),
        (os.path.join(root, "docs", "HOW-TO-GUIDE.html"), "."),  # opened by Help & guide / F1
    ],
    hiddenimports=["gui", "settings", "dispatcher", "ui_kit", "ui_theme", "camera_view", "stage_pad",
                   "projector_panel", "steps_panel", "alignment_page", "chip_pages", "projector_leds",
                   "camera.webcam", "camera.discovery", "camera.modes", "camera.processing", "camera.auto_exposure",
                   "camera.stripes",
                   "camera.dshow_controls", "serial.tools.list_ports", "projector_window", "pygrabber.dshow_graph",
                   "comtypes", "usb", "usb.core", "usb.util", "usb.backend.libusb1", "libusb_package"],
    excludes=["ultralytics", "torch", "torchvision", "matplotlib", "pandas", "scipy", "pytest", "IPython",
              # never used at run time
              "setuptools", "pkg_resources", "_distutils_hack", "distutils", "lib2to3", "pydoc", "pydoc_data",
              "doctest", "pdb", "unittest", "numpy.f2py", "numpy.testing", "numpy.distutils",
              "ssl", "_ssl", "_hashlib", "ftplib", "http.server", "xmlrpc", "tkinter.tix", "turtle", "turtledemo",
              "idlelib", "sqlite3", "_sqlite3"],
    noarchive=False,
)

# Binaries the app never loads: FFmpeg (video files; the camera uses DirectShow), image formats we
# never read (AVIF, WebP), colour management, and OpenSSL (no network code).
_drop_binaries = re.compile(r"(opencv_videoio_ffmpeg|PIL[\\/]_avif|PIL[\\/]_webp|PIL[\\/]_imagingcms|"
                            r"libcrypto-|libssl-|_ssl\.pyd|_hashlib\.pyd|sqlite3)", re.IGNORECASE)
a.binaries = [b for b in a.binaries if not _drop_binaries.search(b[0])]
# Tcl's time-zone database (600 files, unpacked at every start) and Tk's demo images.
_drop_data = re.compile(r"^(_tcl_data[\\/]tzdata|_tk_data[\\/]demos)[\\/]", re.IGNORECASE)
a.datas = [d for d in a.datas if not _drop_data.search(d[0])]

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
