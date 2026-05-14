"""
Eltec 406MCA single-sensor tester.

Reads a 10 Hz pyroelectric detector waveform from a LabJack T7-Pro, checks
sensitivity, polarity, and DC offset, then logs a simple CSV result.

Default wiring:
    AIN0: AM502 amplifier output waveform
    AIN1: DC offset voltage
    AIN2: blade sync signal

Run:
    python eltec_406mca_tester.py
"""

from __future__ import annotations

import csv
import math
import random
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import numpy as np

try:
    from labjack import ljm
except Exception:  # pragma: no cover - hardware library may not exist on dev PCs
    ljm = None


MODEL_NAME = "406MCA"
WAVEFORM_CHANNEL = "AIN0"
OFFSET_CHANNEL = "AIN1"
SYNC_CHANNEL = "AIN2"

EXPECTED_FREQUENCY_HZ = 10.0
FREQUENCY_TOLERANCE_HZ = 0.1
OFFSET_MIN_V = 0.3
OFFSET_MAX_V = 1.2
DEFAULT_SAMPLE_RATE_HZ = 1000.0
DEFAULT_MAX_CAPTURE_CYCLES = 80
DEFAULT_STABILITY_TOLERANCE = 0.10
DEFAULT_STABILITY_WINDOW_CYCLES = 3
DEFAULT_AM502_GAIN = 100.0
POSITIVE_POLARITY = "POSITIVE"
NEGATIVE_POLARITY = "NEGATIVE"
UNKNOWN_POLARITY = "UNKNOWN"

FILTER_SPECS_MV = {
    "-3 filter": 25.0,
    "-27 filter": 25.0,
    "-266 filter": 30.9,
    "-273 filter + blackened tube": 2.3,
    "-284 filter + extra -6 + blackened tube": 4.0,
}

SIM_CASES = [
    "Random good sensor",
    "Known good",
    "Low sensitivity",
    "Wrong polarity",
    "Low offset",
    "High offset",
]

CSV_FIELDS = [
    "timestamp",
    "sensor_id",
    "model",
    "filter_setup",
    "offset_v",
    "sensitivity_mv",
    "polarity",
    "pass_fail",
    "fail_reasons",
]


@dataclass
class WaveformMetrics:
    sensitivity_mv: float
    sensitivity_amplified_mv: float
    polarity: str
    measured_frequency_hz: float | None
    cycles_used: int
    cycle_pp_mv: list[float] = field(default_factory=list)
    all_cycle_pp_mv: list[float] = field(default_factory=list)
    stabilized: bool = False
    stability_change_pct: float | None = None
    stabilization_cycle: int | None = None
    warnings: list[str] = field(default_factory=list)
    edges: list[int] = field(default_factory=list)
    waveform_v: np.ndarray = field(default_factory=lambda: np.array([], dtype=float), repr=False)
    sync_v: np.ndarray = field(default_factory=lambda: np.array([], dtype=float), repr=False)
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ


@dataclass
class FinalResult:
    passed: bool
    offset_v: float | None
    sensitivity_mv: float | None
    polarity: str
    fail_reasons: list[str]
    warnings: list[str]
    waveform_metrics: WaveformMetrics | None = None


def default_results_path() -> Path:
    docs = Path.home() / "Documents" / "Eltec_406MCA_Test_Results"
    return docs / "406mca_results.csv"


def find_sync_edges(
    sync_v: np.ndarray,
    sample_rate_hz: float,
    expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
    edge: str = "Rising",
) -> tuple[list[int], float | None, list[str]]:
    warnings: list[str] = []
    sync_v = np.asarray(sync_v, dtype=float)
    if sync_v.size < 3:
        return [], None, ["Sync signal is too short to detect edges."]

    sync_min = float(np.min(sync_v))
    sync_max = float(np.max(sync_v))
    sync_span = sync_max - sync_min
    if sync_span < 0.5:
        return [], None, ["Blade sync signal is weak or disconnected."]

    threshold = sync_min + 0.5 * sync_span
    above = sync_v >= threshold

    if edge == "Falling":
        crossings = np.flatnonzero(above[:-1] & ~above[1:]) + 1
    else:
        crossings = np.flatnonzero(~above[:-1] & above[1:]) + 1

    min_spacing = max(1, int(0.45 * sample_rate_hz / expected_frequency_hz))
    edges: list[int] = []
    for idx in crossings:
        if not edges or idx - edges[-1] >= min_spacing:
            edges.append(int(idx))

    measured_frequency_hz: float | None = None
    if len(edges) >= 2:
        periods = np.diff(edges) / sample_rate_hz
        mean_period = float(np.mean(periods))
        if mean_period > 0:
            measured_frequency_hz = 1.0 / mean_period
            frequency_error = abs(measured_frequency_hz - expected_frequency_hz)
            if frequency_error > FREQUENCY_TOLERANCE_HZ:
                warnings.append(
                    f"Blade sync frequency is {measured_frequency_hz:.3f} Hz; "
                    f"expected {expected_frequency_hz:.1f} +/- {FREQUENCY_TOLERANCE_HZ:.1f} Hz."
                )
    else:
        warnings.append("Not enough blade sync edges were detected.")

    return edges, measured_frequency_hz, warnings


