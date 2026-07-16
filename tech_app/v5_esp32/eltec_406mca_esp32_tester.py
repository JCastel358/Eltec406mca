"""
Eltec 406MCA ESP32 emitter tester v5 - Xubuntu production application.

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
    ADS AIN1 = permanently-mounted reference sensor (optional; not used here yet)
    ADS AIN7 = 6 V SLA battery through a 100k/100k divider
    GPIO25   = PWM output to the MOSFET module
    sync     = the ESP32 PWM state included with every streamed sample

The emitter is always driven with a fixed 50% duty-cycle square wave, so the
technician never has to think about PWM settings - they only pick the filter.

Guided flow:
    1. Enter batch number, tester name, and filter/setup, then press Enter.
    2. Place the sensor in the rig and press Enter.
    3. The app reads the DC offset (PWM off), then turns on the PWM emitter and
       measures sensitivity and polarity, shows the numbers and a GOOD/BAD
       polarity verdict, and turns the screen green (PASS) or red (FAIL).
       Leave a comment, capture the waveform for troubleshooting, or watch the
       live waveform while it reads.

Run:
    python3 eltec_406mca_esp32_tester.py
"""

from __future__ import annotations

import csv
import json
import math
import struct
import threading
import time
import tkinter as tk
import zlib
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
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
    DEFAULT_MAX_CAPTURE_CYCLES,
    DEFAULT_SAMPLE_RATE_HZ,
    DEFAULT_SETTLE_CYCLES,
    DEFAULT_STABILITY_TOLERANCE,
    DEFAULT_STABILITY_WINDOW_CYCLES,
    EXPECTED_FREQUENCY_HZ,
    FILTER_SPECS_MV,
    MODEL_NAME,
    NEGATIVE_POLARITY,
    OFFSET_MAX_V,
    OFFSET_MIN_V,
    POLARITY_MIN_CONFIDENCE,
    POSITIVE_POLARITY,
    PROCEDURE_SYNC_EDGE,
    SIM_CASES,
    FinalResult,
    WaveformMetrics,
    analyze_waveform,
    cycle_peak_to_peak_values,
    cycle_segments_from_edges,
    estimate_noise_from_segments,
    estimate_polarity,
    evaluate_result,
    fallback_cycle_segments,
    find_sync_edges,
    format_polarity_detail,
    select_stable_cycle_window,
    simulate_offset_v,
    simulate_waveform_samples,
)

from esp32_backend import (
    EXPECTED_FIRMWARE_PREFIX,
    Esp32BackendError,
    Esp32EmitterRig,
    probe_esp32_status,
)

# This rig reads the sensor through a unity-gain buffer (no AM502), so the
# external gain is always 1.0 and the offset rides on the waveform channel.
RIG_GAIN = 1.0

# Technicians run the "-6" setup (the -284 + extra -6 + blackened tube) about
# 80% of the time, so it is the default selection.
DEFAULT_FILTER_SETUP = "-284 filter + extra -6 + blackened tube"

# Fixed ESP32 rig settings. Technicians never change these in production.
EMITTER_PWM_CHANNEL = "GPIO25"
EMITTER_PWM_FREQUENCY_HZ = DEFAULT_EMITTER_PWM_FREQUENCY_HZ
EMITTER_PWM_DUTY_CYCLE = 50.0
WAVEFORM_INPUT_RANGE_V = 2.5  # ADS1256 PGA x2 with 2.5 V reference => +/-2.5 V

# The unity-gain buffer gives AIN0 a low-impedance source, so the DC offset
# median is just as clean from a short burst as from the old 80 x 10 ms read
# (~0.9 s). 24 x 3 ms keeps plenty of noise rejection at ~0.1 s.
OFFSET_READ_SAMPLES = 24
OFFSET_READ_DELAY_S = 0.003

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
SYNC_MIN_SPAN_V = 0.5        # matches the engine's sync edge detection threshold
SYNC_CHECK_CYCLES = 3        # ~0.3 s pre-flight peek at the firmware sync bit
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
# The black-body emitter reaches its steady chopped amplitude only a few
# seconds after the PWM drive turns on (thermal ramp). Early cycles grow and
# drift, and estimate_noise_from_segments counts that ramp as cycle-to-cycle
# noise, so a capture that starts too soon can fail the SNR gate on a healthy
# sensor (seen in fast mode, whose short window could elapse entirely inside
# the ramp). Hold the emitter on this long before the measurement capture
# starts; time already spent on the sync pre-flight and any live-preview
# frames counts toward it. Tune from validation-mode CSV data.
EMITTER_WARMUP_S = 5.0
# Simulator final-capture length: long enough for the engine's rolling-stability
# window to settle (hardware uses read_waveform_stream, which auto-stops when stable).
FINAL_CAPTURE_CYCLES = 80
PREVIEW_CYCLES = 10
PREVIEW_FRAMES = 6

# --------------------------------------------------------------------------- #
# Fast capture (carried forward from v4). The v3 stopping rule cannot even
# check stability before settle 5 + 2 x 20-cycle windows = 45 cycles (4.5 s at
# 10 Hz). The fast path adds a
# margin-based early exit: once the waveform is stable over two short windows
# AND every metric is decisively clear of its limit, the capture stops - a
# clearly good or clearly dead sensor decides in ~1.5-2 s, while marginal
# sensors fall back to the full v3 stopping rule. Three modes:
#   fast        - stop at the early decision (production speed); with no
#                 decision the capture continues under the v3 rule instead
#                 of stopping short on a possibly ramp-contaminated window
#   validation  - run the full v3-length capture, but also log what the fast
#                 path WOULD have decided (fast_* CSV columns). Run batches in
#                 this mode first and confirm fast_match is always YES before
#                 switching to fast.
#   full        - exact v3 timing, no fast-path logging
# --------------------------------------------------------------------------- #
CAPTURE_MODE_FAST = "Fast (early exit)"
CAPTURE_MODE_VALIDATION = "Validation (full capture + fast-path log)"
CAPTURE_MODE_FULL = "Full (v3 timing)"
CAPTURE_MODES = [CAPTURE_MODE_FAST, CAPTURE_MODE_VALIDATION, CAPTURE_MODE_FULL]
DEFAULT_CAPTURE_MODE = CAPTURE_MODE_VALIDATION

FAST_SETTLE_CYCLES = 3              # cycles ignored while the emitter ramps
FAST_STABILITY_WINDOW_CYCLES = 6    # two consecutive 6-cycle windows must agree
FAST_STABILITY_TOLERANCE = 0.10
# "Decisive" margins: how far a metric must sit from its pass/fail limit for
# the early exit to trust a short window. Tune from validation-mode data.
FAST_SENSITIVITY_PASS_FACTOR = 1.5  # pass if >= 1.5x the filter minimum
FAST_SENSITIVITY_FAIL_FACTOR = 0.5  # fail if <= 0.5x the filter minimum
FAST_POLARITY_CONFIDENCE_FACTOR = 1.5   # x POLARITY_MIN_CONFIDENCE (0.10)
FAST_SNR_PASS_FACTOR = 2.0          # x MIN_SIGNAL_TO_NOISE_RATIO
FAST_SNR_FAIL_FACTOR = 0.5
# Keep a few samples past the last cycle's closing sync edge when trimming the
# capture at the decision point: a segment's end index IS that edge, and an
# exclusive slice would drop it, costing the final analysis a whole cycle.
FAST_CUT_MARGIN_SAMPLES = 5


