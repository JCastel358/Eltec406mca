"""
Eltec 406MCA v6.1-base failure-calibration study application.

The measurement engine and guided flow come from the proven v4 LabJack tester,
but the hardware backend is the ESP32 + ADS1256 rig used on Xubuntu:

    - Deep Eltec-blue gradient app bar with an animated signal trace
    - Numbered step rail ("01 / 02 / 03") with animated progress transitions
    - Rounded, soft-shadow cards with the Eltec technical gradient accent
    - Custom hover-animated rounded buttons and an animated toggle switch
    - Count-up result tiles, PASS/FAIL banner sweep, scanning-beam scope view
    - Dark navy oscilloscope panel with grid + glow traces (site "dark section"
      look), with a clean monospace face for technical readouts

This app drives the test rig itself: the ESP32 generates a 10 Hz / 50% PWM
signal on GPIO25 that switches a MOSFET module to drive the black-body emitter.
An ADS1256 reads the 406MCA through a unity-gain voltage-follower buffer,
preserving the ~0.667 V DC offset and the small AC waveform.

Wiring:
    ADS AIN0 = buffered DUT sensor (DC offset + AC waveform), +/-2.5 V range
    ADS AIN1 = permanently-mounted reference sensor (required emitter-health gate)
    ADS AIN7 = 6 V SLA battery through a 100k/100k divider
    GPIO25   = PWM output to the MOSFET module
    sync     = the ESP32 PWM state included with every streamed sample

The emitter is always driven with a fixed 50% duty-cycle square wave, so the
technician never has to think about PWM settings - they only pick the filter.

Guided flow:
    1. Calibrate AIN1 with a known-good emitter, then enter the batch details.
    2. Place the sensor in the rig and press Enter.
    3. The app checks the AIN1 reference against its calibration before it reads
       AIN0. If the reference is outside +/-25%, testing remains locked until
       the emitter is replaced and the reference is calibrated again.
    4. The app reads the DUT DC offset (PWM off), then turns on the emitter and
       measures sensitivity and polarity, shows the numbers and a GOOD/BAD
       polarity verdict, and turns the screen green (PASS) or red (FAIL).
       Leave a comment, capture the waveform for troubleshooting, or watch the
       live waveform while it reads.
    5. Freeze the app prediction, preserve complete evidence, and require a
       technician ground-truth verdict/reason review before saving.

This is an experimental evidence-collection fork, not a production acceptance
application. The v6.1 acquisition and verdict rules are intentionally frozen.

Run:
    python3 eltec_406mca_esp32_tester.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import queue
import struct
import threading
import time
import tkinter as tk
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import numpy as np

# Reuse the proven signal analysis and pass/fail engine from the v1 tester so
# there is still a single source of truth for the production math. Hardware I/O
# is provided locally by esp32_backend.py.
import sys

_V1_TESTER_DIR = Path(__file__).resolve().parents[1] / "v1_single_sensor"
if str(_V1_TESTER_DIR) not in sys.path:
    sys.path.insert(0, str(_V1_TESTER_DIR))

from eltec_406mca_tester import (
    DEFAULT_EMITTER_PWM_FREQUENCY_HZ,
    DEFAULT_SAMPLE_RATE_HZ,
    EXPECTED_FREQUENCY_HZ,
    FILTER_SPECS_MV,
    MODEL_NAME,
    OFFSET_MAX_V,
    OFFSET_MIN_V,
    POSITIVE_POLARITY,
    PROCEDURE_SYNC_EDGE,
    SIM_CASES,
    FinalResult,
    WaveformMetrics,
    analyze_waveform,
    evaluate_result,
    find_sync_edges,
    format_polarity_detail,
    simulate_offset_v,
)

from esp32_backend import (
    EXPECTED_FIRMWARE_PREFIX,
    Esp32BackendError,
    Esp32EmitterRig,
    probe_esp32_status,
)
from stability_analysis import (
    CycleAnalysis,
    DEFAULT_MAX_MEASUREMENT_ATTEMPTS,
    DEFAULT_SETTINGS_PATH,
    StabilityAnalysis,
    StabilitySettings,
    StabilitySettingsError,
    SyncValidationError,
    analyze_stability,
    load_stability_settings,
    rising_edge_indices,
    validate_rising_sync_cycles,
)

# This rig reads the sensor through a unity-gain buffer (no AM502), so the
# external gain is always 1.0 and the offset rides on the waveform channel.
RIG_GAIN = 1.0

# Technicians run the "-6" setup (the -284 + extra -6 + blackened tube) about
# 80% of the time, so it is the default selection.
DEFAULT_FILTER_SETUP = "-284 filter + extra -6 + blackened tube"

# Standard production failure taxonomy. This is the same set used by the
# scope-verification workflow, minus its non-failure GOOD entry.
FAILURE_MODE_CHOICES = (
    "SB - Sensor bad",
    "GO/D - Good offset/no signal",
    "O - No sensitivity",
    "LS - Low sensitivity",
    "N - Noisy",
    "FN - Fast noise",
    "OSC - Oscillation",
    "HO - High offset",
    "LO - Low offset",
    "D - No offset",
    "TO - Technician says offset bad",
    "TS - Technician says sensitivity bad",
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
)
UNSTABLE_FAILURE_MODE = "Unstable - Unstable"

# This fork deliberately freezes the v6.1 verdict policy while collecting
# labels and raw evidence.  Bump the calibration schema when the exported
# record shape changes; bump the app version for any application change.
CALIBRATION_SCHEMA_VERSION = 1
CALIBRATION_APP_VERSION = "v6.1-failure-calibration.4"
CALIBRATION_RULESET_ID = "v6.1-strict-retry-10x20-snr1.5"
CALIBRATION_DISPLAY_NAME = "Eltec 406MCA Failure Calibration — v6.1 Base"
FROZEN_RULE_SOURCE_SHA256 = {
    "shared_v1_verdict_engine": "be8884c597e55d2f1cc7bd5c4ec2cb3f3382bd659fd324918d713239ba6068c1",
    "stability_analysis": "38569481911d60613344648fad83c1f26b9b0dd7bbf198afa1bae17b8bf769ef",
    "stability_settings": "67a1c758e207f0000f880b99444ee609241e135d0784fa4894650cf81f7bc635",
}
GOOD_GROUND_TRUTH_MODE = "GOOD - Sensor passes"
OTHER_GROUND_TRUTH_MODE = "OTHER - Other / engineering review"
UNKNOWN_GROUND_TRUTH_MODE = "UNKNOWN - Not independently confirmed"
GROUND_TRUTH_VERDICT_CHOICES = ("PASS", "FAIL", "UNSURE")
GROUND_TRUTH_FAILURE_MODE_CHOICES = (
    GOOD_GROUND_TRUTH_MODE,
    *FAILURE_MODE_CHOICES,
    OTHER_GROUND_TRUTH_MODE,
    UNKNOWN_GROUND_TRUTH_MODE,
)
FAILURE_REASON_ASSESSMENT_CHOICES = (
    "CORRECT",
    "PARTLY CORRECT",
    "INCORRECT",
    "NOT APPLICABLE",
    "UNSURE",
)
GROUND_TRUTH_BASIS_CHOICES = (
    "Independent bench / scope test",
    "Known failure history",
    "Known-good control",
    "Visual / physical inspection",
    "Engineering adjudication",
    "Other documented evidence",
    "Not independently confirmed",
)
REVIEW_CONFIDENCE_CHOICES = ("HIGH", "MEDIUM", "LOW", "UNSURE")

# Fixed ESP32 rig settings. Technicians never change these in production.
EMITTER_PWM_CHANNEL = "GPIO25"
EMITTER_PWM_FREQUENCY_HZ = DEFAULT_EMITTER_PWM_FREQUENCY_HZ
EMITTER_PWM_DUTY_CYCLE = 50.0
WAVEFORM_INPUT_RANGE_V = 2.5  # ADS1256 PGA x2 with 2.5 V reference => +/-2.5 V

# 6 V SLA watcher. Firmware reads ADS1256 AIN7 through the existing divide-by-2
# divider and returns battery volts after its 12-sample median and scaling.
BATTERY_MIN_V = 5.8               # hard block: recharge at or below this
BATTERY_WARN_V = 6.0              # early warning band (testing is still allowed)
# Reuse the load-step battery check instead of re-reading before the capture
# when the reading is healthy and at most this old (the SLA sags slowly).
BATTERY_REUSE_WINDOW_S = 30.0

# Hardware plausibility guards ("is everything actually plugged in?").
# A floating ADS1256 input reads arbitrary voltages, which can look like a
# healthy battery or a live sensor. These bands catch readings that cannot
# come from a correctly wired rig, so the app blocks the test and tells the
# technician what to plug in instead of recording bogus numbers.
BATTERY_FAULT_MIN_V = 3.0    # below this: battery missing / AIN7 divider not wired
BATTERY_FAULT_MAX_V = 7.5    # above this: not a plausible 6 V SLA reading
SENSOR_OFFSET_MIN_PLAUSIBLE_V = 0.05   # connected 406MCA sits near 0.3-1.2 V;
SENSOR_OFFSET_MAX_PLAUSIBLE_V = 2.5    # outside this band = no sensor / no buffer
# Battery level as displayed by the header gauge: full at ~6.4 V.
BATTERY_GAUGE_FULL_V = 6.4
# Simulator battery levels so the low-battery lockout can be exercised without hardware.
SIM_BATTERY_OK_V = 6.2
SIM_BATTERY_LOW_V = 5.6

# Signal-quality gate. A real chopped-emitter capture has a strong coherent
# waveform standing above the cycle-to-cycle noise (high SNR). A capture that is
# mostly sensor noise - e.g. the emitter is not actually being driven - has a
# low signal-to-noise ratio, so we fail it instead of trusting the raw
# amplitude. Tune this up once real good-sensor SNRs are known (the SNR is now
# logged to the batch CSV to help calibrate it).
MIN_SIGNAL_TO_NOISE_RATIO = 1.5   # ~3.5 dB
# V6.1 production capture policy. The delta threshold comes from the tracked
# JSON file; these timing/count constants are fixed application behavior.
STABILITY_TIMEOUT_S = 20.0
DUT_STABILITY_CONFIRMATION_DELTAS = 10
SENSITIVITY_MEASUREMENT_CYCLES = 20
MAX_MEASUREMENT_ATTEMPTS = DEFAULT_MAX_MEASUREMENT_ATTEMPTS
SYNC_VALIDATION_CYCLES = 3
STREAM_PREVIEW_MAX_SAMPLES = 2000
SIM_CAPTURE_CYCLES = 220

# Permanently-mounted AIN1 reference sensor. Each reference reading starts at
# PWM-on, uses a dedicated robust-peak delta limit, and averages the
# next five complete cycle peak-to-peak values. Calibration averages five of
# those adaptive readings. Every DUT test performs a fresh reference reading
# first and requires it to remain inside the fixed +/-25 percent window.
REFERENCE_CALIBRATION_READINGS = 5
REFERENCE_MEASUREMENT_CYCLES = 5
REFERENCE_PEAK_DELTA_THRESHOLD_MV = 0.250
REFERENCE_TOLERANCE_PERCENT = 25.0
REFERENCE_CALIBRATION_SCHEMA_VERSION = 2
if "Never stabilizes" not in SIM_CASES:
    SIM_CASES = [*SIM_CASES, "Never stabilizes"]


def simulate_v6_startup_capture(
    filter_setup: str,
    case_name: str,
    *,
    cycles: int = SIM_CAPTURE_CYCLES,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    frequency_hz: float = EXPECTED_FREQUENCY_HZ,
) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Generate a deterministic PWM-on drift followed by a stable waveform."""
    duration_s = cycles / frequency_hz
    sample_count = int(round(duration_s * sample_rate_hz))
    t = np.arange(sample_count, dtype=float) / sample_rate_hz
    phase = (t * frequency_hz) % 1.0
    min_mv = FILTER_SPECS_MV[filter_setup]
    if case_name == "Low sensitivity":
        sensitivity_mv = min_mv * 0.62
    elif case_name == "Random good sensor":
        sensitivity_mv = min_mv * 1.40
    else:
        sensitivity_mv = min_mv * 1.35

    triangle = np.where(phase < 0.5, -1.0 + 4.0 * phase, 3.0 - 4.0 * phase)
    if case_name == "Wrong polarity":
        triangle = -triangle
    stable_case = case_name if case_name in SIM_CASES and case_name != "Never stabilizes" else "Known good"
    offset_v = simulate_offset_v(stable_case)
    # A 100 mV exponential baseline transient crosses the provisional 0.1 mV
    # adjacent-cycle threshold at roughly ten seconds.
    startup_drift_v = 0.100 * np.exp(-t / 3.0)
    if case_name == "Never stabilizes":
        cycle_number = np.floor(t * frequency_hz).astype(int)
        startup_drift_v += np.where(cycle_number % 2 == 0, 0.00025, -0.00025)
    seed = sum((index + 1) * ord(char) for index, char in enumerate(case_name))
    rng = np.random.default_rng(seed)
    noise_v = rng.normal(0.0, 0.00001, sample_count)
    waveform_v = (
        offset_v
        + startup_drift_v
        + triangle * ((sensitivity_mv / 1000.0) / 2.0)
        + noise_v
    )
    sync_v = np.where(phase < 0.5, 1.0, 0.0)
    return waveform_v, sync_v, sample_rate_hz, offset_v


def analyze_esp32_waveform(*args, **kwargs) -> WaveformMetrics:
    """Run the shared production analysis with ESP32-specific warning text."""
    metrics = analyze_waveform(*args, **kwargs)
    rewritten: list[str] = []
    for warning in metrics.warnings:
        if warning.startswith("Waveform is near the LabJack"):
            warning = warning.replace("the LabJack", "the ADS1256", 1).replace(
                "choose a larger AIN0 range", "check the buffer/PGA range"
            )
        warning = warning.replace("blade sync", "ESP32 PWM sync")
        warning = warning.replace("Blade sync", "ESP32 PWM sync")
        rewritten.append(warning)
    metrics.warnings = rewritten
    return metrics

# --------------------------------------------------------------------------- #
# v6 theme - palette carried forward from v5 / eltecinstruments.com
# --------------------------------------------------------------------------- #
ELTEC_BLUE = "#1e419c"          # site primary blue
ELTEC_BLUE_DEEP = "#0b3d91"     # hero gradient start
ELTEC_BLUE_DARK = "#16336f"
ELTEC_BLUE_BRIGHT = "#4d8dff"   # hero gradient end
ELTEC_BLUE_LIGHT = "#e1e7f6"    # site light blue-gray tint
ELTEC_RED = "#ed1b44"           # site signal red

NAVY = "#0a1020"                # site dark-section background (scope view)
NAVY_EDGE = "#1b2740"
NAVY_GRID_MINOR = "#131c31"
NAVY_GRID_MAJOR = "#1c2947"

PAGE_BG = "#f3f5fa"
CARD_BG = "#ffffff"
CARD_BORDER = "#dce3f1"
TEXT_DARK = "#141d33"
NEUTRAL_FG = TEXT_DARK
MUTED_FG = "#5c6a88"
HEADER_FG = "#ffffff"
HEADER_SUB_FG = "#bcd0f7"

PASS_BG = "#e4f6eb"
PASS_FG = "#14532d"
PASS_ACCENT = "#17a34a"
FAIL_BG = "#fde7e9"
FAIL_FG = "#991b1b"
FAIL_ACCENT = "#dc2626"
WARN_BG = "#fdf5dd"
WARN_FG = "#854d0e"
WARN_ACCENT = "#f59e0b"
NEUTRAL_BG = "#e8edf6"

STEP_IDLE = "#c7d0e2"
STEP_IDLE_FG = "#93a1bd"
PRIMARY_DISABLED = "#aab9dc"
GHOST_BG = "#e6ebf6"
GHOST_HOVER = "#d4ddf0"

WAVE_BG = NAVY
TRACE_CORE = "#6ab2ff"          # scope trace glow: halo -> mid -> core
TRACE_MID = "#2f6fce"
TRACE_HALO = "#1c3a68"
SYNC_CORE = "#f5b93c"
SYNC_HALO = "#5c4310"

# The site's "technical gradient" strip (blue -> indigo -> violet), used as the
# accent line across the top of cards.
TECH_GRADIENT = ["#3b82f6", "#6366f1", "#a855f7"]
HEADER_GRADIENT = [ELTEC_BLUE_DEEP, ELTEC_BLUE, "#2e5bc0", ELTEC_BLUE_BRIGHT]

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
# Look for the logo next to this script and in an assets/ folder at any ancestor
# directory (the shared repo-root assets/ lives a few levels up), so it is
# found from either location.
LOGO_CANDIDATES = [ASSETS_DIR / "eltec_logo.png"] + [
    parent / "assets" / "eltec_logo.png"
    for parent in Path(__file__).resolve().parents
]


def find_logo_path() -> Path | None:
    for candidate in LOGO_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# High-DPI support. Without this Windows renders the app at 96 DPI and
# stretches the bitmap to the monitor scale, which makes circles and rounded
# shapes look blocky. Declaring DPI awareness + scaling all canvas geometry by
# UI_SCALE keeps everything crisp at 125%/150% display scaling.
# --------------------------------------------------------------------------- #
UI_SCALE = 1.0


def S(value: float) -> int:
    """Scale a logical (96-DPI) pixel dimension to physical pixels."""
    return int(round(value * UI_SCALE))


def Sf(value: float) -> float:
    """Float variant of S() for line widths and sub-pixel geometry."""
    return value * UI_SCALE


def enable_windows_dpi_awareness() -> None:
    """Opt out of DPI virtualization (must run before the Tk window exists).

    System-DPI-aware (not per-monitor v2) on purpose: Tk 8.6 does not fully
    support per-monitor mode and it can corrupt child-window repaints. System
    awareness gives the same crisp rendering on the single-monitor tester PCs.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except Exception:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def load_private_fonts() -> None:
    """Register brand fonts dropped into an assets/fonts folder (Windows only).

    The UI prefers Poppins / Manrope / JetBrains Mono (the faces used on
    eltecinstruments.com). AddFontResourceExW with FR_PRIVATE makes them
    available to this process only - nothing is installed system-wide.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except Exception:
        return
    font_files: list[Path] = []
    seen: set[Path] = set()
    for base in [ASSETS_DIR] + [parent / "assets" for parent in Path(__file__).resolve().parents]:
        fonts_dir = base / "fonts"
        if fonts_dir in seen or not fonts_dir.is_dir():
            continue
        seen.add(fonts_dir)
        font_files.extend(sorted(fonts_dir.glob("*.ttf")) + sorted(fonts_dir.glob("*.otf")))
    FR_PRIVATE = 0x10
    for font_file in font_files:
        try:
            ctypes.windll.gdi32.AddFontResourceExW(str(font_file), FR_PRIVATE, 0)
        except Exception:
            pass


def pick_font_family(root: tk.Misc, candidates: list[str], fallback: str) -> str:
    try:
        available = {family.lower() for family in tkfont.families(root)}
    except tk.TclError:
        return fallback
    for name in candidates:
        if name.lower() in available:
            return name
    return fallback


CSV_FIELDS = [
    "timestamp",
    "batch_number",
    "sensor_number",
    "sensor_id",
    "tester_name",
    "model",
    "filter_setup",
    "pwm_channel",
    "pwm_hz",
    "pwm_duty",
    "offset_v",
    "sensitivity_mv",
    "polarity",
    "polarity_good_bad",
    "pass_fail",
    "fail_reasons",
    "failure_mode_tag",
    "failure_mode_reason",
    "operator_comments",
    "waveform_snapshot_paths",
    "battery_v",
    "noise_rms_mv",
    "snr_db",
    # AIN1 emitter-health gate audit trail.
    "reference_calibrated_at",
    "reference_calibration_mv",
    "reference_lower_mv",
    "reference_upper_mv",
    "reference_check_mv",
    "reference_drift_pct",
    # V6 peak-delta stabilization telemetry.
    "stabilized",
    "stability_timeout",
    "stability_threshold_mv",
    "stability_required_deltas",
    "stabilization_cycle",
    "stabilization_seconds",
    "stability_window_max_delta_mv",
    "last_peak_delta_mv",
    "capture_cycles",
    "measurement_cycles",
    "stability_phase",
    "measurement_attempt",
    "measurement_failures",
    "active_stability_required_deltas",
    "active_measurement_cycles_required",
    "pwm_on_seconds",
    "data_source",
    # Failure-calibration study identity and immutable app prediction.
    "calibration_schema_version",
    "calibration_app_version",
    "calibration_ruleset_id",
    "session_id",
    "run_id",
    "measurement_run_number",
    "specimen_id",
    "specimen_repeat_number",
    "is_synthetic",
    "app_verdict",
    "app_suggested_failure_mode_tag",
    "app_suggested_failure_mode_reason",
    # Technician ground truth and explicit agreement assessment.
    "ground_truth_verdict",
    "ground_truth_failure_mode_tag",
    "ground_truth_failure_mode_reason",
    "verdict_assessment",
    "failure_reason_assessment",
    "review_classification",
    "ground_truth_basis",
    "review_confidence",
    "known_physical_root_cause",
    "review_notes",
    "reviewed_at_utc",
    "reviewer",
    # Snapshot the decision limits so results remain interpretable later.
    "policy_offset_min_v",
    "policy_offset_max_v",
    "policy_sensitivity_min_mv",
    "policy_min_snr_ratio",
    "policy_stability_timeout_s",
    "policy_max_measurement_attempts",
    # Canonical evidence and annotation records.
    "calibration_evidence_paths",
    "run_manifest_path",
    "run_manifest_sha256",
    "review_record_path",
    "review_record_sha256",
    "related_run_ids",
    "offset_plausibility_override",
]

STABILITY_SAMPLE_DIAGNOSTIC_FIELDS = (
    "batch_number",
    "sensor_id",
    "specimen_id",
    "run_id",
    "channel",
    "sample_index",
    "device_timestamp_us",
    "device_elapsed_s",
    "pwm_elapsed_s",
    "raw_count",
    "voltage_v",
    "sync",
)
STABILITY_CYCLE_DIAGNOSTIC_FIELDS = (
    "batch_number",
    "sensor_id",
    "specimen_id",
    "run_id",
    "channel",
    "cycle_number",
    "start_index",
    "end_index",
    "start_elapsed_s",
    "end_elapsed_s",
    "robust_peak_v",
    "raw_max_v",
    "raw_min_v",
    "peak_to_peak_v",
    "signed_peak_delta_mv",
    "absolute_peak_delta_mv",
    "within_threshold",
    "confirmation_run_length",
)


# --------------------------------------------------------------------------- #
# Results location + batch helpers
# --------------------------------------------------------------------------- #
def results_root_dir() -> Path:
    # Each tester version keeps its data in its own subfolder so results can be
    # tracked and analyzed per version. Autosave and waveform-snapshot folders
    # derive from this path, so they follow automatically.
    return (
        Path.home()
        / "Documents"
        / "Eltec_406MCA_Test_Results"
        / "v6_1_failure_calibration"
    )


def study_results_dir(*, is_synthetic: bool = False) -> Path:
    """Keep simulator/training rows outside the hardware study namespace."""
    root = results_root_dir()
    return root / "simulator" if is_synthetic else root


def safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    return cleaned.strip("_") or "UNLABELED"


def batch_results_path(batch_number: str, *, is_synthetic: bool = False) -> Path:
    return study_results_dir(is_synthetic=is_synthetic) / (
        f"406mca_failure_calibration_lot_{safe_filename_part(batch_number)}.csv"
    )


def validate_calibration_csv_schema(
    csv_path: Path,
    *,
    expected_batch_number: str = "",
) -> None:
    """Fail before measurement when a batch file has another schema."""
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            existing = list(reader.fieldnames or [])
            if existing != CSV_FIELDS:
                raise ValueError(
                    "This batch ID belongs to an older or different CSV schema. Use a new "
                    "batch ID; calibration and production rows must never be mixed."
                )
            for row in reader:
                if (
                    expected_batch_number
                    and (row.get("batch_number") or "") != expected_batch_number
                ):
                    raise ValueError(
                        "This batch ID maps to a results filename already used by a "
                        "different raw batch number. Choose a distinct ID using letters, "
                        "numbers, hyphens, or underscores."
                    )
                if (
                    (row.get("calibration_app_version") or "")
                    != CALIBRATION_APP_VERSION
                    or (row.get("calibration_ruleset_id") or "")
                    != CALIBRATION_RULESET_ID
                ):
                    raise ValueError(
                        "This batch contains another calibration app/ruleset version. "
                        "Use a new batch ID so study versions remain separable."
                    )
    except OSError as exc:
        raise OSError(f"Could not validate calibration CSV {csv_path}: {exc}") from exc


def batch_autosave_path(batch_number: str, *, is_synthetic: bool = False) -> Path:
    safe = safe_filename_part(batch_number)
    return (
        study_results_dir(is_synthetic=is_synthetic)
        / "autosave"
        / f"failure_calibration_lot_{safe}_current_sensor.json"
    )


def calibration_run_dir(
    batch_number: str,
    specimen_id: str,
    run_id: str,
    *,
    is_synthetic: bool = False,
) -> Path:
    """Return the immutable evidence directory for one measurement run."""
    return (
        study_results_dir(is_synthetic=is_synthetic)
        / "evidence"
        / f"lot_{safe_filename_part(batch_number)}"
        / safe_filename_part(specimen_id)
        / safe_filename_part(run_id)
    )


def reference_calibration_path() -> Path:
    """Writable AIN1 baseline for this experimental build only."""
    return results_root_dir() / "reference_sensor_calibration.json"


def reference_stability_settings(settings: StabilitySettings) -> StabilitySettings:
    """Use the relaxed fixed delta only for AIN1 reference captures."""
    return StabilitySettings(
        peak_delta_threshold_mv=REFERENCE_PEAK_DELTA_THRESHOLD_MV,
        consecutive_deltas_required=settings.consecutive_deltas_required,
    )


def dut_stability_settings(settings: StabilitySettings) -> StabilitySettings:
    """Apply the fixed v6.1 qualification count to the tracked DUT delta."""
    return StabilitySettings(
        peak_delta_threshold_mv=settings.peak_delta_threshold_mv,
        consecutive_deltas_required=DUT_STABILITY_CONFIRMATION_DELTAS,
    )


def v6_reference_calibration_path() -> Path:
    """Return the production v6 read-only compatibility baseline."""
    return (
        Path.home()
        / "Documents"
        / "Eltec_406MCA_Test_Results"
        / "v6_esp32"
        / "reference_sensor_calibration.json"
    )


def v6_1_reference_calibration_path() -> Path:
    """Return the ordinary v6.1 read-only compatibility baseline."""
    return (
        Path.home()
        / "Documents"
        / "Eltec_406MCA_Test_Results"
        / "v6_1_esp32"
        / "reference_sensor_calibration.json"
    )


def resolved_reference_calibration_path(path: Path | None = None) -> Path:
    """Resolve local -> v6.1 -> v6 while never bypassing an existing local file."""
    if path is not None:
        return Path(path)
    local = reference_calibration_path()
    if local.exists():
        return local
    for candidate in (
        v6_1_reference_calibration_path(),
        v6_reference_calibration_path(),
    ):
        if candidate.exists():
            return candidate
    return local


class ReferenceCalibrationError(RuntimeError):
    """The reference-unit calibration is missing, malformed, or unrepeatable."""


class ReferenceCaptureError(RuntimeError):
    """A chopped-emitter AIN1 capture cannot be used as a reference reading."""


