"""
Eltec 406MCA emitter tester - guided, technician-friendly workflow.

This app drives the test rig itself: the LabJack T7-Pro generates a PWM signal
that switches a MOSFET to drive the black-body emitter. The 406MCA sensor is
read WITHOUT the AM502 amplifier - a unity-gain (voltage-follower) op-amp buffer
feeds the LabJack a low-impedance signal while preserving the ~0.667 V DC offset
and the small AC waveform.

Wiring:
    AIN0 = buffered sensor signal (carries BOTH the DC offset and the AC signal)
    AIN1 = 9V battery through a 100k/100k divider (divide-by-2), so the LabJack
           watches the supply that powers the op-amp buffer + sensor and blocks
           testing once it sags too low. Reads on the +/-10 V range (Vbat/2).
    AIN2 = PWM / MOSFET-gate drive, looped back as the polarity/sync reference
    DIO0 = PWM output to the MOSFET gate (common ground with the emitter supply)

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
    python eltec_406mca_emitter_tester.py
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
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import numpy as np

# Reuse the proven analysis + PWM engine from the v1 single-sensor tester so
# there is a single source of truth for the signal math and the LabJack device
# wrapper. That module lives in a sibling version folder (tech_app/v1_single_sensor),
# so add it to the import path.
import sys

_V1_TESTER_DIR = Path(__file__).resolve().parents[1] / "v1_single_sensor"
if str(_V1_TESTER_DIR) not in sys.path:
    sys.path.insert(0, str(_V1_TESTER_DIR))

from eltec_406mca_tester import (
    DEFAULT_EMITTER_PWM_CHANNEL,
    DEFAULT_EMITTER_PWM_FREQUENCY_HZ,
    DEFAULT_SAMPLE_RATE_HZ,
    DEFAULT_WAVEFORM_INPUT_RANGE_LABEL,
    EXPECTED_FREQUENCY_HZ,
    FILTER_SPECS_MV,
    MODEL_NAME,
    OFFSET_MAX_V,
    OFFSET_MIN_V,
    POSITIVE_POLARITY,
    PROCEDURE_SYNC_EDGE,
    SIM_CASES,
    SYNC_CHANNEL,
    WAVEFORM_CHANNEL,
    WAVEFORM_SETTLING_READS,
    FinalResult,
    LabJackT7,
    WaveformMetrics,
    analyze_waveform,
    evaluate_result,
    format_polarity_detail,
    labjack_ain0_range_from_label,
    ljm,
    probe_labjack_status,
    simulate_offset_v,
    simulate_waveform_samples,
)

# This rig reads the sensor through a unity-gain buffer (no AM502), so the
# external gain is always 1.0 and the offset rides on the waveform channel.
RIG_GAIN = 1.0

# Technicians run the "-6" setup (the -284 + extra -6 + blackened tube) about
# 80% of the time, so it is the default selection.
DEFAULT_FILTER_SETUP = "-284 filter + extra -6 + blackened tube"

# Fixed rig drive settings. Technicians never change these, so they are baked in
# as constants instead of on-screen controls: the emitter is always driven with
# a 50% duty-cycle square wave on DIO0 and the sensor is read on the x10 range.
EMITTER_PWM_CHANNEL = DEFAULT_EMITTER_PWM_CHANNEL
EMITTER_PWM_FREQUENCY_HZ = DEFAULT_EMITTER_PWM_FREQUENCY_HZ
EMITTER_PWM_DUTY_CYCLE = 50.0
WAVEFORM_INPUT_RANGE_LABEL = DEFAULT_WAVEFORM_INPUT_RANGE_LABEL

OFFSET_READ_SAMPLES = 80
OFFSET_READ_DELAY_S = 0.01

# 9V battery watcher. The battery powers the voltage-follower op-amp and the
# sensor; as it sags the readings drift, so we monitor it on AIN1 through a
# 100k/100k divide-by-2 divider and block testing once it drops too low.
BATTERY_CHANNEL = "AIN1"
BATTERY_DIVIDER_RATIO = 2.0        # (R1 + R2) / R2 for a 100k/100k divider
BATTERY_CALIBRATION = 1.0          # trim against a DMM if the divider is not exactly 2x
BATTERY_RANGE_V = 10.0             # read AIN1 on the +/-10 V range (Vbat/2 ~ 4.8 V fresh)
BATTERY_MIN_V = 7.2               # hard block: testing disabled at or below this
BATTERY_WARN_V = 7.7              # early warning band (still allowed to test)
BATTERY_READ_SAMPLES = 40
BATTERY_READ_DELAY_S = 0.01
BATTERY_RESOLUTION_INDEX = 8       # high resolution for a clean DC read
BATTERY_SETTLING_US = 1000.0       # extra settling for the ~50k source impedance
# Simulator battery levels so the low-battery lockout can be exercised without hardware.
SIM_BATTERY_OK_V = 8.8
SIM_BATTERY_LOW_V = 6.9

# Signal-quality gate. A real chopped-emitter capture has a strong coherent
# waveform standing above the cycle-to-cycle noise (high SNR). A capture that is
# mostly sensor noise - e.g. the emitter is not actually being driven - has a
# low signal-to-noise ratio, so we fail it instead of trusting the raw
# amplitude. Tune this up once real good-sensor SNRs are known (the SNR is now
# logged to the batch CSV to help calibrate it).
MIN_SIGNAL_TO_NOISE_RATIO = 1.5   # ~3.5 dB
# Simulator final-capture length: long enough for the engine's rolling-stability
# window to settle (hardware uses read_waveform_stream, which auto-stops when stable).
FINAL_CAPTURE_CYCLES = 80
PREVIEW_CYCLES = 10
PREVIEW_FRAMES = 6

# Company colors (matches the palette already used across the testers).
ELTEC_BLUE = "#1d4aa8"
ELTEC_BLUE_DARK = "#1e3a8a"
ELTEC_BLUE_LIGHT = "#dbeafe"
ELTEC_RED = "#ef2b45"
PAGE_BG = "#f4f6f8"
PASS_BG = "#dcfce7"
PASS_FG = "#14532d"
PASS_ACCENT = "#16a34a"
FAIL_BG = "#fee2e2"
FAIL_FG = "#991b1b"
FAIL_ACCENT = "#dc2626"
NEUTRAL_BG = "#e8edf3"
NEUTRAL_FG = "#1f2937"
WAVE_BG = "#0b1120"

# Extra tones for the polished technician layout.
CARD_BG = "#ffffff"
CARD_BORDER = "#d4dde8"
HEADER_FG = "#ffffff"
HEADER_SUB_FG = "#ccdcf5"
MUTED_FG = "#5b6b7b"
STEP_IDLE = "#cbd5e1"
STEP_IDLE_FG = "#94a3b8"
PRIMARY_DISABLED = "#a9bcd9"

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
# Look for the logo next to this script and in an assets/ folder at any ancestor
# directory (the shared repo-root assets/ lives a few levels up now that the app
# is nested under tech_app/v3_emitter/), so it is found from either location.
LOGO_CANDIDATES = [ASSETS_DIR / "eltec_logo.png"] + [
    parent / "assets" / "eltec_logo.png"
    for parent in Path(__file__).resolve().parents
]


def find_logo_path() -> Path | None:
    for candidate in LOGO_CANDIDATES:
        if candidate.exists():
            return candidate
    return None

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
]


# --------------------------------------------------------------------------- #
# Results location + batch helpers
# --------------------------------------------------------------------------- #
def results_root_dir() -> Path:
    # Each tester version keeps its data in its own subfolder so results can be
    # tracked and analyzed per version. Autosave and waveform-snapshot folders
    # derive from this path, so they follow automatically.
    return Path.home() / "Documents" / "Eltec_406MCA_Test_Results" / "v3_emitter"


def safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    return cleaned.strip("_") or "UNLABELED"


def batch_results_path(batch_number: str) -> Path:
    return results_root_dir() / f"406mca_emitter_lot_{safe_filename_part(batch_number)}.csv"


def batch_autosave_path(batch_number: str) -> Path:
    safe = safe_filename_part(batch_number)
    return results_root_dir() / "autosave" / f"emitter_lot_{safe}_current_sensor.json"


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
    with csv_path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
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
        "AIN2": "Bottom trace (PWM sync)",
    }
    write_rgb_png(snapshot_path, width, height, pixels, metadata)


def save_waveform_snapshot_image(batch_number: str, sensor_id: str, metrics: WaveformMetrics | None, title: str, detail_lines: list[str], filename_suffix: str) -> Path | None:
    if metrics is None or metrics.waveform_v.size == 0:
        return None
    snapshot_dir = results_root_dir() / "waveform_snapshots" / f"lot_{safe_filename_part(batch_number)}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = snapshot_dir / f"{safe_filename_part(sensor_id)}_{timestamp}_{safe_filename_part(filename_suffix)}.png"
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
        axes[1].set_ylabel("AIN2 V (PWM)")
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
# LabJack device with PWM emitter + buffered-offset read on AIN0
# --------------------------------------------------------------------------- #
class EmitterLabJackT7(LabJackT7):
    """LabJackT7 plus an AIN0 offset read and a single-shot waveform frame read."""

    def read_offset_voltage(self, waveform_range_v: float, samples: int = OFFSET_READ_SAMPLES, delay_s: float = OFFSET_READ_DELAY_S) -> float:
        if self.handle is None:
            raise RuntimeError("LabJack is not connected.")
        self.configure_analog_inputs(waveform_range_v=waveform_range_v)
        readings: list[float] = []
        for _ in range(max(1, int(samples))):
            value = float(ljm.eReadName(self.handle, WAVEFORM_CHANNEL))
            if value > -9998.0 and math.isfinite(value):
                readings.append(value)
            if delay_s > 0:
                time.sleep(delay_s)
        if not readings:
            raise RuntimeError("No valid AIN0 offset readings were captured.")
        return float(np.median(np.asarray(readings, dtype=float)))

    def read_battery_voltage(self, samples: int = BATTERY_READ_SAMPLES, delay_s: float = BATTERY_READ_DELAY_S) -> float:
        """Read the 9V supply on AIN1 (via the 100k/100k divider) and scale back to Vbat."""
        if self.handle is None:
            raise RuntimeError("LabJack is not connected.")
        # Configure AIN1 as a single-ended, high-resolution DC read. This is a
        # command-response read, separate from the AIN0/AIN2 waveform stream, so
        # it does not disturb the sensor capture.
        for name, value in (
            (f"{BATTERY_CHANNEL}_NEGATIVE_CH", 199),
            (f"{BATTERY_CHANNEL}_RANGE", BATTERY_RANGE_V),
            (f"{BATTERY_CHANNEL}_RESOLUTION_INDEX", BATTERY_RESOLUTION_INDEX),
            (f"{BATTERY_CHANNEL}_SETTLING_US", BATTERY_SETTLING_US),
        ):
            ljm.eWriteName(self.handle, name, value)
        readings: list[float] = []
        for _ in range(max(1, int(samples))):
            value = float(ljm.eReadName(self.handle, BATTERY_CHANNEL))
            if value > -9998.0 and math.isfinite(value):
                readings.append(value)
            if delay_s > 0:
                time.sleep(delay_s)
        if not readings:
            raise RuntimeError("No valid AIN1 battery readings were captured.")
        divided_v = float(np.median(np.asarray(readings, dtype=float)))
        return divided_v * BATTERY_DIVIDER_RATIO * BATTERY_CALIBRATION

    def read_waveform_frame(
        self,
        cycles: int,
        waveform_range_v: float,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
    ) -> tuple[np.ndarray, np.ndarray, float]:
        if self.handle is None:
            raise RuntimeError("LabJack is not connected.")
        self.configure_analog_inputs(waveform_range_v=waveform_range_v)
        scan_names = [SYNC_CHANNEL] + [WAVEFORM_CHANNEL] * WAVEFORM_SETTLING_READS
        scan_list = ljm.namesToAddresses(len(scan_names), scan_names)[0]
        scans_per_read = max(20, int(sample_rate_hz / 10.0))
        target_scans = int(math.ceil((cycles / expected_frequency_hz) * sample_rate_hz))
        waveform: list[float] = []
        sync: list[float] = []
        actual_scan_rate = float(sample_rate_hz)
        try:
            actual_scan_rate = float(
                ljm.eStreamStart(self.handle, scans_per_read, len(scan_names), scan_list, float(sample_rate_hz))
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


# --------------------------------------------------------------------------- #
# Battery watcher helpers
# --------------------------------------------------------------------------- #
class BatteryTooLowError(RuntimeError):
    """Raised mid-measurement when the 9V supply is at/below the block threshold."""

    def __init__(self, battery_v: float) -> None:
        super().__init__(f"Battery too low to test: {battery_v:.2f} V (minimum {BATTERY_MIN_V:.1f} V).")
        self.battery_v = battery_v


def battery_state_for(battery_v: float | None) -> str:
    """Classify a battery reading as 'low', 'warn', 'ok', or 'unknown'."""
    if battery_v is None:
        return "unknown"
    if battery_v <= BATTERY_MIN_V:
        return "low"
    if battery_v <= BATTERY_WARN_V:
        return "warn"
    return "ok"


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
# Guided emitter tester UI
# --------------------------------------------------------------------------- #
class EmitterTesterApp(tk.Tk):
    SETUP_STEP = "setup"
    LOAD_STEP = "load"
    RESULT_STEP = "result"

    def __init__(self) -> None:
        super().__init__()
        self.title("Eltec 406MCA Emitter Tester")
        self.minsize(1040, 720)

        self.device: EmitterLabJackT7 | None = None
        self.hardware_lock = threading.Lock()
        self.busy = False
        self.measuring = False
        self.step = self.SETUP_STEP
        self.measure_token = 0

        # 9V battery watcher state.
        self.battery_v: float | None = None
        self.battery_state = "unknown"  # "ok" | "warn" | "low" | "unknown"
        self.battery_checking = False

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
        self.preview_waveform: np.ndarray = np.array([], dtype=float)
        self.preview_sync: np.ndarray = np.array([], dtype=float)
        self.snapshot_paths: list[Path] = []

        self.logo_image: tk.PhotoImage | None = None
        self.wave_canvas: tk.Canvas | None = None
        self.default_focus_widget: tk.Widget | None = None
        self.advanced_visible = False
        self.advanced_frame: tk.Frame | None = None
        self.adv_toggle_label: tk.Label | None = None
        self.progress_items: dict[str, dict] = {}

        self._build_variables()
        self._build_style()
        self._load_logo()
        self._build_layout()

        self.bind("<Return>", self.on_enter_key)
        self.bind("<KP_Enter>", self.on_enter_key)
        self.bind("<Escape>", self.on_escape_key)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.render_step()
        self.after(200, self.startup_probe)

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
        self.notes_var = tk.StringVar(value="")

        self.status_var = tk.StringVar(value="Checking LabJack...")
        self.battery_status_var = tk.StringVar(value="Battery: --")
        self.measure_status_var = tk.StringVar(value="")
        self.comment_status_var = tk.StringVar(value="")
        self.snapshot_status_var = tk.StringVar(value="")

    def _build_style(self) -> None:
        self.configure(bg=PAGE_BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=PAGE_BG)
        style.configure("TLabel", background=PAGE_BG, font=("Segoe UI", 14))
        style.configure("Muted.TLabel", background=PAGE_BG, foreground=MUTED_FG, font=("Segoe UI", 11))
        style.configure("Card.TLabel", background=CARD_BG, foreground=MUTED_FG, font=("Segoe UI", 11))

        # Primary call-to-action button (the blue "Next/Measure/Save" button).
        style.configure("Primary.TButton", font=("Segoe UI", 15, "bold"), padding=(20, 12),
                        background=ELTEC_BLUE, foreground="#ffffff", borderwidth=0, focuscolor=ELTEC_BLUE)
        style.map(
            "Primary.TButton",
            background=[("disabled", PRIMARY_DISABLED), ("active", ELTEC_BLUE_DARK), ("pressed", ELTEC_BLUE_DARK)],
            foreground=[("disabled", "#eef2f7")],
        )
        # Neutral / secondary buttons.
        style.configure("Large.TButton", font=("Segoe UI", 14, "bold"), padding=(16, 11))
        style.configure("Ghost.TButton", font=("Segoe UI", 14), padding=(14, 10),
                        background="#e7edf5", foreground=ELTEC_BLUE_DARK, borderwidth=0)
        style.map("Ghost.TButton", background=[("active", "#d6e0ee"), ("disabled", PAGE_BG)],
                  foreground=[("disabled", "#aeb9c5")])
        style.configure("Small.TButton", font=("Segoe UI", 12, "bold"), padding=(10, 7))

        style.configure("TCheckbutton", background=PAGE_BG, font=("Segoe UI", 12))
        style.configure("TCombobox", font=("Segoe UI", 18))
        style.configure("TEntry", font=("Segoe UI", 20))
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 18))

    def _load_logo(self) -> None:
        logo_path = find_logo_path()
        if logo_path is None:
            self.logo_image = None
            return
        try:
            self.logo_image = tk.PhotoImage(file=str(logo_path))
            # Shrink very large logos so the header stays compact.
            while self.logo_image.height() > 90:
                self.logo_image = self.logo_image.subsample(2, 2)
            self.iconphoto(False, self.logo_image)
        except Exception:
            self.logo_image = None

    def create_logo_widget(self, parent: tk.Widget, bg: str = PAGE_BG) -> tk.Widget:
        if self.logo_image is not None:
            return tk.Label(parent, image=self.logo_image, bg=bg)
        # Vector fallback: drawn ELTEC mark.
        logo = tk.Canvas(parent, width=150, height=76, bg=bg, highlightthickness=0)
        logo.create_oval(38, 8, 132, 68, fill=ELTEC_BLUE, outline=ELTEC_BLUE)
        logo.create_line(48, 20, 122, 20, fill="#ffffff", width=2)
        logo.create_line(48, 56, 122, 56, fill="#ffffff", width=2)
        logo.create_rectangle(12, 29, 142, 48, fill=bg, outline=bg)
        logo.create_text(76, 39, text="ELTEC", fill=ELTEC_RED, font=("Segoe UI", 26, "bold italic"))
        logo.create_text(119, 14, text="TM", fill=ELTEC_BLUE, font=("Segoe UI", 6, "bold"))
        return logo

    # ----- layout ----- #
    def _build_layout(self) -> None:
        # ----- top app bar (Eltec blue) ----- #
        header = tk.Frame(self, bg=ELTEC_BLUE)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(2, weight=1)
        badge = tk.Frame(header, bg="#ffffff")
        badge.grid(row=0, column=0, rowspan=2, sticky="w", padx=(18, 16), pady=14)
        self.create_logo_widget(badge, bg="#ffffff").grid(row=0, column=0, padx=12, pady=8)
        tk.Label(header, text="406MCA Emitter Tester", bg=ELTEC_BLUE, fg=HEADER_FG,
                 font=("Segoe UI", 25, "bold")).grid(row=0, column=2, sticky="sw", pady=(16, 0))
        tk.Label(header, textvariable=self.status_var, bg=ELTEC_BLUE, fg=HEADER_SUB_FG,
                 font=("Segoe UI", 12)).grid(row=1, column=2, sticky="nw", pady=(1, 16))
        # Battery watcher pill (recolored by state in _apply_battery_header_style).
        self.battery_header_label = tk.Label(
            header, textvariable=self.battery_status_var, bg=ELTEC_BLUE_DARK, fg=HEADER_FG,
            font=("Segoe UI", 13, "bold"), padx=14, pady=6,
        )
        self.battery_header_label.grid(row=0, column=3, rowspan=2, sticky="e", padx=(16, 18))
        self._apply_battery_header_style()
        accent = tk.Frame(self, bg=ELTEC_BLUE_DARK, height=3)
        accent.grid(row=1, column=0, sticky="ew")
        accent.grid_propagate(False)

        body = ttk.Frame(self, padding=(18, 14, 18, 14))
        body.grid(row=2, column=0, sticky="nsew")
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)

        # ----- progress sidebar with numbered chips ----- #
        sidebar = ttk.Frame(body, padding=(2, 6, 16, 6))
        sidebar.grid(row=0, column=0, sticky="nsw")
        tk.Label(sidebar, text="PROGRESS", bg=PAGE_BG, fg=STEP_IDLE_FG,
                 font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 14))
        for row, (step, text) in enumerate(
            [
                (self.SETUP_STEP, "Batch info"),
                (self.LOAD_STEP, "Load sensor"),
                (self.RESULT_STEP, "Measure & result"),
            ],
            start=1,
        ):
            item = self._build_progress_item(sidebar, row, text)
            item["frame"].grid(row=row, column=0, sticky="ew", pady=5)
            self.progress_items[step] = item

        ttk.Separator(body, orient="vertical").grid(row=0, column=1, sticky="ns", padx=(0, 18))

        self.content = ttk.Frame(body, padding=(4, 2, 4, 2))
        self.content.grid(row=0, column=2, sticky="nsew")
        self.content.columnconfigure(0, weight=1)

    def _build_progress_item(self, parent: tk.Widget, number: int, text: str) -> dict:
        frame = tk.Frame(parent, bg=PAGE_BG)
        canvas = tk.Canvas(frame, width=36, height=36, bg=PAGE_BG, highlightthickness=0)
        canvas.grid(row=0, column=0, padx=(0, 12))
        circle = canvas.create_oval(4, 4, 32, 32, fill=STEP_IDLE, outline="")
        num = canvas.create_text(18, 18, text=str(number), fill="#ffffff", font=("Segoe UI", 14, "bold"))
        label = tk.Label(frame, text=text, bg=PAGE_BG, fg=STEP_IDLE_FG, font=("Segoe UI", 14))
        label.grid(row=0, column=1, sticky="w")
        return {"frame": frame, "canvas": canvas, "circle": circle, "num": num, "label": label, "number": number}

    # ----- step rendering ----- #
    def clear_content(self) -> None:
        self.wave_canvas = None
        for child in self.content.winfo_children():
            child.destroy()
        # Reset row weights so each step controls its own vertical slack.
        for row in range(0, 12):
            self.content.rowconfigure(row, weight=0)

    def update_progress_labels(self) -> None:
        order = [self.SETUP_STEP, self.LOAD_STEP, self.RESULT_STEP]
        current_index = order.index(self.step)
        for index, step in enumerate(order):
            item = self.progress_items.get(step)
            if item is None:
                continue
            canvas = item["canvas"]
            if index < current_index:
                canvas.itemconfigure(item["circle"], fill=PASS_ACCENT)
                canvas.itemconfigure(item["num"], text="✓")
                item["label"].configure(fg=PASS_FG, font=("Segoe UI", 14, "bold"))
            elif index == current_index:
                canvas.itemconfigure(item["circle"], fill=ELTEC_BLUE)
                canvas.itemconfigure(item["num"], text=str(item["number"]))
                item["label"].configure(fg=ELTEC_BLUE_DARK, font=("Segoe UI", 14, "bold"))
            else:
                canvas.itemconfigure(item["circle"], fill=STEP_IDLE)
                canvas.itemconfigure(item["num"], text=str(item["number"]))
                item["label"].configure(fg=STEP_IDLE_FG, font=("Segoe UI", 14))

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
        self.after_idle(self.focus_default_widget)

    def render_setup_step(self) -> None:
        head = ttk.Frame(self.content)
        head.grid(row=0, column=0, sticky="ew")
        tk.Label(head, text="Batch information", bg=PAGE_BG, fg=NEUTRAL_FG,
                 font=("Segoe UI", 30, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(head, text="Enter the batch number and your name, choose the filter, then press Enter.",
                 bg=PAGE_BG, fg=MUTED_FG, font=("Segoe UI", 13)).grid(row=1, column=0, sticky="w", pady=(4, 0))

        card, inner = self._make_card(self.content)
        card.grid(row=1, column=0, sticky="new", pady=(18, 0))
        inner.columnconfigure(1, weight=1)
        batch_entry = self._card_entry(inner, 0, "Batch number", self.batch_var)
        self.default_focus_widget = batch_entry
        self._card_entry(inner, 1, "Tester name", self.tester_var)
        filter_combo = self._card_combo(inner, 2, "Filter / setup", self.filter_var, list(FILTER_SPECS_MV.keys()))
        filter_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_filter_hint())
        tk.Label(inner, textvariable=self.filter_hint_var, bg=CARD_BG, fg=ELTEC_BLUE_DARK,
                 font=("Segoe UI", 12, "bold")).grid(row=3, column=1, sticky="w", pady=(0, 2))
        self.update_filter_hint()

        adv_container = ttk.Frame(self.content)
        adv_container.grid(row=2, column=0, sticky="new", pady=(16, 0))
        adv_container.columnconfigure(0, weight=1)
        self.adv_toggle_label = tk.Label(
            adv_container,
            text=("▾ " if self.advanced_visible else "▸ ") + "Advanced options",
            bg=PAGE_BG, fg=ELTEC_BLUE, font=("Segoe UI", 12, "bold"), cursor="hand2",
        )
        self.adv_toggle_label.grid(row=0, column=0, sticky="w")
        self.adv_toggle_label.bind("<Button-1>", self.toggle_advanced)
        self.advanced_frame = self._build_advanced_panel(adv_container)
        if self.advanced_visible:
            self.advanced_frame.grid(row=1, column=0, sticky="new", pady=(10, 0))

    # ----- card + advanced helpers ----- #
    def _make_card(self, parent: tk.Widget) -> tuple[tk.Frame, tk.Frame]:
        card = tk.Frame(parent, bg=CARD_BG, highlightbackground=CARD_BORDER,
                        highlightcolor=CARD_BORDER, highlightthickness=1, bd=0)
        card.columnconfigure(0, weight=1)
        inner = tk.Frame(card, bg=CARD_BG)
        inner.grid(row=0, column=0, sticky="nsew", padx=26, pady=22)
        return card, inner

    def _card_entry(self, parent: tk.Widget, row: int, label: str, variable: tk.StringVar) -> ttk.Entry:
        tk.Label(parent, text=label, bg=CARD_BG, fg=NEUTRAL_FG,
                 font=("Segoe UI", 16, "bold")).grid(row=row, column=0, sticky="w", padx=(0, 20), pady=12)
        entry = ttk.Entry(parent, textvariable=variable, font=("Segoe UI", 20))
        entry.grid(row=row, column=1, sticky="ew", pady=12)
        return entry

    def _card_combo(self, parent: tk.Widget, row: int, label: str, variable: tk.StringVar, values: list[str]) -> ttk.Combobox:
        tk.Label(parent, text=label, bg=CARD_BG, fg=NEUTRAL_FG,
                 font=("Segoe UI", 16, "bold")).grid(row=row, column=0, sticky="w", padx=(0, 20), pady=(12, 4))
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly",
                             font=("Segoe UI", 16), height=min(max(len(values), 5), 10))
        combo.grid(row=row, column=1, sticky="ew", pady=(12, 4))
        return combo

    def _build_advanced_panel(self, parent: tk.Widget) -> tk.Frame:
        panel = tk.Frame(parent, bg=PAGE_BG)
        panel.columnconfigure(1, weight=1)
        ttk.Checkbutton(panel, text="Simulator mode (no LabJack connected)",
                        variable=self.simulator_var).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(panel, text="Sim case", bg=PAGE_BG, fg=MUTED_FG,
                 font=("Segoe UI", 11)).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        ttk.Combobox(panel, textvariable=self.sim_case_var, values=SIM_CASES, state="readonly",
                     width=20).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(panel, text="Simulate low battery (test the change-battery lockout)",
                        variable=self.sim_low_battery_var,
                        command=self.refresh_battery).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Label(
            panel,
            text=(f"Rig wiring: AIN0 = buffered sensor (offset + AC), AIN1 = 9V battery via "
                  f"100k/100k divider (÷2), AIN2 = PWM sync, {EMITTER_PWM_CHANNEL} = MOSFET gate. "
                  f"Emitter driven at {EMITTER_PWM_FREQUENCY_HZ:g} Hz, {EMITTER_PWM_DUTY_CYCLE:g}% duty (fixed). "
                  f"Sensor read through a unity-gain buffer, gain = 1. Testing is blocked below "
                  f"{BATTERY_MIN_V:.1f} V on the battery."),
            bg=PAGE_BG, fg=MUTED_FG, font=("Segoe UI", 10), wraplength=640, justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        return panel

    def toggle_advanced(self, _event: tk.Event | None = None) -> None:
        self.advanced_visible = not self.advanced_visible
        if self.advanced_frame is None or self.adv_toggle_label is None:
            return
        if self.advanced_visible:
            self.advanced_frame.grid(row=1, column=0, sticky="new", pady=(10, 0))
            self.adv_toggle_label.configure(text="▾ Advanced options")
        else:
            self.advanced_frame.grid_remove()
            self.adv_toggle_label.configure(text="▸ Advanced options")

    def update_filter_hint(self) -> None:
        min_mv = FILTER_SPECS_MV.get(self.filter_var.get())
        self.filter_hint_var.set("" if min_mv is None else f"Minimum sensitivity to pass: {min_mv:.1f} mV")

    def render_load_step(self) -> None:
        tk.Label(self.content, text=f"Load sensor {self.current_sensor_id}", bg=PAGE_BG, fg=NEUTRAL_FG,
                 font=("Segoe UI", 30, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(self.content, text=f"Batch {self.batch_number}    •    Filter: {self.filter_setup}",
                 bg=PAGE_BG, fg=MUTED_FG, font=("Segoe UI", 13)).grid(row=1, column=0, sticky="w", pady=(4, 0))

        panel = tk.Frame(self.content, bg=ELTEC_BLUE_LIGHT, highlightbackground="#bcd4f5",
                         highlightcolor="#bcd4f5", highlightthickness=1, bd=0)
        panel.grid(row=2, column=0, sticky="ew", pady=(22, 0))
        panel.columnconfigure(1, weight=1)
        icon = tk.Canvas(panel, width=170, height=120, bg=ELTEC_BLUE_LIGHT, highlightthickness=0)
        icon.grid(row=0, column=0, padx=(26, 18), pady=24)
        icon.create_rectangle(30, 22, 140, 94, fill=ELTEC_BLUE, outline=ELTEC_BLUE_DARK, width=3)
        icon.create_rectangle(54, 42, 116, 74, fill="#f8fafc", outline="#bfdbfe", width=2)
        icon.create_oval(62, 47, 108, 69, fill=ELTEC_RED, outline="#991b1b", width=2)
        icon.create_line(24, 98, 146, 98, fill=ELTEC_BLUE_DARK, width=5)
        icon.create_text(85, 105, text="RIG", fill=ELTEC_BLUE_DARK, font=("Segoe UI", 13, "bold"))
        text_col = tk.Frame(panel, bg=ELTEC_BLUE_LIGHT)
        text_col.grid(row=0, column=1, sticky="w", padx=(0, 26), pady=24)
        tk.Label(text_col, text="Place the sensor in the testing rig", bg=ELTEC_BLUE_LIGHT,
                 fg=ELTEC_BLUE_DARK, font=("Segoe UI", 26, "bold")).pack(anchor="w")
        tk.Label(text_col, text="Then press Enter to read the offset and run the emitter test.",
                 bg=ELTEC_BLUE_LIGHT, fg="#3a5a9c", font=("Segoe UI", 13)).pack(anchor="w", pady=(6, 0))
        self._build_battery_banner()

    def render_result_step(self) -> None:
        if self.measuring:
            self.render_measuring_view()
        elif self.last_result is not None:
            self.render_result_view()
        else:
            tk.Label(self.content, text=f"{self.current_sensor_id}: ready to measure", bg=PAGE_BG,
                     fg=NEUTRAL_FG, font=("Segoe UI", 28, "bold")).grid(row=0, column=0, sticky="w")
            tk.Label(self.content, text="Press Enter (or Measure) to run the emitter test.", bg=PAGE_BG,
                     fg=MUTED_FG, font=("Segoe UI", 13)).grid(row=1, column=0, sticky="w", pady=(8, 0))
            ttk.Button(self.content, text="Measure", command=self.run_measurement, style="Primary.TButton").grid(row=2, column=0, sticky="w", pady=(20, 0))
        if not self.measuring:
            self._build_battery_banner()

    def _build_battery_banner(self) -> None:
        """Show a yellow low-warning strip or a red block, with a re-check button."""
        if self.battery_state not in ("warn", "low"):
            return
        low = self.battery_state == "low"
        bg = FAIL_BG if low else "#fef9c3"
        fg = FAIL_FG if low else "#854d0e"
        accent = FAIL_ACCENT if low else "#f59e0b"
        volts = "" if self.battery_v is None else f" ({self.battery_v:.2f} V)"
        if low:
            message = f"Change the 9V battery{volts}. Testing is blocked until you replace it and re-check."
        else:
            message = f"Battery is getting low{volts}. Swap it soon — testing is still allowed."
        banner = tk.Frame(self.content, bg=bg, highlightbackground=accent,
                          highlightcolor=accent, highlightthickness=1, bd=0)
        banner.grid(row=8, column=0, sticky="ew", pady=(14, 0))
        banner.columnconfigure(1, weight=1)
        tk.Frame(banner, bg=accent, width=8).grid(row=0, column=0, sticky="ns")
        tk.Label(banner, text=message, bg=bg, fg=fg, font=("Segoe UI", 13, "bold"),
                 wraplength=680, justify="left", padx=14, pady=12).grid(row=0, column=1, sticky="w")
        ttk.Button(banner, text="Re-check battery", command=self.refresh_battery,
                   style="Small.TButton").grid(row=0, column=2, padx=(10, 14))

    def render_measuring_view(self) -> None:
        ttk.Label(self.content, text=f"{self.current_sensor_id}: measuring...", font=("Segoe UI", 30, "bold"), foreground=ELTEC_BLUE_DARK).grid(row=0, column=0, sticky="w")
        ttk.Label(self.content, textvariable=self.measure_status_var, font=("Segoe UI", 16)).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Checkbutton(self.content, text="Show live waveform while reading", variable=self.show_live_var, command=self.toggle_live_view).grid(row=2, column=0, sticky="nw", pady=(10, 0))
        if self.show_live_var.get():
            self._build_wave_canvas(row=3)

    def render_result_view(self) -> None:
        result = self.last_result
        passed = result.passed
        accent = PASS_ACCENT if passed else FAIL_ACCENT
        banner_bg = PASS_BG if passed else FAIL_BG
        banner_fg = PASS_FG if passed else FAIL_FG

        banner = tk.Frame(self.content, bg=banner_bg)
        banner.grid(row=0, column=0, sticky="ew")
        banner.columnconfigure(3, weight=1)
        tk.Frame(banner, bg=accent, width=10).grid(row=0, column=0, rowspan=2, sticky="ns")
        tk.Label(banner, text="✓" if passed else "✕", bg=banner_bg, fg=accent,
                 font=("Segoe UI", 40, "bold")).grid(row=0, column=1, rowspan=2, padx=(20, 16), pady=12)
        tk.Label(banner, text="PASS" if passed else "FAIL", bg=banner_bg, fg=banner_fg,
                 font=("Segoe UI", 34, "bold")).grid(row=0, column=2, sticky="sw", pady=(14, 0))
        tk.Label(banner, text=self.current_sensor_id, bg=banner_bg, fg=banner_fg,
                 font=("Segoe UI", 15)).grid(row=1, column=2, sticky="nw", pady=(0, 14))

        tiles = tk.Frame(self.content, bg=PAGE_BG)
        tiles.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        for column in range(3):
            tiles.columnconfigure(column, weight=1)
        offset_text = "Not measured" if result.offset_v is None else f"{result.offset_v:.3f} V"
        offset_ok = result.offset_v is not None and OFFSET_MIN_V <= result.offset_v <= OFFSET_MAX_V
        sens_text = "Not measured" if result.sensitivity_mv is None else f"{result.sensitivity_mv:.2f} mV"
        min_mv = FILTER_SPECS_MV.get(self.filter_setup)
        sens_ok = result.sensitivity_mv is not None and min_mv is not None and result.sensitivity_mv >= min_mv
        pol_verdict = polarity_good_bad(result.polarity)
        self._result_tile(tiles, 0, "Offset", offset_text, offset_ok)
        self._result_tile(tiles, 1, "Sensitivity", sens_text, sens_ok)
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
        ttk.Label(self.content, text="     |     ".join(detail_bits), style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))

        if result.fail_reasons:
            reasons = tk.Text(self.content, height=4, wrap="word", font=("Segoe UI", 12),
                              relief="flat", bd=0, bg="#fff5f5", fg=FAIL_FG, padx=12, pady=10,
                              highlightbackground="#f3c2c2", highlightthickness=1)
            reasons.grid(row=3, column=0, sticky="ew", pady=(10, 0))
            reasons.insert("1.0", "\n".join(f"•  {reason}" for reason in result.fail_reasons))
            reasons.configure(state="disabled")

        tools = ttk.Frame(self.content)
        tools.grid(row=4, column=0, sticky="w", pady=(14, 0))
        ttk.Button(tools, text="Comment", command=self.open_comment_window, style="Small.TButton").grid(row=0, column=0, padx=(0, 10))
        ttk.Button(tools, text="Capture waveform", command=self.capture_waveform_snapshot, style="Small.TButton").grid(row=0, column=1, padx=(0, 10))
        ttk.Button(tools, text="Re-measure", command=self.run_measurement, style="Small.TButton").grid(row=0, column=2, padx=(0, 10))
        ttk.Checkbutton(tools, text="Show waveform", variable=self.show_live_var, command=self.toggle_live_view).grid(row=0, column=3)
        ttk.Label(tools, textvariable=self.comment_status_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(tools, textvariable=self.snapshot_status_var, style="Muted.TLabel").grid(row=2, column=0, columnspan=4, sticky="w")

        if self.show_live_var.get():
            self._build_wave_canvas(row=5)
            self.redraw_waveform()

    def _result_tile(self, parent: tk.Frame, column: int, label: str, value: str, ok: bool) -> None:
        accent = PASS_ACCENT if ok else FAIL_ACCENT
        tile = tk.Frame(parent, bg=CARD_BG, highlightbackground=CARD_BORDER,
                        highlightcolor=CARD_BORDER, highlightthickness=1, bd=0)
        tile.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 12, 0))
        tile.columnconfigure(0, weight=1)
        strip = tk.Frame(tile, bg=accent, height=4)
        strip.grid(row=0, column=0, sticky="ew")
        strip.grid_propagate(False)
        body = tk.Frame(tile, bg=CARD_BG)
        body.grid(row=1, column=0, sticky="ew", padx=16, pady=(10, 14))
        tk.Label(body, text=label.upper(), bg=CARD_BG, fg="#64748b",
                 font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(body, text=value, bg=CARD_BG, fg=accent,
                 font=("Segoe UI", 26, "bold")).pack(anchor="w", pady=(4, 0))

    def _build_wave_canvas(self, row: int) -> None:
        self.content.rowconfigure(row, weight=1)
        wrapper = ttk.Frame(self.content)
        wrapper.grid(row=row, column=0, sticky="nsew", pady=(12, 0))
        wrapper.columnconfigure(0, weight=1)
        wrapper.rowconfigure(1, weight=1)
        ttk.Label(wrapper, text="Live waveform (AIN0 sensor + AIN2 PWM sync)", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        self.wave_canvas = tk.Canvas(wrapper, height=240, bg=WAVE_BG, highlightthickness=0)
        self.wave_canvas.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.wave_canvas.bind("<Configure>", lambda _event: self.redraw_waveform())
        self.redraw_waveform()

    # ----- navigation ----- #
    def render_navigation(self) -> None:
        # The waveform (when shown) takes the slack; otherwise the spacer does,
        # which keeps the step content at the top and the footer at the bottom.
        self.content.rowconfigure(9, weight=0 if self.wave_canvas is not None else 1)
        spacer = ttk.Frame(self.content)
        spacer.grid(row=9, column=0, sticky="nsew")
        footer = ttk.Frame(self.content)
        footer.grid(row=10, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        self.back_button = ttk.Button(footer, text="Back", command=self.go_back, style="Ghost.TButton")
        self.back_button.grid(row=0, column=0, sticky="w")
        self.secondary_button = ttk.Button(footer, text="Save + Exit Batch", command=self.save_and_end_batch, style="Large.TButton")
        self.secondary_button.grid(row=0, column=1, sticky="e", padx=(0, 10))
        self.primary_button = ttk.Button(footer, text="Next", command=self.go_next, style="Primary.TButton")
        self.primary_button.grid(row=0, column=2, sticky="e")

    def update_navigation_state(self) -> None:
        self.secondary_button.grid_remove()
        if self.step == self.SETUP_STEP:
            self.back_button.configure(state="disabled")
            self.primary_button.configure(text="Start (Enter)", state="disabled" if self.busy else "normal")
        elif self.step == self.LOAD_STEP:
            self.back_button.configure(state="disabled" if self.busy else "normal")
            # Hard block: cannot start a measurement while the battery is low.
            blocked = self.busy or self.battery_state == "low"
            measure_text = "Change battery to test" if self.battery_state == "low" else "Measure (Enter)"
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
        if isinstance(event.widget, ttk.Button):
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
            )
        except Exception as exc:
            messagebox.showerror("Could not save result", str(exc))
            return False
        self.result_saved = True
        self.delete_autosave()
        self.status_var.set(f"Saved {self.current_sensor_id}.")
        self.update_navigation_state()
        return True

    # ----- battery watcher ----- #
    def _update_battery_status_text(self) -> None:
        if self.battery_v is None:
            self.battery_status_var.set("Battery: checking…" if self.battery_checking else "Battery: --")
        elif self.battery_state == "ok":
            self.battery_status_var.set(f"Battery {self.battery_v:.1f} V  ✓")
        elif self.battery_state == "warn":
            self.battery_status_var.set(f"Battery low  {self.battery_v:.1f} V")
        else:
            self.battery_status_var.set(f"CHANGE BATTERY  {self.battery_v:.1f} V")

    def _apply_battery_header_style(self) -> None:
        label = getattr(self, "battery_header_label", None)
        if label is None or not label.winfo_exists():
            return
        palette = {
            "ok": (PASS_ACCENT, "#ffffff"),
            "warn": ("#f59e0b", "#ffffff"),
            "low": (FAIL_ACCENT, "#ffffff"),
            "unknown": (ELTEC_BLUE_DARK, HEADER_SUB_FG),
        }
        bg, fg = palette.get(self.battery_state, palette["unknown"])
        label.configure(bg=bg, fg=fg)

    def refresh_battery(self) -> None:
        """Read the 9V supply in the background and update the watcher state."""
        if self.battery_checking or self.busy or self.measuring:
            return
        self.battery_checking = True
        self._update_battery_status_text()
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
            self.after(0, lambda: self.on_battery_update(battery_v, error))

        threading.Thread(target=worker, daemon=True).start()

    def on_battery_update(self, battery_v: float | None, error: Exception | None = None) -> None:
        self.battery_checking = False
        if error is None and battery_v is not None:
            self.battery_v = battery_v
            self.battery_state = battery_state_for(battery_v)
        elif self.battery_v is None:
            # Never got a reading (e.g. device claimed): leave the pill neutral.
            self.battery_state = "unknown"
        self._update_battery_status_text()
        self._apply_battery_header_style()
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
        # Hard block: refuse to start a test on a known-low battery. The tech
        # must replace it and re-check (which clears the state) before testing.
        if self.battery_state == "low":
            self.status_var.set(
                f"Battery too low ({self.battery_v:.2f} V). Change the 9V battery and press “Re-check battery”."
                if self.battery_v is not None
                else "Battery too low. Change the 9V battery and press “Re-check battery”."
            )
            self.refresh_battery()
            return

        waveform_range_v = labjack_ain0_range_from_label(WAVEFORM_INPUT_RANGE_LABEL)
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
        self.measure_status_var.set("Reading DC offset (emitter off)...")
        self.measure_token += 1
        token = self.measure_token
        self.render_step()

        def push(callback) -> None:
            self.after(0, callback)

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
            except Exception as exc:
                push(lambda exc=exc: self.on_measure_error(token, exc))
            else:
                push(lambda: self.on_measure_done(token, metrics, final))

        threading.Thread(target=worker, daemon=True).start()

    def _hardware_measurement(self, filter_setup, waveform_range_v, pwm_channel, pwm_hz, pwm_duty, show_live, token, push):
        with self.hardware_lock:
            self.ensure_connected()
            device = self.device
            device.disable_emitter_pwm(pwm_channel)
            # Check the 9V supply before doing anything else: if it is too low,
            # bail out before measuring so no unreliable reading is recorded.
            battery_v = device.read_battery_voltage()
            push(lambda v=battery_v: self.on_battery_update(v))
            if battery_v <= BATTERY_MIN_V:
                raise BatteryTooLowError(battery_v)
            offset_v = device.read_offset_voltage(waveform_range_v=waveform_range_v)
            push(lambda v=offset_v: self.on_offset_update(token, v))

            device.configure_emitter_pwm(channel=pwm_channel, frequency_hz=pwm_hz, duty_cycle_percent=pwm_duty)
            push(lambda: self.set_measure_status(token, "Emitter PWM on. Measuring sensitivity and polarity..."))
            try:
                if show_live:
                    for _ in range(PREVIEW_FRAMES):
                        if token != self.measure_token:
                            break
                        wf, sync, _rate = device.read_waveform_frame(PREVIEW_CYCLES, waveform_range_v)
                        push(lambda wf=wf, sync=sync: self.on_preview_frame(token, wf, sync))
                waveform, sync, actual_rate = device.read_waveform_stream(
                    sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
                    expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
                    sync_edge=PROCEDURE_SYNC_EDGE,
                    waveform_range_v=waveform_range_v,
                )
            finally:
                device.disable_emitter_pwm(pwm_channel)

        metrics = analyze_waveform(
            waveform_v=waveform,
            sync_v=sync,
            sample_rate_hz=actual_rate,
            am502_gain=RIG_GAIN,
            sync_edge=PROCEDURE_SYNC_EDGE,
            input_range_v=waveform_range_v,
        )
        metrics.offset_v = offset_v
        final = evaluate_result(offset_v, metrics, filter_setup)
        # Reject captures that are mostly noise (e.g. the emitter is not being
        # driven), which amplitude/polarity alone can let slip through.
        final = apply_signal_quality_gate(final, metrics)
        return metrics, final, offset_v

    def _simulate_measurement(self, filter_setup, sim_case, sim_low_battery, waveform_range_v, show_live, token, push):
        # Mirror the hardware battery gate so the lockout is testable without a T7.
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
        metrics = analyze_waveform(
            waveform_v=waveform,
            sync_v=sync,
            sample_rate_hz=actual_rate,
            am502_gain=RIG_GAIN,
            sync_edge=PROCEDURE_SYNC_EDGE,
            input_range_v=waveform_range_v,
        )
        metrics.offset_v = offset_v
        final = evaluate_result(offset_v, metrics, filter_setup)
        # The SNR gate is intentionally NOT applied here: the simulator injects a
        # large fixed noise so synthetic SNR is not comparable to hardware, and a
        # synthetic capture is coherent by construction (it cannot exhibit the
        # undriven-emitter failure the gate exists to catch).
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
        self._update_battery_status_text()
        self._apply_battery_header_style()
        self.status_var.set(f"Battery too low ({battery_v:.2f} V). Change the 9V battery to continue.")
        self.measure_status_var.set("")
        self.render_step()
        messagebox.showwarning(
            "Change the battery",
            f"The 9V battery is at {battery_v:.2f} V, at or below the {BATTERY_MIN_V:.1f} V minimum.\n\n"
            "Replace the battery, then press “Re-check battery” before testing again.",
        )

    def on_measure_error(self, token: int, exc: Exception) -> None:
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        text = str(exc)
        if "1230" in text or "CLAIMED" in text.upper():
            text = "The T7 is claimed by another LabJack program. Close LJStreamM/Kipling and try again."
        self.status_var.set(text)
        self.measure_status_var.set("")
        self.render_step()
        messagebox.showerror("Measurement problem", text)

    def toggle_live_view(self) -> None:
        if self.step == self.RESULT_STEP:
            self.render_step()

    # ----- waveform drawing (adapted from the signal monitor UI) ----- #
    def redraw_waveform(self) -> None:
        canvas = self.wave_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        waveform = self.preview_waveform
        sync = self.preview_sync
        if waveform.size < 2:
            canvas.create_text(width / 2, height / 2, text="Waveform appears here while reading.", fill="#cbd5e1", font=("Segoe UI", 13))
            return

        n = len(waveform)
        idx = np.linspace(0, n - 1, min(n, max(2, width - 20))).astype(int)
        x = idx / max(1, n - 1) * (width - 24) + 12
        wave_min, wave_max = float(np.min(waveform)), float(np.max(waveform))
        if abs(wave_max - wave_min) < 1e-9:
            wave_min -= 0.5
            wave_max += 0.5
        top = 18
        wave_bottom = int(height * 0.66)
        y = wave_bottom - (waveform[idx] - wave_min) / (wave_max - wave_min) * (wave_bottom - top)
        points: list[float] = []
        for px, py in zip(x, y):
            points.extend([float(px), float(py)])
        canvas.create_line(points, fill="#38bdf8", width=2)

        if sync.size == n:
            sync_top = int(height * 0.74)
            sync_bottom = height - 18
            sync_min, sync_max = float(np.min(sync)), float(np.max(sync))
            if abs(sync_max - sync_min) < 1e-9:
                sync_min -= 0.5
                sync_max += 0.5
            sync_y = sync_bottom - (sync[idx] - sync_min) / (sync_max - sync_min) * (sync_bottom - sync_top)
            sync_points: list[float] = []
            for px, py in zip(x, sync_y):
                sync_points.extend([float(px), float(py)])
            canvas.create_line(sync_points, fill="#facc15", width=1)
            canvas.create_text(12, sync_top, anchor="nw", text="AIN2 PWM sync", fill="#facc15", font=("Segoe UI", 10, "bold"))

        canvas.create_text(12, 12, anchor="nw", text="AIN0 sensor", fill="#38bdf8", font=("Segoe UI", 10, "bold"))
        canvas.create_text(width - 12, 12, anchor="ne", text=f"{wave_min:.4f} to {wave_max:.4f} V", fill="#cbd5e1", font=("Segoe UI", 10))

    # ----- comment / snapshot ----- #
    def open_comment_window(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(f"Comment for {self.current_sensor_id}")
        dialog.minsize(620, 420)
        dialog.configure(bg=PAGE_BG)
        dialog.transient(self)

        frame = ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=f"{self.current_sensor_id} comment", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        text = tk.Text(frame, wrap="word", font=("Segoe UI", 13), undo=True)
        text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", self.notes_var.get())

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(14, 0))

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
        ttk.Button(buttons, text="Cancel", command=dialog.destroy, style="Small.TButton").grid(row=0, column=0, padx=(0, 10))
        ttk.Button(buttons, text="Save Comment (Enter)", command=save_comment, style="Small.TButton").grid(row=0, column=1)
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
        summary.minsize(900, 460)
        summary.configure(bg=PAGE_BG)
        ttk.Label(summary, text=f"Batch {batch_number} results", font=("Segoe UI", 24, "bold")).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 8))

        frame = ttk.Frame(summary, padding=(18, 0, 18, 16))
        frame.grid(row=1, column=0, sticky="nsew")
        summary.rowconfigure(1, weight=1)
        summary.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        style = ttk.Style(summary)
        style.configure("Summary.Treeview", font=("Segoe UI", 12), rowheight=30)
        style.configure("Summary.Treeview.Heading", font=("Segoe UI", 12, "bold"))
        columns = ("sensor", "offset", "sensitivity", "polarity", "result")
        headings = {"sensor": "Sensor", "offset": "Offset", "sensitivity": "Sensitivity", "polarity": "Polarity", "result": "Result"}
        tree = ttk.Treeview(frame, columns=columns, show="headings", style="Summary.Treeview", height=14)
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=150, anchor="center", stretch=True)
        tree.tag_configure("pass", background=PASS_BG)
        tree.tag_configure("fail", background=FAIL_BG)
        for row in self._read_summary_rows(csv_path):
            tag = "pass" if row[-1] == "PASS" else "fail"
            tree.insert("", "end", values=row, tags=(tag,))
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=y_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        ttk.Button(summary, text="Close", command=summary.destroy, style="Large.TButton").grid(row=2, column=0, sticky="e", padx=18, pady=(0, 16))

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
            self.device = EmitterLabJackT7()
        self.device.connect()

    def startup_probe(self) -> None:
        ok, message = probe_labjack_status()
        self.status_var.set(message)
        if not ok:
            self.simulator_var.set(True)
        # In simulator mode we can show the battery pill immediately; with real
        # hardware we defer the first read to the load step (fixture wired) to
        # avoid a false low reading on a floating AIN1.
        if self.simulator_var.get():
            self.refresh_battery()

    def on_close(self) -> None:
        self.measure_token += 1
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
