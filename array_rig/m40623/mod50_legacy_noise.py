"""Unvalidated old-meter-scale estimate for the amplified M25/HUB noise path.

This is a nominal behavioural model, not a calibration of either instrument.
The M25 already supplies nominal x1000 gain: do NOT multiply DAQ volts by
another x1000. Apply the nominal old/new gain ratio, the causal 3 Hz/Q3
band-pass, absolute value x10, a 10 s exponential smoother, then an ideal
peak hold reset at record start. The drawing's "100 second peak hold" is a
retention description, not evidence for a 100 s exponential decay constant.
No droop, circuit offsets, measured transfer correction or acceptance limits
are invented. All states restart for each independent recording/attempt.
"""
from __future__ import annotations

import math

import numpy as np

from legacy_noise import bandpass_coefficients, CENTER_HZ, FILTER_Q, SMOOTH_TAU_S


METHOD_ID = "mod50_legacy_equivalent_3hz_q3_abs10_tau10_idealhold100_v1"
MOD50_NOMINAL_GAIN = 1000.0
LEGACY_FILTER_NOMINAL_GAIN = 1000.0
RECTIFIER_GAIN = 10.0
REFERENCE_SECONDS = 60.0
HOLD_SECONDS = 100.0
VALUE_FIELDS = ("legacy_equivalent_mv", "legacy_equivalent_60s_mv",
                "legacy_smoothed_final_mv", "legacy_peak_time_s")
RESULT_FIELDS = (*VALUE_FIELDS, "legacy_equivalent_method_id",
                 "legacy_equivalent_status", "legacy_equivalent_detail")


def method_metadata(sample_rate_hz=None):
    """Versioned assumptions, included in raw recordings and saved reports."""
    result = {
        "method_id": METHOD_ID, "validation_status": "UNVALIDATED",
        "noise_pass_fail_enabled": False, "acceptance_limits_mv": None,
        "input_reference": "DAQ_OUTPUT", "output_reference": "NOMINAL_LEGACY_METER_MV",
        "mod50_nominal_gain_v_per_v": MOD50_NOMINAL_GAIN,
        "legacy_filter_nominal_gain_v_per_v": LEGACY_FILTER_NOMINAL_GAIN,
        "nominal_gain_ratio": LEGACY_FILTER_NOMINAL_GAIN / MOD50_NOMINAL_GAIN,
        "filter": {"center_hz": CENTER_HZ, "q": FILTER_Q,
                   "implementation": "causal single-pass bilinear band-pass, prewarped at 3 Hz",
                   "band_truncation": None},
        "rectifier": "absolute value", "rectifier_gain": RECTIFIER_GAIN,
        "smoothing": "single-pole exponential, zero-order-held samples", "smoothing_tau_s": SMOOTH_TAU_S,
        "hold": "ideal running maximum of smoothed magnitude since reset",
        "hold_record_s": HOLD_SECONDS, "reference_readout_s": REFERENCE_SECONDS,
        "hold_droop_model": "none; unmeasured", "hold_decay_tau_s": None,
        "startup": "subtract initial DAQ sample; filter, smoother and hold start at zero at recording start",
        "readout": "held value at 60 s and end of record (at most 100 s), using actual sample rate",
        "limitations": [
            "Nominal x1000 M25 and x1000 legacy gains; no measured channel transfer correction.",
            "Ideal hold omits leakage, droop, acquisition time and circuit offsets.",
            "Cold-start filter/smoother does not reconstruct the old fixture's pre-reset state.",
            "Full legacy analogue response and background have not been qualified.",
            "Compare paired detectors under matched vacuum, bias, temperature and timing before enabling limits.",
        ],
        "sources": ["40623_legacy_fixture/9000232_revB_filter.jpeg",
                    "40623_legacy_fixture/9000272_revJ_rectifier_hold.jpeg",
                    "40623_legacy_fixture/TP120_noise_page5.jpeg"],
    }
    if sample_rate_hz is not None:
        b, a = bandpass_coefficients(sample_rate_hz)
        result.update(actual_sample_rate_hz=float(sample_rate_hz))
        result["filter"].update(b=b.tolist(), a=a.tolist())
    return result


