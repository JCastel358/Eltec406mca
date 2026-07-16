"""Peak-delta stabilization analysis for the v6 ESP32 tester.

This module is deliberately independent of the GUI and serial backend.  Both
the production tester and the calibration CLI use the same cycle extraction,
robust peak calculation, and stabilization rule.

Times reported here are elapsed from PWM activation.  Callers whose sample
stream begins after the PWM acknowledgement must pass that delay as
``pwm_elapsed_offset_s``.  A stabilization cycle that closes exactly on the
deadline is accepted; a cycle that closes after it is not.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_PEAK_DELTA_THRESHOLD_MV = 0.100
DEFAULT_CONSECUTIVE_DELTAS_REQUIRED = 5
DEFAULT_STABILITY_DEADLINE_S = 20.0
DEFAULT_MEASUREMENT_CYCLES_REQUIRED = 10
DEFAULT_SYNC_FREQUENCY_HZ = 10.0
DEFAULT_SYNC_FREQUENCY_TOLERANCE_HZ = 0.1
DEFAULT_SYNC_VALIDATION_CYCLES = 3
DEFAULT_SETTINGS_PATH = Path(__file__).with_name("stability_settings.json")


class StabilitySettingsError(ValueError):
    """Raised when the tracked v6 stability configuration is unusable."""


class SyncValidationError(ValueError):
    """Raised when PWM sync cannot prove the required production cadence."""


@dataclass(frozen=True)
class StabilitySettings:
    """Validated configuration for the adjacent-peak stabilization rule."""

    peak_delta_threshold_mv: float = DEFAULT_PEAK_DELTA_THRESHOLD_MV
    consecutive_deltas_required: int = DEFAULT_CONSECUTIVE_DELTAS_REQUIRED

    def __post_init__(self) -> None:
        threshold = self.peak_delta_threshold_mv
        required = self.consecutive_deltas_required

        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise StabilitySettingsError(
                "peak_delta_threshold_mv must be a positive finite number"
            )
        threshold = float(threshold)
        if not math.isfinite(threshold) or threshold <= 0.0:
            raise StabilitySettingsError(
                "peak_delta_threshold_mv must be a positive finite number"
            )
        if isinstance(required, bool) or not isinstance(required, int) or required < 1:
            raise StabilitySettingsError(
                "consecutive_deltas_required must be a positive integer"
            )

        object.__setattr__(self, "peak_delta_threshold_mv", threshold)

    def as_dict(self) -> dict[str, Any]:
        """Return the exact version-controlled JSON field mapping."""

        return asdict(self)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "StabilitySettings":
        """Build settings from a strict two-key JSON-style mapping."""

        required_keys = {
            "peak_delta_threshold_mv",
            "consecutive_deltas_required",
        }
        supplied_keys = set(values)
        missing = sorted(required_keys - supplied_keys)
        unexpected = sorted(supplied_keys - required_keys)
        if missing:
            raise StabilitySettingsError(
                "missing required setting(s): " + ", ".join(missing)
            )
        if unexpected:
            raise StabilitySettingsError(
                "unexpected setting(s): " + ", ".join(unexpected)
            )
        return cls(
            peak_delta_threshold_mv=values["peak_delta_threshold_mv"],
            consecutive_deltas_required=values["consecutive_deltas_required"],
        )


def load_stability_settings(
    path: str | Path = DEFAULT_SETTINGS_PATH,
) -> StabilitySettings:
    """Load the mandatory v6 configuration without silently using defaults.

    ``StabilitySettings`` has defaults for tests and explicit programmatic use,
    but production callers should always call this loader.  Missing, malformed,
    incomplete, or invalid files raise ``StabilitySettingsError`` so the GUI can
    disable measurement and show the operator an actionable configuration error.
    """

    settings_path = Path(path)
    try:
        raw_text = settings_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StabilitySettingsError(
            f"cannot read stability settings {settings_path}: {exc}"
        ) from exc

    try:
        values = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise StabilitySettingsError(
            f"invalid JSON in stability settings {settings_path}: "
            f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(values, dict):
        raise StabilitySettingsError(
            f"stability settings {settings_path} must contain a JSON object"
        )

    try:
        return StabilitySettings.from_mapping(values)
    except StabilitySettingsError as exc:
        raise StabilitySettingsError(
            f"invalid stability settings {settings_path}: {exc}"
        ) from exc


@dataclass(frozen=True)
class CycleAnalysis:
    """Measurements and stability state for one complete rising-edge cycle."""

    cycle_number: int
    start_index: int
    end_index: int
    start_elapsed_s: float
    end_elapsed_s: float
    robust_peak_v: float
    raw_max_v: float
    raw_min_v: float
    peak_to_peak_v: float
    signed_peak_delta_mv: float | None
    absolute_peak_delta_mv: float | None
    within_threshold: bool | None
    confirmation_run_length: int

    @property
    def sample_count(self) -> int:
        return self.end_index - self.start_index

    def as_dict(self) -> dict[str, Any]:
        """Return a stable CSV/JSON-friendly field mapping."""

        return asdict(self)


@dataclass(frozen=True)
class StabilityReport:
    """Production-facing summary of the stabilization/capture decision."""

    configured_threshold_mv: float
    configured_confirmation_count: int
    stabilized: bool
    timed_out: bool
    stabilization_cycle: int | None
    stabilization_elapsed_s: float | None
    confirming_window_max_delta_mv: float | None
    last_delta_mv: float | None
    capture_cycles: int
    measurement_cycle_count: int
    measurement_cycles_required: int
    total_pwm_on_seconds: float
    stability_deadline_s: float
    pwm_elapsed_offset_s: float
    data_source: str

    @property
    def measurement_complete(self) -> bool:
        return (
            self.stabilized
            and self.measurement_cycle_count >= self.measurement_cycles_required
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a stable CSV/JSON-friendly field mapping."""

        return asdict(self)


