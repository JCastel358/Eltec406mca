"""
Eltec 406MCA scope-verification tester.

Reads DC offset from AIN1, then reads a 10 Hz pyroelectric detector waveform
from AIN0 with AIN2 chopper sync. Checks sensitivity, polarity, and offset,
then lets an oscilloscope operator record the verification tag before logging
the CSV result.

Default wiring:
    AIN0: AM502 x100 signal output
    AIN1: DC offset output
    AIN2: 10 Hz chopper signal from the black body emitter

Run:
    python eltec_406mca_scope_verification_tester.py
"""

from __future__ import annotations

import csv
import json
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
DEFAULT_OFFSET_READ_SAMPLES = 80
DEFAULT_OFFSET_READ_DELAY_S = 0.01
DEFAULT_MAX_CAPTURE_CYCLES = 80
DEFAULT_SETTLE_CYCLES = 0
DEFAULT_STABILITY_TOLERANCE = 0.10
DEFAULT_STABILITY_WINDOW_CYCLES = 5
DEFAULT_AM502_GAIN = 100.0
DEFAULT_WAVEFORM_INPUT_RANGE_LABEL = "+/-10 V (x1)"
DEFAULT_FILTER_SETUP = "-284 filter + extra -6 + blackened tube"
DEFAULT_STREAM_RESOLUTION_INDEX = 1
DEFAULT_STREAM_SETTLING_US = 0.0
WAVEFORM_SETTLING_READS = 3
SYNC_INPUT_RANGE_V = 10.0
PROCEDURE_SYNC_EDGE = "Rising"
POLARITY_RESPONSE_SPACING_FRACTION = 0.20
POLARITY_SEARCH_START_FRACTION = 0.00
POLARITY_SEARCH_END_FRACTION = 0.30
POLARITY_SAMPLE_WINDOW_FRACTION = 0.03
POLARITY_MIN_CONFIDENCE = 0.10
POLARITY_SEARCH_STEPS = 31
POSITIVE_POLARITY = "POSITIVE"
NEGATIVE_POLARITY = "NEGATIVE"
UNKNOWN_POLARITY = "UNKNOWN"

LABJACK_AIN0_RANGE_OPTIONS = {
    "+/-10 V (x1)": 10.0,
    "+/-1 V (x10)": 1.0,
    "+/-0.1 V (x100)": 0.1,
    "+/-0.01 V (x1000)": 0.01,
}

FILTER_SPECS_MV = {
    "-3 filter": 25.0,
    "-27 filter": 25.0,
    "-266 filter": 30.9,
    "-273 filter + blackened tube": 2.3,
    "-284 filter + extra -6 + blackened tube": 4.0,
}

SCOPE_GOOD_TAG = "GOOD"
SCOPE_VERIFICATION_CHOICES = [
    "GOOD - Scope good / code verified",
    "GO/D - Good offset/no signal",
    "O - No sensitivity",
    "LS - Low sensitivity",
    "N - Noisy",
    "FN - Fast noise",
    "OSC - Oscillation",
    "HO - High offset",
    "LO - Low offset",
    "D - No offset",
    "HRV - High ref volt",
    "LRV - Low ref volt",
    "RP - Reversed polarity",
    "Unstable - Unstable",
    "SI - Wrong pattern: sinewave",
    "SW - Wrong pattern: sawtooth",
    "SQ - Wrong pattern: square",
    "RSQ - Wrong pattern: rounded square",
    "T - Wrong pattern: triangle",
    "HIG - High IGSS",
    "Drop - Dropped",
]
DEFAULT_SCOPE_VERIFICATION_CHOICE = SCOPE_VERIFICATION_CHOICES[0]

CSV_FIELDS = [
    "timestamp",
    "lot_number",
    "sensor_number",
    "sensor_id",
    "model",
    "filter_setup",
    "am502_gain",
    "labjack_ain0_ain1_range",
    "offset_v",
    "sensitivity_mv",
    "polarity",
    "pass_fail",
    "fail_reasons",
    "scope_pass_fail",
    "scope_tag",
    "scope_reason",
    "operator_polarity",
    "actual_offset_v",
    "actual_offset_delta_v",
    "actual_sensitivity_mv",
    "actual_sensitivity_delta_mv",
    "waveform_snapshot_path",
]


@dataclass
class WaveformMetrics:
    sensitivity_mv: float
    sensitivity_amplified_mv: float
    polarity: str
    measured_frequency_hz: float | None
    cycles_used: int
    offset_v: float | None = None
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
    ignored_initial_cycles: int = 0
    input_range_v: float = LABJACK_AIN0_RANGE_OPTIONS[DEFAULT_WAVEFORM_INPUT_RANGE_LABEL]
    polarity_confidence: float | None = None
    polarity_response_start_fraction: float | None = None
    polarity_response_end_fraction: float | None = None
    polarity_delta_mv: float | None = None


@dataclass
class PolarityEstimate:
    polarity: str
    confidence: float | None = None
    response_start_fraction: float | None = None
    response_end_fraction: float | None = None
    delta_v: float | None = None


@dataclass
class FinalResult:
    passed: bool
    offset_v: float | None
    sensitivity_mv: float | None
    polarity: str
    fail_reasons: list[str]
    warnings: list[str]
    waveform_metrics: WaveformMetrics | None = None


@dataclass
class ScopeVerification:
    tag: str
    reason: str
    offset_v: float | None = None
    offset_delta_v: float | None = None
    sensitivity_mv: float | None = None
    sensitivity_delta_mv: float | None = None
    operator_polarity: str = ""


def results_root_dir() -> Path:
    return Path.home() / "Documents" / "Eltec_406MCA_Test_Results"


def default_results_path() -> Path:
    return results_root_dir() / "406mca_scope_verification_results.csv"


def safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    return cleaned.strip("_") or "UNLABELED"


def batch_results_path(lot_number: str) -> Path:
    safe_lot = safe_filename_part(lot_number)
    return results_root_dir() / f"406mca_scope_verification_lot_{safe_lot}.csv"


def batch_autosave_path(lot_number: str) -> Path:
    safe_lot = safe_filename_part(lot_number)
    return results_root_dir() / "autosave" / f"lot_{safe_lot}_current_sensor.json"


def count_existing_batch_rows(csv_path: Path) -> int:
    if not csv_path.exists():
        return 0
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
            return sum(1 for _row in csv.DictReader(csv_file))
    except Exception:
        return 0


def next_sensor_number_for_batch(csv_path: Path) -> int:
    if not csv_path.exists():
        return 1

    next_number = 1
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
            for row in csv.DictReader(csv_file):
                sensor_number_text = (row.get("sensor_number") or "").strip()
                if sensor_number_text:
                    try:
                        next_number = max(next_number, int(sensor_number_text) + 1)
                        continue
                    except ValueError:
                        pass
                sensor_id = (row.get("sensor_id") or "").strip()
                if "-" in sensor_id:
                    suffix = sensor_id.rsplit("-", 1)[-1]
                    try:
                        next_number = max(next_number, int(suffix) + 1)
                    except ValueError:
                        pass
    except Exception:
        return count_existing_batch_rows(csv_path) + 1
    return next_number


def labjack_ain0_range_from_label(label: str) -> float:
    try:
        return LABJACK_AIN0_RANGE_OPTIONS[label]
    except KeyError as exc:
        raise ValueError(f"Unknown LabJack AIN0 range: {label}") from exc


def split_scope_verification_choice(choice: str) -> tuple[str, str]:
    if " - " not in choice:
        cleaned = choice.strip()
        return cleaned, ""
    tag, reason = choice.split(" - ", 1)
    return tag.strip(), reason.strip()


def labjack_range_offset_warning(input_range_v: float, range_label: str) -> str | None:
    if input_range_v >= OFFSET_MIN_V:
        return None
    return (
        f"{range_label} cannot measure the normal {OFFSET_MIN_V:.1f} to {OFFSET_MAX_V:.1f} V offset on AIN1. "
        "Use +/-10 V (x1) for this AM502 x100 scope-verification setup. "
        "The LabJack range gain improves resolution, but it cannot remove the DC offset."
    )


def find_sync_edges(
    sync_v: np.ndarray,
    sample_rate_hz: float,
    expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
    edge: str = PROCEDURE_SYNC_EDGE,
) -> tuple[list[int], float | None, list[str]]:
    warnings: list[str] = []
    sync_v = np.asarray(sync_v, dtype=float)
    if sync_v.size < 3:
        return [], None, ["Sync signal is too short to detect edges."]

    sync_min = float(np.min(sync_v))
    sync_max = float(np.max(sync_v))
    sync_span = sync_max - sync_min
    if sync_span < 0.5:
        return [], None, ["AIN2 chopper sync signal is weak or disconnected."]

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
                    f"AIN2 chopper sync frequency is {measured_frequency_hz:.3f} Hz; "
                    f"expected {expected_frequency_hz:.1f} +/- {FREQUENCY_TOLERANCE_HZ:.1f} Hz."
                )
    else:
        warnings.append("Not enough AIN2 chopper sync edges were detected.")

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


def estimate_offset_from_waveform(
    waveform_v: np.ndarray,
    segments: list[tuple[int, int]],
) -> float | None:
    waveform_v = np.asarray(waveform_v, dtype=float)
    if waveform_v.size == 0:
        return None

    total = 0.0
    count = 0
    for start, end in segments:
        start = max(0, int(start))
        end = min(len(waveform_v), int(end))
        if end <= start:
            continue
        cycle = waveform_v[start:end]
        total += float(np.sum(cycle))
        count += int(cycle.size)

    if count == 0:
        return float(np.mean(waveform_v))
    return total / count


def samples_from_segments(
    waveform_v: np.ndarray,
    segments: list[tuple[int, int]],
) -> np.ndarray:
    waveform_v = np.asarray(waveform_v, dtype=float)
    if waveform_v.size == 0:
        return waveform_v

    pieces = []
    for start, end in segments:
        start = max(0, int(start))
        end = min(len(waveform_v), int(end))
        if end > start:
            pieces.append(waveform_v[start:end])

    if not pieces:
        return waveform_v
    return np.concatenate(pieces)


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


def segment_window_mean(
    waveform_v: np.ndarray,
    start: int,
    end: int,
    center_fraction: float,
    window_fraction: float = POLARITY_SAMPLE_WINDOW_FRACTION,
) -> float:
    length = max(1, end - start)
    center = start + int(round(center_fraction * length))
    half_width = max(1, int(round(window_fraction * length / 2.0)))
    window_start = max(start, center - half_width)
    window_end = min(end, center + half_width + 1)
    if window_end <= window_start:
        return float(waveform_v[min(max(start, center), end - 1)])
    return float(np.mean(waveform_v[window_start:window_end]))


