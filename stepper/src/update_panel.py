"""Settings → Updates: check for, download and install a newer version of the app (updater.py).

GitHub is only asked from a background thread; results come back through a queue polled on the
Tk thread. The standalone app also checks by itself at most once a day and says so in the status
line when there is something new. A copy run from source updates with git pull instead.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox

import ttkbootstrap as ttk

import updater
from ui_theme import px

AUTO_CHECK_DELAY_MS = 8000   # let the camera and stage connect first


class UpdatePanel:
    def __init__(self, page, app, config):
        self.app, self.page = app, page
        self.settings = config.setdefault("app", {})
        self.results: queue.Queue = queue.Queue()
        self.update: updater.Update | None = None
        self.busy = False
        self.cancel = False
        self.source = updater.source_checkout()
        self.target = updater.install_target()
        self.build = updater.build_info()

        row = 0
        ttk.Label(page, text="Updates", style="Section.TLabel").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Label(page, text="This copy", style="Muted.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, px(14)))
        ttk.Label(page, text=self._describe_copy(), wraplength=px(560), justify="left").grid(
            row=row, column=1, sticky="w")
        row += 1
        self.status = tk.StringVar(value="")
        ttk.Label(page, textvariable=self.status, wraplength=px(560), justify="left").grid(
            row=row, column=1, sticky="w", pady=(px(10), 0))
        row += 1
        self.progress = ttk.Progressbar(page, mode="determinate", maximum=100, length=px(360))
        self.progress.grid(row=row, column=1, sticky="w", pady=(px(6), 0))
        self.progress.grid_remove()
        row += 1
        buttons = ttk.Frame(page)
        buttons.grid(row=row, column=1, sticky="w", pady=(px(8), 0))
        self.check_button = ttk.Button(buttons, text="Check for updates", bootstyle="dark",
                                       command=lambda: self.check(quiet=False))
        self.check_button.pack(side="left")
        install_text = "Update (git pull)" if self.source else "Install and restart"
        self.install_button = ttk.Button(buttons, text=install_text, bootstyle="primary", command=self.install,
                                         state="disabled")
        self.install_button.pack(side="left", padx=(px(8), 0))
        self.notes_button = ttk.Button(buttons, text="What's new", bootstyle="link", command=self.open_notes)
        self.notes_button.pack(side="left", padx=(px(8), 0))
        row += 1
        self.auto = tk.BooleanVar(value=self.settings.get("check-for-updates", True) is not False)
        auto = ttk.Checkbutton(page, text="Check for updates when the app starts (at most once a day)",
                               variable=self.auto, bootstyle="round-toggle",
                               command=lambda: self.settings.__setitem__("check-for-updates", self.auto.get()))
        auto.grid(row=row, column=1, sticky="w", pady=(px(14), 0))
        if self.source or not self.build.channel:
            auto.configure(state="disabled")
        row += 1
        ttk.Label(page, text="Updating replaces only the program. Settings, logs, chip records and snapshots stay as "
                             "they are. Every download is checked against the checksum GitHub publishes for it "
                             "before anything is replaced.", style="Small.TLabel", wraplength=px(560),
                  justify="left").grid(row=row, column=1, sticky="w", pady=(px(10), 0))

        if self.source:
            self.status.set("Click Check for updates to compare with GitHub.")
        elif self.target is None or not self.build.channel:
            self.check_button.configure(state="disabled")
            self.status.set(f"This copy cannot update itself. Download the newest app from {updater.RELEASES_PAGE}.")
        else:
            self.status.set("Click Check for updates to see whether there is a newer version.")
        self._poll()
        if self.auto.get() and updater.automatic_checks_allowed() and updater.check_due(Path.cwd()):
            page.after(AUTO_CHECK_DELAY_MS, lambda: self.check(quiet=True))

    # ------------------------------------------------------------------ text
    def _describe_copy(self) -> str:
        if self.source:
            return f"Run from source: the git checkout in {self.source}. Updates with git pull."
        where = f" · {self.target}" if self.target else ""
        channel = {"stable": " · gets new releases", "latest": " · gets every new build of main"}.get(
            self.build.channel, "")
        return f"{self.build.label.capitalize()}{channel}{where}"

    def open_notes(self):
        webbrowser.open(self.update.page if self.update else updater.RELEASES_PAGE)

    # ------------------------------------------------------------------ actions
    def _run(self, work):
        def runner():
            try:
                work()
            except updater.UpdateError as exc:
                self.results.put(("error", str(exc)))
            except Exception as exc:   # never let a background thread die silently
                self.results.put(("error", f"Unexpected problem: {exc}"))
        self.busy = True
        self._buttons()
        threading.Thread(target=runner, daemon=True).start()

    def check(self, quiet: bool):
        if self.busy:
            return
        self.quiet = quiet
        if not quiet:
            self.status.set("Checking GitHub…")
        if self.source:
            self._run(lambda: self.results.put(("git", updater.git_status(self.source))))
        else:
            self._run(lambda: self.results.put(("found", updater.find_update(self.build))))

    def install(self):
        if self.busy:
            return
        running = self.app.dispatcher.busy_text()
        if running or self.app.dispatcher.patterning_busy:
            messagebox.showinfo("Update", f"Wait until the {running or 'exposure'} has finished, then install the "
                                          "update.", parent=self.page)
            return
        if self.source:
            self.status.set("Running git pull…")
            self._run(lambda: self.results.put(("pulled", updater.git_pull(self.source))))
            return
        update, target = self.update, self.target
        if update is None or target is None:
            return
        if not messagebox.askokcancel("Install update",
                                      f"Download {update.label} ({update.size / 1e6:.0f} MB) and install it?\n\n"
                                      "The app restarts when it is installed. Your settings are saved first.",
                                      parent=self.page):
            return
        self.cancel = False
        self.status.set(f"Downloading {update.label}…")
        self.progress.configure(value=0)
        self.progress.grid()

        def work():
            path = updater.download(update, target.parent,
                                    progress=lambda done, total: self.results.put(("progress", (done, total))),
                                    cancelled=lambda: self.cancel)
            self.results.put(("status", "Installing…"))
            updater.install(path, target)
            self.results.put(("installed", update))
        self._run(work)

    # ------------------------------------------------------------------ results (Tk thread)
    def _buttons(self):
        self.check_button.configure(state="disabled" if self.busy else "normal")
        ready = self.update is not None or getattr(self, "_behind", 0)
        self.install_button.configure(state="normal" if ready and not self.busy else "disabled")

    def _poll(self):
        try:
            while True:
                kind, value = self.results.get_nowait()
                self._handle(kind, value)
        except queue.Empty:
            pass
        try:
            self.page.after(200, self._poll)
        except tk.TclError:
            pass   # window closed

    def _handle(self, kind, value):
        quiet = getattr(self, "quiet", False)
        if kind == "progress":
            done, total = value
            if total:
                self.progress.configure(value=100 * done / total)
                self.status.set(f"Downloading {self.update.label}… {done / 1e6:.0f} of {total / 1e6:.0f} MB")
            return
        if kind == "status":
            self.status.set(value)
            return
        self.busy = False
        if kind == "found":
            self.update = value
            if value is None:
                self.status.set(f"Up to date ({self.build.label}).")
            else:
                self.status.set(f"{value.label} is available ({value.size / 1e6:.0f} MB). Click Install and restart.")
                if quiet:
                    self.app.toast.show(f"Update available: {value.label}. Install it in Settings → Updates.", "ok",
                                        seconds=20)
        elif kind == "git":
            behind, branch = value
            self._behind = behind
            self.status.set(f"{behind} new commit(s) on GitHub for {branch}. Click Update (git pull)." if behind
                            else f"Up to date with GitHub ({branch}).")
        elif kind == "pulled":
            self._behind = 0
            self.status.set("Updated. Close the app and start it again with run.sh / run.bat; it installs any new "
                            "dependencies.")
        elif kind == "installed":
            self.progress.grid_remove()
            self.update = None
            self.status.set(f"{value.label} is installed. The app restarts with it now (or the next time you open it).")
            self.app.restart_after_close = value.label
            self.app.request_close()   # saves settings, turns the LEDs off, closes the stage and camera
        elif kind == "error":
            self.progress.grid_remove()
            if quiet:
                print(f"Automatic update check: {value}")
            else:
                self.status.set(value)
        self._buttons()