def cycle_segments_from_edges(
    edges: list[int],
    settle_cycles: int = 0,
    max_cycles: int | None = None,
) -> list[tuple[int, int]]:
    if len(edges) < 2:
        return []
    segments = [(edges[i], edges[i + 1]) for i in range(settle_cycles, len(edges) - 1)]
    if max_cycles is not None:
        segments = segments[:max_cycles]
    return [(start, end) for start, end in segments if end - start >= 8]


def fallback_cycle_segments(
    sample_count: int,
    sample_rate_hz: float,
    expected_frequency_hz: float,
    settle_cycles: int = 0,
    max_cycles: int | None = None,
) -> list[tuple[int, int]]:
    period_samples = max(8, int(round(sample_rate_hz / expected_frequency_hz)))
    start = settle_cycles * period_samples
    segments: list[tuple[int, int]] = []
    while start + period_samples <= sample_count:
        segments.append((start, start + period_samples))
        if max_cycles is not None and len(segments) >= max_cycles:
            break
        start += period_samples
    return segments


def cycle_peak_to_peak_values(
    waveform_v: np.ndarray,
    segments: list[tuple[int, int]],
) -> list[float]:
    cycle_pp_v: list[float] = []
    for start, end in segments:
        cycle = waveform_v[start:end]
        if cycle.size >= 8:
            cycle_pp_v.append(float(np.max(cycle) - np.min(cycle)))
    return cycle_pp_v


def select_stable_cycle_window(
    segments: list[tuple[int, int]],
    cycle_pp_v: list[float],
    tolerance: float = DEFAULT_STABILITY_TOLERANCE,
    window_cycles: int = DEFAULT_STABILITY_WINDOW_CYCLES,
) -> tuple[list[tuple[int, int]], list[float], bool, float | None, int | None]:
    if not segments or not cycle_pp_v:
        return [], [], False, None, None

    window_cycles = max(1, int(window_cycles))
    available = min(len(segments), len(cycle_pp_v))
    segments = segments[:available]
    cycle_pp_v = cycle_pp_v[:available]

    if available < window_cycles:
        return segments, cycle_pp_v, False, None, None

    if available < window_cycles * 2:
        start = available - window_cycles
        return segments[start:], cycle_pp_v[start:], False, None, start

    last_change_fraction: float | None = None
    for end in range(window_cycles * 2, available + 1):
        previous_values = cycle_pp_v[end - (window_cycles * 2) : end - window_cycles]
        current_values = cycle_pp_v[end - window_cycles : end]
        previous_average = float(np.mean(previous_values))
        current_average = float(np.mean(current_values))
        reference = max(abs(previous_average), abs(current_average), 1e-9)
        change_fraction = abs(current_average - previous_average) / reference
        last_change_fraction = change_fraction

        if change_fraction <= tolerance:
            start = end - window_cycles
            return segments[start:end], cycle_pp_v[start:end], True, change_fraction * 100.0, start

    start = available - window_cycles
    return segments[start:], cycle_pp_v[start:], False, (
        None if last_change_fraction is None else last_change_fraction * 100.0
    ), start


def estimate_polarity(
    waveform_v: np.ndarray,
    segments: list[tuple[int, int]],
    cycle_pp_v: list[float],
) -> str:
    if not segments or not cycle_pp_v:
        return UNKNOWN_POLARITY

    pp_reference = float(np.median(cycle_pp_v))
    if pp_reference <= 0:
        return UNKNOWN_POLARITY

    deltas = []
    for start, end in segments:
        length = end - start
        base_idx = min(end - 1, start + max(1, int(0.05 * length)))
        probe_idx = min(end - 1, start + max(2, int(0.25 * length)))
        deltas.append(float(waveform_v[probe_idx] - waveform_v[base_idx]))

    mean_delta = float(np.mean(deltas))
    if abs(mean_delta) < 0.10 * pp_reference:
        return UNKNOWN_POLARITY
    return POSITIVE_POLARITY if mean_delta > 0 else NEGATIVE_POLARITY


