"""
Eltec Test Rig — unified sensor-version selector.

One desktop entry point for every sensor model the ESP32/ADS1256 rig can
test. The technician picks the sensor version from a dropdown and presses
Start; the selected model's qualified tester application then runs exactly as
it always has (same test flow, thresholds, filter-setup dropdowns, CSV
format, and results folder). The selection is remembered between sessions.

The per-model applications live in subdirectories of this package (see
``sensor_versions.py`` for the registry and for how to add a new model).
They are launched as independent subprocesses so a crash in one model's app
can never corrupt another's, and so this selector stays trivially small.

Firmware: the shared bench board runs the unified ``Arduino/Eltec/Eltec.ino``
v3.0 baseline. Model-specific needs are selected at runtime over serial by
each app (PWM frequency via ``PWM,FREQ``; ADS1256 front end via ``FE,...``).

Runs on Windows and Xubuntu. Launchers/installers:
    run_eltec_rig_tester.cmd / run_eltec_rig_tester.sh
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
    REQUIRED_FIRMWARE,
    SENSOR_VERSIONS,
    SensorVersion,
    get_version,
)

APP_TITLE = "Eltec Test Rig"
STATE_DIR_NAME = "eltec-rig"
STATE_FILE_NAME = "state.json"

PAGE_BG = "#f4f6f8"
CARD_BG = "#ffffff"
ACCENT = "#1f6f43"
MUTED_FG = "#5a6570"
TEXT_FG = "#1c2430"


# ----------------------------------------------------------------------
# Selection persistence (which sensor version was used last)
# ----------------------------------------------------------------------
def state_file_path() -> Path:
    """Per-user state file, mirroring the launchers' log locations."""

    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / STATE_DIR_NAME / STATE_FILE_NAME


def load_last_version_key(path: Path | None = None) -> str:
    """Return the remembered sensor-version key, or the default."""

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
class EltecRigSelector(tk.Tk):
    """Small chooser window: sensor-version dropdown -> Start."""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.configure(bg=PAGE_BG)
        self.resizable(False, False)
        self._apply_window_icon()

        self.child: subprocess.Popen | None = None
        self.child_version: SensorVersion | None = None

        initial_key = load_last_version_key()
        self.version_var = tk.StringVar(value=get_version(initial_key).display_name)

        self._build_widgets()
        self._on_version_selected()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- construction ----- #
    def _apply_window_icon(self) -> None:
        icon_png = Path(__file__).resolve().parent / "assets" / "eltec_desktop_icon.png"
        try:
            self._icon_image = tk.PhotoImage(file=str(icon_png))
            self.iconphoto(True, self._icon_image)
        except Exception:
            pass  # icon is cosmetic; never block startup on it

    def _build_widgets(self) -> None:
        pad = dict(padx=24, pady=(18, 0))

        tk.Label(
            self, text=APP_TITLE, bg=PAGE_BG, fg=TEXT_FG,
            font=("TkDefaultFont", 20, "bold"),
        ).grid(row=0, column=0, sticky="w", **pad)
        tk.Label(
            self,
            text="ESP32/ADS1256 sensor test rig — choose the sensor version to test.",
            bg=PAGE_BG, fg=MUTED_FG, font=("TkDefaultFont", 11),
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(2, 0))

        card = tk.Frame(self, bg=CARD_BG, highlightbackground="#d5dbe1",
                        highlightthickness=1)
        card.grid(row=2, column=0, sticky="ew", padx=24, pady=(14, 0))
        card.columnconfigure(0, weight=1)

        tk.Label(
            card, text="Sensor version", bg=CARD_BG, fg=TEXT_FG,
            font=("TkDefaultFont", 12, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 4))

        self.version_combo = ttk.Combobox(
            card,
            textvariable=self.version_var,
            values=[version.display_name for version in SENSOR_VERSIONS],
            state="readonly",
            font=("TkDefaultFont", 12),
            width=34,
        )
        self.version_combo.grid(row=1, column=0, sticky="ew", padx=16)
        self.version_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_version_selected())

        self.summary_label = tk.Label(
            card, text="", bg=CARD_BG, fg=TEXT_FG,
            font=("TkDefaultFont", 11), wraplength=460, justify="left",
        )
        self.summary_label.grid(row=2, column=0, sticky="w", padx=16, pady=(10, 0))

        self.details_label = tk.Label(
            card, text="", bg=CARD_BG, fg=MUTED_FG,
            font=("TkDefaultFont", 10), wraplength=460, justify="left",
        )
        self.details_label.grid(row=3, column=0, sticky="w", padx=16, pady=(6, 0))

        self.results_label = tk.Label(
            card, text="", bg=CARD_BG, fg=MUTED_FG,
            font=("TkDefaultFont", 10, "italic"), wraplength=460, justify="left",
        )
        self.results_label.grid(row=4, column=0, sticky="w", padx=16, pady=(8, 14))

        self.start_button = tk.Button(
            self, text="Start tester", command=self.start_selected,
            bg=ACCENT, fg="#ffffff", activebackground="#2c8d59",
            activeforeground="#ffffff", relief="flat",
            font=("TkDefaultFont", 13, "bold"), padx=26, pady=8, cursor="hand2",
        )
        self.start_button.grid(row=3, column=0, sticky="w", padx=24, pady=(16, 0))

        self.status_var = tk.StringVar(
            value=f"Rig firmware: {REQUIRED_FIRMWARE}"
        )
        tk.Label(
            self, textvariable=self.status_var, bg=PAGE_BG, fg=MUTED_FG,
            font=("TkDefaultFont", 10), wraplength=500, justify="left",
        ).grid(row=4, column=0, sticky="w", padx=24, pady=(10, 18))

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
                "Close it before starting another tester — both models share "
                "the same rig and serial port.",
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
        self.status_var.set(f"{version.display_name} is running… close it to start another version.")
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
        self.lift()
        if status == 0 or version is None:
            self.status_var.set(f"Rig firmware: {REQUIRED_FIRMWARE}")
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
    app = EltecRigSelector()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
