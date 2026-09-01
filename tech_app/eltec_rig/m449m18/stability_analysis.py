"""Peak-delta stabilization analysis for the 449 M18 (5/18 Hz) ESP32 tester.

Identical rules to the 406MCA v6/v6.1 and 405 M22 builds, retimed for the
Model 449 M18's TP443 frequency-tracking drives: 5 Hz (200 ms cycles) and
18 Hz (55.6 ms cycles). Cycles are short, so the stabilization deadline is
30 s - long enough for the emitter/part to settle thermally after PWM-on,
short enough that a part that never settles is rejected quickly.

Sync validation at 18 Hz has to respect sample quantization: at 1000 SPS an
18 Hz period spans 55.56 samples, so a single measured period can only read
55 or 56 samples (18.18 or 17.86 Hz), i.e. up to 0.18 Hz off even with a
perfect drive. ``validate_rising_sync_cycles`` therefore judges the MEAN
frequency of the validation cycles against the requested tolerance and gives
each individual cycle an extra one-sample quantization allowance.

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


# Library fallback for tests and explicit programmatic use only. The
# production DUT limit is the tracked ``stability_settings.json`` value; the
# AIN1 reference unit uses the application's own
# REFERENCE_PEAK_DELTA_THRESHOLD_MV instead of either of these.
DEFAULT_PEAK_DELTA_THRESHOLD_MV = 0.100
DEFAULT_CONSECUTIVE_DELTAS_REQUIRED = 5
# 5 Hz cycles last 200 ms and 18 Hz cycles 56 ms, so qualification plus the
# measurement window needs only a few seconds of PWM-on time; 30 s leaves
# room for the thermal settle of the emitter/part after PWM-on. The
# production windows (20 cycles at 5 Hz, 4 blocks of 9 cycles at 18 Hz) are
# passed explicitly by the application; this is the library fallback.
DEFAULT_STABILITY_DEADLINE_S = 30.0
DEFAULT_MEASUREMENT_CYCLES_REQUIRED = 10
DEFAULT_MAX_MEASUREMENT_ATTEMPTS = 3
# Model 449 M18 first (low-frequency) drive: 5 Hz +/- 0.05 Hz per TP443.
DEFAULT_SYNC_FREQUENCY_HZ = 5.0
DEFAULT_SYNC_FREQUENCY_TOLERANCE_HZ = 0.05
DEFAULT_SYNC_VALIDATION_CYCLES = 5
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
    unstable: bool = False
    unstable_reason: str | None = None
    phase: str = "stabilizing"
    measurement_attempt: int = 1
    measurement_failures: int = 0
    active_confirmation_count: int = DEFAULT_CONSECUTIVE_DELTAS_REQUIRED
    active_confirmation_run_length: int = 0
    initial_measurement_cycles_required: int = DEFAULT_MEASUREMENT_CYCLES_REQUIRED

    @property
    def measurement_complete(self) -> bool:
        return self.phase == "complete"

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

    N complete cycles require N+1 rising edges.  A mere low/high span or
    isolated transition is deliberately insufficient: a sync wiring/firmware
    fault must be surfaced as a rig error, never as an unstable DUT.

    The MEAN frequency over the validation cycles must sit within
    ``frequency_tolerance_hz`` of ``expected_frequency_hz``. Each individual
    cycle additionally gets a one-sample quantization allowance
    (``expected**2 / sample_rate``): a period can only be measured to whole
    samples, and at 18 Hz / 1000 SPS that alone is worth 0.18 Hz.
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
    # One sample of period quantization, expressed in Hz at this frequency
    # (df = f^2 * dt). Individual cycles get this on top of the tolerance;
    # the mean over the validation cycles does not.
    quantization_hz = expected * expected / rate
    per_cycle_tolerance = tolerance + quantization_hz
    invalid_periods = [
        frequency
        for frequency in period_frequencies_hz
        if abs(frequency - expected) > per_cycle_tolerance
        and not math.isclose(
            abs(frequency - expected),
            per_cycle_tolerance,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ]
    mean_error_hz = abs(measured_frequency_hz - expected)
    mean_invalid = mean_error_hz > tolerance and not math.isclose(
        mean_error_hz, tolerance, rel_tol=1e-12, abs_tol=1e-12
    )
    if invalid_periods or mean_invalid:
        observed_min = min(period_frequencies_hz)
        observed_max = max(period_frequencies_hz)
        raise SyncValidationError(
            f"PWM sync frequency is {measured_frequency_hz:.3f} Hz; "
            f"individual validation cycles span {observed_min:.3f}-"
            f"{observed_max:.3f} Hz, expected {expected:.2f} +/- "
            f"{tolerance:.2f} Hz (each cycle +/- {per_cycle_tolerance:.2f} Hz "
            "with the one-sample quantization allowance)"
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


def block_sync_cycles(
    sync_v: Sequence[float], cycles_per_block: int, *, threshold: float = 0.5
) -> list[float]:
    """Collapse every ``cycles_per_block`` PWM cycles into one analysis cycle.

    Returns a synthetic 0/1 sync whose rising edges are every k-th rising
    edge of the real sync (k = ``cycles_per_block``), so the unchanged
    peak-delta machinery judges stability per BLOCK of k drive cycles: the
    robust upper peak of a block is the median of its top 10 % samples
    across all k pulses, which cancels the sample-grid phase drift that a
    single short cycle cannot (at 18 Hz / 1000 SPS a cycle spans 55.56
    samples, so consecutive cycles sample the waveform at different phases;
    9 cycles = exactly 500 samples, after which the phases repeat). Cycles
    left over after the last full block stay low, exactly like the partial
    cycle after the last real edge. With k = 1 the real sync is returned
    unchanged (as a list).
    """

    if (
        isinstance(cycles_per_block, bool)
        or not isinstance(cycles_per_block, int)
        or cycles_per_block < 1
    ):
        raise ValueError("cycles_per_block must be a positive integer")
    sync = [float(value) for value in sync_v]
    if cycles_per_block == 1:
        return sync
    edges = rising_edge_indices(sync, threshold=threshold)
    block_edges = edges[::cycles_per_block]
    block_sync = [0.0] * len(sync)
    for block_start in block_edges:
        # High for the first real cycle of the block, low for the rest: one
        # rising edge per block and a falling edge well before the next.
        next_edge_index = edges.index(block_start) + 1
        block_end_high = (
            edges[next_edge_index] if next_edge_index < len(edges) else len(sync)
        )
        for index in range(block_start, block_end_high):
            block_sync[index] = 1.0
    return block_sync


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
    enforce_measurement_stability: bool = False,
    max_measurement_attempts: int = DEFAULT_MAX_MEASUREMENT_ATTEMPTS,
    data_source: str = "ESP32 AIN0",
) -> StabilityAnalysis:
    """Analyze an uninterrupted capture using v6 or the v6.1 DUT rule.

    Stability is the earliest run of the configured number of adjacent robust
    peak deltas at or below the threshold whose final cycle closes on or before
    ``stability_deadline_s``.  Official measurement cycles are the next fresh
    complete cycles after that decision cycle.  The deadline governs only the
    stabilization decision; those measurement cycles may close later.

    With ``enforce_measurement_stability``, every selected measurement cycle
    must also remain within the peak-delta threshold. All attempts use the
    configured confirmation count and requested measurement window. A third
    kick is an immediate unstable decision. Requalification must still finish
    by the stability deadline; an in-progress measurement window may finish
    later.

    Re-running this function as a stream grows is safe and deterministic.
    """

    if not isinstance(settings, StabilitySettings):
        raise TypeError("settings must be a StabilitySettings instance")
    if not isinstance(enforce_measurement_stability, bool):
        raise TypeError("enforce_measurement_stability must be a bool")
    if (
        isinstance(max_measurement_attempts, bool)
        or not isinstance(max_measurement_attempts, int)
        or max_measurement_attempts < 1
    ):
        raise ValueError("max_measurement_attempts must be a positive integer")
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

    # Samples are indexed from zero: N samples span N-1 sample intervals.
    captured_sample_span_s = 0.0 if not waveform else (len(waveform) - 1) / rate
    total_pwm_on_seconds = offset + captured_sample_span_s
    initial_confirmation_count = settings.consecutive_deltas_required
    initial_measurement_count = measurement_cycles_required
    measurement_attempt = 1
    measurement_failures = 0
    active_confirmation_count = initial_confirmation_count
    active_measurement_count = initial_measurement_count
    phase = "stabilizing"
    unstable_reason: str | None = None
    active_confirmation_run_length = (
        cycles[-1].confirmation_run_length if cycles else 0
    )

    if enforce_measurement_stability:
        phase_confirmation_run = 0
        active_stabilization_index: int | None = None
        active_confirming_max_delta_mv: float | None = None
        selected_measurement_cycles: list[CycleAnalysis] = []
        deadline_timed_out = False

        for cycle_index, cycle in enumerate(cycles):
            if cycle.within_threshold is None:
                continue

            if phase == "stabilizing":
                phase_confirmation_run = (
                    phase_confirmation_run + 1 if cycle.within_threshold else 0
                )
                if (
                    phase_confirmation_run >= active_confirmation_count
                    and _at_or_before_deadline(cycle.end_elapsed_s, deadline)
                ):
                    active_stabilization_index = cycle_index
                    confirming = cycles[
                        cycle_index - active_confirmation_count + 1 :
                        cycle_index + 1
                    ]
                    active_confirming_max_delta_mv = max(
                        candidate.absolute_peak_delta_mv
                        for candidate in confirming
                        if candidate.absolute_peak_delta_mv is not None
                    )
                    selected_measurement_cycles = []
                    phase = "measuring"
                elif not _at_or_before_deadline(cycle.end_elapsed_s, deadline):
                    phase = "unstable"
                    deadline_timed_out = True
                    unstable_reason = (
                        f"Attempt {measurement_attempt} could not complete "
                        f"{active_confirmation_count} consecutive stable deltas "
                        f"within {deadline:.1f} s."
                    )
                    break
            elif phase == "measuring":
                if cycle.within_threshold:
                    selected_measurement_cycles.append(cycle)
                    if len(selected_measurement_cycles) >= active_measurement_count:
                        phase = "complete"
                        break
                else:
                    measurement_failures += 1
                    selected_measurement_cycles = []
                    if measurement_failures >= max_measurement_attempts:
                        phase = "unstable"
                        unstable_reason = (
                            f"Peak delta exceeded {settings.peak_delta_threshold_mv:.3f} mV "
                            f"during measurement attempt {measurement_attempt} of "
                            f"{max_measurement_attempts}."
                        )
                        break
                    measurement_attempt += 1
                    active_confirmation_count = initial_confirmation_count
                    active_measurement_count = initial_measurement_count
                    phase_confirmation_run = 0
                    active_stabilization_index = None
                    active_confirming_max_delta_mv = None
                    phase = "stabilizing"

        if phase == "stabilizing" and _deadline_reached(
            total_pwm_on_seconds, deadline
        ):
            phase = "unstable"
            deadline_timed_out = True
            unstable_reason = (
                f"Attempt {measurement_attempt} could not complete "
                f"{active_confirmation_count} consecutive stable deltas "
                f"within {deadline:.1f} s."
            )

        timed_out = deadline_timed_out
        unstable = phase == "unstable"
        stabilized = phase in ("measuring", "complete")
        active_confirmation_run_length = phase_confirmation_run
        measurement_cycles = tuple(selected_measurement_cycles)
        if active_stabilization_index is None:
            stabilization_cycle = None
            stabilization_elapsed_s = None
            confirming_window_max_delta_mv = None
        else:
            stabilization = cycles[active_stabilization_index]
            stabilization_cycle = stabilization.cycle_number
            stabilization_elapsed_s = stabilization.end_elapsed_s
            confirming_window_max_delta_mv = active_confirming_max_delta_mv
    else:
        stabilized = stabilization_index is not None
        timed_out = not stabilized and _deadline_reached(
            total_pwm_on_seconds, deadline
        )
        unstable = timed_out
        if stabilization_index is None:
            stabilization_cycle = None
            stabilization_elapsed_s = None
            confirming_window_max_delta_mv = None
            measurement_cycles = ()
        else:
            stabilization = cycles[stabilization_index]
            stabilization_cycle = stabilization.cycle_number
            stabilization_elapsed_s = stabilization.end_elapsed_s
            confirming_cycles = cycles[
                stabilization_index - initial_confirmation_count + 1 :
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
                    stabilization_index + 1 + initial_measurement_count
                ]
            )
        if timed_out:
            phase = "unstable"
            unstable_reason = (
                f"Could not complete {initial_confirmation_count} consecutive "
                f"stable deltas within {deadline:.1f} s."
            )
        elif len(measurement_cycles) >= initial_measurement_count:
            phase = "complete"
        elif stabilized:
            phase = "measuring"

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
        measurement_cycles_required=active_measurement_count,
        total_pwm_on_seconds=total_pwm_on_seconds,
        stability_deadline_s=deadline,
        pwm_elapsed_offset_s=offset,
        data_source=str(data_source),
        unstable=unstable,
        unstable_reason=unstable_reason,
        phase=phase,
        measurement_attempt=measurement_attempt,
        measurement_failures=measurement_failures,
        active_confirmation_count=active_confirmation_count,
        active_confirmation_run_length=active_confirmation_run_length,
        initial_measurement_cycles_required=initial_measurement_count,
    )
    return StabilityAnalysis(
        cycles=tuple(cycles),
        measurement_cycles=measurement_cycles,
        report=report,
    )


# --------------------------------------------------------------------------- #
# Emitter-off noise analysis (TP412 noise test).
#
# The noise capture runs with the PWM drive OFF, so there are no sync edges and
# none of the cycle-based machinery above applies.  The waveform is instead cut
# into fixed non-overlapping time windows and the pass rule operates on those
# windows.  All functions here are pure and sync-free.
# --------------------------------------------------------------------------- #


def fixed_window_segments(
    sample_count: int, window_samples: int
) -> tuple[tuple[int, int], ...]:
    """Return non-overlapping [start, end) windows; a partial tail is dropped."""

    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < 0
    ):
        raise ValueError("sample_count must be a non-negative integer")
    if (
        isinstance(window_samples, bool)
        or not isinstance(window_samples, int)
        or window_samples < 1
    ):
        raise ValueError("window_samples must be a positive integer")
    return tuple(
        (start, start + window_samples)
        for start in range(0, sample_count - window_samples + 1, window_samples)
    )


def window_peak_to_peak_mv(
    waveform_v: Sequence[float],
    segments: Sequence[tuple[int, int]],
) -> tuple[float, ...]:
    """Raw max - min per window, in millivolts."""

    waveform = [float(value) for value in waveform_v]
    if not all(math.isfinite(value) for value in waveform):
        raise ValueError("waveform samples must all be finite")
    peaks: list[float] = []
    for start, end in segments:
        if not 0 <= start < end <= len(waveform):
            raise ValueError(f"window [{start}, {end}) is out of bounds")
        window = waveform[start:end]
        peaks.append((max(window) - min(window)) * 1000.0)
    return tuple(peaks)


def decimate_boxcar(
    waveform_v: Sequence[float], factor: int
) -> tuple[float, ...]:
    """Average non-overlapping ``factor``-sample blocks (partial tail dropped).

    A boxcar reduces white instrument noise by sqrt(factor) and rolls off
    content above ~0.443 * (rate/factor). It is kept for the live preview
    (cheap enough to run inside the capture loop) and for callers that want
    the historical behavior, but the PRODUCTION VERDICT no longer uses it:
    its stopband sidelobes are only ~-13 dB, so out-of-band interference
    (60 Hz mains, spike-train harmonics) folded into the analysis band when
    the trace was decimated — measured at up to 41% of the in-band signal on
    a real interference-heavy capture. ``analyze_noise_capture_band_limited``
    now decimates through ``decimate_antialiased`` instead. ``factor`` 1 is
    a pass-through.
    """

    if isinstance(factor, bool) or not isinstance(factor, int) or factor < 1:
        raise ValueError("factor must be a positive integer")
    waveform = [float(value) for value in waveform_v]
    if not all(math.isfinite(value) for value in waveform):
        raise ValueError("waveform samples must all be finite")
    if factor == 1:
        return tuple(waveform)
    return tuple(
        statistics.fmean(waveform[start : start + factor])
        for start in range(0, len(waveform) - factor + 1, factor)
    )


# Anti-alias FIR design targets, normalized to the post-decimation Nyquist
# (rate / (2 * factor)). The passband edge 0.886 * Nyquist equals the old
# boxcar's -3 dB corner (0.443 * rate/factor), so the judged bandwidth is
# unchanged; the stopband starts just past Nyquist and rejects >= 60 dB,
# where the boxcar's first sidelobe rejected only ~13 dB (60 Hz mains folded
# to 10 Hz at -16 dB and counted as part noise).
_ANTIALIAS_PASSBAND_OF_NYQUIST = 0.886
_ANTIALIAS_STOPBAND_OF_NYQUIST = 1.12
_ANTIALIAS_STOPBAND_DB = 60.0
_antialias_taps_cache: dict[tuple[int, int], tuple[float, ...]] = {}


def _bessel_i0(x: float) -> float:
    """Modified Bessel function I0 by its power series (for the Kaiser window)."""

    total = term = 1.0
    k = 0
    while term > total * 1e-16:
        k += 1
        term *= (x / (2.0 * k)) ** 2
        total += term
    return total


def design_antialias_lowpass_fir(
    factor: int, max_taps: int | None = None
) -> tuple[float, ...]:
    """Kaiser windowed-sinc low-pass for decimation by ``factor``.

    The design depends only on ``factor`` (all edges are proportional to the
    sample rate), so taps are cached. ``max_taps`` (odd, >= 3) caps the
    length for short inputs; attenuation degrades gracefully when capped.
    """

    if isinstance(factor, bool) or not isinstance(factor, int) or factor < 2:
        raise ValueError("factor must be an integer >= 2")
    nyquist = 0.5 / factor  # cycles/sample at the input rate
    pass_edge = _ANTIALIAS_PASSBAND_OF_NYQUIST * nyquist
    stop_edge = _ANTIALIAS_STOPBAND_OF_NYQUIST * nyquist
    beta = 0.1102 * (_ANTIALIAS_STOPBAND_DB - 8.7)
    transition_w = 2.0 * math.pi * (stop_edge - pass_edge)
    taps = int(math.ceil((_ANTIALIAS_STOPBAND_DB - 7.95) / (2.285 * transition_w)))
    taps += (taps + 1) % 2  # symmetric type-I filter needs an odd length
    if max_taps is not None:
        limit = max(3, max_taps - (max_taps + 1) % 2)
        taps = min(taps, limit)
    key = (factor, taps)
    cached = _antialias_taps_cache.get(key)
    if cached is not None:
        return cached
    middle = (taps - 1) // 2
    cutoff_w = math.pi * (pass_edge + stop_edge)  # midpoint, rad/sample
    i0_beta = _bessel_i0(beta)
    kernel = []
    for m in range(taps):
        offset = m - middle
        if offset == 0:
            ideal = cutoff_w / math.pi
        else:
            ideal = math.sin(cutoff_w * offset) / (math.pi * offset)
        window = _bessel_i0(beta * math.sqrt(1.0 - (offset / middle) ** 2)) / i0_beta
        kernel.append(ideal * window)
    scale = 1.0 / math.fsum(kernel)  # exact unity DC gain
    result = tuple(value * scale for value in kernel)
    _antialias_taps_cache[key] = result
    return result


def decimate_antialiased(
    waveform_v: Sequence[float],
    factor: int,
    *,
    left_context_v: Sequence[float] | None = None,
    right_context_v: Sequence[float] | None = None,
) -> tuple[float, ...]:
    """Anti-alias low-pass + decimate: the verdict's band-limiting step.

    Produces the same number of samples at the same positions as
    ``decimate_boxcar`` (one output per ``factor``-sample block, centered on
    the block), so window/clip index math is unchanged — but content above
    the post-decimation Nyquist is attenuated >= 60 dB before the rate drop
    instead of the boxcar's ~13 dB, so mains/EMI can no longer fold into the
    analysis band and read as part noise.

    The FIR needs (taps-1)/2 samples of history beyond each end of the
    record. By default that history is synthesized by odd reflection
    (2*x[edge] - x[i]), which continues a linear trend exactly: residual DC
    settling passes through undistorted for the per-window detrend to
    remove, instead of curling at the capture edges. Reflection is wrong
    for OSCILLATORY content though, so out-of-band interference is only
    attenuated ~11-21 dB (not >= 60 dB) within the outermost (taps-1)/2
    raw samples — up to ~27% of a 1 mV interferer leaking into the first
    and last judged window (2026-08-31 audit). ``left_context_v`` /
    ``right_context_v`` close that hole: pass the REAL samples adjacent to
    the record (the tail of the discarded quiet wait, extra streamed
    samples) and the filter seats on them instead of the reflection,
    restoring full stopband attenuation at the edges. Only the innermost
    ``antialias_edge_context_samples(factor)`` samples of each context are
    used; a shorter (or absent) context falls back to reflection for the
    remainder, so calls without context reproduce the pre-2026-08-31
    output bit-for-bit and every archived capture replays unchanged. The
    contexts are filter history only — never part of the returned trace.
    ``factor`` 1 is a pass-through.
    """

    if isinstance(factor, bool) or not isinstance(factor, int) or factor < 1:
        raise ValueError("factor must be a positive integer")
    waveform = [float(value) for value in waveform_v]
    left = (
        []
        if left_context_v is None
        else [float(value) for value in left_context_v]
    )
    right = (
        []
        if right_context_v is None
        else [float(value) for value in right_context_v]
    )
    if not all(math.isfinite(value) for value in waveform):
        raise ValueError("waveform samples must all be finite")
    if not all(math.isfinite(value) for value in left) or not all(
        math.isfinite(value) for value in right
    ):
        raise ValueError("context samples must all be finite")
    if factor == 1:
        return tuple(waveform)
    blocks = len(waveform) // factor
    if blocks == 0:
        return ()
    taps = design_antialias_lowpass_fir(
        factor, max_taps=2 * len(waveform) - 1
    )
    half = (len(taps) - 1) // 2
    # Seat the filter on real neighbour samples where provided (innermost
    # `half` of each context); odd-reflect the extended record for whatever
    # history is still missing. Empty contexts reduce exactly to the
    # historical all-reflection padding, so replays are bit-identical.
    used_left = left[max(0, len(left) - half):]
    used_right = right[:half]
    core = used_left + waveform + used_right
    need_left = half - len(used_left)
    need_right = half - len(used_right)
    first, last = core[0], core[-1]
    padded = (
        [2.0 * first - value for value in core[need_left:0:-1]]
        + core
        + [2.0 * last - value for value in core[-2 : -need_right - 2 : -1]]
    )
    sumprod = getattr(math, "sumprod", None)
    if sumprod is None:  # Python < 3.12
        def sumprod(a, b):  # noqa: ANN001 - simple local fallback
            return sum(x * y for x, y in zip(a, b))
    # Output k sits at the center of raw block k (index k*factor + factor//2,
    # matching the boxcar's block alignment within half a sample).
    return tuple(
        sumprod(taps, padded[k * factor + factor // 2 : k * factor + factor // 2 + len(taps)])
        for k in range(blocks)
    )


def antialias_edge_context_samples(factor: int) -> int:
    """Raw samples of real history that fully seat the anti-alias FIR.

    (taps - 1) / 2 for the uncapped decimation-by-``factor`` design: the
    number of samples beyond each end of a capture that
    ``decimate_antialiased`` needs so its first and last outputs are
    computed from real signal instead of reflection padding (the
    production factor 20 designs 621 taps -> 310 raw samples = 0.31 s at
    1000 SPS).
    """

    return (len(design_antialias_lowpass_fir(factor)) - 1) // 2


@dataclass(frozen=True)
class NoiseAnalysis:
    """Windowed peak-to-peak verdict for one emitter-off noise capture."""

    windows_total: int
    windows_over: int
    over_fraction: float
    worst_pp_mv: float
    median_pp_mv: float
    window_pp_mv: tuple[float, ...]
    clipped_windows: int
    passed: bool

    def as_dict(self) -> dict[str, Any]:
        """Return a stable CSV/JSON-friendly field mapping."""

        return asdict(self)


def analyze_noise_capture(
    waveform_v: Sequence[float],
    sample_rate_hz: float,
    *,
    window_s: float = 1.0,
    threshold_mv: float = 300.0,
    max_over_fraction: float = 0.20,
    clip_limit_v: float = 4.9,
) -> NoiseAnalysis:
    """Apply the windowed TP412 noise rule to an emitter-off capture.

    The capture is cut into fixed ``window_s`` windows (partial tail dropped).
    A window counts as over-limit when its raw peak-to-peak exceeds
    ``threshold_mv`` OR any of its samples touches ``+/-clip_limit_v`` (a
    clipped window understates the true excursion, so it is counted against
    the sensor rather than for it).  The capture passes when the over-limit
    fraction is at most ``max_over_fraction``.
    """

    rate = float(sample_rate_hz)
    window = float(window_s)
    threshold = float(threshold_mv)
    fraction_limit = float(max_over_fraction)
    clip_limit = float(clip_limit_v)
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("sample_rate_hz must be a positive finite number")
    if not math.isfinite(window) or window <= 0.0:
        raise ValueError("window_s must be a positive finite number")
    if not math.isfinite(threshold) or threshold <= 0.0:
        raise ValueError("threshold_mv must be a positive finite number")
    if not math.isfinite(fraction_limit) or not 0.0 <= fraction_limit < 1.0:
        raise ValueError("max_over_fraction must be a finite number in [0, 1)")
    if not math.isfinite(clip_limit) or clip_limit <= 0.0:
        raise ValueError("clip_limit_v must be a positive finite number")

    waveform = [float(value) for value in waveform_v]
    if not all(math.isfinite(value) for value in waveform):
        raise ValueError("waveform samples must all be finite")
    window_samples = int(round(window * rate))
    if window_samples < 1:
        raise ValueError("window_s is shorter than one sample period")
    segments = fixed_window_segments(len(waveform), window_samples)
    if not segments:
        raise ValueError(
            "noise capture holds no complete window "
            f"({len(waveform)} samples, window {window_samples})"
        )

    peaks_mv = window_peak_to_peak_mv(waveform, segments)
    windows_over = 0
    clipped_windows = 0
    for (start, end), pp_mv in zip(segments, peaks_mv):
        clipped = any(abs(value) >= clip_limit for value in waveform[start:end])
        if clipped:
            clipped_windows += 1
        if clipped or not _at_or_below_threshold(pp_mv, threshold):
            windows_over += 1
    over_fraction = windows_over / len(segments)
    return NoiseAnalysis(
        windows_total=len(segments),
        windows_over=windows_over,
        over_fraction=over_fraction,
        worst_pp_mv=max(peaks_mv),
        median_pp_mv=float(statistics.median(peaks_mv)),
        window_pp_mv=peaks_mv,
        clipped_windows=clipped_windows,
        passed=_at_or_below_threshold(over_fraction, fraction_limit),
    )


def detrend_window_segments(
    waveform_v: Sequence[float],
    segments: Sequence[tuple[int, int]],
) -> list[float]:
    """Remove each window's own least-squares baseline (mean AND slope).

    This gives every window a fresh baseline: a still-settling DC level -
    which shifts between windows and slopes within them - stops inflating
    the windowed pk-pk, while noise excursions around that moving baseline
    are fully retained. Mirrors the legacy AC-coupled amplifier view, where
    slow offset drift never reached the scope. Only samples covered by
    ``segments`` are returned (a partial tail is dropped).
    """

    waveform = [float(value) for value in waveform_v]
    if not all(math.isfinite(value) for value in waveform):
        raise ValueError("waveform samples must all be finite")
    residual: list[float] = []
    for start, end in segments:
        if not 0 <= start < end <= len(waveform):
            raise ValueError(f"window [{start}, {end}) is out of bounds")
        window = waveform[start:end]
        n = len(window)
        if n == 1:
            residual.append(0.0)
            continue
        mean_x = (n - 1) / 2.0
        mean_y = statistics.fmean(window)
        denominator = sum((index - mean_x) ** 2 for index in range(n))
        slope = (
            sum(
                (index - mean_x) * (window[index] - mean_y)
                for index in range(n)
            )
            / denominator
        )
        residual.extend(
            window[index] - (mean_y + slope * (index - mean_x))
            for index in range(n)
        )
    return residual


def analyze_noise_capture_band_limited(
    waveform_v: Sequence[float],
    sample_rate_hz: float,
    *,
    decimation_factor: int,
    window_s: float = 1.0,
    # threshold_mv / max_over_fraction are REQUIRED on purpose (2026-08-31):
    # they are per-model calibration decisions (docs/CALIBRATION_RECORD.md)
    # and the old defaults were the withdrawn 75 uV / 20% limits — a new
    # caller that omitted them silently got a retired gate. Production
    # passes the NOISE_* constants explicitly.
    threshold_mv: float,
    max_over_fraction: float,
    clip_limit_v: float = 4.9,
    detrend_windows: bool = True,
    left_context_v: Sequence[float] | None = None,
    right_context_v: Sequence[float] | None = None,
) -> tuple[NoiseAnalysis, tuple[float, ...], float]:
    """Windowed pk-pk noise verdict on an anti-alias-decimated capture.

    The pk-pk rule runs on the band-limited trace (matching the low-frequency
    view of the legacy band-limited test amplifier and dropping the ADC's own
    white noise by sqrt(factor)); with ``detrend_windows`` each 1 s window is
    additionally judged against its own least-squares baseline so residual
    DC settling cannot fail a quiet part. Clipping is detected on the RAW
    samples of each window - averaging would hide a railed input. Returns
    ``(analysis, judged_waveform, filtered_rate_hz)`` where
    ``judged_waveform`` is exactly the trace the rule ran on (band-limited,
    detrended when enabled) so callers can plot and record what was gated.

    2026-08-20: the band-limiting step changed from ``decimate_boxcar`` to
    ``decimate_antialiased`` (same passband, same output timeline). The
    boxcar's ~-13 dB sidelobes let out-of-band interference alias into the
    judged band when the rate dropped to 50 SPS — on an interference-heavy
    bench capture the folded-in content measured 41% of the honest in-band
    signal (60 Hz mains folds to 10 Hz at only -16 dB) and inflated windowed
    pk-pk with energy the legacy amplifier chain never displayed. Quiet-part
    readings are unchanged (the lot-500 anchor captures replay to the same
    verdicts); only captures with real >25 Hz content read differently, and
    lower, because the phantom fold-down is gone.

    2026-08-31: ``left_context_v``/``right_context_v`` (real samples
    adjacent to the capture) seat the FIR at the capture edges — see
    ``decimate_antialiased``. Without them the first/last judged window
    only rejects out-of-band interference ~11-21 dB instead of >= 60 dB.
    Contexts are filter history only: windows, clip checks and the verdict
    still cover exactly ``waveform_v``. Omitting them reproduces the
    historical output bit-for-bit (archived replays are unchanged).
    """

    if (
        isinstance(decimation_factor, bool)
        or not isinstance(decimation_factor, int)
        or decimation_factor < 1
    ):
        raise ValueError("decimation_factor must be a positive integer")
    rate = float(sample_rate_hz)
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("sample_rate_hz must be a positive finite number")
    clip_limit = float(clip_limit_v)
    if not math.isfinite(clip_limit) or clip_limit <= 0.0:
        raise ValueError("clip_limit_v must be a positive finite number")

    raw = [float(value) for value in waveform_v]
    if not all(math.isfinite(value) for value in raw):
        raise ValueError("waveform samples must all be finite")
    filtered = decimate_antialiased(
        raw,
        decimation_factor,
        left_context_v=left_context_v,
        right_context_v=right_context_v,
    )
    filtered_rate = rate / decimation_factor
    window_samples = int(round(float(window_s) * filtered_rate))
    if window_samples < 1:
        raise ValueError("window_s is shorter than one filtered sample period")
    segments = fixed_window_segments(len(filtered), window_samples)
    if not segments:
        raise ValueError(
            "noise capture holds no complete window "
            f"({len(filtered)} filtered samples, window {window_samples})"
        )
    judged: Sequence[float]
    if detrend_windows:
        judged = detrend_window_segments(filtered, segments)
    else:
        judged = filtered[: segments[-1][1]]
    # Run the standard rule on the judged trace with clipping disabled
    # (a boxcar-averaged rail sits at the rail DC value with ~zero pk-pk),
    # then re-apply the clip rule from the raw samples window by window.
    base = analyze_noise_capture(
        judged,
        filtered_rate,
        window_s=window_s,
        threshold_mv=threshold_mv,
        max_over_fraction=max_over_fraction,
        clip_limit_v=1e30,  # effectively disabled on the averaged trace
    )
    windows_over = 0
    clipped_windows = 0
    for (start, end), pp_mv in zip(segments, base.window_pp_mv):
        raw_window = raw[start * decimation_factor : end * decimation_factor]
        clipped = any(abs(value) >= clip_limit for value in raw_window)
        if clipped:
            clipped_windows += 1
        if clipped or not _at_or_below_threshold(pp_mv, float(threshold_mv)):
            windows_over += 1
    over_fraction = windows_over / len(segments)
    analysis = NoiseAnalysis(
        windows_total=len(segments),
        windows_over=windows_over,
        over_fraction=over_fraction,
        worst_pp_mv=base.worst_pp_mv,
        median_pp_mv=base.median_pp_mv,
        window_pp_mv=base.window_pp_mv,
        clipped_windows=clipped_windows,
        passed=_at_or_below_threshold(over_fraction, float(max_over_fraction)),
    )
    return analysis, tuple(judged), filtered_rate


__all__ = [
    "CycleAnalysis",
    "DEFAULT_CONSECUTIVE_DELTAS_REQUIRED",
    "DEFAULT_MAX_MEASUREMENT_ATTEMPTS",
    "DEFAULT_MEASUREMENT_CYCLES_REQUIRED",
    "DEFAULT_PEAK_DELTA_THRESHOLD_MV",
    "DEFAULT_SETTINGS_PATH",
    "DEFAULT_STABILITY_DEADLINE_S",
    "DEFAULT_SYNC_FREQUENCY_HZ",
    "DEFAULT_SYNC_FREQUENCY_TOLERANCE_HZ",
    "DEFAULT_SYNC_VALIDATION_CYCLES",
    "NoiseAnalysis",
    "StabilityAnalysis",
    "StabilityReport",
    "StabilitySettings",
    "StabilitySettingsError",
    "SyncValidation",
    "SyncValidationError",
    "analyze_noise_capture",
    "analyze_noise_capture_band_limited",
    "analyze_stability",
    "antialias_edge_context_samples",
    "complete_cycle_segments",
    "decimate_antialiased",
    "decimate_boxcar",
    "design_antialias_lowpass_fir",
    "detrend_window_segments",
    "fixed_window_segments",
    "load_stability_settings",
    "rising_edge_indices",
    "robust_upper_peak_v",
    "validate_rising_sync_cycles",
]
