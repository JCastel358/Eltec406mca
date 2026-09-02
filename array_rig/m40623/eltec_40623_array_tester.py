"""Eltec 40623 Array Tester - fifty detectors at once on the DAQ array rig (TP120).

The model application of the array rig for the Eltec 40623 (test procedure
TP120 rev W, ``docs/TP120(40623).pdf``). It is normally launched from the
array selector (``array_rig/eltec_array_tester.py``) and runs standalone with
this directory as cwd. It measures the two TP120 tests that need no emitter:

* **Offset check** - as soon as the rig is powered every position's DC
  offset is read continuously (Phase A, "Load & offset"). Parts over the
  TP120 maximum (1.2 V) or railed turn RED immediately, so the technician
  pulls them BEFORE the noise capture and no time is wasted on them.
  Low reads are not a verdict at this point: offsets settle upward for tens
  of seconds after power-on (405 M22 lot-500 observation), so LO and D are
  judged on the SETTLED reading at the end of the noise capture.
* **Noise** - after "Lock tray" (occupancy frozen, sensor numbers assigned,
  HO parts recorded) the rig waits the TP120 stabilisation time, then
  streams all fifty channels wideband (1000 scans/s per channel) for the
  TP120 hold time (60 s) and judges each channel's windowed pk-pk in the
  single rig's 0.85-22 Hz band (``array_analysis``). The raw capture of all
  fifty channels is saved with every tray so any band or limit decided
  later can be replayed on real parts without re-measuring.

Verdict status: **CALIBRATION PENDING / PROVISIONAL**. TP120's noise limits
(10.0-37.9 mV) are DMM readings behind the legacy amplifier 9000232 and
rectifier-hold 9000272; no pin-level equivalent exists yet, so noise tiles
show the measured value and "no limit yet" until the paired lot derives the
chain factor (``engineer_tools/array_noise_parity.py``). The offset limits
(0.3-1.2 V) are applied, stamped provisional until the PCB loading is
confirmed against fixture 9000054. Every CSV row carries
``calibration_status`` / ``calibration_id`` / ``verdict_status``.

Sensitivity and polarity (TP120's 3 Hz chopper test) are NOT implemented:
the emitter board does not exist yet. The step rail shows the disabled step
and ``ArrayTesterApp.drive`` is the slot the emitter driver will plug into.

Layout of this file (mirrors the single-rig testers): constants -> Tk-free
core (paths, CSV, npz, capture procedure, ``TrayController``) -> Tk GUI.
The core is what the tests drive; the GUI only renders it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

import numpy as np

_MODEL_DIR = Path(__file__).resolve().parent
_RIG_PACKAGE_DIR = _MODEL_DIR.parent
for _entry in (str(_MODEL_DIR), str(_RIG_PACKAGE_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import array_analysis as aa  # noqa: E402
import daq_backend as daq  # noqa: E402
import tray_history  # noqa: E402

# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
APP_TITLE = "Eltec 40623 Array Tester"
# 0.1 (2026-09-02): first build. Offset + noise per TP120 on the DAQ array,
# CALIBRATION PENDING (noise limits None, offset limits provisional).
APP_VERSION = "0.1"
MODEL_NAME = "40623"
PROCEDURE = "TP120 rev W"
RESULTS_ROOT_NAME = "Eltec_40623_Test_Results"
RESULTS_MODEL_DIR = "40623_array_daq"
RESULTS_PREFIX = "40623_array"

# --------------------------------------------------------------------------- #
# DAQ acquisition constants (bench-tunable; every value is recorded per row)
# --------------------------------------------------------------------------- #
# 0-5 V unipolar on every 4-channel group: the TP120 offset window (0.3-1.2 V)
# and a railed bad part (~5 V) are both visible, at 76.3 uV per LSB. A smaller
# range would clip the offsets; a bigger one halves the resolution.
DAQ_RANGE_CODE = 2
# 1000 scans/s per channel so the noise pipeline runs with the single rig's
# exact numbers (decimation 20, 621 taps, 310-sample edge context).
DAQ_SCAN_HZ = 1000.0
# Hardware oversample = extra conversions per channel per scan. 3 -> 4
# conversions; the first (right after the multiplexer hop, the one most likely
# unsettled) is dropped and the other three averaged. 50 ch x 4 x 1000/s =
# 200 kS/s aggregate (40 % of the ADC ceiling) and 400 KB/s over USB (~40 % of
# the practical full-speed ceiling). Proven or adjusted by the bench probe
# (`daq_bench_probe.py slots` / `stream 60`); fallbacks: oversample 2 or 1.
DAQ_OVERSAMPLE = 3
DAQ_DROP_CONVERSIONS_AFTER_MUX = 1
# Callback buffers: a multiple of 512 B (driver rule) AND of the 400-byte scan,
# ~0.16 s each; 32 of them = ~5 s of slack before the driver reports data loss.
STREAM_BUFFER_BYTES = 64_000
STREAM_BUFFER_COUNT = 32
# A stream that fails the integrity check (rate off by > 1 %, driver pool
# exhausted, callback error) is retried this many times before the tray is
# recorded NOT MEASURED - the single rig's stream-retry policy.
STREAM_RETRY_LIMIT = 2
# ADC_SetCal(":AUTO:") at connect: the "A" grade board's real-time calibration
# then corrects offset/gain against its onboard references.
DAQ_SELF_CALIBRATE_ON_CONNECT = True

# --------------------------------------------------------------------------- #
# Offset phase
# --------------------------------------------------------------------------- #
OFFSET_POLL_HZ = 2.0            # live tile refresh while loading
OFFSET_POLL_READS = 3           # median of N immediate scans per poll (the ESP32 rig's OFFSET? is a median too)
# The settled offset (the VERDICT) is the mean of the last seconds of the raw
# capture; the early value is the mean of the first seconds -> TP120's
# +/-0.05 V settle rule becomes a recorded warning.
OFFSET_SETTLED_WINDOW_S = 2.0

# --------------------------------------------------------------------------- #
# Noise phase (TP120 timing; the analysis constants live in array_analysis)
# --------------------------------------------------------------------------- #
# TP120: "Let detectors stand for five minutes for detectors to stabilize"
# after power-on. The countdown can be shortened by the technician; the actual
# wait is recorded on every row, so a short wait is visible in the data.
NOISE_STABILISATION_S = 300.0
# TP120: rectifier-hold read after "a minimum of 60 seconds" -> 60 one-second
# windows. 20 s is the engineering option (same rule, fewer windows).
NOISE_CAPTURE_SECONDS = 60.0
NOISE_CAPTURE_SECONDS_ENGINEERING = 20.0
CAPTURE_LENGTH_CHOICES = ((NOISE_CAPTURE_SECONDS, "60 s (TP120)"), (NOISE_CAPTURE_SECONDS_ENGINEERING, "20 s (engineering)"))
# Adaptive quiet wait carried from the 405 M22: stream and watch 1 s block
# means; capture starts once every loaded channel's last N block-to-block
# changes are within the delta, or at the deadline regardless. Never a verdict.
NOISE_WAIT_BEFORE_CAPTURE_S = 3.0
NOISE_WAIT_MAX_S = 20.0
NOISE_BASELINE_SETTLE_BLOCKS = 2
NOISE_BASELINE_SETTLE_DELTA_MV = 0.1   # the 405's derived value (limit/4 ~ 107 uV), rounded; only affects wait time
NOISE_EDGE_CONTEXT_SAMPLES = aa.antialias_edge_context_samples(aa.NOISE_DECIMATION_FACTOR)  # 310 at 1000 SPS

# --------------------------------------------------------------------------- #
# Phases and the emitter extension point
# --------------------------------------------------------------------------- #
class Phase(Enum):
    LOT_INFO = "LOT_INFO"
    LOAD_OFFSET = "LOAD_OFFSET"
    LOCKED = "LOCKED"
    STABILISING = "STABILISING"
    QUIET_WAIT = "QUIET_WAIT"
    CAPTURING = "CAPTURING"
    JUDGED = "JUDGED"
    SAVED = "SAVED"


# (label, implemented). "Sensitivity" is the emitter-board step: shown greyed
# out so the flow's future shape is visible, never selectable today.
STEP_RAIL: tuple[tuple[str, bool], ...] = (
    ("Lot", True), ("Load & offset", True), ("Lock tray", True), ("Noise", True), ("Sensitivity", False), ("Save", True),
)


class DriveDevice(Protocol):
    """The emitter-board driver the sensitivity phase will need (not built yet)."""

    def configure_emitter(self, *, frequency_hz: float, duty_cycle_percent: float) -> float: ...

    def disable_emitter(self) -> None: ...


class CaptureCancelled(RuntimeError):
    """The technician cancelled the capture."""


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class LockSnapshot:
    occupancy: list[aa.Occupancy]            # per channel 0..49
    sensor_numbers: dict[str, int]           # position -> sensor number (LOADED only)
    offset_initial_v: np.ndarray             # per channel, the insertion read at lock time
    ho_positions: tuple[str, ...]            # failed fast at lock time (HO / railed)
    start_number: int
    locked_at: str

    @property
    def loaded_positions(self) -> tuple[str, ...]:
        return tuple(daq.POSITIONS[c] for c, occ in enumerate(self.occupancy) if occ is aa.Occupancy.LOADED)

    @property
    def measured_positions(self) -> tuple[str, ...]:
        """Loaded positions that were not failed fast (the noise candidates)."""

        return tuple(p for p in self.loaded_positions if p not in self.ho_positions)

    @property
    def loaded_mask(self) -> np.ndarray:
        mask = np.zeros(daq.CHANNEL_COUNT, dtype=bool)
        for position in self.measured_positions:
            mask[daq.channel_for_position(position)] = True
        return mask

    def sensor_id(self, lot: str, position: str) -> str:
        number = self.sensor_numbers.get(position)
        return f"{lot}-{number}" if number is not None else ""


@dataclass(frozen=True)
class CapturePlan:
    capture_seconds: float = NOISE_CAPTURE_SECONDS
    stabilisation_s: float = NOISE_STABILISATION_S
    quiet_min_s: float = NOISE_WAIT_BEFORE_CAPTURE_S
    quiet_max_s: float = NOISE_WAIT_MAX_S
    settle_delta_mv: float = NOISE_BASELINE_SETTLE_DELTA_MV
    settle_blocks: int = NOISE_BASELINE_SETTLE_BLOCKS
    decimation_factor: int = aa.NOISE_DECIMATION_FACTOR
    edge_context_samples: int = NOISE_EDGE_CONTEXT_SAMPLES
    retry_limit: int = STREAM_RETRY_LIMIT
    scan_hz: float = DAQ_SCAN_HZ
    range_code: int = DAQ_RANGE_CODE
    oversample: int = DAQ_OVERSAMPLE
    drop_first: int = DAQ_DROP_CONVERSIONS_AFTER_MUX
    buffer_bytes: int = STREAM_BUFFER_BYTES
    buffer_count: int = STREAM_BUFFER_COUNT
    settled_window_s: float = OFFSET_SETTLED_WINDOW_S

    @property
    def config(self) -> daq.AdcConfig:
        return daq.AdcConfig(range_code=self.range_code, oversample=self.oversample)


@dataclass
class TrayCapture:
    waveform_v: np.ndarray                   # float32 [50, N] volts, the judged record
    sample_rate_hz: float
    actual_timer_hz: float
    left_context_v: np.ndarray | None        # float32 [50, 310] real history before the record
    right_context_v: np.ndarray | None       # float32 [50, 310] real samples after it
    diagnostics: daq.StreamDiagnostics | None
    quiet_wait_s: float
    quiet_settled: bool
    stabilisation_wait_s: float
    started_at: str
    attempts_used: int


@dataclass
class TrayCaptureReport:
    capture: TrayCapture | None
    results: list[aa.PositionResult]
    noise: list[aa.ChannelNoiseAnalysis]
    judged: np.ndarray | None
    rig_fault: str | None
    attempts_used: int
    stabilisation_wait_s: float

    @property
    def by_position(self) -> dict[str, aa.PositionResult]:
        return {result.position: result for result in self.results}


@dataclass
class TrayState:
    lot: str
    tray_number: int
    tester_name: str
    tray_attempt: int = 1
    lock: LockSnapshot | None = None
    report: TrayCaptureReport | None = None
    lock_results: list[aa.PositionResult] = field(default_factory=list)
    saved: bool = False
    capture_seconds: float = NOISE_CAPTURE_SECONDS
    stabilisation_s: float = NOISE_STABILISATION_S
    comments: dict[str, str] = field(default_factory=dict)
    failure_tags: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Results location (outside the repository - docs/DATA_MAP.md)
# --------------------------------------------------------------------------- #
RESULTS_ROOT_ENV = "ELTEC_ARRAY_RESULTS_ROOT"


def results_root_dir() -> Path:
    """The production results root, or the ``ELTEC_ARRAY_RESULTS_ROOT`` override.

    The override exists for engineering runs (a simulator session, a bench
    experiment) that must not leave rows in the production folder; the
    tests always pass an explicit temporary root instead.
    """

    override = os.environ.get(RESULTS_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Documents" / RESULTS_ROOT_NAME / RESULTS_MODEL_DIR


def safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value.strip())
    return cleaned or "unnamed"


def lot_results_path(lot: str, root: Path | None = None) -> Path:
    return (root or results_root_dir()) / f"{RESULTS_PREFIX}_lot_{safe_filename_part(lot)}.csv"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find a free name near {path}")


def raw_capture_path(lot: str, tray: int, root: Path | None = None) -> Path:
    base = (root or results_root_dir()) / "noise_captures" / f"lot_{safe_filename_part(lot)}"
    return _unique_path(base / f"tray_{tray}_raw.npz")


def grid_snapshot_path(lot: str, tray: int, root: Path | None = None) -> Path:
    base = (root or results_root_dir()) / "grid_snapshots" / f"lot_{safe_filename_part(lot)}"
    return _unique_path(base / f"tray_{tray}.png")


def next_sensor_number_for_lot(lot: str, root: Path | None = None) -> int:
    csv_path = lot_results_path(lot, root)
    return tray_history.highest_sensor_number(csv_path, tray_history.attempts_path_for(csv_path)) + 1


def assign_sensor_numbers(occupancy: Sequence[aa.Occupancy], start: int) -> dict[str, int]:
    """Row-major numbering of the LOADED positions from ``start`` (empty sockets never spend a number)."""

    if start < 1:
        raise ValueError("start must be >= 1")
    numbers: dict[str, int] = {}
    next_number = start
    for channel, occ in enumerate(occupancy):
        if occ is aa.Occupancy.LOADED:
            numbers[daq.POSITIONS[channel]] = next_number
            next_number += 1
    return numbers


# --------------------------------------------------------------------------- #
# CSV (one row per position per tray; append-only; existing header wins)
# --------------------------------------------------------------------------- #
CSV_FIELDS = [
    "timestamp", "lot_number", "tray_number", "tray_attempt", "position", "row", "col", "daq_channel",
    "sensor_number", "sensor_id", "tester_name", "model", "procedure", "occupancy",
    "offset_initial_v", "offset_v", "offset_settle_delta_v", "offset_class",
    "offset_limit_min_v", "offset_limit_max_v", "offset_gate_status",
    "noise_worst_pp_mv", "noise_median_pp_mv", "noise_windows_total", "noise_windows_over", "noise_clipped_windows",
    "noise_pp_limit_low_mv", "noise_pp_limit_high_mv", "noise_max_over_fraction", "noise_limit_provenance",
    "noise_verdict", "noise_band_note",
    "stabilisation_wait_s", "quiet_wait_s", "quiet_settled", "capture_seconds",
    "pass_fail", "verdict", "verdict_status", "fail_reasons", "warnings", "failure_mode_tag", "operator_comments",
    "calibration_status", "calibration_id",
    "daq_serial", "daq_range_code", "daq_oversample", "daq_drop_conversions", "daq_scan_rate_hz", "daq_actual_timer_hz",
    "stream_pool_events", "stream_attempts", "raw_capture_path", "grid_snapshot_path", "app_version", "simulated",
]
NOISE_BAND_NOTE = "judged in the single rig's band: FIR decimate 1000->50 SPS, 1 s window detrend (~0.85-22 Hz)"


@dataclass(frozen=True)
class RowContext:
    lot: str
    tray_number: int
    tray_attempt: int
    tester_name: str
    daq_serial: str
    simulated: bool
    plan: CapturePlan
    stabilisation_wait_s: float | None = None
    quiet_wait_s: float | None = None
    quiet_settled: bool | None = None
    capture_seconds: float | None = None
    actual_timer_hz: float | None = None
    pool_events: int | None = None
    stream_attempts: int | None = None
    raw_capture_path: str = ""
    grid_snapshot_path: str = ""
    noise_limits: aa.NoiseLimits = aa.NoiseLimits()
    offset_limits: aa.OffsetLimits = aa.OffsetLimits()


def _fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def position_row(result: aa.PositionResult, ctx: RowContext, *, comment: str = "", failure_tag: str | None = None) -> dict[str, str]:
    row_index, col_index = (int(part) for part in result.position.split("-"))
    noise = result.noise
    return {
        "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
        "lot_number": ctx.lot,
        "tray_number": str(ctx.tray_number),
        "tray_attempt": str(ctx.tray_attempt),
        "position": result.position,
        "row": str(row_index),
        "col": str(col_index),
        "daq_channel": str(result.channel),
        "sensor_number": "" if result.sensor_number is None else str(result.sensor_number),
        "sensor_id": result.sensor_id,
        "tester_name": ctx.tester_name,
        "model": MODEL_NAME,
        "procedure": PROCEDURE,
        "occupancy": result.occupancy.value,
        "offset_initial_v": _fmt(result.offset_initial_v, 5),
        "offset_v": _fmt(result.offset_v, 6),
        "offset_settle_delta_v": _fmt(result.offset_settle_delta_v, 5),
        "offset_class": "" if result.offset_class is None else result.offset_class.value,
        "offset_limit_min_v": _fmt(ctx.offset_limits.min_v, 3),
        "offset_limit_max_v": _fmt(ctx.offset_limits.max_v, 3),
        "offset_gate_status": aa.OFFSET_LIMITS_STATUS,
        "noise_worst_pp_mv": "" if noise is None else _fmt(noise.worst_pp_mv, 6),
        "noise_median_pp_mv": "" if noise is None else _fmt(noise.median_pp_mv, 6),
        "noise_windows_total": "" if noise is None else str(noise.windows_total),
        "noise_windows_over": "" if noise is None or noise.windows_over_high is None else str(noise.windows_over_high),
        "noise_clipped_windows": "" if noise is None else str(noise.clipped_windows),
        "noise_pp_limit_low_mv": _fmt(ctx.noise_limits.low_mv, 6),
        "noise_pp_limit_high_mv": _fmt(ctx.noise_limits.high_mv, 6),
        "noise_max_over_fraction": _fmt(ctx.noise_limits.max_over_fraction, 3),
        "noise_limit_provenance": ctx.noise_limits.provenance,
        "noise_verdict": "" if noise is None else noise.verdict.value,
        "noise_band_note": "" if noise is None else NOISE_BAND_NOTE,
        "stabilisation_wait_s": _fmt(ctx.stabilisation_wait_s, 1),
        "quiet_wait_s": _fmt(ctx.quiet_wait_s, 2),
        "quiet_settled": _fmt(ctx.quiet_settled),
        "capture_seconds": _fmt(ctx.capture_seconds, 1),
        "pass_fail": result.pass_fail_text,
        "verdict": result.verdict.value,
        "verdict_status": result.verdict_status,
        "fail_reasons": "; ".join(str(reason) for reason in result.fail_reasons),
        "warnings": "; ".join(result.warnings),
        "failure_mode_tag": failure_tag if failure_tag is not None else result.failure_mode_tag,
        "operator_comments": " ".join(comment.split()),
        "calibration_status": result.calibration_status,
        "calibration_id": result.calibration_id,
        "daq_serial": ctx.daq_serial,
        "daq_range_code": str(ctx.plan.range_code),
        "daq_oversample": str(ctx.plan.oversample),
        "daq_drop_conversions": str(ctx.plan.drop_first),
        "daq_scan_rate_hz": _fmt(ctx.plan.scan_hz, 1),
        "daq_actual_timer_hz": _fmt(ctx.actual_timer_hz, 4),
        "stream_pool_events": "" if ctx.pool_events is None else str(ctx.pool_events),
        "stream_attempts": "" if ctx.stream_attempts is None else str(ctx.stream_attempts),
        "raw_capture_path": ctx.raw_capture_path,
        "grid_snapshot_path": ctx.grid_snapshot_path,
        "app_version": APP_VERSION,
        "simulated": _fmt(ctx.simulated),
    }


def append_position_rows(csv_path: Path, rows: Iterable[dict[str, str]]) -> int:
    """Append rows; if the file exists its header wins so old files stay column-aligned."""

    rows = list(rows)
    if not rows:
        return 0
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(CSV_FIELDS)
    write_header = True
    if csv_path.exists() and csv_path.stat().st_size > 0:
        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            existing = next(csv.reader(handle), None)
        if existing:
            fieldnames = existing
            write_header = False
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
    return len(rows)


# --------------------------------------------------------------------------- #
# Raw capture (.npz) and grid snapshot (.png)
# --------------------------------------------------------------------------- #
def save_tray_raw_capture(
    path: Path,
    capture: TrayCapture,
    lock: LockSnapshot,
    *,
    lot: str,
    tray_number: int,
    tray_attempt: int,
    daq_info: daq.DaqInfo | None,
    plan: CapturePlan,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    channels = np.arange(daq.CHANNEL_COUNT)
    sensor_numbers = np.array([lock.sensor_numbers.get(p, 0) for p in daq.POSITIONS], dtype=np.int32)
    occupancy = np.array([o.value for o in lock.occupancy])
    meta = {
        "lot_number": lot, "tray_number": tray_number, "tray_attempt": tray_attempt, "recorded_at": capture.started_at,
        "daq_serial": daq_info.serial_number if daq_info else "unknown", "daq_model": daq_info.name if daq_info else "unknown",
        "range_code": plan.range_code, "range_span_v": daq.range_spec(plan.range_code).span_v, "oversample": plan.oversample,
        "drop_conversions": plan.drop_first, "scan_rate_hz": plan.scan_hz, "actual_timer_hz": capture.actual_timer_hz,
        "capture_seconds": plan.capture_seconds, "stabilisation_wait_s": capture.stabilisation_wait_s,
        "quiet_wait_s": capture.quiet_wait_s, "quiet_settled": capture.quiet_settled,
        "decimation_factor": plan.decimation_factor, "calibration_id": aa.CALIBRATION_ID, "app_version": APP_VERSION,
        "model": MODEL_NAME, "simulated": bool(daq_info.simulated) if daq_info else False,
        "stream_attempts": capture.attempts_used,
        "ho_positions": " ".join(lock.ho_positions),
    }
    arrays: dict[str, Any] = {
        "waveform_v": capture.waveform_v.astype(np.float32),
        "sample_rate_hz": np.float64(capture.sample_rate_hz),
        "channels": channels,
        "positions": np.array(daq.POSITIONS),
        "sensor_numbers": sensor_numbers,
        "occupancy": occupancy,
    }
    if capture.left_context_v is not None:
        arrays["left_context_v"] = capture.left_context_v.astype(np.float32)
    if capture.right_context_v is not None:
        arrays["right_context_v"] = capture.right_context_v.astype(np.float32)
    for key, value in meta.items():
        arrays[key] = np.array(str(value))
    np.savez_compressed(path, **arrays)
    return path


GRID_COLOURS: dict[aa.TileState, tuple[str, str]] = {  # (background, text)
    aa.TileState.EMPTY: ("#e8edf6", "#93a1bd"),
    aa.TileState.UNKNOWN: ("#fff3c4", "#7a5a00"),
    aa.TileState.LOADED: ("#e1e7f6", "#16336f"),
    aa.TileState.SETTLING: ("#fdf5dd", "#854d0e"),
    aa.TileState.OFFSET_FAIL: ("#fde7e9", "#991b1b"),
    aa.TileState.NOISE_FAIL: ("#ede4fb", "#5b21b6"),
    aa.TileState.NOISE_LOW: ("#d9c8f5", "#3b0f7a"),
    aa.TileState.PASS: ("#e4f6eb", "#14532d"),
    aa.TileState.NO_LIMIT: ("#dfe8f3", "#1e419c"),
    aa.TileState.NOT_MEASURED: ("#d9dee7", "#4b5563"),
}
GRID_LEGEND: tuple[tuple[aa.TileState, str], ...] = (
    (aa.TileState.LOADED, "loaded"),
    (aa.TileState.OFFSET_FAIL, "offset FAIL (HO / LO / D) - pull the part"),
    (aa.TileState.NOISE_FAIL, "noise FAIL (N)"),
    (aa.TileState.NOISE_LOW, "noise low (NL)"),
    (aa.TileState.PASS, "PASS"),
    (aa.TileState.NO_LIMIT, "measured, no noise limit yet"),
    (aa.TileState.SETTLING, "low / settling (judged later)"),
    (aa.TileState.UNKNOWN, "reads 0 V: click to mark empty or loaded"),
    (aa.TileState.EMPTY, "empty socket"),
    (aa.TileState.NOT_MEASURED, "not measured (rig fault)"),
)


def tile_texts(result: aa.PositionResult | None, *, live_offset_v: float | None = None) -> tuple[str, str]:
    """(headline, detail) shown on a tile."""

    if result is None:
        if live_offset_v is None:
            return "", ""
        return f"{live_offset_v:.3f} V", ""
    if result.verdict is aa.PositionVerdict.EMPTY:
        return "empty", ""
    headline = "" if result.offset_v is None else f"{result.offset_v:.3f} V"
    if result.noise is None:
        detail = result.fail_reasons[0].code if result.fail_reasons else ""
    elif result.noise.verdict is aa.NoiseVerdict.NO_LIMIT:
        detail = f"{result.noise.worst_pp_mv * 1000:.0f} uV pp (no limit)"
    else:
        detail = f"{result.noise.worst_pp_mv * 1000:.0f} uV pp {result.noise.verdict.value}"
    if result.verdict is aa.PositionVerdict.NOT_MEASURED:
        detail = "NOT MEASURED"
    return headline, detail


def save_grid_snapshot(results: Sequence[aa.PositionResult], *, path: Path, title: str) -> Path | None:
    """Render the 5x10 verdict grid to a PNG (matplotlib Agg); None if matplotlib is missing."""

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except Exception:
        return None
    by_position = {r.position: r for r in results}
    fig, ax = plt.subplots(figsize=(14, 8))
    ax.set_xlim(0, daq.COLS)
    ax.set_ylim(0, daq.ROWS + 0.9)
    ax.set_axis_off()
    for row in range(1, daq.ROWS + 1):
        for col in range(1, daq.COLS + 1):
            position = daq.position_label(row, col)
            result = by_position.get(position)
            state = aa.tile_state_for(result) if result else aa.TileState.EMPTY
            bg, fg = GRID_COLOURS[state]
            x, y = col - 1, daq.ROWS - row
            ax.add_patch(plt.Rectangle((x + 0.03, y + 0.03), 0.94, 0.94, facecolor=bg, edgecolor="#9aa5b8", linewidth=0.8))
            headline, detail = tile_texts(result)
            number = "" if result is None or result.sensor_number is None else f"#{result.sensor_number}"
            ax.text(x + 0.08, y + 0.82, position, fontsize=8, color=fg, va="center")
            ax.text(x + 0.92, y + 0.82, number, fontsize=8, color=fg, va="center", ha="right")
            ax.text(x + 0.5, y + 0.5, headline, fontsize=10, color=fg, va="center", ha="center", fontweight="bold")
            ax.text(x + 0.5, y + 0.2, detail, fontsize=7, color=fg, va="center", ha="center")
    handles = [Patch(facecolor=GRID_COLOURS[state][0], edgecolor="#9aa5b8", label=label) for state, label in GRID_LEGEND]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.0), ncol=5, fontsize=7, frameon=False)
    ax.set_title(title, fontsize=11, pad=28)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# The capture procedure (Tk-free)
# --------------------------------------------------------------------------- #
ProgressFn = Callable[[str, float | None, str], None]


def _read_scans_volts(device: daq.DaqDevice, plan: CapturePlan, timeout_s: float = 1.0) -> np.ndarray | None:
    chunk = device.read_stream(timeout_s=timeout_s)
    if chunk is None:
        return None
    return daq.counts_to_volts(chunk, plan.range_code)


def run_quiet_wait(
    device: daq.DaqDevice,
    plan: CapturePlan,
    loaded_mask: np.ndarray,
    *,
    progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[np.ndarray, float, bool]:
    """Stream (discarding) until the loaded channels' block means settle or the deadline.

    Returns ``(tail [50, >= edge samples] volts, wait_s, settled)``. The tail
    is the real history that seats the anti-alias FIR at the capture's start.
    """

    block_samples = int(round(plan.scan_hz))
    min_samples = int(round(plan.quiet_min_s * plan.scan_hz))
    max_samples = int(round(plan.quiet_max_s * plan.scan_hz))
    keep = max(plan.edge_context_samples, 1)
    collected = 0
    block_means: list[np.ndarray] = []
    current: list[np.ndarray] = []
    current_len = 0
    tail: list[np.ndarray] = []
    tail_len = 0
    settled = False
    while collected < max_samples:
        if cancelled and cancelled():
            raise CaptureCancelled("cancelled during the quiet wait")
        chunk = _read_scans_volts(device, plan)
        if chunk is None:
            continue
        collected += chunk.shape[0]
        tail.append(chunk)
        tail_len += chunk.shape[0]
        while tail_len - tail[0].shape[0] >= keep and len(tail) > 1:
            tail_len -= tail[0].shape[0]
            tail.pop(0)
        current.append(chunk)
        current_len += chunk.shape[0]
        if current_len >= block_samples:
            block = np.concatenate(current)[:block_samples]
            block_means.append(block.mean(axis=0))
            current, current_len = [], 0
            if progress:
                progress("quiet", min(1.0, collected / max_samples), f"quiet wait {collected / plan.scan_hz:.0f} s")
            if collected >= min_samples and aa.quiet_wait_settled(
                np.stack(block_means), loaded_mask, delta_mv=plan.settle_delta_mv, blocks_required=plan.settle_blocks
            ):
                settled = True
                break
    history = np.concatenate(tail).T if tail else np.empty((daq.CHANNEL_COUNT, 0))
    return history[:, -keep:], collected / plan.scan_hz, settled


def run_tray_capture(
    device: daq.DaqDevice,
    plan: CapturePlan,
    lock: LockSnapshot,
    *,
    progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] | None = None,
    stabilisation_wait_s: float = 0.0,
    lot: str = "",
    noise_limits: aa.NoiseLimits = aa.NoiseLimits(),
    offset_limits: aa.OffsetLimits = aa.OffsetLimits(),
) -> TrayCaptureReport:
    """Stream -> quiet wait -> capture (+ edge contexts) -> integrity check (retry) -> analysis -> verdicts.

    The stabilisation countdown is the caller's (GUI) job; its actual length
    is passed in for the record.
    """

    capture_samples = int(round(plan.capture_seconds * plan.scan_hz))
    context = plan.edge_context_samples
    loaded_mask = lock.loaded_mask
    started_at = _dt.datetime.now().isoformat(timespec="seconds")
    last_error = "no attempt made"
    attempts = 0
    capture: TrayCapture | None = None
    while attempts <= plan.retry_limit and capture is None:
        attempts += 1
        if cancelled and cancelled():
            raise CaptureCancelled("cancelled before the capture")
        header = device.start_stream(
            scan_hz=plan.scan_hz, buffer_bytes=plan.buffer_bytes, buffer_count=plan.buffer_count, drop_first=plan.drop_first,
        )
        try:
            left, quiet_s, settled = run_quiet_wait(device, plan, loaded_mask, progress=progress, cancelled=cancelled)
            chunks: list[np.ndarray] = []
            got = 0
            wanted = capture_samples + context
            while got < wanted:
                if cancelled and cancelled():
                    raise CaptureCancelled("cancelled during the capture")
                chunk = _read_scans_volts(device, plan)
                if chunk is None:
                    continue
                chunks.append(chunk)
                got += chunk.shape[0]
                if progress:
                    progress("capture", min(1.0, got / wanted), f"capturing {min(got, capture_samples) / plan.scan_hz:.0f} / {plan.capture_seconds:.0f} s")
        except CaptureCancelled:
            try:
                device.stop_stream()
            except daq.DaqError:
                pass
            raise
        except daq.DaqError as exc:
            last_error = f"stream failed: {exc}"
            try:
                device.stop_stream()
            except daq.DaqError:
                pass
            if progress:
                progress("retry", None, f"attempt {attempts} failed: {exc}")
            continue
        diagnostics = device.stop_stream()
        problems = diagnostics.problems()
        if problems:
            last_error = "stream integrity: " + "; ".join(problems)
            if progress:
                progress("retry", None, f"attempt {attempts}: {last_error}")
            continue
        data = np.concatenate(chunks)[:wanted].T  # [50, wanted]
        record = data[:, :capture_samples]
        right = data[:, capture_samples:capture_samples + context]
        capture = TrayCapture(
            waveform_v=record.astype(np.float32),
            sample_rate_hz=plan.scan_hz,
            actual_timer_hz=header.actual_timer_hz,
            left_context_v=left.astype(np.float32) if left.shape[1] else None,
            right_context_v=right.astype(np.float32) if right.shape[1] else None,
            diagnostics=diagnostics,
            quiet_wait_s=quiet_s,
            quiet_settled=settled,
            stabilisation_wait_s=stabilisation_wait_s,
            started_at=started_at,
            attempts_used=attempts,
        )

    results: list[aa.PositionResult] = []
    if capture is None:
        for position in lock.measured_positions:
            channel = daq.channel_for_position(position)
            results.append(aa.judge_position(
                position=position, channel=channel, occupancy=aa.Occupancy.LOADED,
                sensor_number=lock.sensor_numbers.get(position), sensor_id=lock.sensor_id(lot, position),
                offset_initial_v=float(lock.offset_initial_v[channel]), offset_v=None, noise=None,
                offset_limits=offset_limits, noise_limits=noise_limits, rig_fault=last_error,
            ))
        return TrayCaptureReport(capture=None, results=results, noise=[], judged=None, rig_fault=last_error,
                                 attempts_used=attempts, stabilisation_wait_s=stabilisation_wait_s)

    if progress:
        progress("analysis", None, "analysing fifty channels")
    raw = capture.waveform_v.astype(np.float64)
    noise, judged, _ = aa.analyze_tray_noise(
        raw, plan.scan_hz, positions=list(daq.POSITIONS), limits=noise_limits,
        decimation_factor=plan.decimation_factor, window_s=aa.NOISE_WINDOW_S,
        left_context=None if capture.left_context_v is None else capture.left_context_v.astype(np.float64),
        right_context=None if capture.right_context_v is None else capture.right_context_v.astype(np.float64),
    )
    window = max(1, int(round(plan.settled_window_s * plan.scan_hz)))
    early = raw[:, :window].mean(axis=1)
    settled_offsets = raw[:, -window:].mean(axis=1)
    for channel, position in enumerate(daq.POSITIONS):
        occupancy = lock.occupancy[channel]
        if occupancy is not aa.Occupancy.LOADED:
            continue
        if position in lock.ho_positions:
            continue  # failed fast at lock time; its row already exists
        results.append(aa.judge_position(
            position=position, channel=channel, occupancy=occupancy,
            sensor_number=lock.sensor_numbers.get(position), sensor_id=lock.sensor_id(lot, position),
            offset_initial_v=float(lock.offset_initial_v[channel]), offset_v=float(settled_offsets[channel]),
            offset_early_v=float(early[channel]), noise=noise[channel],
            offset_limits=offset_limits, noise_limits=noise_limits,
            extra_warnings=() if capture.quiet_settled else ("Baseline had not settled before the capture (deadline reached).",),
        ))
    return TrayCaptureReport(capture=capture, results=results, noise=noise, judged=judged, rig_fault=None,
                             attempts_used=capture.attempts_used, stabilisation_wait_s=stabilisation_wait_s)


# --------------------------------------------------------------------------- #
# TrayController: the whole flow without Tk (the GUI wraps it; the tests drive it)
# --------------------------------------------------------------------------- #
class TrayController:
    def __init__(
        self,
        device: daq.DaqDevice,
        *,
        lot: str,
        tray_number: int,
        tester_name: str,
        results_root: Path | None = None,
        plan: CapturePlan = CapturePlan(),
        noise_limits: aa.NoiseLimits = aa.NoiseLimits(),
        offset_limits: aa.OffsetLimits = aa.OffsetLimits(),
    ) -> None:
        self.device = device
        self.plan = plan
        self.noise_limits = noise_limits
        self.offset_limits = offset_limits
        self.results_root = results_root or results_root_dir()
        self.state = TrayState(lot=lot.strip(), tray_number=tray_number, tester_name=tester_name.strip())
        self.hardware_lock = threading.RLock()
        self.occupancy_choice: dict[str, aa.Occupancy] = {}
        self.live_offsets: np.ndarray | None = None
        self.phase = Phase.LOT_INFO
        self.drive: DriveDevice | None = None  # emitter board slot (sensitivity phase, later)

    # -- paths ---------------------------------------------------------------
    @property
    def csv_path(self) -> Path:
        return lot_results_path(self.state.lot, self.results_root)

    @property
    def attempts_path(self) -> Path:
        return tray_history.attempts_path_for(self.csv_path)

    # -- hardware ------------------------------------------------------------
    def start(self) -> daq.DaqInfo:
        with self.hardware_lock:
            info = self.device.connect()
            self.device.configure(self.plan.config)
            if DAQ_SELF_CALIBRATE_ON_CONNECT and info.calibration_supported is not False:
                self.device.self_calibrate()
                self.device.configure(self.plan.config)  # calibration can leave the block; make sure ours is live
        self.phase = Phase.LOAD_OFFSET
        return info

    def close(self) -> None:
        with self.hardware_lock:
            self.device.close()

    def poll_offsets(self) -> np.ndarray:
        with self.hardware_lock:
            volts = self.device.read_scan_volts_median(reads=OFFSET_POLL_READS)
        self.live_offsets = volts
        return volts

    # -- phase A -------------------------------------------------------------
    def effective_occupancy(self, position: str, volts: float | None = None) -> aa.Occupancy:
        chosen = self.occupancy_choice.get(position)
        if chosen is not None:
            return chosen
        if volts is None and self.live_offsets is not None:
            volts = float(self.live_offsets[daq.channel_for_position(position)])
        if volts is not None and volts < self.offset_limits.dead_v:
            return aa.Occupancy.UNKNOWN
        return aa.Occupancy.LOADED

    def set_occupancy(self, position: str, occupancy: aa.Occupancy | None) -> None:
        if occupancy is None:
            self.occupancy_choice.pop(position, None)
        else:
            self.occupancy_choice[position] = occupancy

    def toggle_occupancy(self, position: str) -> aa.Occupancy:
        """UNKNOWN/LOADED -> EMPTY -> LOADED -> EMPTY ... (technician click)."""

        current = self.effective_occupancy(position)
        new = aa.Occupancy.LOADED if current is aa.Occupancy.EMPTY else aa.Occupancy.EMPTY
        self.occupancy_choice[position] = new
        return new

    def live_tile_state(self, position: str) -> aa.TileState:
        if self.live_offsets is None:
            return aa.TileState.LOADED if self.effective_occupancy(position) is aa.Occupancy.LOADED else aa.TileState.EMPTY
        volts = float(self.live_offsets[daq.channel_for_position(position)])
        return aa.tile_state_for_live_offset(volts, occupancy=self.effective_occupancy(position, volts), limits=self.offset_limits)

    def unknown_positions(self) -> tuple[str, ...]:
        return tuple(p for p in daq.POSITIONS if self.effective_occupancy(p) is aa.Occupancy.UNKNOWN)

    def lock_tray(self, *, start_number: int | None = None) -> LockSnapshot:
        if self.phase is not Phase.LOAD_OFFSET:
            raise RuntimeError("Lock is only possible in the Load & offset phase.")
        volts = self.poll_offsets()
        unknown = self.unknown_positions()
        if unknown:
            raise ValueError(
                "These positions read ~0 V - click each one to mark it EMPTY or LOADED before locking: " + " ".join(unknown)
            )
        occupancy = [self.effective_occupancy(p, float(volts[c])) for c, p in enumerate(daq.POSITIONS)]
        start = start_number if start_number is not None else next_sensor_number_for_lot(self.state.lot, self.results_root)
        numbers = assign_sensor_numbers(occupancy, start)
        ho = tuple(
            p for c, p in enumerate(daq.POSITIONS)
            if occupancy[c] is aa.Occupancy.LOADED
            and aa.offset_is_fail_fast(aa.classify_offset(float(volts[c]), occupancy=aa.Occupancy.LOADED, limits=self.offset_limits))
        )
        lock = LockSnapshot(
            occupancy=occupancy, sensor_numbers=numbers, offset_initial_v=volts.copy(), ho_positions=ho,
            start_number=start, locked_at=_dt.datetime.now().isoformat(timespec="seconds"),
        )
        self.state.lock = lock
        self.state.report = None
        self.state.saved = False
        # HO parts are judged now (fail-fast) and their rows written now, so a
        # pulled part is never lost even if the noise phase is abandoned.
        lock_results = []
        for position in ho:
            channel = daq.channel_for_position(position)
            lock_results.append(aa.judge_position(
                position=position, channel=channel, occupancy=aa.Occupancy.LOADED,
                sensor_number=numbers.get(position), sensor_id=lock.sensor_id(self.state.lot, position),
                offset_initial_v=float(volts[channel]), offset_v=float(volts[channel]), noise=None,
                offset_limits=self.offset_limits, noise_limits=self.noise_limits,
                extra_warnings=("Failed fast at lock time (insertion read); pull the part before the noise capture.",),
            ))
        self.state.lock_results = lock_results
        if lock_results:
            append_position_rows(self.csv_path, [position_row(r, self._row_context()) for r in lock_results])
        loaded = lock.loaded_positions
        tray_history.append_tray_event(
            self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
            tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_LOCKED, phase=Phase.LOCKED.value,
            tester_name=self.state.tester_name, loaded_count=len(loaded), ho_positions=" ".join(ho),
            first_sensor_number=min(numbers.values()) if numbers else 0, last_sensor_number=max(numbers.values()) if numbers else 0,
            detail=f"{len(ho)} high-offset part(s) failed fast",
        )
        self.phase = Phase.LOCKED
        return lock

    # -- phase B -------------------------------------------------------------
    def run_noise_phase(
        self,
        *,
        stabilisation_wait_s: float,
        progress: ProgressFn | None = None,
        cancelled: Callable[[], bool] | None = None,
        capture_seconds: float | None = None,
    ) -> TrayCaptureReport:
        lock = self.state.lock
        if lock is None:
            raise RuntimeError("Lock the tray before the noise phase.")
        plan = self.plan
        if capture_seconds is not None:
            from dataclasses import replace

            plan = replace(plan, capture_seconds=capture_seconds)
        self.state.capture_seconds = plan.capture_seconds
        self.state.stabilisation_s = stabilisation_wait_s
        tray_history.append_tray_event(
            self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
            tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_CAPTURE_STARTED, phase=Phase.CAPTURING.value,
            tester_name=self.state.tester_name, stabilisation_wait_s=stabilisation_wait_s, capture_seconds=plan.capture_seconds,
        )
        if stabilisation_wait_s + 1e-9 < self.state.stabilisation_s or stabilisation_wait_s + 1e-9 < NOISE_STABILISATION_S:
            tray_history.append_tray_event(
                self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
                tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_STABILISATION_SHORTENED,
                phase=Phase.STABILISING.value, tester_name=self.state.tester_name, stabilisation_wait_s=stabilisation_wait_s,
                detail=f"TP120 asks for {NOISE_STABILISATION_S:.0f} s",
            )
        self.phase = Phase.CAPTURING

        def _progress(kind: str, fraction: float | None, message: str) -> None:
            if kind == "retry":
                tray_history.append_tray_event(
                    self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
                    tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_CAPTURE_RETRY,
                    phase=Phase.CAPTURING.value, tester_name=self.state.tester_name, detail=message,
                )
            if progress:
                progress(kind, fraction, message)

        with self.hardware_lock:
            report = run_tray_capture(
                self.device, plan, lock, progress=_progress, cancelled=cancelled,
                stabilisation_wait_s=stabilisation_wait_s, lot=self.state.lot,
                noise_limits=self.noise_limits, offset_limits=self.offset_limits,
            )
        self.state.report = report
        if report.rig_fault:
            tray_history.append_tray_event(
                self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
                tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_CAPTURE_ERROR,
                phase=Phase.CAPTURING.value, tester_name=self.state.tester_name, error=report.rig_fault,
            )
        else:
            tray_history.append_tray_event(
                self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
                tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_JUDGED, phase=Phase.JUDGED.value,
                tester_name=self.state.tester_name,
                detail=report.capture.diagnostics.summary() if report.capture and report.capture.diagnostics else "",
                quiet_wait_s=report.capture.quiet_wait_s if report.capture else None,
                capture_seconds=plan.capture_seconds, stabilisation_wait_s=stabilisation_wait_s,
            )
        self.phase = Phase.JUDGED
        return report

    # -- save ----------------------------------------------------------------
    def _row_context(self, report: TrayCaptureReport | None = None, *, raw_path: str = "", png_path: str = "") -> RowContext:
        info = self.device.info
        capture = report.capture if report else None
        diagnostics = capture.diagnostics if capture else None
        return RowContext(
            lot=self.state.lot, tray_number=self.state.tray_number, tray_attempt=self.state.tray_attempt,
            tester_name=self.state.tester_name, daq_serial=info.serial_number if info else "unknown",
            simulated=bool(info.simulated) if info else False, plan=self.plan,
            stabilisation_wait_s=report.stabilisation_wait_s if report else None,
            quiet_wait_s=capture.quiet_wait_s if capture else None,
            quiet_settled=capture.quiet_settled if capture else None,
            capture_seconds=self.state.capture_seconds if report else None,
            actual_timer_hz=capture.actual_timer_hz if capture else None,
            pool_events=diagnostics.pool_too_small_events if diagnostics else None,
            stream_attempts=report.attempts_used if report else None,
            raw_capture_path=raw_path, grid_snapshot_path=png_path,
            noise_limits=self.noise_limits, offset_limits=self.offset_limits,
        )

    def save_tray(self) -> dict[str, Any]:
        lock = self.state.lock
        report = self.state.report
        if lock is None or report is None:
            raise RuntimeError("Nothing to save: lock the tray and run the noise phase first.")
        if self.state.saved:
            raise RuntimeError("This tray attempt is already saved; use Re-measure for another attempt.")
        raw_path = ""
        if report.capture is not None:
            raw_path = str(save_tray_raw_capture(
                raw_capture_path(self.state.lot, self.state.tray_number, self.results_root), report.capture, lock,
                lot=self.state.lot, tray_number=self.state.tray_number, tray_attempt=self.state.tray_attempt,
                daq_info=self.device.info, plan=self.plan,
            ))
        all_results = list(self.state.lock_results) + list(report.results)
        png = save_grid_snapshot(
            all_results, path=grid_snapshot_path(self.state.lot, self.state.tray_number, self.results_root),
            title=f"{APP_TITLE} - lot {self.state.lot} tray {self.state.tray_number} attempt {self.state.tray_attempt} "
                  f"({aa.CALIBRATION_STATUS}, {aa.VERDICT_STATUS})",
        )
        ctx = self._row_context(report, raw_path=raw_path, png_path=str(png) if png else "")
        rows = [
            position_row(r, ctx, comment=self.state.comments.get(r.position, ""), failure_tag=self.state.failure_tags.get(r.position))
            for r in report.results
        ]
        written = append_position_rows(self.csv_path, rows)
        tray_history.append_tray_event(
            self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
            tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_SAVED, phase=Phase.SAVED.value,
            tester_name=self.state.tester_name, loaded_count=len(lock.loaded_positions), ho_positions=" ".join(lock.ho_positions),
            first_sensor_number=min(lock.sensor_numbers.values()) if lock.sensor_numbers else 0,
            last_sensor_number=max(lock.sensor_numbers.values()) if lock.sensor_numbers else 0,
            detail=f"{written} rows; raw {raw_path or '-'}; grid {png or '-'}",
            stabilisation_wait_s=report.stabilisation_wait_s, capture_seconds=self.state.capture_seconds,
        )
        self.state.saved = True
        self.phase = Phase.SAVED
        return {"csv": self.csv_path, "rows": written, "raw": raw_path, "png": png}

    def remeasure(self) -> int:
        if self.state.lock is None:
            raise RuntimeError("Lock the tray first.")
        tray_history.append_tray_event(
            self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
            tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_REMEASURE, phase=self.phase.value,
            tester_name=self.state.tester_name, detail="verdicts discarded" if not self.state.saved else "another attempt after save",
        )
        self.state.tray_attempt += 1
        self.state.report = None
        self.state.saved = False
        self.phase = Phase.LOCKED
        return self.state.tray_attempt

    def summary_counts(self) -> dict[str, int]:
        counts = {"loaded": 0, "pass": 0, "fail_offset": 0, "fail_noise": 0, "noise_low": 0, "no_limit": 0, "not_measured": 0}
        lock = self.state.lock
        if lock is None:
            return counts
        counts["loaded"] = len(lock.loaded_positions)
        results = list(self.state.lock_results) + (list(self.state.report.results) if self.state.report else [])
        for result in results:
            state = aa.tile_state_for(result)
            key = {
                aa.TileState.PASS: "pass", aa.TileState.OFFSET_FAIL: "fail_offset", aa.TileState.NOISE_FAIL: "fail_noise",
                aa.TileState.NOISE_LOW: "noise_low", aa.TileState.NO_LIMIT: "no_limit", aa.TileState.NOT_MEASURED: "not_measured",
            }.get(state)
            if key:
                counts[key] += 1
        return counts


# --------------------------------------------------------------------------- #
# Tk GUI
# --------------------------------------------------------------------------- #
ELTEC_BLUE = "#1e419c"
ELTEC_BLUE_DEEP = "#0b3d91"
ELTEC_RED = "#ed1b44"
PAGE_BG = "#f3f5fa"
CARD_BG = "#ffffff"
CARD_BORDER = "#dce3f1"
TEXT_DARK = "#141d33"
MUTED_FG = "#5c6a88"
HEADER_FG = "#ffffff"
WARN_BG = "#fdf5dd"
WARN_FG = "#854d0e"
PRIMARY_DISABLED = "#aab9dc"

ASSETS_DIR = _MODEL_DIR / "assets"
LOGO_CANDIDATES = [ASSETS_DIR / "eltec_logo.png"] + [parent / "assets" / "eltec_logo.png" for parent in _MODEL_DIR.parents]


def find_logo_path() -> Path | None:
    for candidate in LOGO_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


UI_SCALE = 1.0


def S(value: float) -> int:
    return int(round(value * UI_SCALE))


def enable_windows_dpi_awareness() -> None:
    """Opt out of DPI virtualisation (before the Tk window exists) - system-DPI aware, as the 405 tester."""

    if sys.platform != "win32":
        return
    try:
        import ctypes
    except Exception:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def build_gui_classes():
    """Tk classes are built lazily so the core (and the tests) never import tkinter."""

    import tkinter as tk
    from tkinter import messagebox, ttk

    class TrayGrid(tk.Canvas):
        """The 5 x 10 tile grid."""

        TILE_W, TILE_H, GAP = 118, 78, 8

        def __init__(self, master, *, on_tile_click: Callable[[str], None]) -> None:
            width = daq.COLS * (self.TILE_W + self.GAP) + self.GAP
            height = daq.ROWS * (self.TILE_H + self.GAP) + self.GAP
            super().__init__(master, width=S(width), height=S(height), bg=PAGE_BG, highlightthickness=0)
            self._on_tile_click = on_tile_click
            self._items: dict[str, dict[str, int]] = {}
            for channel, position in enumerate(daq.POSITIONS):
                row, col = divmod(channel, daq.COLS)
                x0 = S(self.GAP + col * (self.TILE_W + self.GAP))
                y0 = S(self.GAP + row * (self.TILE_H + self.GAP))
                x1, y1 = x0 + S(self.TILE_W), y0 + S(self.TILE_H)
                rect = self.create_rectangle(x0, y0, x1, y1, fill=GRID_COLOURS[aa.TileState.EMPTY][0], outline="#9aa5b8", width=1)
                label = self.create_text(x0 + S(6), y0 + S(10), text=position, anchor="w", font=("TkDefaultFont", 9))
                number = self.create_text(x1 - S(6), y0 + S(10), text="", anchor="e", font=("TkDefaultFont", 9))
                headline = self.create_text((x0 + x1) // 2, (y0 + y1) // 2, text="", font=("TkDefaultFont", 12, "bold"))
                detail = self.create_text((x0 + x1) // 2, y1 - S(12), text="", font=("TkDefaultFont", 8))
                self._items[position] = {"rect": rect, "label": label, "number": number, "headline": headline, "detail": detail}
                for item in self._items[position].values():
                    self.tag_bind(item, "<Button-1>", lambda _e, p=position: self._on_tile_click(p))

        def set_tile(self, position: str, *, state: aa.TileState, headline: str = "", detail: str = "", sensor_number: int | None = None) -> None:
            bg, fg = GRID_COLOURS[state]
            items = self._items[position]
            stipple = "gray50" if state is aa.TileState.NOT_MEASURED else ("gray25" if state is aa.TileState.NOISE_LOW else "")
            self.itemconfigure(items["rect"], fill=bg, stipple=stipple)
            self.itemconfigure(items["label"], fill=fg)
            self.itemconfigure(items["number"], text="" if sensor_number is None else f"#{sensor_number}", fill=fg)
            self.itemconfigure(items["headline"], text=headline, fill=fg)
            self.itemconfigure(items["detail"], text=detail, fill=fg)

    class ArrayTesterApp(tk.Tk):
        def __init__(self, *, device: daq.DaqDevice | None = None, simulate: bool = False) -> None:
            super().__init__()
            self.title(f"{APP_TITLE} v{APP_VERSION}")
            self.configure(bg=PAGE_BG)
            self.minsize(S(1180), S(760))
            self.simulate = simulate or device is not None and getattr(device.info, "simulated", False)
            self.device: daq.DaqDevice = device or (daq.SimulatedDaq(real_time=True) if simulate else daq.AiousbDaq())
            self.controller: TrayController | None = None
            self.drive: DriveDevice | None = None
            self._poll_job: str | None = None
            self._polling = False
            self._worker: threading.Thread | None = None
            self._cancel = threading.Event()
            self._skip_stabilisation = threading.Event()
            self._build()
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.start_maximized()

        # -- window -----------------------------------------------------------
        def start_maximized(self) -> None:
            try:
                if self.tk.call("tk", "windowingsystem") == "x11":
                    self.after(0, lambda: self.attributes("-zoomed", True))
                    return
                self.state("zoomed")
            except tk.TclError:
                pass

        def _post(self, callback: Callable[[], None]) -> None:
            try:
                self.after(0, callback)
            except (RuntimeError, tk.TclError):
                pass

        # -- construction -----------------------------------------------------
        def _build(self) -> None:
            header = tk.Frame(self, bg=ELTEC_BLUE_DEEP)
            header.pack(fill="x")
            tk.Label(header, text=APP_TITLE, bg=ELTEC_BLUE_DEEP, fg=HEADER_FG, font=("TkDefaultFont", 20, "bold")).pack(side="left", padx=S(18), pady=S(10))
            tk.Label(header, text=f"v{APP_VERSION} · {PROCEDURE} · 50-position DAQ array", bg=ELTEC_BLUE_DEEP, fg="#bcd0f7",
                     font=("TkDefaultFont", 11)).pack(side="left", padx=S(6))
            if self.simulate:
                tk.Label(header, text="SIMULATOR", bg=ELTEC_RED, fg="#ffffff", font=("TkDefaultFont", 10, "bold"), padx=S(10), pady=S(3)).pack(side="right", padx=S(18))
            self.banner = tk.Label(
                self, bg=WARN_BG, fg=WARN_FG, font=("TkDefaultFont", 10, "bold"), anchor="w", padx=S(18), pady=S(6),
                text=(f"CALIBRATION PENDING - noise limits not derived yet (tiles show the measured value, 'no limit yet'); "
                      f"offset limits {aa.OFFSET_MIN_V:.1f}-{aa.OFFSET_MAX_V:.1f} V are PROVISIONAL. Every row is stamped {aa.CALIBRATION_ID}."),
            )
            self.banner.pack(fill="x")

            rail = tk.Frame(self, bg=PAGE_BG)
            rail.pack(fill="x", padx=S(18), pady=(S(8), 0))
            self.step_labels: list[tk.Label] = []
            for label, implemented in STEP_RAIL:
                text = label if implemented else f"{label} (no emitter board yet)"
                widget = tk.Label(rail, text=text, bg=PAGE_BG, fg="#93a1bd" if implemented else "#c7d0e2", font=("TkDefaultFont", 10, "bold"), padx=S(10))
                widget.pack(side="left")
                self.step_labels.append(widget)

            body = tk.Frame(self, bg=PAGE_BG)
            body.pack(fill="both", expand=True, padx=S(18), pady=S(8))
            left = tk.Frame(body, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
            left.pack(side="left", fill="y", padx=(0, S(12)))
            right = tk.Frame(body, bg=PAGE_BG)
            right.pack(side="left", fill="both", expand=True)

            # lot card
            tk.Label(left, text="Lot / tray", bg=CARD_BG, fg=TEXT_DARK, font=("TkDefaultFont", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=S(14), pady=(S(12), S(4)))
            self.lot_var, self.tray_var, self.tester_var = tk.StringVar(), tk.StringVar(value="1"), tk.StringVar()
            self.start_number_var = tk.StringVar()
            for index, (label, var) in enumerate((("Lot number", self.lot_var), ("Tray number", self.tray_var), ("Tester name", self.tester_var), ("First sensor #", self.start_number_var))):
                tk.Label(left, text=label, bg=CARD_BG, fg=MUTED_FG, font=("TkDefaultFont", 10)).grid(row=1 + index, column=0, sticky="w", padx=S(14), pady=S(2))
                tk.Entry(left, textvariable=var, width=14, font=("TkDefaultFont", 11)).grid(row=1 + index, column=1, sticky="w", padx=(0, S(14)), pady=S(2))
            self.start_button = tk.Button(left, text="Start lot (connect DAQ)", command=self.start_lot, bg=ELTEC_BLUE, fg="#ffffff", relief="flat", font=("TkDefaultFont", 11, "bold"), padx=S(10), pady=S(6))
            self.start_button.grid(row=5, column=0, columnspan=2, sticky="ew", padx=S(14), pady=(S(8), S(4)))
            self.lock_button = tk.Button(left, text="Lock tray", command=self.lock_tray, state="disabled", relief="flat", bg=ELTEC_BLUE, fg="#ffffff", disabledforeground="#ffffff", font=("TkDefaultFont", 11, "bold"), padx=S(10), pady=S(6))
            self.lock_button.grid(row=6, column=0, columnspan=2, sticky="ew", padx=S(14), pady=S(4))

            tk.Label(left, text="Noise capture", bg=CARD_BG, fg=TEXT_DARK, font=("TkDefaultFont", 12, "bold")).grid(row=7, column=0, columnspan=2, sticky="w", padx=S(14), pady=(S(12), S(4)))
            self.capture_choice = tk.DoubleVar(value=NOISE_CAPTURE_SECONDS)
            for index, (seconds, label) in enumerate(CAPTURE_LENGTH_CHOICES):
                tk.Radiobutton(left, text=label, variable=self.capture_choice, value=seconds, bg=CARD_BG, anchor="w").grid(row=8 + index, column=0, columnspan=2, sticky="w", padx=S(14))
            self.noise_button = tk.Button(left, text="Start noise test", command=self.start_noise_phase, state="disabled", relief="flat", bg=ELTEC_BLUE, fg="#ffffff", disabledforeground="#ffffff", font=("TkDefaultFont", 11, "bold"), padx=S(10), pady=S(6))
            self.noise_button.grid(row=10, column=0, columnspan=2, sticky="ew", padx=S(14), pady=S(4))
            self.skip_button = tk.Button(left, text="Skip the rest of the stabilisation wait", command=self._skip_stabilisation.set, state="disabled", relief="flat", font=("TkDefaultFont", 9))
            self.skip_button.grid(row=11, column=0, columnspan=2, sticky="ew", padx=S(14), pady=S(2))
            self.cancel_button = tk.Button(left, text="Cancel capture", command=self._cancel.set, state="disabled", relief="flat", font=("TkDefaultFont", 9))
            self.cancel_button.grid(row=12, column=0, columnspan=2, sticky="ew", padx=S(14), pady=S(2))

            tk.Label(left, text="Record", bg=CARD_BG, fg=TEXT_DARK, font=("TkDefaultFont", 12, "bold")).grid(row=13, column=0, columnspan=2, sticky="w", padx=S(14), pady=(S(12), S(4)))
            self.save_button = tk.Button(left, text="Save tray", command=self.save_tray, state="disabled", relief="flat", bg="#17a34a", fg="#ffffff", disabledforeground="#ffffff", font=("TkDefaultFont", 11, "bold"), padx=S(10), pady=S(6))
            self.save_button.grid(row=14, column=0, columnspan=2, sticky="ew", padx=S(14), pady=S(4))
            self.remeasure_button = tk.Button(left, text="Re-measure tray", command=self.remeasure_tray, state="disabled", relief="flat", font=("TkDefaultFont", 10))
            self.remeasure_button.grid(row=15, column=0, columnspan=2, sticky="ew", padx=S(14), pady=S(4))
            self.next_tray_button = tk.Button(left, text="Next tray", command=self.next_tray, state="disabled", relief="flat", font=("TkDefaultFont", 10))
            self.next_tray_button.grid(row=16, column=0, columnspan=2, sticky="ew", padx=S(14), pady=(S(4), S(14)))

            self.grid = TrayGrid(right, on_tile_click=self.on_tile_click)
            self.grid.pack(anchor="nw")
            legend = tk.Frame(right, bg=PAGE_BG)
            legend.pack(anchor="w", pady=(S(6), 0))
            for state, label in GRID_LEGEND:
                bg, fg = GRID_COLOURS[state]
                tk.Label(legend, text=label, bg=bg, fg=fg, font=("TkDefaultFont", 8), padx=S(6), pady=S(2)).pack(side="left", padx=(0, S(4)))
            self.progress = ttk.Progressbar(right, orient="horizontal", mode="determinate", length=S(600))
            self.progress.pack(anchor="w", pady=(S(8), 0))
            self.status_var = tk.StringVar(value="Enter the lot, tray and your name, then Start lot.")
            tk.Label(right, textvariable=self.status_var, bg=PAGE_BG, fg=TEXT_DARK, font=("TkDefaultFont", 11), anchor="w", justify="left", wraplength=S(900)).pack(anchor="w", pady=(S(6), 0))
            self.summary_var = tk.StringVar(value="")
            tk.Label(right, textvariable=self.summary_var, bg=PAGE_BG, fg=MUTED_FG, font=("TkDefaultFont", 10), anchor="w", justify="left", wraplength=S(900)).pack(anchor="w")
            self._set_step(0)

        def _set_step(self, index: int) -> None:
            for i, (widget, (label, implemented)) in enumerate(zip(self.step_labels, STEP_RAIL)):
                if not implemented:
                    continue
                widget.configure(fg=ELTEC_BLUE if i == index else ("#14532d" if i < index else "#93a1bd"))

        def set_status(self, text: str) -> None:
            self.status_var.set(text)

        # -- flow ---------------------------------------------------------------
        def start_lot(self) -> None:
            lot = self.lot_var.get().strip()
            tester = self.tester_var.get().strip()
            try:
                tray = int(self.tray_var.get().strip() or "1")
            except ValueError:
                messagebox.showerror(APP_TITLE, "Tray number must be a whole number.")
                return
            if not lot or not tester:
                messagebox.showerror(APP_TITLE, "Lot number and tester name are required.")
                return
            self.controller = TrayController(self.device, lot=lot, tray_number=tray, tester_name=tester)
            try:
                info = self.controller.start()
            except daq.DaqError as exc:
                messagebox.showerror(APP_TITLE, f"DAQ not ready:\n{exc}")
                self.controller = None
                return
            self.start_number_var.set(str(next_sensor_number_for_lot(lot)))
            self.set_status(f"Connected: {info.summary()}. Load the tray: high offsets turn red - pull them. Click a 0 V tile to mark it empty.")
            self.start_button.configure(state="disabled")
            self.lock_button.configure(state="normal")
            self._set_step(1)
            self._start_polling()

        def _start_polling(self) -> None:
            self._polling = True
            threading.Thread(target=self._poll_worker, name="offset-poll", daemon=True).start()

        def _poll_worker(self) -> None:
            while self._polling and self.controller is not None:
                try:
                    volts = self.controller.poll_offsets()
                except daq.DaqError as exc:
                    self._post(lambda exc=exc: self.set_status(f"Offset poll failed: {exc}"))
                    time.sleep(1.0)
                    continue
                self._post(lambda volts=volts: self._render_live(volts))
                time.sleep(1.0 / OFFSET_POLL_HZ)

        def _render_live(self, volts: np.ndarray) -> None:
            controller = self.controller
            if controller is None:
                return
            for channel, position in enumerate(daq.POSITIONS):
                state = controller.live_tile_state(position)
                headline, detail = tile_texts(None, live_offset_v=float(volts[channel]))
                if state is aa.TileState.OFFSET_FAIL:
                    detail = "HO - pull"
                elif state is aa.TileState.EMPTY:
                    headline, detail = "empty", "click if loaded"
                elif state is aa.TileState.UNKNOWN:
                    detail = "empty? click"
                self.grid.set_tile(position, state=state, headline=headline, detail=detail)

        def on_tile_click(self, position: str) -> None:
            controller = self.controller
            if controller is None or controller.phase is not Phase.LOAD_OFFSET:
                return
            new = controller.toggle_occupancy(position)
            self.set_status(f"Position {position} marked {new.value}.")
            if controller.live_offsets is not None:
                self._render_live(controller.live_offsets)

        def lock_tray(self) -> None:
            controller = self.controller
            if controller is None:
                return
            try:
                start = int(self.start_number_var.get().strip()) if self.start_number_var.get().strip() else None
            except ValueError:
                messagebox.showerror(APP_TITLE, "First sensor # must be a whole number.")
                return
            self._polling = False
            try:
                lock = controller.lock_tray(start_number=start)
            except (ValueError, RuntimeError, daq.DaqError) as exc:
                messagebox.showerror(APP_TITLE, str(exc))
                self._start_polling()
                return
            self._render_lock(lock)
            self.lock_button.configure(state="disabled")
            self.noise_button.configure(state="normal")
            self._set_step(2)
            ho = ", ".join(lock.ho_positions) or "none"
            self.set_status(f"Tray locked: {len(lock.loaded_positions)} loaded, sensor numbers {lock.start_number}-"
                            f"{lock.start_number + len(lock.loaded_positions) - 1}. High-offset parts recorded and to be pulled: {ho}. "
                            f"Then Start noise test.")

        def _render_lock(self, lock: LockSnapshot) -> None:
            for channel, position in enumerate(daq.POSITIONS):
                occ = lock.occupancy[channel]
                number = lock.sensor_numbers.get(position)
                if occ is aa.Occupancy.EMPTY:
                    self.grid.set_tile(position, state=aa.TileState.EMPTY, headline="empty")
                elif position in lock.ho_positions:
                    self.grid.set_tile(position, state=aa.TileState.OFFSET_FAIL, headline=f"{lock.offset_initial_v[channel]:.3f} V", detail="HO - pulled", sensor_number=number)
                else:
                    self.grid.set_tile(position, state=aa.TileState.LOADED, headline=f"{lock.offset_initial_v[channel]:.3f} V", detail="locked", sensor_number=number)

        def start_noise_phase(self) -> None:
            controller = self.controller
            if controller is None or controller.state.lock is None:
                return
            self.noise_button.configure(state="disabled")
            self.skip_button.configure(state="normal")
            self.cancel_button.configure(state="normal")
            self.save_button.configure(state="disabled")
            self.remeasure_button.configure(state="disabled")
            self._cancel.clear()
            self._skip_stabilisation.clear()
            self._set_step(3)
            seconds = float(self.capture_choice.get())
            self._worker = threading.Thread(target=self._noise_worker, args=(seconds,), name="noise-phase", daemon=True)
            self._worker.start()

        def _noise_worker(self, capture_seconds: float) -> None:
            controller = self.controller
            if controller is None:
                return
            # stabilisation countdown (TP120: 5 min after power-on); Skip shortens it, the actual wait is recorded
            total = controller.state.stabilisation_s if controller.state.stabilisation_s else NOISE_STABILISATION_S
            started = time.monotonic()
            waited = 0.0
            while waited < total and not self._skip_stabilisation.is_set() and not self._cancel.is_set():
                time.sleep(0.25)
                waited = time.monotonic() - started
                self._post(lambda w=waited: (self.progress.configure(value=100.0 * w / total), self.set_status(f"Stabilising (TP120: {total:.0f} s after power-on) - {total - w:.0f} s left")))
            if self._cancel.is_set():
                self._post(self._capture_cancelled)
                return
            actual_wait = min(waited, total)

            def progress(kind: str, fraction: float | None, message: str) -> None:
                self._post(lambda: (self.progress.configure(value=0.0 if fraction is None else 100.0 * fraction), self.set_status(message)))

            try:
                report = controller.run_noise_phase(
                    stabilisation_wait_s=actual_wait, progress=progress, cancelled=self._cancel.is_set, capture_seconds=capture_seconds,
                )
            except CaptureCancelled:
                self._post(self._capture_cancelled)
                return
            except daq.DaqError as exc:
                self._post(lambda exc=exc: (messagebox.showerror(APP_TITLE, f"DAQ error during the noise phase:\n{exc}"), self._capture_cancelled()))
                return
            self._post(lambda: self.on_tray_judged(report))

        def _capture_cancelled(self) -> None:
            self.set_status("Capture cancelled. Start noise test again when ready.")
            self.noise_button.configure(state="normal")
            self.skip_button.configure(state="disabled")
            self.cancel_button.configure(state="disabled")
            self.progress.configure(value=0.0)

        def on_tray_judged(self, report: TrayCaptureReport) -> None:
            controller = self.controller
            if controller is None:
                return
            self.skip_button.configure(state="disabled")
            self.cancel_button.configure(state="disabled")
            self.progress.configure(value=100.0)
            for result in list(controller.state.lock_results) + list(report.results):
                headline, detail = tile_texts(result)
                self.grid.set_tile(result.position, state=aa.tile_state_for(result), headline=headline, detail=detail, sensor_number=result.sensor_number)
            counts = controller.summary_counts()
            if report.rig_fault:
                self.set_status(f"Tray NOT MEASURED after {report.attempts_used} attempt(s): {report.rig_fault}. Save records it; Re-measure tries again.")
            else:
                self.set_status(f"Judged (PROVISIONAL). Save tray writes {len(report.results)} rows + the raw capture; Re-measure runs the noise phase again.")
            self.summary_var.set(
                f"loaded {counts['loaded']} · PASS {counts['pass']} · offset FAIL {counts['fail_offset']} · noise FAIL {counts['fail_noise']} · "
                f"noise low {counts['noise_low']} · measured, no limit {counts['no_limit']} · not measured {counts['not_measured']}"
            )
            self.save_button.configure(state="normal")
            self.remeasure_button.configure(state="normal")
            self._set_step(5)

        def save_tray(self) -> None:
            controller = self.controller
            if controller is None:
                return
            try:
                outcome = controller.save_tray()
            except (RuntimeError, OSError) as exc:
                messagebox.showerror(APP_TITLE, f"Could not save the tray:\n{exc}")
                return
            self.save_button.configure(state="disabled")
            self.next_tray_button.configure(state="normal")
            self.set_status(f"Saved {outcome['rows']} rows to {outcome['csv']}; raw capture {outcome['raw'] or '-'}; grid {outcome['png'] or '-'}.")

        def remeasure_tray(self) -> None:
            controller = self.controller
            if controller is None:
                return
            attempt = controller.remeasure()
            self._render_lock(controller.state.lock)
            self.save_button.configure(state="disabled")
            self.remeasure_button.configure(state="disabled")
            self.noise_button.configure(state="normal")
            self.set_status(f"Attempt {attempt}: Start noise test again (the stabilisation wait can be skipped if the parts stayed powered).")
            self._set_step(3)

        def next_tray(self) -> None:
            controller = self.controller
            if controller is None:
                return
            self.tray_var.set(str(controller.state.tray_number + 1))
            self.controller = None
            for position in daq.POSITIONS:
                self.grid.set_tile(position, state=aa.TileState.EMPTY)
            self.summary_var.set("")
            self.progress.configure(value=0.0)
            self.next_tray_button.configure(state="disabled")
            self.save_button.configure(state="disabled")
            self.remeasure_button.configure(state="disabled")
            self.noise_button.configure(state="disabled")
            self.start_button.configure(state="normal")
            self.set_status("Next tray: check the tray number, then Start lot.")
            self._set_step(0)

        def on_close(self) -> None:
            self._polling = False
            self._cancel.set()
            try:
                if self.controller is not None:
                    self.controller.close()
                else:
                    self.device.close()
            except Exception:
                pass
            self.destroy()

    return TrayGrid, ArrayTesterApp


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--simulate", action="store_true", help="run against SimulatedDaq (no hardware)")
    args = parser.parse_args(argv)
    simulate = args.simulate or os.environ.get("ELTEC_ARRAY_SIMULATE", "") == "1"
    enable_windows_dpi_awareness()
    _grid, app_class = build_gui_classes()
    app = app_class(simulate=simulate)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