def analyze_waveform(
    waveform_v: np.ndarray,
    sync_v: np.ndarray,
    sample_rate_hz: float,
    am502_gain: float,
    sync_edge: str = "Rising",
    expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
    stability_tolerance: float = DEFAULT_STABILITY_TOLERANCE,
    stability_window_cycles: int = DEFAULT_STABILITY_WINDOW_CYCLES,
) -> WaveformMetrics:
    waveform_v = np.asarray(waveform_v, dtype=float)
    sync_v = np.asarray(sync_v, dtype=float)
    warnings: list[str] = []

    if waveform_v.size == 0:
        return WaveformMetrics(
            sensitivity_mv=0.0,
            sensitivity_amplified_mv=0.0,
            polarity=UNKNOWN_POLARITY,
            measured_frequency_hz=None,
            cycles_used=0,
            warnings=["No waveform samples were captured."],
            waveform_v=waveform_v,
            sync_v=sync_v,
            sample_rate_hz=sample_rate_hz,
        )

    if np.max(waveform_v) >= 9.8 or np.min(waveform_v) <= -9.8:
        warnings.append("Waveform is near the LabJack +/-10 V input limit; check for clipping or attenuation.")

    if am502_gain <= 0:
        warnings.append("AM502 gain must be positive. Using gain = 1 for this calculation.")
        am502_gain = 1.0

    edges, measured_frequency_hz, sync_warnings = find_sync_edges(
        sync_v,
        sample_rate_hz,
        expected_frequency_hz=expected_frequency_hz,
        edge=sync_edge,
    )
    warnings.extend(sync_warnings)

    segments = cycle_segments_from_edges(edges)
    used_sync_for_polarity = True
    if not segments:
        segments = fallback_cycle_segments(
            sample_count=len(waveform_v),
            sample_rate_hz=sample_rate_hz,
            expected_frequency_hz=expected_frequency_hz,
        )
        used_sync_for_polarity = False
        warnings.append("Sensitivity was estimated without reliable blade sync cycle boundaries.")

    all_cycle_pp_v = cycle_peak_to_peak_values(waveform_v, segments)
    stable_segments, cycle_pp_v, stabilized, stability_change_pct, stabilization_cycle = select_stable_cycle_window(
        segments,
        all_cycle_pp_v,
        tolerance=stability_tolerance,
        window_cycles=stability_window_cycles,
    )

    if not stabilized:
        warnings.append(
            f"Waveform did not stabilize within {stability_tolerance * 100:.0f}% before the capture limit."
        )

    if cycle_pp_v:
        amplified_pp_v = float(np.median(cycle_pp_v))
    else:
        amplified_pp_v = 0.0
        warnings.append("No valid waveform cycles were available for sensitivity measurement.")

    if used_sync_for_polarity:
        polarity = estimate_polarity(waveform_v, stable_segments, cycle_pp_v)
    else:
        polarity = UNKNOWN_POLARITY

    sensitivity_v = amplified_pp_v / am502_gain
    return WaveformMetrics(
        sensitivity_mv=sensitivity_v * 1000.0,
        sensitivity_amplified_mv=amplified_pp_v * 1000.0,
        polarity=polarity,
        measured_frequency_hz=measured_frequency_hz,
        cycles_used=len(cycle_pp_v),
        cycle_pp_mv=[value * 1000.0 for value in cycle_pp_v],
        all_cycle_pp_mv=[value * 1000.0 for value in all_cycle_pp_v],
        stabilized=stabilized,
        stability_change_pct=stability_change_pct,
        stabilization_cycle=stabilization_cycle,
        warnings=warnings,
        edges=edges,
        waveform_v=waveform_v,
        sync_v=sync_v,
        sample_rate_hz=sample_rate_hz,
    )


def evaluate_result(
    offset_v: float | None,
    waveform_metrics: WaveformMetrics | None,
    filter_setup: str,
) -> FinalResult:
    fail_reasons: list[str] = []
    warnings: list[str] = []

    if offset_v is None:
        fail_reasons.append("Offset was not measured.")
    elif offset_v < OFFSET_MIN_V or offset_v > OFFSET_MAX_V:
        fail_reasons.append(
            f"Offset out of range: {offset_v:.3f} V, expected {OFFSET_MIN_V:.1f} to {OFFSET_MAX_V:.1f} V."
        )

    sensitivity_mv: float | None = None
    polarity = UNKNOWN_POLARITY
    if waveform_metrics is None:
        fail_reasons.append("Waveform was not measured.")
    else:
        sensitivity_mv = waveform_metrics.sensitivity_mv
        polarity = waveform_metrics.polarity
        warnings.extend(waveform_metrics.warnings)

        if not waveform_metrics.stabilized:
            fail_reasons.append("Waveform did not stabilize before the capture limit.")

        min_sensitivity_mv = FILTER_SPECS_MV[filter_setup]
        if sensitivity_mv < min_sensitivity_mv:
            fail_reasons.append(
                f"Sensitivity too low: {sensitivity_mv:.2f} mV, minimum is {min_sensitivity_mv:.2f} mV."
            )

        if polarity != POSITIVE_POLARITY:
            fail_reasons.append(f"Polarity is {polarity}; expected {POSITIVE_POLARITY}.")

    return FinalResult(
        passed=not fail_reasons,
        offset_v=offset_v,
        sensitivity_mv=sensitivity_mv,
        polarity=polarity,
        fail_reasons=fail_reasons,
        warnings=warnings,
        waveform_metrics=waveform_metrics,
    )


def simulate_offset_v(case_name: str) -> float:
    if case_name == "Low offset":
        return 0.18
    if case_name == "High offset":
        return 1.38
    return random.uniform(0.55, 0.95)