@dataclass(frozen=True)
class StabilityAnalysis:
    """All complete cycles, the official post-stability cycles, and report."""

    cycles: tuple[CycleAnalysis, ...]
    measurement_cycles: tuple[CycleAnalysis, ...]
    report: StabilityReport

    @property
    def measurement_segments(self) -> tuple[tuple[int, int], ...]:
        """Array slices for exactly the selected official measurement cycles."""

        return tuple(
            (cycle.start_index, cycle.end_index)
            for cycle in self.measurement_cycles
        )


@dataclass(frozen=True)
class SyncValidation:
    """Strict cadence result from the first complete PWM-sync cycles."""

    cycles_validated: int
    rising_edge_count: int
    measured_frequency_hz: float
    first_edge_index: int
    last_edge_index: int


def validate_rising_sync_cycles(
    sync_v: Sequence[float],
    sample_rate_hz: float,
    *,
    expected_frequency_hz: float = DEFAULT_SYNC_FREQUENCY_HZ,
    frequency_tolerance_hz: float = DEFAULT_SYNC_FREQUENCY_TOLERANCE_HZ,
    cycles_required: int = DEFAULT_SYNC_VALIDATION_CYCLES,
) -> SyncValidation:
    """Require the first N complete rising-edge cycles at the expected rate.

    Three complete cycles require four rising edges.  A mere low/high span or
    isolated transition is deliberately insufficient: a sync wiring/firmware
    fault must be surfaced as a rig error, never as an unstable DUT.
    """

    rate = float(sample_rate_hz)
    expected = float(expected_frequency_hz)
    tolerance = float(frequency_tolerance_hz)
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("sample_rate_hz must be a positive finite number")
    if not math.isfinite(expected) or expected <= 0.0:
        raise ValueError("expected_frequency_hz must be a positive finite number")
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise ValueError("frequency_tolerance_hz must be a non-negative finite number")
    if (
        isinstance(cycles_required, bool)
        or not isinstance(cycles_required, int)
        or cycles_required < 1
    ):
        raise ValueError("cycles_required must be a positive integer")

    sync = [float(value) for value in sync_v]
    if not sync:
        raise SyncValidationError("PWM sync capture is empty")
    if not all(math.isfinite(value) for value in sync):
        raise SyncValidationError("PWM sync contains non-finite samples")
    if max(sync) - min(sync) < 0.5:
        raise SyncValidationError("PWM sync did not toggle")

    edges = rising_edge_indices(sync)
    edge_count_required = cycles_required + 1
    if len(edges) < edge_count_required:
        raise SyncValidationError(
            f"PWM sync provided {max(0, len(edges) - 1)} complete rising-edge cycles; "
            f"{cycles_required} are required"
        )

    validation_edges = edges[:edge_count_required]
    periods_s = [
        (validation_edges[index + 1] - validation_edges[index]) / rate
        for index in range(cycles_required)
    ]
    period_frequencies_hz = [1.0 / period_s for period_s in periods_s]
    mean_period_s = statistics.fmean(periods_s)
    measured_frequency_hz = 1.0 / mean_period_s
    invalid_periods = [
        frequency
        for frequency in period_frequencies_hz
        if abs(frequency - expected) > tolerance
        and not math.isclose(
            abs(frequency - expected),
            tolerance,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ]
    if invalid_periods:
        observed_min = min(period_frequencies_hz)
        observed_max = max(period_frequencies_hz)
        raise SyncValidationError(
            f"PWM sync frequency is {measured_frequency_hz:.3f} Hz; "
            f"individual validation cycles span {observed_min:.3f}-"
            f"{observed_max:.3f} Hz, expected {expected:.1f} +/- "
            f"{tolerance:.1f} Hz"
        )

    return SyncValidation(
        cycles_validated=cycles_required,
        rising_edge_count=len(edges),
        measured_frequency_hz=measured_frequency_hz,
        first_edge_index=validation_edges[0],
        last_edge_index=validation_edges[-1],
    )


def robust_upper_peak_v(samples_v: Sequence[float]) -> float:
    """Return the median of the highest 10% of a cycle, using at least 5 samples.

    A complete cycle with fewer than five samples cannot satisfy the specified
    robust-peak definition and is rejected instead of silently changing the
    estimator.
    """

    values = [float(value) for value in samples_v]
    if len(values) < 5:
        raise ValueError("a cycle needs at least 5 samples for a robust upper peak")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("cycle samples must all be finite")
    selected_count = max(5, math.ceil(len(values) * 0.10))
    return float(statistics.median(sorted(values, reverse=True)[:selected_count]))


def rising_edge_indices(
    sync_v: Sequence[float], *, threshold: float = 0.5
) -> tuple[int, ...]:
    """Locate low-to-high sync transitions; an initial high is not a full edge."""

    sync = [float(value) for value in sync_v]
    if not math.isfinite(threshold):
        raise ValueError("sync threshold must be finite")
    if not all(math.isfinite(value) for value in sync):
        raise ValueError("sync samples must all be finite")
    return tuple(
        index
        for index in range(1, len(sync))
        if sync[index - 1] <= threshold < sync[index]
    )


def complete_cycle_segments(
    sync_v: Sequence[float], *, threshold: float = 0.5
) -> tuple[tuple[int, int], ...]:
    """Return slices bounded by consecutive rising edges.

    Samples before the first observed edge and after the last observed edge are
    partial cycles and are intentionally excluded.
    """

    edges = rising_edge_indices(sync_v, threshold=threshold)
    return tuple(zip(edges, edges[1:]))


def _validate_analysis_inputs(
    waveform_v: Sequence[float],
    sync_v: Sequence[float],
    sample_rate_hz: float,
    pwm_elapsed_offset_s: float,
    stability_deadline_s: float,
    measurement_cycles_required: int,
) -> tuple[list[float], list[float], float, float, float]:
    waveform = [float(value) for value in waveform_v]
    sync = [float(value) for value in sync_v]
    if len(waveform) != len(sync):
        raise ValueError("waveform and sync arrays must have the same length")
    if not all(math.isfinite(value) for value in waveform):
        raise ValueError("waveform samples must all be finite")

    rate = float(sample_rate_hz)
    offset = float(pwm_elapsed_offset_s)
    deadline = float(stability_deadline_s)
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("sample_rate_hz must be a positive finite number")
    if not math.isfinite(offset) or offset < 0.0:
        raise ValueError("pwm_elapsed_offset_s must be a non-negative finite number")
    if not math.isfinite(deadline) or deadline <= 0.0:
        raise ValueError("stability_deadline_s must be a positive finite number")
    if (
        isinstance(measurement_cycles_required, bool)
        or not isinstance(measurement_cycles_required, int)
        or measurement_cycles_required < 1
    ):
        raise ValueError("measurement_cycles_required must be a positive integer")
    return waveform, sync, rate, offset, deadline


def _at_or_below_threshold(value: float, threshold: float) -> bool:
    # Avoid allowing binary floating-point representation alone to reject a
    # mathematically exact 0.100 mV boundary.
    return value <= threshold or math.isclose(
        value, threshold, rel_tol=1e-12, abs_tol=1e-12
    )


def _at_or_before_deadline(value: float, deadline: float) -> bool:
    return value <= deadline or math.isclose(
        value, deadline, rel_tol=1e-12, abs_tol=1e-12
    )


def _deadline_reached(value: float, deadline: float) -> bool:
    return value >= deadline or math.isclose(
        value, deadline, rel_tol=1e-12, abs_tol=1e-12
    )


def analyze_stability(
    waveform_v: Sequence[float],
    sync_v: Sequence[float],
    sample_rate_hz: float,
    settings: StabilitySettings,
    *,
    pwm_elapsed_offset_s: float = 0.0,
    stability_deadline_s: float = DEFAULT_STABILITY_DEADLINE_S,
    measurement_cycles_required: int = DEFAULT_MEASUREMENT_CYCLES_REQUIRED,
    data_source: str = "ESP32 AIN0",
) -> StabilityAnalysis:
    """Analyze an uninterrupted AIN0/sync capture using the v6 rule.

    Stability is the earliest run of the configured number of adjacent robust
    peak deltas at or below the threshold whose final cycle closes on or before
    ``stability_deadline_s``.  Official measurement cycles are the next fresh
    complete cycles after that decision cycle.  The deadline governs only the
    stabilization decision; those measurement cycles may close later.

    Re-running this function as a stream grows is safe and deterministic, which
    lets the GUI use the latest cycle/report for live progress without a second
    capture path.
    """

    if not isinstance(settings, StabilitySettings):
        raise TypeError("settings must be a StabilitySettings instance")
    waveform, sync, rate, offset, deadline = _validate_analysis_inputs(
        waveform_v,
        sync_v,
        sample_rate_hz,
        pwm_elapsed_offset_s,
        stability_deadline_s,
        measurement_cycles_required,
    )

    segments = complete_cycle_segments(sync)
    cycles: list[CycleAnalysis] = []
    previous_peak_v: float | None = None
    confirmation_run = 0
    stabilization_index: int | None = None

    for cycle_index, (start, end) in enumerate(segments):
        cycle_values = waveform[start:end]
        robust_peak = robust_upper_peak_v(cycle_values)
        raw_max = max(cycle_values)
        raw_min = min(cycle_values)
        delta_mv: float | None = None
        absolute_delta_mv: float | None = None
        within_threshold: bool | None = None

        if previous_peak_v is not None:
            delta_mv = (robust_peak - previous_peak_v) * 1000.0
            absolute_delta_mv = abs(delta_mv)
            within_threshold = _at_or_below_threshold(
                absolute_delta_mv, settings.peak_delta_threshold_mv
            )
            confirmation_run = confirmation_run + 1 if within_threshold else 0

        end_elapsed_s = offset + end / rate
        cycle = CycleAnalysis(
            cycle_number=cycle_index + 1,
            start_index=start,
            end_index=end,
            start_elapsed_s=offset + start / rate,
            end_elapsed_s=end_elapsed_s,
            robust_peak_v=robust_peak,
            raw_max_v=raw_max,
            raw_min_v=raw_min,
            peak_to_peak_v=raw_max - raw_min,
            signed_peak_delta_mv=delta_mv,
            absolute_peak_delta_mv=absolute_delta_mv,
            within_threshold=within_threshold,
            confirmation_run_length=confirmation_run,
        )
        cycles.append(cycle)

        if (
            stabilization_index is None
            and confirmation_run >= settings.consecutive_deltas_required
            and _at_or_before_deadline(end_elapsed_s, deadline)
        ):
            stabilization_index = cycle_index
        previous_peak_v = robust_peak

    # Samples are indexed from zero: N samples span N-1 sample intervals.  The
    # decision clock must not reach the deadline until the sample at that exact
    # elapsed time is actually present (important for the inclusive 20 s rule).
    captured_sample_span_s = 0.0 if not waveform else (len(waveform) - 1) / rate
    total_pwm_on_seconds = offset + captured_sample_span_s
    stabilized = stabilization_index is not None
    timed_out = not stabilized and _deadline_reached(
        total_pwm_on_seconds, deadline
    )

    if stabilization_index is None:
        stabilization_cycle = None
        stabilization_elapsed_s = None
        confirming_window_max_delta_mv = None
        measurement_cycles: tuple[CycleAnalysis, ...] = ()
    else:
        stabilization = cycles[stabilization_index]
        stabilization_cycle = stabilization.cycle_number
        stabilization_elapsed_s = stabilization.end_elapsed_s
        confirming_cycles = cycles[
            stabilization_index - settings.consecutive_deltas_required + 1 :
            stabilization_index + 1
        ]
        confirming_window_max_delta_mv = max(
            cycle.absolute_peak_delta_mv
            for cycle in confirming_cycles
            if cycle.absolute_peak_delta_mv is not None
        )
        measurement_cycles = tuple(
            cycles[
                stabilization_index + 1 :
                stabilization_index + 1 + measurement_cycles_required
            ]
        )

    # This is diagnostic telemetry, so report the final complete cycle seen in
    # the retained stream—not the earlier delta that happened to confirm
    # stability.
    last_delta_mv = (
        cycles[-1].absolute_peak_delta_mv if cycles else None
    )

    report = StabilityReport(
        configured_threshold_mv=settings.peak_delta_threshold_mv,
        configured_confirmation_count=settings.consecutive_deltas_required,
        stabilized=stabilized,
        timed_out=timed_out,
        stabilization_cycle=stabilization_cycle,
        stabilization_elapsed_s=stabilization_elapsed_s,
        confirming_window_max_delta_mv=confirming_window_max_delta_mv,
        last_delta_mv=last_delta_mv,
        capture_cycles=len(cycles),
        measurement_cycle_count=len(measurement_cycles),
        measurement_cycles_required=measurement_cycles_required,
        total_pwm_on_seconds=total_pwm_on_seconds,
        stability_deadline_s=deadline,
        pwm_elapsed_offset_s=offset,
        data_source=str(data_source),
    )
    return StabilityAnalysis(
        cycles=tuple(cycles),
        measurement_cycles=measurement_cycles,
        report=report,
    )


__all__ = [
    "CycleAnalysis",
    "DEFAULT_CONSECUTIVE_DELTAS_REQUIRED",
    "DEFAULT_MEASUREMENT_CYCLES_REQUIRED",
    "DEFAULT_PEAK_DELTA_THRESHOLD_MV",
    "DEFAULT_SETTINGS_PATH",
    "DEFAULT_STABILITY_DEADLINE_S",
    "DEFAULT_SYNC_FREQUENCY_HZ",
    "DEFAULT_SYNC_FREQUENCY_TOLERANCE_HZ",
    "DEFAULT_SYNC_VALIDATION_CYCLES",
    "StabilityAnalysis",
    "StabilityReport",
    "StabilitySettings",
    "StabilitySettingsError",
    "SyncValidation",
    "SyncValidationError",
    "analyze_stability",
    "complete_cycle_segments",
    "load_stability_settings",
    "rising_edge_indices",
    "robust_upper_peak_v",
    "validate_rising_sync_cycles",
]