def unavailable(status="NOT_MEASURED", reason="No valid noise recording is available."):
    return {**dict.fromkeys(VALUE_FIELDS), "legacy_equivalent_method_id": METHOD_ID,
            "legacy_equivalent_status": status,
            "legacy_equivalent_detail": {"reason": reason}}


def analyze(samples, sample_rate_hz):
    """Return one estimate per channel of finite DAQ volts [channels, samples].

    Work on full-rate samples, never the reduced display excerpt or an RMS
    value. A sample represents [n/fs, (n+1)/fs); elapsed readout time follows
    the update. At 60 s use the first sample boundary at or after 60 s. Retain
    up to 100 s, and withhold numeric readings for shorter-than-60 s records.
    Callers exclude clipped, flat, incomplete and unoccupied channels first.
    """
    b, a = bandpass_coefficients(sample_rate_hz)
    rate = float(sample_rate_hz)
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim == 1:
        values = values[None, :]
    if values.ndim != 2 or min(values.shape) < 1 or not np.isfinite(values).all():
        raise ValueError("Legacy estimate requires finite DAQ volts [channels, samples]")
    channels, available = values.shape
    reference_count = math.ceil(REFERENCE_SECONDS * rate - 1e-9)
    if available < reference_count:
        results = [unavailable("TOO_SHORT", "Legacy estimate needs at least 60 seconds of uninterrupted noise data.")
                   for _ in range(channels)]
        for row in results:
            row["legacy_equivalent_detail"]["recorded_seconds"] = available / rate
        return results
    count = min(available, round(HOLD_SECONDS * rate))
    baseline = values[:, 0]
    z1, z2, smooth, held = (np.zeros(channels) for _ in range(4))
    peak_count = np.zeros(channels, dtype=int)
    reference = None
    alpha = -math.expm1(-1.0 / (rate * SMOOTH_TAU_S))
    scale = RECTIFIER_GAIN * LEGACY_FILTER_NOMINAL_GAIN / MOD50_NOMINAL_GAIN
    stride = max(1, math.ceil(count / 400))
    times, smooth_trace, held_trace = [0.0], [smooth.copy()], [held.copy()]
    for index in range(count):
        x = values[:, index] - baseline
        filtered = b[0] * x + z1
        z1 = z2 - a[1] * filtered
        z2 = b[2] * x - a[2] * filtered
        smooth += alpha * (np.abs(filtered) * scale - smooth)
        peak_count[smooth > held] = index + 1
        np.maximum(held, smooth, out=held)
        elapsed_samples = index + 1
        if elapsed_samples == reference_count:
            reference = held.copy()
        if elapsed_samples % stride == 0 or elapsed_samples in (reference_count, count):
            times.append(elapsed_samples / rate)
            smooth_trace.append(smooth.copy())
            held_trace.append(held.copy())
    smoothed_mv, held_mv = np.asarray(smooth_trace).T * 1000, np.asarray(held_trace).T * 1000
    return [{
        "legacy_equivalent_mv": float(held[ch] * 1000),
        "legacy_equivalent_60s_mv": float(reference[ch] * 1000),
        "legacy_smoothed_final_mv": float(smooth[ch] * 1000),
        "legacy_peak_time_s": float(peak_count[ch] / rate),
        "legacy_equivalent_method_id": METHOD_ID,
        "legacy_equivalent_status": "UNVALIDATED_ESTIMATE",
        "legacy_equivalent_detail": {
            "recorded_seconds": available / rate, "readout_elapsed_s": count / rate,
            "reference_readout_elapsed_s": reference_count / rate,
            "time_s": list(times), "smoothed_mv": smoothed_mv[ch].tolist(), "held_mv": held_mv[ch].tolist(),
        },
    } for ch in range(channels)]