def simulate_waveform_samples(
    filter_setup: str,
    case_name: str,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    cycles: int = DEFAULT_MAX_CAPTURE_CYCLES,
    am502_gain: float = DEFAULT_AM502_GAIN,
    frequency_hz: float = EXPECTED_FREQUENCY_HZ,
    noise_rms_v: float = 0.010,
) -> tuple[np.ndarray, np.ndarray, float]:
    duration_s = max(0.4, cycles / frequency_hz)
    sample_count = int(round(duration_s * sample_rate_hz))
    t = np.arange(sample_count, dtype=float) / sample_rate_hz
    phase = (t * frequency_hz) % 1.0

    min_mv = FILTER_SPECS_MV[filter_setup]
    if case_name == "Low sensitivity":
        sensitivity_mv = min_mv * 0.62
    elif case_name == "Known good":
        sensitivity_mv = min_mv * 1.35
    elif case_name == "Random good sensor":
        sensitivity_mv = min_mv * random.uniform(1.15, 1.65)
    else:
        sensitivity_mv = min_mv * 1.35

    # Triangle from -1 to +1 during the first half-cycle, then back down.
    triangle = np.where(phase < 0.5, -1.0 + 4.0 * phase, 3.0 - 4.0 * phase)
    if case_name == "Wrong polarity":
        triangle = -triangle

    amplified_pp_v = (sensitivity_mv / 1000.0) * am502_gain
    waveform_v = triangle * (amplified_pp_v / 2.0)
    waveform_v += np.random.normal(0.0, noise_rms_v, size=sample_count)

    sync_v = np.where(phase < 0.5, 5.0, 0.0)
    sync_v += np.random.normal(0.0, 0.015, size=sample_count)
    return waveform_v, sync_v, sample_rate_hz


def append_result_csv(
    csv_path: Path,
    sensor_id: str,
    filter_setup: str,
    final_result: FinalResult,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "sensor_id": sensor_id,
        "model": MODEL_NAME,
        "filter_setup": filter_setup,
        "offset_v": "" if final_result.offset_v is None else f"{final_result.offset_v:.6f}",
        "sensitivity_mv": ""
        if final_result.sensitivity_mv is None
        else f"{final_result.sensitivity_mv:.6f}",
        "polarity": final_result.polarity,
        "pass_fail": "PASS" if final_result.passed else "FAIL",
        "fail_reasons": "; ".join(final_result.fail_reasons),
    }
    with csv_path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


class LabJackT7:
    def __init__(self, identifier: str = "ANY") -> None:
        self.identifier = identifier
        self.handle: int | None = None

    def connect(self) -> None:
        if ljm is None:
            raise RuntimeError("The labjack.ljm Python package is not available.")
        if self.handle is not None:
            return
        self.handle = ljm.openS("T7", "ANY", self.identifier)
        self.configure_analog_inputs()

    def configure_analog_inputs(self) -> None:
        if self.handle is None:
            raise RuntimeError("LabJack is not connected.")
        for channel in (WAVEFORM_CHANNEL, OFFSET_CHANNEL, SYNC_CHANNEL):
            for name, value in (
                (f"{channel}_NEGATIVE_CH", 199),
                (f"{channel}_RANGE", 10.0),
                (f"{channel}_RESOLUTION_INDEX", 0),
            ):
                try:
                    ljm.eWriteName(self.handle, name, value)
                except Exception:
                    pass

    def close(self) -> None:
        if self.handle is not None and ljm is not None:
            try:
                ljm.close(self.handle)
            finally:
                self.handle = None

    def read_average(self, channel: str, samples: int = 25, delay_s: float = 0.01) -> float:
        if self.handle is None:
            raise RuntimeError("LabJack is not connected.")
        readings = []
        for _ in range(samples):
            readings.append(float(ljm.eReadName(self.handle, channel)))
            time.sleep(delay_s)
        return float(np.mean(readings))

    def read_waveform_stream(
        self,
        sample_rate_hz: float,
        expected_frequency_hz: float,
        sync_edge: str,
        max_capture_cycles: int = DEFAULT_MAX_CAPTURE_CYCLES,
        stability_tolerance: float = DEFAULT_STABILITY_TOLERANCE,
        stability_window_cycles: int = DEFAULT_STABILITY_WINDOW_CYCLES,
    ) -> tuple[np.ndarray, np.ndarray, float]:
        if self.handle is None:
            raise RuntimeError("LabJack is not connected.")

        scan_names = [WAVEFORM_CHANNEL, SYNC_CHANNEL]
        scan_list = ljm.namesToAddresses(len(scan_names), scan_names)[0]
        scans_per_read = max(20, int(sample_rate_hz / 10.0))
        target_scans = int(math.ceil((max_capture_cycles / expected_frequency_hz) * sample_rate_hz))

        waveform: list[float] = []
        sync: list[float] = []
        actual_scan_rate = float(sample_rate_hz)

        try:
            actual_scan_rate = float(
                ljm.eStreamStart(
                    self.handle,
                    scans_per_read,
                    len(scan_names),
                    scan_list,
                    float(sample_rate_hz),
                )
            )
            while len(waveform) < target_scans:
                data, _device_backlog, _ljm_backlog = ljm.eStreamRead(self.handle)
                arr = np.asarray(data, dtype=float).reshape((-1, len(scan_names)))
                valid = np.all(arr > -9998.0, axis=1)
                arr = arr[valid]
                if arr.size == 0:
                    continue
                waveform.extend(arr[:, 0].tolist())
                sync.extend(arr[:, 1].tolist())
                if len(waveform) >= int((stability_window_cycles * 2 / expected_frequency_hz) * actual_scan_rate):
                    metrics = analyze_waveform(
                        np.asarray(waveform, dtype=float),
                        np.asarray(sync, dtype=float),
                        actual_scan_rate,
                        am502_gain=1.0,
                        sync_edge=sync_edge,
                        expected_frequency_hz=expected_frequency_hz,
                        stability_tolerance=stability_tolerance,
                        stability_window_cycles=stability_window_cycles,
                    )
                    if metrics.stabilized:
                        break
        finally:
            try:
                ljm.eStreamStop(self.handle)
            except Exception:
                pass

        return (
            np.asarray(waveform[:target_scans], dtype=float),
            np.asarray(sync[:target_scans], dtype=float),
            actual_scan_rate,
        )


