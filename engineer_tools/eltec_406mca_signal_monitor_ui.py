"""
Visual live signal-change monitor for the Eltec 406MCA setup.

This UI watches AIN0 continuously, builds an initial baseline, and then shows
whether the incoming signal has changed. It uses the same wiring as the tester:
    AIN0: sensor or conditioned waveform
    AIN2: blade sync signal

Run:
    python eltec_406mca_signal_monitor_ui.py
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

import numpy as np

# The signal math + LabJack wrapper live in the v1 single-sensor tester
# (tech_app/v1_single_sensor). Add it to the import path.
_V1_TESTER_DIR = Path(__file__).resolve().parents[1] / "tech_app" / "v1_single_sensor"
if str(_V1_TESTER_DIR) not in sys.path:
    sys.path.insert(0, str(_V1_TESTER_DIR))

from eltec_406mca_signal_monitor import (
    DEFAULT_BASELINE_SECONDS,
    DEFAULT_MEAN_CHANGE_THRESHOLD_MV,
    DEFAULT_PP_CHANGE_THRESHOLD_MV,
    DEFAULT_REFRESH_SECONDS,
    DEFAULT_RMS_CHANGE_THRESHOLD_MV,
    DEFAULT_WINDOW_SECONDS,
    ChangeResult,
    SignalStats,
    calculate_stats,
    compare_to_baseline,
    format_frequency,
    make_stream,
    open_csv_writer,
    write_csv_row,
)
from eltec_406mca_tester import (
    DEFAULT_AM502_GAIN,
    DEFAULT_EMITTER_PWM_CHANNEL,
    DEFAULT_EMITTER_PWM_DUTY_CYCLE,
    DEFAULT_EMITTER_PWM_FREQUENCY_HZ,
    DEFAULT_SAMPLE_RATE_HZ,
    DEFAULT_WAVEFORM_INPUT_RANGE_LABEL,
    FILTER_SPECS_MV,
    LABJACK_AIN0_RANGE_OPTIONS,
    LABJACK_T7_PWM_CHANNELS,
    SIM_CASES,
    SYNC_CHANNEL,
    WAVEFORM_CHANNEL,
    calculate_pwm_roll_and_config,
    labjack_ain0_range_from_label,
    labjack_range_offset_warning,
    normalize_pwm_channel,
    probe_labjack_status,
)


MAX_HISTORY_POINTS = 180


@dataclass
class MonitorConfig:
    simulator: bool
    filter_setup: str
    sim_case: str
    sim_change_after: float
    sample_rate: float
    window_seconds: float
    baseline_seconds: float
    refresh_seconds: float
    gain: float
    ain0_range: str
    emitter_pwm: bool
    emitter_pwm_channel: str
    emitter_pwm_hz: float
    emitter_pwm_duty: float
    pp_threshold_mv: float
    mean_threshold_mv: float
    rms_threshold_mv: float
    csv_path: Path | None


@dataclass
class MonitorUpdate:
    elapsed_s: float
    stats: SignalStats
    change: ChangeResult
    waveform_v: np.ndarray
    sync_v: np.ndarray
    sample_rate_hz: float


class SignalMonitorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Eltec 406MCA Live Signal Monitor")
        self.minsize(1160, 840)

        self.worker_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.baseline: SignalStats | None = None
        self.last_update: MonitorUpdate | None = None
        self.history: list[MonitorUpdate] = []
        self.csv_file = None
        self.csv_writer = None
        self.close_requested = False

        self._build_variables()
        self._build_style()
        self._build_layout()
        self._initialize_hardware_status()
        self.after(100, self.process_messages)

    def _build_variables(self) -> None:
        self.status_var = tk.StringVar(value="Ready.")
        self.overall_var = tk.StringVar(value="READY")
        self.pp_var = tk.StringVar(value="--")
        self.pp_delta_var = tk.StringVar(value="Delta --")
        self.mean_var = tk.StringVar(value="--")
        self.mean_delta_var = tk.StringVar(value="Delta --")
        self.rms_var = tk.StringVar(value="--")
        self.rms_delta_var = tk.StringVar(value="Delta --")
        self.freq_var = tk.StringVar(value="--")
        self.reason_var = tk.StringVar(value="No baseline yet.")

        self.simulator_var = tk.BooleanVar(value=False)
        self.filter_var = tk.StringVar(value="-3 filter")
        self.sim_case_var = tk.StringVar(value="Known good")
        self.sim_change_after_var = tk.StringVar(value="8.0")
        self.sample_rate_var = tk.StringVar(value=f"{DEFAULT_SAMPLE_RATE_HZ:g}")
        self.window_seconds_var = tk.StringVar(value=f"{DEFAULT_WINDOW_SECONDS:g}")
        self.baseline_seconds_var = tk.StringVar(value=f"{DEFAULT_BASELINE_SECONDS:g}")
        self.refresh_seconds_var = tk.StringVar(value=f"{DEFAULT_REFRESH_SECONDS:g}")
        self.gain_var = tk.StringVar(value=f"{DEFAULT_AM502_GAIN:g}")
        self.ain0_range_var = tk.StringVar(value=DEFAULT_WAVEFORM_INPUT_RANGE_LABEL)
        self.emitter_pwm_var = tk.BooleanVar(value=False)
        self.emitter_pwm_channel_var = tk.StringVar(value=DEFAULT_EMITTER_PWM_CHANNEL)
        self.emitter_pwm_hz_var = tk.StringVar(value=f"{DEFAULT_EMITTER_PWM_FREQUENCY_HZ:g}")
        self.emitter_pwm_duty_var = tk.StringVar(value=f"{DEFAULT_EMITTER_PWM_DUTY_CYCLE:g}")
        self.pp_threshold_var = tk.StringVar(value=f"{DEFAULT_PP_CHANGE_THRESHOLD_MV:g}")
        self.mean_threshold_var = tk.StringVar(value=f"{DEFAULT_MEAN_CHANGE_THRESHOLD_MV:g}")
        self.rms_threshold_var = tk.StringVar(value=f"{DEFAULT_RMS_CHANGE_THRESHOLD_MV:g}")
        self.csv_path_var = tk.StringVar(value="")

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f3f6f8")
        style.configure("TLabel", background="#f3f6f8", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Muted.TLabel", foreground="#52616f")
        style.configure("Large.TButton", font=("Segoe UI", 12, "bold"), padding=(12, 9))
        style.configure("Control.TLabelframe", background="#f3f6f8")
        style.configure("Control.TLabelframe.Label", background="#f3f6f8", font=("Segoe UI", 10, "bold"))

    def _build_layout(self) -> None:
        self.configure(bg="#f3f6f8")
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        header = ttk.Frame(self, padding=(14, 12, 14, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Eltec 406MCA Live Signal Monitor", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel").grid(row=1, column=0, sticky="w")

        main = ttk.Frame(self, padding=(14, 0, 14, 14))
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        controls = ttk.LabelFrame(main, text="Monitor Controls", style="Control.TLabelframe", padding=12)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        controls.columnconfigure(1, weight=1)

        ttk.Checkbutton(controls, text="Simulator mode", variable=self.simulator_var).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        self._add_combo(controls, 1, "Filter/setup", self.filter_var, list(FILTER_SPECS_MV.keys()))
        self._add_combo(controls, 2, "Simulator case", self.sim_case_var, SIM_CASES)
        self._add_entry(controls, 3, "Sim change after s", self.sim_change_after_var)
        self._add_entry(controls, 4, "Sample rate Hz", self.sample_rate_var)
        self._add_entry(controls, 5, "Window seconds", self.window_seconds_var)
        self._add_entry(controls, 6, "Baseline seconds", self.baseline_seconds_var)
        self._add_entry(controls, 7, "Refresh seconds", self.refresh_seconds_var)
        self._add_entry(controls, 8, "External gain", self.gain_var)
        self._add_combo(controls, 9, "LabJack AIN0 range", self.ain0_range_var, list(LABJACK_AIN0_RANGE_OPTIONS))

        ttk.Checkbutton(controls, text="Emitter PWM output", variable=self.emitter_pwm_var).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(12, 2)
        )
        self._add_combo(controls, 11, "PWM DIO", self.emitter_pwm_channel_var, LABJACK_T7_PWM_CHANNELS)
        self._add_entry(controls, 12, "PWM Hz", self.emitter_pwm_hz_var)
        self._add_entry(controls, 13, "PWM duty %", self.emitter_pwm_duty_var)

        self._add_entry(controls, 14, "P-P threshold mV", self.pp_threshold_var)
        self._add_entry(controls, 15, "Mean threshold mV", self.mean_threshold_var)
        self._add_entry(controls, 16, "RMS threshold mV", self.rms_threshold_var)
        self._add_entry(controls, 17, "CSV log path", self.csv_path_var)

        self.start_button = ttk.Button(
            controls,
            text="Start Monitor",
            command=self.start_monitor,
            style="Large.TButton",
        )
        self.start_button.grid(row=18, column=0, columnspan=2, sticky="ew", pady=(18, 8))

        self.stop_button = ttk.Button(
            controls,
            text="Stop",
            command=self.stop_monitor,
            style="Large.TButton",
            state="disabled",
        )
        self.stop_button.grid(row=19, column=0, columnspan=2, sticky="ew")

        wiring = (
            "Wiring:\n"
            f"{WAVEFORM_CHANNEL} = sensor/waveform signal\n"
            f"{SYNC_CHANNEL} = blade sync\n\n"
            "PWM DIO = MOSFET gate signal\n"
            "Use common ground with the 5 V emitter supply.\n\n"
            "The first baseline is treated as no-change.\n"
            "Live windows are compared to that baseline."
        )
        ttk.Label(controls, text=wiring, justify="left").grid(
            row=20, column=0, columnspan=2, sticky="w", pady=(18, 0)
        )

        results = ttk.Frame(main)
        results.grid(row=0, column=1, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(3, weight=1)

        self.overall_card = tk.Label(
            results,
            textvariable=self.overall_var,
            font=("Segoe UI", 42, "bold"),
            bg="#dce3ea",
            fg="#152238",
            padx=18,
            pady=16,
        )
        self.overall_card.grid(row=0, column=0, sticky="ew")

        cards = ttk.Frame(results)
        cards.grid(row=1, column=0, sticky="ew", pady=(12, 10))
        for col in range(4):
            cards.columnconfigure(col, weight=1)

        self.pp_card = self._metric_card(cards, 0, "Peak-to-peak", self.pp_var, self.pp_delta_var)
        self.mean_card = self._metric_card(cards, 1, "Mean", self.mean_var, self.mean_delta_var)
        self.rms_card = self._metric_card(cards, 2, "AC RMS", self.rms_var, self.rms_delta_var)
        self.freq_card = self._metric_card(cards, 3, "Sync frequency", self.freq_var, None)

        ttk.Label(results, textvariable=self.reason_var, font=("Segoe UI", 11, "bold")).grid(
            row=2, column=0, sticky="w", pady=(0, 6)
        )

        plots = ttk.Frame(results)
        plots.grid(row=3, column=0, sticky="nsew")
        plots.columnconfigure(0, weight=1)
        plots.rowconfigure(1, weight=2)
        plots.rowconfigure(3, weight=1)

        ttk.Label(plots, text="Live waveform", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        self.wave_canvas = tk.Canvas(plots, height=270, bg="#0b1120", highlightthickness=0)
        self.wave_canvas.grid(row=1, column=0, sticky="nsew", pady=(4, 12))
        self.wave_canvas.bind("<Configure>", lambda _event: self.redraw_waveform())

        ttk.Label(plots, text="Change history", font=("Segoe UI", 11, "bold")).grid(row=2, column=0, sticky="w")
        self.history_canvas = tk.Canvas(plots, height=190, bg="#ffffff", highlightthickness=1, highlightbackground="#cbd5e1")
        self.history_canvas.grid(row=3, column=0, sticky="nsew", pady=(4, 0))
        self.history_canvas.bind("<Configure>", lambda _event: self.redraw_history())

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _add_entry(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
        entry = ttk.Entry(parent, textvariable=variable, width=20)
        entry.grid(row=row, column=1, sticky="ew", pady=4)

    def _add_combo(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, values: list[str]) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=24)
        combo.grid(row=row, column=1, sticky="ew", pady=4)

    def _metric_card(
        self,
        parent: ttk.Frame,
        column: int,
        label: str,
        value_var: tk.StringVar,
        delta_var: tk.StringVar | None,
    ) -> tk.Frame:
        frame = tk.Frame(parent, bg="#ffffff", bd=1, relief="solid")
        frame.grid(row=0, column=column, sticky="ew", padx=4)
        tk.Label(frame, text=label, bg="#ffffff", fg="#64748b", font=("Segoe UI", 10, "bold")).pack(pady=(8, 0))
        tk.Label(frame, textvariable=value_var, bg="#ffffff", fg="#111827", font=("Segoe UI", 17, "bold")).pack(
            pady=(2, 0)
        )
        if delta_var is not None:
            tk.Label(frame, textvariable=delta_var, bg="#ffffff", fg="#52616f", font=("Segoe UI", 10)).pack(
                pady=(0, 8)
            )
        else:
            tk.Label(frame, text="", bg="#ffffff", fg="#52616f", font=("Segoe UI", 10)).pack(pady=(0, 8))
        return frame

    def _initialize_hardware_status(self) -> None:
        ok, message = probe_labjack_status()
        self.status_var.set(message)
        if not ok:
            self.simulator_var.set(True)

    def read_config(self) -> MonitorConfig:
        def as_float(name: str, variable: tk.StringVar) -> float:
            try:
                return float(variable.get())
            except ValueError as exc:
                raise ValueError(f"{name} must be a number.") from exc

        config = MonitorConfig(
            simulator=self.simulator_var.get(),
            filter_setup=self.filter_var.get(),
            sim_case=self.sim_case_var.get(),
            sim_change_after=as_float("Sim change after", self.sim_change_after_var),
            sample_rate=as_float("Sample rate", self.sample_rate_var),
            window_seconds=as_float("Window seconds", self.window_seconds_var),
            baseline_seconds=as_float("Baseline seconds", self.baseline_seconds_var),
            refresh_seconds=as_float("Refresh seconds", self.refresh_seconds_var),
            gain=as_float("External gain", self.gain_var),
            ain0_range=self.ain0_range_var.get(),
            emitter_pwm=self.emitter_pwm_var.get(),
            emitter_pwm_channel=normalize_pwm_channel(self.emitter_pwm_channel_var.get()),
            emitter_pwm_hz=as_float("PWM Hz", self.emitter_pwm_hz_var),
            emitter_pwm_duty=as_float("PWM duty", self.emitter_pwm_duty_var),
            pp_threshold_mv=as_float("P-P threshold", self.pp_threshold_var),
            mean_threshold_mv=as_float("Mean threshold", self.mean_threshold_var),
            rms_threshold_mv=as_float("RMS threshold", self.rms_threshold_var),
            csv_path=Path(self.csv_path_var.get().strip()) if self.csv_path_var.get().strip() else None,
        )

        if config.sample_rate <= 0:
            raise ValueError("Sample rate must be positive.")
        if config.window_seconds <= 0:
            raise ValueError("Window seconds must be positive.")
        if config.baseline_seconds <= 0:
            raise ValueError("Baseline seconds must be positive.")
        if config.refresh_seconds < 0:
            raise ValueError("Refresh seconds cannot be negative.")
        if config.gain <= 0:
            raise ValueError("External gain must be positive.")
        if config.pp_threshold_mv < 0 or config.mean_threshold_mv < 0 or config.rms_threshold_mv < 0:
            raise ValueError("Change thresholds cannot be negative.")
        if config.sim_change_after < 0:
            raise ValueError("Sim change after cannot be negative.")
        calculate_pwm_roll_and_config(config.emitter_pwm_hz, config.emitter_pwm_duty)
        labjack_ain0_range_from_label(config.ain0_range)
        return config

    def start_monitor(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            return
        try:
            config = self.read_config()
        except ValueError as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return

        self.baseline = None
        self.last_update = None
        self.history.clear()
        self.stop_event.clear()
        self.set_controls_enabled(False)
        self.set_overall("BASELINE", "#dbeafe", "#1e3a8a")
        if config.emitter_pwm:
            self.reason_var.set(
                f"Building baseline. PWM on {config.emitter_pwm_channel} "
                f"at {config.emitter_pwm_hz:g} Hz, {config.emitter_pwm_duty:g}% duty."
            )
        else:
            self.reason_var.set("Building baseline...")
        self.redraw_waveform()
        self.redraw_history()

        self.worker_thread = threading.Thread(target=self.monitor_worker, args=(config,), daemon=True)
        self.worker_thread.start()

    def stop_monitor(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping after the current read window...")
        self.stop_button.configure(state="disabled")

    def set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for child in self.winfo_children():
            self._set_child_controls(child, state)
        self.start_button.configure(state="normal" if enabled else "disabled")
        self.stop_button.configure(state="disabled" if enabled else "normal")

    def _set_child_controls(self, widget: tk.Widget, state: str) -> None:
        for child in widget.winfo_children():
            if child in (self.start_button, self.stop_button):
                continue
            if isinstance(child, (ttk.Entry, ttk.Combobox, ttk.Checkbutton)):
                try:
                    child.configure(state=state if not isinstance(child, ttk.Combobox) else ("readonly" if state == "normal" else state))
                except tk.TclError:
                    pass
            self._set_child_controls(child, state)

    def monitor_worker(self, config: MonitorConfig) -> None:
        csv_file = None
        try:
            waveform_range_v = labjack_ain0_range_from_label(config.ain0_range)
            range_warning = labjack_range_offset_warning(waveform_range_v, config.ain0_range)
            csv_writer, csv_file = open_csv_writer(config.csv_path)

            if range_warning is not None:
                self.messages.put(("warning", range_warning))

            stream_args = self.config_to_stream_args(config)
            with make_stream(stream_args, waveform_range_v) as stream:
                pwm_text = (
                    f" Emitter PWM on {config.emitter_pwm_channel} at "
                    f"{config.emitter_pwm_hz:g} Hz, {config.emitter_pwm_duty:g}% duty."
                    if config.emitter_pwm
                    else ""
                )
                self.messages.put((
                    "status",
                    f"Streaming at {stream.actual_scan_rate_hz:.2f} Hz. Building baseline...{pwm_text}",
                ))
                waveform, sync, actual_rate = stream.read_window(config.baseline_seconds)
                if self.stop_event.is_set():
                    return

                baseline = calculate_stats(waveform, sync, actual_rate, config.gain)
                self.messages.put(("baseline", baseline))
                for warning in baseline.warnings:
                    self.messages.put(("warning", warning))

                started_at = time.monotonic()
                while not self.stop_event.is_set():
                    waveform, sync, actual_rate = stream.read_window(config.window_seconds)
                    stats = calculate_stats(waveform, sync, actual_rate, config.gain)
                    change = compare_to_baseline(
                        stats,
                        baseline,
                        pp_threshold_mv=config.pp_threshold_mv,
                        mean_threshold_mv=config.mean_threshold_mv,
                        rms_threshold_mv=config.rms_threshold_mv,
                    )
                    elapsed_s = time.monotonic() - started_at
                    update = MonitorUpdate(
                        elapsed_s=elapsed_s,
                        stats=stats,
                        change=change,
                        waveform_v=waveform,
                        sync_v=sync,
                        sample_rate_hz=actual_rate,
                    )
                    write_csv_row(csv_writer, elapsed_s, stats, change)
                    if csv_file is not None:
                        csv_file.flush()
                    self.messages.put(("update", update))
                    for warning in stats.warnings[:2]:
                        self.messages.put(("warning", warning))
                    if config.refresh_seconds > 0:
                        self.stop_event.wait(config.refresh_seconds)
        except Exception as exc:
            self.messages.put(("error", exc))
        finally:
            if csv_file is not None:
                csv_file.close()
            self.messages.put(("stopped", None))

    def config_to_stream_args(self, config: MonitorConfig):
        class Args:
            pass

        args = Args()
        args.simulator = config.simulator
        args.filter_setup = config.filter_setup
        args.sim_case = config.sim_case
        args.sim_change_after = config.sim_change_after
        args.sample_rate = config.sample_rate
        args.gain = config.gain
        args.emitter_pwm = config.emitter_pwm
        args.emitter_pwm_channel = config.emitter_pwm_channel
        args.emitter_pwm_hz = config.emitter_pwm_hz
        args.emitter_pwm_duty = config.emitter_pwm_duty
        return args

    def process_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "warning":
                    self.status_var.set(str(payload))
                elif kind == "baseline":
                    self.on_baseline(payload)
                elif kind == "update":
                    self.on_update(payload)
                elif kind == "error":
                    self.on_worker_error(payload)
                elif kind == "stopped":
                    self.on_worker_stopped()
        except queue.Empty:
            pass
        self.after(100, self.process_messages)

    def on_baseline(self, baseline: SignalStats) -> None:
        self.baseline = baseline
        self.set_overall("MONITORING", "#dbeafe", "#1e3a8a")
        self.pp_var.set(f"{baseline.pp_mv:.2f} mV")
        self.pp_delta_var.set("Baseline")
        self.mean_var.set(f"{baseline.mean_v:.4f} V")
        self.mean_delta_var.set("Baseline")
        self.rms_var.set(f"{baseline.rms_ac_mv:.2f} mV")
        self.rms_delta_var.set("Baseline")
        self.freq_var.set(format_frequency(baseline.frequency_hz))
        self.reason_var.set("Baseline captured. Watching for change.")
        self.status_var.set("Baseline captured.")

    def on_update(self, update: MonitorUpdate) -> None:
        self.last_update = update
        self.history.append(update)
        if len(self.history) > MAX_HISTORY_POINTS:
            self.history = self.history[-MAX_HISTORY_POINTS:]

        stats = update.stats
        change = update.change
        self.pp_var.set(f"{stats.pp_mv:.2f} mV")
        self.pp_delta_var.set(f"{change.delta_pp_mv:+.2f} mV")
        self.mean_var.set(f"{stats.mean_v:.4f} V")
        self.mean_delta_var.set(f"{change.delta_mean_mv:+.2f} mV")
        self.rms_var.set(f"{stats.rms_ac_mv:.2f} mV")
        self.rms_delta_var.set(f"{change.delta_rms_mv:+.2f} mV")
        self.freq_var.set(format_frequency(stats.frequency_hz))

        if change.changed:
            self.set_overall("CHANGED", "#f97316", "#431407")
            self.reason_var.set(", ".join(change.reasons))
            card_bg = "#ffedd5"
        else:
            self.set_overall("STEADY", "#22c55e", "#052e16")
            self.reason_var.set("Within thresholds.")
            card_bg = "#dcfce7"

        self.set_card_color(self.pp_card, "#ffedd5" if abs(change.delta_pp_mv) >= self.get_float(self.pp_threshold_var) else card_bg)
        self.set_card_color(
            self.mean_card,
            "#ffedd5" if abs(change.delta_mean_mv) >= self.get_float(self.mean_threshold_var) else card_bg,
        )
        self.set_card_color(
            self.rms_card,
            "#ffedd5" if abs(change.delta_rms_mv) >= self.get_float(self.rms_threshold_var) else card_bg,
        )
        self.set_card_color(self.freq_card, "#fef9c3" if stats.frequency_hz is None else "#ffffff")
        self.redraw_waveform()
        self.redraw_history()

    def get_float(self, variable: tk.StringVar) -> float:
        try:
            return float(variable.get())
        except ValueError:
            return 0.0

    def on_worker_error(self, exc: object) -> None:
        text = str(exc)
        if "1230" in text or "CLAIMED" in text.upper():
            text = "The T7 is claimed by another LabJack program. Close LJStreamM/Kipling and try again."
        self.status_var.set(text)
        self.set_overall("ERROR", "#ef4444", "#450a0a")
        messagebox.showerror("Monitor problem", text)

    def on_worker_stopped(self) -> None:
        self.set_controls_enabled(True)
        if self.close_requested:
            self.destroy()
            return
        if self.overall_var.get() not in ("ERROR", "READY"):
            self.status_var.set("Stopped.")

    def set_overall(self, text: str, bg: str, fg: str) -> None:
        self.overall_var.set(text)
        self.overall_card.configure(bg=bg, fg=fg)

    def set_card_color(self, card: tk.Frame, bg: str) -> None:
        card.configure(bg=bg)
        for child in card.winfo_children():
            child.configure(bg=bg)

    def redraw_waveform(self) -> None:
        canvas = self.wave_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())

        update = self.last_update
        if update is None or update.waveform_v.size == 0:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Live waveform appears here after the baseline.",
                fill="#cbd5e1",
                font=("Segoe UI", 13),
            )
            return

        waveform = update.waveform_v
        sync = update.sync_v
        n = len(waveform)
        if n < 2:
            return

        idx = np.linspace(0, n - 1, min(n, max(2, width - 20))).astype(int)
        x = idx / max(1, n - 1) * (width - 24) + 12

        wave_min = float(np.min(waveform))
        wave_max = float(np.max(waveform))
        if abs(wave_max - wave_min) < 1e-9:
            wave_min -= 0.5
            wave_max += 0.5

        top = 18
        wave_bottom = int(height * 0.68)
        y = wave_bottom - (waveform[idx] - wave_min) / (wave_max - wave_min) * (wave_bottom - top)
        points = []
        for px, py in zip(x, y):
            points.extend([float(px), float(py)])
        canvas.create_line(points, fill="#38bdf8", width=2)

        zero_y = wave_bottom - (0.0 - wave_min) / (wave_max - wave_min) * (wave_bottom - top)
        if top <= zero_y <= wave_bottom:
            canvas.create_line(12, zero_y, width - 12, zero_y, fill="#334155", dash=(3, 4))

        sync_top = int(height * 0.76)
        sync_bottom = height - 18
        if len(sync) == n:
            sync_min = float(np.min(sync))
            sync_max = float(np.max(sync))
            if abs(sync_max - sync_min) < 1e-9:
                sync_min -= 0.5
                sync_max += 0.5
            sync_y = sync_bottom - (sync[idx] - sync_min) / (sync_max - sync_min) * (sync_bottom - sync_top)
            sync_points = []
            for px, py in zip(x, sync_y):
                sync_points.extend([float(px), float(py)])
            canvas.create_line(sync_points, fill="#facc15", width=1)

        canvas.create_text(12, 12, anchor="nw", text="AIN0 waveform", fill="#38bdf8", font=("Segoe UI", 10, "bold"))
        canvas.create_text(
            12,
            sync_top,
            anchor="nw",
            text="AIN2 sync",
            fill="#facc15",
            font=("Segoe UI", 10, "bold"),
        )
        canvas.create_text(
            width - 12,
            12,
            anchor="ne",
            text=f"{wave_min:.4f} to {wave_max:.4f} V",
            fill="#cbd5e1",
            font=("Segoe UI", 10),
        )

    def redraw_history(self) -> None:
        canvas = self.history_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())

        if not self.history:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Change history appears here while monitoring.",
                fill="#64748b",
                font=("Segoe UI", 12),
            )
            return

        left = 46
        right = width - 16
        top = 18
        bottom = height - 28
        canvas.create_line(left, top, left, bottom, fill="#cbd5e1")
        canvas.create_line(left, bottom, right, bottom, fill="#cbd5e1")

        pp_values = np.asarray([item.change.delta_pp_mv for item in self.history], dtype=float)
        rms_values = np.asarray([item.change.delta_rms_mv for item in self.history], dtype=float)
        limit = max(
            1.0,
            self.get_float(self.pp_threshold_var),
            self.get_float(self.rms_threshold_var),
            float(np.max(np.abs(pp_values))) if pp_values.size else 0.0,
            float(np.max(np.abs(rms_values))) if rms_values.size else 0.0,
        )
        limit *= 1.20
        center_y = (top + bottom) / 2.0

        for value, color in (
            (self.get_float(self.pp_threshold_var), "#fed7aa"),
            (-self.get_float(self.pp_threshold_var), "#fed7aa"),
        ):
            y = center_y - value / limit * ((bottom - top) / 2.0)
            canvas.create_line(left, y, right, y, fill=color, dash=(4, 4))

        canvas.create_line(left, center_y, right, center_y, fill="#94a3b8")
        canvas.create_text(8, top, anchor="nw", text=f"+{limit:.0f}", fill="#64748b", font=("Segoe UI", 9))
        canvas.create_text(8, center_y, anchor="w", text="0", fill="#64748b", font=("Segoe UI", 9))
        canvas.create_text(8, bottom, anchor="sw", text=f"-{limit:.0f}", fill="#64748b", font=("Segoe UI", 9))

        def plot(values: np.ndarray, color: str, width_px: int) -> None:
            if values.size < 2:
                return
            x = np.linspace(left, right, values.size)
            y = center_y - values / limit * ((bottom - top) / 2.0)
            points = []
            for px, py in zip(x, y):
                points.extend([float(px), float(py)])
            canvas.create_line(points, fill=color, width=width_px)

        plot(pp_values, "#0ea5e9", 2)
        plot(rms_values, "#a855f7", 2)
        canvas.create_text(left + 6, top + 4, anchor="nw", text="P-P delta", fill="#0ea5e9", font=("Segoe UI", 10, "bold"))
        canvas.create_text(
            left + 92,
            top + 4,
            anchor="nw",
            text="RMS delta",
            fill="#a855f7",
            font=("Segoe UI", 10, "bold"),
        )

    def on_close(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.close_requested = True
            self.stop_event.set()
            self.status_var.set("Stopping monitor before closing...")
            self.stop_button.configure(state="disabled")
            return
        self.destroy()


def main() -> None:
    app = SignalMonitorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