@dataclass(frozen=True)
class ReferenceCalibration:
    """Persisted average and acceptance window for the fixed AIN1 sensor."""

    readings_mv: tuple[float, ...]
    mean_mv: float
    recorded_at: str
    tolerance_percent: float = REFERENCE_TOLERANCE_PERCENT
    valid: bool = True
    invalidated_at: str | None = None
    invalidation_reason: str | None = None
    failed_reading_mv: float | None = None

    @property
    def lower_mv(self) -> float:
        return self.mean_mv * (1.0 - self.tolerance_percent / 100.0)

    @property
    def upper_mv(self) -> float:
        return self.mean_mv * (1.0 + self.tolerance_percent / 100.0)

    def drift_percent(self, reading_mv: float) -> float:
        return (float(reading_mv) - self.mean_mv) / self.mean_mv * 100.0

    def accepts(self, reading_mv: float) -> bool:
        reading_mv = float(reading_mv)
        return (
            self.valid
            and math.isfinite(reading_mv)
            and self.lower_mv <= reading_mv <= self.upper_mv
        )

    def invalidated(self, reason: str, failed_reading_mv: float | None = None) -> "ReferenceCalibration":
        return ReferenceCalibration(
            readings_mv=self.readings_mv,
            mean_mv=self.mean_mv,
            recorded_at=self.recorded_at,
            tolerance_percent=self.tolerance_percent,
            valid=False,
            invalidated_at=datetime.now().isoformat(timespec="seconds"),
            invalidation_reason=str(reason).strip(),
            failed_reading_mv=failed_reading_mv,
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": REFERENCE_CALIBRATION_SCHEMA_VERSION,
            "channel": "AIN1",
            "metric": "mean peak-to-peak response of five post-stability cycles (mV)",
            "reading_count": len(self.readings_mv),
            "readings_mv": [round(value, 6) for value in self.readings_mv],
            "mean_mv": round(self.mean_mv, 6),
            "tolerance_percent": self.tolerance_percent,
            "lower_mv": round(self.lower_mv, 6),
            "upper_mv": round(self.upper_mv, 6),
            "recorded_at": self.recorded_at,
            "valid": self.valid,
            "invalidated_at": self.invalidated_at,
            "invalidation_reason": self.invalidation_reason,
            "failed_reading_mv": self.failed_reading_mv,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ReferenceCalibration":
        try:
            if int(payload.get("schema_version")) != REFERENCE_CALIBRATION_SCHEMA_VERSION:
                raise ValueError("unsupported schema version")
            readings = tuple(float(value) for value in payload["readings_mv"])
            mean_mv = float(payload["mean_mv"])
            tolerance = float(payload["tolerance_percent"])
            recorded_at = str(payload["recorded_at"]).strip()
            valid = bool(payload.get("valid", True))
            invalidated_at = payload.get("invalidated_at")
            invalidation_reason = payload.get("invalidation_reason")
            failed = payload.get("failed_reading_mv")
            failed_reading_mv = None if failed is None else float(failed)
        except (KeyError, TypeError, ValueError) as exc:
            raise ReferenceCalibrationError(
                "Reference calibration file is malformed; calibrate the reference unit again."
            ) from exc
        if (
            len(readings) < 2
            or not recorded_at
            or not math.isfinite(mean_mv)
            or mean_mv <= 0
            or not math.isfinite(tolerance)
            or tolerance <= 0
            or any(not math.isfinite(value) or value <= 0 for value in readings)
        ):
            raise ReferenceCalibrationError(
                "Reference calibration contains invalid readings; calibrate the reference unit again."
            )
        calculated_mean = float(np.mean(readings))
        if not math.isclose(mean_mv, calculated_mean, rel_tol=1e-5, abs_tol=1e-5):
            raise ReferenceCalibrationError(
                "Reference calibration average does not match its readings; run calibration again."
            )
        # A file invalidated only because its last reading missed the former
        # narrower window may be used again when that recorded reading is
        # inside the current policy. A fresh AIN1 check still runs before AIN0.
        if (
            not valid
            and tolerance < REFERENCE_TOLERANCE_PERCENT
            and failed_reading_mv is not None
            and math.isfinite(failed_reading_mv)
            and mean_mv * (1.0 - REFERENCE_TOLERANCE_PERCENT / 100.0)
            <= failed_reading_mv
            <= mean_mv * (1.0 + REFERENCE_TOLERANCE_PERCENT / 100.0)
        ):
            valid = True
            invalidated_at = None
            invalidation_reason = None
            failed_reading_mv = None
        return cls(
            readings_mv=readings,
            mean_mv=mean_mv,
            recorded_at=recorded_at,
            # The acceptance band is an application policy, not a per-file
            # setting. This also upgrades calibration files saved when v6.1
            # used the older +/-10% window.
            tolerance_percent=REFERENCE_TOLERANCE_PERCENT,
            valid=valid,
            invalidated_at=None if invalidated_at is None else str(invalidated_at),
            invalidation_reason=None if invalidation_reason is None else str(invalidation_reason),
            failed_reading_mv=failed_reading_mv,
        )


def build_reference_calibration(
    readings_mv: list[float] | tuple[float, ...],
    *,
    recorded_at: str | None = None,
    required_readings: int = REFERENCE_CALIBRATION_READINGS,
    tolerance_percent: float = REFERENCE_TOLERANCE_PERCENT,
) -> ReferenceCalibration:
    """Average repeatable AIN1 readings and produce the hard acceptance band."""
    readings = tuple(float(value) for value in readings_mv)
    if len(readings) != required_readings:
        raise ReferenceCalibrationError(
            f"Reference calibration requires {required_readings} readings; received {len(readings)}."
        )
    if any(not math.isfinite(value) or value <= 0 for value in readings):
        raise ReferenceCalibrationError("Every reference calibration reading must be finite and positive.")
    mean_mv = float(np.mean(readings))
    deviations = [abs(value - mean_mv) / mean_mv * 100.0 for value in readings]
    if max(deviations) > tolerance_percent:
        formatted = ", ".join(f"{value:.2f}" for value in readings)
        raise ReferenceCalibrationError(
            f"Reference calibration was not repeatable within +/-{tolerance_percent:g}% "
            f"(readings: {formatted} mV). Check the fixed sensor, wiring, battery, "
            "and emitter, then calibrate again."
        )
    return ReferenceCalibration(
        readings_mv=readings,
        mean_mv=mean_mv,
        recorded_at=recorded_at or datetime.now().isoformat(timespec="seconds"),
        tolerance_percent=tolerance_percent,
    )


def load_reference_calibration(path: Path | None = None) -> ReferenceCalibration | None:
    path = resolved_reference_calibration_path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReferenceCalibrationError(
            f"Could not read reference calibration {path}; calibrate the reference unit again."
        ) from exc
    if not isinstance(payload, dict):
        raise ReferenceCalibrationError(
            f"Reference calibration {path} is not a JSON object; run calibration again."
        )
    return ReferenceCalibration.from_dict(payload)


def save_reference_calibration(
    calibration: ReferenceCalibration, path: Path | None = None
) -> Path:
    """Atomically persist the AIN1 baseline or its invalidated state."""
    path = reference_calibration_path() if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(calibration.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ReferenceCalibrationError(
            f"Could not save reference calibration to {path}: {exc}"
        ) from exc
    return path


def analyze_reference_stable_response_mv(analysis: StabilityAnalysis) -> float:
    """Average the five fresh cycle p-p values selected after peak stability."""
    if not analysis.report.measurement_complete:
        if analysis.report.timed_out:
            raise ReferenceCaptureError(
                f"Reference unit did not stabilize within {STABILITY_TIMEOUT_S:g} seconds."
            )
        raise ReferenceCaptureError("Reference-unit capture ended before a stable reading was complete.")
    cycles = analysis.measurement_cycles
    if len(cycles) != REFERENCE_MEASUREMENT_CYCLES:
        raise ReferenceCaptureError(
            f"Reference reading requires {REFERENCE_MEASUREMENT_CYCLES} fresh cycles; "
            f"received {len(cycles)}."
        )
    reading_mv = float(np.mean([cycle.peak_to_peak_v for cycle in cycles])) * 1000.0
    if not math.isfinite(reading_mv) or reading_mv <= 0:
        raise ReferenceCaptureError("Reference-unit response is not finite and positive.")
    return reading_mv


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


def next_specimen_repeat_number(csv_path: Path, specimen_id: str) -> int:
    """Return a one-based saved-run index for a physical specimen."""
    specimen_id = str(specimen_id).strip().casefold()
    if not specimen_id or not csv_path.exists():
        return 1
    repeats = 0
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
            for row in csv.DictReader(csv_file):
                if (row.get("specimen_id") or "").strip().casefold() == specimen_id:
                    repeats += 1
    except Exception:
        return 1
    return repeats + 1


def polarity_good_bad(polarity: str) -> str:
    if not polarity or polarity in ("NOT MEASURED", "UNKNOWN"):
        return ""
    return "GOOD" if polarity == POSITIVE_POLARITY else "BAD"


def split_failure_mode(choice: str) -> tuple[str, str]:
    """Validate and split a displayed production failure-mode choice."""
    choice = str(choice).strip()
    if choice not in FAILURE_MODE_CHOICES:
        raise ValueError("Choose a failure mode before saving this failed sensor.")
    tag, reason = choice.split(" - ", 1)
    return tag, reason


def split_ground_truth_mode(choice: str) -> tuple[str, str]:
    """Split a study ground-truth mode, including GOOD/OTHER/UNKNOWN."""
    choice = str(choice).strip()
    if choice not in GROUND_TRUTH_FAILURE_MODE_CHOICES:
        raise ValueError("Choose the independently confirmed primary condition.")
    tag, reason = choice.split(" - ", 1)
    return tag, reason


@dataclass(frozen=True)
class CalibrationReview:
    """One technician annotation kept separate from the app prediction."""

    specimen_id: str
    app_verdict: str
    app_suggested_failure_mode: str
    ground_truth_verdict: str
    ground_truth_failure_mode: str
    failure_reason_assessment: str
    ground_truth_basis: str
    confidence: str
    known_physical_root_cause: str
    notes: str
    reviewer: str
    reviewed_at_utc: str

    @property
    def verdict_assessment(self) -> str:
        if self.ground_truth_verdict == "UNSURE":
            return "UNSURE"
        return "CORRECT" if self.app_verdict == self.ground_truth_verdict else "INCORRECT"

    @property
    def review_classification(self) -> str:
        if self.verdict_assessment == "UNSURE":
            return "INCONCLUSIVE"
        if self.verdict_assessment == "INCORRECT":
            return "VERDICT_MISMATCH"
        if self.ground_truth_verdict == "PASS":
            return "EXACT_MATCH"
        if self.failure_reason_assessment == "CORRECT":
            return "EXACT_MATCH"
        if self.failure_reason_assessment == "PARTLY CORRECT":
            return "PARTIAL_REASON_MATCH"
        if self.failure_reason_assessment == "INCORRECT":
            return "FAILURE_REASON_MISMATCH"
        return "INCONCLUSIVE_REASON"

    def to_dict(self) -> dict:
        ground_tag, ground_reason = split_ground_truth_mode(
            self.ground_truth_failure_mode
        )
        if self.app_suggested_failure_mode:
            app_tag, app_reason = split_failure_mode(self.app_suggested_failure_mode)
        else:
            app_tag, app_reason = "", ""
        return {
            "specimen_id": self.specimen_id,
            "app_verdict": self.app_verdict,
            "app_suggested_failure_mode": self.app_suggested_failure_mode,
            "app_suggested_failure_mode_tag": app_tag,
            "app_suggested_failure_mode_reason": app_reason,
            "ground_truth_verdict": self.ground_truth_verdict,
            "ground_truth_failure_mode": self.ground_truth_failure_mode,
            "ground_truth_failure_mode_tag": ground_tag,
            "ground_truth_failure_mode_reason": ground_reason,
            "verdict_assessment": self.verdict_assessment,
            "failure_reason_assessment": self.failure_reason_assessment,
            "review_classification": self.review_classification,
            "ground_truth_basis": self.ground_truth_basis,
            "review_confidence": self.confidence,
            "known_physical_root_cause": self.known_physical_root_cause,
            "review_notes": self.notes,
            "reviewed_at_utc": self.reviewed_at_utc,
            "reviewer": self.reviewer,
        }


def build_calibration_review(
    *,
    final_result: FinalResult,
    app_suggested_failure_mode: str,
    specimen_id: str,
    ground_truth_verdict: str,
    ground_truth_failure_mode: str,
    failure_reason_assessment: str,
    ground_truth_basis: str,
    confidence: str,
    known_physical_root_cause: str,
    notes: str,
    reviewer: str,
    reviewed_at_utc: str | None = None,
) -> CalibrationReview:
    """Validate and normalize a required calibration annotation."""
    app_verdict = "PASS" if final_result.passed else "FAIL"
    specimen_id = str(specimen_id).strip()
    ground_truth_verdict = str(ground_truth_verdict).strip().upper()
    ground_truth_failure_mode = str(ground_truth_failure_mode).strip()
    failure_reason_assessment = str(failure_reason_assessment).strip().upper()
    ground_truth_basis = str(ground_truth_basis).strip()
    confidence = str(confidence).strip().upper()
    known_physical_root_cause = str(known_physical_root_cause).strip()
    notes = str(notes).strip()
    reviewer = str(reviewer).strip()

    if not specimen_id:
        raise ValueError("Enter the physical specimen ID used on the sensor label.")
    if ground_truth_verdict not in GROUND_TRUTH_VERDICT_CHOICES:
        raise ValueError("Choose the technician ground-truth verdict: PASS, FAIL, or UNSURE.")
    if failure_reason_assessment not in FAILURE_REASON_ASSESSMENT_CHOICES:
        raise ValueError("Assess whether the application's failure reason was correct.")
    if ground_truth_basis not in GROUND_TRUTH_BASIS_CHOICES:
        raise ValueError("Choose how the ground truth was established.")
    if confidence not in REVIEW_CONFIDENCE_CHOICES:
        raise ValueError("Choose a ground-truth confidence level.")
    if not reviewer:
        raise ValueError("A reviewer/tester name is required.")
    if not notes:
        raise ValueError(
            "Add a brief explanation of the known condition and evidence. "
            "This is required even when the app was correct."
        )

    if ground_truth_basis == "Not independently confirmed" and ground_truth_verdict != "UNSURE":
        raise ValueError(
            "A PASS or FAIL ground truth needs independent evidence. Use UNSURE when it "
            "has not been independently confirmed."
        )
    if ground_truth_verdict == "PASS":
        if ground_truth_failure_mode != GOOD_GROUND_TRUTH_MODE:
            raise ValueError("A ground-truth PASS must use 'GOOD - Sensor passes'.")
        expected_assessment = (
            "NOT APPLICABLE" if app_verdict == "PASS" else "INCORRECT"
        )
        if failure_reason_assessment != expected_assessment:
            raise ValueError(
                f"For this PASS comparison, failure-reason assessment must be {expected_assessment}."
            )
    elif ground_truth_verdict == "FAIL":
        if ground_truth_failure_mode not in (
            *FAILURE_MODE_CHOICES,
            OTHER_GROUND_TRUTH_MODE,
        ):
            raise ValueError("Choose the actual primary failure mode for this failed sensor.")
        if app_verdict == "PASS" and failure_reason_assessment != "INCORRECT":
            raise ValueError(
                "A false PASS supplied no valid failure reason; mark the reason INCORRECT."
            )
        if app_verdict == "FAIL":
            if failure_reason_assessment == "NOT APPLICABLE":
                raise ValueError("A failed result has a reason to assess.")
            if (
                failure_reason_assessment == "CORRECT"
                and app_suggested_failure_mode != ground_truth_failure_mode
            ):
                raise ValueError(
                    "The selected actual mode differs from the app suggestion. "
                    "Use PARTLY CORRECT or INCORRECT, or select the matching mode."
                )
            if (
                failure_reason_assessment == "INCORRECT"
                and app_suggested_failure_mode == ground_truth_failure_mode
            ):
                raise ValueError(
                    "The selected actual mode matches the app suggestion. Use CORRECT or "
                    "PARTLY CORRECT, or select the independently confirmed different mode."
                )
    else:
        if ground_truth_failure_mode != UNKNOWN_GROUND_TRUTH_MODE:
            raise ValueError(
                "An UNSURE verdict must use 'UNKNOWN - Not independently confirmed'."
            )
        if failure_reason_assessment != "UNSURE":
            raise ValueError("An UNSURE verdict requires an UNSURE reason assessment.")

    if ground_truth_verdict == "UNSURE" and confidence == "HIGH":
        raise ValueError("An UNSURE ground truth cannot be recorded with HIGH confidence.")

    if app_verdict == "FAIL":
        split_failure_mode(app_suggested_failure_mode)
    elif app_suggested_failure_mode:
        raise ValueError("A PASS must not carry an app-suggested failure mode.")

    return CalibrationReview(
        specimen_id=specimen_id,
        app_verdict=app_verdict,
        app_suggested_failure_mode=app_suggested_failure_mode,
        ground_truth_verdict=ground_truth_verdict,
        ground_truth_failure_mode=ground_truth_failure_mode,
        failure_reason_assessment=failure_reason_assessment,
        ground_truth_basis=ground_truth_basis,
        confidence=confidence,
        known_physical_root_cause=known_physical_root_cause,
        notes=notes,
        reviewer=reviewer,
        reviewed_at_utc=reviewed_at_utc
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def suggest_failure_mode(final_result: FinalResult | None) -> str:
    """Choose the primary failure in measurement order."""
    if final_result is None or final_result.passed:
        return ""
    reason_text = " ".join(final_result.fail_reasons).lower()

    # Offset is measured before waveform capture, so an electrical offset
    # failure remains primary even when the subsequent waveform is unstable,
    # weak, reversed, or noisy. Continue capturing to retain those diagnostics.
    offset_v = final_result.offset_v
    if offset_v is not None and math.isfinite(offset_v):
        if offset_v > OFFSET_MAX_V:
            return "HO - High offset"
        if offset_v < OFFSET_MIN_V:
            if offset_v <= SENSOR_OFFSET_MIN_PLAUSIBLE_V:
                return "D - No offset"
            return "LO - Low offset"

    if "unstable" in reason_text or "stabiliz" in reason_text:
        return UNSTABLE_FAILURE_MODE
    if "sensitivity too low" in reason_text:
        no_coherent_response = (
            "signal-to-noise too low" in reason_text
            or "snr unavailable" in reason_text
            or "sensor noise with no emitter response" in reason_text
        )
        if (
            no_coherent_response
            and offset_v is not None
            and math.isfinite(offset_v)
            and OFFSET_MIN_V <= offset_v <= OFFSET_MAX_V
        ):
            return "GO/D - Good offset/no signal"
        return "LS - Low sensitivity"
    if "polarity" in reason_text:
        return "RP - Reversed polarity"
    if "signal-to-noise" in reason_text or "snr" in reason_text:
        return "N - Noisy"
    return "SB - Sensor bad"


def _fmt_optional_float(value: float | None, decimals: int) -> str:
    """Format an optional metric for CSV, blank when missing or non-finite."""
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.{decimals}f}"


def append_result_csv(
    csv_path: Path,
    *,
    batch_number: str,
    sensor_number: int,
    sensor_id: str,
    tester_name: str,
    filter_setup: str,
    pwm_channel: str,
    pwm_hz: float,
    pwm_duty: float,
    final_result: FinalResult,
    comment: str,
    snapshot_paths: list[Path],
    battery_v: float | None = None,
    capture_report: "StabilityCaptureReport | None" = None,
    reference_calibration: "ReferenceCalibration | None" = None,
    reference_check_mv: float | None = None,
    failure_mode: str = "",
    calibration_review: "CalibrationReview | None" = None,
    app_suggested_failure_mode: str = "",
    session_id: str = "",
    run_id: str = "",
    measurement_run_number: int = 1,
    specimen_repeat_number: int = 1,
    is_synthetic: bool = False,
    calibration_evidence_paths: list[Path] | None = None,
    run_manifest_path: Path | None = None,
    review_record_path: Path | None = None,
    related_run_ids: list[str] | None = None,
    offset_plausibility_override: bool = False,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    metrics = final_result.waveform_metrics
    if final_result.passed:
        failure_mode_tag, failure_mode_reason = "", ""
    else:
        failure_mode_tag, failure_mode_reason = split_failure_mode(
            failure_mode or suggest_failure_mode(final_result)
        )
    if not app_suggested_failure_mode and not final_result.passed:
        app_suggested_failure_mode = suggest_failure_mode(final_result)
    if app_suggested_failure_mode:
        app_mode_tag, app_mode_reason = split_failure_mode(
            app_suggested_failure_mode
        )
    else:
        app_mode_tag, app_mode_reason = "", ""
    review_fields = calibration_review.to_dict() if calibration_review else {}
    specimen_id = review_fields.get("specimen_id", "")
    evidence_paths = calibration_evidence_paths or []
    related_ids = related_run_ids or ([run_id] if run_id else [])
    sensitivity_min_mv = FILTER_SPECS_MV.get(filter_setup)
    portable_root = study_results_dir(is_synthetic=is_synthetic)

    def portable_path(path: Path | None) -> str:
        if path is None:
            return ""
        try:
            return str(Path(path).relative_to(portable_root))
        except ValueError:
            return str(path)
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "batch_number": batch_number,
        "sensor_number": str(sensor_number),
        "sensor_id": sensor_id,
        "tester_name": tester_name,
        "model": MODEL_NAME,
        "filter_setup": filter_setup,
        "pwm_channel": pwm_channel,
        "pwm_hz": f"{pwm_hz:g}",
        "pwm_duty": f"{pwm_duty:g}",
        "offset_v": "" if final_result.offset_v is None else f"{final_result.offset_v:.6f}",
        "sensitivity_mv": "" if final_result.sensitivity_mv is None else f"{final_result.sensitivity_mv:.6f}",
        "polarity": final_result.polarity,
        "polarity_good_bad": polarity_good_bad(final_result.polarity),
        "pass_fail": "PASS" if final_result.passed else "FAIL",
        "fail_reasons": "; ".join(final_result.fail_reasons),
        "failure_mode_tag": failure_mode_tag,
        "failure_mode_reason": failure_mode_reason,
        "operator_comments": comment.strip(),
        "waveform_snapshot_paths": "; ".join(
            portable_path(path) for path in snapshot_paths
        ),
        "battery_v": "" if battery_v is None else f"{battery_v:.3f}",
        "noise_rms_mv": _fmt_optional_float(metrics.noise_rms_mv if metrics else None, 4),
        "snr_db": _fmt_optional_float(metrics.signal_to_noise_db if metrics else None, 2),
        "reference_calibrated_at": (
            reference_calibration.recorded_at if reference_calibration else ""
        ),
        "reference_calibration_mv": _fmt_optional_float(
            reference_calibration.mean_mv if reference_calibration else None, 4
        ),
        "reference_lower_mv": _fmt_optional_float(
            reference_calibration.lower_mv if reference_calibration else None, 4
        ),
        "reference_upper_mv": _fmt_optional_float(
            reference_calibration.upper_mv if reference_calibration else None, 4
        ),
        "reference_check_mv": _fmt_optional_float(reference_check_mv, 4),
        "reference_drift_pct": _fmt_optional_float(
            reference_calibration.drift_percent(reference_check_mv)
            if reference_calibration and reference_check_mv is not None
            else None,
            3,
        ),
        "calibration_schema_version": str(CALIBRATION_SCHEMA_VERSION),
        "calibration_app_version": CALIBRATION_APP_VERSION,
        "calibration_ruleset_id": CALIBRATION_RULESET_ID,
        "session_id": session_id,
        "run_id": run_id,
        "measurement_run_number": str(measurement_run_number),
        "specimen_id": specimen_id,
        "specimen_repeat_number": str(specimen_repeat_number),
        "is_synthetic": "YES" if is_synthetic else "NO",
        "app_verdict": "PASS" if final_result.passed else "FAIL",
        "app_suggested_failure_mode_tag": app_mode_tag,
        "app_suggested_failure_mode_reason": app_mode_reason,
        "ground_truth_verdict": review_fields.get("ground_truth_verdict", ""),
        "ground_truth_failure_mode_tag": review_fields.get(
            "ground_truth_failure_mode_tag", ""
        ),
        "ground_truth_failure_mode_reason": review_fields.get(
            "ground_truth_failure_mode_reason", ""
        ),
        "verdict_assessment": review_fields.get("verdict_assessment", ""),
        "failure_reason_assessment": review_fields.get(
            "failure_reason_assessment", ""
        ),
        "review_classification": review_fields.get("review_classification", ""),
        "ground_truth_basis": review_fields.get("ground_truth_basis", ""),
        "review_confidence": review_fields.get("review_confidence", ""),
        "known_physical_root_cause": review_fields.get(
            "known_physical_root_cause", ""
        ),
        "review_notes": review_fields.get("review_notes", ""),
        "reviewed_at_utc": review_fields.get("reviewed_at_utc", ""),
        "reviewer": review_fields.get("reviewer", ""),
        "policy_offset_min_v": f"{OFFSET_MIN_V:.6f}",
        "policy_offset_max_v": f"{OFFSET_MAX_V:.6f}",
        "policy_sensitivity_min_mv": _fmt_optional_float(sensitivity_min_mv, 6),
        "policy_min_snr_ratio": f"{MIN_SIGNAL_TO_NOISE_RATIO:.6f}",
        "policy_stability_timeout_s": f"{STABILITY_TIMEOUT_S:.3f}",
        "policy_max_measurement_attempts": str(MAX_MEASUREMENT_ATTEMPTS),
        "calibration_evidence_paths": "; ".join(
            portable_path(path) for path in evidence_paths
        ),
        "run_manifest_path": portable_path(run_manifest_path),
        "run_manifest_sha256": (
            sha256_file(run_manifest_path)
            if run_manifest_path is not None and run_manifest_path.is_file()
            else ""
        ),
        "review_record_path": portable_path(review_record_path),
        "review_record_sha256": (
            sha256_file(review_record_path)
            if review_record_path is not None and review_record_path.is_file()
            else ""
        ),
        "related_run_ids": "; ".join(related_ids),
        "offset_plausibility_override": (
            "YES" if offset_plausibility_override else "NO"
        ),
    }
    if capture_report is not None:
        row.update(capture_report.csv_fields())
    # Calibration data must never silently lose new fields. A stale/mixed CSV
    # is rejected so the technician can choose a new batch ID and preserve both
    # schemas intact.
    if not write_header:
        try:
            validate_calibration_csv_schema(
                csv_path,
                expected_batch_number=batch_number,
            )
        except ValueError as exc:
            raise ValueError(
                "Calibration CSV schema/version mismatch. " + str(exc)
            ) from exc
    fieldnames = CSV_FIELDS
    row = {name: row.get(name, "") for name in CSV_FIELDS}
    with csv_path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# --------------------------------------------------------------------------- #
# Waveform snapshot PNG (self-contained, with a matplotlib upgrade if present)
# --------------------------------------------------------------------------- #
def plot_text_line(text: str, max_chars: int = 180) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(0, max_chars - 3)].rstrip() + "..."


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def write_rgb_png(path: Path, width: int, height: int, pixels: bytearray, text_chunks: dict[str, str] | None = None) -> None:
    rows = []
    stride = width * 3
    for y in range(height):
        rows.append(b"\x00" + bytes(pixels[y * stride : (y + 1) * stride]))
    with path.open("wb") as png_file:
        png_file.write(b"\x89PNG\r\n\x1a\n")
        png_file.write(png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        for key, value in (text_chunks or {}).items():
            safe_key = "".join(ch for ch in key if 32 <= ord(ch) <= 126).strip()[:79] or "Comment"
            safe_value = value.replace("\x00", " ")
            png_file.write(png_chunk(b"tEXt", safe_key.encode("latin-1", "replace") + b"\x00" + safe_value.encode("utf-8", "replace")))
        png_file.write(png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=6)))
        png_file.write(png_chunk(b"IEND", b""))


def set_rgb_pixel(pixels: bytearray, width: int, height: int, x: int, y: int, color: tuple[int, int, int]) -> None:
    if x < 0 or x >= width or y < 0 or y >= height:
        return
    idx = (y * width + x) * 3
    pixels[idx : idx + 3] = bytes(color)


def draw_rgb_line(pixels: bytearray, width: int, height: int, x0: float, y0: float, x1: float, y1: float, color: tuple[int, int, int]) -> None:
    x0_i, y0_i, x1_i, y1_i = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    dx = abs(x1_i - x0_i)
    dy = -abs(y1_i - y0_i)
    sx = 1 if x0_i < x1_i else -1
    sy = 1 if y0_i < y1_i else -1
    err = dx + dy
    while True:
        set_rgb_pixel(pixels, width, height, x0_i, y0_i, color)
        if x0_i == x1_i and y0_i == y1_i:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0_i += sx
        if e2 <= dx:
            err += dx
            y0_i += sy


def draw_rgb_rect_outline(pixels: bytearray, width: int, height: int, left: int, top: int, right: int, bottom: int, color: tuple[int, int, int]) -> None:
    draw_rgb_line(pixels, width, height, left, top, right, top, color)
    draw_rgb_line(pixels, width, height, right, top, right, bottom, color)
    draw_rgb_line(pixels, width, height, right, bottom, left, bottom, color)
    draw_rgb_line(pixels, width, height, left, bottom, left, top, color)


def draw_signal_trace(pixels: bytearray, width: int, height: int, signal: np.ndarray, left: int, top: int, right: int, bottom: int, color: tuple[int, int, int]) -> None:
    signal = np.asarray(signal, dtype=float)
    if signal.size < 2:
        return
    signal_min = float(np.min(signal))
    signal_max = float(np.max(signal))
    if abs(signal_max - signal_min) < 1e-9:
        signal_max = signal_min + 1.0
    plot_width = max(1, right - left)
    plot_height = max(1, bottom - top)
    previous_x = left
    previous_y = bottom - (float(signal[0]) - signal_min) / (signal_max - signal_min) * plot_height
    for idx in range(1, signal.size):
        x = left + idx / max(1, signal.size - 1) * plot_width
        y = bottom - (float(signal[idx]) - signal_min) / (signal_max - signal_min) * plot_height
        draw_rgb_line(pixels, width, height, previous_x, previous_y, x, y, color)
        previous_x = x
        previous_y = y


def save_waveform_snapshot_fallback_png(snapshot_path: Path, metrics: WaveformMetrics, title: str, detail_lines: list[str]) -> None:
    width, height = 1000, 620
    pixels = bytearray([255, 255, 255] * width * height)
    grid = (226, 232, 240)
    axis = (71, 85, 105)
    wave_color = (2, 132, 199)
    sync_color = (202, 138, 4)
    wave_box = (60, 45, width - 28, 385)
    sync_box = (60, 435, width - 28, height - 36)
    for left, top, right, bottom in (wave_box, sync_box):
        draw_rgb_rect_outline(pixels, width, height, left, top, right, bottom, axis)
        for step in range(1, 5):
            x = left + (right - left) * step // 5
            y = top + (bottom - top) * step // 5
            draw_rgb_line(pixels, width, height, x, top, x, bottom, grid)
            draw_rgb_line(pixels, width, height, left, y, right, y, grid)
    draw_signal_trace(pixels, width, height, metrics.waveform_v, *wave_box, wave_color)
    if metrics.sync_v.size:
        draw_signal_trace(pixels, width, height, metrics.sync_v, *sync_box, sync_color)
    metadata = {
        "Title": title,
        "Details": "\n".join(detail_lines),
        "Sample rate": f"{metrics.sample_rate_hz:.6g} Hz",
        "AIN0": "Top trace (buffered sensor)",
        "SYNC": "Bottom trace (ESP32 PWM state)",
    }
    write_rgb_png(snapshot_path, width, height, pixels, metadata)