def probe_labjack_status() -> tuple[bool, str]:
    if ljm is None:
        return False, "LabJack LJM Python library is not available. Simulator mode is ready."
    try:
        devices = ljm.listAllS("T7", "ANY")
    except Exception as exc:
        text = str(exc)
        if "1230" in text or "CLAIMED" in text.upper():
            return (
                False,
                "T7 found, but another LabJack program has claimed it. Close LJStreamM/Kipling, then press Connect.",
            )
        return False, f"Could not check for T7: {exc}"

    count = int(devices[0]) if devices else 0
    if count <= 0:
        return False, "No T7 detected. Simulator mode is ready."
    return True, "T7 detected. Press Connect when the fixture is wired."


class TesterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Eltec 406MCA Single-Sensor Tester")
        self.minsize(1060, 720)

        self.device: LabJackT7 | None = None
        self.offset_v: float | None = None
        self.last_waveform_metrics: WaveformMetrics | None = None
        self.last_result: FinalResult | None = None
        self.busy = False

        self._build_variables()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.startup_probe)

    def _build_variables(self) -> None:
        self.sensor_id_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar(value="-3 filter")
        self.simulator_var = tk.BooleanVar(value=False)
        self.sim_case_var = tk.StringVar(value="Random good sensor")
        self.am502_gain_var = tk.StringVar(value=f"{DEFAULT_AM502_GAIN:.0f}")
        self.sync_edge_var = tk.StringVar(value="Rising")
        self.csv_path_var = tk.StringVar(value=str(default_results_path()))

        self.status_var = tk.StringVar(value="Checking LabJack...")
        self.offset_display_var = tk.StringVar(value="Not measured")
        self.sensitivity_display_var = tk.StringVar(value="Not measured")
        self.polarity_display_var = tk.StringVar(value="Not measured")
        self.frequency_display_var = tk.StringVar(value="Not measured")
        self.overall_display_var = tk.StringVar(value="READY")

    def _build_ui(self) -> None:
        self.configure(bg="#f4f6f8")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabelframe", background="#f4f6f8")
        style.configure("TLabelframe.Label", background="#f4f6f8", font=("Segoe UI", 11, "bold"))
        style.configure("TLabel", background="#f4f6f8", font=("Segoe UI", 10))
        style.configure("Large.TButton", font=("Segoe UI", 13, "bold"), padding=(12, 10))
        style.configure("TCheckbutton", background="#f4f6f8", font=("Segoe UI", 10))

        header = ttk.Frame(self, padding=(14, 12, 14, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text="Eltec 406MCA Single-Sensor Tester", font=("Segoe UI", 20, "bold"))
        title.grid(row=0, column=0, sticky="w")
        status = ttk.Label(header, textvariable=self.status_var, font=("Segoe UI", 10))
        status.grid(row=1, column=0, sticky="w", pady=(4, 0))

        main = ttk.Frame(self, padding=(14, 0, 14, 14))
        main.grid(row=1, column=0, sticky="nsew")
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        main.columnconfigure(0, weight=0)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        controls = ttk.LabelFrame(main, text="Operator Controls", padding=12)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        controls.columnconfigure(1, weight=1)

        self._add_labeled_entry(controls, 0, "Sensor ID", self.sensor_id_var)
        self._add_labeled_combo(controls, 1, "Filter/setup", self.filter_var, list(FILTER_SPECS_MV.keys()))
        self._add_labeled_combo(controls, 2, "Sync edge", self.sync_edge_var, ["Rising", "Falling"])
        self._add_labeled_entry(controls, 3, "AM502 gain", self.am502_gain_var)

        sim_check = ttk.Checkbutton(controls, text="Simulator mode", variable=self.simulator_var)
        sim_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 2))
        self._add_labeled_combo(controls, 5, "Sim case", self.sim_case_var, SIM_CASES)

        ttk.Label(controls, text="CSV log").grid(row=6, column=0, sticky="w", pady=(14, 2))
        csv_entry = ttk.Entry(controls, textvariable=self.csv_path_var, width=38)
        csv_entry.grid(row=7, column=0, columnspan=2, sticky="ew")

        self.connect_button = ttk.Button(
            controls,
            text="Connect",
            command=self.connect_labjack,
            style="Large.TButton",
        )
        self.connect_button.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(18, 8))

        self.test_button = ttk.Button(
            controls,
            text="Start Test",
            command=self.start_waveform_test,
            style="Large.TButton",
        )
        self.test_button.grid(row=9, column=0, columnspan=2, sticky="ew", pady=8)

        wiring = (
            "Wiring:\n"
            "AIN0 = AM502 waveform\n"
            "AIN1 = DC offset\n"
            "AIN2 = blade sync\n\n"
            "Close LJStreamM/Kipling before using hardware mode."
        )
        ttk.Label(controls, text=wiring, justify="left").grid(row=10, column=0, columnspan=2, sticky="w", pady=(18, 0))

        results = ttk.Frame(main)
        results.grid(row=0, column=1, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(2, weight=1)

        self.overall_card = tk.Label(
            results,
            textvariable=self.overall_display_var,
            font=("Segoe UI", 38, "bold"),
            bg="#e8edf3",
            fg="#1f2937",
            padx=18,
            pady=14,
        )
        self.overall_card.grid(row=0, column=0, sticky="ew")

        cards = ttk.Frame(results)
        cards.grid(row=1, column=0, sticky="ew", pady=(12, 12))
        for col in range(4):
            cards.columnconfigure(col, weight=1)

        self.offset_card = self._metric_card(cards, 0, "Offset", self.offset_display_var)
        self.sensitivity_card = self._metric_card(cards, 1, "Sensitivity", self.sensitivity_display_var)
        self.polarity_card = self._metric_card(cards, 2, "Polarity", self.polarity_display_var)
        self.frequency_card = self._metric_card(cards, 3, "Frequency", self.frequency_display_var)

        lower = ttk.Frame(results)
        lower.grid(row=2, column=0, sticky="nsew")
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(1, weight=1)

        ttk.Label(lower, text="Failure reasons and warnings", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.message_text = tk.Text(
            lower,
            height=7,
            wrap="word",
            font=("Segoe UI", 11),
            bg="#ffffff",
            relief="solid",
            bd=1,
        )
        self.message_text.grid(row=1, column=0, sticky="nsew", pady=(4, 12))

        ttk.Label(lower, text="Waveform confidence view", font=("Segoe UI", 11, "bold")).grid(
            row=2, column=0, sticky="w"
        )
        self.wave_canvas = tk.Canvas(lower, height=240, bg="#0b1120", highlightthickness=0)
        self.wave_canvas.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.wave_canvas.bind("<Configure>", lambda _event: self.redraw_waveform())

    def _add_labeled_entry(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 8))
        entry = ttk.Entry(parent, textvariable=variable, width=18, font=("Segoe UI", 11))
        entry.grid(row=row, column=1, sticky="ew", pady=5)

    def _add_labeled_combo(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        values: list[str],
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 8))
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=24)
        combo.grid(row=row, column=1, sticky="ew", pady=5)

    def _metric_card(self, parent: ttk.Frame, column: int, label: str, value_var: tk.StringVar) -> tk.Frame:
        frame = tk.Frame(parent, bg="#ffffff", bd=1, relief="solid")
        frame.grid(row=0, column=column, sticky="ew", padx=4)
        tk.Label(frame, text=label, bg="#ffffff", fg="#64748b", font=("Segoe UI", 10, "bold")).pack(pady=(8, 0))
        tk.Label(frame, textvariable=value_var, bg="#ffffff", fg="#111827", font=("Segoe UI", 16, "bold")).pack(
            pady=(2, 10)
        )
        return frame

    def startup_probe(self) -> None:
        ok, message = probe_labjack_status()
        self.status_var.set(message)
        if not ok:
            self.simulator_var.set(True)

    def connect_labjack(self) -> None:
        if self.simulator_var.get():
            self.status_var.set("Simulator mode is active. Hardware connection is skipped.")
            return
        self.set_busy(True)

        def worker() -> None:
            try:
                if self.device is None:
                    self.device = LabJackT7()
                self.device.connect()
            except Exception as exc:
                self.after(0, lambda exc=exc: self.hardware_error(exc))
            else:
                self.after(0, lambda: self.status_var.set("Connected to LabJack T7. Ready to test."))
            finally:
                self.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def hardware_error(self, exc: Exception) -> None:
        text = str(exc)
        if "1230" in text or "CLAIMED" in text.upper():
            text = "The T7 is claimed by another LabJack program. Close LJStreamM/Kipling and try Connect again."
        self.status_var.set(text)
        messagebox.showerror("LabJack connection problem", text)

    def read_offset_value(self) -> float:
        if self.simulator_var.get():
            offset = simulate_offset_v(self.sim_case_var.get())
            time.sleep(0.25)
            return offset
        self.ensure_connected()
        return self.device.read_average(OFFSET_CHANNEL)

    def read_offset(self) -> None:
        self.set_busy(True)
        self.overall_display_var.set("READING OFFSET")
        self.set_overall_color("#e8edf3", "#1f2937")

        def worker() -> None:
            try:
                offset = self.read_offset_value()
            except Exception as exc:
                self.after(0, lambda exc=exc: self.hardware_error(exc))
            else:
                self.after(0, lambda: self.on_offset_read(offset))
            finally:
                self.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def update_offset_card(self, offset: float) -> bool:
        self.offset_v = offset
        if OFFSET_MIN_V <= offset <= OFFSET_MAX_V:
            self.offset_display_var.set(f"{offset:.3f} V PASS")
            self.set_card_color(self.offset_card, "#dcfce7")
            return True
        self.offset_display_var.set(f"{offset:.3f} V FAIL")
        self.set_card_color(self.offset_card, "#fee2e2")
        return False

    def on_offset_read(self, offset: float) -> None:
        offset_ok = self.update_offset_card(offset)
        if offset_ok:
            self.overall_display_var.set("OFFSET OK")
            self.set_overall_color("#dcfce7", "#166534")
            self.write_note(
                [
                    f"Offset reading: {offset:.6f} V",
                    f"Offset is within the {OFFSET_MIN_V:.1f} to {OFFSET_MAX_V:.1f} V specification.",
                ]
            )
        else:
            self.overall_display_var.set("OFFSET FAIL")
            self.set_overall_color("#fee2e2", "#991b1b")
            self.write_note(
                [
                    f"Offset reading: {offset:.6f} V",
                    f"Offset is outside the {OFFSET_MIN_V:.1f} to {OFFSET_MAX_V:.1f} V specification.",
                ]
            )

    def start_waveform_test(self) -> None:
        try:
            gain = float(self.am502_gain_var.get())
        except ValueError:
            messagebox.showerror("Invalid setup", "AM502 gain must be a number.")
            return

        if gain <= 0:
            messagebox.showerror("Invalid setup", "AM502 gain must be positive.")
            return

        self.set_busy(True)
        self.overall_display_var.set("MEASURING OFFSET")
        self.set_overall_color("#e8edf3", "#1f2937")
        self.write_note(["Measuring offset..."])

        def worker() -> None:
            try:
                offset = self.read_offset_value()
                self.after(0, lambda offset=offset: self.update_offset_card(offset))
                self.after(0, lambda: self.overall_display_var.set("WAITING FOR STABLE WAVE"))
                self.after(0, lambda: self.set_overall_color("#fef3c7", "#92400e"))
                self.after(0, lambda: self.write_note(["Capturing waveform until the cycle average stabilizes..."]))

                if self.simulator_var.get():
                    waveform, sync, actual_rate = simulate_waveform_samples(
                        filter_setup=self.filter_var.get(),
                        case_name=self.sim_case_var.get(),
                        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
                        cycles=DEFAULT_MAX_CAPTURE_CYCLES,
                        am502_gain=gain,
                    )
                    time.sleep(0.5)
                else:
                    self.ensure_connected()
                    waveform, sync, actual_rate = self.device.read_waveform_stream(
                        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
                        expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
                        sync_edge=self.sync_edge_var.get(),
                    )

                metrics = analyze_waveform(
                    waveform_v=waveform,
                    sync_v=sync,
                    sample_rate_hz=actual_rate,
                    am502_gain=gain,
                    sync_edge=self.sync_edge_var.get(),
                )
                final = evaluate_result(offset, metrics, self.filter_var.get())
                append_result_csv(
                    Path(self.csv_path_var.get()),
                    sensor_id=self.sensor_id_var.get().strip() or "UNLABELED",
                    filter_setup=self.filter_var.get(),
                    final_result=final,
                )
            except Exception as exc:
                self.after(0, lambda exc=exc: self.hardware_error(exc))
            else:
                self.after(0, lambda: self.on_test_complete(metrics, final))
            finally:
                self.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def on_test_complete(self, metrics: WaveformMetrics, final: FinalResult) -> None:
        self.last_waveform_metrics = metrics
        self.last_result = final

        offset_ok = final.offset_v is not None and OFFSET_MIN_V <= final.offset_v <= OFFSET_MAX_V
        offset_text = "Not measured" if final.offset_v is None else f"{final.offset_v:.3f} V"
        self.offset_display_var.set(offset_text + (" PASS" if offset_ok else " FAIL"))
        self.sensitivity_display_var.set(f"{metrics.sensitivity_mv:.2f} mV")
        self.polarity_display_var.set(metrics.polarity)
        if metrics.measured_frequency_hz is None:
            self.frequency_display_var.set("No sync")
        else:
            self.frequency_display_var.set(f"{metrics.measured_frequency_hz:.3f} Hz")

        self.set_card_color(self.offset_card, "#dcfce7" if offset_ok else "#fee2e2")
        self.set_card_color(
            self.sensitivity_card,
            "#dcfce7" if metrics.sensitivity_mv >= FILTER_SPECS_MV[self.filter_var.get()] else "#fee2e2",
        )
        self.set_card_color(self.polarity_card, "#dcfce7" if metrics.polarity == POSITIVE_POLARITY else "#fee2e2")
        self.set_card_color(
            self.frequency_card,
            "#fef9c3" if metrics.warnings else "#dcfce7",
        )

        if final.passed:
            self.overall_display_var.set("PASS")
            self.set_overall_color("#22c55e", "#052e16")
        else:
            self.overall_display_var.set("FAIL")
            self.set_overall_color("#ef4444", "#450a0a")

        self.write_messages(final.fail_reasons, final.warnings)
        self.redraw_waveform()
        self.status_var.set(f"Test complete. Result was saved to {self.csv_path_var.get()}")

    def ensure_connected(self) -> None:
        if self.device is None:
            self.device = LabJackT7()
        self.device.connect()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.connect_button, self.test_button):
            button.configure(state=state)

    def set_overall_color(self, bg: str, fg: str) -> None:
        self.overall_card.configure(bg=bg, fg=fg)

    def set_card_color(self, card: tk.Frame, bg: str) -> None:
        card.configure(bg=bg)
        for child in card.winfo_children():
            child.configure(bg=bg)

    def write_messages(self, fail_reasons: list[str], warnings: list[str]) -> None:
        self.message_text.configure(state="normal")
        self.message_text.delete("1.0", "end")
        if fail_reasons:
            self.message_text.insert("end", "FAIL REASONS\n")
            for reason in fail_reasons:
                self.message_text.insert("end", f"- {reason}\n")
        else:
            self.message_text.insert("end", "No fail reasons.\n")
        if warnings:
            self.message_text.insert("end", "\nWARNINGS\n")
            for warning in warnings:
                self.message_text.insert("end", f"- {warning}\n")
        self.message_text.configure(state="disabled")

    def write_note(self, lines: list[str]) -> None:
        self.message_text.configure(state="normal")
        self.message_text.delete("1.0", "end")
        for line in lines:
            self.message_text.insert("end", f"{line}\n")
        self.message_text.configure(state="disabled")

    def redraw_waveform(self) -> None:
        canvas = self.wave_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())

        metrics = self.last_waveform_metrics
        if metrics is None or metrics.waveform_v.size == 0:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Waveform will appear here after a test.",
                fill="#cbd5e1",
                font=("Segoe UI", 13),
            )
            return

        waveform = metrics.waveform_v
        sync = metrics.sync_v
        n = len(waveform)
        if n < 2:
            return

        step = max(1, n // max(200, width))
        idx = np.arange(0, n, step)
        x = idx / max(1, n - 1) * (width - 20) + 10

        wave_min = float(np.min(waveform))
        wave_max = float(np.max(waveform))
        if abs(wave_max - wave_min) < 1e-9:
            wave_max = wave_min + 1.0

        top = 18
        mid_bottom = int(height * 0.70)
        wave_y = mid_bottom - (waveform[idx] - wave_min) / (wave_max - wave_min) * (mid_bottom - top)
        points = []
        for px, py in zip(x, wave_y):
            points.extend([float(px), float(py)])
        canvas.create_line(points, fill="#38bdf8", width=2)

        sync_top = int(height * 0.76)
        sync_bottom = height - 18
        if len(sync) == n:
            sync_min = float(np.min(sync))
            sync_max = float(np.max(sync))
            if abs(sync_max - sync_min) < 1e-9:
                sync_max = sync_min + 1.0
            sync_y = sync_bottom - (sync[idx] - sync_min) / (sync_max - sync_min) * (sync_bottom - sync_top)
            sync_points = []
            for px, py in zip(x, sync_y):
                sync_points.extend([float(px), float(py)])
            canvas.create_line(sync_points, fill="#facc15", width=1)

        for edge_idx in metrics.edges[:50]:
            px = edge_idx / max(1, n - 1) * (width - 20) + 10
            canvas.create_line(px, 12, px, height - 12, fill="#475569", dash=(3, 5))

        canvas.create_text(12, 12, anchor="nw", text="AIN0 waveform", fill="#38bdf8", font=("Segoe UI", 10, "bold"))
        canvas.create_text(12, sync_top, anchor="nw", text="AIN2 blade sync", fill="#facc15", font=("Segoe UI", 10, "bold"))
        canvas.create_text(
            width - 12,
            12,
            anchor="ne",
            text=f"{metrics.cycles_used} cycles used",
            fill="#cbd5e1",
            font=("Segoe UI", 10),
        )

    def on_close(self) -> None:
        if self.device is not None:
            self.device.close()
        self.destroy()


def main() -> None:
    app = TesterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