def estimate_polarity(
    waveform_v: np.ndarray,
    segments: list[tuple[int, int]],
    cycle_pp_v: list[float],
) -> PolarityEstimate:
    if not segments or not cycle_pp_v:
        return PolarityEstimate(UNKNOWN_POLARITY)

    pp_reference = float(np.median(cycle_pp_v))
    if pp_reference <= 0:
        return PolarityEstimate(UNKNOWN_POLARITY)

    response_spacing = POLARITY_RESPONSE_SPACING_FRACTION
    search_start = max(0.0, POLARITY_SEARCH_START_FRACTION)
    search_end = min(POLARITY_SEARCH_END_FRACTION, 1.0 - response_spacing)
    if search_end < search_start:
        return PolarityEstimate(UNKNOWN_POLARITY)

    best_delta: float | None = None
    best_start_fraction: float | None = None
    for response_start_fraction in np.linspace(search_start, search_end, POLARITY_SEARCH_STEPS):
        response_end_fraction = float(response_start_fraction + response_spacing)
        deltas = []
        for start, end in segments:
            if end - start < 8:
                continue
            base = segment_window_mean(waveform_v, start, end, float(response_start_fraction))
            probe = segment_window_mean(waveform_v, start, end, response_end_fraction)
            deltas.append(probe - base)
        if not deltas:
            continue

        mean_delta = float(np.mean(deltas))
        if best_delta is None or abs(mean_delta) > abs(best_delta):
            best_delta = mean_delta
            best_start_fraction = float(response_start_fraction)

    if best_delta is None or best_start_fraction is None:
        return PolarityEstimate(UNKNOWN_POLARITY)

    confidence = min(abs(best_delta) / pp_reference, 1.0)
    response_end_fraction = best_start_fraction + response_spacing
    if confidence < POLARITY_MIN_CONFIDENCE:
        polarity = UNKNOWN_POLARITY
    else:
        polarity = POSITIVE_POLARITY if best_delta > 0 else NEGATIVE_POLARITY

    return PolarityEstimate(
        polarity=polarity,
        confidence=confidence,
        response_start_fraction=best_start_fraction,
        response_end_fraction=response_end_fraction,
        delta_v=best_delta,
    )


def analyze_waveform(
    waveform_v: np.ndarray,
    sync_v: np.ndarray,
    sample_rate_hz: float,
    am502_gain: float,
    sync_edge: str = PROCEDURE_SYNC_EDGE,
    expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
    stability_tolerance: float = DEFAULT_STABILITY_TOLERANCE,
    stability_window_cycles: int = DEFAULT_STABILITY_WINDOW_CYCLES,
    settle_cycles: int = DEFAULT_SETTLE_CYCLES,
    input_range_v: float = LABJACK_AIN0_RANGE_OPTIONS[DEFAULT_WAVEFORM_INPUT_RANGE_LABEL],
    expect_offset_on_waveform: bool = True,
) -> WaveformMetrics:
    waveform_v = np.asarray(waveform_v, dtype=float)
    sync_v = np.asarray(sync_v, dtype=float)
    settle_cycles = max(0, int(settle_cycles))
    input_range_v = abs(float(input_range_v))
    warnings: list[str] = []

    if waveform_v.size == 0:
        return WaveformMetrics(
            sensitivity_mv=0.0,
            sensitivity_amplified_mv=0.0,
            polarity=UNKNOWN_POLARITY,
            measured_frequency_hz=None,
            cycles_used=0,
            offset_v=None,
            warnings=["No waveform samples were captured."],
            waveform_v=waveform_v,
            sync_v=sync_v,
            sample_rate_hz=sample_rate_hz,
            ignored_initial_cycles=settle_cycles,
            input_range_v=input_range_v,
        )

    if input_range_v <= 0:
        input_range_v = LABJACK_AIN0_RANGE_OPTIONS[DEFAULT_WAVEFORM_INPUT_RANGE_LABEL]

    clip_limit_v = input_range_v * 0.98
    if np.max(waveform_v) >= clip_limit_v or np.min(waveform_v) <= -clip_limit_v:
        warnings.append(
            f"Waveform is near the LabJack +/-{input_range_v:g} V AIN0 range; "
            "choose a larger AIN0 range if the waveform is clipping."
        )

    if am502_gain <= 0:
        warnings.append("Signal gain must be positive. Using gain = 1 for this calculation.")
        am502_gain = 1.0

    edges, measured_frequency_hz, sync_warnings = find_sync_edges(
        sync_v,
        sample_rate_hz,
        expected_frequency_hz=expected_frequency_hz,
        edge=sync_edge,
    )
    warnings.extend(sync_warnings)

    segments = cycle_segments_from_edges(edges, settle_cycles=settle_cycles)
    used_sync_for_polarity = True
    if not segments:
        segments = fallback_cycle_segments(
            sample_count=len(waveform_v),
            sample_rate_hz=sample_rate_hz,
            expected_frequency_hz=expected_frequency_hz,
            settle_cycles=settle_cycles,
        )
        used_sync_for_polarity = False
        warnings.append("Sensitivity and offset were estimated without reliable AIN2 chopper cycle boundaries.")

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
        polarity_estimate = estimate_polarity(waveform_v, stable_segments, cycle_pp_v)
    else:
        polarity_estimate = PolarityEstimate(UNKNOWN_POLARITY)

    offset_segments = stable_segments if stable_segments else segments
    offset_v = estimate_offset_from_waveform(waveform_v, offset_segments)
    offset_samples = samples_from_segments(waveform_v, offset_segments)
    if expect_offset_on_waveform and offset_v is not None and offset_v < OFFSET_MIN_V and offset_samples.size:
        offset_min_v = float(np.min(offset_samples))
        offset_max_v = float(np.max(offset_samples))
        if abs(offset_v) < 0.15 and offset_min_v < -0.05 and offset_max_v > 0.05:
            warnings.append(
                "AIN0 average is near ground even though the waveform swings both positive and negative "
                f"(mean {offset_v:.3f} V, min {offset_min_v:.3f} V, max {offset_max_v:.3f} V). "
                "The amplifier/waveform output may be AC-coupled or centered at 0 V, so AIN0 is not carrying "
                "the 0.3 to 1.2 V offset."
            )

    sensitivity_v = amplified_pp_v / am502_gain
    return WaveformMetrics(
        sensitivity_mv=sensitivity_v * 1000.0,
        sensitivity_amplified_mv=amplified_pp_v * 1000.0,
        polarity=polarity_estimate.polarity,
        measured_frequency_hz=measured_frequency_hz,
        cycles_used=len(cycle_pp_v),
        offset_v=offset_v,
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
        ignored_initial_cycles=settle_cycles,
        input_range_v=input_range_v,
        polarity_confidence=polarity_estimate.confidence,
        polarity_response_start_fraction=polarity_estimate.response_start_fraction,
        polarity_response_end_fraction=polarity_estimate.response_end_fraction,
        polarity_delta_mv=None if polarity_estimate.delta_v is None else polarity_estimate.delta_v * 1000.0,
    )


def format_polarity_detail(waveform_metrics: WaveformMetrics | None) -> str | None:
    if waveform_metrics is None or waveform_metrics.polarity_confidence is None:
        return None

    confidence_pct = waveform_metrics.polarity_confidence * 100.0
    parts = [f"confidence {confidence_pct:.0f}% of cycle p-p"]
    if (
        waveform_metrics.polarity_response_start_fraction is not None
        and waveform_metrics.polarity_response_end_fraction is not None
    ):
        start_pct = waveform_metrics.polarity_response_start_fraction * 100.0
        end_pct = waveform_metrics.polarity_response_end_fraction * 100.0
        parts.append(f"response window {start_pct:.0f}-{end_pct:.0f}% after rising sync")
    if waveform_metrics.polarity_delta_mv is not None:
        parts.append(f"delta {waveform_metrics.polarity_delta_mv:.2f} mV")
    return ", ".join(parts)


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
            detail = format_polarity_detail(waveform_metrics)
            detail_suffix = "" if detail is None else f" ({detail})"
            fail_reasons.append(f"Polarity is {polarity}; expected {POSITIVE_POLARITY}.{detail_suffix}")

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
    offset_v: float | None = None,
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
    waveform_v += simulate_offset_v(case_name) if offset_v is None else offset_v
    waveform_v += np.random.normal(0.0, noise_rms_v, size=sample_count)

    sync_v = np.where(phase < 0.5, 5.0, 0.0)
    sync_v += np.random.normal(0.0, 0.015, size=sample_count)
    return waveform_v, sync_v, sample_rate_hz