def unused_snapshot_path(snapshot_dir: Path, sensor_id: str, filename_suffix: str) -> Path:
    """Return a new snapshot path without replacing an earlier capture."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"{safe_filename_part(sensor_id)}_{timestamp}_{safe_filename_part(filename_suffix)}"
    candidate = snapshot_dir / f"{stem}.png"
    duplicate_number = 2
    while candidate.exists():
        candidate = snapshot_dir / f"{stem}_{duplicate_number}.png"
        duplicate_number += 1
    return candidate


def save_waveform_snapshot_image(
    batch_number: str,
    sensor_id: str,
    metrics: WaveformMetrics | None,
    title: str,
    detail_lines: list[str],
    filename_suffix: str,
    *,
    output_dir: Path | None = None,
) -> Path | None:
    if metrics is None or metrics.waveform_v.size == 0:
        return None
    snapshot_dir = output_dir or (
        results_root_dir()
        / "waveform_snapshots"
        / f"lot_{safe_filename_part(batch_number)}"
    )
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = unused_snapshot_path(snapshot_dir, sensor_id, filename_suffix)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        time_axis = np.arange(metrics.waveform_v.size, dtype=float) / max(metrics.sample_rate_hz, 1.0)
        fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
        fig.suptitle(title)
        axes[0].plot(time_axis, metrics.waveform_v, color="#0284c7", linewidth=1.0)
        axes[0].set_ylabel("AIN0 V (sensor)")
        axes[0].grid(True, alpha=0.25)
        axes[1].plot(time_axis[: metrics.sync_v.size], metrics.sync_v, color="#ca8a04", linewidth=1.0)
        axes[1].set_ylabel("PWM sync (0/1)")
        axes[1].set_xlabel("Seconds")
        axes[1].grid(True, alpha=0.25)
        if detail_lines:
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
    except Exception:
        save_waveform_snapshot_fallback_png(snapshot_path, metrics, title, detail_lines)
        snapshot_path.with_suffix(".txt").write_text(title + "\n" + "\n".join(detail_lines) + "\n", encoding="utf-8")
    return snapshot_path


def _device_elapsed_values(raw_samples: tuple | list) -> list[float | None]:
    """Convert validated uint32 device timestamps to elapsed seconds."""
    if not raw_samples:
        return []
    elapsed: list[float | None] = []
    previous: int | None = None
    elapsed_us = 0
    for sample in raw_samples:
        timestamp = getattr(sample, "timestamp_us", None)
        if not isinstance(timestamp, int):
            elapsed.append(None)
            continue
        if previous is not None:
            elapsed_us += (timestamp - previous) & 0xFFFFFFFF
        previous = timestamp
        elapsed.append(elapsed_us / 1_000_000.0)
    return elapsed


def write_stream_sample_csv(
    path: Path,
    *,
    batch_number: str,
    sensor_id: str,
    specimen_id: str,
    run_id: str,
    channel: str,
    waveform: np.ndarray,
    sync: np.ndarray,
    sample_rate_hz: float,
    pwm_elapsed_offset_s: float,
    raw_samples: tuple | list = (),
) -> Path:
    """Persist replayable voltage/sync plus raw ADC/timestamps when available."""
    waveform = np.asarray(waveform, dtype=float)
    sync = np.asarray(sync, dtype=float)
    raw_samples = tuple(raw_samples)
    device_elapsed = _device_elapsed_values(raw_samples)
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=STABILITY_SAMPLE_DIAGNOSTIC_FIELDS)
        writer.writeheader()
        for index, voltage_v in enumerate(waveform):
            raw_sample = raw_samples[index] if index < len(raw_samples) else None
            timestamp_us = getattr(raw_sample, "timestamp_us", None)
            raw_count = getattr(raw_sample, "raw", None)
            elapsed_s = device_elapsed[index] if index < len(device_elapsed) else None
            writer.writerow(
                {
                    "batch_number": batch_number,
                    "sensor_id": sensor_id,
                    "specimen_id": specimen_id,
                    "run_id": run_id,
                    "channel": channel,
                    "sample_index": index,
                    "device_timestamp_us": "" if timestamp_us is None else str(timestamp_us),
                    "device_elapsed_s": "" if elapsed_s is None else f"{elapsed_s:.9f}",
                    "pwm_elapsed_s": (
                        f"{pwm_elapsed_offset_s + index / max(sample_rate_hz, 1.0):.9f}"
                    ),
                    "raw_count": "" if raw_count is None else str(raw_count),
                    "voltage_v": f"{float(voltage_v):.12g}",
                    "sync": "" if index >= len(sync) else f"{float(sync[index]):g}",
                }
            )
    return path


def write_cycle_diagnostic_csv(
    path: Path,
    *,
    batch_number: str,
    sensor_id: str,
    specimen_id: str,
    run_id: str,
    channel: str,
    cycles: tuple[CycleAnalysis, ...] | list[CycleAnalysis],
) -> Path:
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=STABILITY_CYCLE_DIAGNOSTIC_FIELDS)
        writer.writeheader()
        for cycle in cycles:
            writer.writerow(
                {
                    "batch_number": batch_number,
                    "sensor_id": sensor_id,
                    "specimen_id": specimen_id,
                    "run_id": run_id,
                    "channel": channel,
                    **cycle.as_dict(),
                }
            )
    return path


def save_stability_diagnostic_csvs(
    snapshot_path: Path,
    *,
    batch_number: str,
    sensor_id: str,
    metrics: WaveformMetrics,
    report: "StabilityCaptureReport",
    specimen_id: str = "",
    run_id: str = "",
    channel: str = "AIN0",
    raw_samples: tuple | list = (),
) -> list[Path]:
    """Persist the full production stream and every robust-peak decision.

    The files share the collision-safe PNG stem, making a timeout snapshot a
    self-contained troubleshooting bundle without widening the batch CSV with
    hundreds of cycle columns.
    """

    samples_path = snapshot_path.with_name(snapshot_path.stem + "_samples.csv")
    cycles_path = snapshot_path.with_name(snapshot_path.stem + "_cycles.csv")
    sample_rate_hz = max(float(metrics.sample_rate_hz), 1.0)
    waveform = np.asarray(metrics.waveform_v, dtype=float)
    sync = np.asarray(metrics.sync_v, dtype=float)
    write_stream_sample_csv(
        samples_path,
        batch_number=batch_number,
        sensor_id=sensor_id,
        specimen_id=specimen_id,
        run_id=run_id,
        channel=channel,
        waveform=waveform,
        sync=sync,
        sample_rate_hz=sample_rate_hz,
        pwm_elapsed_offset_s=report.pwm_elapsed_offset_s,
        raw_samples=raw_samples,
    )
    write_cycle_diagnostic_csv(
        cycles_path,
        batch_number=batch_number,
        sensor_id=sensor_id,
        specimen_id=specimen_id,
        run_id=run_id,
        channel=channel,
        cycles=report.cycle_diagnostics,
    )
    return [samples_path, cycles_path]


def save_waveform_diagnostic_bundle(
    batch_number: str,
    sensor_id: str,
    metrics: WaveformMetrics,
    report: "StabilityCaptureReport | None",
    *,
    title: str,
    detail_lines: list[str],
    filename_suffix: str,
    output_dir: Path | None = None,
    specimen_id: str = "",
    run_id: str = "",
    raw_samples: tuple | list = (),
) -> list[Path]:
    snapshot_path = save_waveform_snapshot_image(
        batch_number,
        sensor_id,
        metrics,
        title,
        detail_lines,
        filename_suffix,
        output_dir=output_dir,
    )
    if snapshot_path is None:
        return []
    paths = [snapshot_path]
    if report is not None:
        paths.extend(
            save_stability_diagnostic_csvs(
                snapshot_path,
                batch_number=batch_number,
                sensor_id=sensor_id,
                metrics=metrics,
                report=report,
                specimen_id=specimen_id,
                run_id=run_id,
                raw_samples=raw_samples,
            )
        )
    return paths


@dataclass
class ReferenceEvidenceCapture:
    waveform_v: np.ndarray
    sync_v: np.ndarray
    sample_rate_hz: float
    analysis: StabilityAnalysis
    raw_samples: tuple = ()
    stream_diagnostics: object | None = None


def _json_number(value):
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(float(value)) else str(value)
    return value


def atomic_write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def software_fingerprints() -> dict[str, dict[str, str]]:
    """Hash every source/config file that materially affects a verdict."""
    files = {
        "calibration_application": Path(__file__).resolve(),
        "shared_v1_verdict_engine": _V1_TESTER_DIR / "eltec_406mca_tester.py",
        "stability_analysis": Path(__file__).with_name("stability_analysis.py"),
        "stability_settings": Path(DEFAULT_SETTINGS_PATH),
        "esp32_backend": Path(__file__).with_name("esp32_backend.py"),
    }
    fingerprints: dict[str, dict[str, str]] = {}
    for name, path in files.items():
        if path.exists():
            fingerprints[name] = {
                "path": str(path),
                "sha256": sha256_file(path),
            }
    return fingerprints


def frozen_rule_source_error() -> str | None:
    """Refuse mixed-rule study rows when a shared decision source changes."""
    paths = {
        "shared_v1_verdict_engine": _V1_TESTER_DIR / "eltec_406mca_tester.py",
        "stability_analysis": Path(__file__).with_name("stability_analysis.py"),
        "stability_settings": Path(DEFAULT_SETTINGS_PATH),
    }
    changed = []
    for name, expected in FROZEN_RULE_SOURCE_SHA256.items():
        path = paths[name]
        if not path.is_file() or sha256_file(path) != expected:
            changed.append(name)
    if not changed:
        return None
    return (
        "Frozen calibration rules changed (" + ", ".join(changed) + "). "
        "Create a new calibration app/ruleset version before collecting more rows."
    )


def stream_diagnostics_dict(diagnostics) -> dict | None:
    if diagnostics is None:
        return None
    fields = (
        "expected_rate_hz",
        "channel",
        "received_samples",
        "drained_samples",
        "torn_lines",
        "ignored_lines",
        "timestamp_gap_count",
        "estimated_missing_samples",
        "duplicate_timestamps",
        "reordered_timestamps",
        "first_timestamp_us",
        "last_timestamp_us",
        "firmware_samples_sent",
        "firmware_adc_overruns",
        "stop_marker_seen",
        "device_span_seconds",
        "received_rate_hz",
        "measured_rate_hz",
        "rate_error_percent",
        "count_difference",
        "count_matches_firmware",
        "healthy",
    )
    return {
        field: _json_number(getattr(diagnostics, field, None))
        for field in fields
    }


def waveform_metrics_dict(metrics: WaveformMetrics | None) -> dict | None:
    if metrics is None:
        return None
    return {
        "offset_v": _json_number(metrics.offset_v),
        "sensitivity_mv": _json_number(metrics.sensitivity_mv),
        "polarity": metrics.polarity,
        "polarity_confidence": _json_number(metrics.polarity_confidence),
        "polarity_delta_mv": _json_number(metrics.polarity_delta_mv),
        "polarity_response_start_fraction": _json_number(
            metrics.polarity_response_start_fraction
        ),
        "polarity_response_end_fraction": _json_number(
            metrics.polarity_response_end_fraction
        ),
        "noise_rms_mv": _json_number(metrics.noise_rms_mv),
        "signal_rms_mv": _json_number(metrics.signal_rms_mv),
        "signal_to_noise_ratio": _json_number(metrics.signal_to_noise_ratio),
        "signal_to_noise_db": _json_number(metrics.signal_to_noise_db),
        "measured_frequency_hz": _json_number(metrics.measured_frequency_hz),
        "sample_rate_hz": _json_number(metrics.sample_rate_hz),
        "cycles_used": metrics.cycles_used,
        "cycle_pp_mv": [_json_number(value) for value in metrics.cycle_pp_mv],
        "all_cycle_pp_mv": [
            _json_number(value) for value in metrics.all_cycle_pp_mv
        ],
        "stabilized": metrics.stabilized,
        "stabilization_cycle": metrics.stabilization_cycle,
        "ignored_initial_cycles": metrics.ignored_initial_cycles,
        "warnings": list(metrics.warnings),
        "sample_count": int(np.asarray(metrics.waveform_v).size),
    }


def capture_report_dict(report: "StabilityCaptureReport | None") -> dict | None:
    if report is None:
        return None
    payload = {
        "threshold_mv": report.threshold_mv,
        "required_deltas": report.required_deltas,
        "stabilized": report.stabilized,
        "timed_out": report.timed_out,
        "unstable": report.unstable,
        "unstable_reason": report.unstable_reason,
        "phase": report.phase,
        "measurement_attempt": report.measurement_attempt,
        "measurement_failures": report.measurement_failures,
        "active_required_deltas": report.active_required_deltas,
        "measurement_cycles_required": report.measurement_cycles_required,
        "stabilization_cycle": report.stabilization_cycle,
        "stabilization_seconds": report.stabilization_seconds,
        "confirming_max_delta_mv": report.confirming_max_delta_mv,
        "last_peak_delta_mv": report.last_peak_delta_mv,
        "capture_cycles": report.capture_cycles,
        "measurement_cycles": report.measurement_cycles,
        "pwm_on_seconds": report.pwm_on_seconds,
        "pwm_elapsed_offset_s": report.pwm_elapsed_offset_s,
        "data_source": report.data_source,
    }
    return {key: _json_number(value) for key, value in payload.items()}


def validate_calibration_run_manifest(
    manifest_path: Path,
    *,
    expected_run_id: str = "",
    expected_specimen_id: str = "",
    expected_app_verdict: str = "",
) -> dict:
    """Validate a run manifest and every immutable evidence artifact."""
    manifest_path = Path(manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read run manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Run manifest root is not a JSON object.")
    try:
        manifest_schema = int(payload.get("calibration_schema_version"))
    except (TypeError, ValueError):
        manifest_schema = -1
    if manifest_schema != CALIBRATION_SCHEMA_VERSION:
        raise ValueError("Run manifest uses a different calibration schema.")
    if (
        payload.get("calibration_app_version") != CALIBRATION_APP_VERSION
        or payload.get("ruleset_id") != CALIBRATION_RULESET_ID
    ):
        raise ValueError(
            "Run manifest belongs to a different calibration app/ruleset version."
        )
    if expected_run_id and payload.get("run_id") != expected_run_id:
        raise ValueError("Run manifest ID does not match the pending result.")
    if expected_specimen_id and payload.get("specimen_id") != expected_specimen_id:
        raise ValueError("Run manifest specimen does not match the pending result.")
    prediction = payload.get("app_prediction")
    if not isinstance(prediction, dict):
        raise ValueError("Run manifest has no immutable app prediction.")
    if expected_app_verdict and prediction.get("verdict") != expected_app_verdict:
        raise ValueError("Run manifest verdict does not match the pending result.")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Run manifest has no evidence artifacts.")
    run_dir = manifest_path.parent.resolve()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ValueError(f"Run manifest artifact {index} is malformed.")
        relative_text = str(artifact.get("relative_path") or "").strip()
        if not relative_text:
            listed = Path(str(artifact.get("path") or ""))
            relative_text = listed.name
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Run manifest artifact {index} has an unsafe path.")
        artifact_path = (manifest_path.parent / relative).resolve()
        if not artifact_path.is_relative_to(run_dir):
            raise ValueError(f"Run manifest artifact {index} escapes its run directory.")
        if not artifact_path.is_file():
            raise ValueError(f"Run evidence is missing: {artifact_path}")
        try:
            expected_bytes = int(artifact["bytes"])
            expected_sha = str(artifact["sha256"]).strip().casefold()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Run manifest artifact {index} lacks integrity metadata.") from exc
        if artifact_path.stat().st_size != expected_bytes:
            raise ValueError(f"Run evidence size changed: {artifact_path}")
        if sha256_file(artifact_path).casefold() != expected_sha:
            raise ValueError(f"Run evidence hash changed: {artifact_path}")
    return payload


def _finite_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _manifest_artifact_path(manifest_path: Path, artifact: dict) -> Path:
    relative_text = str(artifact.get("relative_path") or "").strip()
    if relative_text:
        return manifest_path.parent / relative_text
    return manifest_path.parent / Path(str(artifact.get("path") or "")).name


def recover_calibration_run_from_manifest(
    manifest_path: Path,
) -> tuple[dict, WaveformMetrics, FinalResult, "StabilityCaptureReport | None", "ReferenceCalibration | None"]:
    """Rebuild the pending result view from immutable run evidence."""
    manifest_path = Path(manifest_path)
    payload = validate_calibration_run_manifest(manifest_path)
    metrics_payload = payload.get("metrics")
    prediction = payload.get("app_prediction")
    if not isinstance(metrics_payload, dict) or not isinstance(prediction, dict):
        raise ValueError("Run manifest does not contain recoverable result metrics.")

    waveform_values: list[float] = []
    sync_values: list[float] = []
    for artifact in payload.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        artifact_path = _manifest_artifact_path(manifest_path, artifact)
        name = artifact_path.name.casefold()
        if not name.endswith("_samples.csv") or "reference" in name:
            continue
        try:
            with artifact_path.open("r", newline="", encoding="utf-8") as csv_file:
                for row in csv.DictReader(csv_file):
                    voltage = _finite_float(row.get("voltage_v"))
                    sync = _finite_float(row.get("sync"), 0.0)
                    if voltage is not None:
                        waveform_values.append(voltage)
                        sync_values.append(0.0 if sync is None else sync)
        except (OSError, csv.Error) as exc:
            raise ValueError(f"Could not rebuild the saved DUT waveform: {exc}") from exc
        break

    sensitivity = _finite_float(metrics_payload.get("sensitivity_mv"))
    noise_rms = _finite_float(metrics_payload.get("noise_rms_mv"))
    cycles_used = int(metrics_payload.get("cycles_used") or 0)
    cycle_pp = [
        value
        for item in metrics_payload.get("cycle_pp_mv", [])
        if (value := _finite_float(item)) is not None
    ]
    all_cycle_pp = [
        value
        for item in metrics_payload.get("all_cycle_pp_mv", [])
        if (value := _finite_float(item)) is not None
    ]
    recovered_polarity = (
        ""
        if metrics_payload.get("polarity") is None
        else str(metrics_payload.get("polarity"))
    )
    metrics = WaveformMetrics(
        sensitivity_mv=sensitivity,
        sensitivity_amplified_mv=sensitivity,
        polarity=recovered_polarity,
        measured_frequency_hz=_finite_float(
            metrics_payload.get("measured_frequency_hz")
        ),
        cycles_used=cycles_used,
        offset_v=_finite_float(metrics_payload.get("offset_v")),
        noise_rms_mv=noise_rms,
        noise_rms_amplified_mv=noise_rms,
        signal_rms_mv=_finite_float(metrics_payload.get("signal_rms_mv")),
        signal_to_noise_ratio=_finite_float(
            metrics_payload.get("signal_to_noise_ratio")
        ),
        signal_to_noise_db=_finite_float(metrics_payload.get("signal_to_noise_db")),
        noise_cycles_used=cycles_used,
        cycle_pp_mv=cycle_pp,
        all_cycle_pp_mv=all_cycle_pp,
        stabilized=bool(metrics_payload.get("stabilized")),
        stabilization_cycle=metrics_payload.get("stabilization_cycle"),
        warnings=[str(item) for item in metrics_payload.get("warnings", [])],
        waveform_v=np.asarray(waveform_values, dtype=float),
        sync_v=np.asarray(sync_values, dtype=float),
        sample_rate_hz=_finite_float(
            metrics_payload.get("sample_rate_hz"), DEFAULT_SAMPLE_RATE_HZ
        ),
        ignored_initial_cycles=int(
            metrics_payload.get("ignored_initial_cycles") or 0
        ),
        input_range_v=WAVEFORM_INPUT_RANGE_V,
        polarity_confidence=_finite_float(
            metrics_payload.get("polarity_confidence")
        ),
        polarity_response_start_fraction=_finite_float(
            metrics_payload.get("polarity_response_start_fraction")
        ),
        polarity_response_end_fraction=_finite_float(
            metrics_payload.get("polarity_response_end_fraction")
        ),
        polarity_delta_mv=_finite_float(metrics_payload.get("polarity_delta_mv")),
    )
    report_payload = payload.get("stability_report")
    recovered_unstable = bool(
        isinstance(report_payload, dict) and report_payload.get("unstable")
    )
    official_sensitivity = (
        _finite_float(prediction.get("sensitivity_mv"))
        if "sensitivity_mv" in prediction
        else (None if recovered_unstable else sensitivity)
    )
    if "polarity" in prediction:
        official_polarity = (
            ""
            if prediction.get("polarity") is None
            else str(prediction.get("polarity"))
        )
    else:
        official_polarity = "" if recovered_unstable else metrics.polarity
    official_offset = (
        _finite_float(prediction.get("offset_v"))
        if "offset_v" in prediction
        else metrics.offset_v
    )
    final = FinalResult(
        passed=str(prediction.get("verdict") or "").upper() == "PASS",
        offset_v=official_offset,
        sensitivity_mv=official_sensitivity,
        polarity=official_polarity,
        fail_reasons=[str(item) for item in prediction.get("fail_reasons", [])],
        warnings=[str(item) for item in prediction.get("warnings", [])],
        waveform_metrics=metrics,
    )

    report = None
    if isinstance(report_payload, dict):
        report = StabilityCaptureReport(
            threshold_mv=float(report_payload.get("threshold_mv") or 0.0),
            required_deltas=int(report_payload.get("required_deltas") or 0),
            stabilized=bool(report_payload.get("stabilized")),
            timed_out=bool(report_payload.get("timed_out")),
            unstable=bool(report_payload.get("unstable")),
            unstable_reason=report_payload.get("unstable_reason"),
            phase=str(report_payload.get("phase") or ""),
            measurement_attempt=int(report_payload.get("measurement_attempt") or 1),
            measurement_failures=int(report_payload.get("measurement_failures") or 0),
            active_required_deltas=int(
                report_payload.get("active_required_deltas") or 0
            ),
            measurement_cycles_required=int(
                report_payload.get("measurement_cycles_required") or 0
            ),
            stabilization_cycle=report_payload.get("stabilization_cycle"),
            stabilization_seconds=_finite_float(
                report_payload.get("stabilization_seconds")
            ),
            confirming_max_delta_mv=_finite_float(
                report_payload.get("confirming_max_delta_mv")
            ),
            last_peak_delta_mv=_finite_float(
                report_payload.get("last_peak_delta_mv")
            ),
            capture_cycles=int(report_payload.get("capture_cycles") or 0),
            measurement_cycles=int(report_payload.get("measurement_cycles") or 0),
            pwm_on_seconds=_finite_float(report_payload.get("pwm_on_seconds"), 0.0),
            pwm_elapsed_offset_s=_finite_float(
                report_payload.get("pwm_elapsed_offset_s"), 0.0
            ),
            data_source=str(report_payload.get("data_source") or ""),
        )

    reference_payload = payload.get("reference")
    reference = None
    if isinstance(reference_payload, dict):
        baseline = _finite_float(reference_payload.get("baseline_mv"))
        lower = _finite_float(reference_payload.get("lower_mv"))
        if baseline is not None and baseline > 0:
            tolerance = (
                REFERENCE_TOLERANCE_PERCENT
                if lower is None
                else max(0.001, (baseline - lower) / baseline * 100.0)
            )
            reference = ReferenceCalibration(
                readings_mv=(baseline, baseline),
                mean_mv=baseline,
                recorded_at=str(reference_payload.get("calibrated_at") or "unknown"),
                tolerance_percent=tolerance,
            )
    return payload, metrics, final, report, reference


def save_calibration_run_evidence(
    *,
    batch_number: str,
    sensor_id: str,
    specimen_id: str,
    tester_name: str,
    filter_setup: str,
    session_id: str,
    run_id: str,
    measurement_run_number: int,
    is_synthetic: bool,
    metrics: WaveformMetrics,
    final_result: FinalResult,
    report: "StabilityCaptureReport | None",
    battery_v: float | None,
    reference_calibration: "ReferenceCalibration | None",
    reference_check_mv: float | None,
    dut_raw_samples: tuple | list = (),
    dut_stream_diagnostics=None,
    reference_capture: ReferenceEvidenceCapture | None = None,
    offset_plausibility_override: bool = False,
    rig_metadata: dict | None = None,
) -> tuple[list[Path], Path]:
    """Persist one run before annotation so re-measure never destroys it."""
    output_dir = calibration_run_dir(
        batch_number,
        specimen_id,
        run_id,
        is_synthetic=is_synthetic,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    app_mode = suggest_failure_mode(final_result)
    detail_lines = snapshot_detail_lines(
        batch_number,
        sensor_id,
        metrics,
        report=report,
    )
    detail_lines.extend(
        [
            f"Specimen: {specimen_id}",
            f"Run ID: {run_id}",
            f"App verdict: {'PASS' if final_result.passed else 'FAIL'}",
            f"App suggested mode: {app_mode or 'none'}",
        ]
    )
    paths = save_waveform_diagnostic_bundle(
        batch_number,
        sensor_id,
        metrics,
        report,
        title=f"{MODEL_NAME} {specimen_id} calibration run {measurement_run_number}",
        detail_lines=detail_lines,
        filename_suffix="dut",
        output_dir=output_dir,
        specimen_id=specimen_id,
        run_id=run_id,
        raw_samples=dut_raw_samples,
    )
    if not paths:
        raise RuntimeError("The DUT evidence bundle did not contain waveform samples.")

    if reference_capture is not None:
        reference_samples_path = output_dir / "reference_samples.csv"
        reference_cycles_path = output_dir / "reference_cycles.csv"
        reference_report = reference_capture.analysis.report
        write_stream_sample_csv(
            reference_samples_path,
            batch_number=batch_number,
            sensor_id=sensor_id,
            specimen_id=specimen_id,
            run_id=run_id,
            channel="AIN1",
            waveform=reference_capture.waveform_v,
            sync=reference_capture.sync_v,
            sample_rate_hz=reference_capture.sample_rate_hz,
            pwm_elapsed_offset_s=reference_report.pwm_elapsed_offset_s,
            raw_samples=reference_capture.raw_samples,
        )
        write_cycle_diagnostic_csv(
            reference_cycles_path,
            batch_number=batch_number,
            sensor_id=sensor_id,
            specimen_id=specimen_id,
            run_id=run_id,
            channel="AIN1",
            cycles=reference_capture.analysis.cycles,
        )
        paths.extend([reference_samples_path, reference_cycles_path])

    artifacts = [
        {
            "path": str(path),
            "relative_path": str(path.relative_to(output_dir)),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in paths
    ]
    manifest_path = output_dir / "run_manifest.json"
    reference_payload = None
    if reference_calibration is not None:
        reference_payload = {
            "source_path": str(resolved_reference_calibration_path()),
            "calibrated_at": reference_calibration.recorded_at,
            "baseline_mv": reference_calibration.mean_mv,
            "lower_mv": reference_calibration.lower_mv,
            "upper_mv": reference_calibration.upper_mv,
            "check_mv": reference_check_mv,
            "drift_percent": (
                None
                if reference_check_mv is None
                else reference_calibration.drift_percent(reference_check_mv)
            ),
        }
    manifest = {
        "calibration_schema_version": CALIBRATION_SCHEMA_VERSION,
        "calibration_app_version": CALIBRATION_APP_VERSION,
        "ruleset_id": CALIBRATION_RULESET_ID,
        "software_fingerprints": software_fingerprints(),
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": session_id,
        "run_id": run_id,
        "measurement_run_number": measurement_run_number,
        "batch_number": batch_number,
        "sensor_id": sensor_id,
        "specimen_id": specimen_id,
        "tester_name": tester_name,
        "is_synthetic": is_synthetic,
        "rig": rig_metadata or {},
        "filter_setup": filter_setup,
        "app_prediction": {
            "verdict": "PASS" if final_result.passed else "FAIL",
            "offset_v": _json_number(final_result.offset_v),
            "sensitivity_mv": _json_number(final_result.sensitivity_mv),
            "polarity": final_result.polarity,
            "fail_reasons": list(final_result.fail_reasons),
            "suggested_failure_mode": app_mode,
            "warnings": list(final_result.warnings),
        },
        "policy": {
            "offset_min_v": OFFSET_MIN_V,
            "offset_max_v": OFFSET_MAX_V,
            "sensitivity_min_mv": FILTER_SPECS_MV[filter_setup],
            "min_signal_to_noise_ratio": MIN_SIGNAL_TO_NOISE_RATIO,
            "peak_delta_threshold_mv": (
                None if report is None else report.threshold_mv
            ),
            "qualification_deltas": DUT_STABILITY_CONFIRMATION_DELTAS,
            "measurement_cycles": SENSITIVITY_MEASUREMENT_CYCLES,
            "max_measurement_attempts": MAX_MEASUREMENT_ATTEMPTS,
            "stability_timeout_s": STABILITY_TIMEOUT_S,
            "pwm_hz": EMITTER_PWM_FREQUENCY_HZ,
            "pwm_duty_percent": EMITTER_PWM_DUTY_CYCLE,
        },
        "battery_v": battery_v,
        "offset_plausibility_override": offset_plausibility_override,
        "metrics": waveform_metrics_dict(metrics),
        "stability_report": capture_report_dict(report),
        "dut_stream_diagnostics": stream_diagnostics_dict(dut_stream_diagnostics),
        "reference": reference_payload,
        "reference_stream_diagnostics": stream_diagnostics_dict(
            None if reference_capture is None else reference_capture.stream_diagnostics
        ),
        "reference_stability_report": (
            None
            if reference_capture is None
            else capture_report_dict(
                StabilityCaptureReport.from_analysis(
                    reference_capture.analysis,
                    data_source="esp32_reference",
                )
            )
        ),
        "artifacts": artifacts,
    }
    atomic_write_json(manifest_path, manifest)
    return [*paths, manifest_path], manifest_path


def save_calibration_review_record(
    *,
    batch_number: str,
    sensor_id: str,
    run_id: str,
    review: CalibrationReview,
    is_synthetic: bool,
    production_failure_mode: str,
    operator_comment: str,
    related_run_ids: list[str],
    evidence_paths: list[Path],
) -> Path:
    output_dir = calibration_run_dir(
        batch_number,
        review.specimen_id,
        run_id,
        is_synthetic=is_synthetic,
    )
    review_path = output_dir / "review.json"
    payload = {
        "calibration_schema_version": CALIBRATION_SCHEMA_VERSION,
        "calibration_app_version": CALIBRATION_APP_VERSION,
        "ruleset_id": CALIBRATION_RULESET_ID,
        "batch_number": batch_number,
        "sensor_id": sensor_id,
        "selected_run_id": run_id,
        "related_run_ids": list(related_run_ids),
        "review": review.to_dict(),
        "production_failure_mode": production_failure_mode,
        "operator_comment": operator_comment.strip(),
        "evidence_paths": [str(path) for path in evidence_paths],
    }
    return atomic_write_json(review_path, payload)


def snapshot_detail_lines(
    batch_number: str,
    sensor_id: str,
    metrics: WaveformMetrics,
    comment: str = "",
    report: "StabilityCaptureReport | None" = None,
) -> list[str]:
    sensitivity_text = (
        f"{metrics.sensitivity_mv:.2f} mV"
        if metrics.stabilized
        else "Not measured (unstable)"
    )
    polarity_text = (
        f"{metrics.polarity} ({polarity_good_bad(metrics.polarity)})"
        if metrics.stabilized
        else "Not measured"
    )
    lines = [
        f"Batch: {batch_number}",
        f"Sensor: {sensor_id}",
        f"Sensitivity: {sensitivity_text}",
        f"Polarity: {polarity_text}",
    ]
    if metrics.offset_v is not None:
        lines.insert(2, f"Offset: {metrics.offset_v:.3f} V")
    if metrics.measured_frequency_hz is not None:
        lines.append(f"PWM sync: {metrics.measured_frequency_hz:.3f} Hz")
    if report is not None:
        state = "stabilized" if report.stabilized else "unstable"
        lines.append(
            f"Stability: {state}; attempt {report.measurement_attempt}; "
            f"{report.active_required_deltas} deltas <= "
            f"{report.threshold_mv:.3f} mV"
        )
        if report.stabilization_seconds is not None:
            lines.append(
                f"Stable at cycle {report.stabilization_cycle}, "
                f"{report.stabilization_seconds:.3f} s after PWM on"
            )
        if report.last_peak_delta_mv is not None:
            lines.append(f"Last observed peak delta: {report.last_peak_delta_mv:.6f} mV")
    if comment.strip():
        lines.append("Comment: " + plot_text_line(comment, 220))
    return lines


# --------------------------------------------------------------------------- #
# ESP32 + ADS1256 device adapter for the v4-compatible measurement engine
# --------------------------------------------------------------------------- #
class EmitterEsp32Rig(Esp32EmitterRig):
    """Add NumPy frame/adaptive-capture methods to the serial backend."""

    STREAM_CHUNK_SAMPLES = 100  # 0.1 s at 1000 SPS: responsive progress/early exit
    STREAM_TIMEOUT_S = 2.0

    @staticmethod
    def _sample_arrays(samples) -> tuple[np.ndarray, np.ndarray]:
        waveform = np.asarray([sample.volts for sample in samples], dtype=float)
        sync = np.asarray([sample.sync for sample in samples], dtype=float)
        return waveform, sync

    @staticmethod
    def _validate_stream_diagnostics(diagnostics, *, minimum_samples: int) -> None:
        """Reject incomplete/corrupted streams instead of recording a verdict."""
        problems: list[str] = []
        if diagnostics.received_samples < minimum_samples:
            problems.append(
                f"short capture ({diagnostics.received_samples}/{minimum_samples} samples)"
            )
        if diagnostics.torn_lines:
            problems.append(f"{diagnostics.torn_lines} malformed serial records")
        if diagnostics.timestamp_gap_count:
            problems.append(
                f"{diagnostics.timestamp_gap_count} timestamp gaps "
                f"(~{diagnostics.estimated_missing_samples} missing samples)"
            )
        if diagnostics.duplicate_timestamps:
            problems.append(f"{diagnostics.duplicate_timestamps} duplicate timestamps")
        if diagnostics.reordered_timestamps:
            problems.append(f"{diagnostics.reordered_timestamps} reordered timestamps")
        if diagnostics.firmware_adc_overruns:
            problems.append(
                f"{diagnostics.firmware_adc_overruns} ADC conversions overran the serial loop"
            )
        if diagnostics.count_matches_firmware is False:
            problems.append(
                "host/firmware sample counts differ "
                f"({diagnostics.received_samples}/{diagnostics.firmware_samples_sent})"
            )
        rate_error = diagnostics.rate_error_percent
        if rate_error is not None and abs(rate_error) > 2.0:
            problems.append(
                f"sample rate is {diagnostics.measured_rate_hz:.1f} Hz "
                f"({rate_error:+.1f}% from expected)"
            )
        if problems:
            raise Esp32BackendError(
                "ESP32 waveform stream was not reliable; nothing was recorded: "
                + "; ".join(problems)
                + ". Check the USB cable and close other serial programs, then retry."
            )

    def read_waveform_frame(
        self,
        cycles: int,
        waveform_range_v: float,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
        channel: str = "sensor",
    ) -> tuple[np.ndarray, np.ndarray, float]:
        del waveform_range_v
        self.connect()
        self.last_stream_samples = ()
        self.last_stream_diagnostics = None
        self.last_stream_channel = str(channel)
        target_scans = int(
            math.ceil((cycles / expected_frequency_hz) * sample_rate_hz)
        )
        samples = []
        header = self.start_stream(channel)
        if not math.isclose(header.sample_rate_hz, sample_rate_hz, rel_tol=0.01):
            try:
                self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
            finally:
                raise Esp32BackendError(
                    f"ESP32 advertised {header.sample_rate_hz:g} samples/s; "
                    f"this tester requires {sample_rate_hz:g}. Re-flash "
                    f"{EXPECTED_FIRMWARE_PREFIX}1.7 or newer."
                )
        diagnostics = None
        try:
            while len(samples) < target_scans:
                chunk = self.read_stream(
                    max_samples=min(self.STREAM_CHUNK_SAMPLES, target_scans - len(samples)),
                    timeout_s=self.STREAM_TIMEOUT_S,
                )
                if not chunk:
                    raise Esp32BackendError(
                        f"ESP32 waveform stream stalled after {len(samples)}/{target_scans} samples."
                    )
                samples.extend(chunk)
        finally:
            if self.is_streaming:
                diagnostics = self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
        if diagnostics is None:
            diagnostics = self.stream_diagnostics
        if diagnostics is None:
            raise Esp32BackendError("ESP32 stream diagnostics were unavailable.")
        self._validate_stream_diagnostics(diagnostics, minimum_samples=target_scans)
        self.last_stream_samples = tuple(samples[:target_scans])
        self.last_stream_diagnostics = diagnostics
        waveform, sync = self._sample_arrays(samples[:target_scans])
        actual_scan_rate = diagnostics.measured_rate_hz or header.sample_rate_hz
        return waveform, sync, float(actual_scan_rate)

    def read_reference_until_stable(
        self,
        *,
        waveform_range_v: float,
        settings: StabilitySettings,
        pwm_started_monotonic: float,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
        progress=None,
        cancelled=None,
    ) -> tuple[np.ndarray, np.ndarray, float, StabilityAnalysis]:
        """Adaptively stabilize AIN1, then retain five fresh reference cycles."""
        return self.read_waveform_until_stable(
            waveform_range_v=waveform_range_v,
            settings=reference_stability_settings(settings),
            pwm_started_monotonic=pwm_started_monotonic,
            sample_rate_hz=sample_rate_hz,
            expected_frequency_hz=expected_frequency_hz,
            stability_timeout_s=STABILITY_TIMEOUT_S,
            measurement_cycles=REFERENCE_MEASUREMENT_CYCLES,
            progress=progress,
            cancelled=cancelled,
            channel="ref",
        )

    def read_waveform_until_stable(
        self,
        *,
        waveform_range_v: float,
        settings: StabilitySettings,
        pwm_started_monotonic: float,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
        stability_timeout_s: float = STABILITY_TIMEOUT_S,
        measurement_cycles: int = SENSITIVITY_MEASUREMENT_CYCLES,
        progress=None,
        preview=None,
        cancelled=None,
        channel: str = "sensor",
    ) -> tuple[np.ndarray, np.ndarray, float, StabilityAnalysis]:
        """Capture one uninterrupted PWM-on stream through stability/measurement.

        The same samples drive sync validation, peak-delta progress, optional
        live preview, and the final result. The stability deadline is measured
        from PWM activation; a started measurement window may finish afterward.
        """
        del waveform_range_v
        self.connect()
        self.last_stream_samples = ()
        self.last_stream_diagnostics = None
        self.last_stream_channel = str(channel)
        samples = []
        header = self.start_stream(channel)
        if not math.isclose(header.sample_rate_hz, sample_rate_hz, rel_tol=0.01):
            try:
                self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
            finally:
                raise Esp32BackendError(
                    f"ESP32 advertised {header.sample_rate_hz:g} samples/s; "
                    f"this tester requires {sample_rate_hz:g}. Re-flash {EXPECTED_FIRMWARE_PREFIX}1.7 or newer."
                )
        actual_scan_rate = float(header.sample_rate_hz)
        pwm_elapsed_offset_s = max(0.0, time.monotonic() - pwm_started_monotonic)
        is_reference = str(channel).lower() in {"ref", "reference", "ain1"}
        enforce_retry_policy = not is_reference
        analysis_settings = (
            dut_stability_settings(settings) if enforce_retry_policy else settings
        )
        maximum_measurement_cycles = measurement_cycles
        # Enough room for a final measurement window that begins at the
        # stability deadline, plus two edge-closing cycles. This is only a
        # safety ceiling; the state machine normally stops earlier.
        max_stream_s = max(0.0, stability_timeout_s - pwm_elapsed_offset_s) + (
            maximum_measurement_cycles + 2
        ) / expected_frequency_hz
        # N samples span N-1 intervals, so include the sample at the safety
        # ceiling rather than stopping one conversion before it.
        target_scans = int(math.ceil(max_stream_s * actual_scan_rate)) + 1
        diagnostics = None
        stream_data_source = (
            "esp32_reference"
            if is_reference
            else "esp32_v6_1_failure_calibration"
        )
        analysis = analyze_stability(
            [], [], actual_scan_rate, analysis_settings,
            pwm_elapsed_offset_s=pwm_elapsed_offset_s,
            stability_deadline_s=stability_timeout_s,
            measurement_cycles_required=measurement_cycles,
            enforce_measurement_stability=enforce_retry_policy,
            max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
            data_source=stream_data_source,
        )
        sync_validated = False
        samples_through_deadline = max(
            1,
            int(
                math.ceil(
                    max(0.0, stability_timeout_s - pwm_elapsed_offset_s)
                    * actual_scan_rate
                )
            )
            + 1,
        )
        try:
            while len(samples) < target_scans:
                if cancelled is not None and cancelled():
                    raise Esp32BackendError("Measurement was cancelled.")
                read_count = min(
                    self.STREAM_CHUNK_SAMPLES,
                    target_scans - len(samples),
                )
                if not analysis.report.stabilized:
                    # Approach the inclusive deadline exactly so the closing
                    # sample at 20.000 s is analyzed without a coarse serial
                    # chunk overshooting it.
                    read_count = min(
                        read_count,
                        max(1, samples_through_deadline - len(samples)),
                    )
                chunk = self.read_stream(
                    max_samples=read_count,
                    timeout_s=self.STREAM_TIMEOUT_S,
                )
                if not chunk:
                    raise Esp32BackendError(
                        f"ESP32 waveform stream stalled after {len(samples)}/{target_scans} samples."
                    )
                samples.extend(chunk)
                waveform_np, sync_np = self._sample_arrays(samples)
                analysis = analyze_stability(
                    waveform_np,
                    sync_np,
                    actual_scan_rate,
                    analysis_settings,
                    pwm_elapsed_offset_s=pwm_elapsed_offset_s,
                    stability_deadline_s=stability_timeout_s,
                    measurement_cycles_required=measurement_cycles,
                    enforce_measurement_stability=enforce_retry_policy,
                    max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
                    data_source=stream_data_source,
                )
                captured_s = (
                    0.0 if not samples else (len(samples) - 1) / actual_scan_rate
                )
                if not sync_validated:
                    rising_edges = rising_edge_indices(sync_np)
                    validation_observation_limit_s = (
                        (SYNC_VALIDATION_CYCLES + 2) / expected_frequency_hz
                    )
                    if (
                        len(rising_edges) >= SYNC_VALIDATION_CYCLES + 1
                        or captured_s >= validation_observation_limit_s
                    ):
                        try:
                            validate_rising_sync_cycles(
                                sync_np,
                                actual_scan_rate,
                                expected_frequency_hz=expected_frequency_hz,
                                cycles_required=SYNC_VALIDATION_CYCLES,
                            )
                        except SyncValidationError as exc:
                            raise HardwareNotReadyError(
                                f"ESP32 {exc}. Check firmware and "
                                f"{EMITTER_PWM_CHANNEL}, then measure again."
                            ) from exc
                        sync_validated = True
                if progress is not None:
                    progress(analysis)
                if preview is not None:
                    preview(
                        waveform_np[-STREAM_PREVIEW_MAX_SAMPLES:].copy(),
                        sync_np[-STREAM_PREVIEW_MAX_SAMPLES:].copy(),
                    )
                if analysis.report.measurement_complete or analysis.report.unstable:
                    break
        finally:
            if self.is_streaming:
                diagnostics = self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)

        if diagnostics is None:
            diagnostics = self.stream_diagnostics
        if diagnostics is None:
            raise Esp32BackendError("ESP32 stream diagnostics were unavailable.")
        # STREAM,STOP can drain a short tail that was sampled while PWM was
        # still on. Retain it so timeout troubleshooting gets the full stream
        # represented by the backend diagnostics.
        drained_samples = list(self.drained_samples)
        if drained_samples:
            samples.extend(drained_samples)
        self._validate_stream_diagnostics(diagnostics, minimum_samples=len(samples))
        waveform_np, sync_np = self._sample_arrays(samples)
        analysis = analyze_stability(
            waveform_np,
            sync_np,
            actual_scan_rate,
            analysis_settings,
            pwm_elapsed_offset_s=pwm_elapsed_offset_s,
            stability_deadline_s=stability_timeout_s,
            measurement_cycles_required=measurement_cycles,
            enforce_measurement_stability=enforce_retry_policy,
            max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
            data_source=stream_data_source,
        )
        if not sync_validated:
            raise HardwareNotReadyError(
                "ESP32 PWM sync could not be validated before the capture ended."
            )
        if not (analysis.report.measurement_complete or analysis.report.unstable):
            raise Esp32BackendError(
                "Adaptive capture reached its safety limit before producing a complete decision."
            )
        self.last_stream_samples = tuple(samples)
        self.last_stream_diagnostics = diagnostics
        return waveform_np, sync_np, actual_scan_rate, analysis


# --------------------------------------------------------------------------- #
# Battery watcher helpers
# --------------------------------------------------------------------------- #
class BatteryTooLowError(RuntimeError):
    """Raised mid-measurement when the 6 V SLA is at/below the block threshold."""

    def __init__(self, battery_v: float) -> None:
        super().__init__(f"Battery too low to test: {battery_v:.2f} V (minimum {BATTERY_MIN_V:.1f} V).")
        self.battery_v = battery_v


class HardwareNotReadyError(RuntimeError):
    """Raised before/at the start of a measurement when the rig is not wired
    up (missing sensor, unwired battery divider, no PWM sync). Nothing is
    measured or recorded; the message tells the technician what to plug in."""


class ReferenceGateError(HardwareNotReadyError):
    """AIN1 is uncalibrated, invalidated, or outside its calibrated window."""


class ReferenceCheckFailedError(ReferenceGateError):
    def __init__(self, reading_mv: float, calibration: ReferenceCalibration) -> None:
        drift = calibration.drift_percent(reading_mv)
        super().__init__(
            f"Reference unit measured {reading_mv:.2f} mV, {drift:+.1f}% from the "
            f"{calibration.mean_mv:.2f} mV calibration. The allowed range is "
            f"{calibration.lower_mv:.2f}-{calibration.upper_mv:.2f} mV "
            f"(+/-{calibration.tolerance_percent:g}%). The sensor under test was not read. "
            "Replace/check the emitter, then recalibrate the reference unit before testing another sensor."
        )
        self.reading_mv = reading_mv
        self.calibration = calibration


def battery_state_for(battery_v: float | None) -> str:
    """Classify a battery reading: 'fault', 'low', 'warn', 'ok', or 'unknown'.

    'fault' means the number cannot be a real 6 V SLA through the divider
    (missing battery / AIN7 not wired / floating input) - testing is blocked.
    """
    if battery_v is None:
        return "unknown"
    if battery_v < BATTERY_FAULT_MIN_V or battery_v > BATTERY_FAULT_MAX_V:
        return "fault"
    if battery_v <= BATTERY_MIN_V:
        return "low"
    if battery_v <= BATTERY_WARN_V:
        return "warn"
    return "ok"


def battery_gauge_fraction(battery_v: float | None) -> float:
    """Map the 6 V SLA reading to a 0..1 fill for the header battery gauge."""
    if battery_v is None:
        return 0.0
    span = BATTERY_GAUGE_FULL_V - BATTERY_MIN_V
    return max(0.0, min(1.0, (battery_v - BATTERY_MIN_V) / span))


def apply_signal_quality_gate(final: FinalResult, metrics: WaveformMetrics | None) -> FinalResult:
    """Fail captures that are mostly noise (e.g. the emitter is not being driven).

    ``evaluate_result`` can pass a capture on amplitude + polarity alone, but a
    dead/undriven emitter produces sensor noise that can still look "big enough".
    Here we require a real coherent signal above the noise, mutating ``final``
    in place so the fail reason flows through to the CSV, autosave and UI.
    """
    if metrics is None:
        return final
    snr = metrics.signal_to_noise_ratio
    if snr is None:
        reason = (
            "Signal quality could not be verified (SNR unavailable) - confirm the "
            "emitter is powered and driving before retesting."
        )
    elif math.isfinite(snr) and snr < MIN_SIGNAL_TO_NOISE_RATIO:
        db = metrics.signal_to_noise_db
        db_text = "" if db is None or not math.isfinite(db) else f", {db:.1f} dB"
        reason = (
            f"Signal-to-noise too low: SNR {snr:.2f}{db_text} (minimum "
            f"{MIN_SIGNAL_TO_NOISE_RATIO:.1f}). This looks like sensor noise with no "
            "emitter response - check the emitter drive."
        )
    else:
        return final
    if reason not in final.fail_reasons:
        final.fail_reasons.append(reason)
    final.passed = False
    return final


# --------------------------------------------------------------------------- #
# V6.1 adaptive-stability capture telemetry
# --------------------------------------------------------------------------- #
@dataclass
class StabilityCaptureReport:
    threshold_mv: float
    required_deltas: int
    stabilized: bool = False
    timed_out: bool = False
    unstable: bool = False
    unstable_reason: str | None = None
    phase: str = "stabilizing"
    measurement_attempt: int = 1
    measurement_failures: int = 0
    active_required_deltas: int = 5
    measurement_cycles_required: int = 10
    stabilization_cycle: int | None = None
    stabilization_seconds: float | None = None
    confirming_max_delta_mv: float | None = None
    last_peak_delta_mv: float | None = None
    capture_cycles: int = 0
    measurement_cycles: int = 0
    pwm_on_seconds: float = 0.0
    pwm_elapsed_offset_s: float = 0.0
    data_source: str = ""
    cycle_diagnostics: tuple[CycleAnalysis, ...] = ()

    @classmethod
    def from_analysis(
        cls,
        analysis: StabilityAnalysis,
        *,
        data_source: str,
        pwm_on_seconds: float | None = None,
    ) -> "StabilityCaptureReport":
        source = analysis.report
        return cls(
            threshold_mv=source.configured_threshold_mv,
            required_deltas=source.configured_confirmation_count,
            stabilized=source.stabilized,
            timed_out=source.timed_out,
            unstable=source.unstable,
            unstable_reason=source.unstable_reason,
            phase=source.phase,
            measurement_attempt=source.measurement_attempt,
            measurement_failures=source.measurement_failures,
            active_required_deltas=source.active_confirmation_count,
            measurement_cycles_required=source.measurement_cycles_required,
            stabilization_cycle=source.stabilization_cycle,
            stabilization_seconds=source.stabilization_elapsed_s,
            confirming_max_delta_mv=source.confirming_window_max_delta_mv,
            last_peak_delta_mv=source.last_delta_mv,
            capture_cycles=source.capture_cycles,
            measurement_cycles=source.measurement_cycle_count,
            pwm_on_seconds=(
                source.total_pwm_on_seconds
                if pwm_on_seconds is None
                else max(0.0, float(pwm_on_seconds))
            ),
            pwm_elapsed_offset_s=source.pwm_elapsed_offset_s,
            data_source=data_source,
            cycle_diagnostics=analysis.cycles,
        )

    def csv_fields(self) -> dict[str, str]:
        return {
            "stabilized": "YES" if self.stabilized else "NO",
            "stability_timeout": "YES" if self.timed_out else "NO",
            "stability_threshold_mv": f"{self.threshold_mv:.6f}",
            "stability_required_deltas": str(self.required_deltas),
            "stabilization_cycle": "" if self.stabilization_cycle is None else str(self.stabilization_cycle),
            "stabilization_seconds": _fmt_optional_float(self.stabilization_seconds, 3),
            "stability_window_max_delta_mv": _fmt_optional_float(self.confirming_max_delta_mv, 6),
            "last_peak_delta_mv": _fmt_optional_float(self.last_peak_delta_mv, 6),
            "capture_cycles": str(self.capture_cycles),
            "measurement_cycles": str(self.measurement_cycles),
            "stability_phase": self.phase,
            "measurement_attempt": str(self.measurement_attempt),
            "measurement_failures": str(self.measurement_failures),
            "active_stability_required_deltas": str(self.active_required_deltas),
            "active_measurement_cycles_required": str(self.measurement_cycles_required),
            "pwm_on_seconds": f"{self.pwm_on_seconds:.3f}",
            "data_source": self.data_source,
        }


def analyze_v6_stable_measurement(
    waveform_v: np.ndarray,
    sync_v: np.ndarray,
    sample_rate_hz: float,
    analysis: StabilityAnalysis,
    *,
    offset_v: float,
    input_range_v: float,
) -> WaveformMetrics:
    """Apply production signal math to the successful 20-cycle window."""
    segments = analysis.measurement_segments
    expected_cycles = analysis.report.measurement_cycles_required
    if len(segments) != expected_cycles:
        raise ValueError(
            f"Stable measurement requires {expected_cycles} complete cycles; "
            f"received {len(segments)}."
        )
    first_start = segments[0][0]
    last_end = segments[-1][1]
    # Include the low sample before the first rising edge and the closing edge
    # after the last selected cycle so the shared edge detector sees them all.
    slice_start = max(0, first_start - 1)
    slice_end = min(len(waveform_v), last_end + 1)
    measured_waveform = waveform_v[slice_start:slice_end]
    measured_sync = sync_v[slice_start:slice_end]
    metrics = analyze_esp32_waveform(
        waveform_v=measured_waveform,
        sync_v=measured_sync,
        sample_rate_hz=sample_rate_hz,
        am502_gain=RIG_GAIN,
        sync_edge=PROCEDURE_SYNC_EDGE,
        stability_window_cycles=expected_cycles,
        settle_cycles=0,
        input_range_v=input_range_v,
    )
    if metrics.cycles_used != expected_cycles:
        raise Esp32BackendError(
            f"Selected stability window contained {metrics.cycles_used} analyzable cycles; "
            f"expected {expected_cycles}. Nothing was recorded."
        )
    metrics.warnings = [
        warning for warning in metrics.warnings
        if not warning.startswith("Waveform did not stabilize")
    ]
    metrics.stabilized = True
    metrics.stability_change_pct = None
    metrics.stabilization_cycle = analysis.report.stabilization_cycle
    metrics.ignored_initial_cycles = analysis.report.stabilization_cycle or 0
    metrics.offset_v = offset_v
    # Keep the complete PWM-on transient available for the result scope and
    # troubleshooting snapshot while the numerical metrics remain restricted
    # to the selected post-stability cycles above.
    full_edges, full_frequency, full_sync_warnings = find_sync_edges(
        sync_v,
        sample_rate_hz,
        expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
        edge=PROCEDURE_SYNC_EDGE,
    )
    metrics.waveform_v = waveform_v
    metrics.sync_v = sync_v
    metrics.edges = full_edges
    metrics.measured_frequency_hz = full_frequency
    for warning in full_sync_warnings:
        rewritten = warning.replace("blade sync", "ESP32 PWM sync").replace("Blade sync", "ESP32 PWM sync")
        if rewritten not in metrics.warnings:
            metrics.warnings.append(rewritten)
    return metrics


def build_stability_timeout_result(
    waveform_v: np.ndarray,
    sync_v: np.ndarray,
    sample_rate_hz: float,
    analysis: StabilityAnalysis,
    *,
    offset_v: float,
    input_range_v: float,
) -> tuple[WaveformMetrics, FinalResult]:
    """Build a diagnostic waveform plus a FAIL with no official signal result."""
    edges, frequency, sync_warnings = find_sync_edges(
        sync_v,
        sample_rate_hz,
        expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
        edge=PROCEDURE_SYNC_EDGE,
    )
    detail = analysis.report.unstable_reason
    if not detail:
        detail = (
            f"Waveform peak did not stabilize within {analysis.report.stability_deadline_s:.1f} s: "
            f"required {analysis.report.active_confirmation_count} consecutive peak deltas "
            f"at or below {analysis.report.configured_threshold_mv:.3f} mV."
        )
    reason = "Unstable: " + detail
    warnings = [
        warning.replace("blade sync", "ESP32 PWM sync").replace("Blade sync", "ESP32 PWM sync")
        for warning in sync_warnings
    ]
    metrics = WaveformMetrics(
        sensitivity_mv=0.0,
        sensitivity_amplified_mv=0.0,
        polarity="NOT MEASURED",
        measured_frequency_hz=frequency,
        cycles_used=0,
        offset_v=offset_v,
        all_cycle_pp_mv=[cycle.peak_to_peak_v * 1000.0 for cycle in analysis.cycles],
        stabilized=False,
        stabilization_cycle=None,
        warnings=warnings + [reason],
        edges=edges,
        waveform_v=waveform_v,
        sync_v=sync_v,
        sample_rate_hz=sample_rate_hz,
        ignored_initial_cycles=0,
        input_range_v=input_range_v,
    )
    fail_reasons = [reason]
    if not (OFFSET_MIN_V <= offset_v <= OFFSET_MAX_V):
        fail_reasons.insert(
            0,
            f"Offset out of range: {offset_v:.3f} V, expected {OFFSET_MIN_V:.1f} to {OFFSET_MAX_V:.1f} V.",
        )
    final = FinalResult(
        passed=False,
        offset_v=offset_v,
        sensitivity_mv=None,
        polarity="",
        fail_reasons=fail_reasons,
        warnings=warnings,
        waveform_metrics=metrics,
    )
    return metrics, final


# --------------------------------------------------------------------------- #
# v6 UI toolkit - colors, easing, animation engine
# --------------------------------------------------------------------------- #
def hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(channel)))) for channel in rgb)


def mix_color(color_a: str, color_b: str, t: float) -> str:
    """Linear blend from color_a (t=0) to color_b (t=1)."""
    t = max(0.0, min(1.0, t))
    a = hex_to_rgb(color_a)
    b = hex_to_rgb(color_b)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def gradient_color(stops: list[str], t: float) -> str:
    """Sample a multi-stop gradient at position t in [0, 1]."""
    if len(stops) == 1:
        return stops[0]
    t = max(0.0, min(1.0, t)) * (len(stops) - 1)
    idx = min(int(t), len(stops) - 2)
    return mix_color(stops[idx], stops[idx + 1], t - idx)


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_in_out(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def ease_out_back(t: float) -> float:
    """Ease-out with a small overshoot - used for 'pop' effects."""
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


class Animator:
    """Single-clock animation engine (~60 fps, named + cancellable).

    Every animation is registered under a name; starting a new animation with
    the same name replaces the old one, and cancel_prefix() lets the app drop
    a whole family (e.g. every "step:*" animation when a step is torn down).
    Frame callbacks that raise TclError (their widget was destroyed) silently
    stop the animation, so callers never have to guard widget lifetime.

    ONE after() timer drives every active animation. This matters: scheduling
    a separate 15 ms timer per animation keeps the Tk event queue permanently
    busy, which starves Tk's idle queue - and geometry propagation/redraws run
    as idle tasks, so layout would visibly lag while animations play.
    """

    FRAME_MS = 16

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self._anims: dict[str, dict] = {}
        self._job: str | None = None

    def animate(
        self,
        name: str,
        duration_ms: int,
        on_frame,
        easing=ease_out_cubic,
        on_done=None,
        loop: bool = False,
        delay_ms: int = 0,
    ) -> None:
        self._anims[name] = {
            "start": time.perf_counter() + delay_ms / 1000.0,
            "duration": max(1, duration_ms) / 1000.0,
            "on_frame": on_frame,
            "easing": easing,
            "on_done": on_done,
            "loop": loop,
        }
        if self._job is None:
            self._job = self.root.after(self.FRAME_MS, self._tick)

    def _tick(self) -> None:
        self._job = None
        now = time.perf_counter()
        for name in list(self._anims):
            anim = self._anims.get(name)
            if anim is None:
                continue
            elapsed = (now - anim["start"]) / anim["duration"]
            if elapsed < 0.0:  # still in its start delay
                continue
            if anim["loop"]:
                finished = False
                raw_t = elapsed % 1.0
            else:
                finished = elapsed >= 1.0
                raw_t = 1.0 if finished else elapsed
            easing = anim["easing"]
            try:
                anim["on_frame"](easing(raw_t) if easing is not None else raw_t)
            except tk.TclError:
                self._anims.pop(name, None)
                continue
            if finished:
                self._anims.pop(name, None)
                if anim["on_done"] is not None:
                    try:
                        anim["on_done"]()
                    except tk.TclError:
                        pass
        if self._anims and self._job is None:
            self._job = self.root.after(self.FRAME_MS, self._tick)

    def cancel(self, name: str) -> None:
        self._anims.pop(name, None)

    def cancel_prefix(self, prefix: str) -> None:
        for name in [key for key in self._anims if key.startswith(prefix)]:
            self._anims.pop(name, None)

    def cancel_all(self) -> None:
        self._anims.clear()
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except (tk.TclError, ValueError):
                pass
            self._job = None


def rounded_rect_points(x0: float, y0: float, x1: float, y1: float, r: float) -> list[float]:
    """Point list for a smooth=True polygon that renders as a rounded rect."""
    r = max(1.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]


def draw_round_rect(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, r: float, **kwargs) -> int:
    return canvas.create_polygon(rounded_rect_points(x0, y0, x1, y1, r), smooth=True, **kwargs)


def draw_horizontal_gradient(canvas: tk.Canvas, x0: int, y0: int, x1: int, y1: int, stops: list[str], tags: str, step: int = 4) -> None:
    """Paint a horizontal multi-stop gradient as thin vertical line segments."""
    span = max(1, x1 - x0)
    for x in range(x0, x1, step):
        color = gradient_color(stops, (x - x0) / span)
        canvas.create_line(x, y0, x, y1, fill=color, width=step, tags=tags)


# --------------------------------------------------------------------------- #
# v6 UI toolkit - custom widgets
# --------------------------------------------------------------------------- #
class RoundButton(tk.Canvas):
    """Rounded, hover-animated button (site-style primary / outline / ghost)."""

    PALETTES = {
        "primary": {
            "fill": ELTEC_BLUE, "hover": ELTEC_BLUE_DEEP, "press": "#092e6d",
            "fg": "#ffffff", "outline": "",
            "disabled_fill": PRIMARY_DISABLED, "disabled_fg": "#eef2f7", "disabled_outline": "",
        },
        "outline": {
            "fill": CARD_BG, "hover": "#eaf0fc", "press": "#dbe6fa",
            "fg": ELTEC_BLUE_DARK, "outline": "#b6c6e8",
            "disabled_fill": PAGE_BG, "disabled_fg": "#aeb9c5", "disabled_outline": "#d7deea",
        },
        "ghost": {
            "fill": GHOST_BG, "hover": GHOST_HOVER, "press": "#c3cfe8",
            "fg": ELTEC_BLUE_DARK, "outline": "",
            "disabled_fill": PAGE_BG, "disabled_fg": "#aeb9c5", "disabled_outline": "",
        },
    }
    SIZE_PADS = {"lg": (26, 14), "md": (20, 11), "sm": (16, 8)}

    def __init__(
        self,
        parent: tk.Widget,
        text: str = "",
        command=None,
        kind: str = "primary",
        size: str = "lg",
        font=("DejaVu Sans", 14, "bold"),
        parent_bg: str = PAGE_BG,
        radius: float | None = None,
    ) -> None:
        self._palette = self.PALETTES[kind]
        base_padx, base_pady = self.SIZE_PADS[size]
        self._padx, self._pady = S(base_padx), S(base_pady)
        self._text = text
        self._command = command
        self._state = "normal"
        self._hover_t = 0.0
        self._hover_target = 0.0
        self._hover_job: str | None = None
        self._pressed = False
        self._font = tkfont.Font(font=font)
        height = self._font.metrics("linespace") + 2 * self._pady
        self._radius = Sf(radius) if radius is not None else min(Sf(12.0), height / 2.0)
        super().__init__(
            parent,
            width=self._font.measure(text) + 2 * self._padx,
            height=height,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._font_spec = font
        self._redraw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # -- tk-compatible configure so app code can keep using configure(text=, state=) -- #
    def configure(self, cnf=None, **kwargs):  # noqa: D102 - tk API
        kwargs = dict(cnf or {}, **kwargs)
        dirty = False
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            super().configure(width=self._font.measure(self._text) + 2 * self._padx)
            dirty = True
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if "state" in kwargs:
            self._state = str(kwargs.pop("state"))
            super().configure(cursor="hand2" if self._state == "normal" else "arrow")
            dirty = True
        if kwargs:
            super().configure(**kwargs)
        if dirty:
            self._redraw()
        return None

    config = configure

    def _current_colors(self) -> tuple[str, str, str]:
        palette = self._palette
        if self._state != "normal":
            return palette["disabled_fill"], palette["disabled_fg"], palette["disabled_outline"]
        if self._pressed:
            return palette["press"], palette["fg"], palette["outline"]
        return mix_color(palette["fill"], palette["hover"], self._hover_t), palette["fg"], palette["outline"]

    def _redraw(self) -> None:
        self.delete("all")
        width = int(self["width"])
        height = int(self["height"])
        fill, fg, outline = self._current_colors()
        draw_round_rect(
            self, 1, 1, width - 2, height - 2, self._radius,
            fill=fill, outline=outline or fill, width=1,
        )
        offset = 1 if self._pressed and self._state == "normal" else 0
        self.create_text(width / 2, height / 2 + offset, text=self._text, fill=fg, font=self._font_spec)

    # -- hover animation (self-contained after() loop) -- #
    def _animate_hover(self, target: float) -> None:
        self._hover_target = target
        if self._hover_job is None:
            self._hover_tick()

    def _hover_tick(self) -> None:
        delta = self._hover_target - self._hover_t
        if abs(delta) < 0.04:
            self._hover_t = self._hover_target
            self._hover_job = None
        else:
            self._hover_t += delta * 0.28
            self._hover_job = self.after(15, self._hover_tick)
        try:
            self._redraw()
        except tk.TclError:
            self._hover_job = None

    def _on_enter(self, _event) -> None:
        if self._state == "normal":
            self._animate_hover(1.0)

    def _on_leave(self, _event) -> None:
        self._pressed = False
        self._animate_hover(0.0)

    def _on_press(self, _event) -> None:
        if self._state == "normal":
            self._pressed = True
            self._redraw()

    def _on_release(self, event) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        if was_pressed and inside and self._state == "normal" and self._command is not None:
            self._command()


class ToggleSwitch(tk.Frame):
    """iOS-style animated toggle bound to a BooleanVar, with a text label."""

    TRACK_W, TRACK_H, KNOB_PAD = 46, 24, 3

    def __init__(self, parent: tk.Widget, text: str, variable: tk.BooleanVar, command=None, bg: str = PAGE_BG, font=("DejaVu Sans", 12)) -> None:
        super().__init__(parent, bg=bg)
        self._var = variable
        self._command = command
        self._t = 1.0 if variable.get() else 0.0
        self._job: str | None = None
        self._tw, self._th, self._knob_pad = S(self.TRACK_W), S(self.TRACK_H), Sf(self.KNOB_PAD)
        self._canvas = tk.Canvas(self, width=self._tw, height=self._th, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._canvas.grid(row=0, column=0)
        self._label = tk.Label(self, text=text, bg=bg, fg=TEXT_DARK, font=font, cursor="hand2")
        self._label.grid(row=0, column=1, sticky="w", padx=(S(9), 0))
        self._canvas.bind("<Button-1>", self._on_click)
        self._label.bind("<Button-1>", self._on_click)
        self._redraw()

    def _on_click(self, _event) -> None:
        self._var.set(not self._var.get())
        self._animate_to(1.0 if self._var.get() else 0.0)
        if self._command is not None:
            self._command()

    def _animate_to(self, target: float) -> None:
        self._target = target
        if self._job is None:
            self._tick()

    def _tick(self) -> None:
        delta = self._target - self._t
        if abs(delta) < 0.05:
            self._t = self._target
            self._job = None
        else:
            self._t += delta * 0.3
            self._job = self.after(15, self._tick)
        try:
            self._redraw()
        except tk.TclError:
            self._job = None

    def _redraw(self) -> None:
        canvas = self._canvas
        canvas.delete("all")
        track = mix_color("#c3cde2", ELTEC_BLUE, self._t)
        draw_round_rect(canvas, 1, 1, self._tw - 2, self._th - 2, (self._th - 3) / 2, fill=track, outline=mix_color(track, self["bg"], 0.4))
        r = (self._th - 2 * self._knob_pad - 2) / 2
        min_x = self._knob_pad + 1 + r
        max_x = self._tw - self._knob_pad - 1 - r
        x = min_x + (max_x - min_x) * self._t
        y = self._th / 2
        canvas.create_oval(x - r, y - r, x + r, y + r, fill="#ffffff", outline=mix_color("#ffffff", track, 0.45))


class BatteryPill(tk.Canvas):
    """Header battery gauge: rounded pill + battery glyph, animated color."""

    W, H = 196, 36
    STATE_COLORS = {"ok": "#0f9d44", "warn": WARN_ACCENT, "low": FAIL_ACCENT, "fault": FAIL_ACCENT, "unknown": ELTEC_BLUE_DARK}

    def __init__(self, parent: tk.Widget, command=None, bg: str = ELTEC_BLUE) -> None:
        # Note: tkinter reserves self._w for the widget path, so use _pw/_ph.
        self._pw, self._ph = S(self.W), S(self.H)
        super().__init__(parent, width=self._pw, height=self._ph, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._command = command
        self._text = "Battery: --"
        self._fraction = 0.0
        self._color = hex_to_rgb(self.STATE_COLORS["unknown"])
        self._target = self._color
        self._job: str | None = None
        self.bind("<Button-1>", lambda _e: self._command() if self._command else None)
        self._redraw()

    def set_state(self, state: str, text: str, fraction: float) -> None:
        self._text = text
        self._fraction = max(0.0, min(1.0, fraction))
        self._target = hex_to_rgb(self.STATE_COLORS.get(state, self.STATE_COLORS["unknown"]))
        if self._job is None:
            self._tick()

    def _tick(self) -> None:
        moved = False
        blended = []
        for current, target in zip(self._color, self._target):
            delta = target - current
            if abs(delta) > 1.5:
                moved = True
                blended.append(current + delta * 0.22)
            else:
                blended.append(float(target))
        self._color = tuple(blended)
        self._job = self.after(15, self._tick) if moved else None
        try:
            self._redraw()
        except tk.TclError:
            self._job = None

    @property
    def pill_width(self) -> int:
        return self._pw

    def _redraw(self) -> None:
        self.delete("all")
        fill = rgb_to_hex(self._color)
        draw_round_rect(self, 1, 1, self._pw - 2, self._ph - 2, (self._ph - 3) / 2, fill=fill, outline=mix_color(fill, "#ffffff", 0.22))
        # Battery glyph: body + tip + level fill.
        bx, by, bw, bh = S(16), self._ph / 2 - S(6), S(24), S(12)
        self.create_rectangle(bx, by, bx + bw, by + bh, outline="#ffffff", width=Sf(1.4))
        self.create_rectangle(bx + bw, by + Sf(3.5), bx + bw + S(3), by + bh - Sf(3.5), fill="#ffffff", outline="#ffffff")
        pad = Sf(2.5)
        level_w = (bw - 2 * pad) * self._fraction
        if level_w > 0.5:
            self.create_rectangle(bx + pad, by + pad, bx + pad + level_w, by + bh - pad, fill="#ffffff", outline="")
        self.create_text(bx + bw + S(13), self._ph / 2, anchor="w", text=self._text, fill="#ffffff", font=("DejaVu Sans", 11, "bold"))


class PulseDot(tk.Canvas):
    """Small pulsing status dot (measuring / live indicators)."""

    def __init__(self, parent: tk.Widget, animator: Animator, name: str, color: str = ELTEC_RED, bg: str = PAGE_BG, size: int = 16) -> None:
        size = S(size)
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self._size = size
        self._color = color
        self._bg = bg
        self._dot = self.create_oval(0, 0, 0, 0, fill=color, outline="")
        animator.animate(name, 1300, self._frame, easing=None, loop=True)
        self._frame(0.0)

    def _frame(self, t: float) -> None:
        pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi)
        center = self._size / 2
        radius = self._size * (0.22 + 0.14 * pulse)
        self.coords(self._dot, center - radius, center - radius, center + radius, center + radius)
        self.itemconfigure(self._dot, fill=mix_color(self._color, self._bg, 0.45 * (1.0 - pulse)))


class Card(tk.Canvas):
    """Rounded card with a soft drop shadow and optional gradient accent strip.

    Content goes into ``card.inner`` (a plain tk.Frame). The card stretches to
    its grid cell horizontally and sizes its height to the content.
    """

    MARGIN = 9          # room around the card for the shadow layers
    SHADOW_LAYERS = ((6, 0.05), (4, 0.08), (2, 0.11))

    def __init__(
        self,
        parent: tk.Widget,
        page_bg: str = PAGE_BG,
        card_bg: str = CARD_BG,
        radius: float = 14,
        accent_stops: list[str] | None = None,
        pad: tuple[int, int] = (26, 22),
        border: str = CARD_BORDER,
    ) -> None:
        # Start with a small height: the real height is set from the content
        # as soon as it lays out. (The Tk canvas default is ~7cm tall, which
        # would flash a giant empty card for a frame otherwise.)
        super().__init__(parent, bg=page_bg, highlightthickness=0, bd=0, height=S(64))
        self._page_bg = page_bg
        self._card_bg = card_bg
        self._radius = Sf(radius)
        self._accent_stops = accent_stops
        self._pad = (S(pad[0]), S(pad[1]))
        self._margin = S(self.MARGIN)
        self._border = border
        self._last_drawn = (0, 0)
        self.inner = tk.Frame(self, bg=card_bg)
        self._window = self.create_window(self._margin + self._pad[0], self._margin + self._pad[1], anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.bind("<Configure>", self._on_canvas_configure)

    def _on_inner_configure(self, _event=None) -> None:
        wanted = self.inner.winfo_reqheight() + 2 * (self._margin + self._pad[1])
        if int(float(self["height"])) != wanted:
            super().configure(height=wanted)
        self._redraw()

    def settle(self) -> None:
        """Adopt the final content size NOW instead of waiting for <Configure>
        events (which need extra event-loop round-trips and would paint the
        card at the wrong size first)."""
        inner_width = self.winfo_width() - 2 * (self._margin + self._pad[0])
        if inner_width > 1:
            self.itemconfigure(self._window, width=inner_width)
        self._on_inner_configure()

    def _on_canvas_configure(self, _event=None) -> None:
        inner_width = self.winfo_width() - 2 * (self._margin + self._pad[0])
        if inner_width > 1:
            self.itemconfigure(self._window, width=inner_width)
        self._redraw()

    def _redraw(self) -> None:
        width = self.winfo_width()
        height = int(float(self["height"]))
        # The decorations only depend on size; skip redundant redraws so
        # layout churn elsewhere doesn't trigger expensive gradient repaints.
        if (width, height) == self._last_drawn:
            return
        self._last_drawn = (width, height)
        self.delete("deco")
        if width <= 2 * self._margin or height <= 2 * self._margin:
            return
        x0, y0 = self._margin, self._margin
        x1, y1 = width - self._margin, height - self._margin
        for offset, strength in self.SHADOW_LAYERS:
            scaled = Sf(offset)
            draw_round_rect(
                self, x0 - scaled / 2, y0 + scaled / 2, x1 + scaled / 2, y1 + scaled,
                self._radius + scaled / 2,
                fill=mix_color(self._page_bg, "#1d2c55", strength),
                outline="", tags="deco",
            )
        draw_round_rect(self, x0, y0, x1, y1, self._radius, fill=self._card_bg, outline=self._border, width=1, tags="deco")
        if self._accent_stops:
            strip_x0 = int(x0 + self._radius * 0.8)
            strip_x1 = int(x1 - self._radius * 0.8)
            draw_horizontal_gradient(self, strip_x0, y0 + S(2), strip_x1, y0 + S(2), self._accent_stops, tags="deco", step=4)
        self.tag_lower("deco")


class StepRail(tk.Canvas):
    """Numbered vertical step rail ("01 / 02 / 03") with animated transitions."""

    WIDTH = 218
    TOP = 52
    GAP = 88
    CHIP_R = 17
    CHIP_X = 28

    def __init__(self, parent: tk.Widget, steps: list[str], animator: Animator, mono_family: str, body_family: str, bg: str = PAGE_BG) -> None:
        self._top, self._gap = S(self.TOP), S(self.GAP)
        self._chip_r, self._chip_x = S(self.CHIP_R), S(self.CHIP_X)
        super().__init__(parent, width=S(self.WIDTH), height=self._top + len(steps) * self._gap, bg=bg, highlightthickness=0, bd=0)
        self._animator = animator
        self._mono = mono_family
        self._body = body_family
        self._bg = bg
        self._steps = steps
        self._current = 0
        self._chip_colors = [STEP_IDLE] * len(steps)
        self._label_colors = [STEP_IDLE_FG] * len(steps)
        self._connector_fill = [0.0] * len(steps)
        self.create_text(S(6), S(14), anchor="w", text="TEST SEQUENCE", fill=STEP_IDLE_FG, font=(mono_family, 10, "bold"))
        self._items: list[dict] = []
        for index, label in enumerate(steps):
            cy = self._top + index * self._gap + self._chip_r
            item: dict = {"cy": cy}
            # Two pulse rings: a wide faint halo under a thinner core ring reads
            # as an anti-aliased circle instead of a hard jagged outline.
            item["ring_halo"] = self.create_oval(0, 0, 0, 0, outline="", width=Sf(4.5))
            item["ring"] = self.create_oval(0, 0, 0, 0, outline="", width=Sf(2.0))
            # The chip gets a soft blended outline for the same reason.
            item["chip"] = self.create_oval(
                self._chip_x - self._chip_r, cy - self._chip_r, self._chip_x + self._chip_r, cy + self._chip_r,
                fill=STEP_IDLE, outline=mix_color(STEP_IDLE, bg, 0.45), width=Sf(2.0),
            )
            item["num"] = self.create_text(self._chip_x, cy, text=f"{index + 1:02d}", fill="#ffffff", font=(mono_family, 11, "bold"))
            item["label"] = self.create_text(self._chip_x + self._chip_r + S(13), cy, anchor="w", text=label, fill=STEP_IDLE_FG, font=(body_family, 13, "bold"))
            if index < len(steps) - 1:
                y_from = cy + self._chip_r + S(6)
                y_to = cy + self._gap - self._chip_r - S(6)
                item["track"] = self.create_line(self._chip_x, y_from, self._chip_x, y_to, fill=STEP_IDLE, width=S(3), capstyle="round")
                item["fill_line"] = self.create_line(self._chip_x, y_from, self._chip_x, y_from, fill=PASS_ACCENT, width=S(3), state="hidden", capstyle="round")
                item["y_from"], item["y_to"] = y_from, y_to
            self._items.append(item)

    def set_current(self, current: int) -> None:
        self._current = current
        for index, item in enumerate(self._items):
            if index < current:
                chip_target, label_target = PASS_ACCENT, PASS_FG
                num_text, connector_target = "✓", 1.0
            elif index == current:
                chip_target, label_target = ELTEC_BLUE, ELTEC_BLUE_DARK
                num_text, connector_target = f"{index + 1:02d}", 0.0
            else:
                chip_target, label_target = STEP_IDLE, STEP_IDLE_FG
                num_text, connector_target = f"{index + 1:02d}", 0.0
            self._animate_chip(index, chip_target, label_target)
            if self.itemcget(item["num"], "text") != num_text:
                self.itemconfigure(item["num"], text=num_text)
                if num_text == "✓":
                    self._pop_number(index)
            if "fill_line" in item:
                self._animate_connector(index, connector_target)
        self._start_pulse()

    def _animate_chip(self, index: int, chip_target: str, label_target: str) -> None:
        chip_from = self._chip_colors[index]
        label_from = self._label_colors[index]
        self._chip_colors[index] = chip_target
        self._label_colors[index] = label_target
        item = self._items[index]

        def frame(t: float) -> None:
            chip = mix_color(chip_from, chip_target, t)
            self.itemconfigure(item["chip"], fill=chip, outline=mix_color(chip, self._bg, 0.45))
            self.itemconfigure(item["label"], fill=mix_color(label_from, label_target, t))

        self._animator.animate(f"rail:chip{index}", 380, frame, easing=ease_in_out)

    def _pop_number(self, index: int) -> None:
        item = self._items[index]

        def frame(t: float) -> None:
            size = max(1, int(round(4 + 9 * t)))
            self.itemconfigure(item["num"], font=(self._mono, size, "bold"))

        self._animator.animate(f"rail:pop{index}", 420, frame, easing=ease_out_back)

    def _animate_connector(self, index: int, target: float) -> None:
        item = self._items[index]
        start = self._connector_fill[index]
        if abs(start - target) < 0.001:
            return
        self._connector_fill[index] = target

        def frame(t: float) -> None:
            frac = start + (target - start) * t
            if frac <= 0.001:
                self.itemconfigure(item["fill_line"], state="hidden")
                return
            self.itemconfigure(item["fill_line"], state="normal")
            y_end = item["y_from"] + (item["y_to"] - item["y_from"]) * frac
            self.coords(item["fill_line"], self._chip_x, item["y_from"], self._chip_x, y_end)

        self._animator.animate(f"rail:conn{index}", 420, frame, easing=ease_in_out, delay_ms=120)

    def _start_pulse(self) -> None:
        item = self._items[self._current]
        for other in self._items:
            if other is not item:
                self.itemconfigure(other["ring"], outline="")
                self.itemconfigure(other["ring_halo"], outline="")

        def frame(t: float) -> None:
            pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi)
            radius = self._chip_r + Sf(3.5) + Sf(2.5) * pulse
            cy = item["cy"]
            strength = 0.18 + 0.30 * pulse
            self.coords(item["ring"], self._chip_x - radius, cy - radius, self._chip_x + radius, cy + radius)
            self.coords(item["ring_halo"], self._chip_x - radius, cy - radius, self._chip_x + radius, cy + radius)
            self.itemconfigure(item["ring"], outline=mix_color(self._bg, ELTEC_BLUE, strength))
            self.itemconfigure(item["ring_halo"], outline=mix_color(self._bg, ELTEC_BLUE, strength * 0.35))

        self._animator.animate("rail:pulse", 2100, frame, easing=None, loop=True)


class ScopeView(tk.Canvas):
    """Dark navy oscilloscope panel: grid + glow traces (site dark-section look)."""

    PAD_X = 14
    PAD_TOP = 30
    PAD_BOTTOM = 12

    def __init__(self, parent: tk.Widget, animator: Animator, name_prefix: str, height: int = 250) -> None:
        super().__init__(parent, height=S(height), bg=WAVE_BG, highlightthickness=1, highlightbackground=NAVY_EDGE, bd=0)
        self._animator = animator
        self._prefix = name_prefix
        self._pad_x = S(self.PAD_X)
        self._pad_top = S(self.PAD_TOP)
        self._pad_bottom = S(self.PAD_BOTTOM)
        self.waveform: np.ndarray = np.array([], dtype=float)
        self.sync: np.ndarray = np.array([], dtype=float)
        self.bind("<Configure>", lambda _e: self.redraw())

    def set_data(self, waveform: np.ndarray, sync: np.ndarray) -> None:
        self.waveform = waveform
        self.sync = sync
        self.redraw()

    def _draw_grid(self, width: int, height: int) -> None:
        spacing = S(34)
        for x in range(self._pad_x, width - self._pad_x, spacing):
            major = ((x - self._pad_x) // spacing) % 4 == 0
            self.create_line(x, S(4), x, height - S(4), fill=NAVY_GRID_MAJOR if major else NAVY_GRID_MINOR)
        for y in range(S(6), height - S(4), spacing):
            self.create_line(self._pad_x - S(8), y, width - self._pad_x + S(8), y, fill=NAVY_GRID_MINOR)

    def _chip(self, x: int, y: int, text: str, core: str, tags: str = "") -> None:
        font_spec = ("DejaVu Sans Mono", 9, "bold")
        text_width = tkfont.Font(font=font_spec).measure(text)
        draw_round_rect(self, x, y, x + text_width + S(18), y + S(20), Sf(9), fill=mix_color(WAVE_BG, core, 0.16), outline=mix_color(WAVE_BG, core, 0.45), tags=tags)
        self.create_text(x + S(9) + text_width / 2, y + S(10), text=text, fill=core, font=font_spec, tags=tags)

    def _plot_trace(self, signal: np.ndarray, idx: np.ndarray, x: np.ndarray, top: float, bottom: float, halo: str, mid: str, core: str) -> None:
        lo, hi = float(np.min(signal)), float(np.max(signal))
        if abs(hi - lo) < 1e-9:
            lo -= 0.5
            hi += 0.5
        y = bottom - (signal[idx] - lo) / (hi - lo) * (bottom - top)
        points: list[float] = []
        for px, py in zip(x, y):
            points.extend([float(px), float(py)])
        self.create_line(points, fill=halo, width=Sf(6), joinstyle="round", capstyle="round")
        self.create_line(points, fill=mid, width=Sf(3), joinstyle="round", capstyle="round")
        self.create_line(points, fill=core, width=Sf(1.4), joinstyle="round", capstyle="round")

    def redraw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        self._draw_grid(width, height)
        waveform = self.waveform
        sync = self.sync
        if waveform.size < 2:
            self.create_text(
                width / 2, height / 2 - S(6),
                text="SIGNAL APPEARS HERE DURING MEASUREMENT",
                fill="#3d4f78", font=("DejaVu Sans Mono", 11, "bold"),
            )
            mid_y = height / 2 + S(18)
            self.create_line(self._pad_x + S(20), mid_y, width - self._pad_x - S(20), mid_y, fill="#22345c", width=Sf(1.4), dash=(6, 5))
        else:
            n = len(waveform)
            idx = np.linspace(0, n - 1, min(n, max(2, width - 2 * self._pad_x))).astype(int)
            x = idx / max(1, n - 1) * (width - 2 * self._pad_x) + self._pad_x
            wave_bottom = height * 0.62
            self._plot_trace(waveform, idx, x, self._pad_top, wave_bottom, TRACE_HALO, TRACE_MID, TRACE_CORE)
            if sync.size == n:
                self._plot_trace(sync, idx, x, height * 0.72, height - self._pad_bottom - S(12), SYNC_HALO, SYNC_HALO, SYNC_CORE)
            lo, hi = float(np.min(waveform)), float(np.max(waveform))
            self.create_text(width - self._pad_x, S(12), anchor="e", text=f"{lo:+.4f} V  …  {hi:+.4f} V", fill="#8ea6d4", font=("DejaVu Sans Mono", 10))
        self._chip(self._pad_x, S(8), "AIN0 · SENSOR", TRACE_CORE)
        if waveform.size >= 2 and sync.size == waveform.size:
            self._chip(self._pad_x, int(height * 0.72) - S(24), "ESP32 · PWM SYNC", SYNC_CORE)


# --------------------------------------------------------------------------- #
# Guided ESP32 emitter tester UI (v6.1)
# --------------------------------------------------------------------------- #
class EmitterTesterApp(tk.Tk):
    SETUP_STEP = "setup"
    LOAD_STEP = "load"
    RESULT_STEP = "result"
    HEADER_H = 96

    def __init__(self) -> None:
        enable_windows_dpi_awareness()
        super().__init__()
        # With DPI awareness on, winfo_fpixels reports the true monitor DPI.
        # UI_SCALE drives all canvas geometry; "tk scaling" makes point-sized
        # fonts render at the same physical size, but crisp.
        global UI_SCALE
        try:
            UI_SCALE = max(1.0, self.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            UI_SCALE = 1.0
        try:
            self.tk.call("tk", "scaling", UI_SCALE * 96.0 / 72.0)
        except tk.TclError:
            pass
        self.title(CALIBRATION_DISPLAY_NAME)
        self.minsize(S(1100), S(740))

        self.animator = Animator(self)
        load_private_fonts()
        self.FONT_DISPLAY = pick_font_family(
            self,
            ["Poppins SemiBold", "Poppins", "Manrope", "Noto Sans", "DejaVu Sans", "Segoe UI"],
            "DejaVu Sans",
        )
        self.FONT_BODY = pick_font_family(
            self, ["Manrope", "Roboto", "Noto Sans", "DejaVu Sans", "Segoe UI"], "DejaVu Sans"
        )
        self.FONT_MONO = pick_font_family(
            self,
            ["JetBrains Mono", "Cascadia Code", "DejaVu Sans Mono", "Liberation Mono", "Consolas"],
            "DejaVu Sans Mono",
        )

        self.device: EmitterEsp32Rig | None = None
        self.hardware_lock = threading.Lock()
        self.ui_queue: queue.Queue = queue.Queue()
        self.closing = False
        self.busy = False
        self.measuring = False
        self.step = self.SETUP_STEP
        self.measure_token = 0
        self.stability_settings: StabilitySettings | None = None
        self.stability_config_error: str | None = None
        try:
            self.stability_settings = load_stability_settings(DEFAULT_SETTINGS_PATH)
        except StabilitySettingsError as exc:
            self.stability_config_error = str(exc)
        frozen_error = frozen_rule_source_error()
        if frozen_error is not None:
            self.stability_settings = None
            self.stability_config_error = frozen_error

        # 6 V SLA battery watcher state.
        self.battery_v: float | None = None
        self.battery_state = "unknown"  # "ok" | "warn" | "low" | "unknown"
        self.battery_checking = False
        self.battery_read_time: float | None = None  # time.monotonic() of last good read
        self.battery_read_is_synthetic: bool | None = None
        self.battery_request_generation = 0

        # Permanently-mounted AIN1 sensor calibration / emitter-health gate.
        self.reference_calibration: ReferenceCalibration | None = None
        self.reference_calibration_error: str | None = None
        self.reference_calibrating = False
        self.last_reference_check_mv: float | None = None
        self.last_reference_calibration: ReferenceCalibration | None = None
        try:
            self.reference_calibration = load_reference_calibration()
        except ReferenceCalibrationError as exc:
            self.reference_calibration_error = str(exc)

        # Batch / sensor state.
        self.batch_number = ""
        self.tester_name = ""
        self.filter_setup = DEFAULT_FILTER_SETUP
        self.current_sensor_number = 0
        self.current_sensor_id = ""
        self.result_saved = True
        self.batch_is_synthetic = False
        self.batch_active = False
        self.session_id = str(uuid.uuid4())

        # Current-sensor measurement state.
        self.last_metrics: WaveformMetrics | None = None
        self.last_result: FinalResult | None = None
        self.last_capture_report: StabilityCaptureReport | None = None
        self.preview_waveform: np.ndarray = np.array([], dtype=float)
        self.preview_sync: np.ndarray = np.array([], dtype=float)
        self.snapshot_paths: list[Path] = []
        self.stability_diagnostics_saved = False
        self.active_run_id = ""
        self.measured_specimen_id = ""
        self.measurement_run_number = 0
        self.run_history: list[dict] = []
        self.last_run_evidence_paths: list[Path] = []
        self.last_run_manifest_path: Path | None = None
        self.last_run_evidence_error: str | None = None
        self.evidence_saving = False
        self.autosave_error: str | None = None
        self.auto_suggested_failure_mode = ""
        self.last_dut_raw_samples: tuple = ()
        self.last_dut_stream_diagnostics = None
        self.last_rig_metadata: dict = {}
        self.last_reference_capture: ReferenceEvidenceCapture | None = None
        self.offset_plausibility_override_applied = False

        self.logo_image: tk.PhotoImage | None = None
        self.wave_canvas: ScopeView | None = None
        self.default_focus_widget: tk.Widget | None = None
        self._advanced_dialog: tk.Toplevel | None = None
        self.step_frame: tk.Frame | None = None

        self._build_variables()
        if self.stability_config_error is not None:
            self.status_var.set(
                "V6.1 stability configuration error — measurement is disabled. "
                + self.stability_config_error
            )
        self._build_style()
        self._load_logo()
        self._build_layout()

        self.bind("<Return>", self.on_enter_key)
        self.bind("<KP_Enter>", self.on_enter_key)
        self.bind("<Escape>", self.on_escape_key)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.render_step()
        # Worker threads communicate through a plain Python queue. Tk itself is
        # touched only from this main-thread poller, avoiding shutdown deadlocks
        # when a capture owns the hardware lock.
        self.after(20, self._drain_ui_queue)
        # Technicians run this full screen; start maximized so every control
        # (especially the footer buttons) is visible from the first launch.
        try:
            if self.tk.call("tk", "windowingsystem") == "x11":
                # XFCE/X11 uses the EWMH zoomed attribute instead of the
                # Windows-only ``state('zoomed')`` value. Delay until mapped so
                # xfwm sees and applies the request.
                self.after(0, lambda: self.attributes("-zoomed", True))
            else:
                self.state("zoomed")
        except tk.TclError:
            pass
        self.after(200, self.startup_probe)

    # ----- font shorthands ----- #
    def fd(self, size: int, weight: str = "bold") -> tuple:
        return (self.FONT_DISPLAY, size, weight)

    def fb(self, size: int, weight: str = "normal") -> tuple:
        return (self.FONT_BODY, size, weight)

    def fm(self, size: int, weight: str = "normal") -> tuple:
        return (self.FONT_MONO, size, weight)

    def btn(self, parent: tk.Widget, text: str, command, kind: str = "primary", size: str = "lg", parent_bg: str = PAGE_BG) -> RoundButton:
        fonts = {"lg": self.fd(15), "md": self.fd(13), "sm": self.fb(12, "bold")}
        return RoundButton(parent, text=text, command=command, kind=kind, size=size, font=fonts[size], parent_bg=parent_bg)

    # ----- variables / style / logo ----- #
    def _build_variables(self) -> None:
        self.batch_var = tk.StringVar(value="")
        self.tester_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar(value=DEFAULT_FILTER_SETUP)
        self.filter_hint_var = tk.StringVar(value="")
        self.simulator_var = tk.BooleanVar(value=False)
        self.sim_case_var = tk.StringVar(value="Random good sensor")
        self.sim_low_battery_var = tk.BooleanVar(value=False)
        self.show_live_var = tk.BooleanVar(value=False)
        self.show_details_var = tk.BooleanVar(value=False)
        self.notes_var = tk.StringVar(value="")
        self.failure_mode_var = tk.StringVar(value="")
        self.specimen_id_var = tk.StringVar(value="")
        self.ground_truth_verdict_var = tk.StringVar(value="")
        self.ground_truth_failure_mode_var = tk.StringVar(value="")
        self.failure_reason_assessment_var = tk.StringVar(value="")
        self.ground_truth_basis_var = tk.StringVar(value="")
        self.review_confidence_var = tk.StringVar(value="")
        self.reviewer_var = tk.StringVar(value="")
        self.known_root_cause_var = tk.StringVar(value="")
        self.review_notes_var = tk.StringVar(value="")
        self.sensor_seated_override_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="Checking ESP32 rig...")
        self.measure_status_var = tk.StringVar(value="")
        self.comment_status_var = tk.StringVar(value="")
        self.snapshot_status_var = tk.StringVar(value="")
        self.reference_progress_var = tk.StringVar(value="")
        self.review_status_var = tk.StringVar(value="Ground-truth review required")
        self.review_notes_status_var = tk.StringVar(value="Explanation required")

        # One-line summary shown next to the "Advanced options" link.
        self.adv_summary_var = tk.StringVar()
        self.simulator_var.trace_add("write", lambda *_a: self._update_adv_summary())
        self._update_adv_summary()

    def _update_adv_summary(self) -> None:
        bits = ["capture: adaptive peak stability"]
        if self.simulator_mode():
            bits.append("SIMULATOR ON")
        self.adv_summary_var.set("   ·   ".join(bits))

    def _build_style(self) -> None:
        self.configure(bg=PAGE_BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=PAGE_BG)
        style.configure("TLabel", background=PAGE_BG, foreground=TEXT_DARK, font=self.fb(14))
        style.configure("Muted.TLabel", background=PAGE_BG, foreground=MUTED_FG, font=self.fb(11))
        style.configure("TCheckbutton", background=PAGE_BG, foreground=TEXT_DARK, font=self.fb(12))
        style.map("TCheckbutton", background=[("active", PAGE_BG)])
        style.configure("TSeparator", background=CARD_BORDER)
        style.configure(
            "Card.TEntry", fieldbackground="#f7f9fd", foreground=TEXT_DARK,
            bordercolor=CARD_BORDER, lightcolor=CARD_BORDER, darkcolor=CARD_BORDER,
            insertcolor=ELTEC_BLUE, padding=(12, 8),
        )
        style.map("Card.TEntry", bordercolor=[("focus", ELTEC_BLUE)], lightcolor=[("focus", ELTEC_BLUE)], darkcolor=[("focus", ELTEC_BLUE)])
        style.configure("TCombobox", font=self.fb(16), padding=(10, 6))
        self.option_add("*TCombobox*Listbox.font", self.fb(15))

    def _load_logo(self) -> None:
        logo_path = find_logo_path()
        if logo_path is None:
            self.logo_image = None
            return
        try:
            self.logo_image = tk.PhotoImage(file=str(logo_path))
            # Shrink very large logos with a single integer factor chosen so the
            # image best fills the header badge (~140x52) without overflowing.
            factor = max(
                1,
                math.ceil(self.logo_image.height() / 52),
                math.ceil(self.logo_image.width() / 140),
            )
            if factor > 1:
                self.logo_image = self.logo_image.subsample(factor, factor)
            self.iconphoto(False, self.logo_image)
        except Exception:
            self.logo_image = None

    # ----- layout: header + step rail + content ----- #
    def _build_layout(self) -> None:
        self._hh = S(self.HEADER_H)
        self.header = tk.Canvas(self, height=self._hh, bg=ELTEC_BLUE, highlightthickness=0, bd=0)
        self.header.grid(row=0, column=0, sticky="ew")
        self.battery_pill = BatteryPill(self.header, command=self.refresh_battery)
        self._battery_window = self.header.create_window(0, self._hh / 2, anchor="e", window=self.battery_pill)
        self._header_status_item: int | None = None
        self._header_width = 0
        self.header.bind("<Configure>", self._redraw_header)
        self.status_var.trace_add("write", lambda *_args: self._update_header_status())
        self.animator.animate("header:wave", 5600, self._header_wave_frame, easing=None, loop=True)

        # Technical gradient accent strip under the app bar (site signature).
        self.accent_strip = tk.Canvas(self, height=S(3), bg=ELTEC_BLUE, highlightthickness=0, bd=0)
        self.accent_strip.grid(row=1, column=0, sticky="ew")
        self.accent_strip.bind(
            "<Configure>",
            lambda event: (
                self.accent_strip.delete("all"),
                draw_horizontal_gradient(self.accent_strip, 0, S(1), event.width, S(1), [ELTEC_BLUE_BRIGHT, "#6366f1", "#a855f7", ELTEC_RED], tags="grad", step=4),
            ),
        )

        body = tk.Frame(self, bg=PAGE_BG)
        body.grid(row=2, column=0, sticky="nsew", padx=(S(20), S(22)), pady=(S(16), S(14)))
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)

        self.rail = StepRail(
            body,
            ["Batch info", "Load sensor", "Measure & result"],
            self.animator,
            mono_family=self.FONT_MONO,
            body_family=self.FONT_BODY,
        )
        self.rail.grid(row=0, column=0, sticky="nw", pady=(S(6), 0))

        divider = tk.Frame(body, bg=CARD_BORDER, width=1)
        divider.grid(row=0, column=1, sticky="ns", padx=(S(4), S(22)))

        self.content = tk.Frame(body, bg=PAGE_BG)
        self.content.grid(row=0, column=2, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)
        # The step content lives inside a scroll canvas: when a step is taller
        # than the window (e.g. a FAIL with reasons + the waveform open), a
        # scrollbar appears and the mouse wheel brings the rest into view
        # instead of clipping it.
        self.step_scroll = tk.Canvas(self.content, bg=PAGE_BG, highlightthickness=0, bd=0)
        self.step_scroll.grid(row=0, column=0, sticky="nsew")
        self.step_vbar = ttk.Scrollbar(self.content, orient="vertical", command=self.step_scroll.yview)
        self.step_scroll.configure(yscrollcommand=self._on_step_scroll_set)
        self.step_scroll.bind("<Configure>", lambda _e: self._sync_step_scroll())
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        # Tk/X11 reports wheel motion as buttons 4/5 rather than MouseWheel.
        self.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.bind_all("<Button-5>", self._on_mousewheel, add="+")
        self._step_window: int | None = None
        # The navigation footer lives OUTSIDE the scrolling area in its own
        # fixed row, so it is always fully visible no matter how tall the step
        # content gets.
        self.footer_bar = tk.Frame(self.content, bg=PAGE_BG)
        self.footer_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(S(12), 0))
        self.footer_bar.columnconfigure(0, weight=1)

    def _on_step_scroll_set(self, first: str, last: str) -> None:
        self.step_vbar.set(first, last)
        # Auto-hide: only show the scrollbar when there is something to scroll.
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.step_vbar.grid_remove()
        else:
            self.step_vbar.grid(row=0, column=1, sticky="ns", padx=(S(4), 0))

    def _sync_step_scroll(self) -> None:
        """Size the embedded step frame: full canvas width, and at least the
        canvas height (so weight rows keep absorbing surplus space) but taller
        when the content needs it - that overflow is what scrolls."""
        if self._step_window is None or self.step_frame is None or not self.step_frame.winfo_exists():
            return
        canvas_w = max(1, self.step_scroll.winfo_width())
        canvas_h = max(1, self.step_scroll.winfo_height())
        total_h = max(canvas_h, self.step_frame.winfo_reqheight())
        self.step_scroll.itemconfigure(self._step_window, width=canvas_w, height=total_h)
        self.step_scroll.configure(scrollregion=(0, 0, canvas_w, total_h))

    def _on_mousewheel(self, event: tk.Event) -> None:
        widget = event.widget
        # Only scroll the main window's step area; leave dialogs (comment box,
        # batch summary) and text widgets to their own wheel handling.
        try:
            if not isinstance(widget, tk.Widget) or widget.winfo_toplevel() is not self:
                return
            if isinstance(widget, tk.Text):
                return
            first, last = self.step_vbar.get()
            if first <= 0.0 and last >= 1.0:
                return
            if getattr(event, "num", None) == 4:
                direction = -1
            elif getattr(event, "num", None) == 5:
                direction = 1
            else:
                delta = int(getattr(event, "delta", 0) / 120)
                if delta == 0:
                    return
                direction = -delta
            self.step_scroll.yview_scroll(3 * direction, "units")
        except tk.TclError:
            pass

    def _redraw_header(self, _event=None) -> None:
        width = self.header.winfo_width()
        if width <= 2:
            return
        self._header_width = width
        height = self._hh
        self.header.delete("static")
        for x in range(0, width, 4):
            color = gradient_color(HEADER_GRADIENT, x / max(1, width))
            self.header.create_line(x, 0, x, height, fill=color, width=4, tags="static")
        # Faint vertical grid ticks (site dark-section texture).
        for x in range(S(56), width, S(128)):
            tick = mix_color(gradient_color(HEADER_GRADIENT, x / max(1, width)), "#ffffff", 0.05)
            self.header.create_line(x, 0, x, height, fill=tick, tags="static")

        # Logo badge (white rounded chip, like the site's logo-on-white).
        badge_x0, badge_y0, badge_x1, badge_y1 = S(18), S(14), S(176), height - S(14)
        draw_round_rect(self.header, badge_x0, badge_y0, badge_x1, badge_y1, Sf(12), fill="#ffffff", outline=mix_color(ELTEC_BLUE, "#ffffff", 0.75), tags="static")
        badge_cx = (badge_x0 + badge_x1) / 2
        badge_cy = (badge_y0 + badge_y1) / 2
        if self.logo_image is not None:
            self.header.create_image(badge_cx, badge_cy, image=self.logo_image, tags="static")
        else:
            self.header.create_text(badge_cx, badge_cy, text="ELTEC", fill=ELTEC_RED, font=(self.FONT_DISPLAY, 22, "bold italic"), tags="static")

        title_x = badge_x1 + S(26)
        header_title = "406MCA FAILURE CALIBRATION"
        self.header.create_text(title_x, S(26), anchor="w", text=header_title, fill=HEADER_FG, font=self.fd(20), tags="static")
        title_width = tkfont.Font(font=self.fd(20)).measure(header_title)
        chip_x = title_x + title_width + S(14)
        draw_round_rect(self.header, chip_x, S(15), chip_x + S(64), S(37), Sf(8), fill=ELTEC_RED, outline="", tags="static")
        self.header.create_text(chip_x + S(32), S(26), text="V6.1 BASE", fill="#ffffff", font=self.fm(9, "bold"), tags="static")
        warning_text = "NOT PRODUCTION"
        warning_font = self.fm(10, "bold")
        warning_w = tkfont.Font(font=warning_font).measure(warning_text) + S(20)
        warning_x = chip_x + S(64) + S(9)
        draw_round_rect(self.header, warning_x, S(15), warning_x + warning_w, S(37), Sf(8), fill=WARN_ACCENT, outline="", tags="static")
        self.header.create_text(warning_x + warning_w / 2, S(26), text=warning_text, fill="#3d2c00", font=warning_font, tags="static")
        if self.simulator_mode():
            # Loud amber badge: everything on screen is synthetic.
            sim_text = "SIMULATOR"
            sim_font = self.fm(11, "bold")
            sim_w = tkfont.Font(font=sim_font).measure(sim_text) + S(22)
            sim_x = warning_x + warning_w + S(9)
            draw_round_rect(self.header, sim_x, S(15), sim_x + sim_w, S(37), Sf(8), fill=WARN_ACCENT, outline="", tags="static")
            self.header.create_text(sim_x + sim_w / 2, S(26), text=sim_text, fill="#3d2c00", font=sim_font, tags="static")
        self.header.create_text(title_x, S(47), anchor="w", text="GROUND-TRUTH STUDY  ·  V6.1 VERDICT RULES FROZEN", fill=mix_color(HEADER_SUB_FG, ELTEC_BLUE, 0.25), font=self.fm(9, "bold"), tags="static")

        if self._header_status_item is not None:
            self.header.delete(self._header_status_item)
        self._header_status_item = self.header.create_text(
            title_x, S(70), anchor="w", text=self.status_var.get(), fill=HEADER_SUB_FG, font=self.fb(11), tags="status",
        )
        # Blend the battery pill into the local gradient color and pin it right.
        pill_x = width - S(26)
        pill_center_t = (pill_x - self.battery_pill.pill_width / 2) / max(1, width)
        self.battery_pill.configure(bg=gradient_color(HEADER_GRADIENT, pill_center_t))
        self.header.coords(self._battery_window, pill_x, height / 2)
        self.header.tag_raise("wave")

    def _update_header_status(self) -> None:
        if self._header_status_item is not None:
            try:
                self.header.itemconfigure(self._header_status_item, text=self.status_var.get())
            except tk.TclError:
                pass

    def _header_wave_frame(self, t: float) -> None:
        width = self._header_width
        if width <= 2:
            return
        base_y = self._hh - S(12)
        step = S(8)
        points: list[float] = []
        for x in range(0, width + step, step):
            y = base_y + Sf(5.0) * math.sin(2 * math.pi * (2.5 * x / max(1, width) + t))
            points.extend([x, y])
        if not self.header.find_withtag("wave"):
            self.header.create_line(points, fill="#89b4f8", width=Sf(1.4), smooth=True, tags="wave")
        else:
            self.header.coords("wave", *points)
            self.header.tag_raise("wave")

    # ----- step rendering ----- #
    def clear_content(self) -> None:
        self.animator.cancel_prefix("step:")
        self.wave_canvas = None
        if self.step_frame is not None and self.step_frame.winfo_exists():
            self.step_frame.destroy()
        if self._step_window is not None:
            self.step_scroll.delete(self._step_window)
        self.step_frame = tk.Frame(self.step_scroll, bg=PAGE_BG)
        self.step_frame.columnconfigure(0, weight=1)
        self._step_window = self.step_scroll.create_window(0, 0, anchor="nw", window=self.step_frame)
        self.step_frame.bind("<Configure>", lambda _e: self._sync_step_scroll())
        self.step_scroll.yview_moveto(0.0)

    def update_progress_labels(self) -> None:
        order = [self.SETUP_STEP, self.LOAD_STEP, self.RESULT_STEP]
        self.rail.set_current(order.index(self.step))

    def render_step(self) -> None:
        self.clear_content()
        self.default_focus_widget = None
        self.update_progress_labels()
        if self.step == self.SETUP_STEP:
            self.render_setup_step()
        elif self.step == self.LOAD_STEP:
            self.render_load_step()
        else:
            self.render_result_step()
        self.render_navigation()
        self.update_navigation_state()
        self._settle_layout()
        self._slide_in_step()
        self.after_idle(self.focus_default_widget)
        # Insurance against Windows leaving stale pixels in child widgets
        # after the initial layout passes (moved windows keep their old bits).
        self.after(250, self._force_full_repaint)
        self.after(900, self._force_full_repaint)

    def _settle_layout(self) -> None:
        """Bring every Card in the new step to its final size synchronously,
        so the first paint of the step is already the final layout."""
        try:
            self.update_idletasks()  # give every widget its requested size
        except tk.TclError:
            return
        stack: list[tk.Widget] = [self.step_frame]
        while stack:
            widget = stack.pop()
            if isinstance(widget, Card):
                widget.settle()
            stack.extend(widget.winfo_children())
        try:
            self.update_idletasks()  # apply the new card heights to the grid
        except tk.TclError:
            pass

    def _force_full_repaint(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.GetAncestor.restype = ctypes.c_void_p
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.RedrawWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
            hwnd = user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
            RDW_FLAGS = 0x1 | 0x4 | 0x80 | 0x100  # INVALIDATE|ERASE|ALLCHILDREN|UPDATENOW
            user32.RedrawWindow(hwnd, None, None, RDW_FLAGS)
        except Exception:
            pass

    def _slide_in_step(self) -> None:
        window = self._step_window

        def on_frame(t: float) -> None:
            self.step_scroll.coords(window, int(round(S(44) * (1.0 - t))), 0)

        self.animator.animate("step:slide", 340, on_frame, easing=ease_out_cubic)

    def _step_heading(self, row: int, number: str, title: str, subtitle: str) -> None:
        head = tk.Frame(self.step_frame, bg=PAGE_BG)
        head.grid(row=row, column=0, sticky="ew")
        tk.Label(head, text=f"{number} —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(13, "bold")).pack(anchor="w")
        tk.Label(head, text=title, bg=PAGE_BG, fg=TEXT_DARK, font=self.fd(29)).pack(anchor="w", pady=(2, 0))
        if subtitle:
            tk.Label(head, text=subtitle, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(13)).pack(anchor="w", pady=(4, 0))

    def _field_label(self, parent: tk.Widget, row: int, text: str, bg: str = CARD_BG, pady: tuple = (14, 4)) -> None:
        tk.Label(parent, text=text.upper(), bg=bg, fg=MUTED_FG, font=self.fb(11, "bold")).grid(row=row, column=0, sticky="w", pady=pady)

    def render_setup_step(self) -> None:
        self._step_heading(
            0,
            "01",
            "Failure-calibration study",
            "Experimental evidence collection only — enter the study batch, reviewer, and normal filter setup.",
        )

        card = Card(self.step_frame, accent_stops=TECH_GRADIENT)
        card.grid(row=1, column=0, sticky="new", pady=(20, 0))
        inner = card.inner
        inner.columnconfigure(0, weight=1)

        self._field_label(inner, 0, "Batch number", pady=(0, 4))
        batch_entry = ttk.Entry(inner, textvariable=self.batch_var, font=self.fb(19), style="Card.TEntry")
        batch_entry.grid(row=1, column=0, sticky="ew")
        self.default_focus_widget = batch_entry

        self._field_label(inner, 2, "Tester name")
        ttk.Entry(inner, textvariable=self.tester_var, font=self.fb(19), style="Card.TEntry").grid(row=3, column=0, sticky="ew")

        self._field_label(inner, 4, "Filter / setup")
        filter_combo = ttk.Combobox(
            inner, textvariable=self.filter_var, values=list(FILTER_SPECS_MV.keys()), state="readonly",
            font=self.fb(15), height=min(max(len(FILTER_SPECS_MV), 5), 10),
        )
        filter_combo.grid(row=5, column=0, sticky="ew")
        filter_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_filter_hint())
        tk.Label(inner, textvariable=self.filter_hint_var, bg=CARD_BG, fg=ELTEC_BLUE, font=self.fm(11, "bold")).grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.update_filter_hint()

        self._build_reference_calibration_card(row=2)

        adv_container = tk.Frame(self.step_frame, bg=PAGE_BG)
        adv_container.grid(row=3, column=0, sticky="new", pady=(S(16), 0))
        link = tk.Label(adv_container, text="⚙  Advanced options…", bg=PAGE_BG, fg=ELTEC_BLUE,
                        font=self.fb(12, "bold"), cursor="hand2")
        link.grid(row=0, column=0, sticky="w")
        link.bind("<Button-1>", lambda _e: self.open_advanced_options())
        tk.Label(adv_container, textvariable=self.adv_summary_var, bg=PAGE_BG, fg=MUTED_FG,
                 font=self.fb(11)).grid(row=0, column=1, sticky="w", padx=(S(14), 0))

    def _build_advanced_panel(self, parent: tk.Widget) -> tk.Frame:
        panel = tk.Frame(parent, bg=PAGE_BG)
        panel.columnconfigure(1, weight=1)
        settings = self.stability_settings
        if settings is None:
            stability_rule_text = "The tracked v6.1 stability settings are invalid; measurement is disabled."
        else:
            stability_rule_text = (
                f"V6.1 DUT attempts 1, 2, and 3 each require "
                f"{DUT_STABILITY_CONFIRMATION_DELTAS} consecutive "
                f"cycle-to-cycle peak deltas at or below {settings.peak_delta_threshold_mv:.3f} mV, "
                f"then {SENSITIVITY_MEASUREMENT_CYCLES} measurement cycles that must stay within "
                f"the same limit. A kick discards that window and restarts the same 10/20 check. "
                f"A third measurement kick fails as unstable. Requalification must finish within "
                f"{STABILITY_TIMEOUT_S:g} seconds of PWM-on."
            )
        ttk.Checkbutton(panel, text="Simulator mode (training only - synthetic data, clearly badged)",
                        variable=self.simulator_var,
                        command=self.on_simulator_toggle).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(panel, text="Sim case", bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        ttk.Combobox(panel, textvariable=self.sim_case_var, values=SIM_CASES, state="readonly",
                     width=24, font=self.fb(11)).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(panel, text="Simulate low battery (test the change-battery lockout)",
                        variable=self.sim_low_battery_var,
                        command=self.refresh_battery).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Label(
            panel,
            text=stability_rule_text,
            bg=PAGE_BG, fg=MUTED_FG, font=self.fb(10), wraplength=S(640), justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        tk.Label(
            panel,
            text=(f"ESP32 rig: ADS AIN1 = fixed reference/emitter gate, ADS AIN0 = buffered DUT "
                  f"(offset + AC), ADS AIN7 = 6 V SLA via "
                  f"100k/100k divider (÷2), streamed sync = PWM state, {EMITTER_PWM_CHANNEL} = MOSFET gate. "
                  f"Emitter driven at {EMITTER_PWM_FREQUENCY_HZ:g} Hz, {EMITTER_PWM_DUTY_CYCLE:g}% duty (fixed). "
                  f"ADS sensor range is ±{WAVEFORM_INPUT_RANGE_V:g} V through a unity-gain buffer. "
                  f"AIN1 is checked before any AIN0 read and must remain within "
                  f"+/-{REFERENCE_TOLERANCE_PERCENT:g}% of its calibration. Testing is blocked "
                  f"at or below {BATTERY_MIN_V:.1f} V."),
            bg=PAGE_BG, fg=MUTED_FG, font=self.fb(10), wraplength=S(640), justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        return panel

    def open_advanced_options(self) -> None:
        """All advanced settings live in their own window: the inline panel
        used to expand below the setup card where it could not be scrolled
        into view on smaller screens."""
        if self._advanced_dialog is not None and self._advanced_dialog.winfo_exists():
            self._advanced_dialog.lift()
            self._advanced_dialog.focus_set()
            return
        dialog = tk.Toplevel(self)
        self._advanced_dialog = dialog
        dialog.title("Advanced options")
        dialog.configure(bg=PAGE_BG)
        dialog.transient(self)
        dialog.minsize(S(760), S(420))
        dialog.geometry(f"+{self.winfo_rootx() + S(240)}+{self.winfo_rooty() + S(130)}")

        frame = tk.Frame(dialog, bg=PAGE_BG)
        frame.grid(row=0, column=0, sticky="nsew", padx=S(22), pady=S(18))
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text="ADVANCED —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(11, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(frame, text="Tester configuration", bg=PAGE_BG, fg=TEXT_DARK, font=self.fd(19)).grid(row=1, column=0, sticky="w", pady=(2, S(14)))
        self._build_advanced_panel(frame).grid(row=2, column=0, sticky="new")
        buttons = tk.Frame(frame, bg=PAGE_BG)
        buttons.grid(row=3, column=0, sticky="e", pady=(S(16), 0))
        self.btn(buttons, "Close (Enter)", dialog.destroy, kind="primary", size="sm").grid(row=0, column=0)

        def close(_event: tk.Event | None = None) -> str:
            dialog.destroy()
            return "break"

        dialog.bind("<Return>", close)
        dialog.bind("<KP_Enter>", close)
        dialog.bind("<Escape>", close)
        dialog.focus_set()

    def update_filter_hint(self) -> None:
        min_mv = FILTER_SPECS_MV.get(self.filter_var.get())
        self.filter_hint_var.set("" if min_mv is None else f"MINIMUM SENSITIVITY TO PASS: {min_mv:.1f} mV")

    def reference_gate_ready(self) -> bool:
        if self.simulator_mode():
            return True
        calibration = self.reference_calibration
        return calibration is not None and calibration.valid

    def simulator_mode(self) -> bool:
        """Return the mode frozen at batch start, or the setup selection."""
        if self.batch_active:
            return bool(self.batch_is_synthetic)
        return bool(self.simulator_var.get())

    def _build_reference_calibration_card(self, row: int) -> None:
        calibration = self.reference_calibration
        simulator = self.simulator_mode()
        ready = simulator or (calibration is not None and calibration.valid)
        if simulator:
            title = "Reference unit simulated"
            detail = "Training mode uses a synthetic reference reading; hardware calibration is unchanged."
            accent = WARN_ACCENT
        elif self.reference_calibrating:
            title = "Calibrating reference unit…"
            detail = self.reference_progress_var.get() or (
                f"Collecting {REFERENCE_CALIBRATION_READINGS} stable readings."
            )
            accent = ELTEC_BLUE_BRIGHT
        elif calibration is not None and calibration.valid:
            title = "Reference unit calibrated"
            detail = "Ready. The reference unit will be checked automatically before every sensor."
            accent = PASS_ACCENT
        elif calibration is not None:
            title = "Reference unit lockout — recalibration required"
            detail = calibration.invalidation_reason or (
                "The previous calibration is invalid. Replace/check the emitter and calibrate again."
            )
            accent = FAIL_ACCENT
        else:
            title = "Reference unit calibration required"
            detail = self.reference_calibration_error or (
                f"Install a known-good/new emitter, then collect {REFERENCE_CALIBRATION_READINGS} "
                "stable reference readings. Sensor testing stays locked until this is complete."
            )
            accent = FAIL_ACCENT

        bg = CARD_BG
        card = Card(
            self.step_frame,
            card_bg=bg,
            border=mix_color(accent, bg, 0.60),
            accent_stops=[accent, accent],
            pad=(18, 14),
        )
        card.grid(row=row, column=0, sticky="ew", pady=(S(16), 0))
        inner = card.inner
        inner.columnconfigure(0, weight=1)
        tk.Label(inner, text=title, bg=bg, fg=TEXT_DARK, font=self.fb(14, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        detail_options = (
            {"textvariable": self.reference_progress_var}
            if self.reference_calibrating
            else {"text": detail}
        )
        tk.Label(
            inner,
            bg=bg,
            fg=MUTED_FG,
            font=self.fb(11),
            wraplength=S(650),
            justify="left",
            **detail_options,
        ).grid(row=1, column=0, sticky="w", pady=(S(4), 0))
        if not simulator:
            button_text = "Recalibrate reference unit" if ready else "Calibrate reference unit"
            button = self.btn(
                inner,
                button_text,
                self.run_reference_calibration,
                kind="outline" if ready else "primary",
                size="sm",
                parent_bg=bg,
            )
            button.grid(row=0, column=1, rowspan=2, padx=(S(16), 0))
            if self.busy or self.measuring or self.reference_calibrating:
                button.configure(state="disabled")

    def render_load_step(self) -> None:
        self._step_heading(0, "02", f"Load sensor {self.current_sensor_id}", f"Batch {self.batch_number}    ·    Filter: {self.filter_setup}")

        self._build_reference_calibration_card(row=1)

        card = Card(self.step_frame, accent_stops=TECH_GRADIENT)
        card.grid(row=2, column=0, sticky="ew", pady=(22, 0))
        inner = card.inner
        inner.columnconfigure(1, weight=1)
        rig = tk.Canvas(inner, width=S(190), height=S(128), bg=CARD_BG, highlightthickness=0, bd=0)
        rig.grid(row=0, column=0, padx=(0, S(24)))
        self._draw_rig_illustration(rig)
        text_col = tk.Frame(inner, bg=CARD_BG)
        text_col.grid(row=0, column=1, sticky="w")
        tk.Label(text_col, text="Place the sensor in the testing rig", bg=CARD_BG, fg=TEXT_DARK, font=self.fd(24)).pack(anchor="w")
        tk.Label(text_col, text="Then press Enter to read the offset and run the emitter test.", bg=CARD_BG, fg=MUTED_FG, font=self.fb(13)).pack(anchor="w", pady=(6, 0))
        chips = tk.Frame(text_col, bg=CARD_BG)
        chips.pack(anchor="w", pady=(14, 0))
        for chip_text in (f"SENSOR {self.current_sensor_id}", f"{EMITTER_PWM_FREQUENCY_HZ:g} Hz · 50% DUTY", "GAIN ×1 BUFFER"):
            chip = tk.Label(chips, text=chip_text, bg=ELTEC_BLUE_LIGHT, fg=ELTEC_BLUE_DARK, font=self.fm(9, "bold"), padx=10, pady=4)
            chip.pack(side="left", padx=(0, 8))
        specimen = tk.Frame(text_col, bg=CARD_BG)
        specimen.pack(anchor="w", fill="x", pady=(S(14), 0))
        tk.Label(
            specimen,
            text="PHYSICAL SPECIMEN ID (REQUIRED)",
            bg=CARD_BG,
            fg=MUTED_FG,
            font=self.fm(9, "bold"),
        ).pack(anchor="w")
        specimen_entry = ttk.Entry(
            specimen,
            textvariable=self.specimen_id_var,
            font=self.fb(15),
            style="Card.TEntry",
            width=28,
        )
        specimen_entry.pack(anchor="w", fill="x", pady=(S(4), 0))
        self.default_focus_widget = specimen_entry
        ttk.Checkbutton(
            text_col,
            text=(
                "Diagnostic override: I physically confirmed this sensor is seated; "
                "allow capture if its DC offset looks disconnected"
            ),
            variable=self.sensor_seated_override_var,
            command=lambda: self.write_autosave("offset_override_changed"),
        ).pack(anchor="w", pady=(S(10), 0))
        tk.Label(
            text_col,
            text=(
                "Use the override only for suspected no-offset/extreme-offset failures. "
                "It is recorded in every artifact and never changes the ordinary v6.1 app."
            ),
            bg=CARD_BG,
            fg=WARN_FG,
            font=self.fb(9),
            wraplength=S(650),
            justify="left",
        ).pack(anchor="w", pady=(S(3), 0))
        self._build_battery_banner()

    def _draw_rig_illustration(self, rig: tk.Canvas) -> None:
        """Draw the rig glyph with a pulsing emitter glow."""
        draw_round_rect(rig, S(24), S(18), S(166), S(96), Sf(10), fill=NAVY, outline=NAVY_EDGE)
        draw_round_rect(rig, S(50), S(38), S(140), S(76), Sf(7), fill="#111b31", outline="#2a3a5f")
        rig.create_line(S(18), S(102), S(172), S(102), fill=ELTEC_BLUE_DARK, width=Sf(5), capstyle="round")
        rig.create_text(S(95), S(114), text="EMITTER RIG", fill=MUTED_FG, font=self.fm(9, "bold"))
        glow = rig.create_oval(0, 0, 0, 0, fill="", outline="")
        core = rig.create_oval(0, 0, 0, 0, fill=ELTEC_RED, outline="")

        def frame(t: float) -> None:
            pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi)
            cx, cy = Sf(95), Sf(57)
            core_rx = Sf(min(24.0, (7.0 + 1.5 * pulse) * 1.7))
            core_ry = Sf(7.0 + 1.5 * pulse)
            glow_rx = Sf(min(40.0, (7.0 + 1.5 * pulse) * 1.7 + 8 + 5.0 * pulse))
            glow_ry = Sf(min(14.0, 7.0 + 1.5 * pulse + 4 + 2.5 * pulse))
            rig.coords(core, cx - core_rx, cy - core_ry, cx + core_rx, cy + core_ry)
            rig.coords(glow, cx - glow_rx, cy - glow_ry, cx + glow_rx, cy + glow_ry)
            rig.itemconfigure(glow, fill=mix_color("#111b31", ELTEC_RED, 0.22 + 0.18 * pulse))
            rig.itemconfigure(core, fill=mix_color(ELTEC_RED, "#ff7a92", 0.5 * pulse))

        self.animator.animate("step:rig", 2000, frame, easing=None, loop=True)

    def render_result_step(self) -> None:
        if self.measuring:
            self.render_measuring_view()
        elif self.last_result is not None:
            self.render_result_view()
        else:
            self._step_heading(0, "03", f"{self.current_sensor_id}: ready to measure", "Press Enter (or Measure) to run the emitter test.")
            self.btn(self.step_frame, "Measure", self.run_measurement, kind="primary", size="lg").grid(row=2, column=0, sticky="w", pady=(22, 0))
        if not self.measuring:
            self._build_battery_banner()

    def _build_battery_banner(self) -> None:
        """Show a yellow low-warning strip or a red block, with a re-check button."""
        if self.battery_state not in ("warn", "low", "fault"):
            return
        blocked = self.battery_state in ("low", "fault")
        bg = FAIL_BG if blocked else WARN_BG
        fg = FAIL_FG if blocked else WARN_FG
        accent = FAIL_ACCENT if blocked else WARN_ACCENT
        volts = "" if self.battery_v is None else f" ({self.battery_v:.2f} V)"
        if self.battery_state == "fault":
            message = (f"Battery reads{volts}, which is not a valid 6 V SLA level — the battery or the ADS AIN7 "
                       "divider is probably not connected. Check the battery clip and rig wiring, then re-check.")
        elif self.battery_state == "low":
            message = f"Recharge the 6 V SLA{volts}. Testing is blocked until it is charged and re-checked."
        else:
            message = f"Battery is getting low{volts}. Swap it soon — testing is still allowed."
        card = Card(self.step_frame, card_bg=bg, border=mix_color(accent, bg, 0.45), accent_stops=[accent, accent], pad=(18, 12))
        card.grid(row=8, column=0, sticky="ew", pady=(16, 0))
        inner = card.inner
        inner.columnconfigure(1, weight=1)
        tk.Label(inner, text="⚠", bg=bg, fg=accent, font=self.fd(22)).grid(row=0, column=0, padx=(0, S(12)))
        tk.Label(inner, text=message, bg=bg, fg=fg, font=self.fb(13, "bold"), wraplength=S(620), justify="left").grid(row=0, column=1, sticky="w")
        self.btn(inner, "Re-check battery", self.refresh_battery, kind="outline", size="sm", parent_bg=bg).grid(row=0, column=2, padx=(S(12), 0))

    def render_measuring_view(self) -> None:
        head = tk.Frame(self.step_frame, bg=PAGE_BG)
        head.grid(row=0, column=0, sticky="ew")
        PulseDot(head, self.animator, "step:pulse", color=ELTEC_RED, bg=PAGE_BG, size=18).pack(side="left", padx=(0, 10), pady=(6, 0))
        tk.Label(head, text=f"{self.current_sensor_id}: measuring…", bg=PAGE_BG, fg=ELTEC_BLUE_DARK, font=self.fd(28)).pack(side="left")
        tk.Label(self.step_frame, textvariable=self.measure_status_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(15)).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self._build_scan_bar(row=2)
        ToggleSwitch(self.step_frame, "Show live waveform while reading", self.show_live_var, command=self.toggle_live_view, font=self.fb(12)).grid(row=3, column=0, sticky="w", pady=(S(14), 0))
        if self.show_live_var.get():
            self._build_wave_canvas(row=4, live=True)

    def _build_scan_bar(self, row: int) -> None:
        """Indeterminate scanning bar shown while a measurement runs."""
        bar = tk.Canvas(self.step_frame, height=S(6), bg=PAGE_BG, highlightthickness=0, bd=0)
        bar.grid(row=row, column=0, sticky="ew", pady=(S(16), 0))
        track = bar.create_rectangle(0, S(1), 0, S(5), fill=GHOST_BG, outline="")
        segment = bar.create_rectangle(0, S(1), 0, S(5), fill=ELTEC_BLUE, outline="")

        def frame(t: float) -> None:
            width = max(1, bar.winfo_width())
            bar.coords(track, 0, S(2), width, S(5))
            seg_w = max(S(90), width * 0.22)
            x = (width + seg_w) * t - seg_w
            bar.coords(segment, x, S(1), x + seg_w, S(6))
            bar.itemconfigure(segment, fill=mix_color(ELTEC_BLUE, ELTEC_BLUE_BRIGHT, 0.5 + 0.5 * math.sin(t * 2 * math.pi)))

        self.animator.animate("step:scan", 1400, frame, easing=None, loop=True)

    def _current_calibration_review(self) -> CalibrationReview:
        if self.last_result is None:
            raise ValueError("Run the measurement before completing the review.")
        return build_calibration_review(
            final_result=self.last_result,
            app_suggested_failure_mode=self.auto_suggested_failure_mode,
            specimen_id=self.measured_specimen_id or self.specimen_id_var.get(),
            ground_truth_verdict=self.ground_truth_verdict_var.get(),
            ground_truth_failure_mode=self.ground_truth_failure_mode_var.get(),
            failure_reason_assessment=self.failure_reason_assessment_var.get(),
            ground_truth_basis=self.ground_truth_basis_var.get(),
            confidence=self.review_confidence_var.get(),
            known_physical_root_cause=self.known_root_cause_var.get(),
            notes=self.review_notes_var.get(),
            reviewer=self.reviewer_var.get(),
        )

    def _update_review_status(self) -> None:
        notes = self.review_notes_var.get().strip()
        self.review_notes_status_var.set(
            f"Explanation saved ({len(notes)} chars)" if notes else "Explanation required"
        )
        try:
            review = self._current_calibration_review()
        except ValueError as exc:
            self.review_status_var.set("Incomplete: " + str(exc))
        else:
            label = review.review_classification.replace("_", " ").title()
            self.review_status_var.set(
                f"Ready to save · {label} · app verdict {review.verdict_assessment.lower()}"
            )

    def on_ground_truth_changed(self, _event=None) -> None:
        verdict = self.ground_truth_verdict_var.get().strip().upper()
        app_verdict = (
            "PASS" if self.last_result is not None and self.last_result.passed else "FAIL"
        )
        if verdict == "PASS":
            self.ground_truth_failure_mode_var.set(GOOD_GROUND_TRUTH_MODE)
            self.failure_reason_assessment_var.set(
                "NOT APPLICABLE" if app_verdict == "PASS" else "INCORRECT"
            )
        elif verdict == "UNSURE":
            self.ground_truth_failure_mode_var.set(UNKNOWN_GROUND_TRUTH_MODE)
            self.failure_reason_assessment_var.set("UNSURE")
        elif verdict == "FAIL":
            current = self.ground_truth_failure_mode_var.get()
            if current not in (*FAILURE_MODE_CHOICES, OTHER_GROUND_TRUTH_MODE):
                self.ground_truth_failure_mode_var.set("")
            self.failure_reason_assessment_var.set(
                "INCORRECT" if app_verdict == "PASS" else ""
            )
        self._update_review_status()
        self.write_autosave("ground_truth_verdict_changed")

    def on_review_field_changed(self, _event=None) -> None:
        self._update_review_status()
        self.write_autosave("calibration_review_changed")

    def open_review_notes_window(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(f"Ground-truth explanation — {self.specimen_id_var.get() or self.current_sensor_id}")
        dialog.minsize(S(700), S(460))
        dialog.configure(bg=PAGE_BG)
        dialog.transient(self)
        frame = tk.Frame(dialog, bg=PAGE_BG)
        frame.grid(row=0, column=0, sticky="nsew", padx=S(18), pady=S(16))
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        frame.columnconfigure(0, weight=1)
        tk.Label(
            frame,
            text="CALIBRATION EVIDENCE — REQUIRED",
            bg=PAGE_BG,
            fg=ELTEC_RED,
            font=self.fm(11, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            frame,
            text="Explain the known condition and why this ground truth is trusted",
            bg=PAGE_BG,
            fg=TEXT_DARK,
            font=self.fd(18),
        ).grid(row=1, column=0, sticky="w", pady=(S(3), 0))
        tk.Label(
            frame,
            text=(
                "Include independent scope/bench evidence, observed symptom, prior failure history, "
                "or why the sensor remains uncertain. Do not merely repeat the app verdict."
            ),
            bg=PAGE_BG,
            fg=MUTED_FG,
            font=self.fb(11),
            wraplength=S(650),
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(S(6), S(10)))
        text_widget = tk.Text(
            frame,
            wrap="word",
            font=self.fb(13),
            undo=True,
            relief="flat",
            bd=0,
            bg=CARD_BG,
            fg=TEXT_DARK,
            padx=12,
            pady=10,
            insertbackground=ELTEC_BLUE,
            highlightbackground=CARD_BORDER,
            highlightcolor=ELTEC_BLUE,
            highlightthickness=1,
        )
        text_widget.grid(row=3, column=0, sticky="nsew")
        text_widget.insert("1.0", self.review_notes_var.get())
        buttons = tk.Frame(frame, bg=PAGE_BG)
        buttons.grid(row=4, column=0, sticky="e", pady=(S(14), 0))

        def save_notes(_event=None) -> str:
            self.review_notes_var.set(text_widget.get("1.0", "end-1c").strip())
            self._update_review_status()
            self.write_autosave("calibration_review_notes_changed")
            dialog.destroy()
            return "break"

        self.btn(buttons, "Cancel", dialog.destroy, kind="ghost", size="sm").grid(row=0, column=0, padx=(0, S(10)))
        self.btn(buttons, "Save Explanation", save_notes, kind="primary", size="sm").grid(row=0, column=1)
        dialog.bind("<Control-Return>", save_notes)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        text_widget.focus_set()

    def _build_calibration_review_card(self, row: int) -> None:
        result = self.last_result
        if result is None:
            return
        review_bg = "#fff8e1"
        card = Card(
            self.step_frame,
            card_bg=review_bg,
            border=mix_color(WARN_ACCENT, review_bg, 0.40),
            accent_stops=[WARN_ACCENT, ELTEC_RED],
            pad=(18, 16),
        )
        card.grid(row=row, column=0, sticky="ew", pady=(S(14), 0))
        inner = card.inner
        inner.columnconfigure(0, weight=1, uniform="review")
        inner.columnconfigure(1, weight=1, uniform="review")
        tk.Label(
            inner,
            text="GROUND-TRUTH REVIEW · REQUIRED BEFORE SAVE",
            bg=review_bg,
            fg=WARN_FG,
            font=self.fm(10, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        app_verdict = "PASS" if result.passed else "FAIL"
        automatic_reason = "; ".join(result.fail_reasons) or "No automatic failure reason"
        tk.Label(
            inner,
            text=(
                f"Frozen app prediction: {app_verdict}"
                + (
                    f" · {self.auto_suggested_failure_mode}"
                    if self.auto_suggested_failure_mode
                    else ""
                )
                + f"\nAutomatic reasons: {automatic_reason}"
            ),
            bg=review_bg,
            fg=TEXT_DARK,
            font=self.fb(11, "bold"),
            wraplength=S(820),
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(S(6), S(12)))

        def field_label(text: str, grid_row: int, column: int) -> None:
            tk.Label(
                inner,
                text=text,
                bg=review_bg,
                fg=MUTED_FG,
                font=self.fm(9, "bold"),
            ).grid(row=grid_row, column=column, sticky="w", padx=(0 if column == 0 else S(10), 0))

        field_label("PHYSICAL SPECIMEN ID", 2, 0)
        field_label("GROUND-TRUTH VERDICT", 2, 1)
        specimen_entry = ttk.Entry(
            inner,
            textvariable=self.specimen_id_var,
            font=self.fb(13),
            style="Card.TEntry",
            state="readonly",
        )
        specimen_entry.grid(row=3, column=0, sticky="ew", padx=(0, S(10)), pady=(S(3), S(10)))
        truth_combo = ttk.Combobox(
            inner,
            textvariable=self.ground_truth_verdict_var,
            values=GROUND_TRUTH_VERDICT_CHOICES,
            state="readonly",
            font=self.fb(12),
        )
        truth_combo.grid(row=3, column=1, sticky="ew", padx=(S(10), 0), pady=(S(3), S(10)))
        truth_combo.bind("<<ComboboxSelected>>", self.on_ground_truth_changed)

        field_label("ACTUAL PRIMARY CONDITION", 4, 0)
        field_label("WAS THE APP FAILURE REASON CORRECT?", 4, 1)
        mode_combo = ttk.Combobox(
            inner,
            textvariable=self.ground_truth_failure_mode_var,
            values=GROUND_TRUTH_FAILURE_MODE_CHOICES,
            state="readonly",
            font=self.fb(11),
            height=12,
        )
        mode_combo.grid(row=5, column=0, sticky="ew", padx=(0, S(10)), pady=(S(3), S(10)))
        mode_combo.bind("<<ComboboxSelected>>", self.on_review_field_changed)
        reason_combo = ttk.Combobox(
            inner,
            textvariable=self.failure_reason_assessment_var,
            values=FAILURE_REASON_ASSESSMENT_CHOICES,
            state="readonly",
            font=self.fb(11),
        )
        reason_combo.grid(row=5, column=1, sticky="ew", padx=(S(10), 0), pady=(S(3), S(10)))
        reason_combo.bind("<<ComboboxSelected>>", self.on_review_field_changed)

        field_label("GROUND-TRUTH BASIS", 6, 0)
        field_label("CONFIDENCE", 6, 1)
        basis_combo = ttk.Combobox(
            inner,
            textvariable=self.ground_truth_basis_var,
            values=GROUND_TRUTH_BASIS_CHOICES,
            state="readonly",
            font=self.fb(11),
        )
        basis_combo.grid(row=7, column=0, sticky="ew", padx=(0, S(10)), pady=(S(3), S(10)))
        basis_combo.bind("<<ComboboxSelected>>", self.on_review_field_changed)
        confidence_combo = ttk.Combobox(
            inner,
            textvariable=self.review_confidence_var,
            values=REVIEW_CONFIDENCE_CHOICES,
            state="readonly",
            font=self.fb(11),
        )
        confidence_combo.grid(row=7, column=1, sticky="ew", padx=(S(10), 0), pady=(S(3), S(10)))
        confidence_combo.bind("<<ComboboxSelected>>", self.on_review_field_changed)

        field_label("GROUND-TRUTH REVIEWER", 8, 0)
        field_label("KNOWN PHYSICAL ROOT CAUSE (OPTIONAL)", 8, 1)
        reviewer_entry = ttk.Entry(
            inner,
            textvariable=self.reviewer_var,
            font=self.fb(12),
            style="Card.TEntry",
        )
        reviewer_entry.grid(
            row=9,
            column=0,
            sticky="ew",
            padx=(0, S(10)),
            pady=(S(3), S(10)),
        )
        reviewer_entry.bind("<FocusOut>", self.on_review_field_changed)
        root_cause_entry = ttk.Entry(
            inner,
            textvariable=self.known_root_cause_var,
            font=self.fb(12),
            style="Card.TEntry",
        )
        root_cause_entry.grid(
            row=9,
            column=1,
            sticky="ew",
            padx=(S(10), 0),
            pady=(S(3), S(10)),
        )
        root_cause_entry.bind("<FocusOut>", self.on_review_field_changed)

        notes_row = tk.Frame(inner, bg=review_bg)
        notes_row.grid(row=10, column=0, columnspan=2, sticky="ew")
        self.btn(
            notes_row,
            "Add / Edit Required Explanation",
            self.open_review_notes_window,
            kind="outline",
            size="sm",
            parent_bg=review_bg,
        ).pack(side="left")
        tk.Label(
            notes_row,
            textvariable=self.review_notes_status_var,
            bg=review_bg,
            fg=MUTED_FG,
            font=self.fb(10),
        ).pack(side="left", padx=(S(10), 0))
        self._update_review_status()
        tk.Label(
            inner,
            textvariable=self.review_status_var,
            bg=review_bg,
            fg=WARN_FG,
            font=self.fb(10, "bold"),
            wraplength=S(820),
            justify="left",
        ).grid(row=11, column=0, columnspan=2, sticky="w", pady=(S(9), 0))
        evidence_text = (
            f"Run evidence saved: {self.last_run_manifest_path}"
            if self.last_run_manifest_path is not None
            else "Run evidence is not yet saved. Saving the reviewed result will retry."
        )
        if self.last_run_evidence_error:
            evidence_text += f" Error: {self.last_run_evidence_error}"
        tk.Label(
            inner,
            text=evidence_text,
            bg=review_bg,
            fg=MUTED_FG,
            font=self.fm(8),
            wraplength=S(820),
            justify="left",
        ).grid(row=12, column=0, columnspan=2, sticky="w", pady=(S(5), 0))

    def render_result_view(self) -> None:
        result = self.last_result
        passed = result.passed
        self._build_result_banner(row=0, passed=passed)
        next_row = 1
        if not passed:
            failure_card = Card(
                self.step_frame,
                card_bg=FAIL_BG,
                border=mix_color(FAIL_ACCENT, FAIL_BG, 0.45),
                accent_stops=[FAIL_ACCENT, FAIL_ACCENT],
                pad=(18, 14),
            )
            failure_card.grid(row=next_row, column=0, sticky="ew", pady=(14, 0))
            failure_inner = failure_card.inner
            failure_inner.columnconfigure(0, weight=1)
            tk.Label(
                failure_inner,
                text="PRODUCTION FAILURE MODE (EDITABLE RECORD)",
                bg=FAIL_BG,
                fg=FAIL_FG,
                font=self.fm(10, "bold"),
            ).grid(row=0, column=0, sticky="w", pady=(0, 5))
            failure_combo = ttk.Combobox(
                failure_inner,
                textvariable=self.failure_mode_var,
                values=FAILURE_MODE_CHOICES,
                state="readonly",
                font=self.fb(14),
                height=10,
            )
            failure_combo.grid(row=1, column=0, sticky="ew")
            failure_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event: self.write_autosave("failure_mode_selected"),
            )
            tk.Label(
                failure_inner,
                text=(
                    "This starts with the app suggestion. Editing it does not alter the frozen "
                    "prediction shown in the ground-truth card below."
                ),
                bg=FAIL_BG,
                fg=FAIL_FG,
                font=self.fb(10),
            ).grid(row=2, column=0, sticky="w", pady=(6, 0))
            next_row += 1
        self._build_calibration_review_card(next_row)
        next_row += 1
        if self.show_details_var.get():
            tiles = tk.Frame(self.step_frame, bg=PAGE_BG)
            tiles.grid(row=next_row, column=0, sticky="ew", pady=(14, 0))
            for column in range(3):
                tiles.columnconfigure(column, weight=1, uniform="tiles")
            offset_ok = result.offset_v is not None and OFFSET_MIN_V <= result.offset_v <= OFFSET_MAX_V
            min_mv = FILTER_SPECS_MV.get(self.filter_setup)
            sens_ok = result.sensitivity_mv is not None and min_mv is not None and result.sensitivity_mv >= min_mv
            pol_verdict = polarity_good_bad(result.polarity)
            self._result_tile(tiles, 0, "Offset", result.offset_v, offset_ok, unit=" V", decimals=3)
            self._result_tile(tiles, 1, "Sensitivity", result.sensitivity_mv, sens_ok, unit=" mV", decimals=2)
            self._result_tile(tiles, 2, "Polarity", pol_verdict or None, pol_verdict == "GOOD")
            next_row += 1

            detail_bits = [f"Filter: {self.filter_setup}"]
            if min_mv is not None:
                detail_bits.append(f"min sensitivity {min_mv:.1f} mV")
            if result.polarity and polarity_good_bad(result.polarity):
                detail_bits.append(f"polarity {result.polarity}")
            pol_detail = format_polarity_detail(self.last_metrics)
            if pol_detail:
                detail_bits.append(pol_detail)
            if self.last_metrics is not None and self.last_metrics.signal_to_noise_db is not None \
                    and math.isfinite(self.last_metrics.signal_to_noise_db):
                detail_bits.append(f"SNR {self.last_metrics.signal_to_noise_db:.1f} dB")
            report = self.last_capture_report
            if report is not None and report.data_source == "simulator":
                detail_bits.append("SIMULATED DATA")
            if self.last_reference_check_mv is not None:
                calibration = self.reference_calibration
                if calibration is not None and calibration.valid:
                    detail_bits.append(
                        f"reference {self.last_reference_check_mv:.2f} mV "
                        f"({calibration.drift_percent(self.last_reference_check_mv):+.1f}%)"
                    )
                else:
                    detail_bits.append(f"reference {self.last_reference_check_mv:.2f} mV")
            if report is not None and report.capture_cycles:
                detail_bits.append(f"PWM on {report.pwm_on_seconds:.1f} s / {report.capture_cycles} cycles")
            if report is not None and report.stabilized:
                detail_bits.append(
                    f"stable at {report.stabilization_seconds:.1f} s / cycle {report.stabilization_cycle}"
                )
                detail_bits.append(
                    f"attempt {report.measurement_attempt} / sensitivity window "
                    f"{report.measurement_cycles} cycles"
                )
            elif report is not None and report.unstable:
                detail_bits.append(
                    f"unstable on attempt {report.measurement_attempt}"
                )
            tk.Label(
                self.step_frame,
                text="   ·   ".join(detail_bits),
                bg=PAGE_BG,
                fg=MUTED_FG,
                font=self.fb(11),
                wraplength=S(880),
                justify="left",
                anchor="w",
            ).grid(row=next_row, column=0, sticky="w", pady=(S(12), 0))
            next_row += 1

            if result.fail_reasons:
                reasons = tk.Text(
                    self.step_frame,
                    height=min(len(result.fail_reasons) + 1, 4),
                    wrap="word",
                    font=self.fb(12),
                    relief="flat",
                    bd=0,
                    bg="#fff5f5",
                    fg=FAIL_FG,
                    padx=12,
                    pady=10,
                    highlightbackground="#f3c2c2",
                    highlightcolor="#f3c2c2",
                    highlightthickness=1,
                )
                reasons.grid(row=next_row, column=0, sticky="ew", pady=(10, 0))
                reasons.insert("1.0", "\n".join(f"•  {reason}" for reason in result.fail_reasons))
                reasons.configure(state="disabled")
                next_row += 1

        tools = tk.Frame(self.step_frame, bg=PAGE_BG)
        tools.grid(row=next_row, column=0, sticky="w", pady=(16, 0))
        self.btn(tools, "Comment", self.open_comment_window, kind="ghost", size="sm").grid(row=0, column=0, padx=(0, 10))
        self.btn(tools, "Capture waveform", self.capture_waveform_snapshot, kind="ghost", size="sm").grid(row=0, column=1, padx=(0, 10))
        tk.Label(
            tools,
            text="To repeat: complete this review, then use Save + Repeat Same Specimen.",
            bg=PAGE_BG,
            fg=MUTED_FG,
            font=self.fb(10, "bold"),
        ).grid(row=0, column=2, padx=(S(8), S(14)))
        ToggleSwitch(
            tools,
            "Show test details",
            self.show_details_var,
            command=self.toggle_result_details,
            font=self.fb(12),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(S(12), 0))
        ToggleSwitch(
            tools,
            "Show waveform",
            self.show_live_var,
            command=self.toggle_live_view,
            font=self.fb(12),
        ).grid(row=1, column=2, sticky="w", pady=(S(12), 0))
        tk.Label(tools, textvariable=self.comment_status_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        tk.Label(tools, textvariable=self.snapshot_status_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=3, column=0, columnspan=3, sticky="w")

        if self.show_live_var.get():
            self._build_wave_canvas(row=next_row + 1, live=False)
            self.redraw_waveform()

    def _build_result_banner(self, row: int, passed: bool) -> None:
        accent = PASS_ACCENT if passed else FAIL_ACCENT
        banner_bg = PASS_BG if passed else FAIL_BG
        banner_fg = PASS_FG if passed else FAIL_FG
        banner = tk.Canvas(self.step_frame, height=S(112), bg=PAGE_BG, highlightthickness=0, bd=0)
        banner.grid(row=row, column=0, sticky="ew")
        vals = {"bar": 0.0, "glyph": 0.0, "text": 0.0}
        glyph = "✓" if passed else "✕"
        verdict = "PASS" if passed else "FAIL"

        def redraw() -> None:
            banner.delete("all")
            width = max(1, banner.winfo_width())
            height = S(112)
            draw_round_rect(banner, S(2), S(2), width - S(2), height - S(2), Sf(14), fill=banner_bg, outline=mix_color(accent, banner_bg, 0.55))
            bar_h = (height - S(16)) * vals["bar"]
            if bar_h > 2:
                draw_round_rect(banner, S(10), S(8) + (height - S(16) - bar_h) / 2, S(18), S(8) + (height - S(16) + bar_h) / 2, Sf(4), fill=accent, outline="")
            glyph_size = max(1, int(round(6 + 38 * vals["glyph"])))
            center_x = width / 2
            banner.create_text(center_x - S(78), height / 2, text=glyph, fill=accent, font=self.fd(glyph_size))
            text_x = center_x - S(30) + S(26) * (1.0 - vals["text"])
            text_color = mix_color(banner_bg, banner_fg, vals["text"])
            banner.create_text(text_x, height / 2, anchor="w", text=verdict, fill=text_color, font=self.fd(38))

        def animate(key: str, name: str, duration: int, easing, delay: int) -> None:
            def frame(t: float) -> None:
                vals[key] = t
                redraw()
            self.animator.animate(name, duration, frame, easing=easing, delay_ms=delay)

        banner.bind("<Configure>", lambda _e: redraw())
        animate("bar", "step:banner_bar", 420, ease_out_cubic, 0)
        animate("glyph", "step:banner_glyph", 520, ease_out_back, 120)
        animate("text", "step:banner_text", 420, ease_out_cubic, 220)

    def _result_tile(self, parent: tk.Frame, column: int, label: str, value, ok: bool, unit: str = "", decimals: int = 2) -> None:
        accent = PASS_ACCENT if ok else FAIL_ACCENT
        card = Card(parent, accent_stops=[accent, accent], pad=(18, 14))
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else S(12), 0))
        inner = card.inner
        tk.Label(inner, text=label.upper(), bg=CARD_BG, fg=MUTED_FG, font=self.fm(10, "bold")).pack(anchor="w")
        # Fixed character width so the count-up animation never changes the
        # label's requested size (a growing label would relayout the whole
        # step frame on every animation frame).
        value_label = tk.Label(inner, bg=CARD_BG, fg=accent, font=self.fd(25), width=12, anchor="w")
        value_label.pack(anchor="w", pady=(S(4), 0))
        if isinstance(value, (int, float)):
            target = float(value)

            def frame(t: float) -> None:
                value_label.configure(text=f"{target * t:.{decimals}f}{unit}")

            frame(0.0)
            self.animator.animate(f"step:tile{column}", 700, frame, easing=ease_out_cubic, delay_ms=140 + column * 110)
        elif value is None:
            value_label.configure(text="Not measured", font=self.fd(17))
        else:
            # Fade the color in rather than animating the font size: a size
            # animation changes the label's requested size every frame, which
            # would relayout the whole step frame at 60 fps.
            value_label.configure(text=str(value))

            def fade(t: float) -> None:
                value_label.configure(fg=mix_color(CARD_BG, accent, t))

            fade(0.0)
            self.animator.animate(f"step:tile{column}", 520, fade, easing=ease_in_out, delay_ms=140 + column * 110)

    def _build_wave_canvas(self, row: int, live: bool) -> None:
        # minsize keeps the scope readable even when the step content above it
        # (e.g. a FAIL view with reasons) is tall.
        self.step_frame.rowconfigure(row, weight=1, minsize=S(170))
        wrapper = tk.Frame(self.step_frame, bg=PAGE_BG)
        wrapper.grid(row=row, column=0, sticky="nsew", pady=(S(14), 0))
        wrapper.columnconfigure(1, weight=1)
        wrapper.rowconfigure(1, weight=1)
        if live:
            PulseDot(wrapper, self.animator, "step:live", color=ELTEC_RED, bg=PAGE_BG, size=13).grid(row=0, column=0, padx=(0, S(6)))
        tk.Label(wrapper, text="LIVE SIGNAL  ·  ADS AIN0 SENSOR + ESP32 PWM SYNC", bg=PAGE_BG, fg=MUTED_FG, font=self.fm(10, "bold")).grid(row=0, column=1, sticky="w")
        scope = ScopeView(wrapper, self.animator, "step:scope", height=240)
        scope.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(S(6), 0))
        self.wave_canvas = scope
        scope.set_data(self.preview_waveform, self.preview_sync)

    # ----- navigation ----- #
    def render_navigation(self) -> None:
        # The footer bar is a fixed row below the step frame (built once in
        # _build_layout), so the buttons can never be pushed off-screen by
        # tall step content. Rebuild its buttons for the current step.
        for child in self.footer_bar.winfo_children():
            child.destroy()
        self.back_button = self.btn(self.footer_bar, "Back", self.go_back, kind="ghost", size="lg")
        self.back_button.grid(row=0, column=0, sticky="w")
        self.repeat_button = self.btn(
            self.footer_bar,
            "Save + Repeat Specimen",
            self.save_and_repeat_specimen,
            kind="outline",
            size="lg",
        )
        self.repeat_button.grid(row=0, column=1, sticky="e", padx=(0, S(10)))
        self.secondary_button = self.btn(self.footer_bar, "Save + Exit Batch", self.save_and_end_batch, kind="outline", size="lg")
        self.secondary_button.grid(row=0, column=2, sticky="e", padx=(0, S(10)))
        self.primary_button = self.btn(self.footer_bar, "Next", self.go_next, kind="primary", size="lg")
        self.primary_button.grid(row=0, column=3, sticky="e")

    def update_navigation_state(self) -> None:
        self.secondary_button.grid_remove()
        self.repeat_button.grid_remove()
        if self.step == self.SETUP_STEP:
            self.back_button.configure(state="disabled")
            self.primary_button.configure(text="Start (Enter)", state="disabled" if self.busy else "normal")
        elif self.step == self.LOAD_STEP:
            self.back_button.configure(state="disabled" if self.busy else "normal")
            # Hard block: no measurement on a low battery or a wiring fault.
            blocked = (
                self.busy
                or self.battery_state in ("low", "fault")
                or self.stability_config_error is not None
                or not self.reference_gate_ready()
            )
            if self.stability_config_error is not None:
                measure_text = "Fix stability settings"
            elif self.battery_state == "fault":
                measure_text = "Check wiring to test"
            elif self.battery_state == "low":
                measure_text = "Recharge battery to test"
            elif not self.reference_gate_ready():
                measure_text = "Calibrate reference unit to test"
            else:
                measure_text = "Measure (Enter)"
            self.primary_button.configure(text=measure_text, state="disabled" if blocked else "normal")
        else:
            self.secondary_button.grid()
            self.repeat_button.grid()
            self.back_button.configure(state="disabled" if self.busy or self.result_saved else "normal")
            ready = not self.busy and not self.measuring and self.last_result is not None and not self.result_saved
            self.primary_button.configure(text="Save + Next Sensor (Enter)", state="normal" if ready else "disabled")
            self.secondary_button.configure(text="Save + Exit Batch (Esc)", state="normal" if ready else "disabled")
            self.repeat_button.configure(text="Save + Repeat Same Specimen", state="normal" if ready else "disabled")

    def go_next(self) -> None:
        if self.busy:
            return
        if self.step == self.SETUP_STEP:
            self.start_batch()
        elif self.step == self.LOAD_STEP:
            if self.stability_config_error is not None:
                self.run_measurement()
                return
            self.show_step(self.RESULT_STEP)
            self.run_measurement()
        elif self.step == self.RESULT_STEP:
            self.save_and_continue()

    def go_back(self) -> None:
        if self.busy or self.measuring:
            return
        if self.step == self.LOAD_STEP:
            # Returning to setup explicitly ends the in-memory batch session;
            # already-saved CSV rows remain available if the same batch resumes.
            self.batch_active = False
            self.show_step(self.SETUP_STEP)
        elif self.step == self.RESULT_STEP and not self.result_saved:
            messagebox.showinfo(
                "Review this run before leaving",
                "Every acquisition must receive a ground-truth review. Save it, or use "
                "Save + Repeat Same Specimen to collect another independent run.",
            )

    def show_step(self, step: str) -> None:
        self.step = step
        self.render_step()
        # Re-check the battery whenever we arrive at the load step (a sensor is
        # about to be tested) so the watcher reflects the current supply.
        if step == self.LOAD_STEP:
            self.refresh_battery()

    def on_enter_key(self, event: tk.Event) -> str | None:
        if isinstance(event.widget, (ttk.Button, RoundButton)):
            return None
        if self.busy or self.measuring:
            return "break"
        if self.step == self.RESULT_STEP and (self.last_result is None or self.result_saved):
            return "break"
        self.go_next()
        return "break"

    def on_escape_key(self, _event: tk.Event) -> str | None:
        if self.step == self.RESULT_STEP and not self.busy and not self.measuring and self.last_result is not None and not self.result_saved:
            self.save_and_end_batch()
            return "break"
        return None

    def focus_default_widget(self) -> None:
        widget = self.default_focus_widget
        if self.busy or widget is None or not widget.winfo_exists():
            if widget is None:
                self.focus_set()
            return
        widget.focus_set()
        try:
            widget.selection_range(0, tk.END)
            widget.icursor(tk.END)
        except (AttributeError, tk.TclError):
            pass

    # ----- batch lifecycle ----- #
    def start_batch(self) -> None:
        if self.last_result is not None and not self.result_saved:
            messagebox.showerror(
                "Unsaved calibration run",
                "Return to the verdict page and save the required ground-truth review "
                "before starting or resuming another batch.",
            )
            self.step = self.RESULT_STEP
            self.render_step()
            return
        batch_number = self.batch_var.get().strip()
        if not batch_number:
            messagebox.showerror("Batch number needed", "Please enter a batch number.")
            return
        tester_name = self.tester_var.get().strip()
        if not tester_name:
            messagebox.showerror("Tester name needed", "Please enter the tester name.")
            return

        selected_synthetic = bool(self.simulator_var.get())
        csv_path = batch_results_path(
            batch_number,
            is_synthetic=selected_synthetic,
        )
        try:
            validate_calibration_csv_schema(
                csv_path,
                expected_batch_number=batch_number,
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Choose a new calibration batch ID", str(exc))
            return
        self.batch_number = batch_number
        self.tester_name = tester_name
        self.reviewer_var.set(tester_name)
        self.filter_setup = self.filter_var.get()
        self.batch_is_synthetic = selected_synthetic
        self.batch_active = True
        if self._advanced_dialog is not None and self._advanced_dialog.winfo_exists():
            self._advanced_dialog.destroy()
        if self.recover_pending_autosave(csv_path):
            return
        self.current_sensor_number = next_sensor_number_for_batch(csv_path)
        existing = count_existing_batch_rows(csv_path)
        position = "next" if existing else "first"
        self.status_var.set(f"Batch {batch_number}: {position} sensor is {batch_number}-{self.current_sensor_number}.")
        self.prepare_current_sensor()
        self.show_step(self.LOAD_STEP)

    def prepare_current_sensor(self, *, preserve_specimen_id: bool = False) -> None:
        specimen_id = self.specimen_id_var.get().strip() if preserve_specimen_id else ""
        self.current_sensor_id = f"{self.batch_number}-{self.current_sensor_number}"
        self.result_saved = False
        self.last_metrics = None
        self.last_result = None
        self.last_capture_report = None
        self.last_reference_check_mv = None
        self.last_reference_calibration = None
        self.show_details_var.set(False)
        self.preview_waveform = np.array([], dtype=float)
        self.preview_sync = np.array([], dtype=float)
        self.snapshot_paths = []
        self.stability_diagnostics_saved = False
        self.active_run_id = ""
        self.measured_specimen_id = ""
        self.measurement_run_number = 0
        self.run_history = []
        self.last_run_evidence_paths = []
        self.last_run_manifest_path = None
        self.last_run_evidence_error = None
        self.evidence_saving = False
        self.autosave_error = None
        self.auto_suggested_failure_mode = ""
        self.last_dut_raw_samples = ()
        self.last_dut_stream_diagnostics = None
        self.last_rig_metadata = {}
        self.last_reference_capture = None
        self.offset_plausibility_override_applied = False
        self.notes_var.set("")
        self.failure_mode_var.set("")
        self.specimen_id_var.set(specimen_id)
        self.ground_truth_verdict_var.set("")
        self.ground_truth_failure_mode_var.set("")
        self.failure_reason_assessment_var.set("")
        self.ground_truth_basis_var.set("")
        self.review_confidence_var.set("")
        self.reviewer_var.set(self.tester_name)
        self.known_root_cause_var.set("")
        self.review_notes_var.set("")
        self.sensor_seated_override_var.set(False)
        self.comment_status_var.set("")
        self.snapshot_status_var.set("")
        self.measure_status_var.set("")
        self.review_status_var.set("Ground-truth review required")
        self.review_notes_status_var.set("Explanation required")

    def save_and_continue(self) -> None:
        if self.save_current_sensor():
            self.tester_name = self.tester_var.get().strip() or self.tester_name
            self.current_sensor_number += 1
            self.prepare_current_sensor()
            self.show_step(self.LOAD_STEP)

    def save_and_repeat_specimen(self) -> None:
        specimen_id = self.specimen_id_var.get().strip()
        if self.save_current_sensor():
            self.tester_name = self.tester_var.get().strip() or self.tester_name
            self.current_sensor_number += 1
            self.specimen_id_var.set(specimen_id)
            self.prepare_current_sensor(preserve_specimen_id=True)
            self.status_var.set(
                f"Saved. Reseat specimen {specimen_id} for an independent repeat."
            )
            self.show_step(self.LOAD_STEP)

    def save_and_end_batch(self) -> None:
        if self.save_current_sensor():
            saved_batch = self.batch_number
            saved_csv = batch_results_path(
                saved_batch,
                is_synthetic=self.batch_is_synthetic,
            )
            self.status_var.set(f"Batch {saved_batch} ended.")
            self.batch_active = False
            self.step = self.SETUP_STEP
            self.result_saved = True
            self.show_batch_summary_window(saved_batch, saved_csv)
            self.render_step()

    def save_current_sensor(self) -> bool:
        if self.last_result is None:
            messagebox.showinfo("Nothing to save", "Run the measurement before saving.")
            return False
        try:
            calibration_review = self._current_calibration_review()
        except ValueError as exc:
            messagebox.showerror("Ground-truth review incomplete", str(exc))
            self._update_review_status()
            return False
        pwm_hz, pwm_duty = EMITTER_PWM_FREQUENCY_HZ, EMITTER_PWM_DUTY_CYCLE
        failure_mode = ""
        if not self.last_result.passed:
            try:
                split_failure_mode(self.failure_mode_var.get())
            except ValueError as exc:
                messagebox.showerror("Failure mode needed", str(exc))
                return False
            failure_mode = self.failure_mode_var.get()
        if not self._ensure_current_run_evidence():
            messagebox.showerror(
                "Could not preserve calibration evidence",
                self.last_run_evidence_error
                or "The full waveform/cycle evidence bundle could not be created.",
            )
            return False
        try:
            if (
                self.last_capture_report is not None
                and self.last_capture_report.unstable
                and not self.stability_diagnostics_saved
            ):
                if self.last_metrics is None:
                    raise RuntimeError(
                        "The unstable waveform is unavailable; the result was not saved."
                    )
                diagnostic_paths = save_waveform_diagnostic_bundle(
                    self.batch_number,
                    self.current_sensor_id,
                    self.last_metrics,
                    self.last_capture_report,
                    title=(
                        f"{MODEL_NAME} {self.current_sensor_id} unstable"
                    ),
                    detail_lines=snapshot_detail_lines(
                        self.batch_number,
                        self.current_sensor_id,
                        self.last_metrics,
                        self.notes_var.get(),
                        self.last_capture_report,
                    ),
                    filename_suffix="unstable",
                    output_dir=(
                        study_results_dir(is_synthetic=self.batch_is_synthetic)
                        / "waveform_snapshots"
                        / f"lot_{safe_filename_part(self.batch_number)}"
                    ),
                    specimen_id=self.measured_specimen_id,
                    run_id=self.active_run_id,
                    raw_samples=self.last_dut_raw_samples,
                )
                if not diagnostic_paths:
                    raise RuntimeError(
                        "The unstable diagnostic bundle could not be created."
                    )
                self.snapshot_paths.extend(diagnostic_paths)
                self.stability_diagnostics_saved = True
            related_run_ids = [item["run_id"] for item in self.run_history]
            if self.active_run_id not in related_run_ids:
                related_run_ids.append(self.active_run_id)
            all_evidence_paths: list[Path] = []
            for item in self.run_history:
                all_evidence_paths.extend(
                    Path(path) for path in item.get("evidence_paths", [])
                )
            if not all_evidence_paths:
                all_evidence_paths = list(self.last_run_evidence_paths)
            review_record_path = save_calibration_review_record(
                batch_number=self.batch_number,
                sensor_id=self.current_sensor_id,
                run_id=self.active_run_id,
                review=calibration_review,
                is_synthetic=self.batch_is_synthetic,
                production_failure_mode=failure_mode,
                operator_comment=self.notes_var.get(),
                related_run_ids=related_run_ids,
                evidence_paths=all_evidence_paths,
            )
            csv_path = batch_results_path(
                self.batch_number,
                is_synthetic=self.batch_is_synthetic,
            )
            repeat_number = next_specimen_repeat_number(
                csv_path,
                calibration_review.specimen_id,
            )
            append_result_csv(
                csv_path,
                batch_number=self.batch_number,
                sensor_number=self.current_sensor_number,
                sensor_id=self.current_sensor_id,
                tester_name=self.tester_name,
                filter_setup=self.filter_setup,
                pwm_channel=EMITTER_PWM_CHANNEL,
                pwm_hz=pwm_hz,
                pwm_duty=pwm_duty,
                final_result=self.last_result,
                comment=self.notes_var.get(),
                snapshot_paths=self.snapshot_paths,
                battery_v=self.battery_v,
                capture_report=self.last_capture_report,
                reference_calibration=(
                    None if self.batch_is_synthetic else self.last_reference_calibration
                ),
                reference_check_mv=(
                    None if self.batch_is_synthetic else self.last_reference_check_mv
                ),
                failure_mode=failure_mode,
                calibration_review=calibration_review,
                app_suggested_failure_mode=self.auto_suggested_failure_mode,
                session_id=self.session_id,
                run_id=self.active_run_id,
                measurement_run_number=max(1, self.measurement_run_number),
                specimen_repeat_number=repeat_number,
                is_synthetic=self.batch_is_synthetic,
                calibration_evidence_paths=all_evidence_paths,
                run_manifest_path=self.last_run_manifest_path,
                review_record_path=review_record_path,
                related_run_ids=related_run_ids,
                offset_plausibility_override=self.offset_plausibility_override_applied,
            )
        except Exception as exc:
            messagebox.showerror("Could not save result", str(exc))
            return False
        self.result_saved = True
        self.delete_autosave()
        self.status_var.set(
            f"Saved {self.current_sensor_id} · {calibration_review.review_classification.replace('_', ' ').lower()}."
        )
        self.update_navigation_state()
        return True

    def _post(self, callback) -> None:
        """Queue a worker result without calling Tk from a worker thread."""
        if not self.closing:
            self.ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        """Run queued worker callbacks on Tk's main thread."""
        if self.closing:
            return
        try:
            for _ in range(200):
                try:
                    callback = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback()
                except Exception as exc:  # keep later hardware completions flowing
                    if not self.closing:
                        try:
                            self.status_var.set(
                                "Internal completion callback failed: " + str(exc)
                            )
                        except (RuntimeError, tk.TclError):
                            pass
        finally:
            if not self.closing:
                try:
                    self.after(20, self._drain_ui_queue)
                except (RuntimeError, tk.TclError):
                    pass

    # ----- battery watcher ----- #
    def _refresh_battery_pill(self) -> None:
        if self.battery_v is None:
            text = "Battery: checking…" if self.battery_checking else "Battery: --"
        elif self.battery_state == "ok":
            text = f"Battery {self.battery_v:.1f} V  ✓"
        elif self.battery_state == "warn":
            text = f"Battery low  {self.battery_v:.1f} V"
        elif self.battery_state == "fault":
            text = f"CHECK WIRING  {self.battery_v:.1f} V"
        else:
            text = f"RECHARGE BATTERY  {self.battery_v:.1f} V"
        self.battery_pill.set_state(self.battery_state, text, battery_gauge_fraction(self.battery_v))

    def refresh_battery(self) -> None:
        """Read the 6 V SLA in the background and update the watcher state."""
        if self.battery_checking or self.busy or self.measuring:
            return
        self.battery_checking = True
        self.battery_request_generation += 1
        request_generation = self.battery_request_generation
        self._refresh_battery_pill()
        simulator = self.simulator_mode()
        sim_low = self.sim_low_battery_var.get()

        def worker() -> None:
            error: Exception | None = None
            battery_v: float | None = None
            try:
                if simulator:
                    time.sleep(0.15)
                    battery_v = SIM_BATTERY_LOW_V if sim_low else SIM_BATTERY_OK_V
                else:
                    with self.hardware_lock:
                        self.ensure_connected()
                        battery_v = self.device.read_battery_voltage()
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not fatal
                error = exc
            self._post(
                lambda v=battery_v, e=error, g=request_generation, s=simulator: (
                    self.on_battery_update(
                        v,
                        e,
                        request_generation=g,
                        source_is_synthetic=s,
                    )
                )
            )

        threading.Thread(target=worker, daemon=True).start()

    def on_battery_update(
        self,
        battery_v: float | None,
        error: Exception | None = None,
        *,
        request_generation: int | None = None,
        source_is_synthetic: bool | None = None,
    ) -> None:
        if request_generation is not None and (
            request_generation != self.battery_request_generation
            or bool(source_is_synthetic) != self.simulator_mode()
        ):
            return
        self.battery_checking = False
        if error is None and battery_v is not None:
            self.battery_v = battery_v
            self.battery_state = battery_state_for(battery_v)
            self.battery_read_time = time.monotonic()
            self.battery_read_is_synthetic = (
                self.simulator_mode()
                if source_is_synthetic is None
                else bool(source_is_synthetic)
            )
        elif self.battery_v is None:
            # Never got a reading (device missing/claimed): leave the pill
            # neutral and tell the technician what is wrong in the status bar.
            self.battery_state = "unknown"
            if error is not None:
                self.status_var.set(self._friendly_hardware_error(str(error)))
        self._refresh_battery_pill()
        if self.busy or self.measuring:
            return
        # Refresh on-screen banners + the Measure button lock when idle.
        if self.step in (self.LOAD_STEP, self.RESULT_STEP):
            self.render_step()
        else:
            self.update_navigation_state()

    # ----- AIN1 reference calibration / emitter-health gate ----- #
    def _capture_reference_reading(
        self,
        device: EmitterEsp32Rig,
        *,
        pwm_started_monotonic: float,
        token: int,
        push,
        status_prefix: str,
        calibration_ui: bool = False,
    ) -> float:
        dut_settings = self.stability_settings
        if dut_settings is None:
            raise StabilitySettingsError("V6.1 stability settings are unavailable.")
        settings = reference_stability_settings(dut_settings)

        def progress(current: StabilityAnalysis) -> None:
            report = current.report
            if report.stabilized:
                text = (
                    f"{status_prefix}: stable. Averaging cycle "
                    f"{report.measurement_cycle_count}/{REFERENCE_MEASUREMENT_CYCLES}…"
                )
            else:
                latest = current.cycles[-1] if current.cycles else None
                delta_text = (
                    "waiting for two peaks"
                    if latest is None or latest.absolute_peak_delta_mv is None
                    else f"peak Δ {latest.absolute_peak_delta_mv:.3f} mV"
                )
                confirmation = 0 if latest is None else latest.confirmation_run_length
                text = (
                    f"{status_prefix}: {delta_text} · "
                    f"{confirmation}/{settings.consecutive_deltas_required} stable"
                )
            push(lambda value=text: self.set_measure_status(token, value))
            if calibration_ui:
                push(lambda value=text: self.reference_progress_var.set(value))

        waveform, sync, sample_rate_hz, analysis = device.read_reference_until_stable(
            waveform_range_v=WAVEFORM_INPUT_RANGE_V,
            settings=settings,
            pwm_started_monotonic=pwm_started_monotonic,
            sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
            expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
            progress=progress,
            cancelled=lambda: token != self.measure_token,
        )
        if not calibration_ui:
            self.last_reference_capture = ReferenceEvidenceCapture(
                waveform_v=np.asarray(waveform, dtype=float).copy(),
                sync_v=np.asarray(sync, dtype=float).copy(),
                sample_rate_hz=float(sample_rate_hz),
                analysis=analysis,
                raw_samples=tuple(getattr(device, "last_stream_samples", ())),
                stream_diagnostics=getattr(
                    device, "last_stream_diagnostics", None
                ),
            )
        return analyze_reference_stable_response_mv(analysis)

    def run_reference_calibration(self) -> None:
        """Collect and persist five adaptive readings with a known-good emitter."""
        if self.busy or self.measuring or self.reference_calibrating or self.simulator_mode():
            return

        # Starting a recalibration means the old emitter baseline must no longer
        # authorize tests. Keep its audit values, but invalidate it immediately.
        if self.reference_calibration is not None and self.reference_calibration.valid:
            self.reference_calibration = self.reference_calibration.invalidated(
                "Reference-unit recalibration was started; testing remains locked until it completes."
            )
            try:
                save_reference_calibration(self.reference_calibration)
            except ReferenceCalibrationError as exc:
                self.reference_calibration_error = str(exc)

        self.busy = True
        self.reference_calibrating = True
        self.reference_progress_var.set(
            "Starting the emitter and watching reference peak stability…"
        )
        self.status_var.set("Reference-unit calibration: watching peak stability…")
        self.measure_token += 1
        token = self.measure_token
        self.render_step()

        def push(callback) -> None:
            self._post(callback)

        def worker() -> None:
            try:
                calibration = self._hardware_reference_calibration(token, push)
            except Exception as exc:  # noqa: BLE001 - shown in the calibration card/dialog
                push(lambda exc=exc: self.on_reference_calibration_error(token, exc))
            else:
                push(lambda: self.on_reference_calibration_done(token, calibration))

        threading.Thread(target=worker, daemon=True).start()

    def _hardware_reference_calibration(self, token: int, push) -> ReferenceCalibration:
        readings_mv: list[float] = []
        with self.hardware_lock:
            self.ensure_connected()
            device = self.device
            device.disable_emitter_pwm(EMITTER_PWM_CHANNEL)
            battery_v = device.read_battery_voltage()
            push(lambda v=battery_v: self.on_battery_update(v))
            if battery_state_for(battery_v) == "fault":
                raise HardwareNotReadyError(
                    f"The battery input reads {battery_v:.2f} V, which is not a valid 6 V SLA level. "
                    "Check the battery clip and ADS AIN7 divider before calibrating the reference unit."
                )
            if battery_v <= BATTERY_MIN_V:
                raise BatteryTooLowError(battery_v)
            try:
                activation_time = device.configure_emitter_pwm(
                    channel=EMITTER_PWM_CHANNEL,
                    frequency_hz=EMITTER_PWM_FREQUENCY_HZ,
                    duty_cycle_percent=EMITTER_PWM_DUTY_CYCLE,
                )
                first_reading_start = (
                    float(activation_time)
                    if isinstance(activation_time, (int, float))
                    else time.monotonic()
                )
                for reading_number in range(1, REFERENCE_CALIBRATION_READINGS + 1):
                    reading_start = (
                        first_reading_start if reading_number == 1 else time.monotonic()
                    )
                    reading_mv = self._capture_reference_reading(
                        device,
                        pwm_started_monotonic=reading_start,
                        token=token,
                        push=push,
                        status_prefix=(
                            f"Reference calibration {reading_number}/"
                            f"{REFERENCE_CALIBRATION_READINGS}"
                        ),
                        calibration_ui=True,
                    )
                    readings_mv.append(reading_mv)
                    progress_text = (
                        f"Reference reading {reading_number}/{REFERENCE_CALIBRATION_READINGS} complete"
                    )
                    push(
                        lambda value=progress_text: self.reference_progress_var.set(value)
                    )
                    push(lambda value=progress_text: self.status_var.set(value))
            finally:
                device.disable_emitter_pwm(EMITTER_PWM_CHANNEL)

        calibration = build_reference_calibration(readings_mv)
        save_reference_calibration(calibration)
        return calibration

    def on_reference_calibration_done(
        self, token: int, calibration: ReferenceCalibration
    ) -> None:
        if token != self.measure_token:
            return
        self.busy = False
        self.reference_calibrating = False
        self.reference_calibration = calibration
        self.reference_calibration_error = None
        self.reference_progress_var.set("")
        self.status_var.set("Reference unit calibrated and ready.")
        self.render_step()
        messagebox.showinfo(
            "Reference calibration complete",
            f"Saved {len(calibration.readings_mv)} stable reference readings.\n\n"
            "The reference unit will be checked automatically before every sensor test.",
        )

    def on_reference_calibration_error(self, token: int, exc: Exception) -> None:
        if token != self.measure_token:
            return
        self.busy = False
        self.reference_calibrating = False
        self.reference_progress_var.set("")
        text = self._friendly_hardware_error(str(exc))
        self.reference_calibration_error = text
        if self.reference_calibration is not None and not self.reference_calibration.valid:
            self.reference_calibration = self.reference_calibration.invalidated(
                "Reference-unit recalibration failed: " + text
            )
            try:
                save_reference_calibration(self.reference_calibration)
            except ReferenceCalibrationError:
                pass
        self.status_var.set("Reference calibration failed — sensor testing remains locked. " + text)
        self.render_step()
        messagebox.showerror("Reference calibration failed", text)

    # ----- measurement ----- #
    def run_measurement(self, _event: tk.Event | None = None) -> None:
        if self.busy or self.measuring:
            return
        specimen_id = self.specimen_id_var.get().strip()
        if not specimen_id:
            self.step = self.LOAD_STEP
            self.render_step()
            messagebox.showerror(
                "Physical specimen ID needed",
                "Enter the permanent ID written on this physical sensor before measuring. "
                "The ID is required to join repeat runs without mixing sensors.",
            )
            return
        if self.last_result is not None and not self.result_saved:
            messagebox.showinfo(
                "Save this run before repeating",
                "Calibration statistics count every acquisition. Complete this run's "
                "ground-truth review, then choose Save + Repeat Same Specimen so an "
                "earlier wrong verdict cannot be hidden by a later measurement.",
            )
            return
        simulator = self.simulator_mode()
        if self.stability_config_error is not None or self.stability_settings is None:
            text = (
                "Measurement is disabled because the tracked v6.1 stability settings could not be loaded.\n\n"
                + (self.stability_config_error or "Unknown stability configuration error.")
            )
            self.status_var.set(text.replace("\n", " "))
            messagebox.showerror("Fix stability settings", text)
            return
        if not simulator and not self.reference_gate_ready():
            calibration = self.reference_calibration
            if calibration is not None and calibration.invalidation_reason:
                detail = calibration.invalidation_reason
            else:
                detail = self.reference_calibration_error or "No valid reference calibration is saved."
            self.step = self.LOAD_STEP
            self.status_var.set("Reference calibration required — the sensor was not read.")
            self.render_step()
            messagebox.showwarning(
                "Calibrate the reference unit before testing",
                detail + "\n\nReplace/check the emitter, then press “Calibrate reference unit” before testing a sensor.",
            )
            return
        # Hard block: refuse to start a test on a known-low battery or a
        # wiring fault. The tech must fix it and re-check before testing.
        if self.battery_state in ("low", "fault"):
            volts = "" if self.battery_v is None else f" ({self.battery_v:.2f} V)"
            if self.battery_state == "fault":
                self.status_var.set(f"Battery reading is not valid{volts}. Check the battery clip and ADS AIN7 divider, then press “Re-check battery”.")
            else:
                self.status_var.set(f"Battery too low{volts}. Recharge the 6 V SLA and press “Re-check battery”.")
            self.refresh_battery()
            return

        waveform_range_v = WAVEFORM_INPUT_RANGE_V
        pwm_channel = EMITTER_PWM_CHANNEL
        pwm_hz = EMITTER_PWM_FREQUENCY_HZ
        pwm_duty = EMITTER_PWM_DUTY_CYCLE
        sim_case = self.sim_case_var.get()
        sim_low_battery = self.sim_low_battery_var.get()
        filter_setup = self.filter_setup
        show_live = self.show_live_var.get()
        offset_override = bool(self.sensor_seated_override_var.get())

        self.measuring = True
        self.busy = True
        self.measurement_run_number += 1
        self.active_run_id = str(uuid.uuid4())
        self.measured_specimen_id = specimen_id
        self.last_metrics = None
        self.last_result = None
        self.last_capture_report = None
        self.last_reference_check_mv = None
        self.last_reference_calibration = None
        self.last_reference_capture = None
        self.last_dut_raw_samples = ()
        self.last_dut_stream_diagnostics = None
        self.last_rig_metadata = {}
        self.last_run_evidence_paths = []
        self.last_run_manifest_path = None
        self.last_run_evidence_error = None
        self.auto_suggested_failure_mode = ""
        self.offset_plausibility_override_applied = False
        self.show_details_var.set(False)
        self.stability_diagnostics_saved = False
        self.measure_status_var.set("Checking reference unit before reading the sensor…")
        self.measure_token += 1
        token = self.measure_token
        self.render_step()

        def push(callback) -> None:
            self._post(callback)

        def worker() -> None:
            try:
                if simulator:
                    metrics, final, offset_v = self._simulate_measurement(filter_setup, sim_case, sim_low_battery, waveform_range_v, show_live, token, push)
                else:
                    metrics, final, offset_v = self._hardware_measurement(
                        filter_setup,
                        waveform_range_v,
                        pwm_channel,
                        pwm_hz,
                        pwm_duty,
                        show_live,
                        token,
                        push,
                        offset_override,
                    )
            except BatteryTooLowError as exc:
                push(lambda exc=exc: self.on_battery_block(token, exc.battery_v))
            except ReferenceGateError as exc:
                push(lambda exc=exc: self.on_reference_block(token, exc))
            except HardwareNotReadyError as exc:
                push(lambda exc=exc: self.on_hardware_not_ready(token, exc))
            except Exception as exc:
                push(lambda exc=exc: self.on_measure_error(token, exc))
            else:
                push(lambda: self.on_measure_done(token, metrics, final))

        threading.Thread(target=worker, daemon=True).start()

    def _fresh_battery_reading(self) -> float | None:
        """Reuse the load-step battery check when it is healthy and recent,
        so the measurement does not spend ~0.3 s re-reading a slow-moving DC
        value. Warn/low readings are never reused - those always re-check."""
        if (
            self.battery_v is not None
            and self.battery_state == "ok"
            and not self.battery_read_is_synthetic
            and self.battery_read_time is not None
            and (time.monotonic() - self.battery_read_time) <= BATTERY_REUSE_WINDOW_S
        ):
            return self.battery_v
        return None

    def _hardware_measurement(
        self,
        filter_setup,
        waveform_range_v,
        pwm_channel,
        pwm_hz,
        pwm_duty,
        show_live,
        token,
        push,
        offset_plausibility_override=False,
    ):
        # Preview samples always flow from the production stream; this initial
        # UI state must never select a different acquisition path.
        del show_live
        settings = self.stability_settings
        if settings is None:
            raise StabilitySettingsError("V6.1 stability settings are unavailable.")
        calibration = self.reference_calibration
        if calibration is None or not calibration.valid:
            raise ReferenceGateError(
                "The reference unit has no valid calibration. The sensor was not read. "
                "Replace/check the emitter and recalibrate the reference unit before testing."
            )
        self.last_reference_calibration = calibration
        with self.hardware_lock:
            self.ensure_connected()
            device = self.device
            identity = getattr(device, "identity", None)
            self.last_rig_metadata = {
                "port": getattr(device, "port_name", None),
                "firmware_identity": getattr(identity, "text", None),
                "firmware_version": getattr(identity, "version_text", None),
                "firmware_ready_banner_seen": getattr(
                    identity, "ready_banner_seen", None
                ),
            }
            device.disable_emitter_pwm(pwm_channel)
            # Check the 6 V SLA before doing anything else: if it is too low,
            # bail out before measuring so no unreliable reading is recorded.
            battery_v = self._fresh_battery_reading()
            if battery_v is None:
                battery_v = device.read_battery_voltage()
                push(lambda v=battery_v: self.on_battery_update(v))
            if battery_state_for(battery_v) == "fault":
                raise HardwareNotReadyError(
                    f"The battery input reads {battery_v:.2f} V, which is not a valid 6 V SLA level. "
                    "The battery or the ADS AIN7 100k/100k divider is probably not connected - "
                    "check the battery clip and the rig wiring, then press Re-check battery."
                )
            if battery_v <= BATTERY_MIN_V:
                raise BatteryTooLowError(battery_v)

            # The reference gate is deliberately first: start the emitter and
            # immediately stream the fixed reference unit. The same 0.1 mV / 5
            # consecutive-peak stability rule as AIN0 selects five fresh cycles;
            # there is no fixed warm-up delay.
            push(lambda: self.set_measure_status(
                token,
                "Checking reference unit first — watching peak stability…",
            ))
            try:
                reference_activation_time = device.configure_emitter_pwm(
                    channel=pwm_channel,
                    frequency_hz=pwm_hz,
                    duty_cycle_percent=pwm_duty,
                )
                reference_started_monotonic = (
                    float(reference_activation_time)
                    if isinstance(reference_activation_time, (int, float))
                    else time.monotonic()
                )
                try:
                    reference_check_mv = self._capture_reference_reading(
                        device,
                        pwm_started_monotonic=reference_started_monotonic,
                        token=token,
                        push=push,
                        status_prefix="Reference unit",
                    )
                except ReferenceCaptureError as exc:
                    reason = (
                        f"Reference unit could not establish a stable five-cycle reading: {exc} "
                        "No AIN0 reading was taken. Replace/check the emitter, then recalibrate "
                        "the reference unit before testing."
                    )
                    invalidated = calibration.invalidated(reason)
                    self.reference_calibration = invalidated
                    try:
                        save_reference_calibration(invalidated)
                    except ReferenceCalibrationError as save_exc:
                        self.reference_calibration_error = str(save_exc)
                    raise ReferenceGateError(reason) from exc
            finally:
                device.disable_emitter_pwm(pwm_channel)

            if not calibration.accepts(reference_check_mv):
                failure = ReferenceCheckFailedError(reference_check_mv, calibration)
                invalidated = calibration.invalidated(str(failure), reference_check_mv)
                self.reference_calibration = invalidated
                self.last_reference_check_mv = reference_check_mv
                try:
                    save_reference_calibration(invalidated)
                except ReferenceCalibrationError as exc:
                    self.reference_calibration_error = str(exc)
                raise failure

            self.last_reference_check_mv = reference_check_mv
            reference_drift = calibration.drift_percent(reference_check_mv)
            push(lambda value=reference_check_mv, drift=reference_drift: self.set_measure_status(
                token,
                "Reference unit passed. Reading sensor offset…",
            ))

            offset_v = device.read_offset_voltage(waveform_range_v=waveform_range_v)
            # Pre-flight: a connected 406MCA presents its ~0.3-1.2 V DC offset
            # through the buffer. A floating/railed AIN0 means no sensor (or no
            # buffer) - abort before capturing anything.
            if not math.isfinite(offset_v):
                raise HardwareNotReadyError(
                    "AIN0 returned a non-finite offset. Check the buffer and ADC wiring; "
                    "the calibration override cannot accept an invalid numeric reading."
                )
            if not (
                SENSOR_OFFSET_MIN_PLAUSIBLE_V
                <= offset_v
                <= SENSOR_OFFSET_MAX_PLAUSIBLE_V
            ):
                if not offset_plausibility_override:
                    raise HardwareNotReadyError(
                        f"No sensor detected: AIN0 reads {offset_v:.3f} V DC, but a connected 406MCA "
                        "sits near 0.3-1.2 V. Seat the sensor in the rig and check the buffer wiring, "
                        "then press Measure again. For a known no-offset failure in this calibration "
                        "build, physically confirm it is seated and enable the diagnostic override."
                    )
                self.offset_plausibility_override_applied = True
                push(lambda v=offset_v: self.set_measure_status(
                    token,
                    f"Diagnostic override recorded for implausible offset {v:.3f} V. Capturing DUT…",
                ))
            push(lambda v=offset_v: self.on_offset_update(token, v))

            emitter_on_time: float | None = None
            emitter_off_time: float | None = None
            try:
                # The command may reach the ESP32 before its acknowledgement
                # fails, so PWM shutdown must cover activation as well as the
                # subsequent stream capture.
                activation_time = device.configure_emitter_pwm(
                    channel=pwm_channel,
                    frequency_hz=pwm_hz,
                    duty_cycle_percent=pwm_duty,
                )
                emitter_on_time = (
                    float(activation_time)
                    if isinstance(activation_time, (int, float))
                    else time.monotonic()
                )
                push(lambda: self.set_measure_status(
                    token,
                    f"Emitter PWM on. Attempt 1/{MAX_MEASUREMENT_ATTEMPTS}: "
                    f"stabilizing peak (0/{DUT_STABILITY_CONFIRMATION_DELTAS})...",
                ))

                def progress(current: StabilityAnalysis) -> None:
                    current_report = current.report
                    if current_report.stabilized:
                        text = (
                            f"Attempt {current_report.measurement_attempt}/{MAX_MEASUREMENT_ATTEMPTS}: "
                            f"stable at {current_report.stabilization_elapsed_s:.1f} s. "
                            f"Measuring sensitivity cycle {current_report.measurement_cycle_count}/"
                            f"{current_report.measurement_cycles_required}..."
                        )
                    else:
                        latest = current.cycles[-1] if current.cycles else None
                        delta_text = (
                            "waiting for first two peaks"
                            if latest is None or latest.absolute_peak_delta_mv is None
                            else f"peak Δ {latest.absolute_peak_delta_mv:.3f} mV"
                        )
                        confirmation = current_report.active_confirmation_run_length
                        text = (
                            f"Attempt {current_report.measurement_attempt}/{MAX_MEASUREMENT_ATTEMPTS}: "
                            f"stabilizing... {min(current_report.total_pwm_on_seconds, STABILITY_TIMEOUT_S):.1f}/"
                            f"{STABILITY_TIMEOUT_S:.1f} s · {delta_text} · "
                            f"{min(confirmation, current_report.active_confirmation_count)}/"
                            f"{current_report.active_confirmation_count} stable"
                        )
                    push(lambda value=text: self.set_measure_status(token, value))

                def preview(waveform_preview: np.ndarray, sync_preview: np.ndarray) -> None:
                    # Keep the rolling preview current even while hidden so a
                    # technician can turn the live scope on mid-measurement.
                    push(lambda wf=waveform_preview, sy=sync_preview: self.on_preview_frame(token, wf, sy))

                waveform, sync, actual_rate, stability_analysis = device.read_waveform_until_stable(
                    waveform_range_v=waveform_range_v,
                    settings=settings,
                    pwm_started_monotonic=emitter_on_time,
                    sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
                    expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
                    progress=progress,
                    preview=preview,
                    cancelled=lambda: token != self.measure_token,
                )
                self.last_dut_raw_samples = tuple(
                    getattr(device, "last_stream_samples", ())
                )
                self.last_dut_stream_diagnostics = getattr(
                    device, "last_stream_diagnostics", None
                )
            finally:
                deactivation_time = device.disable_emitter_pwm(pwm_channel)
                emitter_off_time = (
                    float(deactivation_time)
                    if isinstance(deactivation_time, (int, float))
                    else time.monotonic()
                )

        if stability_analysis.report.measurement_complete:
            metrics = analyze_v6_stable_measurement(
                waveform,
                sync,
                actual_rate,
                stability_analysis,
                offset_v=offset_v,
                input_range_v=waveform_range_v,
            )
            final = evaluate_result(offset_v, metrics, filter_setup)
            final = apply_signal_quality_gate(final, metrics)
        elif stability_analysis.report.unstable:
            metrics, final = build_stability_timeout_result(
                waveform,
                sync,
                actual_rate,
                stability_analysis,
                offset_v=offset_v,
                input_range_v=waveform_range_v,
            )
        else:
            raise Esp32BackendError("Adaptive capture ended without a complete result.")
        host_pwm_on_seconds = None
        if emitter_on_time is not None and emitter_off_time is not None:
            host_pwm_on_seconds = emitter_off_time - emitter_on_time
        report = StabilityCaptureReport.from_analysis(
            stability_analysis,
            data_source="esp32_v6_1_failure_calibration",
            pwm_on_seconds=host_pwm_on_seconds,
        )
        self.last_capture_report = report
        return metrics, final, offset_v

    def _simulate_measurement(self, filter_setup, sim_case, sim_low_battery, waveform_range_v, show_live, token, push):
        # Mirror the hardware battery gate so the lockout is testable without the ESP32.
        self.last_rig_metadata = {
            "port": None,
            "firmware_identity": None,
            "simulator_case": sim_case,
        }
        battery_v = SIM_BATTERY_LOW_V if sim_low_battery else SIM_BATTERY_OK_V
        push(lambda v=battery_v: self.on_battery_update(v))
        if battery_v <= BATTERY_MIN_V:
            raise BatteryTooLowError(battery_v)
        settings = self.stability_settings
        if settings is None:
            raise StabilitySettingsError("V6.1 stability settings are unavailable.")
        # Synthetic runs must never cite or appear to validate the real AIN1
        # baseline. Simulator status and rig metadata identify this fake gate.
        self.last_reference_check_mv = None
        self.last_reference_calibration = None
        push(lambda: self.set_measure_status(
            token,
            "Reference unit passed (simulated). Reading sensor offset…",
        ))
        waveform, sync, actual_rate, offset_v = simulate_v6_startup_capture(
            filter_setup,
            sim_case,
        )
        push(lambda v=offset_v: self.on_offset_update(token, v))
        push(lambda: self.set_measure_status(
            token,
            "Emitter PWM on (simulated). Evaluating startup peak drift...",
        ))
        dut_settings = dut_stability_settings(settings)
        full_analysis = analyze_stability(
            waveform,
            sync,
            actual_rate,
            dut_settings,
            stability_deadline_s=STABILITY_TIMEOUT_S,
            measurement_cycles_required=SENSITIVITY_MEASUREMENT_CYCLES,
            enforce_measurement_stability=True,
            max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
            data_source="simulator",
        )
        if full_analysis.report.measurement_complete:
            cut_sample = min(
                len(waveform),
                full_analysis.measurement_cycles[-1].end_index + 1,
            )
        else:
            cut_sample = min(
                len(waveform),
                int(math.ceil(STABILITY_TIMEOUT_S * actual_rate)) + 1,
            )
        waveform = waveform[:cut_sample]
        sync = sync[:cut_sample]
        stability_analysis = analyze_stability(
            waveform,
            sync,
            actual_rate,
            dut_settings,
            stability_deadline_s=STABILITY_TIMEOUT_S,
            measurement_cycles_required=SENSITIVITY_MEASUREMENT_CYCLES,
            enforce_measurement_stability=True,
            max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
            data_source="simulator",
        )
        if show_live:
            wf = waveform[-STREAM_PREVIEW_MAX_SAMPLES:].copy()
            sy = sync[-STREAM_PREVIEW_MAX_SAMPLES:].copy()
            push(lambda: self.on_preview_frame(token, wf, sy))
        if stability_analysis.report.measurement_complete:
            push(lambda: self.set_measure_status(
                token,
                f"Stable at {stability_analysis.report.stabilization_elapsed_s:.1f} s. "
                f"Measured {stability_analysis.report.measurement_cycles_required} "
                f"sensitivity cycles on attempt {stability_analysis.report.measurement_attempt}.",
            ))
            metrics = analyze_v6_stable_measurement(
                waveform,
                sync,
                actual_rate,
                stability_analysis,
                offset_v=offset_v,
                input_range_v=waveform_range_v,
            )
            final = evaluate_result(offset_v, metrics, filter_setup)
            # The SNR gate is intentionally NOT applied here: the simulator
            # is a training model rather than calibrated hardware noise.
        elif stability_analysis.report.unstable:
            metrics, final = build_stability_timeout_result(
                waveform,
                sync,
                actual_rate,
                stability_analysis,
                offset_v=offset_v,
                input_range_v=waveform_range_v,
            )
        else:
            raise RuntimeError("Simulator capture did not reach a complete v6.1 decision.")
        report = StabilityCaptureReport.from_analysis(stability_analysis, data_source="simulator")
        self.last_capture_report = report
        return metrics, final, offset_v

    def set_measure_status(self, token: int, text: str) -> None:
        if token == self.measure_token:
            self.measure_status_var.set(text)

    def on_offset_update(self, token: int, offset_v: float) -> None:
        if token != self.measure_token:
            return
        self.measure_status_var.set(f"DC offset: {offset_v:.3f} V. Driving emitter...")

    def on_preview_frame(self, token: int, waveform: np.ndarray, sync: np.ndarray) -> None:
        if token != self.measure_token:
            return
        self.preview_waveform = waveform
        self.preview_sync = sync
        self.redraw_waveform()

    def _ensure_current_run_evidence(self) -> bool:
        if self.last_run_manifest_path is not None and self.last_run_manifest_path.exists():
            try:
                validate_calibration_run_manifest(
                    self.last_run_manifest_path,
                    expected_run_id=self.active_run_id,
                    expected_specimen_id=self.measured_specimen_id,
                    expected_app_verdict=(
                        ""
                        if self.last_result is None
                        else ("PASS" if self.last_result.passed else "FAIL")
                    ),
                )
            except ValueError as exc:
                self.last_run_evidence_error = str(exc)
                return False
            self.last_run_evidence_error = None
            return True
        if self.last_metrics is None or self.last_result is None:
            self.last_run_evidence_error = "No completed waveform is available to preserve."
            return False
        if not self.active_run_id:
            self.active_run_id = str(uuid.uuid4())
        try:
            paths, manifest_path = save_calibration_run_evidence(
                batch_number=self.batch_number,
                sensor_id=self.current_sensor_id,
                specimen_id=self.measured_specimen_id,
                tester_name=self.tester_name,
                filter_setup=self.filter_setup,
                session_id=self.session_id,
                run_id=self.active_run_id,
                measurement_run_number=max(1, self.measurement_run_number),
                is_synthetic=self.batch_is_synthetic,
                metrics=self.last_metrics,
                final_result=self.last_result,
                report=self.last_capture_report,
                battery_v=self.battery_v,
                reference_calibration=(
                    None if self.batch_is_synthetic else self.last_reference_calibration
                ),
                reference_check_mv=(
                    None if self.batch_is_synthetic else self.last_reference_check_mv
                ),
                dut_raw_samples=self.last_dut_raw_samples,
                dut_stream_diagnostics=self.last_dut_stream_diagnostics,
                reference_capture=(
                    None if self.batch_is_synthetic else self.last_reference_capture
                ),
                offset_plausibility_override=self.offset_plausibility_override_applied,
                rig_metadata=self.last_rig_metadata,
            )
        except Exception as exc:
            self.last_run_evidence_paths = []
            self.last_run_manifest_path = None
            self.last_run_evidence_error = str(exc)
            return False
        self.last_run_evidence_paths = paths
        self.last_run_manifest_path = manifest_path
        self.last_run_evidence_error = None
        self.stability_diagnostics_saved = True
        if not any(item.get("run_id") == self.active_run_id for item in self.run_history):
            self.run_history.append(
                {
                    "run_id": self.active_run_id,
                    "measurement_run_number": max(1, self.measurement_run_number),
                    "manifest_path": str(manifest_path),
                    "evidence_paths": [str(path) for path in paths],
                    "app_verdict": "PASS" if self.last_result.passed else "FAIL",
                    "app_suggested_failure_mode": suggest_failure_mode(self.last_result),
                }
            )
        return True

    def on_measure_done(self, token: int, metrics: WaveformMetrics, final: FinalResult) -> None:
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = True
        self.last_metrics = metrics
        self.last_result = final
        self.auto_suggested_failure_mode = suggest_failure_mode(final)
        self.failure_mode_var.set(self.auto_suggested_failure_mode)
        self.preview_waveform = metrics.waveform_v
        self.preview_sync = metrics.sync_v
        verdict = "PASS" if final.passed else "FAIL"
        self.evidence_saving = True
        self.status_var.set(
            f"{self.current_sensor_id}: {verdict}. Preserving the run evidence…"
        )
        self.render_step()

        def evidence_worker() -> None:
            evidence_saved = self._ensure_current_run_evidence()
            self.evidence_saving = False
            self._post(
                lambda: self.on_run_evidence_saved(
                    token,
                    verdict,
                    evidence_saved,
                )
            )

        threading.Thread(target=evidence_worker, daemon=True).start()

    def on_run_evidence_saved(
        self,
        token: int,
        verdict: str,
        evidence_saved: bool,
    ) -> None:
        if token != self.measure_token:
            return
        self.busy = False
        if evidence_saved:
            self.status_var.set(
                f"{self.current_sensor_id}: {verdict}. Complete the required ground-truth review."
            )
        else:
            self.status_var.set(
                f"{self.current_sensor_id}: {verdict}. Evidence save failed; review it and retry Save."
            )
        self.write_autosave("measurement_complete")
        self.render_step()
        if not evidence_saved:
            messagebox.showerror(
                "Calibration evidence was not saved",
                (self.last_run_evidence_error or "Unknown evidence error")
                + "\n\nThe verdict is still visible, but Save is blocked until the evidence bundle succeeds.",
            )

    def on_battery_block(self, token: int, battery_v: float) -> None:
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        self.battery_v = battery_v
        self.battery_state = "low"
        self._refresh_battery_pill()
        self.status_var.set(f"Battery too low ({battery_v:.2f} V). Recharge the 6 V SLA to continue.")
        self.measure_status_var.set("")
        self.render_step()
        messagebox.showwarning(
            "Recharge the battery",
            f"The 6 V SLA is at {battery_v:.2f} V, at or below the {BATTERY_MIN_V:.1f} V minimum.\n\n"
            "Recharge it, then press “Re-check battery” before testing again.",
        )

    def on_reference_block(self, token: int, exc: ReferenceGateError) -> None:
        """Return to the load step and keep DUT testing locked after AIN1 fails."""
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        self.step = self.LOAD_STEP
        self.status_var.set("Reference-unit lockout — the sensor was not read.")
        self.measure_status_var.set("")
        self.render_step()
        messagebox.showwarning("Reference unit blocked the sensor test", str(exc))

    def on_hardware_not_ready(self, token: int, exc: HardwareNotReadyError) -> None:
        """A pre-flight check failed: nothing was measured or recorded."""
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        self.status_var.set("Rig not ready - nothing was recorded. Check the wiring and measure again.")
        self.measure_status_var.set("")
        self.render_step()
        messagebox.showwarning("Plug everything in first", str(exc))

    @staticmethod
    def _friendly_hardware_error(text: str) -> str:
        upper = text.upper()
        if "PERMISSION" in upper or "ACCESS DENIED" in upper:
            return ("The ESP32 serial port is not accessible. Add this user to the dialout group, "
                    "log out and back in, then retry.")
        if "BUSY" in upper or "CLAIMED" in upper or "EXCLUSIV" in upper:
            return ("The ESP32 serial port is already in use. Close Arduino Serial Monitor, "
                    "live_waveform.py, and other serial tools, then retry.")
        if ("NOT_FOUND" in upper or "NOT FOUND" in upper or "NO ESP32" in upper
                or "NO SERIAL" in upper or "DISCONNECTED" in upper):
            return ("No Eltec ESP32 rig detected. Plug in its USB cable and rig power, then try again. "
                    "(For training without hardware, turn on Simulator mode under Advanced options.)")
        return text

    def on_measure_error(self, token: int, exc: Exception) -> None:
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        text = self._friendly_hardware_error(str(exc))
        self.status_var.set(text)
        self.measure_status_var.set("")
        self.render_step()
        messagebox.showerror("Measurement problem", text)

    def toggle_live_view(self) -> None:
        if self.measuring or self.step == self.RESULT_STEP:
            self.render_step()

    def toggle_result_details(self) -> None:
        if self.step == self.RESULT_STEP and not self.measuring:
            self.render_step()

    def redraw_waveform(self) -> None:
        if self.wave_canvas is not None and self.wave_canvas.winfo_exists():
            self.wave_canvas.set_data(self.preview_waveform, self.preview_sync)

    # ----- comment / snapshot ----- #
    def open_comment_window(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(f"Comment for {self.current_sensor_id}")
        dialog.minsize(S(620), S(420))
        dialog.configure(bg=PAGE_BG)
        dialog.transient(self)

        frame = tk.Frame(dialog, bg=PAGE_BG)
        frame.grid(row=0, column=0, sticky="nsew", padx=18, pady=16)
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text="COMMENT —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(frame, text=f"Sensor {self.current_sensor_id}", bg=PAGE_BG, fg=TEXT_DARK, font=self.fd(19)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))
        text = tk.Text(frame, wrap="word", font=self.fb(13), undo=True, relief="flat", bd=0,
                       bg=CARD_BG, fg=TEXT_DARK, padx=12, pady=10, insertbackground=ELTEC_BLUE,
                       highlightbackground=CARD_BORDER, highlightcolor=ELTEC_BLUE, highlightthickness=1)
        text.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", self.notes_var.get())

        buttons = tk.Frame(frame, bg=PAGE_BG)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))

        def save_comment(_event: tk.Event | None = None) -> str:
            self.notes_var.set(text.get("1.0", "end-1c").strip())
            self.update_comment_snapshot_status()
            self.write_autosave("comment_updated")
            dialog.destroy()
            return "break"

        def newline(_event: tk.Event | None = None) -> str:
            text.insert("insert", "\n")
            return "break"

        dialog.bind("<Return>", save_comment)
        dialog.bind("<KP_Enter>", save_comment)
        text.bind("<Return>", save_comment)
        text.bind("<Shift-Return>", newline)
        self.btn(buttons, "Cancel", dialog.destroy, kind="ghost", size="sm").grid(row=0, column=0, padx=(0, 10))
        self.btn(buttons, "Save Comment (Enter)", save_comment, kind="primary", size="sm").grid(row=0, column=1)
        text.focus_set()

    def update_comment_snapshot_status(self) -> None:
        comment = self.notes_var.get().strip()
        self.comment_status_var.set(f"Comment saved ({len(comment)} chars)" if comment else "")
        image_count = sum(path.suffix.lower() == ".png" for path in self.snapshot_paths)
        csv_count = sum(path.suffix.lower() == ".csv" for path in self.snapshot_paths)
        if image_count == 0:
            self.snapshot_status_var.set("")
        elif image_count == 1:
            suffix = " + stability CSV diagnostics" if csv_count else ""
            self.snapshot_status_var.set("1 waveform snapshot saved" + suffix)
        else:
            suffix = " + stability CSV diagnostics" if csv_count else ""
            self.snapshot_status_var.set(
                f"{image_count} waveform snapshots saved" + suffix
            )

    def capture_waveform_snapshot(self) -> None:
        if self.last_metrics is None:
            messagebox.showinfo("No waveform yet", "Run the measurement before capturing a waveform.")
            return
        try:
            snapshot_paths = save_waveform_diagnostic_bundle(
                self.batch_number,
                self.current_sensor_id,
                self.last_metrics,
                self.last_capture_report,
                title=f"{MODEL_NAME} {self.current_sensor_id} waveform snapshot",
                detail_lines=snapshot_detail_lines(
                    self.batch_number,
                    self.current_sensor_id,
                    self.last_metrics,
                    self.notes_var.get(),
                    self.last_capture_report,
                ),
                filename_suffix="snapshot",
                output_dir=(
                    study_results_dir(is_synthetic=self.batch_is_synthetic)
                    / "waveform_snapshots"
                    / f"lot_{safe_filename_part(self.batch_number)}"
                ),
                specimen_id=self.measured_specimen_id,
                run_id=self.active_run_id,
                raw_samples=self.last_dut_raw_samples,
            )
        except Exception as exc:
            messagebox.showerror("Waveform snapshot problem", str(exc))
            return
        if not snapshot_paths:
            messagebox.showinfo("No waveform", "No waveform samples were available to capture.")
            return
        self.snapshot_paths.extend(snapshot_paths)
        self.stability_diagnostics_saved = self.last_capture_report is not None
        self.update_comment_snapshot_status()
        self.write_autosave("waveform_snapshot_saved")
        self.status_var.set(f"Saved waveform snapshot: {snapshot_paths[0]}")

    # ----- autosave ----- #
    @staticmethod
    def _csv_contains_run_id(csv_path: Path, run_id: str) -> bool:
        if not run_id or not csv_path.exists():
            return False
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
                return any(
                    (row.get("run_id") or "").strip() == run_id
                    for row in csv.DictReader(csv_file)
                )
        except (OSError, csv.Error):
            return False

    def recover_pending_autosave(self, csv_path: Path) -> bool:
        """Recover a crash-interrupted reviewed run; never silently overwrite it.

        Return True when batch startup has been handled (recovered or blocked).
        """
        autosave_path = batch_autosave_path(
            self.batch_number,
            is_synthetic=self.batch_is_synthetic,
        )
        if not autosave_path.exists():
            return False
        try:
            autosave = json.loads(autosave_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.batch_active = False
            messagebox.showerror(
                "Pending calibration recovery needs attention",
                f"The pending autosave cannot be read and will not be overwritten:\n{autosave_path}\n\n{exc}\n\n"
                "Use another batch ID until this file is archived or repaired.",
            )
            return True
        if not isinstance(autosave, dict):
            self.batch_active = False
            messagebox.showerror(
                "Pending calibration recovery needs attention",
                f"The pending autosave is malformed and will not be overwritten:\n{autosave_path}",
            )
            return True
        run_id = str(autosave.get("active_run_id") or "").strip()
        if self._csv_contains_run_id(csv_path, run_id):
            # A crash between CSV append and autosave deletion is harmless: the
            # finalized row is authoritative, so remove only this stale pointer.
            try:
                autosave_path.unlink()
            except OSError:
                pass
            return False
        try:
            autosave_schema = int(autosave.get("calibration_schema_version"))
        except (TypeError, ValueError):
            autosave_schema = -1
        autosave_mode = autosave.get("is_synthetic")
        if (
            autosave_schema != CALIBRATION_SCHEMA_VERSION
            or str(autosave.get("batch_number") or "") != self.batch_number
            or not isinstance(autosave_mode, bool)
            or autosave_mode != self.batch_is_synthetic
            or not run_id
        ):
            self.batch_active = False
            messagebox.showerror(
                "Pending calibration recovery needs attention",
                f"An incompatible pending run exists and will not be overwritten:\n{autosave_path}\n\n"
                "Use another batch ID until the pending data is reviewed.",
            )
            return True

        specimen_id = str(
            autosave.get("measured_specimen_id")
            or autosave.get("specimen_id")
            or ""
        ).strip()
        manifest_text = str(autosave.get("last_run_manifest_path") or "").strip()
        manifest_path = Path(manifest_text) if manifest_text else Path()
        if not manifest_text or not manifest_path.exists():
            manifest_path = (
                calibration_run_dir(
                    self.batch_number,
                    specimen_id,
                    run_id,
                    is_synthetic=self.batch_is_synthetic,
                )
                / "run_manifest.json"
            )
        try:
            manifest, metrics, final, report, captured_reference = (
                recover_calibration_run_from_manifest(manifest_path)
            )
            validate_calibration_run_manifest(
                manifest_path,
                expected_run_id=run_id,
                expected_specimen_id=specimen_id,
                expected_app_verdict="PASS" if final.passed else "FAIL",
            )
        except (OSError, ValueError, TypeError) as exc:
            self.batch_active = False
            messagebox.showerror(
                "Pending calibration recovery needs attention",
                f"The prior run cannot be safely reconstructed and will not be overwritten:\n"
                f"{autosave_path}\n\n{exc}\n\nUse another batch ID until the evidence is reviewed.",
            )
            return True

        try:
            recovered_sensor_number = int(autosave.get("sensor_number"))
            recovered_measurement_number = int(
                autosave.get("measurement_run_number")
                or manifest.get("measurement_run_number")
            )
            if recovered_sensor_number < 1 or recovered_measurement_number < 1:
                raise ValueError("run counters must be positive")
        except (TypeError, ValueError) as exc:
            self.batch_active = False
            messagebox.showerror(
                "Pending calibration recovery needs attention",
                f"The prior autosave has invalid run counters and will not be overwritten:\n"
                f"{autosave_path}\n\n{exc}",
            )
            return True

        if not messagebox.askyesno(
            "Recover unsaved calibration review?",
            f"Batch {self.batch_number} has an unsaved {final.passed and 'PASS' or 'FAIL'} run for "
            f"specimen {specimen_id}. Recover its verdict and technician fields now?\n\n"
            "Choosing No leaves the autosave untouched and blocks this batch from starting, "
            "so it cannot be accidentally overwritten.",
        ):
            self.batch_active = False
            self.status_var.set("Pending calibration run was preserved; recovery is still required.")
            return True

        recovering_reviewer = (
            self.reviewer_var.get().strip()
            or self.tester_var.get().strip()
            or self.tester_name
        )
        self.tester_name = str(autosave.get("tester_name") or self.tester_name).strip()
        self.reviewer_var.set(
            str(autosave.get("reviewer") or "").strip() or recovering_reviewer
        )
        self.filter_setup = str(
            autosave.get("filter_setup") or manifest.get("filter_setup") or self.filter_setup
        )
        self.filter_var.set(self.filter_setup)
        self.current_sensor_number = recovered_sensor_number
        self.current_sensor_id = str(
            autosave.get("sensor_id") or f"{self.batch_number}-{self.current_sensor_number}"
        )
        self.session_id = str(autosave.get("session_id") or self.session_id)
        self.active_run_id = run_id
        self.measurement_run_number = recovered_measurement_number
        self.measured_specimen_id = specimen_id
        self.specimen_id_var.set(specimen_id)
        self.last_metrics = metrics
        self.last_result = final
        self.last_capture_report = report
        self.preview_waveform = metrics.waveform_v
        self.preview_sync = metrics.sync_v
        self.last_reference_check_mv = _finite_float(
            (manifest.get("reference") or {}).get("check_mv")
            if isinstance(manifest.get("reference"), dict)
            else None
        )
        self.last_reference_calibration = captured_reference
        self.battery_v = _finite_float(manifest.get("battery_v"))
        # This is historical audit data, never a fresh preflight reading for a
        # subsequent sensor.
        self.battery_read_time = None
        self.battery_read_is_synthetic = self.batch_is_synthetic
        if self.battery_v is not None:
            self.battery_state = battery_state_for(self.battery_v)
            self._refresh_battery_pill()
        self.last_rig_metadata = (
            dict(manifest.get("rig")) if isinstance(manifest.get("rig"), dict) else {}
        )
        self.auto_suggested_failure_mode = str(
            (manifest.get("app_prediction") or {}).get("suggested_failure_mode") or ""
        )
        self.failure_mode_var.set(
            str(autosave.get("failure_mode") or self.auto_suggested_failure_mode)
        )
        self.ground_truth_verdict_var.set(str(autosave.get("ground_truth_verdict") or ""))
        self.ground_truth_failure_mode_var.set(
            str(autosave.get("ground_truth_failure_mode") or "")
        )
        self.failure_reason_assessment_var.set(
            str(autosave.get("failure_reason_assessment") or "")
        )
        self.ground_truth_basis_var.set(str(autosave.get("ground_truth_basis") or ""))
        self.review_confidence_var.set(str(autosave.get("review_confidence") or ""))
        self.known_root_cause_var.set(
            str(autosave.get("known_physical_root_cause") or "")
        )
        self.review_notes_var.set(str(autosave.get("review_notes") or ""))
        self.notes_var.set(str(autosave.get("comment") or ""))
        self.sensor_seated_override_var.set(
            autosave.get("sensor_seated_override") is True
        )
        self.offset_plausibility_override_applied = (
            autosave.get("offset_plausibility_override_applied") is True
        )
        snapshot_values = autosave.get("waveform_snapshot_paths")
        if not isinstance(snapshot_values, list):
            snapshot_values = []
        self.snapshot_paths = [
            Path(value)
            for value in snapshot_values
            if isinstance(value, str) and value.strip() and Path(value).exists()
        ]
        self.update_comment_snapshot_status()
        evidence_paths = [
            _manifest_artifact_path(manifest_path, artifact)
            for artifact in manifest.get("artifacts", [])
            if isinstance(artifact, dict)
        ]
        self.last_run_evidence_paths = [*evidence_paths, manifest_path]
        self.last_run_manifest_path = manifest_path
        self.last_run_evidence_error = None
        self.stability_diagnostics_saved = True
        self.last_reference_capture = None
        self.last_dut_raw_samples = ()
        self.last_dut_stream_diagnostics = None
        self.run_history = [
            {
                "run_id": run_id,
                "measurement_run_number": self.measurement_run_number,
                "manifest_path": str(manifest_path),
                "evidence_paths": [str(path) for path in self.last_run_evidence_paths],
                "app_verdict": "PASS" if final.passed else "FAIL",
                "app_suggested_failure_mode": self.auto_suggested_failure_mode,
            }
        ]
        self.result_saved = False
        self.evidence_saving = False
        self.busy = False
        self.measuring = False
        self.status_var.set(
            f"Recovered unsaved run {run_id[:8]} for specimen {specimen_id}. Review and save it."
        )
        self.step = self.RESULT_STEP
        self.write_autosave("recovered_after_restart")
        self.render_step()
        return True

    def write_autosave(self, stage: str) -> bool:
        if (
            not self.batch_number
            or not self.current_sensor_id
            or (self.last_result is None and not self.active_run_id)
        ):
            return True
        autosave_path = batch_autosave_path(
            self.batch_number,
            is_synthetic=self.batch_is_synthetic,
        )
        autosave_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "calibration_schema_version": CALIBRATION_SCHEMA_VERSION,
            "calibration_app_version": CALIBRATION_APP_VERSION,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "batch_number": self.batch_number,
            "tester_name": self.tester_name,
            "sensor_number": self.current_sensor_number,
            "sensor_id": self.current_sensor_id,
            "specimen_id": self.specimen_id_var.get(),
            "measured_specimen_id": self.measured_specimen_id,
            "session_id": self.session_id,
            "active_run_id": self.active_run_id,
            "measurement_run_number": self.measurement_run_number,
            "is_synthetic": self.batch_is_synthetic,
            "filter_setup": self.filter_setup,
            "offset_v": None if self.last_result is None else self.last_result.offset_v,
            "sensitivity_mv": None if self.last_result is None else self.last_result.sensitivity_mv,
            "polarity": None if self.last_result is None else self.last_result.polarity,
            "pass_fail": None if self.last_result is None else ("PASS" if self.last_result.passed else "FAIL"),
            "fail_reasons": [] if self.last_result is None else self.last_result.fail_reasons,
            "failure_mode": self.failure_mode_var.get(),
            "app_suggested_failure_mode": self.auto_suggested_failure_mode,
            "ground_truth_verdict": self.ground_truth_verdict_var.get(),
            "ground_truth_failure_mode": self.ground_truth_failure_mode_var.get(),
            "failure_reason_assessment": self.failure_reason_assessment_var.get(),
            "ground_truth_basis": self.ground_truth_basis_var.get(),
            "review_confidence": self.review_confidence_var.get(),
            "reviewer": self.reviewer_var.get(),
            "known_physical_root_cause": self.known_root_cause_var.get(),
            "review_notes": self.review_notes_var.get(),
            "sensor_seated_override": self.sensor_seated_override_var.get(),
            "offset_plausibility_override_applied": self.offset_plausibility_override_applied,
            "battery_v": self.battery_v,
            "reference_calibration_mv": (
                None if self.reference_calibration is None else self.reference_calibration.mean_mv
            ),
            "reference_check_mv": self.last_reference_check_mv,
            "comment": self.notes_var.get(),
            "waveform_snapshot_paths": [str(path) for path in self.snapshot_paths],
            "run_history": self.run_history,
            "last_run_evidence_paths": [
                str(path) for path in self.last_run_evidence_paths
            ],
            "last_run_manifest_path": (
                None
                if self.last_run_manifest_path is None
                else str(self.last_run_manifest_path)
            ),
            "last_run_evidence_error": self.last_run_evidence_error,
        }
        try:
            atomic_write_json(autosave_path, payload)
        except Exception as exc:
            self.autosave_error = str(exc)
            if not self.closing:
                self.status_var.set(
                    "AUTOSAVE FAILED — do not close the app until storage is fixed: "
                    + self.autosave_error
                )
            return False
        self.autosave_error = None
        return True

    def delete_autosave(self) -> None:
        if not self.batch_number:
            return
        try:
            batch_autosave_path(
                self.batch_number,
                is_synthetic=self.batch_is_synthetic,
            ).unlink(missing_ok=True)
        except Exception:
            pass

    # ----- batch summary ----- #
    def show_batch_summary_window(self, batch_number: str, csv_path: Path) -> None:
        summary = tk.Toplevel(self)
        summary.title(f"Batch {batch_number} Summary")
        summary.minsize(S(900), S(500))
        summary.configure(bg=PAGE_BG)

        rows = self._read_summary_rows(csv_path)
        tested = len(rows)
        exact = sum(1 for row in rows if row[-1] == "EXACT_MATCH")
        verdict_mismatches = sum(
            1 for row in rows if row[-1] == "VERDICT_MISMATCH"
        )
        reason_mismatches = sum(
            1
            for row in rows
            if row[-1] in ("FAILURE_REASON_MISMATCH", "PARTIAL_REASON_MATCH")
        )
        inconclusive = tested - exact - verdict_mismatches - reason_mismatches

        head = tk.Frame(summary, bg=PAGE_BG)
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 6))
        tk.Label(head, text="BATCH SUMMARY —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(11, "bold")).pack(anchor="w")
        tk.Label(head, text=f"Batch {batch_number} calibration agreement", bg=PAGE_BG, fg=TEXT_DARK, font=self.fd(24)).pack(anchor="w", pady=(2, 0))
        chips = tk.Frame(head, bg=PAGE_BG)
        chips.pack(anchor="w", pady=(10, 0))
        chip_specs = [
            (f"{tested} TESTED", ELTEC_BLUE_DARK, ELTEC_BLUE_LIGHT),
            (f"{exact} EXACT", PASS_FG, PASS_BG),
            (f"{verdict_mismatches} VERDICT MISMATCH", FAIL_FG, FAIL_BG),
            (f"{reason_mismatches} REASON REVIEW", WARN_FG, WARN_BG),
            (f"{max(0, inconclusive)} INCONCLUSIVE", ELTEC_BLUE_DARK, ELTEC_BLUE_LIGHT),
        ]
        for chip_text, chip_fg, chip_bg in chip_specs:
            tk.Label(chips, text=chip_text, bg=chip_bg, fg=chip_fg, font=self.fm(10, "bold"), padx=12, pady=5).pack(side="left", padx=(0, 8))

        frame = tk.Frame(summary, bg=PAGE_BG)
        frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=(8, 0))
        summary.rowconfigure(1, weight=1)
        summary.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        style = ttk.Style(summary)
        style.configure("Summary.Treeview", font=self.fb(12), rowheight=S(32), background=CARD_BG, fieldbackground=CARD_BG, foreground=TEXT_DARK, borderwidth=0)
        style.configure("Summary.Treeview.Heading", font=self.fb(12, "bold"), background=ELTEC_BLUE_LIGHT, foreground=ELTEC_BLUE_DARK, relief="flat")
        columns = ("specimen", "app", "app_mode", "truth", "truth_mode", "comparison")
        headings = {
            "specimen": "Specimen",
            "app": "App",
            "app_mode": "App mode",
            "truth": "Ground truth",
            "truth_mode": "Actual mode",
            "comparison": "Comparison",
        }
        tree = ttk.Treeview(frame, columns=columns, show="headings", style="Summary.Treeview", height=14)
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=150, anchor="center", stretch=True)
        tree.tag_configure("exact", background=PASS_BG)
        tree.tag_configure("mismatch", background=FAIL_BG)
        tree.tag_configure("review", background=WARN_BG)
        for row in rows:
            if row[-1] == "EXACT_MATCH":
                tag = "exact"
            elif row[-1] in ("VERDICT_MISMATCH", "FAILURE_REASON_MISMATCH"):
                tag = "mismatch"
            else:
                tag = "review"
            tree.insert("", "end", values=row, tags=(tag,))
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=y_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.btn(summary, "Close", summary.destroy, kind="primary", size="md").grid(row=2, column=0, sticky="e", padx=20, pady=14)

    def _read_summary_rows(self, csv_path: Path) -> list[tuple[str, str, str, str, str, str]]:
        rows: list[tuple[str, str, str, str, str, str]] = []
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
                for row in csv.DictReader(csv_file):
                    rows.append(
                        (
                            row.get("specimen_id", "") or row.get("sensor_id", ""),
                            row.get("app_verdict", "") or row.get("pass_fail", ""),
                            row.get("app_suggested_failure_mode_tag", ""),
                            row.get("ground_truth_verdict", ""),
                            row.get("ground_truth_failure_mode_tag", ""),
                            row.get("review_classification", ""),
                        )
                    )
        except Exception:
            rows.append(("Could not read batch CSV", "", "", "", "", "INCONCLUSIVE"))
        return rows

    def _fmt(self, value: str, decimals: int, suffix: str) -> str:
        if not value:
            return ""
        try:
            return f"{float(value):.{decimals}f}{suffix}"
        except ValueError:
            return value

    # ----- hardware lifecycle ----- #
    def ensure_connected(self) -> None:
        if self.device is None:
            self.device = EmitterEsp32Rig()
        self.device.connect()

    def startup_probe(self) -> None:
        if self.stability_config_error is not None:
            self.status_var.set(
                "V6.1 stability configuration error — measurement is disabled. "
                + self.stability_config_error
            )
            return
        ok, message = probe_esp32_status()
        # Simulator mode is an explicit choice (Advanced options) - NEVER
        # auto-enable it. Older builds silently switched to the simulator when
        # found, which let a technician run a "test" against synthetic numbers
        # (with plausible-looking results) without
        # noticing that nothing was plugged in.
        if not ok and not self.simulator_mode():
            message = self._friendly_hardware_error(message)
        self.status_var.set(message)
        if self.simulator_mode():
            self.refresh_battery()

    def on_simulator_toggle(self) -> None:
        """Make entering/leaving simulator mode loud and reset stale readings."""
        requested = bool(self.simulator_var.get())
        if self.batch_active and requested != self.batch_is_synthetic:
            self.simulator_var.set(self.batch_is_synthetic)
            self._redraw_header()
            messagebox.showinfo(
                "Mode is locked for this batch",
                "Hardware versus simulator mode is frozen when a batch starts. "
                "Finish the current run or return to setup before changing it.",
            )
            return
        self.battery_request_generation += 1
        self.battery_checking = False
        self.battery_v = None
        self.battery_state = "unknown"
        self.battery_read_time = None
        self.battery_read_is_synthetic = None
        self._refresh_battery_pill()
        self._redraw_header()  # shows/hides the SIMULATOR badge
        if self.stability_config_error is not None:
            self.status_var.set(
                "V6.1 stability configuration error — measurement is disabled. "
                + self.stability_config_error
            )
        elif self.simulator_mode():
            self.status_var.set("SIMULATOR MODE - results are synthetic, no hardware is read.")
            self.refresh_battery()
        else:
            self.startup_probe()
        if not self.busy and not self.measuring:
            self.render_step()
        else:
            self.update_navigation_state()

    def on_close(self) -> None:
        if self.closing:
            return
        unsaved_result = (
            getattr(self, "last_result", None) is not None
            and not getattr(self, "result_saved", True)
        )
        if unsaved_result and not self.write_autosave("close_safety_check"):
            messagebox.showerror(
                "Cannot close safely",
                "The pending calibration review could not be autosaved:\n\n"
                + (self.autosave_error or "Unknown storage error")
                + "\n\nFix the results-folder storage problem, then close again.",
            )
            return
        if unsaved_result and not messagebox.askyesno(
                "Close with an unsaved calibration review?",
                "This sensor has a measured run that is not in the batch CSV. "
                "The run evidence and current review fields will remain in the protected "
                "autosave and will be offered for recovery next time this batch starts. Close now?",
            ):
            return
        self.closing = True
        self.measure_token += 1
        self.animator.cancel_all()
        self.status_var.set("Stopping capture and turning the emitter off…")
        self._finish_close_when_hardware_idle()

    def _finish_close_when_hardware_idle(self) -> None:
        """Keep Tk responsive while a cancelled capture releases the rig."""
        if self.evidence_saving:
            self.after(50, self._finish_close_when_hardware_idle)
            return
        if (
            self.last_result is not None
            and not self.result_saved
            and not self._ensure_current_run_evidence()
        ):
            self.closing = False
            self.after(20, self._drain_ui_queue)
            messagebox.showerror(
                "Close cancelled — evidence is incomplete",
                "The measured run remains open because its immutable evidence could not "
                "be validated:\n\n"
                + (self.last_run_evidence_error or "Unknown evidence error"),
            )
            return
        if not self.hardware_lock.acquire(blocking=False):
            self.after(50, self._finish_close_when_hardware_idle)
            return
        autosave_ok = True
        try:
            if self.last_result is not None and not self.result_saved:
                autosave_ok = self.write_autosave("closed_with_unsaved_review")
            if autosave_ok and self.device is not None:
                try:
                    self.device.disable_emitter_pwm(EMITTER_PWM_CHANNEL)
                except Exception:
                    pass
                self.device.close()
        finally:
            self.hardware_lock.release()
        if not autosave_ok:
            self.closing = False
            self.after(20, self._drain_ui_queue)
            messagebox.showerror(
                "Close cancelled — autosave failed",
                "The pending calibration review is still open because its autosave failed:\n\n"
                + (self.autosave_error or "Unknown storage error"),
            )
            return
        self.destroy()


def main() -> None:
    app = EmitterTesterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
