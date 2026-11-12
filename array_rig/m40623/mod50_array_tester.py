"""Operator window for the current M25/HUB 50-position array hardware."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import webbrowser

import mod50_hardware as hw
from mod50_check import TrayCheck, results_root


class ArrayWindow:
    def __init__(self, root, *, simulate=False, output_root=None, worker_factory=TrayCheck):
        self.root, self.worker_factory = root, worker_factory
        self.output_root = output_root
        self.worker, self.report_path = None, None
        self.closing = False
        self.restart_required = False
        root.title("Eltec Array Rig | M25/HUB · 50-piece check")
        width = min(1220, root.winfo_screenwidth() - 60)
        height = min(860, root.winfo_screenheight() - 80)
        root.geometry(f"{width}x{height}")
        root.minsize(min(980, width), min(680, height))
        root.configure(bg="#f1f5fa")
        self.lot = tk.StringVar()
        self.operator = tk.StringVar()
        self.tray = tk.StringVar(value="1")
        self.source = tk.StringVar(value="Generated demo" if simulate else "Actual detectors")
        self.ready = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Enter the lot and technician, then start the check.")
        self.phase = tk.StringVar(value="Ready · 10 s offset + 100 s noise, plus settling/warm-up")
        self.source_note = tk.StringVar()
        self.population = tk.StringVar(value="50 detectors selected")
        self.selected = {position: tk.BooleanVar(value=True) for position in hw.POSITIONS}
        self.widgets = []

        tk.Label(root, text="ELTEC  |  50-piece array check", bg="#173b67", fg="white",
                 font=("Segoe UI", 23, "bold"), anchor="w", padx=22, pady=17).pack(fill="x")
        top = ttk.Frame(root, padding=(20, 12))
        top.pack(fill="x")
        for col, label, variable, width in ((0, "Lot / batch", self.lot, 20),
                                             (2, "Technician", self.operator, 24), (4, "Tray", self.tray, 6)):
            ttk.Label(top, text=label).grid(row=0, column=col, sticky="w", padx=(0, 8))
            entry = ttk.Entry(top, textvariable=variable, width=width)
            entry.grid(row=0, column=col + 1, padx=(0, 22))
            self.widgets.append(entry)
        source = ttk.Combobox(top, textvariable=self.source, values=("Actual detectors", "Generated demo"),
                              state="readonly", width=20)
        source.grid(row=0, column=6)
        source.bind("<<ComboboxSelected>>", self.source_changed)
        self.source_widget = source
        ttk.Label(root, textvariable=self.source_note, padding=(20, 0)).pack(fill="x")
        ttk.Label(root, text="Automatic sequence: settle DC → DC 10 s → switch → warm up 90 s → noise 100 s → return to DC",
                  padding=(20, 10), font=("Segoe UI", 11)).pack(fill="x")

        occupied = ttk.LabelFrame(root, text="Installed detectors · position labels follow the holder wiring map", padding=8)
        occupied.pack(fill="x", padx=20)
        for i, position in enumerate(hw.POSITIONS):
            check = ttk.Checkbutton(occupied, text=position, variable=self.selected[position], command=self.population_changed)
            check.grid(row=i // 10, column=i % 10, sticky="w", padx=12, pady=2)
            self.widgets.append(check)
        for col in range(10):
            occupied.columnconfigure(col, weight=1)
        population_bar = ttk.Frame(root, padding=(20, 5))
        population_bar.pack(fill="x")
        all_button = ttk.Button(population_bar, text="Select all 50", command=lambda: self.select_all(True))
        all_button.pack(side="left")
        none_button = ttk.Button(population_bar, text="Clear selection", command=lambda: self.select_all(False))
        none_button.pack(side="left", padx=8)
        self.widgets.extend((all_button, none_button))
        ttk.Label(population_bar, textvariable=self.population).pack(side="left", padx=10)

        self.ready_check = ttk.Checkbutton(root, variable=self.ready, command=self.controls,
            text="Physical RUN is active; cooling and supplies are checked; the HUB owns the DAQ control port; other DAQ apps are stopped.")
        self.ready_check.pack(anchor="w", padx=20, pady=5)
        actions = ttk.Frame(root, padding=(20, 8))
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="Start 50-piece check", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Stop check", command=self.stop)
        self.stop_button.pack(side="left", padx=8)
        self.next_button = ttk.Button(actions, text="Next tray", command=self.next_tray)
        self.next_button.pack(side="left")
        self.open_button = ttk.Button(actions, text="Open report", command=self.open_report)
        self.open_button.pack(side="right")
        self.progress = ttk.Progressbar(root, maximum=1.)
        self.progress.pack(fill="x", padx=20, pady=(0, 7))
        ttk.Label(root, textvariable=self.phase, font=("Segoe UI", 13, "bold"), padding=(20, 0)).pack(fill="x")
        ttk.Label(root, textvariable=self.status, wraplength=1120, padding=(20, 6)).pack(fill="x")

        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True, padx=20, pady=(5, 8))
        columns = ("position", "path", "dc", "dc_check", "noise3", "noise100", "legacy", "result")
        self.table = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        for key, label, width in (("position", "Position", 70), ("path", "Card / DAQ CH", 120),
                                  ("dc", "DC (V)", 85), ("dc_check", "DC check · provisional", 205),
                                  ("noise3", "3 Hz/Q3 (µV RMS)", 140), ("noise100", "1–100 Hz (µV RMS)", 150),
                                  ("legacy", "Legacy estimate (mV)", 155),
                                  ("result", "Review", 220)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=60, stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.tag_configure("check", background="#fff0d1")
        self.table.tag_configure("empty", foreground="#7b8796")
        ttk.Label(root, text="RMS is at the DAQ output. Legacy estimate uses nominal gain, filtering, rectification, smoothing and ideal hold.\n"
                  "Noise pass/fail is disabled until comparison with the old fixture; REVIEW is not PASS.\n"
                  "Supply supervision and physical RUN/STOP remain manual. Software Stop does not remove hardware power.",
                  padding=(20, 8), wraplength=1150).pack(fill="x", before=frame)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.source_changed()
        self.reset_table()
        self.poll_job = root.after(100, self.poll)

    def is_busy(self):
        return self.worker is not None and self.worker.is_alive()

    def source_changed(self, event=None):
        if self.is_busy():
            return
        self.ready.set(False)
        demo = self.source.get() == "Generated demo"
        self.source_note.set("SIMULATION · generated data, accelerated waits, separate result files" if demo else
                             "Actual M25/HUB hardware · 1 kHz · both cards checked throughout the run")
        self.report_path = None
        self.reset_table()
        self.controls()

    def population_changed(self):
        count = sum(variable.get() for variable in self.selected.values())
        self.population.set(f"{count} detector{'s' if count != 1 else ''} selected")
        self.start_button.configure(text=f"Start {count}-piece check")
        self.controls()

    def select_all(self, value):
        if self.is_busy():
            return
        for variable in self.selected.values():
            variable.set(value)
        self.population_changed()

    def controls(self):
        busy = self.is_busy() or self.closing
        demo = self.source.get() == "Generated demo"
        for widget in self.widgets:
            widget.configure(state="disabled" if busy else "normal")
        self.source_widget.configure(state="disabled" if busy else "readonly")
        self.ready_check.configure(state="disabled" if busy or demo else "normal")
        allowed = not busy and not self.restart_required and (demo or self.ready.get()) and any(v.get() for v in self.selected.values())
        self.start_button.configure(state="normal" if allowed else "disabled")
        self.stop_button.configure(state="normal" if busy and not self.closing else "disabled")
        self.next_button.configure(state="disabled" if busy else "normal")
        self.open_button.configure(state="normal" if self.report_path is not None and self.report_path.exists() else "disabled")

    def reset_table(self):
        self.table.delete(*self.table.get_children())
        for row in hw.CHANNEL_MAP:
            self.table.insert("", "end", iid=row["position"], values=(row["position"],
                f"{row['card']} / {row['physical_daq_channel']}", "—", "Not measured", "—", "—", "—", "Waiting"))

    def start(self):
        if self.is_busy() or self.closing or self.restart_required:
            return
        simulate = self.source.get() == "Generated demo"
        if not simulate and not self.ready.get():
            return
        try:
            self.worker = self.worker_factory(lot=self.lot.get(), operator=self.operator.get(), tray=int(self.tray.get()),
                installed_positions=[p for p, v in self.selected.items() if v.get()], simulate=simulate,
                output_root=self.output_root)
        except (ValueError, TypeError) as exc:
            self.status.set(str(exc))
            return
        self.report_path = None
        self.reset_table()
        self.progress.configure(value=0.)
        self.phase.set("Starting")
        self.status.set("Connecting and checking the hardware…" if not simulate else "Running generated demo…")
        self.worker.start()
        self.controls()

    def show_results(self, result):
        def number(value, places):
            return "—" if value is None else f"{value:.{places}f}"
        for row in result["rows"]:
            label = ("Not installed" if not row["installed"] else
                     "Incomplete" if not result["data_complete"] else
                     "CHECK · " + "; ".join(row["flags"]) if row["flags"] else "REVIEW · no noise limit")
            self.table.item(row["position"], values=(row["position"], f"{row['card']} / {row['physical_daq_channel']}",
                number(row["dc_v"], 5), row["dc_check"].replace("_", " ").lower(),
                number(row["daq_3hz_q3_rms_uv"], 3), number(row["daq_1_100_hz_rms_uv"], 3),
                number(row.get("legacy_equivalent_mv"), 2), label),
                tags=("empty" if not row["installed"] else "check" if row["flags"] or not result["data_complete"] else "",))

    def poll(self):
        if self.worker is not None:
            state = self.worker.status()
            self.phase.set(state["phase"] + (" · SIMULATION" if self.worker.simulate else ""))
            self.status.set(state["message"])
            self.progress.configure(value=state["progress"])
            if self.worker.finished.is_set() and not self.worker.is_alive():
                self.show_results(self.worker.result)
                self.restart_required = self.worker.result.get("restart_required", False)
                if self.worker.directory:
                    self.report_path = self.worker.directory / "report.html"
                self.worker = None
                self.controls()
        if self.closing and not self.is_busy():
            self.root.destroy()
            return
        self.poll_job = self.root.after(100, self.poll)

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.status.set("Stopping and saving available data…")

    def next_tray(self):
        if self.is_busy():
            return
        try:
            self.tray.set(str(int(self.tray.get()) + 1))
        except ValueError:
            self.tray.set("1")
        self.ready.set(False)
        self.report_path = None
        self.reset_table()
        self.progress.configure(value=0.)
        self.phase.set("Ready · next tray")
        self.status.set("Load the next tray with hardware power removed, then restore RUN and confirm readiness.")
        self.controls()

    def close(self):
        self.closing = True
        self.stop()
        self.controls()

    def open_report(self):
        if self.report_path and self.report_path.exists():
            webbrowser.open(self.report_path.resolve().as_uri())


def main(argv=None):
    # The demo-style dashboard uses the same acquisition worker locally.
    # Keep ArrayWindow available for existing engineering callers.
    import mod50_web
    return mod50_web.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
