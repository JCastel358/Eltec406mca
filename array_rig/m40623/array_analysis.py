"""Noise and offset analysis for the Eltec 50-position array rig - model 40623 (TP120).

Pure math and verdict logic: arrays in, dataclasses out. No hardware, no
files, no Tkinter. The tester (``eltec_40623_array_tester.py``) owns the
orchestration and the CSV; ``daq_backend.py`` owns the DAQ.

Noise pipeline
--------------
The numbers are the single-detector rig's (405 M22 build,
``stability_analysis.py`` "Emitter-off noise analysis"), re-expressed in
numpy for fifty channels at once:

    raw 1000 SPS per channel
      -> Kaiser windowed-sinc anti-alias FIR, decimate by 20 (50 SPS)
      -> per-1-s-window least-squares detrend (mean AND slope)
      -> per-window peak-to-peak, clipping re-checked on the RAW window

The judged band is EMERGENT, not a coded band-pass: the FIR's passband
edge (0.886 x the post-decimation Nyquist = 22.15 Hz at 1000/20) is the
top, the 1 s detrend window (-3 dB at 0.85 Hz) is the bottom. Sampling the
DAQ at 1000 scans/s per channel keeps every constant (factor 20, 621 taps,
310-sample edge context) numerically identical to the single rig, so any
number derived on one rig is comparable on the other. The pure-Python
originals are frozen in ``tests/golden_noise_reference.py`` and every test
run checks this port against them.

Limits (docs/CALIBRATION_RECORD.md is the only authority)
---------------------------------------------------------
TP120 rev W gives the model 40623 an OFFSET window of 0.3-1.2 V and a
NOISE window of 10.0-37.9 mV - but the noise figures are DMM readings
behind the legacy amplifier box 9000232 and rectifier-hold circuit
9000272, NOT pin-level numbers. No pin-level equivalent exists yet, so the
noise limit constants below are ``None`` until a paired lot (the same
parts on the legacy fixture and on this rig, ``engineer_tools/
array_noise_parity.py``) derives the chain factor - exactly how the 405
M22's 300 mV / 700 limit was derived. With ``None`` limits every noise
verdict is ``NO_LIMIT``: measured, recorded, never a failure. Everything
this module emits is stamped PROVISIONAL / CALIBRATION PENDING.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Sequence

import numpy as np

# ----------------------------------------------------------------------
# Limits and policy constants (provenance in docs/CALIBRATION_RECORD.md)
# ----------------------------------------------------------------------
# TP120 rev W, "40623 Offset Check": test box 9000054 with a 100 kOhm source
# resistor at +8 V, DMM on the 20 V scale, "let detectors stand for ten to
# fifteen minutes, if needed"; specification min 0.3 V, max 1.2 V. The
# sensitivity pages add the settle rule "wait until reading does not shift
# more than +/- 0.05 V". PROVISIONAL on this rig until the PCB's loading
# (source resistor, supply) is confirmed to match 9000054.
OFFSET_MIN_V = 0.3
OFFSET_MAX_V = 1.2
OFFSET_SETTLE_DELTA_V = 0.05
OFFSET_LIMITS_STATUS = "PROVISIONAL"
# Below this a LOADED socket is judged D (dead / no output: a shorted or open
# FET reads at ground) rather than LO. Same value as the 405 M22's wake-up
# floor (a just-inserted part can read this low for seconds while its DC
# level wakes up, which is why Phase A keeps polling and a LOADED/EMPTY
# decision is the technician's at lock time). Bench-tunable.
OFFSET_DEAD_V = 0.05
# A bad high-offset part rails the buffer/ADC at the top of the 0-5 V range;
# that is an HO failure, never a wiring error (405 lesson).
OFFSET_RAIL_V = 5.0 * 0.98

# TP120 rev W, "40623 Noise": fixture 9000233 (50 sockets), +/-5 V, under
# vacuum, 5 min stabilisation, amplifier box 9000232 -> rectifier hold
# circuit 9000272 -> DMM 200 mV DC, >= 60 s hold. "Noise level must be
# between 10.0 mV and 37.9 mV." A LOW limit exists: too little noise means
# a dead crystal/FET. These are DMM readings at the end of an unknown
# chain and MUST NOT be applied at the pin.
NOISE_LEGACY_PP_LIMIT_LOW_MV = 10.0
NOISE_LEGACY_PP_LIMIT_HIGH_MV = 37.9
# Pin-level limits = legacy limits / chain factor. Not derived yet (the
# 9000232 gain and passband are unknown): None = no noise verdict, only
# measurement + record. Fill in from the paired lot, update
# CALIBRATION_RECORD, bump CALIBRATION_ID, add a CHANGELOG entry.
NOISE_LEGACY_CHAIN_FACTOR: float | None = None
NOISE_PP_LIMIT_LOW_MV: float | None = None
NOISE_PP_LIMIT_HIGH_MV: float | None = None
NOISE_LIMIT_PROVENANCE = "not derived (TP120 legacy limits 10.0-37.9 mV are DMM readings behind 9000232 + 9000272)"
# Structural rule copied from the 405 M22's lot-500 decision (<= 15 % of the
# 1 s windows may exceed the high limit, so one environmental bang does not
# fail a part). RE-DECIDE with the paired lot; recorded per row.
NOISE_MAX_OVER_FRACTION = 0.15
NOISE_DECIMATION_FACTOR = 20       # 1000 SPS -> 50 SPS, identical to the 405 pipeline
NOISE_WINDOW_S = 1.0
NOISE_CLIP_LIMIT_V = OFFSET_RAIL_V  # a window touching the rail is over-limit regardless of pk-pk
CALIBRATION_STATUS = "PENDING"
CALIBRATION_ID = "40623_array50_daq_PENDING"
VERDICT_STATUS = "PROVISIONAL"

# TP120 page 7 failure taxonomy (offset side -> replace FET; noise -> replace
# crystal). NL is this rig's name for "noise below the low limit"; Drop is a
# handling incident, never a verdict.
FAILURE_MODE_CHOICES: tuple[str, ...] = (
    "HO - High offset",
    "LO - Low offset",
    "SH - Shorted FET",
    "D - Dead / no output",
    "N - Noisy (replace crystal)",
    "NL - Noise low (dead crystal/FET)",
    "NM - Not measured (rig fault)",
    "Drop - Dropped / handling",
)
FAILURE_MODE_NOT_MEASURED = "NM"


# ----------------------------------------------------------------------
# Verdict model
# ----------------------------------------------------------------------
class Occupancy(Enum):
    UNKNOWN = "UNKNOWN"   # reads ~0 V; technician has not said empty or loaded yet
    LOADED = "LOADED"
    EMPTY = "EMPTY"


class OffsetClass(Enum):
    OK = "OK"
    HO = "HO"
    HO_RAILED = "HO_RAILED"
    LO = "LO"
    DEAD = "DEAD"
    EMPTY = "EMPTY"


class NoiseVerdict(Enum):
    PASS = "PASS"
    HIGH = "HIGH"
    LOW = "LOW"
    NO_LIMIT = "NO_LIMIT"
    NOT_MEASURED = "NOT_MEASURED"


class PositionVerdict(Enum):
    EMPTY = "EMPTY"
    PASS = "PASS"
    FAIL_OFFSET = "FAIL_OFFSET"
    FAIL_NOISE_HIGH = "FAIL_NOISE_HIGH"
    NOISE_LOW = "NOISE_LOW"
    NOT_MEASURED = "NOT_MEASURED"


class TileState(Enum):
    EMPTY = "EMPTY"
    LOADED = "LOADED"
    SETTLING = "SETTLING"
    OFFSET_FAIL = "OFFSET_FAIL"
    NOISE_FAIL = "NOISE_FAIL"
    NOISE_LOW = "NOISE_LOW"
    PASS = "PASS"
    NOT_MEASURED = "NOT_MEASURED"
    NO_LIMIT = "NO_LIMIT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FailReason:
    """A structured fail reason (no string-prefix sniffing downstream)."""

    code: str            # HO, HO_RAILED, LO, D, N, NL, NM, ...
    text: str
    value: float | None = None
    limit: float | None = None

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class OffsetLimits:
    min_v: float = OFFSET_MIN_V
    max_v: float = OFFSET_MAX_V
    dead_v: float = OFFSET_DEAD_V
    rail_v: float = OFFSET_RAIL_V
    settle_delta_v: float = OFFSET_SETTLE_DELTA_V


@dataclass(frozen=True)
class NoiseLimits:
    low_mv: float | None = NOISE_PP_LIMIT_LOW_MV
    high_mv: float | None = NOISE_PP_LIMIT_HIGH_MV
    max_over_fraction: float = NOISE_MAX_OVER_FRACTION
    clip_limit_v: float = NOISE_CLIP_LIMIT_V
    provenance: str = NOISE_LIMIT_PROVENANCE

    @property
    def defined(self) -> bool:
        return self.low_mv is not None or self.high_mv is not None


@dataclass(frozen=True)
class ChannelNoiseAnalysis:
    channel: int
    position: str
    windows_total: int
    windows_over_high: int | None
    over_fraction: float | None
    worst_pp_mv: float
    median_pp_mv: float
    window_pp_mv: tuple[float, ...]
    clipped_windows: int
    verdict: NoiseVerdict

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verdict"] = self.verdict.value
        return data


@dataclass
class PositionResult:
    position: str
    channel: int
    occupancy: Occupancy
    sensor_number: int | None
    sensor_id: str
    offset_initial_v: float | None
    offset_v: float | None
    offset_settle_delta_v: float | None
    offset_class: OffsetClass | None
    noise: ChannelNoiseAnalysis | None
    verdict: PositionVerdict
    fail_reasons: tuple[FailReason, ...] = ()
    warnings: tuple[str, ...] = ()
    failure_mode_tag: str = ""
    calibration_status: str = CALIBRATION_STATUS
    calibration_id: str = CALIBRATION_ID
    verdict_status: str = VERDICT_STATUS
    provisional: bool = True

    @property
    def passed(self) -> bool:
        return self.verdict is PositionVerdict.PASS

    @property
    def pass_fail_text(self) -> str:
        if self.verdict in (PositionVerdict.PASS,):
            return "PASS"
        if self.verdict is PositionVerdict.NOT_MEASURED:
            return "NOT MEASURED"
        if self.verdict is PositionVerdict.EMPTY:
            return ""
        return "FAIL"


# ----------------------------------------------------------------------
# Anti-alias FIR (design identical to the golden pure-Python version)
# ----------------------------------------------------------------------
_ANTIALIAS_PASSBAND_OF_NYQUIST = 0.886
_ANTIALIAS_STOPBAND_OF_NYQUIST = 1.12
_ANTIALIAS_STOPBAND_DB = 60.0
_antialias_taps_cache: dict[tuple[int, int], np.ndarray] = {}


def _bessel_i0(x: float) -> float:
    total = term = 1.0
    k = 0
    while term > total * 1e-16:
        k += 1
        term *= (x / (2.0 * k)) ** 2
        total += term
    return total


def design_antialias_lowpass_fir(factor: int, max_taps: int | None = None) -> np.ndarray:
    """Kaiser windowed-sinc low-pass for decimation by ``factor`` (golden design, numpy result).

    The tap VALUES are computed with the same scalar arithmetic as the golden
    function (including ``math.fsum`` normalisation) so they are identical,
    not merely close.
    """

    if isinstance(factor, bool) or not isinstance(factor, int) or factor < 2:
        raise ValueError("factor must be an integer >= 2")
    nyquist = 0.5 / factor
    pass_edge = _ANTIALIAS_PASSBAND_OF_NYQUIST * nyquist
    stop_edge = _ANTIALIAS_STOPBAND_OF_NYQUIST * nyquist
    beta = 0.1102 * (_ANTIALIAS_STOPBAND_DB - 8.7)
    transition_w = 2.0 * math.pi * (stop_edge - pass_edge)
    taps = int(math.ceil((_ANTIALIAS_STOPBAND_DB - 7.95) / (2.285 * transition_w)))
    taps += (taps + 1) % 2
    if max_taps is not None:
        limit = max(3, max_taps - (max_taps + 1) % 2)
        taps = min(taps, limit)
    key = (factor, taps)
    cached = _antialias_taps_cache.get(key)
    if cached is not None:
        return cached
    middle = (taps - 1) // 2
    cutoff_w = math.pi * (pass_edge + stop_edge)
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
    scale = 1.0 / math.fsum(kernel)
    result = np.array([value * scale for value in kernel], dtype=np.float64)
    result.setflags(write=False)
    _antialias_taps_cache[key] = result
    return result


def antialias_edge_context_samples(factor: int) -> int:
    """(taps-1)/2 of the uncapped design: 310 raw samples at factor 20."""

    return (len(design_antialias_lowpass_fir(factor)) - 1) // 2


def fixed_window_segments(sample_count: int, window_samples: int) -> tuple[tuple[int, int], ...]:
    """Non-overlapping [start, end) windows; a partial tail is dropped (golden)."""

    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 0:
        raise ValueError("sample_count must be a non-negative integer")
    if isinstance(window_samples, bool) or not isinstance(window_samples, int) or window_samples < 1:
        raise ValueError("window_samples must be a positive integer")
    return tuple(
        (start, start + window_samples)
        for start in range(0, sample_count - window_samples + 1, window_samples)
    )


def at_or_below_threshold(values: Any, threshold: float) -> Any:
    """``<=`` that forgives binary representation at an exact boundary (golden rule)."""

    array = np.asarray(values, dtype=np.float64)
    result = (array <= threshold) | np.isclose(array, threshold, rtol=1e-12, atol=1e-12)
    if array.ndim == 0:
        return bool(result)
    return result


# ----------------------------------------------------------------------
# Multi-channel numpy port of the golden pipeline
# ----------------------------------------------------------------------
def _as_channels(raw: Any) -> np.ndarray:
    array = np.asarray(raw, dtype=np.float64)
    if array.ndim == 1:
        array = array[None, :]
    if array.ndim != 2:
        raise ValueError("expected a [channels, samples] array")
    if not np.all(np.isfinite(array)):
        raise ValueError("waveform samples must all be finite")
    return array


def _context_rows(context: Any, channels: int, name: str) -> list[np.ndarray] | None:
    if context is None:
        return None
    array = np.asarray(context, dtype=np.float64)
    if array.ndim == 1:
        array = array[None, :]
    if array.ndim != 2 or array.shape[0] != channels:
        raise ValueError(f"{name} must be [channels, n] with the same channel count as the capture")
    if not np.all(np.isfinite(array)):
        raise ValueError("context samples must all be finite")
    return [array[c] for c in range(channels)]


def decimate_antialiased_multi(
    raw: Any,
    factor: int,
    *,
    left_context: Any | None = None,
    right_context: Any | None = None,
) -> np.ndarray:
    """Golden ``decimate_antialiased`` for a ``[channels, samples]`` array.

    Same tap design, same odd-reflection padding, same real-neighbour
    seating when contexts are given, same block centring
    (output k = taps . padded[k*factor + factor//2 : +taps]).
    """

    if isinstance(factor, bool) or not isinstance(factor, int) or factor < 1:
        raise ValueError("factor must be a positive integer")
    array = _as_channels(raw)
    channels, samples = array.shape
    if factor == 1:
        return array.copy()
    blocks = samples // factor
    if blocks == 0:
        return np.empty((channels, 0), dtype=np.float64)
    taps = design_antialias_lowpass_fir(factor, max_taps=2 * samples - 1)
    half = (len(taps) - 1) // 2
    lefts = _context_rows(left_context, channels, "left_context")
    rights = _context_rows(right_context, channels, "right_context")
    starts = np.arange(blocks) * factor + factor // 2
    out = np.empty((channels, blocks), dtype=np.float64)
    for c in range(channels):
        wave = array[c]
        left = lefts[c] if lefts is not None else np.empty(0)
        right = rights[c] if rights is not None else np.empty(0)
        used_left = left[max(0, len(left) - half):]
        used_right = right[:half]
        core = np.concatenate((used_left, wave, used_right))
        need_left = half - len(used_left)
        need_right = half - len(used_right)
        first, last = core[0], core[-1]
        left_pad = 2.0 * first - core[1 : need_left + 1][::-1]
        right_pad = 2.0 * last - core[len(core) - 1 - need_right : len(core) - 1][::-1]
        padded = np.concatenate((left_pad, core, right_pad))
        windows = np.lib.stride_tricks.sliding_window_view(padded, len(taps))
        out[c] = windows[starts] @ taps
    return out


def detrend_window_segments_multi(filtered: Any, window_samples: int) -> np.ndarray:
    """Golden ``detrend_window_segments`` for ``[channels, samples]``; partial tail dropped."""

    array = _as_channels(filtered)
    if isinstance(window_samples, bool) or not isinstance(window_samples, int) or window_samples < 1:
        raise ValueError("window_samples must be a positive integer")
    channels, samples = array.shape
    windows = samples // window_samples
    n = window_samples
    cube = array[:, : windows * n].reshape(channels, windows, n)
    if n == 1:
        return np.zeros((channels, windows), dtype=np.float64)
    x = np.arange(n, dtype=np.float64) - (n - 1) / 2.0
    mean_y = cube.mean(axis=2, keepdims=True)
    slope = ((cube - mean_y) * x).sum(axis=2, keepdims=True) / float((x * x).sum())
    residual = cube - (mean_y + slope * x)
    return residual.reshape(channels, windows * n)


def window_peak_to_peak_mv_multi(judged: Any, window_samples: int) -> np.ndarray:
    """``[channels, windows]`` max-min per window in millivolts (partial tail dropped)."""

    array = _as_channels(judged)
    channels, samples = array.shape
    windows = samples // window_samples
    if windows == 0:
        return np.empty((channels, 0), dtype=np.float64)
    cube = array[:, : windows * window_samples].reshape(channels, windows, window_samples)
    return np.ptp(cube, axis=2) * 1000.0


def band_limited_window_pp_mv(
    raw: Any,
    sample_rate_hz: float,
    *,
    decimation_factor: int = NOISE_DECIMATION_FACTOR,
    window_s: float = NOISE_WINDOW_S,
    detrend_windows: bool = True,
    left_context: Any | None = None,
    right_context: Any | None = None,
) -> np.ndarray:
    """Convenience for the bench probe: ``[channels, windows]`` judged-band pk-pk in mV."""

    filtered = decimate_antialiased_multi(raw, decimation_factor, left_context=left_context, right_context=right_context)
    filtered_rate = float(sample_rate_hz) / decimation_factor
    window_samples = int(round(float(window_s) * filtered_rate))
    if window_samples < 1:
        raise ValueError("window_s is shorter than one filtered sample period")
    judged = detrend_window_segments_multi(filtered, window_samples) if detrend_windows else filtered
    return window_peak_to_peak_mv_multi(judged, window_samples)


def analyze_tray_noise(
    raw: Any,
    sample_rate_hz: float,
    *,
    positions: Sequence[str],
    limits: NoiseLimits = NoiseLimits(),
    decimation_factor: int = NOISE_DECIMATION_FACTOR,
    window_s: float = NOISE_WINDOW_S,
    detrend_windows: bool = True,
    left_context: Any | None = None,
    right_context: Any | None = None,
    channels: Sequence[int] | None = None,
) -> tuple[list[ChannelNoiseAnalysis], np.ndarray, float]:
    """Golden ``analyze_noise_capture_band_limited`` for a whole tray.

    ``raw`` is ``[channels, samples]`` volts at ``sample_rate_hz``.
    Returns ``(per-channel analyses, judged trace [channels, windows*n],
    filtered_rate_hz)``. The verdict per channel:

    * HIGH  - more than ``max_over_fraction`` of the windows are over the
      high limit (pk-pk above it OR the raw window touched the clip limit),
    * LOW   - the MEDIAN window pk-pk is below the low limit (a dead
      crystal/FET is quiet in every window; the median ignores a single
      environmental bang),
    * PASS  - otherwise, when at least one limit is defined,
    * NO_LIMIT - no limits defined (measured and recorded only).
    """

    if isinstance(decimation_factor, bool) or not isinstance(decimation_factor, int) or decimation_factor < 1:
        raise ValueError("decimation_factor must be a positive integer")
    rate = float(sample_rate_hz)
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("sample_rate_hz must be a positive finite number")
    if not math.isfinite(limits.clip_limit_v) or limits.clip_limit_v <= 0.0:
        raise ValueError("clip_limit_v must be a positive finite number")
    if not 0.0 <= limits.max_over_fraction < 1.0:
        raise ValueError("max_over_fraction must be in [0, 1)")
    array = _as_channels(raw)
    channel_count = array.shape[0]
    if len(positions) != channel_count:
        raise ValueError("positions must name every channel of the capture")
    channel_numbers = list(channels) if channels is not None else list(range(channel_count))
    if len(channel_numbers) != channel_count:
        raise ValueError("channels must number every channel of the capture")

    filtered = decimate_antialiased_multi(array, decimation_factor, left_context=left_context, right_context=right_context)
    filtered_rate = rate / decimation_factor
    window_samples = int(round(float(window_s) * filtered_rate))
    if window_samples < 1:
        raise ValueError("window_s is shorter than one filtered sample period")
    windows = filtered.shape[1] // window_samples
    if windows == 0:
        raise ValueError(
            f"noise capture holds no complete window ({filtered.shape[1]} filtered samples, window {window_samples})"
        )
    judged = detrend_window_segments_multi(filtered, window_samples) if detrend_windows else filtered[:, : windows * window_samples]
    pp_mv = window_peak_to_peak_mv_multi(judged, window_samples)
    raw_span = windows * window_samples * decimation_factor
    raw_windows = array[:, :raw_span].reshape(channel_count, windows, window_samples * decimation_factor)
    clipped = (np.abs(raw_windows) >= limits.clip_limit_v).any(axis=2)

    results: list[ChannelNoiseAnalysis] = []
    for c in range(channel_count):
        peaks = pp_mv[c]
        worst = float(peaks.max())
        median = float(np.median(peaks))
        clipped_count = int(clipped[c].sum())
        if limits.high_mv is not None:
            over = clipped[c] | ~at_or_below_threshold(peaks, float(limits.high_mv))
            windows_over: int | None = int(over.sum())
            over_fraction: float | None = windows_over / windows
        else:
            windows_over = None
            over_fraction = None
        verdict = NoiseVerdict.NO_LIMIT
        if limits.defined:
            verdict = NoiseVerdict.PASS
            if over_fraction is not None and not at_or_below_threshold(over_fraction, float(limits.max_over_fraction)):
                verdict = NoiseVerdict.HIGH
            elif limits.low_mv is not None and not at_or_below_threshold(float(limits.low_mv), median):
                verdict = NoiseVerdict.LOW
        results.append(
            ChannelNoiseAnalysis(
                channel=int(channel_numbers[c]),
                position=str(positions[c]),
                windows_total=int(windows),
                windows_over_high=windows_over,
                over_fraction=over_fraction,
                worst_pp_mv=worst,
                median_pp_mv=median,
                window_pp_mv=tuple(float(v) for v in peaks),
                clipped_windows=clipped_count,
                verdict=verdict,
            )
        )
    return results, judged, filtered_rate


# ----------------------------------------------------------------------
# Offset classification and the position verdict
# ----------------------------------------------------------------------
def classify_offset(volts: float, *, occupancy: Occupancy, limits: OffsetLimits = OffsetLimits()) -> OffsetClass:
    """TP120 offset band with the rail and dead floors; boundaries are inclusive."""

    if occupancy is Occupancy.EMPTY:
        return OffsetClass.EMPTY
    value = float(volts)
    if not math.isfinite(value):
        raise ValueError("offset must be finite")
    if value >= limits.rail_v or math.isclose(value, limits.rail_v, rel_tol=1e-12, abs_tol=1e-12):
        return OffsetClass.HO_RAILED
    if not at_or_below_threshold(value, limits.max_v):
        return OffsetClass.HO
    if occupancy is Occupancy.LOADED and value < limits.dead_v and not math.isclose(value, limits.dead_v, rel_tol=1e-12, abs_tol=1e-12):
        return OffsetClass.DEAD
    if not at_or_below_threshold(limits.min_v, value):
        return OffsetClass.LO
    return OffsetClass.OK


def offset_is_fail_fast(offset_class: OffsetClass) -> bool:
    """Only HIGH offsets are judged at insertion (offsets settle UPWARD, so a high read is real)."""

    return offset_class in (OffsetClass.HO, OffsetClass.HO_RAILED)


def settle_warning(first_v: float | None, last_v: float | None, *, delta_v: float = OFFSET_SETTLE_DELTA_V) -> str | None:
    """TP120's +/-0.05 V settle rule as a warning (never a verdict)."""

    if first_v is None or last_v is None:
        return None
    shift = float(last_v) - float(first_v)
    if abs(shift) > delta_v and not math.isclose(abs(shift), delta_v, rel_tol=1e-12, abs_tol=1e-12):
        return (
            f"Offset still settling: shifted {shift:+.3f} V during the capture "
            f"(TP120 settle rule: within +/-{delta_v:.2f} V)."
        )
    return None


def _offset_reason(offset_class: OffsetClass, offset_v: float | None, limits: OffsetLimits) -> FailReason | None:
    value = None if offset_v is None else float(offset_v)
    if offset_class is OffsetClass.HO:
        return FailReason("HO", f"High offset: {value:.3f} V, TP120 max {limits.max_v:.1f} V.", value, limits.max_v)
    if offset_class is OffsetClass.HO_RAILED:
        return FailReason("HO", f"High offset (railed at {value:.2f} V, top of the 0-5 V range).", value, limits.max_v)
    if offset_class is OffsetClass.LO:
        return FailReason("LO", f"Low offset: {value:.3f} V, TP120 min {limits.min_v:.1f} V.", value, limits.min_v)
    if offset_class is OffsetClass.DEAD:
        return FailReason("D", f"No output: {value:.3f} V on a loaded socket (dead or shorted FET).", value, limits.dead_v)
    return None


def _noise_reason(noise: ChannelNoiseAnalysis, limits: NoiseLimits) -> FailReason | None:
    high_text = "the high limit" if limits.high_mv is None else f"{limits.high_mv:.4f} mV pk-pk"
    low_text = "the low limit" if limits.low_mv is None else f"{limits.low_mv:.4f} mV"
    if noise.verdict is NoiseVerdict.HIGH:
        return FailReason(
            "N",
            f"Noise high: {noise.windows_over_high} of {noise.windows_total} windows over "
            f"{high_text} (allowed {100.0 * limits.max_over_fraction:.0f} %; worst {noise.worst_pp_mv:.4f} mV).",
            noise.worst_pp_mv,
            limits.high_mv,
        )
    if noise.verdict is NoiseVerdict.LOW:
        return FailReason(
            "NL",
            f"Noise low: median {noise.median_pp_mv:.4f} mV pk-pk below {low_text} (dead crystal/FET).",
            noise.median_pp_mv,
            limits.low_mv,
        )
    return None


def judge_position(
    *,
    position: str,
    channel: int,
    occupancy: Occupancy,
    sensor_number: int | None,
    sensor_id: str,
    offset_initial_v: float | None,
    offset_v: float | None,
    offset_early_v: float | None = None,
    noise: ChannelNoiseAnalysis | None,
    offset_limits: OffsetLimits = OffsetLimits(),
    noise_limits: NoiseLimits = NoiseLimits(),
    rig_fault: str | None = None,
    extra_warnings: Sequence[str] = (),
) -> PositionResult:
    """Combine the settled offset and the noise analysis into one PROVISIONAL verdict.

    Precedence: EMPTY -> NOT_MEASURED (rig fault) -> FAIL_OFFSET (HO, railed,
    LO, D) -> FAIL_NOISE_HIGH -> NOISE_LOW -> PASS. ``offset_v`` is the
    SETTLED value (end of the capture) and is the verdict; ``offset_initial_v``
    is the insertion read, recorded only; ``offset_early_v`` (start of the
    capture) feeds the TP120 settle warning.
    """

    warnings: list[str] = list(extra_warnings)
    if occupancy is Occupancy.EMPTY:
        return PositionResult(
            position=position, channel=channel, occupancy=occupancy, sensor_number=sensor_number,
            sensor_id=sensor_id, offset_initial_v=offset_initial_v, offset_v=offset_v,
            offset_settle_delta_v=None, offset_class=OffsetClass.EMPTY, noise=None,
            verdict=PositionVerdict.EMPTY, warnings=tuple(warnings),
        )
    if rig_fault:
        return PositionResult(
            position=position, channel=channel, occupancy=occupancy, sensor_number=sensor_number,
            sensor_id=sensor_id, offset_initial_v=offset_initial_v, offset_v=offset_v,
            offset_settle_delta_v=None, offset_class=None, noise=None,
            verdict=PositionVerdict.NOT_MEASURED,
            fail_reasons=(FailReason("NM", f"Not measured: {rig_fault}"),),
            warnings=tuple(warnings), failure_mode_tag=FAILURE_MODE_NOT_MEASURED,
        )
    settle_delta = None
    if offset_early_v is not None and offset_v is not None:
        settle_delta = float(offset_v) - float(offset_early_v)
        note = settle_warning(offset_early_v, offset_v, delta_v=offset_limits.settle_delta_v)
        if note:
            warnings.append(note)
    offset_class = None if offset_v is None else classify_offset(offset_v, occupancy=occupancy, limits=offset_limits)
    reasons: list[FailReason] = []
    if offset_class is not None:
        reason = _offset_reason(offset_class, offset_v, offset_limits)
        if reason:
            reasons.append(reason)
    if noise is not None:
        reason = _noise_reason(noise, noise_limits)
        if reason:
            reasons.append(reason)
    if reasons and reasons[0].code in ("HO", "LO", "D"):
        verdict = PositionVerdict.FAIL_OFFSET
    elif noise is not None and noise.verdict is NoiseVerdict.HIGH:
        verdict = PositionVerdict.FAIL_NOISE_HIGH
    elif noise is not None and noise.verdict is NoiseVerdict.LOW:
        verdict = PositionVerdict.NOISE_LOW
    elif offset_v is None:
        verdict = PositionVerdict.NOT_MEASURED
        reasons.append(FailReason("NM", "Not measured: no settled offset reading."))
    else:
        verdict = PositionVerdict.PASS
        if noise is not None and noise.verdict is NoiseVerdict.NO_LIMIT:
            warnings.append("Noise measured and recorded; no pin-level limit derived yet (CALIBRATION PENDING).")
    tag = suggest_failure_mode(verdict, reasons)
    return PositionResult(
        position=position, channel=channel, occupancy=occupancy, sensor_number=sensor_number,
        sensor_id=sensor_id, offset_initial_v=offset_initial_v, offset_v=offset_v,
        offset_settle_delta_v=settle_delta, offset_class=offset_class, noise=noise,
        verdict=verdict, fail_reasons=tuple(reasons), warnings=tuple(warnings), failure_mode_tag=tag,
    )


def suggest_failure_mode(verdict: PositionVerdict, reasons: Sequence[FailReason]) -> str:
    """The TP120 tag the technician will most likely record (editable in the app)."""

    if verdict is PositionVerdict.NOT_MEASURED:
        return FAILURE_MODE_NOT_MEASURED
    codes = [reason.code for reason in reasons]
    for code in ("HO", "LO", "D", "N", "NL"):
        if code in codes:
            return code
    return ""


def tile_state_for(result: PositionResult) -> TileState:
    if result.verdict is PositionVerdict.EMPTY:
        return TileState.EMPTY
    if result.verdict is PositionVerdict.NOT_MEASURED:
        return TileState.NOT_MEASURED
    if result.verdict is PositionVerdict.FAIL_OFFSET:
        return TileState.OFFSET_FAIL
    if result.verdict is PositionVerdict.FAIL_NOISE_HIGH:
        return TileState.NOISE_FAIL
    if result.verdict is PositionVerdict.NOISE_LOW:
        return TileState.NOISE_LOW
    if result.noise is not None and result.noise.verdict is NoiseVerdict.NO_LIMIT:
        return TileState.NO_LIMIT
    return TileState.PASS


def tile_state_for_live_offset(volts: float, *, occupancy: Occupancy, limits: OffsetLimits = OffsetLimits()) -> TileState:
    """Phase A colouring: only HIGH offsets are failures before the capture."""

    if occupancy is Occupancy.EMPTY:
        return TileState.EMPTY
    offset_class = classify_offset(volts, occupancy=occupancy, limits=limits)
    if offset_is_fail_fast(offset_class):
        return TileState.OFFSET_FAIL
    if occupancy is Occupancy.UNKNOWN:
        return TileState.UNKNOWN
    if offset_class in (OffsetClass.LO, OffsetClass.DEAD):
        return TileState.SETTLING
    return TileState.LOADED


# ----------------------------------------------------------------------
# Adaptive quiet wait (never a verdict)
# ----------------------------------------------------------------------
def quiet_wait_settled(
    block_means_v: Any,
    loaded_mask: Any,
    *,
    delta_mv: float,
    blocks_required: int,
) -> bool:
    """True when the last ``blocks_required`` block-to-block mean changes of every LOADED channel are within ``delta_mv``.

    ``block_means_v`` is ``[blocks, channels]`` (one mean per 1 s block).
    Mirrors the 405's adaptive noise wait: it only decides WHEN the capture
    starts (the deadline measures anyway), never whether a part passes.
    """

    means = np.asarray(block_means_v, dtype=np.float64)
    mask = np.asarray(loaded_mask, dtype=bool)
    if blocks_required < 1:
        raise ValueError("blocks_required must be >= 1")
    if means.ndim != 2 or means.shape[1] != mask.shape[0]:
        raise ValueError("block_means_v must be [blocks, channels] matching loaded_mask")
    if means.shape[0] < blocks_required + 1:
        return False
    if not mask.any():
        return True
    deltas = np.abs(np.diff(means[:, mask], axis=0))[-blocks_required:]
    return bool(np.all(at_or_below_threshold(deltas * 1000.0, float(delta_mv))))


__all__ = [
    "CALIBRATION_ID", "CALIBRATION_STATUS", "ChannelNoiseAnalysis", "FAILURE_MODE_CHOICES",
    "FAILURE_MODE_NOT_MEASURED", "FailReason", "NOISE_CLIP_LIMIT_V", "NOISE_DECIMATION_FACTOR",
    "NOISE_LEGACY_CHAIN_FACTOR", "NOISE_LEGACY_PP_LIMIT_HIGH_MV", "NOISE_LEGACY_PP_LIMIT_LOW_MV",
    "NOISE_LIMIT_PROVENANCE", "NOISE_MAX_OVER_FRACTION", "NOISE_PP_LIMIT_HIGH_MV", "NOISE_PP_LIMIT_LOW_MV",
    "NOISE_WINDOW_S", "NoiseLimits", "NoiseVerdict", "OFFSET_DEAD_V", "OFFSET_LIMITS_STATUS", "OFFSET_MAX_V",
    "OFFSET_MIN_V", "OFFSET_RAIL_V", "OFFSET_SETTLE_DELTA_V", "Occupancy", "OffsetClass", "OffsetLimits",
    "PositionResult", "PositionVerdict", "TileState", "VERDICT_STATUS", "analyze_tray_noise",
    "antialias_edge_context_samples", "at_or_below_threshold", "band_limited_window_pp_mv", "classify_offset",
    "decimate_antialiased_multi", "design_antialias_lowpass_fir", "detrend_window_segments_multi",
    "fixed_window_segments", "judge_position", "offset_is_fail_fast", "quiet_wait_settled", "settle_warning",
    "suggest_failure_mode", "tile_state_for", "tile_state_for_live_offset", "window_peak_to_peak_mv_multi",
]
