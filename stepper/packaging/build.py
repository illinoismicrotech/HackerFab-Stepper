"""Build the standalone HackerFab Stepper app for the current OS, test it, and package it.

    python packaging/build.py            (run from the stepper/ folder, in an environment
                                          with packaging/requirements-build.txt installed)

Output, ready to upload, in dist/release/:
    Windows  HackerfabStepper-Windows.exe
    macOS    HackerfabStepper-macOS-<arm64|x86_64>.zip   (contains HackerfabStepper.app)
    Linux    HackerfabStepper-Linux-x86_64.tar.gz        (contains the HackerfabStepper program)

On Linux the smoke test needs a display; in CI it runs under xvfb-run.
"""
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

STEPPER = Path(__file__).resolve().parents[1]
DIST = STEPPER / "dist"
RELEASE = DIST / "release"
NAME = "HackerfabStepper"


def run(*command, **kwargs):
    print("+", " ".join(str(c) for c in command), flush=True)
    subprocess.run([str(c) for c in command], check=True, **kwargs)


def build():
    env = dict(os.environ)
    if sys.platform.startswith("linux"):
        # Standalone Python builds keep libtcl/libtk next to libpython. Let PyInstaller's
        # dependency scan find them, or the app starts with "libtcl...: cannot open shared object".
        lib = str(Path(sys.base_prefix) / "lib")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [lib, env.get("LD_LIBRARY_PATH")]))
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", DIST, "--workpath", STEPPER / "build", STEPPER / "packaging" / f"{NAME}.spec",
        cwd=STEPPER, env=env)


def program() -> Path:
    if sys.platform == "win32":
        return DIST / f"{NAME}.exe"
    if sys.platform == "darwin":
        return DIST / f"{NAME}.app" / "Contents" / "MacOS" / NAME
    return DIST / NAME


def smoke_test():
    """Start the built app with --smoke-test in an empty folder and check that it passes."""
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, HACKERFAB_HOME=home)
        result = subprocess.run([str(program()), "--smoke-test"], env=env, timeout=300)
        log = Path(home) / f"{NAME}.log"
        if log.exists():
            print("".join(log.read_text(errors="replace").splitlines(keepends=True)[-12:]))
        if result.returncode != 0:
            sys.exit(f"Smoke test failed (exit code {result.returncode})")
    print("Smoke test passed")


def package() -> Path:
    shutil.rmtree(RELEASE, ignore_errors=True)
    RELEASE.mkdir(parents=True)
    if sys.platform == "win32":
        target = RELEASE / f"{NAME}-Windows.exe"
        shutil.copy2(program(), target)
    elif sys.platform == "darwin":
        arch = "arm64" if platform.machine() == "arm64" else "x86_64"
        target = RELEASE / f"{NAME}-macOS-{arch}.zip"
        # ditto keeps the bundle's symlinks and code signature intact (zipfile does not).
        run("ditto", "-c", "-k", "--keepParent", DIST / f"{NAME}.app", target)
    else:
        arch = platform.machine() or "x86_64"
        target = RELEASE / f"{NAME}-Linux-{arch}.tar.gz"
        with tarfile.open(target, "w:gz") as archive:  # a tarball keeps the "executable" permission
            archive.add(program(), arcname=NAME)
    print(f"\nBuilt {target}  ({target.stat().st_size / 1e6:.0f} MB)")
    return target


if __name__ == "__main__":
    build()
    smoke_test()
    package()
