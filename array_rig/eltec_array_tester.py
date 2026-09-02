"""
Eltec Array Rig — detector-model selector for the 50-position DAQ rig.

The mirror of ``single_detector_rig/eltec_rig_tester.py`` for the second
rig. One desktop entry point for every detector model the array PCB can
hold. The technician picks the model from a dropdown and presses Start; the
selected model's tester application then runs exactly as qualified (same
flow, limits, CSV format and results folder). The selection is remembered
between sessions.

The per-model applications live in subdirectories of this package (see
``sensor_versions.py`` for the registry and for how to add a new model).
They are launched as independent subprocesses so a crash in one model's app
can never corrupt another's, and so this selector stays trivially small.

Hardware: the ACCES USB-AIO16-64MA DAQ-PACK and the 50-position buffer PCB
(``sensor_versions.REQUIRED_HARDWARE``). There is no firmware to keep in
step: the DAQ loads its own from the driver package at plug-in. The
single-detector rig's selector and this one can run at the same time - they
use different hardware.

Runs on Windows and Xubuntu. Launchers/installers:
    run_eltec_array_tester.cmd / run_eltec_array_tester.sh
    install_windows_launcher.ps1 / install_xubuntu_launcher.sh
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from sensor_versions import (
    DEFAULT_VERSION_KEY,
    REQUIRED_HARDWARE,
    SENSOR_VERSIONS,
    SensorVersion,
    get_version,
)

APP_TITLE = "Eltec Array Rig"
# 0.1 (2026-09-02): first build - 40623 offset + noise on the DAQ array,
# CALIBRATION PENDING.
APP_VERSION = "0.1"
STATE_DIR_NAME = "eltec-array-rig"
STATE_FILE_NAME = "state.json"

PAGE_BG = "#f4f6f8"
CARD_BG = "#ffffff"
ACCENT = "#1f6f43"
MUTED_FG = "#5a6570"
TEXT_FG = "#1c2430"

# The rig PC runs everything full screen, so the selector starts maximized
# (like each model's tester) and its one content block is centered rather
# than stretched: type stays this size at any screen size, the surrounding
# space just grows. CONTENT_WRAP caps the text column so a wide monitor does
# not produce line-long paragraphs.
FONT_TITLE = ("TkDefaultFont", 26, "bold")
FONT_SUBTITLE = ("TkDefaultFont", 13)
FONT_SECTION = ("TkDefaultFont", 13, "bold")
FONT_INPUT = ("TkDefaultFont", 14)
FONT_BODY = ("TkDefaultFont", 12)
FONT_SMALL = ("TkDefaultFont", 11)
FONT_BUTTON = ("TkDefaultFont", 15, "bold")
CONTENT_WRAP = 640
MIN_WINDOW = (640, 520)


# ----------------------------------------------------------------------
# Selection persistence (which model was used last)
# ----------------------------------------------------------------------
def state_file_path() -> Path:
    """Per-user state file, mirroring the launchers' log locations."""

    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / STATE_DIR_NAME / STATE_FILE_NAME


def load_last_version_key(path: Path | None = None) -> str:
    """Return the remembered model key, or the default."""

    state_path = state_file_path() if path is None else path
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        key = state.get("last_sensor_version")
        if isinstance(key, str):
            get_version(key)  # validate: a removed model falls back to default
            return key
    except Exception:
        pass
    return DEFAULT_VERSION_KEY


def save_last_version_key(key: str, path: Path | None = None) -> None:
    """Best-effort persistence; a read-only disk must never block testing."""

    state_path = state_file_path() if path is None else path
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"last_sensor_version": key}, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


# ----------------------------------------------------------------------
# Launch plumbing (kept GUI-free so tests can exercise it headlessly)
# ----------------------------------------------------------------------
def build_launch_command(version: SensorVersion) -> list[str]:
    """Command line that starts one model's tester with this interpreter.

    sys.executable is pythonw.exe when the selector itself was started
    windowless, so the child GUI inherits the no-console behavior.
    """

    return [sys.executable, str(version.app_script_path)]