def capture_mode_key(label: str) -> str:
    if label == CAPTURE_MODE_FAST:
        return "fast"
    if label == CAPTURE_MODE_FULL:
        return "full"
    return "validation"


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
# v5 theme - palette carried forward from v4 / eltecinstruments.com
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
    "operator_comments",
    "waveform_snapshot_paths",
    "battery_v",
    "noise_rms_mv",
    "snr_db",
    # Capture telemetry + fast-path validation log.
    "capture_mode",
    "capture_cycles",
    "capture_seconds",
    "fast_stop_cycle",
    "fast_sensitivity_mv",
    "fast_polarity",
    "fast_pass_fail",
    "fast_match",
    "data_source",
]


# --------------------------------------------------------------------------- #
# Results location + batch helpers
# --------------------------------------------------------------------------- #
def results_root_dir() -> Path:
    # Each tester version keeps its data in its own subfolder so results can be
    # tracked and analyzed per version. Autosave and waveform-snapshot folders
    # derive from this path, so they follow automatically.
    return Path.home() / "Documents" / "Eltec_406MCA_Test_Results" / "v5_esp32"


def safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    return cleaned.strip("_") or "UNLABELED"


def batch_results_path(batch_number: str) -> Path:
    return results_root_dir() / f"406mca_esp32_lot_{safe_filename_part(batch_number)}.csv"


def batch_autosave_path(batch_number: str) -> Path:
    safe = safe_filename_part(batch_number)
    return results_root_dir() / "autosave" / f"esp32_lot_{safe}_current_sensor.json"


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


def polarity_good_bad(polarity: str) -> str:
    return "GOOD" if polarity == POSITIVE_POLARITY else "BAD"


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
    capture_report: "CaptureReport | None" = None,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    metrics = final_result.waveform_metrics
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
        "operator_comments": comment.strip(),
        "waveform_snapshot_paths": "; ".join(str(path) for path in snapshot_paths),
        "battery_v": "" if battery_v is None else f"{battery_v:.3f}",
        "noise_rms_mv": _fmt_optional_float(metrics.noise_rms_mv if metrics else None, 4),
        "snr_db": _fmt_optional_float(metrics.signal_to_noise_db if metrics else None, 2),
    }
    if capture_report is not None:
        row.update(capture_report.csv_fields())
    # Batch CSVs created before a column was added keep their original header;
    # write only the columns that file already has so rows stay aligned.
    fieldnames = CSV_FIELDS
    if not write_header:
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
                existing = next(csv.reader(csv_file), None)
            if existing:
                fieldnames = existing
        except Exception:
            pass
    row = {name: row.get(name, "") for name in fieldnames}
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


def save_waveform_snapshot_image(batch_number: str, sensor_id: str, metrics: WaveformMetrics | None, title: str, detail_lines: list[str], filename_suffix: str) -> Path | None:
    if metrics is None or metrics.waveform_v.size == 0:
        return None
    snapshot_dir = results_root_dir() / "waveform_snapshots" / f"lot_{safe_filename_part(batch_number)}"
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


def snapshot_detail_lines(batch_number: str, sensor_id: str, metrics: WaveformMetrics, comment: str = "") -> list[str]:
    lines = [
        f"Batch: {batch_number}",
        f"Sensor: {sensor_id}",
        f"Sensitivity: {metrics.sensitivity_mv:.2f} mV",
        f"Polarity: {metrics.polarity} ({polarity_good_bad(metrics.polarity)})",
    ]
    if metrics.offset_v is not None:
        lines.insert(2, f"Offset: {metrics.offset_v:.3f} V")
    if metrics.measured_frequency_hz is not None:
        lines.append(f"PWM sync: {metrics.measured_frequency_hz:.3f} Hz")
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
    ) -> tuple[np.ndarray, np.ndarray, float]:
        del waveform_range_v
        self.connect()
        target_scans = int(
            math.ceil((cycles / expected_frequency_hz) * sample_rate_hz)
        )
        samples = []
        header = self.start_stream("sensor")
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
        waveform, sync = self._sample_arrays(samples[:target_scans])
        actual_scan_rate = diagnostics.measured_rate_hz or header.sample_rate_hz
        return waveform, sync, float(actual_scan_rate)

    @staticmethod
    def _full_rule_stable(waveform_v: np.ndarray, sync_v: np.ndarray, sample_rate_hz: float, sync_edge: str, expected_frequency_hz: float) -> bool:
        """The v3 stopping rule (settle 5 + two agreeing 20-cycle windows),
        computed from the cheap per-cycle peak-to-peak list instead of running
        the full waveform analysis on every stream read."""
        edges, _freq, _warnings = find_sync_edges(
            sync_v, sample_rate_hz, expected_frequency_hz=expected_frequency_hz, edge=sync_edge,
        )
        segments = cycle_segments_from_edges(edges, settle_cycles=DEFAULT_SETTLE_CYCLES)
        if not segments:
            # Same fallback analyze_waveform uses when sync is unusable, so the
            # stop decision matches v3 for undriven/odd captures too.
            segments = fallback_cycle_segments(
                sample_count=len(waveform_v),
                sample_rate_hz=sample_rate_hz,
                expected_frequency_hz=expected_frequency_hz,
                settle_cycles=DEFAULT_SETTLE_CYCLES,
            )
        cycle_pp_v = cycle_peak_to_peak_values(waveform_v, segments)
        _segments, _pp, stabilized, _change, _idx = select_stable_cycle_window(
            segments, cycle_pp_v,
            tolerance=DEFAULT_STABILITY_TOLERANCE,
            window_cycles=DEFAULT_STABILITY_WINDOW_CYCLES,
        )
        return stabilized

    def read_waveform_stream_decided(
        self,
        *,
        waveform_range_v: float,
        mode: str,
        decider: FastPathDecider | None,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
        sync_edge: str = PROCEDURE_SYNC_EDGE,
        progress=None,
    ) -> tuple[np.ndarray, np.ndarray, float, CaptureReport]:
        """Stream ADS AIN0 + the digital PWM state with mode-dependent stopping.

        fast:       stop as soon as the FastPathDecider reaches a decision.
                    A capture with no early decision is not an easy call
                    (e.g. the emitter is still ramping), so it falls back to
                    the v3 stopping rule instead of stopping short and
                    failing on a ramp-contaminated window.
        validation: stop per the v3 rule; the decider only logs what the fast
                    path would have done.
        full:       stop per the v3 rule, no fast-path logging.
        """
        del waveform_range_v
        self.connect()
        fast_check_cycles = FAST_SETTLE_CYCLES + 2 * FAST_STABILITY_WINDOW_CYCLES
        full_check_cycles = DEFAULT_SETTLE_CYCLES + 2 * DEFAULT_STABILITY_WINDOW_CYCLES

        samples = []
        started = time.monotonic()
        header = self.start_stream("sensor")
        if not math.isclose(header.sample_rate_hz, sample_rate_hz, rel_tol=0.01):
            try:
                self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
            finally:
                raise Esp32BackendError(
                    f"ESP32 advertised {header.sample_rate_hz:g} samples/s; "
                    f"this tester requires {sample_rate_hz:g}. Re-flash {EXPECTED_FIRMWARE_PREFIX}1.7 or newer."
                )
        actual_scan_rate = float(header.sample_rate_hz)
        target_scans = int(
            math.ceil(
                (DEFAULT_MAX_CAPTURE_CYCLES / expected_frequency_hz)
                * actual_scan_rate
            )
        )
        diagnostics = None
        try:
            while len(samples) < target_scans:
                chunk = self.read_stream(
                    max_samples=min(
                        self.STREAM_CHUNK_SAMPLES, target_scans - len(samples)
                    ),
                    timeout_s=self.STREAM_TIMEOUT_S,
                )
                if not chunk:
                    raise Esp32BackendError(
                        f"ESP32 waveform stream stalled after {len(samples)}/{target_scans} samples."
                    )
                samples.extend(chunk)
                cycles_seen = len(samples) / actual_scan_rate * expected_frequency_hz
                if progress is not None:
                    progress(int(cycles_seen))
                waveform_np, sync_np = self._sample_arrays(samples)
                if decider is not None and cycles_seen >= fast_check_cycles:
                    if decider.update(waveform_np, sync_np, actual_scan_rate) and mode == "fast":
                        break
                # In fast mode this is the fallback for captures the decider
                # has not called yet: the v3 rule cannot fire before 45
                # cycles, so the early exit still governs every easy call.
                if cycles_seen >= full_check_cycles:
                    if self._full_rule_stable(waveform_np, sync_np, actual_scan_rate, sync_edge, expected_frequency_hz):
                        break
        finally:
            if self.is_streaming:
                diagnostics = self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
        elapsed = time.monotonic() - started

        if diagnostics is None:
            diagnostics = self.stream_diagnostics
        if diagnostics is None:
            raise Esp32BackendError("ESP32 stream diagnostics were unavailable.")
        self._validate_stream_diagnostics(diagnostics, minimum_samples=len(samples))
        actual_scan_rate = float(diagnostics.measured_rate_hz or actual_scan_rate)
        waveform_np, sync_np = self._sample_arrays(samples[:target_scans])
        report = CaptureReport(mode=mode, capture_seconds=elapsed)
        if decider is not None:
            decider.fill_report(report)
        if mode == "fast" and report.decided and report.cut_sample:
            # Trim the tail of the last stream read so the recorded capture is
            # exactly what the decision was based on.
            waveform_np = waveform_np[: report.cut_sample]
            sync_np = sync_np[: report.cut_sample]
        report.capture_cycles = int(round(len(waveform_np) / max(actual_scan_rate, 1.0) * expected_frequency_hz))
        return waveform_np, sync_np, actual_scan_rate, report


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
# Fast capture: margin-based early decision
# --------------------------------------------------------------------------- #
@dataclass
class CaptureReport:
    """What the capture did, plus the fast-path decision for the CSV log."""

    mode: str
    capture_cycles: int = 0
    capture_seconds: float = 0.0
    decided: bool = False
    stop_cycle: int | None = None       # PWM cycles from emitter start
    cut_sample: int | None = None       # sample index the fast path stopped at
    sensitivity_mv: float | None = None
    polarity: str = ""
    would_pass: bool | None = None
    fail_reasons: list = dataclass_field(default_factory=list)
    match: bool | None = None           # validation mode: fast verdict == full verdict
    data_source: str = ""               # "esp32" | "simulator"

    def csv_fields(self) -> dict[str, str]:
        return {
            "data_source": self.data_source,
            "capture_mode": self.mode,
            "capture_cycles": str(self.capture_cycles),
            "capture_seconds": f"{self.capture_seconds:.2f}",
            "fast_stop_cycle": "" if self.stop_cycle is None else str(self.stop_cycle),
            "fast_sensitivity_mv": "" if self.sensitivity_mv is None else f"{self.sensitivity_mv:.4f}",
            "fast_polarity": self.polarity,
            "fast_pass_fail": "" if self.would_pass is None else ("PASS" if self.would_pass else "FAIL"),
            "fast_match": "" if self.match is None else ("YES" if self.match else "NO"),
        }


