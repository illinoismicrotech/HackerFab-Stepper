"""Operating-system specific wording and helpers, so every message tells the user what to do
on the computer they are actually using (Windows, macOS or Linux)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    OS_NAME = "Windows"
    EXTEND_DISPLAYS = "press Win+P and choose Extend"
    DISPLAY_SCALING = "the Windows display scaling"
    FILE_MANAGER = "File Explorer"
elif sys.platform == "darwin":
    OS_NAME = "macOS"
    EXTEND_DISPLAYS = "open System Settings → Displays and turn off mirroring"
    DISPLAY_SCALING = "the macOS display scaling"
    FILE_MANAGER = "Finder"
else:
    OS_NAME = "Linux"
    EXTEND_DISPLAYS = "set your display settings to extend (not mirror) the desktop"
    DISPLAY_SCALING = "the desktop's display scaling"
    FILE_MANAGER = "the file manager"


def open_path(path) -> None:
    """Open a file or folder with the system's default program."""
    path = str(Path(path))
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
