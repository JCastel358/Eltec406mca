#!/usr/bin/env python3
"""Collect and summarize known-good data for the v6 stability threshold.

This is an engineering calibration utility, not a production tester.  It
never emits a part verdict and never changes ``stability_settings.json``.
Capture uses the same peak/cycle extractor as the v6 application so that the
CSV evidence describes exactly the quantity used in production.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO

try:  # Package import (tests / ``python -m``).
    from .esp32_backend import (
        SAMPLE_RATE_HZ,
        Esp32BackendError,
        Esp32Rig,
        StreamDiagnostics,
        StreamSample,
    )
    from .stability_analysis import (
        DEFAULT_SETTINGS_PATH,
        StabilitySettings,
        SyncValidationError,
        analyze_stability,
        load_stability_settings,
        validate_rising_sync_cycles,
    )
except ImportError:  # Direct script execution from this directory.
    from esp32_backend import (  # type: ignore[no-redef]
        SAMPLE_RATE_HZ,
        Esp32BackendError,
        Esp32Rig,
        StreamDiagnostics,
        StreamSample,
    )
    from stability_analysis import (  # type: ignore[no-redef]
        DEFAULT_SETTINGS_PATH,
        StabilitySettings,
        SyncValidationError,
        analyze_stability,
        load_stability_settings,
        validate_rising_sync_cycles,
    )


DEFAULT_CAPTURE_DURATION_S = 20.0
DEFAULT_OUTPUT_DIR = (
    Path.home()
    / "Documents"
    / "Eltec_406MCA_Test_Results"
    / "v6_esp32"
    / "calibration"
)
STREAM_READ_CHUNK_SAMPLES = 250
STREAM_READ_TIMEOUT_S = 2.0

# Keep these preflight limits aligned with the production v6 rig without
# importing the Tk application (and therefore Tk/Matplotlib) into this CLI.
BATTERY_MIN_V = 5.8
BATTERY_PLAUSIBLE_MIN_V = 3.0
BATTERY_PLAUSIBLE_MAX_V = 7.5
SENSOR_OFFSET_PLAUSIBLE_MIN_V = 0.05
SENSOR_OFFSET_PLAUSIBLE_MAX_V = 2.5

RAW_CSV_FIELDS: tuple[str, ...] = (
    "run_id",
    "sensor_id",
    "sample_index",
    "timestamp_us",
    "elapsed_s",
    "pwm_elapsed_s",
    "raw_count",
    "voltage_v",
    "sync",
)

CYCLE_CSV_FIELDS: tuple[str, ...] = (
    "run_id",
    "sensor_id",
    "cycle_number",
    "start_sample_index",
    "end_sample_index",
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


class CalibrationError(RuntimeError):
    """Base class for expected calibration-capture failures."""


class PreflightError(CalibrationError):
    """The battery or DUT offset makes a capture unsafe or meaningless."""


class StreamIntegrityError(CalibrationError):
    """The serial stream is not reliable enough for calibration evidence."""


class CalibrationDataError(CalibrationError):
    """A cycle CSV cannot be used as calibration evidence."""


@dataclass(frozen=True)
class CaptureConfig:
    """Inputs for one hardware calibration capture."""

    sensor_id: str
    port: str | None = None
    duration_s: float = DEFAULT_CAPTURE_DURATION_S
    output_dir: Path = DEFAULT_OUTPUT_DIR
    settings_path: Path = DEFAULT_SETTINGS_PATH

    def __post_init__(self) -> None:
        sensor_id = str(self.sensor_id).strip()
        if not sensor_id:
            raise ValueError("sensor_id cannot be empty.")
        duration_s = float(self.duration_s)
        if not math.isfinite(duration_s) or duration_s <= 0:
            raise ValueError("duration_s must be a finite value greater than zero.")
        object.__setattr__(self, "sensor_id", sensor_id)
        object.__setattr__(self, "duration_s", duration_s)
        object.__setattr__(self, "output_dir", Path(self.output_dir).expanduser())
        object.__setattr__(self, "settings_path", Path(self.settings_path).expanduser())


@dataclass(frozen=True)
class CaptureArtifacts:
    """Files and machine-readable metadata produced by a capture."""

    raw_csv: Path
    cycle_csv: Path
    summary_json: Path
    summary: Mapping[str, Any]


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return cleaned[:80] or "sensor"


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_id(sensor_id: str, captured_at: datetime) -> str:
    stamp = captured_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{_safe_filename_component(sensor_id)}_{stamp}"


def _validate_preflight(battery_v: float, offset_v: float) -> None:
    if not math.isfinite(battery_v):
        raise PreflightError("Battery reading is not finite.")
    if not (BATTERY_PLAUSIBLE_MIN_V <= battery_v <= BATTERY_PLAUSIBLE_MAX_V):
        raise PreflightError(
            f"Battery input reads {battery_v:.3f} V, outside the plausible "
            f"{BATTERY_PLAUSIBLE_MIN_V:.1f}-{BATTERY_PLAUSIBLE_MAX_V:.1f} V range."
        )
    if battery_v <= BATTERY_MIN_V:
        raise PreflightError(
            f"Battery is too low for calibration ({battery_v:.3f} V; must be "
            f"above {BATTERY_MIN_V:.1f} V)."
        )
    if not math.isfinite(offset_v):
        raise PreflightError("AIN0 offset reading is not finite.")
    if not (
        SENSOR_OFFSET_PLAUSIBLE_MIN_V
        <= offset_v
        <= SENSOR_OFFSET_PLAUSIBLE_MAX_V
    ):
        raise PreflightError(
            f"AIN0 offset reads {offset_v:.6f} V, outside the plausible sensor "
            f"range {SENSOR_OFFSET_PLAUSIBLE_MIN_V:.2f}-"
            f"{SENSOR_OFFSET_PLAUSIBLE_MAX_V:.1f} V."
        )


def _validate_stream_diagnostics(
    diagnostics: StreamDiagnostics,
    *,
    minimum_samples: int,
    captured_samples: int,
) -> None:
    """Reject any stream defect that could bias millivolt-scale deltas."""

    problems: list[str] = []
    if diagnostics.received_samples < minimum_samples:
        problems.append(
            f"short capture ({diagnostics.received_samples}/{minimum_samples} samples)"
        )
    if diagnostics.received_samples != captured_samples:
        problems.append(
            "captured/diagnostic sample counts differ "
            f"({captured_samples}/{diagnostics.received_samples})"
        )
    if diagnostics.torn_lines:
        problems.append(f"{diagnostics.torn_lines} malformed records")
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
        problems.append(f"{diagnostics.firmware_adc_overruns} ADC overruns")
    if not diagnostics.stop_marker_seen:
        problems.append("STREAM,END was not received")
    if diagnostics.count_matches_firmware is not True:
        if diagnostics.firmware_samples_sent is None:
            problems.append("firmware sample count was not reported")
        else:
            problems.append(
                "host/firmware sample counts differ "
                f"({diagnostics.received_samples}/"
                f"{diagnostics.firmware_samples_sent})"
            )
    rate_error = diagnostics.rate_error_percent
    if rate_error is None:
        problems.append("measured stream rate is unavailable")
    elif abs(rate_error) > 2.0:
        problems.append(
            f"measured stream rate differs by {rate_error:+.2f}% from advertised"
        )
    if problems:
        raise StreamIntegrityError(
            "ESP32 stream is not valid calibration evidence: " + "; ".join(problems)
        )


def _capture_stream(
    rig: Esp32Rig,
    duration_s: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[list[StreamSample], float, StreamDiagnostics, float]:
    header = rig.start_stream("sensor")
    stream_started_monotonic = monotonic()
    if not math.isclose(header.sample_rate_hz, SAMPLE_RATE_HZ, rel_tol=0.01):
        try:
            if rig.is_streaming:
                rig.stop_stream(timeout_s=STREAM_READ_TIMEOUT_S)
        finally:
            raise StreamIntegrityError(
                f"ESP32 advertised {header.sample_rate_hz:g} samples/s; "
                f"calibration requires {SAMPLE_RATE_HZ:g} samples/s."
            )

    # N samples span N-1 intervals.  The extra sample therefore guarantees
    # at least the requested amount of device time rather than stopping one
    # conversion early.
    minimum_samples = int(math.ceil(duration_s * header.sample_rate_hz)) + 1
    samples: list[StreamSample] = []
    diagnostics: StreamDiagnostics | None = None
    capture_error: BaseException | None = None
    try:
        while True:
            if len(samples) >= 2:
                device_span_s = (
                    (samples[-1].timestamp_us - samples[0].timestamp_us)
                    & 0xFFFFFFFF
                ) / 1_000_000.0
                if (
                    len(samples) >= minimum_samples
                    and device_span_s >= duration_s
                ):
                    break
                remaining_samples = max(
                    1,
                    minimum_samples - len(samples),
                    int(
                        math.ceil(
                            (duration_s - device_span_s) * header.sample_rate_hz
                        )
                    ) if device_span_s < duration_s else 0,
                )
            else:
                remaining_samples = minimum_samples - len(samples)
            chunk = rig.read_stream(
                max_samples=min(STREAM_READ_CHUNK_SAMPLES, remaining_samples),
                timeout_s=STREAM_READ_TIMEOUT_S,
            )
            if not chunk:
                raise StreamIntegrityError(
                    f"ESP32 stream stalled after {len(samples)}/{minimum_samples} "
                    "required samples."
                )
            samples.extend(chunk)
    except BaseException as exc:
        capture_error = exc
        raise
    finally:
        if rig.is_streaming:
            try:
                diagnostics = rig.stop_stream(timeout_s=STREAM_READ_TIMEOUT_S)
            except Exception:
                if capture_error is None:
                    raise

    if diagnostics is None:
        diagnostics = rig.stream_diagnostics
    if diagnostics is None:
        raise StreamIntegrityError("ESP32 stream diagnostics were unavailable.")
    samples.extend(rig.drained_samples)
    _validate_stream_diagnostics(
        diagnostics,
        minimum_samples=minimum_samples,
        captured_samples=len(samples),
    )
    actual_rate_hz = float(diagnostics.measured_rate_hz or header.sample_rate_hz)
    return samples, actual_rate_hz, diagnostics, stream_started_monotonic


_MISSING = object()


def _record_attr(record: Any, *names: str, default: Any = _MISSING) -> Any:
    """Read a stability record while keeping CSV schema independent of it."""

    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    if default is not _MISSING:
        return default
    joined = ", ".join(names)
    raise CalibrationDataError(f"Stability cycle is missing required field: {joined}")


def _raw_rows(
    samples: Sequence[StreamSample],
    run_id: str,
    sensor_id: str,
    *,
    pwm_to_stream_offset_s: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    elapsed_us = 0
    previous_timestamp: int | None = None
    for index, sample in enumerate(samples):
        if previous_timestamp is not None:
            elapsed_us += (sample.timestamp_us - previous_timestamp) & 0xFFFFFFFF
        rows.append(
            {
                "run_id": run_id,
                "sensor_id": sensor_id,
                "sample_index": index,
                "timestamp_us": sample.timestamp_us,
                "elapsed_s": f"{elapsed_us / 1_000_000.0:.9f}",
                "pwm_elapsed_s": (
                    f"{pwm_to_stream_offset_s + elapsed_us / 1_000_000.0:.9f}"
                ),
                "raw_count": sample.raw,
                "voltage_v": f"{sample.volts:.12g}",
                "sync": sample.sync,
            }
        )
        previous_timestamp = sample.timestamp_us
    return rows


def _cycle_rows(
    cycles: Iterable[Any], run_id: str, sensor_id: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fallback_number, cycle in enumerate(cycles, start=1):
        signed_delta = _record_attr(
            cycle, "signed_peak_delta_mv", "peak_delta_mv", default=None
        )
        absolute_delta = _record_attr(
            cycle, "absolute_peak_delta_mv", "abs_peak_delta_mv", default=None
        )
        row = {
            "run_id": run_id,
            "sensor_id": sensor_id,
            "cycle_number": _record_attr(
                cycle, "cycle_number", "cycle_index", default=fallback_number
            ),
            "start_sample_index": _record_attr(
                cycle, "start_sample_index", "start_index"
            ),
            "end_sample_index": _record_attr(
                cycle, "end_sample_index", "end_index"
            ),
            "start_elapsed_s": _record_attr(
                cycle, "start_elapsed_s", "cycle_start_elapsed_s"
            ),
            "end_elapsed_s": _record_attr(
                cycle, "end_elapsed_s", "cycle_end_elapsed_s", "elapsed_s"
            ),
            "robust_peak_v": _record_attr(cycle, "robust_peak_v"),
            "raw_max_v": _record_attr(cycle, "raw_max_v", "maximum_v"),
            "raw_min_v": _record_attr(cycle, "raw_min_v", "minimum_v"),
            "peak_to_peak_v": _record_attr(
                cycle, "peak_to_peak_v", "raw_peak_to_peak_v"
            ),
            "signed_peak_delta_mv": "" if signed_delta is None else signed_delta,
            "absolute_peak_delta_mv": (
                "" if absolute_delta is None else absolute_delta
            ),
            "within_threshold": _record_attr(
                cycle, "within_threshold", "qualifies", default=False
            ),
            "confirmation_run_length": _record_attr(
                cycle, "confirmation_run_length", "consecutive_qualifying_deltas",
                default=0,
            ),
        }
        rows.append(row)
    return rows


def _earliest_stabilization(
    cycle_rows: Sequence[Mapping[str, Any]], settings: StabilitySettings
) -> dict[str, Any] | None:
    required = int(settings.consecutive_deltas_required)
    for row in cycle_rows:
        run_length = int(row["confirmation_run_length"])
        if run_length >= required:
            return {
                "cycle_number": int(row["cycle_number"]),
                "elapsed_s": float(row["end_elapsed_s"]),
                "confirming_window_max_delta_mv": max(
                    float(item["absolute_peak_delta_mv"])
                    for item in cycle_rows
                    if item["absolute_peak_delta_mv"] != ""
                    and int(item["cycle_number"])
                    > int(row["cycle_number"]) - required
                    and int(item["cycle_number"]) <= int(row["cycle_number"])
                ),
            }
    return None


def _diagnostic_summary(diagnostics: StreamDiagnostics) -> dict[str, Any]:
    return {
        "received_samples": diagnostics.received_samples,
        "firmware_samples_sent": diagnostics.firmware_samples_sent,
        "drained_samples": diagnostics.drained_samples,
        "measured_rate_hz": diagnostics.measured_rate_hz,
        "rate_error_percent": diagnostics.rate_error_percent,
        "torn_lines": diagnostics.torn_lines,
        "timestamp_gap_count": diagnostics.timestamp_gap_count,
        "estimated_missing_samples": diagnostics.estimated_missing_samples,
        "duplicate_timestamps": diagnostics.duplicate_timestamps,
        "reordered_timestamps": diagnostics.reordered_timestamps,
        "firmware_adc_overruns": diagnostics.firmware_adc_overruns,
        "stop_marker_seen": diagnostics.stop_marker_seen,
        "count_matches_firmware": diagnostics.count_matches_firmware,
    }


def _write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def capture_calibration(
    config: CaptureConfig,
    *,
    rig_factory: Callable[..., Esp32Rig] = Esp32Rig,
    monotonic: Callable[[], float] = time.monotonic,
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CaptureArtifacts:
    """Capture one known-good sensor run and persist its calibration evidence."""

    settings = load_stability_settings(config.settings_path)
    captured_at = now_utc()
    run_id = _run_id(config.sensor_id, captured_at)
    rig = rig_factory(port=config.port)

    battery_v: float
    offset_v: float
    samples: list[StreamSample]
    actual_rate_hz: float
    diagnostics: StreamDiagnostics
    pwm_started_monotonic: float | None = None
    pwm_finished_monotonic: float | None = None
    stream_started_monotonic: float | None = None
    hardware_port: str | None = config.port
    firmware_identity: str | None = None
    primary_error: BaseException | None = None
    deactivation_time: float | None = None
    try:
        rig.connect()
        hardware_port = getattr(rig, "port_name", None) or config.port
        identity = getattr(rig, "identity", None)
        firmware_identity = (
            None if identity is None else getattr(identity, "text", str(identity))
        )
        # Establish a known-safe preflight state before reading either scalar.
        rig.disable_emitter_pwm("GPIO25")
        battery_v = float(rig.read_battery_voltage())
        offset_v = float(rig.read_offset_voltage())
        _validate_preflight(battery_v, offset_v)

        # The v6 backend returns the PWM,ON command timestamp after any PIN
        # setup. Test doubles/older compatible adapters fall back to the first
        # host instant after the acknowledged command.
        activation_time = rig.enable_emitter_pwm("GPIO25")
        pwm_started_monotonic = (
            float(activation_time)
            if isinstance(activation_time, (int, float))
            else monotonic()
        )
        (
            samples,
            actual_rate_hz,
            diagnostics,
            stream_started_monotonic,
        ) = _capture_stream(rig, config.duration_s, monotonic=monotonic)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            deactivation_time = rig.disable_emitter_pwm("GPIO25")
        except Exception:
            if primary_error is None:
                raise
        finally:
            pwm_finished_monotonic = (
                float(deactivation_time)
                if isinstance(deactivation_time, (int, float))
                else monotonic()
            )
            # Esp32Rig.close() makes a second best-effort PWM-off attempt.  It
            # is intentionally redundant so even a failed acknowledged PWM
            # command still takes the backend's safe-shutdown path.
            rig.close()

    waveform_v = [sample.volts for sample in samples]
    sync = [sample.sync for sample in samples]
    pwm_to_stream_offset_s = 0.0
    if pwm_started_monotonic is not None and stream_started_monotonic is not None:
        pwm_to_stream_offset_s = max(
            0.0, stream_started_monotonic - pwm_started_monotonic
        )
    try:
        sync_validation = validate_rising_sync_cycles(
            sync,
            actual_rate_hz,
        )
    except SyncValidationError as exc:
        raise StreamIntegrityError(
            f"ESP32 PWM sync is not valid calibration evidence: {exc}"
        ) from exc
    analysis = analyze_stability(
        waveform_v,
        sync,
        actual_rate_hz,
        settings,
        pwm_elapsed_offset_s=pwm_to_stream_offset_s,
        stability_deadline_s=config.duration_s,
        measurement_cycles_required=10,
        data_source="ESP32 AIN0 calibration",
    )
    raw_rows = _raw_rows(
        samples,
        run_id,
        config.sensor_id,
        pwm_to_stream_offset_s=pwm_to_stream_offset_s,
    )
    cycle_rows = _cycle_rows(analysis.cycles, run_id, config.sensor_id)
    if not cycle_rows:
        raise CalibrationDataError(
            "No complete rising-edge PWM cycles were found in the capture."
        )
    earliest = _earliest_stabilization(cycle_rows, settings)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = config.output_dir / f"{run_id}_raw.csv"
    cycle_path = config.output_dir / f"{run_id}_cycles.csv"
    summary_path = config.output_dir / f"{run_id}_summary.json"
    _write_csv(raw_path, RAW_CSV_FIELDS, raw_rows)
    _write_csv(cycle_path, CYCLE_CSV_FIELDS, cycle_rows)

    if samples:
        captured_device_s = float(raw_rows[-1]["elapsed_s"])
    else:  # Guarded by strict minimum_samples, kept explicit for type safety.
        captured_device_s = 0.0
    pwm_on_host_s = None
    if pwm_started_monotonic is not None and pwm_finished_monotonic is not None:
        pwm_on_host_s = max(0.0, pwm_finished_monotonic - pwm_started_monotonic)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "purpose": "known-good stability calibration evidence",
        "calibration_only": True,
        "run_id": run_id,
        "sensor_id": config.sensor_id,
        "captured_at_utc": _iso_utc(captured_at),
        "hardware": {
            "port": hardware_port,
            "firmware_identity": firmware_identity,
            "battery_v": battery_v,
            "offset_v": offset_v,
        },
        "settings": {
            "path": str(config.settings_path),
            "peak_delta_threshold_mv": settings.peak_delta_threshold_mv,
            "consecutive_deltas_required": settings.consecutive_deltas_required,
        },
        "capture": {
            "requested_duration_s": config.duration_s,
            "captured_device_duration_s": captured_device_s,
            "pwm_on_host_duration_s": pwm_on_host_s,
            "pwm_to_stream_offset_s": pwm_to_stream_offset_s,
            "sample_rate_hz": actual_rate_hz,
            "sample_count": len(samples),
            "complete_cycle_count": len(cycle_rows),
            "sync_validation": {
                "cycles_validated": sync_validation.cycles_validated,
                "rising_edge_count": sync_validation.rising_edge_count,
                "measured_frequency_hz": sync_validation.measured_frequency_hz,
            },
            "data_source": "ESP32 AIN0",
            "diagnostics": _diagnostic_summary(diagnostics),
        },
        "earliest_qualifying_stabilization": earliest,
        "artifacts": {
            "raw_csv": raw_path.name,
            "cycle_csv": cycle_path.name,
            "summary_json": summary_path.name,
        },
        "note": (
            "Review calibration evidence before manually changing the tracked "
            "stability settings; this utility does not update them."
        ),
    }
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return CaptureArtifacts(raw_path, cycle_path, summary_path, summary)


def _finite_float(value: str, *, field: str, path: Path, row_number: int) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CalibrationDataError(
            f"{path}: row {row_number} has invalid {field}: {value!r}"
        ) from exc
    if not math.isfinite(parsed):
        raise CalibrationDataError(
            f"{path}: row {row_number} has non-finite {field}: {value!r}"
        )
    return parsed


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Return a linearly interpolated percentile (the common type-7 rule)."""

    if not values:
        raise ValueError("At least one value is required.")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * (percentile / 100.0)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _percentile_report(values: Sequence[float]) -> dict[str, float | None]:
    """Return the calibration percentile set, using nulls for an empty region."""

    if not values:
        return {"p50": None, "p90": None, "p95": None, "p99": None, "max": None}
    report = {
        f"p{percentile}": _percentile(values, percentile)
        for percentile in (50, 90, 95, 99)
    }
    report["max"] = max(values)
    return report


