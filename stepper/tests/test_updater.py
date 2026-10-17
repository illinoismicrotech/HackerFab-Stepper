"""Over-the-air updates: which release a build moves to, verified downloads, the swap and its clean-up."""
import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401

import updater
from updater import BuildInfo, Update, UpdateError


def asset(name, data=b"x", digest=True):
    return {"name": name, "size": len(data), "browser_download_url": f"https://example.invalid/{name}",
            "digest": "sha256:" + hashlib.sha256(data).hexdigest() if digest else None}


def release(tag, prerelease=False, commit="main", names=("HackerfabStepper-Windows.exe",)):
    return {"tag_name": tag, "prerelease": prerelease, "draft": False, "target_commitish": commit,
            "published_at": "2026-10-16T12:00:00Z", "html_url": f"https://example.invalid/{tag}",
            "assets": [asset(n) for n in names]}


class VersionTests(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(updater.parse_version("v1.2"), ((1, 2, 0), False))
        self.assertEqual(updater.parse_version("v1.2.0-beta"), ((1, 2, 0), True))
        self.assertIsNone(updater.parse_version("latest"))
        self.assertTrue(updater.newer("v1.10.0", "1.9.3"))
        self.assertTrue(updater.newer("v1.2.0", "1.2.0-rc1"))
        self.assertFalse(updater.newer("v1.2.0-rc1", "1.2.0"))
        self.assertFalse(updater.newer("v1.2.0", "1.2.0"))
        self.assertTrue(updater.is_prerelease_version("0.4.0"))
        self.assertFalse(updater.is_prerelease_version("1.0.0"))

    def test_asset_names_match_the_build_script(self):
        self.assertEqual(updater.asset_name("win32"), "HackerfabStepper-Windows.exe")
        self.assertEqual(updater.asset_name("darwin", "arm64"), "HackerfabStepper-macOS-arm64.zip")
        self.assertEqual(updater.asset_name("darwin", "x86_64"), "HackerfabStepper-macOS-x86_64.zip")
        self.assertEqual(updater.asset_name("linux", "x86_64"), "HackerfabStepper-Linux-x86_64.tar.gz")
        self.assertEqual(updater.asset_name("linux", "AMD64"), "HackerfabStepper-Linux-x86_64.tar.gz")


class FindUpdateTests(unittest.TestCase):
    EXE = "HackerfabStepper-Windows.exe"

    def find(self, build, answers):
        return updater.find_update(build, self.EXE, get_json=lambda url: answers[url.rsplit("/", 1)[-1]])

    def test_latest_channel_follows_the_main_build(self):
        answers = {"latest": release("latest", True, commit="abcdef1234567890")}
        self.assertIsNone(self.find(BuildInfo("0.0.0", "latest", "abcdef1234567890"), answers))
        update = self.find(BuildInfo("0.0.0", "latest", "1111111"), answers)
        self.assertEqual(update.asset, self.EXE)
        self.assertIn("abcdef1", update.label)
        self.assertEqual(len(update.sha256), 64)

    def test_latest_build_without_this_os_says_so(self):
        answers = {"latest": release("latest", True, commit="abc", names=("other.zip",))}
        with self.assertRaises(UpdateError):
            self.find(BuildInfo("0.0.0", "latest", "def"), answers)

    def test_stable_channel_picks_the_newest_allowed_tag(self):
        answers = {"releases?per_page=50": [release("latest", True, "x"), release("v1.1.0"), release("v1.3.0"),
                                            release("v2.0.0-beta", True), release("v1.2.0")]}
        self.assertEqual(self.find(BuildInfo("1.1.0", "stable"), answers).tag, "v1.3.0")
        self.assertIsNone(self.find(BuildInfo("1.3.0", "stable"), answers))
        # A pre-release build (v0.x) also gets pre-releases.
        self.assertEqual(self.find(BuildInfo("0.1.0", "stable"), answers).tag, "v2.0.0-beta")

    def test_unknown_channel_cannot_update(self):
        with self.assertRaises(UpdateError):
            self.find(BuildInfo(), {})


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def served(self, name, data, digest=None):
        source = self.folder / "served" / name
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(data)
        return Update("version 9", "v9", name, source.as_uri(), len(data),
                      digest if digest is not None else hashlib.sha256(data).hexdigest(), "page")

    def test_download_is_verified(self):
        seen = []
        path = updater.download(self.served("app.exe", b"new program"), self.folder,
                                progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(path.read_bytes(), b"new program")
        self.assertEqual(seen[-1], (11, 11))
        with self.assertRaises(UpdateError):
            updater.download(self.served("bad.exe", b"tampered", "0" * 64), self.folder)
        self.assertEqual([p.name for p in self.folder.glob(".HackerfabStepper-update-bad*")], [])
        with self.assertRaises(UpdateError):
            updater.download(self.served("nosum.exe", b"x", ""), self.folder)

    def test_exe_swap_keeps_the_old_copy_until_cleanup(self):
        target = self.folder / "HackerfabStepper.exe"
        target.write_bytes(b"old")
        downloaded = updater.download(self.served("HackerfabStepper-Windows.exe", b"new"), self.folder)
        updater.install(downloaded, target)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual((self.folder / "HackerfabStepper.old.exe").read_bytes(), b"old")
        self.assertFalse(downloaded.exists())
        updater.cleanup(target)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["HackerfabStepper.exe", "served"])

    def test_linux_program_is_unpacked_and_executable(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            info = tarfile.TarInfo("HackerfabStepper")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"new"))
        target = self.folder / "HackerfabStepper"
        target.write_bytes(b"old")
        downloaded = updater.download(self.served("HackerfabStepper-Linux-x86_64.tar.gz", buffer.getvalue()),
                                      self.folder)
        updater.install(downloaded, target)
        self.assertEqual(target.read_bytes(), b"new")
        if os.name != "nt":
            self.assertTrue(os.access(target, os.X_OK))
        self.assertTrue((self.folder / "HackerfabStepper.old").exists())

    def test_failed_swap_puts_the_original_back(self):
        target = self.folder / "HackerfabStepper.exe"
        target.write_bytes(b"old")
        downloaded = updater.download(self.served("HackerfabStepper-Windows.exe", b"new"), self.folder)
        real_replace = os.replace

        def flaky(src, dst):
            if Path(src).name.endswith(".new"):
                raise PermissionError("locked")
            return real_replace(src, dst)
        with patch("updater.os.replace", side_effect=flaky), self.assertRaises(UpdateError):
            updater.install(downloaded, target)
        self.assertEqual(target.read_bytes(), b"old")

    def test_automatic_checks_at_most_daily(self):
        self.assertTrue(updater.check_due(self.folder, now=1000.0))
        self.assertFalse(updater.check_due(self.folder, now=1000.0 + 3600))
        self.assertTrue(updater.check_due(self.folder, now=1000.0 + 25 * 3600))

    def test_restart_reuses_settings_file_and_stage_port(self):
        config = self.folder / "config.toml"
        config.write_text("[stage]\n")
        argv = ["HackerfabStepper"] + updater.restart_arguments(str(config), "/dev/ttyACM0")
        self.assertEqual(updater.parse_restart_arguments(argv), (str(config.resolve()), "/dev/ttyACM0"))
        self.assertEqual(updater.parse_restart_arguments(["x"] + updater.restart_arguments(str(config), None))[1],
                         "none")
        self.assertIsNone(updater.parse_restart_arguments(["HackerfabStepper"]))
        # A settings file that is gone: show the setup window as usual.
        missing = ["x"] + updater.restart_arguments(str(self.folder / "gone.toml"), "none")
        self.assertIsNone(updater.parse_restart_arguments(missing))

    def test_updating_window_closes_when_the_new_version_is_on_screen(self):
        started = []

        class Running:
            def poll(self):
                return None

        def fake_relaunch(arguments=None):
            started.append(arguments)
            updater.mark_ready(self.folder)    # the new version shows its window
            return Running()
        with patch("updater.relaunch", side_effect=fake_relaunch):
            try:
                updater.restart("version 9", str(self.folder / "config.toml"), "COM5", home=self.folder)
            except Exception as exc:   # no display
                self.skipTest(str(exc))
        self.assertEqual(started[0][0], updater.AFTER_UPDATE)
        self.assertIn("COM5", started[0])
        self.assertFalse((self.folder / updater.READY_MARKER).exists())

    def test_never_checks_by_itself_from_source(self):
        self.assertFalse(updater.automatic_checks_allowed())


if __name__ == "__main__":
    unittest.main()
