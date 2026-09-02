"""FROZEN golden reference for the array rig's noise DSP - DO NOT EDIT.

Verbatim copy of the emitter-off noise functions of the single-detector
rig's 405 M22 build, taken from single_detector_rig/m405m22/stability_analysis.py at commit d7526b5
(2026-09-02): _at_or_below_threshold and the whole "Emitter-off noise
analysis" section. It is pure stdlib and slow, and that is the point: the
numpy port in array_analysis.py must reproduce these numbers, and
test_array_analysis.py compares the two on every run. A separate test
compares this file's text with the live 405 module so drift in either
direction is noticed.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Sequence


def _at_or_below_threshold(value: float, threshold: float) -> bool:
    # Avoid allowing binary floating-point representation alone to reject a
    # mathematically exact 0.100 mV boundary.
    return value <= threshold or math.isclose(
        value, threshold, rel_tol=1e-12, abs_tol=1e-12
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