def summarize_cycle_csvs(
    cycle_csvs: Sequence[Path | str],
    settings: StabilitySettings,
) -> dict[str, Any]:
    """Summarize known-good cycle CSVs under the supplied candidate rule."""

    if not cycle_csvs:
        raise ValueError("At least one cycle CSV is required.")
    combined_deltas: list[float] = []
    combined_post_stabilization_deltas: list[float] = []
    parts: list[dict[str, Any]] = []

    for supplied_path in cycle_csvs:
        path = Path(supplied_path).expanduser()
        try:
            handle = path.open(newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CalibrationDataError(f"Could not read {path}: {exc}") from exc
        with handle:
            reader = csv.DictReader(handle)
            required_columns = {
                "run_id",
                "sensor_id",
                "cycle_number",
                "end_elapsed_s",
                "absolute_peak_delta_mv",
            }
            missing = required_columns.difference(reader.fieldnames or ())
            if missing:
                raise CalibrationDataError(
                    f"{path}: missing required columns: {', '.join(sorted(missing))}"
                )
            rows = list(reader)
        if not rows:
            raise CalibrationDataError(f"{path}: cycle CSV has no data rows.")

        grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
        for row in rows:
            key = ((row.get("run_id") or "").strip(), (row.get("sensor_id") or "").strip())
            if not key[0] or not key[1]:
                raise CalibrationDataError(
                    f"{path}: run_id and sensor_id must be populated on every row."
                )
            grouped.setdefault(key, []).append(row)

        for (run_id, sensor_id), run_rows in grouped.items():
            confirmation_run = 0
            earliest: dict[str, Any] | None = None
            run_delta_count = 0
            run_post_stabilization_deltas: list[float] = []
            for row_index, row in enumerate(run_rows, start=2):
                delta_text = (row.get("absolute_peak_delta_mv") or "").strip()
                if not delta_text:
                    confirmation_run = 0
                    continue
                delta_mv = _finite_float(
                    delta_text,
                    field="absolute_peak_delta_mv",
                    path=path,
                    row_number=row_index,
                )
                if delta_mv < 0:
                    raise CalibrationDataError(
                        f"{path}: row {row_index} has a negative absolute peak delta."
                    )
                combined_deltas.append(delta_mv)
                run_delta_count += 1
                if delta_mv <= settings.peak_delta_threshold_mv or math.isclose(
                    delta_mv,
                    settings.peak_delta_threshold_mv,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                ):
                    confirmation_run += 1
                else:
                    confirmation_run = 0
                if (
                    earliest is None
                    and confirmation_run >= settings.consecutive_deltas_required
                ):
                    try:
                        cycle_number = int(row["cycle_number"])
                    except (TypeError, ValueError) as exc:
                        raise CalibrationDataError(
                            f"{path}: row {row_index} has invalid cycle_number."
                        ) from exc
                    elapsed_s = _finite_float(
                        row["end_elapsed_s"],
                        field="end_elapsed_s",
                        path=path,
                        row_number=row_index,
                    )
                    earliest = {
                        "cycle_number": cycle_number,
                        "elapsed_s": elapsed_s,
                    }
                # Include the confirming endpoint itself.  This makes the
                # steady-state region begin with the exact observation that
                # completed the configured rule, followed by every later
                # recorded delta even if later drift exceeds the threshold.
                if earliest is not None:
                    run_post_stabilization_deltas.append(delta_mv)
            combined_post_stabilization_deltas.extend(
                run_post_stabilization_deltas
            )
            parts.append(
                {
                    "source_csv": str(path),
                    "run_id": run_id,
                    "sensor_id": sensor_id,
                    "cycle_count": len(run_rows),
                    "delta_count": run_delta_count,
                    "post_stabilization_delta_count": len(
                        run_post_stabilization_deltas
                    ),
                    "earliest_qualifying_stabilization": earliest,
                }
            )

    if not combined_deltas:
        raise CalibrationDataError("The supplied cycle CSVs contain no peak deltas.")
    all_delta_percentiles = _percentile_report(combined_deltas)
    post_stabilization_percentiles = _percentile_report(
        combined_post_stabilization_deltas
    )
    return {
        "schema_version": 1,
        "purpose": "known-good stability calibration summary",
        "calibration_only": True,
        "settings": {
            "peak_delta_threshold_mv": settings.peak_delta_threshold_mv,
            "consecutive_deltas_required": settings.consecutive_deltas_required,
        },
        "input_file_count": len(cycle_csvs),
        "part_run_count": len(parts),
        "combined_delta_count": len(combined_deltas),
        "combined_absolute_peak_delta_mv": all_delta_percentiles,
        "combined_post_stabilization_delta_count": len(
            combined_post_stabilization_deltas
        ),
        "post_stabilization_contributing_run_count": sum(
            part["post_stabilization_delta_count"] > 0 for part in parts
        ),
        "combined_post_stabilization_absolute_peak_delta_mv": (
            post_stabilization_percentiles
        ),
        "post_stabilization_definition": (
            "The delta on each run's earliest stabilization-confirming cycle, "
            "plus every later recorded delta in that run."
        ),
        "parts": parts,
        "note": (
            "Review these known-good observations before manually changing the "
            "tracked production settings."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect and summarize v6 peak-delta calibration evidence from "
            "known-good 406MCA sensors."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture", help="Capture one uninterrupted known-good sensor run."
    )
    capture.add_argument("--sensor-id", required=True, help="Known-good sensor identifier.")
    capture.add_argument("--port", help="ESP32 serial port (auto-detect when omitted).")
    capture.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_CAPTURE_DURATION_S,
        metavar="SECONDS",
        help=f"Capture duration (default: {DEFAULT_CAPTURE_DURATION_S:g} seconds).",
    )
    capture.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Artifact directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    capture.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        help=f"Tracked stability settings JSON (default: {DEFAULT_SETTINGS_PATH}).",
    )

    summarize = subparsers.add_parser(
        "summarize", help="Summarize cycle CSVs from several known-good runs."
    )
    summarize.add_argument(
        "cycle_csvs", type=Path, nargs="+", metavar="CYCLE_CSV"
    )
    summarize.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        help=f"Candidate stability settings JSON (default: {DEFAULT_SETTINGS_PATH}).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    rig_factory: Callable[..., Esp32Rig] = Esp32Rig,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "capture":
            config = CaptureConfig(
                sensor_id=args.sensor_id,
                port=args.port,
                duration_s=args.duration,
                output_dir=args.output_dir,
                settings_path=args.settings,
            )
            artifacts = capture_calibration(config, rig_factory=rig_factory)
            print(f"Raw samples: {artifacts.raw_csv}", file=stdout)
            print(f"Cycle analysis: {artifacts.cycle_csv}", file=stdout)
            print(f"Run summary: {artifacts.summary_json}", file=stdout)
        else:
            settings = load_stability_settings(args.settings)
            summary = summarize_cycle_csvs(args.cycle_csvs, settings)
            json.dump(summary, stdout, indent=2, sort_keys=True, allow_nan=False)
            stdout.write("\n")
        return 0
    except KeyboardInterrupt:
        print("Calibration interrupted; PWM shutdown was requested.", file=stderr)
        return 130
    except (CalibrationError, Esp32BackendError, OSError, ValueError) as exc:
        print(f"Calibration error: {exc}", file=stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
