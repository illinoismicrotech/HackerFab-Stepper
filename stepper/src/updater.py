"""Over-the-air updates for the standalone app, from the GitHub releases of this repository.

Every standalone build carries build_info.json (written by packaging/build.py): its version,
the commit it was built from and its update channel:

    stable   built from a tag such as v1.2.0: updates to the newest tagged release
    latest   built from main: updates to the "Latest build" pre-release whenever main changes

Updating downloads the file for this computer (the same one a person would pick on the Releases
page), checks its SHA-256 against the one GitHub publishes for it, and swaps it in next to the
running program:

    Windows   HackerfabStepper.exe        the running exe is renamed to .old.exe (Windows allows
                                           renaming, not overwriting, a running program)
    Linux     HackerfabStepper            unpacked from the .tar.gz, same rename
    macOS     HackerfabStepper.app        unpacked from the .zip with ditto, same rename

The .old copy is deleted the next time the app starts. Settings, logs and snapshots live in the
app's home folder and are never touched. A copy run from source (run.sh / run.bat) is a git
checkout: it is updated with git pull instead.

Uses only the standard library so it also works when everything else is broken:
    HackerfabStepper --update
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPOSITORY = "illinoismicrotech/HackerFab-Stepper"
API = f"https://api.github.com/repos/{REPOSITORY}"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"
NAME = "HackerfabStepper"
LATEST_TAG = "latest"            # the pre-release CI refreshes on every push to main
CHECK_INTERVAL = 24 * 3600       # automatic checks: at most once a day
CHECK_STAMP = ".update-check"    # in the app's home folder: when the last automatic check ran
READY_MARKER = ".update-ready"   # in the home folder: the restarted app is on screen
AFTER_UPDATE = "--after-update"  # restart flag: skip the setup window, reuse the last choices
RESTART_WAIT = 120               # seconds the "Updating…" window waits for the new version
TIMEOUT = 20


class UpdateError(Exception):
    """Something the user can read: what went wrong and what to do."""


@dataclass(frozen=True)
class BuildInfo:
    version: str = "0.0.0"
    channel: str = ""            # "stable", "latest" or "" (unknown: no updates offered)
    commit: str = ""

    @property
    def label(self) -> str:
        if self.channel == "stable":
            return f"version {self.version}"
        if self.commit:
            return f"build {self.commit[:7]} from main"
        return "development copy"


@dataclass(frozen=True)
class Update:
    label: str                   # "version 1.3.0" / "build 1a2b3c4 from main (2026-10-16)"
    tag: str
    asset: str                   # file name
    url: str                     # download URL
    size: int
    sha256: str                  # "" if GitHub gave none (then the download is refused)
    page: str                    # release page, for the notes


# --------------------------------------------------------------------------- this installation
def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def build_info() -> BuildInfo:
    """The bundled build_info.json; a default (no channel) when run from source or missing."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    try:
        data = json.loads((base / "build_info.json").read_text(encoding="utf-8"))
        return BuildInfo(str(data.get("version") or "0.0.0"), str(data.get("channel") or ""),
                         str(data.get("commit") or ""))
    except (OSError, ValueError):
        return BuildInfo()


def install_target() -> Optional[Path]:
    """What an update replaces: the exe / program file, or the .app bundle. None from source."""
    if not frozen():
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
        return None
    return exe


def source_checkout() -> Optional[Path]:
    """The git checkout this copy runs from, if any."""
    if frozen():
        return None
    root = Path(__file__).resolve().parents[2]
    return root if (root / ".git").exists() else None


def _mac_arch() -> str:
    # An Intel build running under Rosetta reports x86_64; ask the hardware so Apple-silicon
    # Macs move to the native build.
    try:
        out = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=3)
        if out.stdout.strip() == "1":
            return "arm64"
    except (OSError, subprocess.SubprocessError):
        pass
    return "arm64" if platform.machine() == "arm64" else "x86_64"