def launch_version(version: SensorVersion) -> subprocess.Popen:
    script = version.app_script_path
    if not script.is_file():
        raise FileNotFoundError(f"Tester application not found: {script}")
    # cwd = the app's own directory: every per-model app resolves its
    # settings/assets relative to its script location and expects this.
    return subprocess.Popen(build_launch_command(version), cwd=str(version.app_dir_path))


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
class EltecArraySelector(tk.Tk):
    """Small chooser window: model dropdown -> Start."""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.configure(bg=PAGE_BG)
        # Resizable (a fixed-size window cannot be maximized by the window
        # manager) with a floor that keeps the whole block reachable.
        self.resizable(True, True)
        self.minsize(*MIN_WINDOW)
        self._apply_window_icon()

        self.child: subprocess.Popen | None = None
        self.child_version: SensorVersion | None = None

        initial_key = load_last_version_key()
        self.version_var = tk.StringVar(value=get_version(initial_key).display_name)

        self._build_widgets()
        self._on_version_selected()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.start_maximized()

    # ----- window state ----- #
    def start_maximized(self) -> None:
        """Open full screen — technicians should never have to maximize it."""
        try:
            windowing = self.tk.call("tk", "windowingsystem")
        except tk.TclError:
            windowing = ""
        if windowing == "x11":
            self.after(0, self._zoom_x11)
            return
        try:
            self.state("zoomed")
        except tk.TclError:
            self._fill_screen()

    def _zoom_x11(self) -> None:
        try:
            self.attributes("-zoomed", True)
        except tk.TclError:
            self._fill_screen()

    def _fill_screen(self) -> None:
        try:
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        except tk.TclError:
            pass  # cosmetic only; never block startup on window geometry

    # ----- construction ----- #
    def _apply_window_icon(self) -> None:
        icon_png = Path(__file__).resolve().parent / "assets" / "eltec_desktop_icon.png"
        try:
            self._icon_image = tk.PhotoImage(file=str(icon_png))
            self.iconphoto(True, self._icon_image)
        except Exception:
            pass  # icon is cosmetic; never block startup on it

    def _build_widgets(self) -> None:
        # One content block, centered in the window: with the selector
        # maximized, empty grid rows/columns absorb the extra space instead
        # of the card stretching across the whole screen.
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=0)
        self.columnconfigure(2, weight=1)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(1, weight=0)
        self.rowconfigure(2, weight=1)
        content = tk.Frame(self, bg=PAGE_BG)
        content.grid(row=1, column=1, sticky="n", pady=(24, 24))
        content.columnconfigure(0, weight=1)
        self.content = content

        pad = dict(padx=24, pady=(18, 0))

        tk.Label(
            content, text=APP_TITLE, bg=PAGE_BG, fg=TEXT_FG, font=FONT_TITLE,
        ).grid(row=0, column=0, sticky="w", **pad)
        tk.Label(
            content,
            text=f"v{APP_VERSION} · 50-position DAQ array test rig — choose the detector model to test.",
            bg=PAGE_BG, fg=MUTED_FG, font=FONT_SUBTITLE,
            wraplength=CONTENT_WRAP, justify="left",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(4, 0))

        card = tk.Frame(content, bg=CARD_BG, highlightbackground="#d5dbe1",
                        highlightthickness=1)
        card.grid(row=2, column=0, sticky="ew", padx=24, pady=(16, 0))
        card.columnconfigure(0, weight=1)

        tk.Label(
            card, text="Detector model", bg=CARD_BG, fg=TEXT_FG, font=FONT_SECTION,
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 6))

        self.version_combo = ttk.Combobox(
            card,
            textvariable=self.version_var,
            values=[version.display_name for version in SENSOR_VERSIONS],
            state="readonly",
            font=FONT_INPUT,
            width=34,
        )
        self.version_combo.grid(row=1, column=0, sticky="ew", padx=18)
        self.version_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_version_selected())

        self.summary_label = tk.Label(
            card, text="", bg=CARD_BG, fg=TEXT_FG,
            font=FONT_BODY, wraplength=CONTENT_WRAP - 60, justify="left",
        )
        self.summary_label.grid(row=2, column=0, sticky="w", padx=18, pady=(12, 0))

        self.details_label = tk.Label(
            card, text="", bg=CARD_BG, fg=MUTED_FG,
            font=FONT_SMALL, wraplength=CONTENT_WRAP - 60, justify="left",
        )
        self.details_label.grid(row=3, column=0, sticky="w", padx=18, pady=(8, 0))

        self.results_label = tk.Label(
            card, text="", bg=CARD_BG, fg=MUTED_FG,
            font=(FONT_SMALL[0], FONT_SMALL[1], "italic"),
            wraplength=CONTENT_WRAP - 60, justify="left",
        )
        self.results_label.grid(row=4, column=0, sticky="w", padx=18, pady=(10, 16))

        self.start_button = tk.Button(
            content, text="Start tester", command=self.start_selected,
            bg=ACCENT, fg="#ffffff", activebackground="#2c8d59",
            activeforeground="#ffffff", relief="flat",
            font=FONT_BUTTON, padx=32, pady=12, cursor="hand2",
        )
        self.start_button.grid(row=3, column=0, sticky="w", padx=24, pady=(18, 0))

        self.status_var = tk.StringVar(value=f"Rig hardware: {REQUIRED_HARDWARE}")
        tk.Label(
            content, textvariable=self.status_var, bg=PAGE_BG, fg=MUTED_FG,
            font=FONT_SMALL, wraplength=CONTENT_WRAP, justify="left",
        ).grid(row=4, column=0, sticky="w", padx=24, pady=(12, 0))

    # ----- behavior ----- #
    def selected_version(self) -> SensorVersion:
        display = self.version_var.get()
        for version in SENSOR_VERSIONS:
            if version.display_name == display:
                return version
        return get_version(DEFAULT_VERSION_KEY)

    def _on_version_selected(self) -> None:
        version = self.selected_version()
        self.summary_label.configure(text=version.summary)
        self.details_label.configure(text="\n".join(f"•  {line}" for line in version.details))
        self.results_label.configure(text=f"Results: {version.results_note}")
        save_last_version_key(version.key)

    def start_selected(self) -> None:
        if self.child is not None and self.child.poll() is None:
            messagebox.showinfo(
                APP_TITLE,
                f"{self.child_version.display_name} is already running.\n"
                "Close it before starting another model — every model shares "
                "the same DAQ and array PCB.",
            )
            return
        version = self.selected_version()
        try:
            self.child = launch_version(version)
        except Exception as exc:  # surfaced to the operator, never fatal here
            messagebox.showerror(APP_TITLE, f"Could not start the tester:\n{exc}")
            return
        self.child_version = version
        save_last_version_key(version.key)
        self.start_button.configure(state="disabled")
        self.status_var.set(f"{version.display_name} is running… close it to start another model.")
        self.iconify()  # get out of the way; the tester window is the real UI
        threading.Thread(target=self._wait_for_child, daemon=True).start()

    def _wait_for_child(self) -> None:
        child = self.child
        if child is None:
            return
        status = child.wait()
        try:
            self.after(0, lambda: self._on_child_exit(status))
        except (RuntimeError, tk.TclError):
            pass  # selector already closed

    def _on_child_exit(self, status: int) -> None:
        version = self.child_version
        self.child = None
        self.child_version = None
        self.start_button.configure(state="normal")
        self.deiconify()
        # Un-minimizing can drop the zoomed state on some window managers.
        self.start_maximized()
        self.lift()
        if status == 0 or version is None:
            self.status_var.set(f"Rig hardware: {REQUIRED_HARDWARE}")
        else:
            self.status_var.set(
                f"{version.display_name} exited with status {status} — see its "
                "launcher log / console output if this was not intentional."
            )

    def _on_close(self) -> None:
        # A running tester is an independent process and keeps running; only
        # the selector window goes away.
        self.destroy()


def main() -> int:
    app = EltecArraySelector()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