def append_result_csv(
    csv_path: Path,
    sensor_id: str,
    filter_setup: str,
    am502_gain: float,
    labjack_range_label: str,
    final_result: FinalResult,
    scope_verification: ScopeVerification,
    lot_number: str = "",
    sensor_number: int | None = None,
    waveform_snapshot_path: Path | str | None = None,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "lot_number": lot_number,
        "sensor_number": "" if sensor_number is None else str(sensor_number),
        "sensor_id": sensor_id,
        "model": MODEL_NAME,
        "filter_setup": filter_setup,
        "am502_gain": f"{am502_gain:.6g}",
        "labjack_ain0_ain1_range": labjack_range_label,
        "offset_v": "" if final_result.offset_v is None else f"{final_result.offset_v:.6f}",
        "sensitivity_mv": ""
        if final_result.sensitivity_mv is None
        else f"{final_result.sensitivity_mv:.6f}",
        "polarity": final_result.polarity,
        "pass_fail": "PASS" if final_result.passed else "FAIL",
        "fail_reasons": "; ".join(final_result.fail_reasons),
        "scope_pass_fail": "PASS" if scope_verification.tag == SCOPE_GOOD_TAG else "FAIL",
        "scope_tag": scope_verification.tag,
        "scope_reason": scope_verification.reason,
        "operator_polarity": scope_verification.operator_polarity,
        "actual_offset_v": ""
        if scope_verification.offset_v is None
        else f"{scope_verification.offset_v:.6f}",
        "actual_offset_delta_v": ""
        if scope_verification.offset_delta_v is None
        else f"{scope_verification.offset_delta_v:.6f}",
        "actual_sensitivity_mv": ""
        if scope_verification.sensitivity_mv is None
        else f"{scope_verification.sensitivity_mv:.6f}",
        "actual_sensitivity_delta_mv": ""
        if scope_verification.sensitivity_delta_mv is None
        else f"{scope_verification.sensitivity_delta_mv:.6f}",
        "waveform_snapshot_path": "" if waveform_snapshot_path is None else str(waveform_snapshot_path),
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

    def configure_analog_inputs(
        self,
        waveform_range_v: float = LABJACK_AIN0_RANGE_OPTIONS[DEFAULT_WAVEFORM_INPUT_RANGE_LABEL],
    ) -> None:
        if self.handle is None:
            raise RuntimeError("LabJack is not connected.")
        channel_ranges = {
            WAVEFORM_CHANNEL: float(waveform_range_v),
            OFFSET_CHANNEL: float(waveform_range_v),
            SYNC_CHANNEL: SYNC_INPUT_RANGE_V,
        }
        for channel, input_range_v in channel_ranges.items():
            for name, value in (
                (f"{channel}_NEGATIVE_CH", 199),
                (f"{channel}_RANGE", input_range_v),
                (f"{channel}_RESOLUTION_INDEX", 0),
            ):
                ljm.eWriteName(self.handle, name, value)
        ljm.eWriteName(self.handle, "STREAM_RESOLUTION_INDEX", DEFAULT_STREAM_RESOLUTION_INDEX)
        ljm.eWriteName(self.handle, "STREAM_SETTLING_US", DEFAULT_STREAM_SETTLING_US)

    def close(self) -> None:
        if self.handle is not None and ljm is not None:
            try:
                ljm.close(self.handle)
            finally:
                self.handle = None

    def read_offset_voltage(
        self,
        waveform_range_v: float = LABJACK_AIN0_RANGE_OPTIONS[DEFAULT_WAVEFORM_INPUT_RANGE_LABEL],
        samples: int = DEFAULT_OFFSET_READ_SAMPLES,
        delay_s: float = DEFAULT_OFFSET_READ_DELAY_S,
    ) -> float:
        if self.handle is None:
            raise RuntimeError("LabJack is not connected.")

        self.configure_analog_inputs(waveform_range_v=waveform_range_v)
        readings: list[float] = []
        for _ in range(max(1, int(samples))):
            value = float(ljm.eReadName(self.handle, OFFSET_CHANNEL))
            if value > -9998.0 and math.isfinite(value):
                readings.append(value)
            if delay_s > 0:
                time.sleep(delay_s)

        if not readings:
            raise RuntimeError("No valid AIN1 offset readings were captured.")
        return float(np.median(np.asarray(readings, dtype=float)))

    def read_waveform_stream(
        self,
        sample_rate_hz: float,
        expected_frequency_hz: float,
        sync_edge: str,
        max_capture_cycles: int = DEFAULT_MAX_CAPTURE_CYCLES,
        stability_tolerance: float = DEFAULT_STABILITY_TOLERANCE,
        stability_window_cycles: int = DEFAULT_STABILITY_WINDOW_CYCLES,
        settle_cycles: int = DEFAULT_SETTLE_CYCLES,
        waveform_range_v: float = LABJACK_AIN0_RANGE_OPTIONS[DEFAULT_WAVEFORM_INPUT_RANGE_LABEL],
    ) -> tuple[np.ndarray, np.ndarray, float]:
        if self.handle is None:
            raise RuntimeError("LabJack is not connected.")

        self.configure_analog_inputs(waveform_range_v=waveform_range_v)
        # Direct pyroelectric outputs can be high impedance. Sample AIN0 more
        # than once after the sync channel and keep the final, settled value.
        scan_names = [SYNC_CHANNEL] + [WAVEFORM_CHANNEL] * WAVEFORM_SETTLING_READS
        scan_list = ljm.namesToAddresses(len(scan_names), scan_names)[0]
        scans_per_read = max(20, int(sample_rate_hz / 10.0))
        target_scans = int(math.ceil((max_capture_cycles / expected_frequency_hz) * sample_rate_hz))

        waveform: list[float] = []
        sync: list[float] = []
        actual_scan_rate = float(sample_rate_hz)
        stable_check_cycles = max(0, int(settle_cycles)) + (stability_window_cycles * 2)

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
                sync.extend(arr[:, 0].tolist())
                waveform.extend(arr[:, -1].tolist())
                if len(waveform) >= int((stable_check_cycles / expected_frequency_hz) * actual_scan_rate):
                    metrics = analyze_waveform(
                        np.asarray(waveform, dtype=float),
                        np.asarray(sync, dtype=float),
                        actual_scan_rate,
                        am502_gain=1.0,
                        sync_edge=sync_edge,
                        expected_frequency_hz=expected_frequency_hz,
                        stability_tolerance=stability_tolerance,
                        stability_window_cycles=stability_window_cycles,
                        settle_cycles=settle_cycles,
                        input_range_v=waveform_range_v,
                        expect_offset_on_waveform=False,
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
        return False, "LabJack LJM Python library is not available."
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
        return False, "No T7 detected. Check USB/power, then press Connect."
    return True, "T7 detected. Press Connect when the fixture is wired."


def suggest_scope_verification_choice(final_result: FinalResult | None, operator_polarity: str = "") -> str:
    if operator_polarity == "Bad":
        return "RP - Reversed polarity"
    if final_result is None or final_result.passed:
        return DEFAULT_SCOPE_VERIFICATION_CHOICE

    reason_text = " ".join(final_result.fail_reasons).lower()
    if "sensitivity too low" in reason_text:
        return "LS - Low sensitivity"
    if "offset out of range" in reason_text:
        offset = final_result.offset_v
        if offset is not None and offset > OFFSET_MAX_V:
            return "HO - High offset"
        if offset is not None and offset < OFFSET_MIN_V:
            return "LO - Low offset"
        return "D - No offset"
    if "polarity" in reason_text:
        return "RP - Reversed polarity"
    if "stabilize" in reason_text:
        return "Unstable - Unstable"
    return "N - Noisy"


def save_failed_waveform_snapshot(
    lot_number: str,
    sensor_id: str,
    metrics: WaveformMetrics | None,
    final_result: FinalResult,
) -> Path | None:
    if metrics is None or metrics.waveform_v.size == 0:
        return None

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None

    snapshot_dir = results_root_dir() / "waveform_snapshots" / f"lot_{safe_filename_part(lot_number)}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = snapshot_dir / f"{safe_filename_part(sensor_id)}_{timestamp}.png"

    time_axis = np.arange(metrics.waveform_v.size, dtype=float) / max(metrics.sample_rate_hz, 1.0)
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    fig.suptitle(f"{MODEL_NAME} {sensor_id} failed verification")
    axes[0].plot(time_axis, metrics.waveform_v, color="#0284c7", linewidth=1.0)
    axes[0].set_ylabel("AIN0 V")
    axes[0].grid(True, alpha=0.25)
    axes[1].plot(time_axis[: metrics.sync_v.size], metrics.sync_v, color="#ca8a04", linewidth=1.0)
    axes[1].set_ylabel("AIN2 V")
    axes[1].set_xlabel("Seconds")
    axes[1].grid(True, alpha=0.25)

    detail_lines = [
        f"Sensitivity: {metrics.sensitivity_mv:.2f} mV",
        f"Polarity: {metrics.polarity}",
        "Failures: " + "; ".join(final_result.fail_reasons),
    ]
    axes[0].text(
        0.01,
        0.98,
        "\n".join(detail_lines),
        transform=axes[0].transAxes,
        va="top",
        ha="left",
        fontsize=9,
        bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "#cbd5e1"},
    )
    fig.tight_layout()
    fig.savefig(snapshot_path, dpi=140)
    plt.close(fig)
    return snapshot_path


class TesterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Eltec 406MCA Scope Verification Tester")
        self.minsize(1180, 780)

        self.device: LabJackT7 | None = None
        self.offset_v: float | None = None
        self.last_waveform_metrics: WaveformMetrics | None = None
        self.last_result: FinalResult | None = None
        self.last_sensor_id = "UNLABELED"
        self.last_filter_setup = DEFAULT_FILTER_SETUP
        self.last_am502_gain = DEFAULT_AM502_GAIN
        self.last_labjack_range_label = DEFAULT_WAVEFORM_INPUT_RANGE_LABEL
        self.pending_offset_v: float | None = None
        self.result_saved = True
        self.busy = False

        self._build_variables()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.startup_probe)

    def _build_variables(self) -> None:
        self.sensor_id_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar(value=DEFAULT_FILTER_SETUP)
        self.am502_gain_var = tk.StringVar(value=f"{DEFAULT_AM502_GAIN:.0f}")
        self.labjack_range_var = tk.StringVar(value=DEFAULT_WAVEFORM_INPUT_RANGE_LABEL)
        self.sync_edge_var = tk.StringVar(value=PROCEDURE_SYNC_EDGE)
        self.csv_path_var = tk.StringVar(value=str(default_results_path()))
        self.scope_verification_var = tk.StringVar(value=DEFAULT_SCOPE_VERIFICATION_CHOICE)
        self.scope_offset_var = tk.StringVar(value="")
        self.scope_sensitivity_var = tk.StringVar(value="")

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

        title = ttk.Label(header, text="Eltec 406MCA Scope Verification Tester", font=("Segoe UI", 20, "bold"))
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
        self._add_labeled_static(controls, 2, "Sync edge", PROCEDURE_SYNC_EDGE)
        self._add_labeled_entry(controls, 3, "External gain", self.am502_gain_var)
        self._add_labeled_combo(
            controls,
            4,
            "AIN0/AIN1 range",
            self.labjack_range_var,
            list(LABJACK_AIN0_RANGE_OPTIONS.keys()),
        )
        self._add_labeled_combo(
            controls,
            5,
            "Scope tag",
            self.scope_verification_var,
            SCOPE_VERIFICATION_CHOICES,
        )
        self._add_labeled_entry(controls, 6, "Actual offset V", self.scope_offset_var)
        self._add_labeled_entry(controls, 7, "Actual sensitivity mV", self.scope_sensitivity_var)

        ttk.Label(controls, text="CSV log").grid(row=8, column=0, sticky="w", pady=(14, 2))
        csv_entry = ttk.Entry(controls, textvariable=self.csv_path_var, width=38)
        csv_entry.grid(row=9, column=0, columnspan=2, sticky="ew")

        self.connect_button = ttk.Button(
            controls,
            text="Connect",
            command=self.connect_labjack,
            style="Large.TButton",
        )
        self.connect_button.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(18, 8))

        self.offset_button = ttk.Button(
            controls,
            text="Read Offset",
            command=self.start_offset_read,
            style="Large.TButton",
        )
        self.offset_button.grid(row=11, column=0, columnspan=2, sticky="ew", pady=8)

        self.signal_button = ttk.Button(
            controls,
            text="Capture Signal",
            command=self.start_signal_capture,
            style="Large.TButton",
            state="disabled",
        )
        self.signal_button.grid(row=12, column=0, columnspan=2, sticky="ew", pady=8)

        self.save_button = ttk.Button(
            controls,
            text="Save Result",
            command=self.save_last_result,
            style="Large.TButton",
            state="disabled",
        )
        self.save_button.grid(row=13, column=0, columnspan=2, sticky="ew", pady=8)

        wiring = (
            "Wiring:\n"
            "AIN0 = AM502 x100 signal output\n"
            "AIN1 = offset output\n"
            "AIN2 = 10 Hz chopper signal from black body emitter\n\n"
            "Offset is read from AIN1 before the AIN0 signal capture.\n"
            f"Signal waits for a stable {DEFAULT_STABILITY_WINDOW_CYCLES}-cycle window.\n\n"
            "Default setup is AM502 x100 with LabJack +/-10 V (x1).\n"
            "Leave actual fields blank unless the scope reading differs.\n\n"
            "Close LJStreamM/Kipling before using hardware mode."
        )
        ttk.Label(controls, text=wiring, justify="left").grid(row=14, column=0, columnspan=2, sticky="w", pady=(18, 0))

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
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=32)
        combo.grid(row=row, column=1, sticky="ew", pady=5)

    def _add_labeled_static(self, parent: ttk.Frame, row: int, label: str, value: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 8))
        ttk.Label(parent, text=value, font=("Segoe UI", 11, "bold")).grid(row=row, column=1, sticky="w", pady=5)

    def _metric_card(self, parent: ttk.Frame, column: int, label: str, value_var: tk.StringVar) -> tk.Frame:
        frame = tk.Frame(parent, bg="#ffffff", bd=1, relief="solid")
        frame.grid(row=0, column=column, sticky="ew", padx=4)
        tk.Label(frame, text=label, bg="#ffffff", fg="#64748b", font=("Segoe UI", 10, "bold")).pack(pady=(8, 0))
        tk.Label(frame, textvariable=value_var, bg="#ffffff", fg="#111827", font=("Segoe UI", 16, "bold")).pack(
            pady=(2, 10)
        )
        return frame

    def startup_probe(self) -> None:
        _ok, message = probe_labjack_status()
        self.status_var.set(message)

    def connect_labjack(self) -> None:
        range_label = self.labjack_range_var.get()
        try:
            waveform_range_v = labjack_ain0_range_from_label(range_label)
        except ValueError as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return
        range_warning = labjack_range_offset_warning(waveform_range_v, range_label)
        if range_warning is not None:
            messagebox.showerror("AIN1 range too small for offset", range_warning)
            return
        self.set_busy(True)

        def worker() -> None:
            try:
                if self.device is None:
                    self.device = LabJackT7()
                self.device.connect()
                self.device.configure_analog_inputs(waveform_range_v=waveform_range_v)
            except Exception as exc:
                self.after(0, lambda exc=exc: self.hardware_error(exc))
            else:
                self.after(
                    0,
                    lambda: self.status_var.set(
                        f"Connected to LabJack T7. AIN0/AIN1 range is {range_label}."
                    ),
                )
            finally:
                self.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def hardware_error(self, exc: Exception) -> None:
        text = str(exc)
        if "1230" in text or "CLAIMED" in text.upper():
            text = "The T7 is claimed by another LabJack program. Close LJStreamM/Kipling and try Connect again."
        self.status_var.set(text)
        messagebox.showerror("LabJack connection problem", text)

    def update_offset_card(self, offset: float) -> bool:
        self.offset_v = offset
        if OFFSET_MIN_V <= offset <= OFFSET_MAX_V:
            self.offset_display_var.set(f"{offset:.3f} V PASS")
            self.set_card_color(self.offset_card, "#dcfce7")
            return True
        self.offset_display_var.set(f"{offset:.3f} V FAIL")
        self.set_card_color(self.offset_card, "#fee2e2")
        return False

    def read_scope_verification(self) -> ScopeVerification:
        tag, reason = split_scope_verification_choice(self.scope_verification_var.get())
        if not tag:
            raise ValueError("Choose a scope tag before saving.")

        def read_optional_float(text: str, label: str) -> float | None:
            text = text.strip()
            if not text:
                return None
            try:
                value = float(text)
            except ValueError as exc:
                raise ValueError(f"{label} must be blank or a number.") from exc
            if not math.isfinite(value):
                raise ValueError(f"{label} must be a finite number.")
            return value

        scope_offset_v = read_optional_float(self.scope_offset_var.get(), "Actual offset")
        scope_sensitivity_mv = read_optional_float(self.scope_sensitivity_var.get(), "Actual sensitivity")

        offset_delta_v: float | None = None
        sensitivity_delta_mv: float | None = None
        if self.last_result is not None:
            if scope_offset_v is not None and self.last_result.offset_v is not None:
                offset_delta_v = scope_offset_v - self.last_result.offset_v
            if scope_sensitivity_mv is not None and self.last_result.sensitivity_mv is not None:
                sensitivity_delta_mv = scope_sensitivity_mv - self.last_result.sensitivity_mv

        return ScopeVerification(
            tag=tag,
            reason=reason,
            offset_v=scope_offset_v,
            offset_delta_v=offset_delta_v,
            sensitivity_mv=scope_sensitivity_mv,
            sensitivity_delta_mv=sensitivity_delta_mv,
        )

    def save_last_result(self) -> None:
        if self.last_result is None:
            messagebox.showinfo("Nothing to save", "Run a test before saving a result.")
            return

        try:
            scope_verification = self.read_scope_verification()
        except ValueError as exc:
            messagebox.showerror("Invalid scope check", str(exc))
            return

        try:
            append_result_csv(
                Path(self.csv_path_var.get()),
                sensor_id=self.last_sensor_id,
                filter_setup=self.last_filter_setup,
                am502_gain=self.last_am502_gain,
                labjack_range_label=self.last_labjack_range_label,
                final_result=self.last_result,
                scope_verification=scope_verification,
            )
        except Exception as exc:
            messagebox.showerror("Could not save result", str(exc))
            return

        self.result_saved = True
        self.pending_offset_v = None
        self.update_save_button_state()
        self.status_var.set(f"Saved scope-verified result to {self.csv_path_var.get()}")

    def start_offset_read(self) -> None:
        try:
            gain = float(self.am502_gain_var.get())
        except ValueError:
            messagebox.showerror("Invalid setup", "External gain must be a number.")
            return

        if gain <= 0:
            messagebox.showerror("Invalid setup", "External gain must be positive.")
            return

        range_label = self.labjack_range_var.get()
        try:
            waveform_range_v = labjack_ain0_range_from_label(range_label)
        except ValueError as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return
        range_warning = labjack_range_offset_warning(waveform_range_v, range_label)
        if range_warning is not None:
            messagebox.showerror("AIN1 range too small for offset", range_warning)
            return

        sensor_id = self.sensor_id_var.get().strip() or "UNLABELED"
        filter_setup = self.filter_var.get()
        continuing_unsaved = (self.last_result is not None and not self.result_saved) or (
            self.last_result is None and self.pending_offset_v is not None
        )
        self.last_sensor_id = sensor_id
        self.last_filter_setup = filter_setup
        self.last_am502_gain = gain
        self.last_labjack_range_label = range_label
        if not continuing_unsaved:
            self.last_waveform_metrics = None
            self.last_result = None
            self.pending_offset_v = None
            self.result_saved = True
            self.scope_verification_var.set(DEFAULT_SCOPE_VERIFICATION_CHOICE)
            self.scope_offset_var.set("")
            self.scope_sensitivity_var.set("")
        self.update_save_button_state()

        self.set_busy(True)
        self.overall_display_var.set("READING OFFSET")
        self.set_overall_color("#e8edf3", "#1f2937")
        self.offset_display_var.set("Reading AIN1")
        if self.last_waveform_metrics is None:
            self.sensitivity_display_var.set("Not measured")
            self.polarity_display_var.set("Not measured")
            self.frequency_display_var.set("Not measured")
        self.write_note(
            [
                "Reading DC offset from AIN1...",
                "After the offset is captured, set the test bed for signal and click Capture Signal.",
                f"AIN0/AIN1 range is {range_label}; external gain correction is x{gain:g}.",
            ]
        )

        def worker() -> None:
            try:
                self.ensure_connected()
                offset_v = self.device.read_offset_voltage(waveform_range_v=waveform_range_v)
            except Exception as exc:
                self.after(0, lambda exc=exc: self.hardware_error(exc))
            else:
                self.after(0, lambda: self.on_offset_complete(offset_v))
            finally:
                self.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def on_offset_complete(self, offset_v: float) -> None:
        self.offset_v = offset_v
        self.pending_offset_v = offset_v

        offset_ok = OFFSET_MIN_V <= offset_v <= OFFSET_MAX_V
        self.offset_display_var.set(f"{offset_v:.3f} V" + (" PASS" if offset_ok else " FAIL"))
        self.set_card_color(self.offset_card, "#dcfce7" if offset_ok else "#fee2e2")
        if self.last_waveform_metrics is not None:
            self.last_waveform_metrics.offset_v = offset_v
            final = evaluate_result(offset_v, self.last_waveform_metrics, self.last_filter_setup)
            self.on_test_complete(
                self.last_waveform_metrics,
                final,
                status_message="AIN1 offset reread. You can reread offset/signal again or save the result.",
            )
            return

        self.overall_display_var.set("READY FOR SIGNAL")
        self.set_overall_color("#e8edf3", "#1f2937")
        self.status_var.set("AIN1 offset captured. Set the test bed for signal, then click Capture Signal.")
        self.write_note(
            [
                f"AIN1 offset captured: {offset_v:.3f} V.",
                "Set the test bed for signal, then click Capture Signal.",
                f"Signal capture waits for a stable {DEFAULT_STABILITY_WINDOW_CYCLES}-cycle window.",
            ]
        )
        self.update_save_button_state()

    def start_signal_capture(self) -> None:
        if self.pending_offset_v is None:
            messagebox.showinfo("Offset needed", "Read the offset before capturing the signal.")
            return

        offset_v = self.pending_offset_v
        filter_setup = self.last_filter_setup
        gain = self.last_am502_gain
        range_label = self.last_labjack_range_label
        try:
            waveform_range_v = labjack_ain0_range_from_label(range_label)
        except ValueError as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return

        self.set_busy(True)
        self.overall_display_var.set("WAITING FOR STABLE WAVE")
        self.set_overall_color("#e8edf3", "#1f2937")
        self.sensitivity_display_var.set("Measuring")
        self.polarity_display_var.set("Measuring")
        self.frequency_display_var.set("Measuring")
        self.write_note(
            [
                "Capturing AIN0 signal waveform...",
                f"Signal capture waits for a stable {DEFAULT_STABILITY_WINDOW_CYCLES}-cycle window.",
                f"Polarity is referenced to the {PROCEDURE_SYNC_EDGE.lower()} edge of AIN2.",
                f"AIN2 should be the incoming black body chopper signal at {EXPECTED_FREQUENCY_HZ:g} Hz.",
                f"AIN0/AIN1 range is {range_label}; external gain correction is x{gain:g}.",
            ]
        )

        def worker() -> None:
            try:
                self.ensure_connected()
                waveform, sync, actual_rate = self.device.read_waveform_stream(
                    sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
                    expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
                    sync_edge=PROCEDURE_SYNC_EDGE,
                    waveform_range_v=waveform_range_v,
                )

                metrics = analyze_waveform(
                    waveform_v=waveform,
                    sync_v=sync,
                    sample_rate_hz=actual_rate,
                    am502_gain=gain,
                    sync_edge=PROCEDURE_SYNC_EDGE,
                    input_range_v=waveform_range_v,
                    expect_offset_on_waveform=False,
                )
                metrics.offset_v = offset_v
                final = evaluate_result(offset_v, metrics, filter_setup)
            except Exception as exc:
                self.after(0, lambda exc=exc: self.hardware_error(exc))
            else:
                self.after(0, lambda: self.on_test_complete(metrics, final))
            finally:
                self.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def on_test_complete(
        self,
        metrics: WaveformMetrics,
        final: FinalResult,
        status_message: str = "Test complete. Reread offset/signal, enter optional actual values, or save.",
    ) -> None:
        self.last_waveform_metrics = metrics
        self.last_result = final
        self.result_saved = False
        self.pending_offset_v = final.offset_v
        filter_setup = self.last_filter_setup

        offset_ok = final.offset_v is not None and OFFSET_MIN_V <= final.offset_v <= OFFSET_MAX_V
        offset_text = "Not measured" if final.offset_v is None else f"{final.offset_v:.3f} V"
        self.offset_display_var.set(offset_text + (" PASS" if offset_ok else " FAIL"))
        self.sensitivity_display_var.set(f"{metrics.sensitivity_mv:.2f} mV")
        if metrics.polarity_confidence is None:
            self.polarity_display_var.set(metrics.polarity)
        else:
            self.polarity_display_var.set(f"{metrics.polarity} {metrics.polarity_confidence * 100:.0f}%")
        if metrics.measured_frequency_hz is None:
            self.frequency_display_var.set("No sync")
        else:
            self.frequency_display_var.set(f"{metrics.measured_frequency_hz:.3f} Hz")

        self.set_card_color(self.offset_card, "#dcfce7" if offset_ok else "#fee2e2")
        self.set_card_color(
            self.sensitivity_card,
            "#dcfce7" if metrics.sensitivity_mv >= FILTER_SPECS_MV[filter_setup] else "#fee2e2",
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

        self.write_messages(final.fail_reasons, final.warnings, metrics)
        self.redraw_waveform()
        self.update_save_button_state()
        self.status_var.set(status_message)

    def ensure_connected(self) -> None:
        if self.device is None:
            self.device = LabJackT7()
        self.device.connect()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.connect_button.configure(state="disabled" if busy else "normal")
        self.update_save_button_state()

    def update_save_button_state(self) -> None:
        action_state = "disabled" if self.busy else "normal"
        self.offset_button.configure(state=action_state)
        signal_ready = self.pending_offset_v is not None and not self.busy
        self.signal_button.configure(state="normal" if signal_ready else "disabled")
        pending_result = self.last_result is not None and not self.result_saved and not self.busy
        self.save_button.configure(state="normal" if pending_result else "disabled")

    def set_overall_color(self, bg: str, fg: str) -> None:
        self.overall_card.configure(bg=bg, fg=fg)

    def set_card_color(self, card: tk.Frame, bg: str) -> None:
        card.configure(bg=bg)
        for child in card.winfo_children():
            child.configure(bg=bg)

    def write_messages(
        self,
        fail_reasons: list[str],
        warnings: list[str],
        waveform_metrics: WaveformMetrics | None = None,
    ) -> None:
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
        polarity_detail = format_polarity_detail(waveform_metrics)
        if polarity_detail is not None:
            self.message_text.insert("end", "\nPOLARITY CHECK\n")
            self.message_text.insert("end", f"- {waveform_metrics.polarity}: {polarity_detail}.\n")
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

        wave_points = []
        for px, py in zip(x, wave_y):
            wave_points.extend([float(px), float(py)])
        canvas.create_line(wave_points, fill="#38bdf8", width=2)

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
        canvas.create_text(
            12,
            sync_top,
            anchor="nw",
            text="AIN2 chopper sync",
            fill="#facc15",
            font=("Segoe UI", 10, "bold"),
        )
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


class GuidedTesterApp(tk.Tk):
    LOT_STEP = "lot"
    LOAD_STEP = "load"
    OFFSET_STEP = "offset"
    SENSITIVITY_STEP = "sensitivity"
    RESULT_STEP = "result"

    def __init__(self) -> None:
        super().__init__()
        self.title("Eltec 406MCA Batch Scope Verification")
        self.minsize(980, 700)

        self.device: LabJackT7 | None = None
        self.busy = False
        self.step = self.LOT_STEP

        self.lot_number = ""
        self.current_sensor_number = 0
        self.current_sensor_id = ""
        self.offset_read_started = False
        self.signal_capture_started = False
        self.result_saved = True
        self.advance_after_offset_capture = False
        self.advance_after_signal_capture = False
        self.default_focus_widget: tk.Widget | None = None

        self.offset_v: float | None = None
        self.pending_offset_v: float | None = None
        self.last_waveform_metrics: WaveformMetrics | None = None
        self.last_result: FinalResult | None = None
        self.last_filter_setup = DEFAULT_FILTER_SETUP
        self.last_am502_gain = DEFAULT_AM502_GAIN
        self.last_labjack_range_label = DEFAULT_WAVEFORM_INPUT_RANGE_LABEL

        self._build_variables()
        self._build_ui()
        self.bind("<Return>", self.on_enter_key)
        self.bind("<KP_Enter>", self.on_enter_key)
        self.bind("<Escape>", self.on_escape_key)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.startup_probe)

    def _build_variables(self) -> None:
        self.lot_number_var = tk.StringVar(value="")
        self.current_sensor_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar(value=DEFAULT_FILTER_SETUP)
        self.am502_gain_var = tk.StringVar(value=f"{DEFAULT_AM502_GAIN:.0f}")
        self.labjack_range_var = tk.StringVar(value=DEFAULT_WAVEFORM_INPUT_RANGE_LABEL)
        self.csv_path_var = tk.StringVar(value=str(default_results_path()))
        self.scope_offset_var = tk.StringVar(value="")
        self.scope_sensitivity_var = tk.StringVar(value="")
        self.operator_polarity_var = tk.StringVar(value="Good")
        self.scope_verification_var = tk.StringVar(value=DEFAULT_SCOPE_VERIFICATION_CHOICE)

        self.status_var = tk.StringVar(value="Checking LabJack...")
        self.step_title_var = tk.StringVar(value="")
        self.step_instruction_var = tk.StringVar(value="")
        self.offset_display_var = tk.StringVar(value="Not measured")
        self.sensitivity_display_var = tk.StringVar(value="Not measured")
        self.polarity_display_var = tk.StringVar(value="Not measured")
        self.frequency_display_var = tk.StringVar(value="Not measured")
        self.overall_display_var = tk.StringVar(value="READY")
        self.notes_var = tk.StringVar(value="")

    def _build_ui(self) -> None:
        self.configure(bg="#f4f6f8")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabel", background="#f4f6f8", font=("Segoe UI", 14))
        style.configure("Step.TLabel", background="#f4f6f8", font=("Segoe UI", 15))
        style.configure("CurrentStep.TLabel", background="#dbeafe", foreground="#1e3a8a", font=("Segoe UI", 15, "bold"))
        style.configure("DoneStep.TLabel", background="#dcfce7", foreground="#14532d", font=("Segoe UI", 15, "bold"))
        style.configure("Large.TButton", font=("Segoe UI", 17, "bold"), padding=(18, 14))
        style.configure("Small.TButton", font=("Segoe UI", 13, "bold"), padding=(10, 8))
        style.configure("TCombobox", font=("Segoe UI", 18))
        style.configure("TEntry", font=("Segoe UI", 20))
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 20))

        header = ttk.Frame(self, padding=(18, 14, 18, 12))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        self.create_eltec_logo(header).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 18))
        ttk.Label(header, text="Eltec 406MCA Batch Scope Verification", font=("Segoe UI", 26, "bold")).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(header, textvariable=self.status_var, font=("Segoe UI", 13)).grid(row=1, column=1, sticky="w", pady=(4, 0))

        body = ttk.Frame(self, padding=(18, 0, 18, 12))
        body.grid(row=1, column=0, sticky="nsew")
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(body, padding=(0, 8, 14, 8))
        sidebar.grid(row=0, column=0, sticky="nsw")
        ttk.Label(sidebar, text="Progress", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.progress_labels: dict[str, ttk.Label] = {}
        for row, (step, text) in enumerate(
            [
                (self.LOT_STEP, "1. Lot number"),
                (self.LOAD_STEP, "2. Load sensor"),
                (self.OFFSET_STEP, "3. Offset"),
                (self.SENSITIVITY_STEP, "4. Sensitivity"),
                (self.RESULT_STEP, "5. Save"),
            ],
            start=1,
        ):
            label = ttk.Label(sidebar, text=text, style="Step.TLabel", padding=(10, 8))
            label.grid(row=row, column=0, sticky="ew", pady=3)
            self.progress_labels[step] = label

        self.content = ttk.Frame(body, padding=22)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(3, weight=1)

        self.render_step()

    def create_eltec_logo(self, parent: ttk.Frame) -> tk.Canvas:
        logo = tk.Canvas(parent, width=150, height=76, bg="#f4f6f8", highlightthickness=0)
        logo.create_oval(38, 8, 132, 68, fill="#1d4aa8", outline="#1d4aa8")
        logo.create_line(48, 20, 122, 20, fill="#ffffff", width=2)
        logo.create_line(48, 56, 122, 56, fill="#ffffff", width=2)
        logo.create_rectangle(12, 29, 142, 48, fill="#f4f6f8", outline="#f4f6f8")
        logo.create_text(76, 39, text="ELTEC", fill="#ef2b45", font=("Segoe UI", 26, "bold italic"))
        logo.create_text(119, 14, text="TM", fill="#1d4aa8", font=("Segoe UI", 6, "bold"))
        return logo

    def clear_content(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()

    def render_step(self) -> None:
        self.clear_content()
        self.default_focus_widget = None
        self.update_progress_labels()
        if self.step == self.LOT_STEP:
            self.render_lot_step()
        elif self.step == self.LOAD_STEP:
            self.render_load_step()
        elif self.step == self.OFFSET_STEP:
            self.render_offset_step()
        elif self.step == self.SENSITIVITY_STEP:
            self.render_sensitivity_step()
        else:
            self.render_result_step()
        self.render_navigation()
        self.update_navigation_state()
        self.after_idle(self.focus_default_widget)

    def render_navigation(self) -> None:
        footer = ttk.Frame(self.content)
        footer.grid(row=2, column=0, sticky="ew", pady=(22, 0))
        footer.columnconfigure(0, weight=1)
        self.back_button = ttk.Button(footer, text="Back", command=self.go_back, style="Large.TButton")
        self.back_button.grid(row=0, column=0, sticky="w")
        self.secondary_button = ttk.Button(footer, text="Save + Exit Batch", command=self.save_and_end_batch, style="Large.TButton")
        self.secondary_button.grid(row=0, column=1, sticky="e", padx=(0, 10))
        self.primary_button = ttk.Button(footer, text="Next", command=self.go_next, style="Large.TButton")
        self.primary_button.grid(row=0, column=2, sticky="e")

        spacer = ttk.Frame(self.content)
        spacer.grid(row=3, column=0, sticky="nsew")

    def set_default_focus(self, widget: tk.Widget) -> None:
        self.default_focus_widget = widget

    def focus_default_widget(self) -> None:
        widget = self.default_focus_widget
        if self.busy:
            return
        if widget is None:
            self.focus_set()
            return
        if not widget.winfo_exists():
            return
        widget.focus_set()
        try:
            widget.selection_range(0, tk.END)
            widget.icursor(tk.END)
        except (AttributeError, tk.TclError):
            pass

    def update_progress_labels(self) -> None:
        order = [self.LOT_STEP, self.LOAD_STEP, self.OFFSET_STEP, self.SENSITIVITY_STEP, self.RESULT_STEP]
        current_index = order.index(self.step)
        for index, step in enumerate(order):
            style_name = "CurrentStep.TLabel" if index == current_index else "DoneStep.TLabel" if index < current_index else "Step.TLabel"
            self.progress_labels[step].configure(style=style_name)

    def render_lot_step(self) -> None:
        self.step_title_var.set("Please enter Lot number")
        ttk.Label(self.content, textvariable=self.step_title_var, font=("Segoe UI", 34, "bold")).grid(row=0, column=0, sticky="w")

        form = ttk.Frame(self.content)
        form.grid(row=1, column=0, sticky="new", pady=(28, 0))
        form.columnconfigure(1, weight=1)
        lot_entry = self._add_labeled_entry(form, 0, "Lot number", self.lot_number_var, width=28)
        self.set_default_focus(lot_entry)
        self._add_labeled_combo(form, 1, "Filter", self.filter_var, list(FILTER_SPECS_MV.keys()), width=46)

    def render_load_step(self) -> None:
        self.step_title_var.set(f"Load sensor {self.current_sensor_id}")
        ttk.Label(self.content, textvariable=self.step_title_var, font=("Segoe UI", 36, "bold")).grid(row=0, column=0, sticky="w")

        panel = tk.Frame(self.content, bg="#dbeafe", bd=0, highlightthickness=0)
        panel.grid(row=1, column=0, sticky="ew", pady=(28, 0))
        panel.columnconfigure(1, weight=1)

        icon = tk.Canvas(panel, width=170, height=120, bg="#dbeafe", highlightthickness=0)
        icon.grid(row=0, column=0, padx=(26, 18), pady=24)
        icon.create_rectangle(30, 22, 140, 94, fill="#1d4aa8", outline="#1e3a8a", width=3)
        icon.create_rectangle(54, 42, 116, 74, fill="#f8fafc", outline="#bfdbfe", width=2)
        icon.create_oval(62, 47, 108, 69, fill="#ef2b45", outline="#991b1b", width=2)
        icon.create_line(24, 98, 146, 98, fill="#1e3a8a", width=5)
        icon.create_text(85, 105, text="RIG", fill="#1e3a8a", font=("Segoe UI", 13, "bold"))

        message = ttk.Label(
            panel,
            text="Place the sensor in the testing rig",
            font=("Segoe UI", 30, "bold"),
            background="#dbeafe",
            foreground="#1e3a8a",
        )
        message.grid(row=0, column=1, sticky="w", padx=(0, 26), pady=24)

    def render_offset_step(self) -> None:
        self.step_title_var.set(f"{self.current_sensor_id}: enter offset reading")
        ttk.Label(self.content, textvariable=self.step_title_var, font=("Segoe UI", 32, "bold")).grid(row=0, column=0, sticky="w")

        panel = ttk.Frame(self.content)
        panel.grid(row=1, column=0, sticky="new", pady=(28, 0))
        panel.columnconfigure(1, weight=1)
        offset_entry = self._add_labeled_entry(panel, 0, "Offset V", self.scope_offset_var, width=18)
        self.set_default_focus(offset_entry)

    def render_sensitivity_step(self) -> None:
        self.step_title_var.set(f"{self.current_sensor_id}: switch to testing sensitivity")
        ttk.Label(self.content, textvariable=self.step_title_var, font=("Segoe UI", 32, "bold")).grid(row=0, column=0, sticky="w")

        panel = ttk.Frame(self.content)
        panel.grid(row=1, column=0, sticky="new", pady=(28, 0))
        panel.columnconfigure(1, weight=1)
        sensitivity_entry = self._add_labeled_entry(panel, 0, "Scope sensitivity mV", self.scope_sensitivity_var, width=18)
        self.set_default_focus(sensitivity_entry)
        self._add_labeled_combo(panel, 1, "Polarity", self.operator_polarity_var, ["Good", "Bad"], width=12)

    def render_result_step(self) -> None:
        overall_failed = self.current_sensor_failed()
        self.overall_display_var.set("FAIL" if overall_failed else "PASS")
        title_color = "#991b1b" if overall_failed else "#14532d"
        ttk.Label(
            self.content,
            text=f"{self.current_sensor_id}: {self.overall_display_var.get()}",
            font=("Segoe UI", 36, "bold"),
            foreground=title_color,
        ).grid(row=0, column=0, sticky="w")

        panel = ttk.Frame(self.content)
        panel.grid(row=1, column=0, sticky="new", pady=(28, 0))
        panel.columnconfigure(1, weight=1)
        self._add_labeled_static(panel, 0, "Offset V", self.scope_offset_var.get())
        if self.is_offset_only_failure():
            self._add_labeled_static(panel, 1, "Scope sensitivity mV", "Skipped")
            self._add_labeled_static(panel, 2, "Polarity", "Skipped")
        else:
            self._add_labeled_static(panel, 1, "Scope sensitivity mV", self.scope_sensitivity_var.get())
            self._add_labeled_static(panel, 2, "Polarity", self.operator_polarity_var.get())

        if overall_failed:
            self._add_labeled_combo(
                panel,
                3,
                "Failure reason",
                self.scope_verification_var,
                SCOPE_VERIFICATION_CHOICES,
                width=54,
                dropdown_rows=8,
            )
        else:
            self.scope_verification_var.set(DEFAULT_SCOPE_VERIFICATION_CHOICE)

    def _add_labeled_entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        width: int = 18,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label, font=("Segoe UI", 20, "bold")).grid(
            row=row, column=0, sticky="w", pady=12, padx=(0, 18)
        )
        entry = ttk.Entry(parent, textvariable=variable, width=width, font=("Segoe UI", 22))
        entry.grid(row=row, column=1, sticky="w", pady=12)
        return entry

    def _add_labeled_combo(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        values: list[str],
        width: int = 28,
        dropdown_rows: int | None = None,
    ) -> ttk.Combobox:
        ttk.Label(parent, text=label, font=("Segoe UI", 20, "bold")).grid(
            row=row, column=0, sticky="w", pady=12, padx=(0, 18)
        )
        combo = ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            state="readonly",
            width=width,
            height=dropdown_rows if dropdown_rows is not None else min(max(len(values), 6), 10),
            font=("Segoe UI", 20),
        )
        combo.grid(row=row, column=1, sticky="w", pady=12)
        return combo

    def _add_labeled_static(self, parent: ttk.Frame, row: int, label: str, value: str) -> None:
        ttk.Label(parent, text=label, font=("Segoe UI", 20, "bold")).grid(
            row=row, column=0, sticky="w", pady=12, padx=(0, 18)
        )
        ttk.Label(parent, text=value, font=("Segoe UI", 22, "bold")).grid(row=row, column=1, sticky="w", pady=12)

    def _add_labeled_value_var(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, font=("Segoe UI", 20, "bold")).grid(
            row=row, column=0, sticky="w", pady=12, padx=(0, 18)
        )
        ttk.Label(parent, textvariable=variable, font=("Segoe UI", 22, "bold")).grid(
            row=row, column=1, sticky="w", pady=12
        )

    def button_is_enabled(self, button: ttk.Button) -> bool:
        return button.winfo_exists() and "disabled" not in button.state()

    def on_enter_key(self, _event: tk.Event) -> str | None:
        if isinstance(_event.widget, ttk.Button):
            return None
        if self.busy or not self.button_is_enabled(self.primary_button):
            return "break"
        self.go_next()
        return "break"

    def on_escape_key(self, _event: tk.Event) -> str | None:
        if self.step == self.RESULT_STEP and self.button_is_enabled(self.secondary_button):
            self.save_and_end_batch()
            return "break"
        return None

    def is_offset_only_failure(self) -> bool:
        if self.last_result is None or self.last_result.waveform_metrics is not None:
            return False
        return any("Offset out of range" in reason for reason in self.last_result.fail_reasons)

    def make_offset_only_result(self, offset_v: float) -> FinalResult:
        return FinalResult(
            passed=False,
            offset_v=offset_v,
            sensitivity_mv=None,
            polarity=UNKNOWN_POLARITY,
            fail_reasons=[
                f"Offset out of range: {offset_v:.3f} V, expected {OFFSET_MIN_V:.1f} to {OFFSET_MAX_V:.1f} V."
            ],
            warnings=[],
            waveform_metrics=None,
        )

    def go_next(self) -> None:
        if self.step == self.LOT_STEP:
            self.start_batch()
        elif self.step == self.LOAD_STEP:
            self.show_step(self.OFFSET_STEP)
        elif self.step == self.OFFSET_STEP:
            self.finish_offset_step()
        elif self.step == self.SENSITIVITY_STEP:
            self.finish_sensitivity_step()
        elif self.step == self.RESULT_STEP:
            self.save_and_continue()

    def go_back(self) -> None:
        if self.busy:
            return
        if self.step == self.LOAD_STEP:
            self.show_step(self.LOT_STEP)
        elif self.step == self.OFFSET_STEP:
            self.show_step(self.LOAD_STEP)
        elif self.step == self.SENSITIVITY_STEP:
            self.show_step(self.OFFSET_STEP)
        elif self.step == self.RESULT_STEP and not self.result_saved:
            previous_step = self.OFFSET_STEP if self.is_offset_only_failure() else self.SENSITIVITY_STEP
            self.show_step(previous_step)

    def show_step(self, step: str) -> None:
        self.step = step
        self.render_step()

    def update_navigation_state(self) -> None:
        self.secondary_button.grid_remove()
        if self.step == self.LOT_STEP:
            self.back_button.configure(state="disabled")
            self.primary_button.configure(text="Next (Enter)", state="disabled" if self.busy else "normal")
        elif self.step == self.LOAD_STEP:
            self.back_button.configure(state="disabled" if self.busy else "normal")
            self.primary_button.configure(text="Sensor Loaded (Enter)", state="disabled" if self.busy else "normal")
        elif self.step == self.OFFSET_STEP:
            self.back_button.configure(state="disabled" if self.busy else "normal")
            self.primary_button.configure(text="Next (Enter)", state="disabled" if self.busy else "normal")
        elif self.step == self.SENSITIVITY_STEP:
            self.back_button.configure(state="disabled" if self.busy else "normal")
            self.primary_button.configure(text="Next (Enter)", state="disabled" if self.busy else "normal")
        else:
            self.back_button.configure(state="disabled" if self.busy or self.result_saved else "normal")
            self.secondary_button.grid()
            state = "disabled" if self.busy or self.result_saved or self.last_result is None else "normal"
            self.primary_button.configure(text="Save + Next Sensor (Enter)", state=state)
            self.secondary_button.configure(text="Save + Exit Batch (Esc)", state=state)

        for name in ("connect_button", "read_offset_button", "capture_signal_button"):
            button = getattr(self, name, None)
            if button is not None and button.winfo_exists():
                button.configure(state="disabled" if self.busy else "normal")

    def read_setup(self) -> tuple[float, str, float]:
        try:
            gain = float(self.am502_gain_var.get())
        except ValueError as exc:
            raise ValueError("External gain must be a number.") from exc
        if gain <= 0:
            raise ValueError("External gain must be positive.")

        range_label = self.labjack_range_var.get()
        waveform_range_v = labjack_ain0_range_from_label(range_label)
        range_warning = labjack_range_offset_warning(waveform_range_v, range_label)
        if range_warning is not None:
            raise ValueError(range_warning)
        return gain, range_label, waveform_range_v

    def start_batch(self) -> None:
        lot_number = self.lot_number_var.get().strip()
        if not lot_number:
            messagebox.showerror("Lot number needed", "Please enter Lot number.")
            return
        try:
            gain, range_label, _waveform_range_v = self.read_setup()
        except ValueError as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return

        self.lot_number = lot_number
        self.last_filter_setup = self.filter_var.get()
        self.last_am502_gain = gain
        self.last_labjack_range_label = range_label
        csv_path = batch_results_path(lot_number)
        self.csv_path_var.set(str(csv_path))
        self.current_sensor_number = next_sensor_number_for_batch(csv_path)
        existing_rows = count_existing_batch_rows(csv_path)
        if existing_rows:
            self.status_var.set(f"Lot {lot_number}: next sensor is {lot_number}-{self.current_sensor_number}.")
        else:
            self.status_var.set(f"Lot {lot_number}: first sensor is {lot_number}-{self.current_sensor_number}.")
        self.prepare_current_sensor()
        self.show_step(self.LOAD_STEP)

    def prepare_current_sensor(self) -> None:
        self.current_sensor_id = f"{self.lot_number}-{self.current_sensor_number}"
        self.current_sensor_var.set(self.current_sensor_id)
        self.offset_read_started = False
        self.signal_capture_started = False
        self.advance_after_offset_capture = False
        self.advance_after_signal_capture = False
        self.result_saved = False
        self.offset_v = None
        self.pending_offset_v = None
        self.last_waveform_metrics = None
        self.last_result = None
        self.scope_offset_var.set("")
        self.scope_sensitivity_var.set("")
        self.operator_polarity_var.set("Good")
        self.scope_verification_var.set(DEFAULT_SCOPE_VERIFICATION_CHOICE)
        self.offset_display_var.set("Not measured")
        self.sensitivity_display_var.set("Not measured")
        self.polarity_display_var.set("Not measured")
        self.frequency_display_var.set("Not measured")
        self.overall_display_var.set("READY")
        self.notes_var.set("")
        self.status_var.set(f"Ready for {self.current_sensor_id}.")
        self.write_autosave("sensor_started")

    def finish_offset_step(self) -> None:
        try:
            self.read_required_float(self.scope_offset_var.get(), "Operator offset", allowed_units=("v",))
        except ValueError as exc:
            messagebox.showerror("Invalid offset", str(exc))
            return
        self.write_autosave("operator_offset_entered")
        self.last_result = None
        self.last_waveform_metrics = None
        self.signal_capture_started = False
        self.start_offset_read(advance_to_next_step=True)

    def finish_sensitivity_step(self) -> None:
        if self.pending_offset_v is None:
            messagebox.showinfo("Offset needed", "Capture the offset before continuing.")
            return
        try:
            self.read_required_float(
                self.scope_sensitivity_var.get(),
                "Operator sensitivity",
                allowed_units=("mv",),
            )
        except ValueError as exc:
            messagebox.showerror("Invalid sensitivity", str(exc))
            return
        if self.operator_polarity_var.get() not in ("Good", "Bad"):
            messagebox.showerror("Polarity needed", "Choose whether polarity is Good or Bad.")
            return
        self.write_autosave("operator_sensitivity_entered")
        self.start_signal_capture(advance_to_next_step=True)

    def read_required_float(
        self,
        text: str,
        label: str,
        allowed_units: tuple[str, ...] = (),
    ) -> float:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError(f"{label} is required.")

        lowered = cleaned.lower()
        for unit in sorted(allowed_units, key=len, reverse=True):
            if lowered.endswith(unit):
                cleaned = cleaned[: -len(unit)].strip()
                lowered = cleaned.lower()
                break

        try:
            value = float(cleaned)
        except ValueError as exc:
            unit_hint = "" if not allowed_units else f" Units like {allowed_units[0]} are okay."
            raise ValueError(f"{label} must be a number.{unit_hint}") from exc
        if not math.isfinite(value):
            raise ValueError(f"{label} must be a finite number.")
        return value

    def read_scope_sensitivity_input_mv(self) -> float:
        return self.read_required_float(
            self.scope_sensitivity_var.get(),
            "Operator sensitivity",
            allowed_units=("mv",),
        )

    def scope_sensitivity_to_sensor_mv(self, scope_sensitivity_mv: float) -> float:
        gain = self.last_am502_gain if self.last_am502_gain > 0 else DEFAULT_AM502_GAIN
        return scope_sensitivity_mv / gain

    def read_scope_verification(self) -> ScopeVerification:
        if self.last_result is None:
            raise ValueError("No completed tester result is available.")

        scope_offset_v = self.read_required_float(self.scope_offset_var.get(), "Operator offset", allowed_units=("v",))
        if self.is_offset_only_failure():
            scope_sensitivity_mv = None
            operator_polarity = ""
        else:
            scope_sensitivity_input_mv = self.read_scope_sensitivity_input_mv()
            scope_sensitivity_mv = self.scope_sensitivity_to_sensor_mv(scope_sensitivity_input_mv)
            operator_polarity = self.operator_polarity_var.get()
        tag, reason = split_scope_verification_choice(self.scope_verification_var.get())
        if not tag:
            raise ValueError("Choose an operator failure reason.")
        if self.current_sensor_failed() and tag == SCOPE_GOOD_TAG:
            raise ValueError("This sensor failed. Choose the operator failure reason from the drop-down.")

        offset_delta_v: float | None = None
        sensitivity_delta_mv: float | None = None
        if self.last_result.offset_v is not None:
            offset_delta_v = scope_offset_v - self.last_result.offset_v
        if scope_sensitivity_mv is not None and self.last_result.sensitivity_mv is not None:
            sensitivity_delta_mv = scope_sensitivity_mv - self.last_result.sensitivity_mv

        return ScopeVerification(
            tag=tag,
            reason=reason,
            offset_v=scope_offset_v,
            offset_delta_v=offset_delta_v,
            sensitivity_mv=scope_sensitivity_mv,
            sensitivity_delta_mv=sensitivity_delta_mv,
            operator_polarity=operator_polarity,
        )

    def current_sensor_failed(self) -> bool:
        if self.last_result is None:
            return True
        if not self.last_result.passed:
            return True
        if self.operator_polarity_var.get() == "Bad":
            return True
        tag, _reason = split_scope_verification_choice(self.scope_verification_var.get())
        return bool(tag and tag != SCOPE_GOOD_TAG)

    def result_message_text(self) -> str:
        lines: list[str] = []
        if self.last_result is None:
            return "No result captured."
        if self.last_result.fail_reasons:
            lines.append("Tester failure reasons:")
            lines.extend(f"- {reason}" for reason in self.last_result.fail_reasons)
        else:
            lines.append("Tester did not find a failure.")
        if self.operator_polarity_var.get() == "Bad":
            lines.append("- Operator marked polarity as Bad.")
        if self.last_result.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"- {warning}" for warning in self.last_result.warnings)
        polarity_detail = format_polarity_detail(self.last_waveform_metrics)
        if polarity_detail is not None:
            lines.append("")
            lines.append(f"Polarity detail: {polarity_detail}.")
        return "\n".join(lines)

    def save_and_continue(self) -> None:
        if self.save_current_sensor():
            self.current_sensor_number += 1
            self.prepare_current_sensor()
            self.show_step(self.LOAD_STEP)

    def save_and_end_batch(self) -> None:
        if self.save_current_sensor():
            saved_lot = self.lot_number
            saved_csv_path = Path(self.csv_path_var.get())
            self.step = self.LOT_STEP
            self.result_saved = True
            self.status_var.set(f"Lot {saved_lot} ended.")
            self.show_batch_summary_window(saved_lot, saved_csv_path)
            self.render_step()

    def show_batch_summary_window(self, lot_number: str, csv_path: Path) -> None:
        rows = self.read_batch_summary_rows(csv_path)
        summary = tk.Toplevel(self)
        summary.title(f"Lot {lot_number} Summary")
        summary.minsize(1120, 520)
        summary.configure(bg="#f4f6f8")

        ttk.Label(
            summary,
            text=f"Lot {lot_number}: Tester vs Operator",
            font=("Segoe UI", 24, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 8))

        frame = ttk.Frame(summary, padding=(18, 0, 18, 16))
        frame.grid(row=1, column=0, sticky="nsew")
        summary.rowconfigure(1, weight=1)
        summary.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        style = ttk.Style(summary)
        style.configure("Summary.Treeview", font=("Segoe UI", 12), rowheight=32)
        style.configure("Summary.Treeview.Heading", font=("Segoe UI", 12, "bold"))

        columns = (
            "sensor",
            "tester_offset",
            "operator_offset",
            "offset_delta",
            "tester_sensitivity",
            "operator_sensitivity",
            "sensitivity_delta",
            "result",
        )
        tree = ttk.Treeview(frame, columns=columns, show="headings", style="Summary.Treeview", height=14)
        headings = {
            "sensor": "Sensor",
            "tester_offset": "Tester Offset",
            "operator_offset": "Operator Offset",
            "offset_delta": "Offset Diff",
            "tester_sensitivity": "Tester Sens.",
            "operator_sensitivity": "Scope Input",
            "sensitivity_delta": "Sens. Diff",
            "result": "Result",
        }
        widths = {
            "sensor": 110,
            "tester_offset": 130,
            "operator_offset": 140,
            "offset_delta": 120,
            "tester_sensitivity": 130,
            "operator_sensitivity": 140,
            "sensitivity_delta": 120,
            "result": 90,
        }
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="center", stretch=True)

        tree.tag_configure("pass", background="#dcfce7")
        tree.tag_configure("fail", background="#fee2e2")
        for row in rows:
            tag = "pass" if row[-1] == "PASS" else "fail"
            tree.insert("", "end", values=row, tags=(tag,))

        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=y_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")

        ttk.Button(summary, text="Close", command=summary.destroy, style="Large.TButton").grid(
            row=2, column=0, sticky="e", padx=18, pady=(0, 16)
        )

    def read_batch_summary_rows(self, csv_path: Path) -> list[tuple[str, str, str, str, str, str, str, str]]:
        rows: list[tuple[str, str, str, str, str, str, str, str]] = []
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
                for row in csv.DictReader(csv_file):
                    result = "FAIL" if row.get("pass_fail") == "FAIL" or row.get("scope_pass_fail") == "FAIL" else "PASS"
                    rows.append(
                        (
                            row.get("sensor_id", ""),
                            self.format_summary_number(row.get("offset_v", ""), 3, " V"),
                            self.format_summary_number(row.get("actual_offset_v", ""), 3, " V"),
                            self.format_summary_number(row.get("actual_offset_delta_v", ""), 3, " V", signed=True),
                            self.format_summary_number(row.get("sensitivity_mv", ""), 2, " mV"),
                            self.format_scope_input_from_summary_row(row),
                            self.format_summary_number(row.get("actual_sensitivity_delta_mv", ""), 2, " mV", signed=True),
                            result,
                        )
                    )
        except Exception:
            rows.append(("Could not read batch CSV", "", "", "", "", "", "", "FAIL"))
        return rows

    def format_summary_number(self, value: str, decimals: int, suffix: str, signed: bool = False) -> str:
        if not value:
            return ""
        try:
            number = float(value)
        except ValueError:
            return value
        sign = "+" if signed else ""
        return f"{number:{sign}.{decimals}f}{suffix}"

    def format_scope_input_from_summary_row(self, row: dict[str, str]) -> str:
        sensitivity = row.get("actual_sensitivity_mv", "")
        gain_text = row.get("am502_gain", "")
        if not sensitivity:
            return ""
        try:
            scope_input_mv = float(sensitivity) * float(gain_text or "1")
        except ValueError:
            return self.format_summary_number(sensitivity, 2, " mV")
        return f"{scope_input_mv:.2f} mV"

    def save_current_sensor(self) -> bool:
        if self.last_result is None:
            messagebox.showinfo("Nothing to save", "Complete the tester result before saving.")
            return False
        try:
            scope_verification = self.read_scope_verification()
        except ValueError as exc:
            messagebox.showerror("Invalid operator check", str(exc))
            return False

        snapshot_path: Path | None = None
        if self.current_sensor_failed():
            try:
                snapshot_path = save_failed_waveform_snapshot(
                    self.lot_number,
                    self.current_sensor_id,
                    self.last_waveform_metrics,
                    self.last_result,
                )
            except Exception:
                snapshot_path = None

        try:
            append_result_csv(
                Path(self.csv_path_var.get()),
                sensor_id=self.current_sensor_id,
                filter_setup=self.last_filter_setup,
                am502_gain=self.last_am502_gain,
                labjack_range_label=self.last_labjack_range_label,
                final_result=self.last_result,
                scope_verification=scope_verification,
                lot_number=self.lot_number,
                sensor_number=self.current_sensor_number,
                waveform_snapshot_path=snapshot_path,
            )
        except Exception as exc:
            messagebox.showerror("Could not save result", str(exc))
            return False

        self.result_saved = True
        self.delete_autosave()
        self.status_var.set(f"Saved {self.current_sensor_id}.")
        return True

    def write_autosave(self, stage: str) -> None:
        if not self.lot_number or not self.current_sensor_id:
            return
        autosave_path = batch_autosave_path(self.lot_number)
        autosave_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "lot_number": self.lot_number,
            "sensor_number": self.current_sensor_number,
            "sensor_id": self.current_sensor_id,
            "csv_path": self.csv_path_var.get(),
            "filter_setup": self.last_filter_setup,
            "am502_gain": self.last_am502_gain,
            "labjack_ain0_ain1_range": self.last_labjack_range_label,
            "tester_offset_v": self.pending_offset_v,
            "operator_offset_v": self.scope_offset_var.get(),
            "tester_sensitivity_mv": None if self.last_result is None else self.last_result.sensitivity_mv,
            "tester_polarity": None if self.last_result is None else self.last_result.polarity,
            "operator_sensitivity_mv": self.scope_sensitivity_var.get(),
            "operator_polarity": self.operator_polarity_var.get(),
            "tester_fail_reasons": [] if self.last_result is None else self.last_result.fail_reasons,
        }
        try:
            with autosave_path.open("w", encoding="utf-8") as autosave_file:
                json.dump(payload, autosave_file, indent=2)
        except Exception:
            pass

    def delete_autosave(self) -> None:
        if not self.lot_number:
            return
        try:
            batch_autosave_path(self.lot_number).unlink(missing_ok=True)
        except Exception:
            pass

    def startup_probe(self) -> None:
        _ok, message = probe_labjack_status()
        self.status_var.set(message)

    def connect_labjack(self) -> None:
        try:
            _gain, range_label, waveform_range_v = self.read_setup()
        except ValueError as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return
        self.set_busy(True)

        def worker() -> None:
            try:
                if self.device is None:
                    self.device = LabJackT7()
                self.device.connect()
                self.device.configure_analog_inputs(waveform_range_v=waveform_range_v)
            except Exception as exc:
                self.after(0, lambda exc=exc: self.hardware_error(exc))
            else:
                self.after(0, lambda: self.status_var.set(f"Connected to LabJack T7. AIN0/AIN1 range is {range_label}."))
            finally:
                self.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def start_offset_read(self, advance_to_next_step: bool = False) -> None:
        try:
            gain, range_label, waveform_range_v = self.read_setup()
        except ValueError as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return

        self.last_filter_setup = self.filter_var.get()
        self.last_am502_gain = gain
        self.last_labjack_range_label = range_label
        self.offset_read_started = True
        self.advance_after_offset_capture = advance_to_next_step
        self.set_busy(True)
        self.offset_display_var.set("Captured")
        self.status_var.set("Capturing offset...")
        self.update_navigation_state()

        def worker() -> None:
            try:
                self.ensure_connected()
                offset_v = self.device.read_offset_voltage(waveform_range_v=waveform_range_v)
            except Exception as exc:
                self.after(0, lambda exc=exc: self.hardware_error(exc))
            else:
                self.after(0, lambda: self.on_offset_complete(offset_v))
            finally:
                self.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def on_offset_complete(self, offset_v: float) -> None:
        self.offset_v = offset_v
        self.pending_offset_v = offset_v
        offset_ok = OFFSET_MIN_V <= offset_v <= OFFSET_MAX_V
        self.offset_display_var.set("Captured" + (" PASS" if offset_ok else " FAIL"))
        self.status_var.set("Offset captured.")
        if not offset_ok:
            self.last_waveform_metrics = None
            self.last_result = self.make_offset_only_result(offset_v)
            self.sensitivity_display_var.set("Skipped")
            self.polarity_display_var.set("Skipped")
            self.frequency_display_var.set("Skipped")
            self.scope_verification_var.set(suggest_scope_verification_choice(self.last_result))
        if self.last_waveform_metrics is not None:
            self.last_waveform_metrics.offset_v = offset_v
            self.last_result = evaluate_result(offset_v, self.last_waveform_metrics, self.last_filter_setup)
            self.update_result_display_from_metrics(self.last_waveform_metrics, self.last_result)
        self.write_autosave("tester_offset_captured")
        if self.advance_after_offset_capture:
            self.advance_after_offset_capture = False
            if offset_ok:
                self.show_step(self.SENSITIVITY_STEP)
            else:
                self.status_var.set("Offset failed. Choose the reject mode, then save this sensor or exit the batch.")
                self.show_step(self.RESULT_STEP)
        elif self.step == self.OFFSET_STEP:
            self.render_step()
        else:
            self.update_navigation_state()

    def start_signal_capture(self, advance_to_next_step: bool = False) -> None:
        if self.pending_offset_v is None:
            messagebox.showinfo("Offset needed", "Read the offset before capturing sensitivity.")
            return
        try:
            _gain, _range_label, waveform_range_v = self.read_setup()
        except ValueError as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return

        offset_v = self.pending_offset_v
        filter_setup = self.last_filter_setup
        gain = self.last_am502_gain
        self.signal_capture_started = True
        self.advance_after_signal_capture = advance_to_next_step
        self.set_busy(True)
        self.sensitivity_display_var.set("Captured")
        self.polarity_display_var.set("Captured")
        self.frequency_display_var.set("Captured")
        self.status_var.set("Capturing sensitivity...")
        self.update_navigation_state()

        def worker() -> None:
            try:
                self.ensure_connected()
                waveform, sync, actual_rate = self.device.read_waveform_stream(
                    sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
                    expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
                    sync_edge=PROCEDURE_SYNC_EDGE,
                    waveform_range_v=waveform_range_v,
                )
                metrics = analyze_waveform(
                    waveform_v=waveform,
                    sync_v=sync,
                    sample_rate_hz=actual_rate,
                    am502_gain=gain,
                    sync_edge=PROCEDURE_SYNC_EDGE,
                    input_range_v=waveform_range_v,
                    expect_offset_on_waveform=False,
                )
                metrics.offset_v = offset_v
                final = evaluate_result(offset_v, metrics, filter_setup)
            except Exception as exc:
                self.after(0, lambda exc=exc: self.hardware_error(exc))
            else:
                self.after(0, lambda: self.on_signal_capture_complete(metrics, final))
            finally:
                self.after(0, lambda: self.set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def on_signal_capture_complete(self, metrics: WaveformMetrics, final: FinalResult) -> None:
        self.last_waveform_metrics = metrics
        self.last_result = final
        self.update_result_display_from_metrics(metrics, final)
        self.scope_verification_var.set(suggest_scope_verification_choice(final, self.operator_polarity_var.get()))
        self.status_var.set("Sensitivity captured.")
        self.write_autosave("tester_sensitivity_captured")
        if self.advance_after_signal_capture:
            self.advance_after_signal_capture = False
            self.show_step(self.RESULT_STEP)
        elif self.step == self.SENSITIVITY_STEP:
            self.render_step()
        else:
            self.update_navigation_state()

    def update_result_display_from_metrics(self, metrics: WaveformMetrics, final: FinalResult) -> None:
        offset_ok = final.offset_v is not None and OFFSET_MIN_V <= final.offset_v <= OFFSET_MAX_V
        offset_text = "Not measured" if final.offset_v is None else f"{final.offset_v:.3f} V"
        self.offset_display_var.set(offset_text + (" PASS" if offset_ok else " FAIL"))
        self.sensitivity_display_var.set(f"{metrics.sensitivity_mv:.2f} mV")
        if metrics.polarity_confidence is None:
            self.polarity_display_var.set(metrics.polarity)
        else:
            self.polarity_display_var.set(f"{metrics.polarity} {metrics.polarity_confidence * 100:.0f}%")
        if metrics.measured_frequency_hz is None:
            self.frequency_display_var.set("No sync")
        else:
            self.frequency_display_var.set(f"{metrics.measured_frequency_hz:.3f} Hz")

    def ensure_connected(self) -> None:
        if self.device is None:
            self.device = LabJackT7()
        self.device.connect()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.update_navigation_state()
        if not busy:
            self.after_idle(self.focus_default_widget)

    def hardware_error(self, exc: Exception) -> None:
        text = str(exc)
        if "1230" in text or "CLAIMED" in text.upper():
            text = "The T7 is claimed by another LabJack program. Close LJStreamM/Kipling and try Connect again."
        self.status_var.set(text)
        messagebox.showerror("LabJack connection problem", text)

    def on_close(self) -> None:
        if self.device is not None:
            self.device.close()
        self.destroy()


def main() -> None:
    app = GuidedTesterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