def asset_name(system: str = sys.platform, machine: Optional[str] = None) -> str:
    """The release file for this computer, as packaging/build.py names it."""
    if system == "win32":
        return f"{NAME}-Windows.exe"
    if system == "darwin":
        return f"{NAME}-macOS-{machine or _mac_arch()}.zip"
    machine = machine or platform.machine() or "x86_64"
    machine = {"amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine.lower(), machine)
    return f"{NAME}-Linux-{machine}.tar.gz"


# --------------------------------------------------------------------------- versions
def parse_version(text: str) -> Optional[tuple[tuple[int, ...], bool]]:
    """'v1.2.0' -> ((1, 2, 0), False); 'v1.2.0-beta' -> ((1, 2, 0), True); others -> None."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(-[0-9A-Za-z.-]+)?", (text or "").strip())
    if not match:
        return None
    numbers = tuple(int(n) for n in match.group(1).split("."))
    numbers += (0,) * (3 - len(numbers))
    return numbers, bool(match.group(2))


def is_prerelease_version(text: str) -> bool:
    """v0.x and dashed tags are pre-releases (as the release workflow marks them)."""
    parsed = parse_version(text)
    return parsed is None or parsed[1] or parsed[0][0] == 0


def newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if a is None:
        return False
    if b is None:
        return True
    if a[0] != b[0]:
        return a[0] > b[0]
    return b[1] and not a[1]     # 1.2.0 is newer than 1.2.0-beta


# --------------------------------------------------------------------------- GitHub
def _request(url: str, accept: str = "application/vnd.github+json"):
    headers = {"Accept": accept, "User-Agent": f"{NAME}-updater"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)


def _get_json(url: str):
    try:
        with _request(url) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise UpdateError("GitHub refused the update check for now (too many requests from this network). "
                              "Try again in an hour.") from exc
        if exc.code == 404:
            raise UpdateError("No release was found on GitHub.") from exc
        raise UpdateError(f"GitHub answered {exc.code} {exc.reason}.") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"Could not reach GitHub ({getattr(exc, 'reason', exc)}). Check the internet "
                          "connection.") from exc


def _update_from_release(release: dict, label: str, asset: str) -> Optional[Update]:
    for item in release.get("assets") or []:
        if item.get("name") == asset:
            digest = str(item.get("digest") or "")
            return Update(label, release.get("tag_name", ""), asset, item.get("browser_download_url", ""),
                          int(item.get("size") or 0), digest[7:] if digest.startswith("sha256:") else "",
                          release.get("html_url") or RELEASES_PAGE)
    return None


def find_update(build: Optional[BuildInfo] = None, asset: Optional[str] = None,
                get_json: Callable[[str], object] = _get_json) -> Optional[Update]:
    """The release this build should update to, or None when it is up to date.
    Raises UpdateError when GitHub cannot be asked, or this build has no update channel."""
    build = build or build_info()
    asset = asset or asset_name()
    if build.channel == "latest":
        release = get_json(f"{API}/releases/tags/{LATEST_TAG}")
        commit = str(release.get("target_commitish") or "")
        if not commit or (build.commit and commit.startswith(build.commit)):
            return None
        date = str(release.get("published_at") or "")[:10]
        update = _update_from_release(release, f"build {commit[:7]} from main" + (f" ({date})" if date else ""),
                                      asset)
        if update is None:
            raise UpdateError(f"The latest build has no {asset} yet (it may still be building). Try again later.")
        return update
    if build.channel == "stable":
        releases = get_json(f"{API}/releases?per_page=50")
        allow_pre = is_prerelease_version(build.version)
        best = None
        for release in releases:
            tag = str(release.get("tag_name") or "")
            if release.get("draft") or parse_version(tag) is None:
                continue
            if (release.get("prerelease") or is_prerelease_version(tag)) and not allow_pre:
                continue
            if newer(tag, build.version) and (best is None or newer(tag, best["tag_name"])):
                if _update_from_release(release, "", asset) is not None:
                    best = release
        if best is None:
            return None
        return _update_from_release(best, f"version {best['tag_name'].lstrip('v')}", asset)
    raise UpdateError("This copy was not built by the release workflow, so it does not know where its updates "
                      f"come from. Download the app from {RELEASES_PAGE}.")


# --------------------------------------------------------------------------- download
def download(update: Update, folder: Path, progress: Callable[[int, int], None] = lambda done, total: None,
             cancelled: Callable[[], bool] = lambda: False) -> Path:
    """Download into `folder` (next to the program, so the final rename stays on one disk) and
    verify the SHA-256. Returns the file; a partial or wrong download is deleted."""
    if not update.sha256:
        raise UpdateError("GitHub did not publish a checksum for this file, so it cannot be verified. "
                          f"Download it by hand from {update.page}.")
    target = folder / f".{NAME}-update-{update.asset}"
    part = target.with_name(target.name + ".part")
    digest, done = hashlib.sha256(), 0
    try:
        with _request(update.url, accept="application/octet-stream") as response, open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or update.size or 0)
            while True:
                if cancelled():
                    raise UpdateError("Update cancelled.")
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done, total)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except PermissionError as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Cannot write to {folder}. Move the app to a folder you can change "
                          "(not Program Files / a read-only disk), or install the update by hand.") from exc
    except (urllib.error.URLError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"The download failed ({getattr(exc, 'reason', exc)}). Try again.") from exc
    if digest.hexdigest() != update.sha256.lower():
        part.unlink(missing_ok=True)
        raise UpdateError("The downloaded file is damaged (its checksum does not match). Nothing was changed; "
                          "try again.")
    os.replace(part, target)
    return target


# --------------------------------------------------------------------------- install
def _old_parts(target: Path) -> tuple[str, str]:
    # Windows keeps the .exe ending (HackerfabStepper.old.exe) so the old copy can still start the
    # camera worker processes; elsewhere HackerfabStepper.old / HackerfabStepper.app.old.
    if target.suffix.lower() == ".exe":
        return target.stem, target.suffix
    return target.name, ""


def _old(target: Path, tag: str = "") -> Path:
    base, ext = _old_parts(target)
    return target.with_name(f"{base}{tag}.old{ext}")


def _unpack(archive: Path, target: Path) -> Path:
    """The new program / bundle, unpacked next to `target` as <name>.new."""
    new = target.with_name(target.name + ".new")
    _remove(new)
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive) as tar:
            member = next((m for m in tar.getmembers() if m.isfile() and Path(m.name).name == NAME), None)
            if member is None:
                raise UpdateError(f"The download does not contain {NAME}.")
            with tar.extractfile(member) as src, open(new, "wb") as out:
                shutil.copyfileobj(src, out)
        new.chmod(0o755)
    elif archive.name.endswith(".zip"):
        staging = Path(tempfile.mkdtemp(prefix=f".{NAME}-update-", dir=target.parent))
        try:
            # ditto keeps the bundle's symlinks, permissions and signature (zipfile does not).
            subprocess.run(["ditto", "-x", "-k", str(archive), str(staging)], check=True, capture_output=True)
            bundle = next(staging.glob("*.app"), None)
            if bundle is None:
                raise UpdateError(f"The download does not contain {NAME}.app.")
            os.replace(bundle, new)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise UpdateError(f"The download could not be unpacked ({exc}).") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    else:
        os.replace(archive, new)
        if sys.platform != "win32":
            new.chmod(0o755)
    return new


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def install(downloaded: Path, target: Path) -> None:
    """Swap the new program in. The running one becomes <name>.old; on any error the original
    is put back, so the app is never left without a working program."""
    new = _unpack(downloaded, target)
    old = _old(target)
    _remove(old)
    if old.exists():
        # A previous .old is still locked (Windows: an old copy still running): use another name.
        old = _old(target, f".{int(time.time())}")
    try:
        os.replace(target, old)
    except OSError as exc:
        _remove(new)
        raise UpdateError(f"Cannot replace {target} ({exc}). Move the app to a folder you can change, "
                          "or install the update by hand.") from exc
    try:
        os.replace(new, target)
    except OSError as exc:
        os.replace(old, target)
        _remove(new)
        raise UpdateError(f"Cannot replace {target} ({exc}). Nothing was changed.") from exc
    finally:
        _remove(downloaded)
    if frozen() and target == install_target():
        # The running app may still start camera worker processes, which re-launch "itself":
        # point those at the copy that is actually running (now .old), not the new version.
        import multiprocessing
        running = old / "Contents" / "MacOS" / NAME if sys.platform == "darwin" else old
        multiprocessing.set_executable(str(running))


def cleanup(target: Optional[Path] = None) -> None:
    """At start: delete what an earlier update left behind (.old copies, unfinished downloads)."""
    target = target or install_target()
    if target is None:
        return
    base, ext = _old_parts(target)
    for leftover in target.parent.glob(f"{base}*.old{ext}"):
        _remove(leftover)
    for leftover in list(target.parent.glob(f".{NAME}-update-*")) + [target.with_name(target.name + ".new")]:
        _remove(leftover)


def restart_arguments(config_path: str, port: Optional[str]) -> list[str]:
    """What the restarted app needs to skip the setup window: the same settings file and stage port."""
    return [AFTER_UPDATE, "--config", str(Path(config_path).resolve()), "--port", port or "none"]


def parse_restart_arguments(argv: list[str]) -> Optional[tuple[str, str]]:
    """(config path, stage port) when started by an update, else None."""
    if AFTER_UPDATE not in argv:
        return None

    def value(flag):
        try:
            return argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            return None
    config, port = value("--config"), value("--port")
    if not config or not Path(config).is_file():
        return None
    return config, port or "none"


def mark_ready(home: Path) -> None:
    """Called by the restarted app once its window is on screen: closes the "Updating…" window."""
    try:
        (home / READY_MARKER).write_text(f"{time.time():.0f}\n")
    except OSError:
        pass


def relaunch(target: Optional[Path] = None, arguments: Optional[list[str]] = None):
    """Start the (updated) app as a fresh process. Call after the window has closed.
    Returns the process (None on macOS, where `open` starts it)."""
    target = target or install_target()
    if target is None:
        return None
    arguments = list(arguments or [])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI_", "_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"   # a new program, not a child sharing our unpacked files
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-n", str(target)] + (["--args"] + arguments if arguments else []), env=env)
        return None
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen([str(target)] + arguments, env=env, creationflags=flags, close_fds=True)
    return subprocess.Popen([str(target)] + arguments, env=env, start_new_session=True, close_fds=True)


def restart(label: str, config_path: str, port: Optional[str], home: Optional[Path] = None) -> None:
    """Start the new version and show a small "Updating…" window until it is on screen.

    A standalone app unpacks itself before anything appears (10-30 s), which would otherwise look
    like nothing happened. The window closes when the new version writes READY_MARKER, when it
    exits early (then it says so), or after RESTART_WAIT seconds."""
    import tkinter as tk

    home = home or Path.cwd()
    marker = home / READY_MARKER
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        process = relaunch(arguments=restart_arguments(config_path, port))
    except OSError as exc:
        print(f"Could not start the new version: {exc}")
        process = None
        failed = f"Could not start the new version ({exc}). Start HackerFab Stepper again yourself."
    else:
        failed = ""
    try:
        root = tk.Tk()
    except tk.TclError:
        return   # no screen: nothing to show
    root.title("HackerFab Stepper")
    root.configure(background="#1e1f22")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    text = tk.StringVar(value=failed or f"Updated to {label}.\nStarting the new version…")
    tk.Label(root, textvariable=text, background="#1e1f22", foreground="#e8e8e8", justify="center",
             font=("TkDefaultFont", 12), padx=36, pady=28).pack()
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 2}")
    started, dots = time.monotonic(), [0]

    def poll():
        if failed:
            root.after(8000, root.destroy)
            return
        if marker.exists():
            marker.unlink(missing_ok=True)
            root.destroy()
            return
        code = process.poll() if process is not None else None
        if code == 0:
            root.destroy()   # it already ran and closed
            return
        if code is not None:
            text.set("The new version did not start. Start HackerFab Stepper again yourself;\n"
                     "if that fails, run the install command from the README.")
            root.after(10000, root.destroy)
            return
        if time.monotonic() - started > RESTART_WAIT:
            root.destroy()
            return
        dots[0] = (dots[0] + 1) % 4
        text.set(f"Updated to {label}.\nStarting the new version" + "." * dots[0] + " " * (3 - dots[0]))
        root.after(400, poll)
    root.after(400, poll)
    root.mainloop()


# --------------------------------------------------------------------------- source checkouts
def git_status(root: Path) -> tuple[int, str]:
    """(commits the checkout is behind its upstream, description). Fetches first."""
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    try:
        fetched = git("fetch", "--quiet")
        if fetched.returncode:
            raise UpdateError(f"git fetch failed: {fetched.stderr.strip() or 'no details'}")
        behind = git("rev-list", "--count", "HEAD..@{upstream}")
        if behind.returncode:
            raise UpdateError("This branch does not follow a branch on GitHub, so there is nothing to compare with.")
        count = int(behind.stdout.strip() or 0)
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        return count, branch
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"git is not available ({exc}).") from exc


def git_pull(root: Path) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), "pull", "--ff-only"], capture_output=True, text=True,
                                timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"git is not available ({exc}).") from exc
    if result.returncode:
        raise UpdateError("git pull could not update this copy automatically (local changes or a diverged "
                          f"branch):\n{(result.stderr or result.stdout).strip()[-600:]}")
    return result.stdout.strip()


# --------------------------------------------------------------------------- automatic checks
def check_due(home: Path, now: Optional[float] = None) -> bool:
    """True at most once per CHECK_INTERVAL (records the time when it says yes)."""
    now = time.time() if now is None else now
    stamp = home / CHECK_STAMP
    try:
        if now - float(stamp.read_text().strip()) < CHECK_INTERVAL:
            return False
    except (OSError, ValueError):
        pass
    try:
        stamp.write_text(f"{now:.0f}\n")
    except OSError:
        pass
    return True


def automatic_checks_allowed() -> bool:
    """Only the standalone app checks by itself (never tests, smoke tests or source copies)."""
    return frozen() and not os.environ.get("HACKERFAB_NO_UPDATE_CHECK") and build_info().channel != ""


# --------------------------------------------------------------------------- command line
def main(argv: list[str]) -> int:
    """HackerfabStepper --update [--check]: update the standalone app from a terminal."""
    check_only = "--check" in argv
    try:
        root = source_checkout()
        if root is not None:
            behind, branch = git_status(root)
            if not behind:
                print(f"Up to date ({branch}).")
                return 0
            print(f"{behind} new commit(s) on {branch}.")
            if not check_only:
                print(git_pull(root))
                print("Updated. Start the app with run.sh / run.bat (it installs any new dependencies).")
            return 0
        target = install_target()
        build = build_info()
        print(f"This copy: {build.label} ({target or 'not a standalone app'})")
        update = find_update(build)
        if update is None:
            print("Up to date.")
            return 0
        print(f"Available: {update.label}  ({update.asset}, {update.size / 1e6:.0f} MB)")
        if check_only:
            return 0
        if target is None:
            raise UpdateError("Only the standalone app can update itself.")
        last = [-1]

        def progress(done, total):
            percent = int(100 * done / total) if total else 0
            if percent // 10 != last[0]:
                last[0] = percent // 10
                print(f"  {percent:3d} %", flush=True)
        install(download(update, target.parent, progress), target)
        print(f"Installed {update.label}. Start the app again to use it.")
        return 0
    except UpdateError as exc:
        print(f"Update failed: {exc}")
        return 1
