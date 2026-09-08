"""Eltec 40623 Array Tester - fifty detectors at once on the DAQ array rig (TP120).

The model application of the array rig for the Eltec 40623 (test procedure
TP120 rev W, ``docs/TP120(40623).pdf``). It is normally launched from the
array selector (``array_rig/eltec_array_tester.py``) and runs standalone with
this directory as cwd. It measures the two TP120 tests that need no emitter:

* **Offset check** - the technician powers the rig and presses "Measure
  offset". Every loaded detector is checked against the full provisional
  offset window; low/dead values are shown for rechecking after settling.
  Repeated checks let the technician replace bad parts or mark sockets
  empty. Every check is retained in the tray history without consuming
  sensor numbers. The legacy continuous-poll/lock API remains available
  to engineering callers.
* **Noise** - after all loaded offsets pass, the technician turns on the
  vacuum, waits for the required setting, then presses "Measure noise".
  The accepted offset snapshot is frozen and sensor numbers assigned.
  The rig streams all fifty channels wideband (1000 scans/s per channel)
  for the TP120 hold time (60 s), judging each channel's windowed pk-pk
  in the single rig's 0.85-22 Hz band (``array_analysis``). Raw captures
  are saved so future calibrated bands or limits can be replayed.

Verdict status: **CALIBRATION PENDING / PROVISIONAL**. TP120's noise limits
(10.0-37.9 mV) are DMM readings behind the legacy amplifier 9000232 and
rectifier-hold 9000272; no pin-level equivalent exists yet, so noise tiles
show the measured value and "no limit yet" until the paired lot derives the
chain factor (``engineer_tools/array_parity/array_noise_parity.py``). The offset limits
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
import json
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
# 0.2 (2026-09-08): explicit repeatable offset checks before noise; the
# calibration remains pending (noise limits None, offsets provisional).
APP_VERSION = "0.2"
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
# 200 kS/s aggregate (40 % of the 500 kS/s ADC ceiling) and 400 KB/s over USB
# (~40 % of the driver's 1 MB/s throughput figure; the link itself is USB 2.0
# high-speed, 480 Mb/s, bench-verified 2026-09-02). Proven or adjusted by the bench probe
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
# A stream that stops delivering data (the pacing clock ticking with the
# trigger bit cleared - the 2026-09-02 bench finding, since fixed in the
# backend -, a pulled cable, a driver stall) used to hold the tray at "quiet
# wait" forever. After this many seconds without a chunk the attempt is
# abandoned as a rig fault and the retry policy above takes over. Generous
# next to the 64 000-byte callback buffers (~0.16 s each) so a busy laptop
# never trips it.
STREAM_NO_DATA_TIMEOUT_S = 5.0
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
# after power-on. The operator screen starts this wait after vacuum confirmation;
# the actual wait is recorded on every row. Simulation skips the wall-clock wait.
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
    no_data_timeout_s: float = STREAM_NO_DATA_TIMEOUT_S
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


def _check_no_data(last_data_monotonic: float, plan: CapturePlan, stage: str) -> None:
    """Raise when the stream has been silent longer than the plan allows.

    ``read_stream`` returning ``None`` is normal for a moment (the callback
    buffers are ~0.16 s apart); a stream that produces nothing at all is a
    rig fault and must fail into the retry path, never hold the tray.
    """

    silent = time.monotonic() - last_data_monotonic
    if silent > plan.no_data_timeout_s:
        raise daq.StreamTimeoutError(
            f"no data from the stream for {silent:.1f} s during the {stage} (limit {plan.no_data_timeout_s:.0f} s)"
        )


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
    last_data = time.monotonic()
    while collected < max_samples:
        if cancelled and cancelled():
            raise CaptureCancelled("cancelled during the quiet wait")
        chunk = _read_scans_volts(device, plan)
        if chunk is None:
            _check_no_data(last_data, plan, "quiet wait")
            continue
        last_data = time.monotonic()
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
            last_data = time.monotonic()
            while got < wanted:
                if cancelled and cancelled():
                    raise CaptureCancelled("cancelled during the capture")
                chunk = _read_scans_volts(device, plan)
                if chunk is None:
                    _check_no_data(last_data, plan, "capture")
                    continue
                last_data = time.monotonic()
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
        # Explicit operator checks keep their own immutable measurement basis.
        # The legacy polling/lock API remains available to engineering callers.
        self.offset_checked = False
        self.offset_measurement_count = 0
        self._operator_offset_workflow = False
        self._measured_offsets: np.ndarray | None = None
        self._measured_loaded: frozenset[str] = frozenset()
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
        if self._operator_offset_workflow:
            return aa.Occupancy.LOADED
        if volts is None and self.live_offsets is not None:
            volts = float(self.live_offsets[daq.channel_for_position(position)])
        if volts is not None and volts < self.offset_limits.dead_v:
            return aa.Occupancy.UNKNOWN
        return aa.Occupancy.LOADED

    def set_occupancy(self, position: str, occupancy: aa.Occupancy | None) -> None:
        daq.channel_for_position(position)
        if self._operator_offset_workflow:
            if self.phase is not Phase.LOAD_OFFSET:
                raise RuntimeError("Tray positions cannot change during or after a noise measurement.")
            if occupancy is aa.Occupancy.UNKNOWN:
                raise ValueError("Mark a position loaded or empty.")
        previous = self.effective_occupancy(position)
        if occupancy is None:
            self.occupancy_choice.pop(position, None)
        else:
            self.occupancy_choice[position] = occupancy
        # Removing a detector keeps the remaining checked readings valid. A
        # newly loaded detector always needs another explicit offset check.
        if previous is not aa.Occupancy.LOADED and self.effective_occupancy(position) is aa.Occupancy.LOADED:
            self.offset_checked = False

    def toggle_occupancy(self, position: str) -> aa.Occupancy:
        """UNKNOWN/LOADED -> EMPTY -> LOADED -> EMPTY ... (technician click)."""

        current = self.effective_occupancy(position)
        new = aa.Occupancy.LOADED if current is aa.Occupancy.EMPTY else aa.Occupancy.EMPTY
        self.set_occupancy(position, new)
        return new

    def live_tile_state(self, position: str) -> aa.TileState:
        if self.live_offsets is None:
            return aa.TileState.LOADED if self.effective_occupancy(position) is aa.Occupancy.LOADED else aa.TileState.EMPTY
        volts = float(self.live_offsets[daq.channel_for_position(position)])
        if self._operator_offset_workflow:
            occupancy = self.effective_occupancy(position)
            if occupancy is aa.Occupancy.EMPTY:
                return aa.TileState.EMPTY
            offset_class = aa.classify_offset(volts, occupancy=occupancy, limits=self.offset_limits)
            return aa.TileState.LOADED if offset_class is aa.OffsetClass.OK else aa.TileState.OFFSET_FAIL
        return aa.tile_state_for_live_offset(volts, occupancy=self.effective_occupancy(position, volts), limits=self.offset_limits)

    def unknown_positions(self) -> tuple[str, ...]:
        return tuple(p for p in daq.POSITIONS if self.effective_occupancy(p) is aa.Occupancy.UNKNOWN)

    def measure_offsets(self) -> np.ndarray:
        """Check the current tray, retaining every check before replacements.

        The operator starts with all fifty positions loaded and explicitly
        marks absent parts empty. Low/dead values are visible recheck failures,
        not a prompt to guess whether a detector is present. No final sensor
        numbers or result rows are assigned until ``prepare_noise``.
        """

        if self.phase is not Phase.LOAD_OFFSET:
            raise RuntimeError("Measure offsets before starting the noise measurement.")
        self._operator_offset_workflow = True
        self.offset_checked = False
        # A legacy UNKNOWN choice must not silently exclude a zero-volt part.
        self.occupancy_choice = {
            p: occ for p, occ in self.occupancy_choice.items() if occ is not aa.Occupancy.UNKNOWN
        }
        with self.hardware_lock:
            volts = np.asarray(self.device.read_scan_volts_median(reads=OFFSET_POLL_READS), dtype=np.float64)
        if volts.shape != (daq.CHANNEL_COUNT,) or not np.all(np.isfinite(volts)):
            raise ValueError("Offset measurement did not return fifty finite readings; measure offset again.")
        loaded = frozenset(p for p in daq.POSITIONS if self.effective_occupancy(p) is aa.Occupancy.LOADED)
        readings = {
            p: {
                "occupancy": self.effective_occupancy(p).value,
                "offset_v": float(volts[c]),
                "offset_class": aa.classify_offset(
                    float(volts[c]), occupancy=self.effective_occupancy(p), limits=self.offset_limits,
                ).value,
            }
            for c, p in enumerate(daq.POSITIONS)
        }
        bad = tuple(p for p in daq.POSITIONS if p in loaded and readings[p]["offset_class"] != aa.OffsetClass.OK.value)
        check_number = self.offset_measurement_count + 1
        tray_history.append_tray_event(
            self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
            tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_OFFSET_MEASURED,
            phase=Phase.LOAD_OFFSET.value, tester_name=self.state.tester_name, loaded_count=len(loaded),
            detail=json.dumps({
                "offset_measurement": check_number, "bad_positions": bad, "readings": readings,
                "offset_min_v": self.offset_limits.min_v, "offset_max_v": self.offset_limits.max_v,
                "simulated": bool(self.device.info and self.device.info.simulated),
            }, separators=(",", ":")),
        )
        self._measured_offsets = volts.copy()
        self._measured_loaded = loaded
        self.live_offsets = volts.copy()
        self.offset_measurement_count = check_number
        self.offset_checked = True
        return volts.copy()

    def offset_bad_positions(self) -> tuple[str, ...]:
        """Currently loaded positions outside the full offset window."""

        if self._measured_offsets is None:
            return ()
        return tuple(
            p for c, p in enumerate(daq.POSITIONS)
            if self.effective_occupancy(p) is aa.Occupancy.LOADED
            and aa.classify_offset(float(self._measured_offsets[c]), occupancy=aa.Occupancy.LOADED,
                                   limits=self.offset_limits) is not aa.OffsetClass.OK
        )

    def offset_good_positions(self) -> tuple[str, ...]:
        """Currently loaded positions that passed their last explicit check."""

        if self._measured_offsets is None:
            return ()
        bad = set(self.offset_bad_positions())
        return tuple(p for p in daq.POSITIONS if p in self._measured_loaded and p not in bad
                     and self.effective_occupancy(p) is aa.Occupancy.LOADED)

    def prepare_noise(self, *, start_number: int | None = None) -> LockSnapshot:
        """Freeze the accepted offset measurement without reading the DAQ again."""

        if self.phase is not Phase.LOAD_OFFSET:
            raise RuntimeError("Prepare noise after measuring offsets and before starting noise.")
        if not self.offset_checked or self._measured_offsets is None:
            raise ValueError("Measure offset for the currently loaded detectors first.")
        occupancy = [self.effective_occupancy(p) for p in daq.POSITIONS]
        loaded = tuple(p for p, occ in zip(daq.POSITIONS, occupancy) if occ is aa.Occupancy.LOADED)
        if not loaded:
            raise ValueError("Load at least one detector, then measure offset.")
        if any(p not in self._measured_loaded for p in loaded):
            raise ValueError("Newly loaded detectors need another offset measurement.")
        bad = self.offset_bad_positions()
        if bad:
            raise ValueError("Recheck, replace, or mark these offset positions empty before noise: " + " ".join(bad))
        start = start_number if start_number is not None else next_sensor_number_for_lot(self.state.lot, self.results_root)
        numbers = assign_sensor_numbers(occupancy, start)
        lock = LockSnapshot(
            occupancy=occupancy, sensor_numbers=numbers, offset_initial_v=self._measured_offsets.copy(),
            ho_positions=(), start_number=start, locked_at=_dt.datetime.now().isoformat(timespec="seconds"),
        )
        tray_history.append_tray_event(
            self.attempts_path, lot_number=self.state.lot, tray_number=self.state.tray_number,
            tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_LOCKED, phase=Phase.LOCKED.value,
            tester_name=self.state.tester_name, loaded_count=len(loaded),
            first_sensor_number=min(numbers.values()), last_sensor_number=max(numbers.values()),
            detail=f"Accepted offset measurement {self.offset_measurement_count}; all loaded offsets within limits",
        )
        self.state.lock = lock
        self.state.lock_results = []
        self.state.report = None
        self.state.saved = False
        self.phase = Phase.LOCKED
        return lock

    def lock_tray(self, *, start_number: int | None = None) -> LockSnapshot:
        if self._operator_offset_workflow:
            return self.prepare_noise(start_number=start_number)
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
        import shutil
        import tempfile

        lock = self.state.lock
        report = self.state.report
        if lock is None or report is None:
            raise RuntimeError("Nothing to save: lock the tray and run the noise phase first.")
        if self.state.saved:
            raise RuntimeError("This tray attempt is already saved; use Re-measure for another attempt.")
        # Retry save resumes completed stages for this exact measurement. In
        # particular, an attempts-log failure after writing the result rows
        # must never append those fifty result rows a second time.
        pending = getattr(self, "_pending_save", None)
        if pending is None or pending["report"] is not report or pending["attempt"] != self.state.tray_attempt:
            pending = {"report": report, "attempt": self.state.tray_attempt}
            self._pending_save = pending

        def atomic_append(path: Path, append: Callable[[Path], Any]) -> Any:
            """Preserve the existing file if an append or disk flush fails."""

            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as target:
                    if path.exists():
                        with path.open("rb") as source:
                            shutil.copyfileobj(source, target)
                value = append(temporary_path)
                with temporary_path.open("ab") as target:
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary_path, path)
                return value
            finally:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass  # Never mask the storage error that the operator needs.

        if "raw" not in pending:
            raw_path = ""
            if report.capture is not None:
                if "raw_target" not in pending:
                    pending["raw_target"] = raw_capture_path(self.state.lot, self.state.tray_number, self.results_root)
                raw_path = str(save_tray_raw_capture(
                    pending["raw_target"], report.capture, lock,
                    lot=self.state.lot, tray_number=self.state.tray_number, tray_attempt=self.state.tray_attempt,
                    daq_info=self.device.info, plan=self.plan,
                ))
            pending["raw"] = raw_path
        raw_path = pending["raw"]
        if "png" not in pending:
            if "png_target" not in pending:
                pending["png_target"] = grid_snapshot_path(self.state.lot, self.state.tray_number, self.results_root)
            pending["png"] = save_grid_snapshot(
                list(self.state.lock_results) + list(report.results), path=pending["png_target"],
                title=f"{APP_TITLE} - lot {self.state.lot} tray {self.state.tray_number} attempt {self.state.tray_attempt} "
                      f"({aa.CALIBRATION_STATUS}, {aa.VERDICT_STATUS})",
            )
        png = pending["png"]
        if "rows" not in pending:
            ctx = self._row_context(report, raw_path=raw_path, png_path=str(png) if png else "")
            rows = [
                position_row(r, ctx, comment=self.state.comments.get(r.position, ""), failure_tag=self.state.failure_tags.get(r.position))
                for r in report.results
            ]
            pending["rows"] = atomic_append(self.csv_path, lambda path: append_position_rows(path, rows)) if rows else 0
        written = pending["rows"]
        atomic_append(self.attempts_path, lambda path: tray_history.append_tray_event(
            path, lot_number=self.state.lot, tray_number=self.state.tray_number,
            tray_attempt=self.state.tray_attempt, event=tray_history.EVENT_SAVED, phase=Phase.SAVED.value,
            tester_name=self.state.tester_name, loaded_count=len(lock.loaded_positions), ho_positions=" ".join(lock.ho_positions),
            first_sensor_number=min(lock.sensor_numbers.values()) if lock.sensor_numbers else 0,
            last_sensor_number=max(lock.sensor_numbers.values()) if lock.sensor_numbers else 0,
            detail=f"{written} rows; raw {raw_path or '-'}; grid {png or '-'}",
            stabilisation_wait_s=report.stabilisation_wait_s, capture_seconds=self.state.capture_seconds,
        ))
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


# Strong, consistent verdict colours; labels carry the meaning as well as colour.
OPERATOR_TILE_COLOURS = {
    aa.TileState.EMPTY: ("#e8edf4", "#67758c"),
    aa.TileState.LOADED: ("#e1e9fa", ELTEC_BLUE),
    aa.TileState.UNKNOWN: ("#e1e9fa", ELTEC_BLUE),
    aa.TileState.SETTLING: ("#fff0c2", "#815500"),
    aa.TileState.OFFSET_FAIL: ("#c72f46", "#ffffff"),
    aa.TileState.NOISE_FAIL: ("#c72f46", "#ffffff"),
    aa.TileState.NOISE_LOW: ("#c72f46", "#ffffff"),
    aa.TileState.PASS: ("#16824f", "#ffffff"),
    aa.TileState.NO_LIMIT: ("#fff0c2", "#815500"),
    aa.TileState.NOT_MEASURED: ("#e8edf4", "#67758c"),
}


def simulation_results_root() -> Path:
    """Practice data never shares production numbering or result files."""
    import tempfile

    override = os.environ.get(RESULTS_ROOT_ENV, "").strip()
    return Path(override).expanduser() / "simulation" if override else Path(tempfile.gettempdir()) / "eltec-array-simulation"


def operator_simulator() -> daq.SimulatedDaq:
    """A fast practice tray with offset rejects and repeatable noise examples."""
    from dataclasses import replace

    profile = replace(daq.default_sim_profile(), settle_drop_v=0.0,
                      noise_rms_uv={"3-6": 1800.0, "4-3": 0.0})
    return daq.SimulatedDaq(profile, real_time=False)


def simulation_noise_limits() -> aa.NoiseLimits:
    # Illustrative ONLY: these must never become the production pin limits.
    return aa.NoiseLimits(low_mv=0.01, high_mv=0.3,
                          provenance="SIMULATION ONLY: illustrative demo limits, not calibrated for hardware")


def build_gui_classes():
    """Tk classes are built lazily so the acquisition core stays headless."""
    import math
    import queue
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import messagebox, ttk

    class TrayGrid(tk.Canvas):
        """Responsive top view of the fixture, with round, labelled sockets."""

        def __init__(self, master, *, on_tile_click: Callable[[str], None], on_empty_click=None) -> None:
            super().__init__(master, width=S(1000), height=S(400), bg=CARD_BG, highlightthickness=0)
            self._on_tile_click = on_tile_click
            self._on_empty_click = on_empty_click or on_tile_click
            self._items: dict[str, dict[str, int]] = {}
            self._tile_data = {}
            self.show_details = False
            self._radius = 25
            self._fonts = {}
            self._board = self.create_rectangle(0, 0, 1, 1, fill="#f2f5fa", outline=CARD_BORDER)
            self._columns = [self.create_text(0, 0, text=str(c + 1), fill=MUTED_FG) for c in range(daq.COLS)]
            self._rows = [self.create_text(0, 0, text=str(r + 1), fill=MUTED_FG) for r in range(daq.ROWS)]
            for position in daq.POSITIONS:
                tag = f"socket-{position}"
                items = {
                    "shadow": self.create_oval(0, 0, 1, 1, fill="#ccd5e3", outline="", tags=tag),
                    "rim": self.create_oval(0, 0, 1, 1, fill="#ffffff", outline="#b9c7db", width=2, tags=tag),
                    "rect": self.create_oval(0, 0, 1, 1, fill="#e1e9fa", outline="", tags=tag),
                    "label": self.create_text(0, 0, text=position, tags=tag),
                    "headline": self.create_text(0, 0, text="", tags=tag),
                    "detail": self.create_text(0, 0, text="", tags=tag),
                    "number": self.create_text(0, 0, text="", fill=MUTED_FG, tags=tag),
                }
                self._items[position] = items
                self.tag_bind(tag, "<Button-1>", lambda _e, p=position: self._on_tile_click(p))
                self.tag_bind(tag, "<Button-3>", lambda _e, p=position: self._on_empty_click(p))
            self.bind("<Configure>", self._layout)

        def _layout(self, event=None) -> None:
            width, height = (event.width, event.height) if event is not None else (self.winfo_width(), self.winfo_height())
            left, top = S(30), S(25)
            cw = (width - left - S(12)) / daq.COLS
            ch = (height - top - S(8)) / daq.ROWS
            radius = max(18, min(cw * .40, ch * .40, S(43)))
            self._radius = radius
            self.coords(self._board, left - S(10), top - S(8), width - S(4), height - S(4))
            for c, item in enumerate(self._columns):
                self.coords(item, left + (c + .5) * cw, S(10))
            for r, item in enumerate(self._rows):
                self.coords(item, S(10), top + (r + .5) * ch)
            for channel, position in enumerate(daq.POSITIONS):
                r, c = divmod(channel, daq.COLS)
                x, y = left + (c + .5) * cw, top + (r + .5) * ch - 2
                items = self._items[position]
                self.coords(items["shadow"], x-radius-2, y-radius+2, x+radius+2, y+radius+6)
                self.coords(items["rim"], x-radius-2, y-radius-2, x+radius+2, y+radius+2)
                self.coords(items["rect"], x-radius+1, y-radius+1, x+radius-1, y+radius-1)
                self.coords(items["label"], x, y-radius*.49)
                self.coords(items["headline"], x, y+1 if self.show_details else y+radius*.20)
                self.coords(items["detail"], x, y+radius*.51)
                self.coords(items["number"], x, y+radius+11)
                self._fit_text(items)

        def _fit_text(self, items) -> None:
            # Pixel fonts and measured widths keep readings inside the socket at
            # both Windows DPI scales and the minimum supported window size.
            for name in ("label", "headline", "detail", "number"):
                size = max(9, min(18, int(self._radius * (.45 if name in ("label", "headline") else .34))))
                if name == "headline" and not self.show_details:
                    size = max(10, min(22, int(self._radius * .58)))
                width = self._radius * (1.55 if name != "headline" else 1.85)
                value = self.itemcget(items[name], "text")
                while True:
                    key = (size, name == "number")
                    if key not in self._fonts:
                        self._fonts[key] = tkfont.Font(self, family="TkDefaultFont", size=-size,
                                                       weight="normal" if name == "number" else "bold")
                    font = self._fonts[key]
                    if size <= 8 or font.measure(value) <= width:
                        break
                    size -= 1
                self.itemconfigure(items[name], font=font)

        def set_tile(self, position: str, *, state: aa.TileState, headline: str = "", detail: str = "", sensor_number: int | None = None) -> None:
            self._tile_data[position] = (state, headline, detail, sensor_number)
            self._paint_tile(position)

        def _paint_tile(self, position: str) -> None:
            state, headline, detail, sensor_number = self._tile_data[position]
            bg, fg = OPERATOR_TILE_COLOURS[state]
            items = self._items[position]
            status = {
                aa.TileState.PASS: "PASS",
                aa.TileState.OFFSET_FAIL: "FAIL",
                aa.TileState.NOISE_FAIL: "FAIL",
                aa.TileState.NOISE_LOW: "FAIL",
                aa.TileState.NO_LIMIT: "NO LIMIT",
                aa.TileState.NOT_MEASURED: "NOT READ",
                aa.TileState.EMPTY: "EMPTY",
            }.get(state, detail or "WAITING")
            self.itemconfigure(items["rect"], fill=bg)
            self.itemconfigure(items["label"], fill=fg)
            self.itemconfigure(items["headline"], text=headline if self.show_details else status, fill=fg)
            visibility = "normal" if self.show_details else "hidden"
            self.itemconfigure(items["detail"], text=detail, fill=fg, state=visibility)
            self.itemconfigure(items["number"], text="" if sensor_number is None else f"#{sensor_number}", state=visibility)
            self._fit_text(items)

        def set_details_visible(self, visible: bool) -> None:
            self.show_details = visible
            for position in self._tile_data:
                self._paint_tile(position)
            if self.winfo_width() > 1:
                self._layout()

    class ArrayTesterApp(tk.Tk):
        def __init__(self, *, device: daq.DaqDevice | None = None, simulate: bool = False) -> None:
            super().__init__()
            self.title(f"{APP_TITLE} v{APP_VERSION}")
            self.configure(bg=PAGE_BG)
            self.minsize(S(1000), S(720))
            self.simulate = simulate or isinstance(device, daq.SimulatedDaq) or bool(getattr(getattr(device, "info", None), "simulated", False))
            self.device = device or (operator_simulator() if self.simulate else daq.AiousbDaq())
            self.controller: TrayController | None = None
            self.drive: DriveDevice | None = None
            self._tray_number = 1
            self._last_lot = ""
            self._busy = False
            self._closing = False
            self._worker: threading.Thread | None = None
            self._cancel = threading.Event()
            self._callbacks = queue.Queue()
            self._save_error = False
            self._empty_positions: set[str] = set()
            self._build()
            self._queue_job = self.after(40, self._drain_callbacks)
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.bind("<Escape>", lambda _e: self.stop())
            self.start_maximized()

        def start_maximized(self) -> None:
            try:
                if self.tk.call("tk", "windowingsystem") == "x11":
                    self.after(0, lambda: self.attributes("-zoomed", True))
                else:
                    self.state("zoomed")
            except tk.TclError:
                pass

        def _post(self, callback: Callable[[], None]) -> None:
            # Workers never call Tk, including after(); the main thread drains this queue.
            self._callbacks.put(callback)

        def _drain_callbacks(self) -> None:
            for _ in range(100):
                try:
                    callback = self._callbacks.get_nowait()
                except queue.Empty:
                    break
                callback()
            if self._closing:
                if not self._busy and self._callbacks.empty() and (self._worker is None or not self._worker.is_alive()):
                    if self._save_error:
                        self._closing = False
                        self.status_var.set("Could not save the completed measurement. Press Retry save before closing.")
                        self._queue_job = self.after(40, self._drain_callbacks)
                        return
                    self.device.close()
                    self.destroy()
                    return
            self._queue_job = self.after(40, self._drain_callbacks)

        def _build(self) -> None:
            header = tk.Frame(self, bg=ELTEC_BLUE_DEEP)
            header.pack(fill="x")
            badge = tk.Frame(header, bg="white", padx=S(10), pady=S(3))
            badge.pack(side="left", padx=S(22), pady=S(10))
            self.logo_image = None
            logo_path = find_logo_path()
            if logo_path:
                try:
                    source = tk.PhotoImage(file=str(logo_path))
                    factor = max(1, math.ceil(source.width()/130), math.ceil(source.height()/54))
                    self.logo_image = source.subsample(factor, factor)
                    self.iconphoto(True, source)
                except tk.TclError:
                    pass
            tk.Label(badge, image=self.logo_image, text="ELTEC" if self.logo_image is None else "", bg="white",
                     fg=ELTEC_RED, font=("TkDefaultFont", 22, "bold italic")).pack()
            title = tk.Frame(header, bg=ELTEC_BLUE_DEEP)
            title.pack(side="left", pady=S(10))
            tk.Label(title, text="40623 ARRAY TESTER", bg=ELTEC_BLUE_DEEP, fg="white",
                     font=("TkDefaultFont", 21, "bold")).pack(anchor="w")
            tk.Label(title, text="OFFSET  /  NOISE     ·     50 DETECTORS", bg=ELTEC_BLUE_DEEP, fg="#bcd0f7",
                     font=("TkDefaultFont", 10)).pack(anchor="w", pady=(S(3), 0))
            tk.Label(header, text="SIMULATION" if self.simulate else "ARRAY RIG", bg=ELTEC_RED if self.simulate else ELTEC_BLUE,
                     fg="white", padx=S(12), pady=S(6), font=("TkDefaultFont", 10, "bold")).pack(side="right", padx=S(24))
            self.banner = tk.Label(self, text=(
                "SIMULATION · No hardware needed · Demo pass/fail limits only · Production calibration PENDING"
                if self.simulate else "CALIBRATION PENDING · Offset limits provisional · Noise is recorded; pass/fail awaits calibrated limits"),
                bg=WARN_BG, fg=WARN_FG, font=("TkDefaultFont", 10), anchor="w", padx=S(24), pady=S(5))
            self.banner.pack(fill="x")

            # Reserve the action bar first so it stays visible on smaller screens.
            footer = tk.Frame(self, bg=CARD_BG, padx=S(24), pady=S(12))
            footer.pack(side="bottom", fill="x")
            self.footer_var = tk.StringVar(value="Results save automatically after noise measurement.")
            tk.Label(footer, textvariable=self.footer_var, bg=CARD_BG, fg=MUTED_FG,
                     font=("TkDefaultFont", 10), anchor="w", justify="left", wraplength=S(390)).pack(side="left")
            self.noise_button = self._button(footer, "Measure noise", self.start_noise_phase, "#16824f")
            self.noise_button.pack(side="right", padx=(S(10), 0))
            self.offset_button = self._button(footer, "Measure offset", self.measure_offset, ELTEC_BLUE)
            self.offset_button.pack(side="right", padx=(S(10), 0))
            self.stop_button = self._button(footer, "Stop", self.stop, "#c72f46")

            body = tk.Frame(self, bg=PAGE_BG, padx=S(24), pady=S(10))
            body.pack(fill="both", expand=True)
            setup = tk.Frame(body, bg=PAGE_BG)
            setup.pack(fill="x", pady=(0, S(10)))
            self.tester_var, self.lot_var = tk.StringVar(), tk.StringVar()
            self.entries = []
            for label, var in (("Technician name", self.tester_var), ("Batch number", self.lot_var)):
                field = tk.Frame(setup, bg=PAGE_BG)
                field.pack(side="left", padx=(0, S(24)))
                tk.Label(field, text=label, bg=PAGE_BG, fg=MUTED_FG, font=("TkDefaultFont", 10)).pack(anchor="w")
                entry = ttk.Entry(field, textvariable=var, font=("TkDefaultFont", 14), width=23)
                entry.pack(pady=(S(3), 0))
                self.entries.append(entry)
            self.tray_var = tk.StringVar(value="Tray 1  ·  up to 50 detectors")
            tk.Label(setup, textvariable=self.tray_var, bg=PAGE_BG, fg=MUTED_FG,
                     font=("TkDefaultFont", 11)).pack(side="right", anchor="s", pady=S(5))

            guide = tk.Frame(body, bg=CARD_BG, padx=S(18), pady=S(10), highlightthickness=1, highlightbackground=CARD_BORDER)
            guide.pack(fill="x", pady=(0, S(8)))
            self.step_var = tk.StringVar(value="1  /  Measure offset")
            tk.Label(guide, textvariable=self.step_var, bg=CARD_BG, fg=ELTEC_BLUE,
                     font=("TkDefaultFont", 17, "bold")).pack(anchor="w")
            self.status_var = tk.StringVar(value="")
            self.status_label = tk.Label(guide, textvariable=self.status_var, bg=CARD_BG, fg=TEXT_DARK,
                                        font=("TkDefaultFont", 11), justify="left", anchor="w", wraplength=S(1150))
            self.status_label.pack(fill="x", pady=(S(4), 0))
            guide.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(100, e.width-S(40))))
            self.vacuum_var = tk.BooleanVar(value=False)
            self.vacuum_check = tk.Checkbutton(guide, text="Vacuum is at the required setting (checked on the rig gauge)",
                                              variable=self.vacuum_var, command=self._refresh_controls, bg=CARD_BG,
                                              activebackground=CARD_BG, fg=TEXT_DARK, font=("TkDefaultFont", 11), anchor="w")

            tray_card = tk.Frame(body, bg=CARD_BG, padx=S(12), pady=S(5), highlightthickness=1, highlightbackground=CARD_BORDER)
            tray_card.pack(fill="both", expand=True)
            summary = tk.Frame(tray_card, bg=CARD_BG)
            summary.pack(fill="x")
            tk.Label(summary, text="DETECTOR TRAY", bg=CARD_BG, fg=TEXT_DARK, font=("TkDefaultFont", 10, "bold")).pack(side="left")
            self.show_more_button = tk.Button(summary, text="Show more", command=self.toggle_details,
                                             bg=CARD_BG, fg=ELTEC_BLUE, activebackground=CARD_BG,
                                             activeforeground=ELTEC_BLUE_DEEP, relief="flat", borderwidth=0,
                                             font=("TkDefaultFont", 10), cursor="hand2", padx=S(12))
            self.show_more_button.pack(side="right")
            self.summary_var = tk.StringVar(value="50 positions  ·  waiting for offset measurement")
            tk.Label(summary, textvariable=self.summary_var, bg=CARD_BG, fg=MUTED_FG, font=("TkDefaultFont", 10)).pack(side="right")
            self.grid = TrayGrid(tray_card, on_tile_click=self.on_tile_click, on_empty_click=self.toggle_empty)
            self.grid.pack(fill="both", expand=True)
            self.map_hint_var = tk.StringVar(value="Positions are row-column, viewed from above. Click a physically empty socket to exclude it.")
            tk.Label(tray_card, textvariable=self.map_hint_var, bg=CARD_BG, fg=MUTED_FG,
                     font=("TkDefaultFont", 9), anchor="w").pack(fill="x", pady=(S(3), 0))
            self.progress = ttk.Progressbar(body, mode="determinate")
            self.progress.pack(fill="x", pady=(S(7), 0))
            self._reset_grid()
            self._offset_instructions()
            self._refresh_controls()
            self.entries[0].focus_set()

        def _button(self, parent, text, command, colour):
            return tk.Button(parent, text=text, command=command, bg=colour, fg="white", activebackground=colour,
                             activeforeground="white", disabledforeground="#e5e9f0", relief="flat", borderwidth=0,
                             font=("TkDefaultFont", 13, "bold"), padx=S(20), pady=S(13), cursor="hand2")

        def toggle_details(self) -> None:
            self.grid.set_details_visible(not self.grid.show_details)
            self.show_more_button.configure(text="Show less" if self.grid.show_details else "Show more")

        def _reset_grid(self) -> None:
            for position in daq.POSITIONS:
                self.grid.set_tile(position, state=aa.TileState.LOADED, headline="—", detail="WAITING")

        def _offset_instructions(self) -> None:
            self.step_var.set("1  /  Measure offset")
            self.status_var.set("Enter your name and batch number. Load the detectors, turn ON the rig's power switch, then press Measure offset."
                                if not self.simulate else "Enter your name and batch number, then press Measure offset. This practice run needs no hardware.")

        def _can_noise(self) -> bool:
            c = self.controller
            return bool(c and c.offset_checked and c.offset_good_positions() and not c.offset_bad_positions() and self.vacuum_var.get())

        def _refresh_controls(self) -> None:
            c = self.controller
            has_report = bool(c and c.state.report)
            saved = bool(c and c.state.saved)
            for entry in self.entries:
                entry.configure(state="disabled" if self._busy or c else "normal")
            self.offset_button.configure(text="Retry save" if self._save_error else "Next tray" if saved else "Measure offset",
                                         command=self.retry_save if self._save_error else self.next_tray if saved else self.measure_offset,
                                         state="disabled" if self._busy else "normal", bg=PRIMARY_DISABLED if self._busy else ELTEC_BLUE)
            noise_ready = self._can_noise() and not has_report
            if saved and c.state.report.rig_fault:
                noise_ready = self.vacuum_var.get()
            noise_enabled = noise_ready and not self._busy
            self.noise_button.configure(state="normal" if noise_enabled else "disabled",
                                        bg="#16824f" if noise_enabled else PRIMARY_DISABLED)
            if saved and not c.state.report.rig_fault:
                self.noise_button.pack_forget()
            elif not self.noise_button.winfo_manager():
                self.noise_button.pack(side="right", before=self.offset_button, padx=(S(10), 0))
            self.vacuum_check.configure(state="disabled" if self._busy or has_report else "normal")
            if self._busy:
                self.stop_button.pack(side="right", padx=(S(10), 0))
                self.stop_button.configure(state="normal", text="Stop")
            else:
                self.stop_button.pack_forget()

        def _run_worker(self, function) -> None:
            if self._busy:
                return
            self._busy = True
            self._cancel.clear()
            self._refresh_controls()
            self._worker = threading.Thread(target=function, name="array-measurement", daemon=True)
            self._worker.start()

        def measure_offset(self) -> None:
            if self._busy:
                return
            lot, tester = self.lot_var.get().strip(), self.tester_var.get().strip()
            if not tester or not lot:
                self.status_var.set("Enter technician name and batch number before measuring.")
                self.entries[0 if not tester else 1].focus_set()
                return
            self.vacuum_var.set(False)
            self.vacuum_check.pack_forget()
            self.progress.configure(value=0)
            self.step_var.set("1  /  Measuring offset…")
            self.status_var.set("Reading all 50 positions. Keep the rig powered.")

            def work():
                try:
                    if self.controller is None:
                        if lot != self._last_lot:
                            self._tray_number = 1
                        self._last_lot = lot
                        c = TrayController(self.device, lot=lot, tray_number=self._tray_number, tester_name=tester,
                                           results_root=simulation_results_root() if self.simulate else None,
                                           noise_limits=simulation_noise_limits() if self.simulate else aa.NoiseLimits())
                        # Continue an existing batch across app restarts as well as Next tray.
                        self._tray_number = max(self._tray_number, tray_history.highest_tray_number(c.attempts_path) + 1)
                        c.state.tray_number = self._tray_number
                        for position in self._empty_positions:
                            c.set_occupancy(position, aa.Occupancy.EMPTY)
                        c.start()
                        self.controller = c
                    c = self.controller
                    if c.state.report is not None:
                        raise RuntimeError("Save this tray before starting another.")
                    c.state.lock = None
                    c.phase = Phase.LOAD_OFFSET
                    c.measure_offsets()
                    if self._cancel.is_set():
                        c.offset_checked = False
                        self._post(self._stopped)
                    else:
                        self._post(self._offset_done)
                except Exception as exc:
                    self._post(lambda exc=exc: self._failed("Offset measurement", exc))
            self._run_worker(work)

        def _offset_done(self) -> None:
            self._busy = False
            self.progress.configure(value=100)
            self.tray_var.set(f"Tray {self._tray_number}  ·  up to 50 detectors")
            self._render_offsets()

        def _render_offsets(self) -> None:
            c = self.controller
            if c is None or c.live_offsets is None:
                return
            for channel, position in enumerate(daq.POSITIONS):
                empty = c.effective_occupancy(position) is aa.Occupancy.EMPTY
                volts = float(c.live_offsets[channel])
                kind = aa.classify_offset(volts, occupancy=aa.Occupancy.EMPTY if empty else aa.Occupancy.LOADED)
                if empty:
                    state, headline, detail = aa.TileState.EMPTY, "—", "EMPTY"
                elif not c.offset_checked:
                    state, headline, detail = aa.TileState.LOADED, "—", "RECHECK"
                else:
                    state = aa.TileState.PASS if kind is aa.OffsetClass.OK else aa.TileState.OFFSET_FAIL
                    headline = f"{volts:.3f} V"
                    detail = {aa.OffsetClass.OK: "OK", aa.OffsetClass.HO: "HIGH", aa.OffsetClass.HO_RAILED: "HIGH",
                              aa.OffsetClass.LO: "LOW", aa.OffsetClass.DEAD: "ZERO"}[kind]
                self.grid.set_tile(position, state=state, headline=headline, detail=detail)
            good, bad = c.offset_good_positions(), c.offset_bad_positions()
            empty_count = sum(c.effective_occupancy(p) is aa.Occupancy.EMPTY for p in daq.POSITIONS)
            self.summary_var.set(f"{len(good)} offset OK  ·  {len(bad)} to check  ·  {empty_count} empty")
            self.map_hint_var.set("Green = offset OK   ·   Red = check offset   ·   Gray = empty   ·   " +
                                  ("Click red to simulate replacement; right-click to mark empty." if self.simulate else "Click a physically empty socket to exclude it; click again to reload."))
            if not c.offset_checked:
                self.step_var.set("1  /  Check the updated tray")
                self.status_var.set("The tray has changed. Press Measure offset to check the loaded detectors again.")
            elif bad:
                self.step_var.set(f"1  /  Check {len(bad)} offset position{'s' if len(bad) != 1 else ''}")
                self.status_var.set("Replace the red detectors, then press Measure offset again. Low readings may still be settling: recheck before rejecting. "
                                    "No replacements left? Remove the bad detectors and mark those sockets empty.")
            elif good:
                self.step_var.set("2  /  Prepare vacuum")
                self.status_var.set(f"{len(good)} detectors are ready. Turn on the vacuum and wait until the gauge reaches the required setting. "
                                    "Confirm below, then press Measure noise. Keep power and vacuum on.")
            else:
                self.step_var.set("1  /  Load detectors")
                self.status_var.set("The tray is empty. Load detectors, click their sockets, then press Measure offset.")
            if c.offset_checked and good and not bad:
                self.vacuum_check.pack(fill="x", pady=(S(5), 0))
            else:
                self.vacuum_var.set(False)
                self.vacuum_check.pack_forget()
            self._refresh_controls()

        def _editable_tray(self) -> bool:
            return bool(not self._busy and self.controller and self.controller.live_offsets is not None and not self.controller.state.report)

        def on_tile_click(self, position: str) -> None:
            if not self._busy and self.controller is None:
                self.toggle_empty(position)
                return
            if not self._editable_tray():
                return
            c = self.controller
            c.state.lock = None
            c.phase = Phase.LOAD_OFFSET
            if self.simulate and (position in c.offset_bad_positions() or c.effective_occupancy(position) is aa.Occupancy.EMPTY):
                self.device.replace_simulated_positions((position,))
                c.set_occupancy(position, aa.Occupancy.LOADED)
                # Keep other bad positions visible while replacing several parts.
                self._simulation_replacement(position)
            else:
                self.toggle_empty(position)

        def _simulation_replacement(self, position: str) -> None:
            # The measured snapshot is invalid until Measure offset; don't render invented readings.
            self.controller.offset_checked = False
            self.grid.set_tile(position, state=aa.TileState.LOADED, headline="—", detail="RECHECK")
            self.vacuum_var.set(False)
            self.vacuum_check.pack_forget()
            self.status_var.set(f"Simulated replacement at {position}. Replace any other red positions, then press Measure offset.")
            self._refresh_controls()

        def toggle_empty(self, position: str) -> None:
            if not self._busy and self.controller is None:
                if position in self._empty_positions:
                    self._empty_positions.remove(position)
                    self.grid.set_tile(position, state=aa.TileState.LOADED, headline="—", detail="WAITING")
                else:
                    self._empty_positions.add(position)
                    self.grid.set_tile(position, state=aa.TileState.EMPTY, headline="—", detail="EMPTY")
                self.summary_var.set(f"{50-len(self._empty_positions)} loaded  ·  {len(self._empty_positions)} empty  ·  waiting for offset measurement")
                return
            if not self._editable_tray():
                return
            c = self.controller
            c.state.lock = None
            c.phase = Phase.LOAD_OFFSET
            c.toggle_occupancy(position)
            self.vacuum_var.set(False)
            self._render_offsets()

        def start_noise_phase(self) -> None:
            if self._busy or not self._can_noise():
                return
            c = self.controller
            if c.state.report and not c.state.saved:
                return
            self.step_var.set("3  /  Measuring noise")
            self.status_var.set("Keep power and vacuum on. Leave all detectors in place until measurement finishes.")
            self.progress.configure(value=0)
            self._run_worker(self._noise_worker)

        def _noise_worker(self) -> None:
            c = self.controller
            try:
                if c.state.report:
                    c.remeasure()
                elif c.state.lock is None:
                    c.prepare_noise()
                tray_history.append_tray_event(
                    c.attempts_path, lot_number=c.state.lot, tray_number=c.state.tray_number,
                    tray_attempt=c.state.tray_attempt, event=tray_history.EVENT_VACUUM_CONFIRMED,
                    phase=Phase.LOCKED.value, tester_name=c.state.tester_name,
                    detail="Operator confirmed required vacuum setting on the rig gauge; pressure is not monitored by the app."
                           + (" SIMULATION ONLY." if self.simulate else ""),
                )
                # The app cannot read the vacuum gauge. Confirmation starts the prescribed
                # wait; no engineering skip control is exposed to the operator.
                started = time.monotonic()
                wait_s = 0.0
                if not self.simulate:
                    while wait_s < c.plan.stabilisation_s:
                        if self._cancel.wait(.2):
                            raise CaptureCancelled("stopped during stabilisation")
                        wait_s = min(c.plan.stabilisation_s, time.monotonic() - started)
                        left = max(0, math.ceil(c.plan.stabilisation_s-wait_s))
                        self._post(lambda w=wait_s, left=left: self._progress_update(
                            .2*w/max(1, c.plan.stabilisation_s), f"Stabilising under vacuum · {left//60}:{left%60:02d} remaining. Keep the rig powered."))
                else:
                    self._post(lambda: self._progress_update(.2, "Simulation: five-minute wait skipped; capturing with a fast virtual clock."))

                def progress(kind, fraction, message):
                    self._post(lambda f=fraction, message=message: self._progress_update(.2+.75*(f or 0), message))
                c.run_noise_phase(stabilisation_wait_s=wait_s, progress=progress, cancelled=self._cancel.is_set)
                self._post(lambda: self._progress_update(.98, "Measurement complete. Saving results…"))
                try:
                    outcome = c.save_tray()
                except Exception as exc:
                    self._post(lambda exc=exc: self._save_failed(exc))
                else:
                    self._post(lambda: self._noise_done(outcome))
            except CaptureCancelled:
                self._post(self._stopped)
            except Exception as exc:
                self._post(lambda exc=exc: self._failed("Noise measurement", exc))

        def _progress_update(self, fraction: float, message: str) -> None:
            self.progress.configure(value=100*fraction)
            self.status_var.set(message)

        def _render_results(self) -> None:
            c = self.controller
            self.vacuum_check.pack_forget()
            for result in c.state.report.results:
                state = aa.tile_state_for(result)
                detail = {aa.TileState.PASS: "PASS", aa.TileState.NOISE_FAIL: "NOISY", aa.TileState.NOISE_LOW: "LOW",
                          aa.TileState.OFFSET_FAIL: "OFFSET", aa.TileState.NO_LIMIT: "NO LIMIT",
                          aa.TileState.NOT_MEASURED: "NOT READ", aa.TileState.EMPTY: "EMPTY"}.get(state, "")
                headline = f"{result.noise.worst_pp_mv*1000:.0f} µV" if result.noise else "—"
                self.grid.set_tile(result.position, state=state, headline=headline, detail=detail, sensor_number=result.sensor_number)
            counts = c.summary_counts()
            failed = counts['fail_offset'] + counts['fail_noise'] + counts['noise_low']
            self.summary_var.set(f"{counts['pass']} pass  ·  {failed} fail  ·  {counts['no_limit']} no limit  ·  {counts['not_measured']} not read")
            self.map_hint_var.set("Green = pass   ·   Red = fail   ·   Amber = no calibrated noise limit   ·   Gray = empty / not read")

        def _noise_done(self, outcome) -> None:
            self._busy = False
            self._save_error = False
            self._render_results()
            self.progress.configure(value=100)
            c = self.controller
            self.step_var.set("3  /  Results saved" if not c.state.report.rig_fault else "3  /  Measurement needs attention")
            if c.state.report.rig_fault:
                self.status_var.set("The rig could not complete the measurement. Check the connection, power and vacuum, then press Measure noise to retry. " + c.state.report.rig_fault)
            else:
                self.status_var.set("Results are saved. Remove the red detectors and keep the green detectors. Press Next tray when ready."
                                    if self.simulate or c.noise_limits.defined else
                                    "Results are saved. Amber detectors have a noise reading but no calibrated pass/fail limit yet. Press Next tray when ready.")
            self.footer_var.set("Simulation results saved separately." if self.simulate else "Results saved automatically.")
            self._refresh_controls()

        def _save_failed(self, exc) -> None:
            self._busy = False
            self._save_error = True
            self._render_results()
            self.step_var.set("3  /  Results need saving")
            self.status_var.set(f"Measurement is complete, but results could not be saved: {exc}. Check storage and press Retry save.")
            self.footer_var.set("Results are not saved. Keep this tray open.")
            self._refresh_controls()

        def retry_save(self) -> None:
            if self._busy or not self._save_error:
                return
            def work():
                try:
                    outcome = self.controller.save_tray()
                except Exception as exc:
                    self._post(lambda exc=exc: self._save_failed(exc))
                else:
                    self._post(lambda: self._noise_done(outcome))
            self._run_worker(work)

        def _failed(self, stage: str, exc: Exception) -> None:
            self._busy = False
            if self.controller and self.controller.state.report is None:
                self.controller.phase = Phase.LOCKED if self.controller.state.lock else Phase.LOAD_OFFSET
            self.status_var.set(f"{stage} could not finish: {exc}. Check the rig and try again.")
            self.progress.configure(value=0)
            self._refresh_controls()

        def stop(self) -> None:
            if self._busy:
                self._cancel.set()
                self.stop_button.configure(text="Stopping…", state="disabled")
                self.status_var.set("Stopping safely. Keep the rig connected until the measurement stops.")

        def _stopped(self) -> None:
            self._busy = False
            if self.controller and self.controller.state.report is None:
                self.controller.phase = Phase.LOCKED if self.controller.state.lock else Phase.LOAD_OFFSET
            self.progress.configure(value=0)
            self.step_var.set("Measurement stopped")
            self.status_var.set("No noise verdict was recorded. Keep power and vacuum on to retry Measure noise, or use Measure offset after changing detectors."
                                if self.controller and self.controller.offset_checked else
                                "Press Measure offset when the rig is powered and ready.")
            self._refresh_controls()

        def next_tray(self) -> None:
            if self._busy or not self.controller or not self.controller.state.saved:
                return
            self._tray_number += 1
            self.controller = None
            self.vacuum_var.set(False)
            self.vacuum_check.pack_forget()
            self._save_error = False
            self._empty_positions.clear()
            if self.simulate:
                self.device.close()
                self.device = operator_simulator()
            self.tray_var.set(f"Tray {self._tray_number}  ·  up to 50 detectors")
            self.summary_var.set("50 positions  ·  waiting for offset measurement")
            self.map_hint_var.set("Positions are row-column, viewed from above. Click a physically empty socket to exclude it.")
            self.footer_var.set("Results save automatically after noise measurement.")
            self.progress.configure(value=0)
            self._reset_grid()
            self._offset_instructions()
            self._refresh_controls()

        def on_close(self) -> None:
            if self._save_error and not messagebox.askyesno(APP_TITLE, "Results could not be saved. Close and lose the unsaved measurement?"):
                return
            self._closing = True
            self._cancel.set()
            if self._busy or self._worker is not None and self._worker.is_alive():
                self.status_var.set("Stopping the measurement before closing…")
            else:
                self.after_cancel(self._queue_job)
                self.device.close()
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