class FastPathDecider:
    """Watches a growing capture and decides PASS/FAIL as soon as it is safe.

    The rule: after the first FAST_SETTLE_CYCLES + 2 x FAST_STABILITY_WINDOW
    cycles, once two consecutive short windows agree within tolerance AND every
    metric sits decisively clear of its limit (see the FAST_*_FACTOR margins),
    freeze the verdict. Sensors near any limit never trigger the early exit
    and keep capturing, so only easy calls are shortened.

    The frozen verdict is produced by the SAME analysis + evaluation chain the
    app uses (`analyze_waveform` -> `evaluate_result` -> SNR gate) on the
    samples captured so far, so a validation-mode comparison is apples to
    apples with the full-capture result.
    """

    MIN_PP_CYCLES = FAST_STABILITY_WINDOW_CYCLES * 2

    def __init__(
        self,
        *,
        filter_setup: str,
        offset_v: float | None,
        gain: float,
        input_range_v: float,
        sync_edge: str = PROCEDURE_SYNC_EDGE,
        expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
        use_snr_gate: bool = True,
    ) -> None:
        self.filter_setup = filter_setup
        self.offset_v = offset_v
        self.gain = max(gain, 1e-9)
        self.input_range_v = input_range_v
        self.sync_edge = sync_edge
        self.expected_frequency_hz = expected_frequency_hz
        self.use_snr_gate = use_snr_gate
        self.min_sensitivity_mv = FILTER_SPECS_MV.get(filter_setup)
        self.decision: dict | None = None
        self._evaluated = self.MIN_PP_CYCLES - 1
        # A decisive verdict must repeat on two consecutive cycles before it
        # is frozen, so a single noisy window cannot trigger the early exit.
        self._pending_verdict: tuple[bool, int] | None = None

    def update(self, waveform_v: np.ndarray, sync_v: np.ndarray, sample_rate_hz: float) -> bool:
        """Feed all samples captured so far; True once a decision exists."""
        if self.decision is not None:
            return True
        if self.min_sensitivity_mv is None or waveform_v.size < 2:
            return False
        edges, _freq, _warnings = find_sync_edges(
            sync_v, sample_rate_hz, expected_frequency_hz=self.expected_frequency_hz, edge=self.sync_edge,
        )
        segments = cycle_segments_from_edges(edges, settle_cycles=FAST_SETTLE_CYCLES)
        if not segments:
            segments = fallback_cycle_segments(
                sample_count=len(waveform_v),
                sample_rate_hz=sample_rate_hz,
                expected_frequency_hz=self.expected_frequency_hz,
                settle_cycles=FAST_SETTLE_CYCLES,
            )
        cycle_pp_v = cycle_peak_to_peak_values(waveform_v, segments)
        # Evaluate once per newly completed cycle (each check replays the live
        # situation: only cycles 1..count are visible to the decision).
        for count in range(self._evaluated + 1, len(cycle_pp_v) + 1):
            if self._evaluate_at(count, segments, cycle_pp_v, waveform_v, sync_v, sample_rate_hz):
                return True
        self._evaluated = max(self._evaluated, len(cycle_pp_v))
        return False

    def _evaluate_at(self, count, segments, cycle_pp_v, waveform_v, sync_v, sample_rate_hz) -> bool:
        stable_segments, stable_pp, stabilized, _change, _idx = select_stable_cycle_window(
            segments[:count],
            cycle_pp_v[:count],
            tolerance=FAST_STABILITY_TOLERANCE,
            window_cycles=FAST_STABILITY_WINDOW_CYCLES,
        )
        if not stabilized or not stable_pp:
            return False
        min_mv = self.min_sensitivity_mv
        sensitivity_mv = float(np.median(stable_pp)) / self.gain * 1000.0
        sens_pass = sensitivity_mv >= min_mv * FAST_SENSITIVITY_PASS_FACTOR
        sens_fail = sensitivity_mv <= min_mv * FAST_SENSITIVITY_FAIL_FACTOR

        polarity = estimate_polarity(waveform_v, stable_segments, stable_pp)
        confidence = polarity.confidence or 0.0
        decisive_confidence = POLARITY_MIN_CONFIDENCE * FAST_POLARITY_CONFIDENCE_FACTOR
        pol_pass = polarity.polarity == POSITIVE_POLARITY and confidence >= decisive_confidence
        pol_fail = polarity.polarity == NEGATIVE_POLARITY and confidence >= decisive_confidence

        if self.use_snr_gate:
            noise_rms_v, signal_rms_v, _cycles = estimate_noise_from_segments(waveform_v, stable_segments)
            snr = None
            if noise_rms_v is not None and signal_rms_v is not None and noise_rms_v > 0:
                snr = signal_rms_v / noise_rms_v
            snr_pass = snr is not None and snr >= MIN_SIGNAL_TO_NOISE_RATIO * FAST_SNR_PASS_FACTOR
            snr_fail = snr is not None and snr <= MIN_SIGNAL_TO_NOISE_RATIO * FAST_SNR_FAIL_FACTOR
        else:
            snr_pass, snr_fail = True, False

        offset_ok = self.offset_v is not None and OFFSET_MIN_V <= self.offset_v <= OFFSET_MAX_V
        clear_fail = sens_fail or pol_fail or snr_fail or not offset_ok
        clear_pass = sens_pass and pol_pass and snr_pass and offset_ok
        if not (clear_fail or clear_pass):
            self._pending_verdict = None
            return False
        # Confirmation step: the same verdict direction must appear on the
        # very next cycle as well (costs 0.1 s, kills single-window flukes).
        verdict_direction = clear_pass
        if self._pending_verdict is None or self._pending_verdict != (verdict_direction, count - 1):
            self._pending_verdict = (verdict_direction, count)
            return False

        # Freeze the verdict exactly as the app would report it had the
        # capture stopped right here. The margin keeps the closing sync edge
        # inside the slice so this analysis sees every decided cycle.
        cut = int(stable_segments[-1][1]) + FAST_CUT_MARGIN_SAMPLES if stable_segments else len(waveform_v)
        cut = min(len(waveform_v), max(cut, 2))
        metrics = analyze_esp32_waveform(
            waveform_v=waveform_v[:cut],
            sync_v=sync_v[:cut],
            sample_rate_hz=sample_rate_hz,
            am502_gain=self.gain,
            sync_edge=self.sync_edge,
            expected_frequency_hz=self.expected_frequency_hz,
            stability_tolerance=FAST_STABILITY_TOLERANCE,
            stability_window_cycles=FAST_STABILITY_WINDOW_CYCLES,
            settle_cycles=FAST_SETTLE_CYCLES,
            input_range_v=self.input_range_v,
        )
        metrics.offset_v = self.offset_v
        final = evaluate_result(self.offset_v, metrics, self.filter_setup)
        if self.use_snr_gate:
            final = apply_signal_quality_gate(final, metrics)
        self.decision = {
            "stop_cycle": FAST_SETTLE_CYCLES + count,
            "cut_sample": cut,
            "sensitivity_mv": metrics.sensitivity_mv,
            "polarity": metrics.polarity,
            "would_pass": final.passed,
            "fail_reasons": list(final.fail_reasons),
            # Kept so fast mode can use this analysis AS the result instead of
            # re-analyzing the trimmed capture (a recompute can disagree at the
            # cycle boundary, e.g. a spurious "did not stabilize").
            "metrics": metrics,
            "final": final,
        }
        return True

    def fill_report(self, report: CaptureReport) -> None:
        if self.decision is None:
            return
        report.decided = True
        report.stop_cycle = self.decision["stop_cycle"]
        report.cut_sample = self.decision["cut_sample"]
        report.sensitivity_mv = self.decision["sensitivity_mv"]
        report.polarity = self.decision["polarity"]
        report.would_pass = self.decision["would_pass"]
        report.fail_reasons = self.decision["fail_reasons"]


# --------------------------------------------------------------------------- #
# v5 UI toolkit - colors, easing, animation engine
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
# v5 UI toolkit - custom widgets
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
# Guided ESP32 emitter tester UI (v5)
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
        self.title("Eltec 406MCA ESP32 Emitter Tester v5")
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
        self.busy = False
        self.measuring = False
        self.step = self.SETUP_STEP
        self.measure_token = 0

        # 6 V SLA battery watcher state.
        self.battery_v: float | None = None
        self.battery_state = "unknown"  # "ok" | "warn" | "low" | "unknown"
        self.battery_checking = False
        self.battery_read_time: float | None = None  # time.monotonic() of last good read

        # Batch / sensor state.
        self.batch_number = ""
        self.tester_name = ""
        self.filter_setup = DEFAULT_FILTER_SETUP
        self.current_sensor_number = 0
        self.current_sensor_id = ""
        self.result_saved = True

        # Current-sensor measurement state.
        self.last_metrics: WaveformMetrics | None = None
        self.last_result: FinalResult | None = None
        self.last_capture_report: CaptureReport | None = None
        self.preview_waveform: np.ndarray = np.array([], dtype=float)
        self.preview_sync: np.ndarray = np.array([], dtype=float)
        self.snapshot_paths: list[Path] = []

        self.logo_image: tk.PhotoImage | None = None
        self.wave_canvas: ScopeView | None = None
        self.default_focus_widget: tk.Widget | None = None
        self._advanced_dialog: tk.Toplevel | None = None
        self.step_frame: tk.Frame | None = None

        self._build_variables()
        self._build_style()
        self._load_logo()
        self._build_layout()

        self.bind("<Return>", self.on_enter_key)
        self.bind("<KP_Enter>", self.on_enter_key)
        self.bind("<Escape>", self.on_escape_key)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.render_step()
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
        self.capture_mode_var = tk.StringVar(value=DEFAULT_CAPTURE_MODE)
        self.show_live_var = tk.BooleanVar(value=False)
        self.notes_var = tk.StringVar(value="")

        self.status_var = tk.StringVar(value="Checking ESP32 rig...")
        self.measure_status_var = tk.StringVar(value="")
        self.comment_status_var = tk.StringVar(value="")
        self.snapshot_status_var = tk.StringVar(value="")

        # One-line summary shown next to the "Advanced options" link so the
        # active capture mode / simulator state is visible without opening it.
        self.adv_summary_var = tk.StringVar()
        self.capture_mode_var.trace_add("write", lambda *_a: self._update_adv_summary())
        self.simulator_var.trace_add("write", lambda *_a: self._update_adv_summary())
        self._update_adv_summary()

    def _update_adv_summary(self) -> None:
        bits = [f"capture: {self.capture_mode_var.get()}"]
        if self.simulator_var.get():
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
        self.header.create_text(title_x, S(26), anchor="w", text="406MCA EMITTER TESTER", fill=HEADER_FG, font=self.fd(21), tags="static")
        title_width = tkfont.Font(font=self.fd(21)).measure("406MCA EMITTER TESTER")
        chip_x = title_x + title_width + S(14)
        draw_round_rect(self.header, chip_x, S(15), chip_x + S(40), S(37), Sf(8), fill=ELTEC_RED, outline="", tags="static")
        self.header.create_text(chip_x + S(20), S(26), text="V5", fill="#ffffff", font=self.fm(11, "bold"), tags="static")
        if self.simulator_var.get():
            # Loud amber badge: everything on screen is synthetic.
            sim_text = "SIMULATOR"
            sim_font = self.fm(11, "bold")
            sim_w = tkfont.Font(font=sim_font).measure(sim_text) + S(22)
            sim_x = chip_x + S(40) + S(10)
            draw_round_rect(self.header, sim_x, S(15), sim_x + sim_w, S(37), Sf(8), fill=WARN_ACCENT, outline="", tags="static")
            self.header.create_text(sim_x + sim_w / 2, S(26), text=sim_text, fill="#3d2c00", font=sim_font, tags="static")
        self.header.create_text(title_x, S(47), anchor="w", text="PYROELECTRIC SENSOR QC  ·  EMITTER RIG", fill=mix_color(HEADER_SUB_FG, ELTEC_BLUE, 0.25), font=self.fm(9, "bold"), tags="static")

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
        self._step_heading(0, "01", "Batch information", "Enter the batch number and your name, choose the filter, then press Enter.")

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

        adv_container = tk.Frame(self.step_frame, bg=PAGE_BG)
        adv_container.grid(row=2, column=0, sticky="new", pady=(S(16), 0))
        link = tk.Label(adv_container, text="⚙  Advanced options…", bg=PAGE_BG, fg=ELTEC_BLUE,
                        font=self.fb(12, "bold"), cursor="hand2")
        link.grid(row=0, column=0, sticky="w")
        link.bind("<Button-1>", lambda _e: self.open_advanced_options())
        tk.Label(adv_container, textvariable=self.adv_summary_var, bg=PAGE_BG, fg=MUTED_FG,
                 font=self.fb(11)).grid(row=0, column=1, sticky="w", padx=(S(14), 0))

    def _build_advanced_panel(self, parent: tk.Widget) -> tk.Frame:
        panel = tk.Frame(parent, bg=PAGE_BG)
        panel.columnconfigure(1, weight=1)
        ttk.Checkbutton(panel, text="Simulator mode (training only - synthetic data, clearly badged)",
                        variable=self.simulator_var,
                        command=self.on_simulator_toggle).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(panel, text="Sim case", bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        ttk.Combobox(panel, textvariable=self.sim_case_var, values=SIM_CASES, state="readonly",
                     width=24, font=self.fb(11)).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(panel, text="Simulate low battery (test the change-battery lockout)",
                        variable=self.sim_low_battery_var,
                        command=self.refresh_battery).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Label(panel, text="Capture mode", bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=3, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        ttk.Combobox(panel, textvariable=self.capture_mode_var, values=CAPTURE_MODES, state="readonly",
                     width=42, font=self.fb(11)).grid(row=3, column=1, sticky="w", pady=(10, 0))
        tk.Label(
            panel,
            text=("Fast stops the capture as soon as the sensor is decisively good or bad (~1.5-3 s). "
                  "Validation runs the full-length capture AND logs what Fast would have decided "
                  "(fast_* CSV columns) - run batches in Validation first and confirm every fast_match "
                  "is YES before switching to Fast. Full reproduces the v3 timing exactly."),
            bg=PAGE_BG, fg=MUTED_FG, font=self.fb(10), wraplength=S(640), justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        tk.Label(
            panel,
            text=(f"ESP32 rig: ADS AIN0 = buffered DUT (offset + AC), ADS AIN7 = 6 V SLA via "
                  f"100k/100k divider (÷2), streamed sync = PWM state, {EMITTER_PWM_CHANNEL} = MOSFET gate. "
                  f"Emitter driven at {EMITTER_PWM_FREQUENCY_HZ:g} Hz, {EMITTER_PWM_DUTY_CYCLE:g}% duty (fixed). "
                  f"ADS sensor range is ±{WAVEFORM_INPUT_RANGE_V:g} V through a unity-gain buffer. "
                  f"Testing is blocked at or below {BATTERY_MIN_V:.1f} V."),
            bg=PAGE_BG, fg=MUTED_FG, font=self.fb(10), wraplength=S(640), justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))
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

    def render_load_step(self) -> None:
        self._step_heading(0, "02", f"Load sensor {self.current_sensor_id}", f"Batch {self.batch_number}    ·    Filter: {self.filter_setup}")

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

    def render_result_view(self) -> None:
        result = self.last_result
        passed = result.passed
        self._build_result_banner(row=0, passed=passed)

        tiles = tk.Frame(self.step_frame, bg=PAGE_BG)
        tiles.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        for column in range(3):
            tiles.columnconfigure(column, weight=1, uniform="tiles")
        offset_ok = result.offset_v is not None and OFFSET_MIN_V <= result.offset_v <= OFFSET_MAX_V
        min_mv = FILTER_SPECS_MV.get(self.filter_setup)
        sens_ok = result.sensitivity_mv is not None and min_mv is not None and result.sensitivity_mv >= min_mv
        pol_verdict = polarity_good_bad(result.polarity)
        self._result_tile(tiles, 0, "Offset", result.offset_v, offset_ok, unit=" V", decimals=3)
        self._result_tile(tiles, 1, "Sensitivity", result.sensitivity_mv, sens_ok, unit=" mV", decimals=2)
        self._result_tile(tiles, 2, "Polarity", pol_verdict, pol_verdict == "GOOD")

        detail_bits = [f"Filter: {self.filter_setup}"]
        if min_mv is not None:
            detail_bits.append(f"min sensitivity {min_mv:.1f} mV")
        if result.polarity:
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
        if report is not None and report.capture_cycles:
            detail_bits.append(f"capture {report.capture_cycles} cyc / {report.capture_seconds:.1f} s")
        if report is not None and report.mode == "validation":
            if report.decided:
                fast_verdict = "PASS" if report.would_pass else "FAIL"
                match_text = "match" if report.match else "MISMATCH ⚠"
                detail_bits.append(f"fast path: {fast_verdict} @ cycle {report.stop_cycle} ({match_text})")
            else:
                detail_bits.append("fast path: no early decision")
        tk.Label(self.step_frame, text="   ·   ".join(detail_bits), bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11),
                 wraplength=S(880), justify="left", anchor="w").grid(row=2, column=0, sticky="w", pady=(S(12), 0))

        if result.fail_reasons:
            reasons = tk.Text(self.step_frame, height=min(len(result.fail_reasons) + 1, 4), wrap="word", font=self.fb(12),
                              relief="flat", bd=0, bg="#fff5f5", fg=FAIL_FG, padx=12, pady=10,
                              highlightbackground="#f3c2c2", highlightcolor="#f3c2c2", highlightthickness=1)
            reasons.grid(row=3, column=0, sticky="ew", pady=(10, 0))
            reasons.insert("1.0", "\n".join(f"•  {reason}" for reason in result.fail_reasons))
            reasons.configure(state="disabled")

        tools = tk.Frame(self.step_frame, bg=PAGE_BG)
        tools.grid(row=4, column=0, sticky="w", pady=(16, 0))
        self.btn(tools, "Comment", self.open_comment_window, kind="ghost", size="sm").grid(row=0, column=0, padx=(0, 10))
        self.btn(tools, "Capture waveform", self.capture_waveform_snapshot, kind="ghost", size="sm").grid(row=0, column=1, padx=(0, 10))
        self.btn(tools, "Re-measure", self.run_measurement, kind="ghost", size="sm").grid(row=0, column=2, padx=(0, 14))
        ToggleSwitch(tools, "Show waveform", self.show_live_var, command=self.toggle_live_view, font=self.fb(12)).grid(row=0, column=3)
        tk.Label(tools, textvariable=self.comment_status_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        tk.Label(tools, textvariable=self.snapshot_status_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=2, column=0, columnspan=4, sticky="w")

        if self.show_live_var.get():
            self._build_wave_canvas(row=5, live=False)
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
            banner.create_text(S(64), height / 2, text=glyph, fill=accent, font=self.fd(glyph_size))
            text_x = S(118) + S(26) * (1.0 - vals["text"])
            text_color = mix_color(banner_bg, banner_fg, vals["text"])
            banner.create_text(text_x, height / 2 - S(12), anchor="w", text=verdict, fill=text_color, font=self.fd(33))
            banner.create_text(text_x + S(2), height / 2 + S(26), anchor="w", text=f"SENSOR {self.current_sensor_id}", fill=mix_color(banner_bg, banner_fg, vals["text"] * 0.75), font=self.fm(11, "bold"))
            stamp = datetime.now().strftime("%H:%M")
            banner.create_text(width - S(22), height / 2, anchor="e", text=f"BATCH {self.batch_number}  ·  {stamp}", fill=mix_color(banner_bg, banner_fg, 0.6), font=self.fm(10, "bold"))

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
        self.secondary_button = self.btn(self.footer_bar, "Save + Exit Batch", self.save_and_end_batch, kind="outline", size="lg")
        self.secondary_button.grid(row=0, column=1, sticky="e", padx=(0, S(10)))
        self.primary_button = self.btn(self.footer_bar, "Next", self.go_next, kind="primary", size="lg")
        self.primary_button.grid(row=0, column=2, sticky="e")

    def update_navigation_state(self) -> None:
        self.secondary_button.grid_remove()
        if self.step == self.SETUP_STEP:
            self.back_button.configure(state="disabled")
            self.primary_button.configure(text="Start (Enter)", state="disabled" if self.busy else "normal")
        elif self.step == self.LOAD_STEP:
            self.back_button.configure(state="disabled" if self.busy else "normal")
            # Hard block: no measurement on a low battery or a wiring fault.
            blocked = self.busy or self.battery_state in ("low", "fault")
            if self.battery_state == "fault":
                measure_text = "Check wiring to test"
            elif self.battery_state == "low":
                measure_text = "Recharge battery to test"
            else:
                measure_text = "Measure (Enter)"
            self.primary_button.configure(text=measure_text, state="disabled" if blocked else "normal")
        else:
            self.secondary_button.grid()
            self.back_button.configure(state="disabled" if self.busy or self.result_saved else "normal")
            ready = not self.busy and not self.measuring and self.last_result is not None and not self.result_saved
            self.primary_button.configure(text="Save + Next Sensor (Enter)", state="normal" if ready else "disabled")
            self.secondary_button.configure(text="Save + Exit Batch (Esc)", state="normal" if ready else "disabled")

    def go_next(self) -> None:
        if self.busy:
            return
        if self.step == self.SETUP_STEP:
            self.start_batch()
        elif self.step == self.LOAD_STEP:
            self.show_step(self.RESULT_STEP)
            self.run_measurement()
        elif self.step == self.RESULT_STEP:
            self.save_and_continue()

    def go_back(self) -> None:
        if self.busy or self.measuring:
            return
        if self.step == self.LOAD_STEP:
            self.show_step(self.SETUP_STEP)
        elif self.step == self.RESULT_STEP and not self.result_saved:
            self.show_step(self.LOAD_STEP)

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
        batch_number = self.batch_var.get().strip()
        if not batch_number:
            messagebox.showerror("Batch number needed", "Please enter a batch number.")
            return
        tester_name = self.tester_var.get().strip()
        if not tester_name:
            messagebox.showerror("Tester name needed", "Please enter the tester name.")
            return

        self.batch_number = batch_number
        self.tester_name = tester_name
        self.filter_setup = self.filter_var.get()
        csv_path = batch_results_path(batch_number)
        self.current_sensor_number = next_sensor_number_for_batch(csv_path)
        existing = count_existing_batch_rows(csv_path)
        position = "next" if existing else "first"
        self.status_var.set(f"Batch {batch_number}: {position} sensor is {batch_number}-{self.current_sensor_number}.")
        self.prepare_current_sensor()
        self.show_step(self.LOAD_STEP)

    def prepare_current_sensor(self) -> None:
        self.current_sensor_id = f"{self.batch_number}-{self.current_sensor_number}"
        self.result_saved = False
        self.last_metrics = None
        self.last_result = None
        self.last_capture_report = None
        self.preview_waveform = np.array([], dtype=float)
        self.preview_sync = np.array([], dtype=float)
        self.snapshot_paths = []
        self.notes_var.set("")
        self.comment_status_var.set("")
        self.snapshot_status_var.set("")
        self.measure_status_var.set("")

    def save_and_continue(self) -> None:
        if self.save_current_sensor():
            self.current_sensor_number += 1
            self.prepare_current_sensor()
            self.show_step(self.LOAD_STEP)

    def save_and_end_batch(self) -> None:
        if self.save_current_sensor():
            saved_batch = self.batch_number
            saved_csv = batch_results_path(saved_batch)
            self.status_var.set(f"Batch {saved_batch} ended.")
            self.step = self.SETUP_STEP
            self.result_saved = True
            self.show_batch_summary_window(saved_batch, saved_csv)
            self.render_step()

    def save_current_sensor(self) -> bool:
        if self.last_result is None:
            messagebox.showinfo("Nothing to save", "Run the measurement before saving.")
            return False
        pwm_hz, pwm_duty = EMITTER_PWM_FREQUENCY_HZ, EMITTER_PWM_DUTY_CYCLE
        try:
            append_result_csv(
                batch_results_path(self.batch_number),
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
            )
        except Exception as exc:
            messagebox.showerror("Could not save result", str(exc))
            return False
        self.result_saved = True
        self.delete_autosave()
        self.status_var.set(f"Saved {self.current_sensor_id}.")
        self.update_navigation_state()
        return True

    def _post(self, callback) -> None:
        """Schedule a callback on the UI thread, ignoring app-shutdown races."""
        try:
            self.after(0, callback)
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
        self._refresh_battery_pill()
        simulator = self.simulator_var.get()
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
            self._post(lambda: self.on_battery_update(battery_v, error))

        threading.Thread(target=worker, daemon=True).start()

    def on_battery_update(self, battery_v: float | None, error: Exception | None = None) -> None:
        self.battery_checking = False
        if error is None and battery_v is not None:
            self.battery_v = battery_v
            self.battery_state = battery_state_for(battery_v)
            self.battery_read_time = time.monotonic()
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

    # ----- measurement ----- #
    def run_measurement(self, _event: tk.Event | None = None) -> None:
        if self.busy or self.measuring:
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
        simulator = self.simulator_var.get()
        sim_case = self.sim_case_var.get()
        sim_low_battery = self.sim_low_battery_var.get()
        filter_setup = self.filter_setup
        show_live = self.show_live_var.get()

        self.measuring = True
        self.busy = True
        self.last_metrics = None
        self.last_result = None
        self.last_capture_report = None
        self.measure_status_var.set("Reading DC offset (emitter off)...")
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
                        filter_setup, waveform_range_v, pwm_channel, pwm_hz, pwm_duty, show_live, token, push
                    )
            except BatteryTooLowError as exc:
                push(lambda exc=exc: self.on_battery_block(token, exc.battery_v))
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
            and self.battery_read_time is not None
            and (time.monotonic() - self.battery_read_time) <= BATTERY_REUSE_WINDOW_S
        ):
            return self.battery_v
        return None

    def _hardware_measurement(self, filter_setup, waveform_range_v, pwm_channel, pwm_hz, pwm_duty, show_live, token, push):
        mode = capture_mode_key(self.capture_mode_var.get())
        with self.hardware_lock:
            self.ensure_connected()
            device = self.device
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
            offset_v = device.read_offset_voltage(waveform_range_v=waveform_range_v)
            # Pre-flight: a connected 406MCA presents its ~0.3-1.2 V DC offset
            # through the buffer. A floating/railed AIN0 means no sensor (or no
            # buffer) - abort before capturing anything.
            if not (SENSOR_OFFSET_MIN_PLAUSIBLE_V <= offset_v <= SENSOR_OFFSET_MAX_PLAUSIBLE_V):
                raise HardwareNotReadyError(
                    f"No sensor detected: AIN0 reads {offset_v:.3f} V DC, but a connected 406MCA "
                    "sits near 0.3-1.2 V. Seat the sensor in the rig and check the buffer wiring, "
                    "then press Measure again."
                )
            push(lambda v=offset_v: self.on_offset_update(token, v))

            device.configure_emitter_pwm(channel=pwm_channel, frequency_hz=pwm_hz, duty_cycle_percent=pwm_duty)
            emitter_on_time = time.monotonic()
            push(lambda: self.set_measure_status(token, "Emitter PWM on. Checking the ESP32 sync stream..."))
            try:
                # Pre-flight: confirm the firmware's streamed PWM state toggles.
                # This intentionally does not require a physical AIN2 loopback;
                # signal quality is still checked after the DUT capture.
                _wf_check, sync_check, _rate_check = device.read_waveform_frame(SYNC_CHECK_CYCLES, waveform_range_v)
                sync_span = float(np.max(sync_check) - np.min(sync_check)) if sync_check.size else 0.0
                if sync_span < SYNC_MIN_SPAN_V:
                    raise HardwareNotReadyError(
                        f"ESP32 PWM sync did not toggle (span {sync_span:.2f}). Check firmware and the "
                        f"{EMITTER_PWM_CHANNEL} configuration, then press Measure again."
                    )
                if show_live:
                    for _ in range(PREVIEW_FRAMES):
                        if token != self.measure_token:
                            break
                        wf, sync, _rate = device.read_waveform_frame(PREVIEW_CYCLES, waveform_range_v)
                        push(lambda wf=wf, sync=sync: self.on_preview_frame(token, wf, sync))
                # Warm-up: the emitter's chopped amplitude keeps ramping for a
                # few seconds after the drive turns on, and a capture that
                # starts inside that ramp reads the ramp as cycle-to-cycle
                # noise and can fail the SNR gate on a healthy sensor. Wait
                # out whatever the sync pre-flight / preview frames have not
                # already spent with the emitter running.
                warmup_left = EMITTER_WARMUP_S - (time.monotonic() - emitter_on_time)
                if warmup_left > 0:
                    push(lambda w=warmup_left: self.set_measure_status(
                        token, f"Emitter PWM on. Warming up the emitter ({w:.0f} s)..."))
                    time.sleep(warmup_left)
                push(lambda: self.set_measure_status(token, "Emitter PWM on. Measuring sensitivity and polarity..."))
                decider = None
                if mode in ("fast", "validation"):
                    decider = FastPathDecider(
                        filter_setup=filter_setup,
                        offset_v=offset_v,
                        gain=RIG_GAIN,
                        input_range_v=waveform_range_v,
                        sync_edge=PROCEDURE_SYNC_EDGE,
                        expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
                    )

                def progress(cycles: int) -> None:
                    push(lambda c=cycles: self.set_measure_status(
                        token, f"Emitter PWM on. Measuring sensitivity and polarity... cycle {c}"))

                waveform, sync, actual_rate, report = device.read_waveform_stream_decided(
                    waveform_range_v=waveform_range_v,
                    mode=mode,
                    decider=decider,
                    sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
                    expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
                    sync_edge=PROCEDURE_SYNC_EDGE,
                    progress=progress,
                )
            finally:
                device.disable_emitter_pwm(pwm_channel)

        if mode == "fast" and decider is not None and decider.decision is not None:
            # The early exit already ran the full analysis + evaluation chain
            # on exactly this capture - use that verdict verbatim. Re-analyzing
            # the trimmed stream can disagree at the final cycle boundary and
            # fail a capture the decider validated (seen on hardware as a
            # spurious "did not stabilize" / SNR flip).
            metrics = decider.decision["metrics"]
            final = decider.decision["final"]
        else:
            # No early decision means the capture ran on under the v3 stopping
            # rule (fast mode falls back to it rather than stopping short), so
            # every mode analyzes a full-length capture with the v3 windows.
            metrics = analyze_esp32_waveform(
                waveform_v=waveform,
                sync_v=sync,
                sample_rate_hz=actual_rate,
                am502_gain=RIG_GAIN,
                sync_edge=PROCEDURE_SYNC_EDGE,
                input_range_v=waveform_range_v,
            )
            metrics.offset_v = offset_v
            final = evaluate_result(offset_v, metrics, filter_setup)
            # Reject captures that are mostly noise (e.g. the emitter is not
            # being driven), which amplitude/polarity alone can let through.
            final = apply_signal_quality_gate(final, metrics)
        if report.mode == "validation" and report.decided and report.would_pass is not None:
            report.match = report.would_pass == final.passed
        report.data_source = "esp32"
        self.last_capture_report = report
        return metrics, final, offset_v

    def _simulate_measurement(self, filter_setup, sim_case, sim_low_battery, waveform_range_v, show_live, token, push):
        # Mirror the hardware battery gate so the lockout is testable without the ESP32.
        battery_v = SIM_BATTERY_LOW_V if sim_low_battery else SIM_BATTERY_OK_V
        push(lambda v=battery_v: self.on_battery_update(v))
        if battery_v <= BATTERY_MIN_V:
            raise BatteryTooLowError(battery_v)
        offset_v = simulate_offset_v(sim_case)
        push(lambda v=offset_v: self.on_offset_update(token, v))
        push(lambda: self.set_measure_status(token, "Emitter PWM on (simulated). Measuring sensitivity and polarity..."))
        if show_live:
            for _ in range(PREVIEW_FRAMES):
                if token != self.measure_token:
                    break
                wf, sync, _rate = simulate_waveform_samples(filter_setup, sim_case, cycles=PREVIEW_CYCLES, am502_gain=RIG_GAIN)
                wf = wf + offset_v
                push(lambda wf=wf, sync=sync: self.on_preview_frame(token, wf, sync))
                time.sleep(0.4)
        waveform, sync, actual_rate = simulate_waveform_samples(filter_setup, sim_case, cycles=FINAL_CAPTURE_CYCLES, am502_gain=RIG_GAIN)
        waveform = waveform + offset_v

        # Replay the fast path offline over the synthetic capture so capture
        # modes (incl. the validation log) can be exercised without hardware.
        # Mirror the hardware stopping rules: fast mode only shortens the
        # capture when the decider reaches an early decision (the trim below);
        # with no decision it runs a full-length capture like the other modes.
        mode = capture_mode_key(self.capture_mode_var.get())
        report = CaptureReport(mode=mode)
        decider = None
        if mode in ("fast", "validation"):
            # The simulator skips the SNR gate (synthetic noise is not
            # comparable to hardware), so the fast path skips it too.
            decider = FastPathDecider(
                filter_setup=filter_setup,
                offset_v=offset_v,
                gain=RIG_GAIN,
                input_range_v=waveform_range_v,
                sync_edge=PROCEDURE_SYNC_EDGE,
                expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
                use_snr_gate=False,
            )
            decider.update(waveform, sync, actual_rate)
            decider.fill_report(report)
        if mode == "fast" and report.decided and report.cut_sample:
            waveform = waveform[: report.cut_sample]
            sync = sync[: report.cut_sample]

        if mode == "fast" and decider is not None and decider.decision is not None:
            # Same as hardware fast mode: the frozen decision analysis IS the
            # result for this capture.
            metrics = decider.decision["metrics"]
            final = decider.decision["final"]
        else:
            metrics = analyze_esp32_waveform(
                waveform_v=waveform,
                sync_v=sync,
                sample_rate_hz=actual_rate,
                am502_gain=RIG_GAIN,
                sync_edge=PROCEDURE_SYNC_EDGE,
                input_range_v=waveform_range_v,
            )
            metrics.offset_v = offset_v
            final = evaluate_result(offset_v, metrics, filter_setup)
            # The SNR gate is intentionally NOT applied here: the simulator
            # injects a large fixed noise so synthetic SNR is not comparable to
            # hardware, and a synthetic capture is coherent by construction (it
            # cannot exhibit the undriven-emitter failure the gate catches).
        report.capture_cycles = int(round(len(waveform) / max(actual_rate, 1.0) * EXPECTED_FREQUENCY_HZ))
        report.capture_seconds = report.capture_cycles / EXPECTED_FREQUENCY_HZ  # simulated wall time
        if report.mode == "validation" and report.decided and report.would_pass is not None:
            report.match = report.would_pass == final.passed
        report.data_source = "simulator"
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

    def on_measure_done(self, token: int, metrics: WaveformMetrics, final: FinalResult) -> None:
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        self.last_metrics = metrics
        self.last_result = final
        self.preview_waveform = metrics.waveform_v
        self.preview_sync = metrics.sync_v
        verdict = "PASS" if final.passed else "FAIL"
        self.status_var.set(f"{self.current_sensor_id}: {verdict}.")
        self.render_step()

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
        if self.step == self.RESULT_STEP:
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
        count = len(self.snapshot_paths)
        if count == 0:
            self.snapshot_status_var.set("")
        elif count == 1:
            self.snapshot_status_var.set("1 waveform snapshot saved")
        else:
            self.snapshot_status_var.set(f"{count} waveform snapshots saved")

    def capture_waveform_snapshot(self) -> None:
        if self.last_metrics is None:
            messagebox.showinfo("No waveform yet", "Run the measurement before capturing a waveform.")
            return
        try:
            snapshot_path = save_waveform_snapshot_image(
                self.batch_number,
                self.current_sensor_id,
                self.last_metrics,
                title=f"{MODEL_NAME} {self.current_sensor_id} waveform snapshot",
                detail_lines=snapshot_detail_lines(self.batch_number, self.current_sensor_id, self.last_metrics, self.notes_var.get()),
                filename_suffix="snapshot",
            )
        except Exception as exc:
            messagebox.showerror("Waveform snapshot problem", str(exc))
            return
        if snapshot_path is None:
            messagebox.showinfo("No waveform", "No waveform samples were available to capture.")
            return
        self.snapshot_paths.append(snapshot_path)
        self.update_comment_snapshot_status()
        self.write_autosave("waveform_snapshot_saved")
        self.status_var.set(f"Saved waveform snapshot: {snapshot_path}")

    # ----- autosave ----- #
    def write_autosave(self, stage: str) -> None:
        if not self.batch_number or not self.current_sensor_id:
            return
        autosave_path = batch_autosave_path(self.batch_number)
        autosave_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "batch_number": self.batch_number,
            "tester_name": self.tester_name,
            "sensor_number": self.current_sensor_number,
            "sensor_id": self.current_sensor_id,
            "filter_setup": self.filter_setup,
            "offset_v": None if self.last_result is None else self.last_result.offset_v,
            "sensitivity_mv": None if self.last_result is None else self.last_result.sensitivity_mv,
            "polarity": None if self.last_result is None else self.last_result.polarity,
            "pass_fail": None if self.last_result is None else ("PASS" if self.last_result.passed else "FAIL"),
            "battery_v": self.battery_v,
            "comment": self.notes_var.get(),
            "waveform_snapshot_paths": [str(path) for path in self.snapshot_paths],
        }
        try:
            with autosave_path.open("w", encoding="utf-8") as autosave_file:
                json.dump(payload, autosave_file, indent=2)
        except Exception:
            pass

    def delete_autosave(self) -> None:
        if not self.batch_number:
            return
        try:
            batch_autosave_path(self.batch_number).unlink(missing_ok=True)
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
        passed = sum(1 for row in rows if row[-1] == "PASS")
        yield_pct = (100.0 * passed / tested) if tested else 0.0
        fast_matches, fast_mismatches = self._count_fast_matches(csv_path)

        head = tk.Frame(summary, bg=PAGE_BG)
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 6))
        tk.Label(head, text="BATCH SUMMARY —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(11, "bold")).pack(anchor="w")
        tk.Label(head, text=f"Batch {batch_number} results", bg=PAGE_BG, fg=TEXT_DARK, font=self.fd(24)).pack(anchor="w", pady=(2, 0))
        chips = tk.Frame(head, bg=PAGE_BG)
        chips.pack(anchor="w", pady=(10, 0))
        chip_specs = [
            (f"{tested} TESTED", ELTEC_BLUE_DARK, ELTEC_BLUE_LIGHT),
            (f"{passed} PASSED", PASS_FG, PASS_BG),
            (f"{tested - passed} FAILED", FAIL_FG, FAIL_BG),
            (f"YIELD {yield_pct:.0f}%", ELTEC_BLUE_DARK, ELTEC_BLUE_LIGHT),
        ]
        if fast_matches or fast_mismatches:
            # Validation-mode tally: the fast path agreed with the full capture
            # on N of M sensors. All-match is the green light for Fast mode.
            total_compared = fast_matches + fast_mismatches
            if fast_mismatches == 0:
                chip_specs.append((f"FAST PATH {fast_matches}/{total_compared} MATCH", PASS_FG, PASS_BG))
            else:
                chip_specs.append((f"FAST PATH {fast_mismatches} MISMATCH", FAIL_FG, FAIL_BG))
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
        columns = ("sensor", "offset", "sensitivity", "polarity", "result")
        headings = {"sensor": "Sensor", "offset": "Offset", "sensitivity": "Sensitivity", "polarity": "Polarity", "result": "Result"}
        tree = ttk.Treeview(frame, columns=columns, show="headings", style="Summary.Treeview", height=14)
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=150, anchor="center", stretch=True)
        tree.tag_configure("pass", background=PASS_BG)
        tree.tag_configure("fail", background=FAIL_BG)
        for row in rows:
            tag = "pass" if row[-1] == "PASS" else "fail"
            tree.insert("", "end", values=row, tags=(tag,))
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=y_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.btn(summary, "Close", summary.destroy, kind="primary", size="md").grid(row=2, column=0, sticky="e", padx=20, pady=14)

    def _count_fast_matches(self, csv_path: Path) -> tuple[int, int]:
        matches = mismatches = 0
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
                for row in csv.DictReader(csv_file):
                    value = (row.get("fast_match") or "").strip().upper()
                    if value == "YES":
                        matches += 1
                    elif value == "NO":
                        mismatches += 1
        except Exception:
            pass
        return matches, mismatches

    def _read_summary_rows(self, csv_path: Path) -> list[tuple[str, str, str, str, str]]:
        rows: list[tuple[str, str, str, str, str]] = []
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
                for row in csv.DictReader(csv_file):
                    rows.append(
                        (
                            row.get("sensor_id", ""),
                            self._fmt(row.get("offset_v", ""), 3, " V"),
                            self._fmt(row.get("sensitivity_mv", ""), 2, " mV"),
                            row.get("polarity_good_bad", "") or row.get("polarity", ""),
                            row.get("pass_fail", ""),
                        )
                    )
        except Exception:
            rows.append(("Could not read batch CSV", "", "", "", "FAIL"))
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
        ok, message = probe_esp32_status()
        # Simulator mode is an explicit choice (Advanced options) - NEVER
        # auto-enable it. Older builds silently switched to the simulator when
        # found, which let a technician run a "test" against synthetic numbers
        # (with plausible-looking results) without
        # noticing that nothing was plugged in.
        if not ok and not self.simulator_var.get():
            message = self._friendly_hardware_error(message)
        self.status_var.set(message)
        if self.simulator_var.get():
            self.refresh_battery()

    def on_simulator_toggle(self) -> None:
        """Make entering/leaving simulator mode loud and reset stale readings."""
        self.battery_v = None
        self.battery_state = "unknown"
        self.battery_read_time = None
        self._refresh_battery_pill()
        self._redraw_header()  # shows/hides the SIMULATOR badge
        if self.simulator_var.get():
            self.status_var.set("SIMULATOR MODE - results are synthetic, no hardware is read.")
            self.refresh_battery()
        else:
            self.startup_probe()
        self.update_navigation_state()

    def on_close(self) -> None:
        self.measure_token += 1
        self.animator.cancel_all()
        if self.device is not None:
            try:
                self.device.disable_emitter_pwm(EMITTER_PWM_CHANNEL)
            except Exception:
                pass
            self.device.close()
        self.destroy()


def main() -> None:
    app = EmitterTesterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
