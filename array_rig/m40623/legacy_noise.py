"""40623 noise evidence around the legacy fixture's 3 Hz passband.

The detector is connected through a unity-gain buffer, not through fixtures
9000232 and 9000272. This module therefore reports detector-referred quantities.
Its causal, unity-centre-gain second-order band-pass models the *nominal* 3 Hz,
Q=3 stage in drawing 9000232 rev B. It is not a measured model of the complete
analogue chain. In particular it does not simulate the 100-second peak-hold
circuit in 9000272, whose response must be established experimentally.

Signed waveform averaging is deliberately never used as a noise amplitude.
RMS, mean absolute value and a 10-second exponentially smoothed absolute value
are distinct metrics. None is assigned TP120's 10.0--37.9 mV meter limits.
Only an explicit, paired calibration for this acquisition setup can enable
noise decisions. Measurements below its demonstrated resolution remain
unresolved rather than being called low-noise detector failures.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


METHOD_ID = "40623_3hz_q3_rectified_tau10_v1"
CENTER_HZ = 3.0
FILTER_Q = 3.0
SMOOTH_TAU_S = 10.0
FILTER_SETTLE_S = 5.0
MIN_CAPTURE_S = 60.0
LEGACY_METER_LOW_MV = 10.0
LEGACY_METER_HIGH_MV = 37.9
_METRICS = ("band_rms_mv", "band_mean_abs_mv", "smoothed_abs_final_mv")


def _finite(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{name} must be {'positive and ' if positive else ''}finite")
    return number


@dataclass(frozen=True)
class LegacyNoiseConfig:
    """The acquisition identity recorded with every calibration and capture.

    ``setup_id`` identifies the specific analogue board/wiring and qualification
    configuration. The default UNQUALIFIED value cannot enable acceptance.
    Oversampling's ideal noise reduction is not assumed by the floor estimate.
    """

    input_gain: float = 1.0
    adc_min_v: float = 0.0
    adc_max_v: float = 5.0
    adc_bits: int = 16
    range_code: int = 2
    oversample_extra: int = 3
    drop_first: int = 1
    setup_id: str = "UNQUALIFIED"

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_gain", _finite(self.input_gain, "input_gain", positive=True))
        object.__setattr__(self, "adc_min_v", _finite(self.adc_min_v, "adc_min_v"))
        object.__setattr__(self, "adc_max_v", _finite(self.adc_max_v, "adc_max_v"))
        if self.adc_max_v <= self.adc_min_v:
            raise ValueError("adc_max_v must exceed adc_min_v")
        for name, low, high in (("adc_bits", 2, 32), ("range_code", 0, 255),
                               ("oversample_extra", 0, 255), ("drop_first", 0, 255)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}]")
        if self.drop_first > self.oversample_extra:
            raise ValueError("drop_first must leave at least one conversion per channel")
        if not isinstance(self.setup_id, str) or not self.setup_id.strip():
            raise ValueError("setup_id must be a nonempty string")

    @property
    def adc_lsb_v(self) -> float:
        return (self.adc_max_v - self.adc_min_v) / (2 ** self.adc_bits)

    def acquisition_signature(self, sample_rate_hz: float) -> dict[str, Any]:
        return {**asdict(self), "sample_rate_hz": _finite(sample_rate_hz, "sample_rate_hz", positive=True)}


@dataclass(frozen=True)
class LegacyNoiseCalibration:
    """Measured bounds for a named software metric, never nominal gain division.

    The background and resolution values use the same units and metric as the
    bounds. ``minimum_resolvable_metric_mv`` is the lowest demonstrated usable
    signal in the paired qualification; it must exceed the measured background
    and be below the low acceptance boundary. The acquisition fingerprint is
    stored as canonical JSON so later mutation of a caller's dictionary cannot
    change which hardware configuration this record qualifies.
    """

    calibration_id: str
    provenance: str
    metric: str
    low_mv: float
    high_mv: float
    background_metric_mv: float
    minimum_resolvable_metric_mv: float
    acquisition_json: str
    method_id: str = METHOD_ID
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise ValueError("unsupported noise calibration schema_version")
        if self.method_id != METHOD_ID:
            raise ValueError("noise calibration method_id does not match this analysis")
        if not isinstance(self.calibration_id, str) or not self.calibration_id.strip():
            raise ValueError("calibration_id must identify the paired calibration")
        if any(word in self.calibration_id.upper() for word in ("PENDING", "UNQUALIFIED")):
            raise ValueError("a pending calibration cannot enable noise decisions")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ValueError("provenance must identify the paired measurements and background qualification")
        if self.metric not in _METRICS:
            raise ValueError(f"metric must be one of {_METRICS}")
        for name in ("low_mv", "high_mv", "background_metric_mv", "minimum_resolvable_metric_mv"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if not (0 <= self.background_metric_mv < self.minimum_resolvable_metric_mv < self.low_mv <= self.high_mv):
            raise ValueError("require 0 <= background < minimum resolvable < low <= high, in the calibrated metric")
        acquisition = json.loads(self.acquisition_json)
        if not isinstance(acquisition, dict):
            raise ValueError("acquisition must be an object")
        values = dict(acquisition)
        if "sample_rate_hz" not in values:
            raise ValueError("acquisition.sample_rate_hz is required")
        rate = _finite(values.pop("sample_rate_hz"), "sample_rate_hz", positive=True)
        if set(values) != set(LegacyNoiseConfig.__dataclass_fields__):
            raise ValueError("acquisition must contain every LegacyNoiseConfig field and sample_rate_hz")
        config = LegacyNoiseConfig(**values)
        if any(word in config.setup_id.upper() for word in ("PENDING", "UNQUALIFIED")):
            raise ValueError("acquisition.setup_id must identify a qualified analogue setup")
        _validate_rate(rate)
        object.__setattr__(self, "acquisition_json", json.dumps(config.acquisition_signature(rate), sort_keys=True))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LegacyNoiseCalibration":
        values = dict(data)
        required = {"schema_version", "method_id", "calibration_id", "provenance", "metric", "low_mv",
                    "high_mv", "background_metric_mv", "minimum_resolvable_metric_mv", "acquisition"}
        if set(values) != required:
            raise ValueError(f"calibration fields mismatch; missing={sorted(required-set(values))}, unexpected={sorted(set(values)-required)}")
        acquisition = values.pop("acquisition")
        if not isinstance(acquisition, Mapping):
            raise ValueError("calibration acquisition must be an object")
        values["acquisition_json"] = json.dumps(dict(acquisition), sort_keys=True, allow_nan=False)
        return cls(**values)

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["acquisition"] = json.loads(values.pop("acquisition_json"))
        return values


def load_noise_calibration(path: str | Path) -> LegacyNoiseCalibration:
    """Read a strict JSON calibration. Errors are actionable, never ignored."""
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("noise calibration root must be an object")
    return LegacyNoiseCalibration.from_dict(data)


@dataclass(frozen=True)
class LegacyNoiseAnalysis:
    channel: int
    position: str
    band_rms_mv: float
    band_mean_abs_mv: float
    smoothed_abs_final_mv: float
    nominal_meter_estimate_mv: float
    duration_s: float
    analysis_duration_s: float
    adc_lsb_mv: float
    raw_span_lsb: float
    estimated_white_quantization_rms_mv: float
    clipped_samples: int
    verdict: str
    quality_status: str
    quality_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    calibration_id: str
    calibrated_metric: str | None
    calibrated_low_mv: float | None
    calibrated_high_mv: float | None
    acquisition_json: str
    method_id: str = METHOD_ID
    calibration_json: str = "null"

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["acquisition"] = json.loads(values.pop("acquisition_json"))
        values["calibration"] = json.loads(values.pop("calibration_json"))
        return values


def _validate_rate(sample_rate_hz: float) -> float:
    rate = _finite(sample_rate_hz, "sample_rate_hz", positive=True)
    # A low sample rate cannot reproduce this passband accurately or retain
    # sufficient anti-alias margin. The rig normally runs at 1000 scans/s.
    if rate < 20.0:
        raise ValueError("3 Hz noise analysis requires at least 20 samples/s per channel")
    return rate


def bandpass_coefficients(sample_rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    """Bilinear transform of H(s)=(w0/Q)s/(s*s+(w0/Q)s+w0*w0).

    Prewarping fixes the digital centre at 3 Hz. The centre gain is one;
    the analogue bandwidth is f0/Q = 1 Hz. This is a causal single pass,
    not forward-backward filtering, which would change the response.
    """
    rate = _validate_rate(sample_rate_hz)
    w = math.tan(math.pi * CENTER_HZ / rate)
    normalizer = 1.0 + w / FILTER_Q + w * w
    b0 = (w / FILTER_Q) / normalizer
    return (np.array([b0, 0.0, -b0]),
            np.array([1.0, 2.0 * (w*w - 1.0) / normalizer,
                      (1.0 - w/FILTER_Q + w*w) / normalizer]))


def analyze_legacy_noise(
    raw: Any,
    sample_rate_hz: float,
    *,
    positions: Sequence[str],
    channels: Sequence[int] | None = None,
    config: LegacyNoiseConfig = LegacyNoiseConfig(),
    calibration: LegacyNoiseCalibration | None = None,
) -> tuple[list[LegacyNoiseAnalysis], np.ndarray, float]:
    """Analyse uniformly timed DAQ volts shaped [channels, samples].

    Returns results, the normalized band-passed waveform in detector volts,
    and its unchanged sample rate. Five seconds of filter startup are excluded
    from RMS/mean-absolute statistics. The 10-second smoother starts at zero
    at capture start and runs for the entire record; at least 60 seconds are
    required for a production decision. Uniform timing, analogue anti-aliasing
    and real instrument noise must be established by bench qualification.
    """
    rate = _validate_rate(sample_rate_hz)
    array = np.asarray(raw, dtype=np.float64)
    if array.ndim == 1:
        array = array[None, :]
    if array.ndim != 2 or array.shape[1] < 2 or array.shape[0] < 1:
        raise ValueError("raw must be [channels, samples] with at least two samples")
    if not np.isfinite(array).all():
        raise ValueError("noise waveform samples must all be finite")
    channel_count, sample_count = array.shape
    if len(positions) != channel_count:
        raise ValueError("positions must name every capture channel")
    channel_ids = list(range(channel_count)) if channels is None else list(channels)
    if len(channel_ids) != channel_count:
        raise ValueError("channels must number every capture channel")

    b, a = bandpass_coefficients(rate)
    # Subtract only the initial DC value before a zero-state band-pass. This
    # avoids an artificial startup step from detector offset; no one-second
    # detrending is applied, since that would redefine the 3 Hz measurement.
    centred = (array - array[:, :1]) / config.input_gain
    filtered = np.empty_like(centred)
    z1 = np.zeros(channel_count)
    z2 = np.zeros(channel_count)
    for index in range(sample_count):
        value = centred[:, index]
        out = b[0] * value + z1
        z1 = z2 - a[1] * out
        z2 = b[2] * value - a[2] * out
        filtered[:, index] = out

    duration = sample_count / rate
    skipped = min(sample_count - 1, int(math.ceil(FILTER_SETTLE_S * rate)))
    usable = filtered[:, skipped:]
    rms = np.sqrt(np.mean(usable * usable, axis=1)) * 1000.0
    mean_abs = np.mean(np.abs(usable), axis=1) * 1000.0
    # Exact final value of the causal discrete exponential smoother with
    # zero initial state. expm1 keeps accuracy for high acquisition rates.
    step = 1.0 / (rate * SMOOTH_TAU_S)
    weights = -math.expm1(-step) * np.exp(-step * np.arange(sample_count-1, -1, -1))
    smoothed = np.abs(filtered) @ weights * 1000.0
    # The q/sqrt(12) model assumes uniform, uncorrelated quantisation error.
    # This ENBW approximation is for fs >> f0 and is NOT a measured noise
    # floor, nor evidence that an undithered sub-LSB signal was recovered.
    enbw_hz = math.pi * CENTER_HZ / (2.0 * FILTER_Q)
    lsb_detector_mv = config.adc_lsb_v / config.input_gain * 1000.0
    theoretical_quantization = lsb_detector_mv / math.sqrt(12.0) * math.sqrt(2.0 * enbw_hz / rate)
    signature = config.acquisition_signature(rate)
    signature_json = json.dumps(signature, sort_keys=True)
    mismatch = calibration is not None and json.loads(calibration.acquisition_json) != signature
    results: list[LegacyNoiseAnalysis] = []
    for index in range(channel_count):
        quality: list[str] = []
        warnings = [
            "Nominal 3 Hz/Q=3 filter model; complete legacy analogue and peak-hold response is not reproduced.",
            "Nominal meter estimate uses 1000 x 10 gain and the 10 s smoother; it is not an acceptance reading.",
            "Quantization estimate assumes white error; buffer/DAQ background and anti-aliasing require measured qualification.",
        ]
        if duration < MIN_CAPTURE_S and not math.isclose(duration, MIN_CAPTURE_S, abs_tol=1e-12):
            quality.append(f"Capture is {duration:.3f} s; at least {MIN_CAPTURE_S:.0f} s is required for a noise decision.")
        # Include both converter rails. Range overruns are acquisition faults,
        # never a pass merely because a band-pass suppresses a flat rail.
        clipped = int(np.count_nonzero((array[index] <= config.adc_min_v + config.adc_lsb_v) |
                                      (array[index] >= config.adc_max_v - config.adc_lsb_v)))
        if clipped:
            quality.append(f"{clipped} raw samples are at or within one ADC code of a converter rail.")
        span_lsb = float(np.ptp(array[index]) / config.adc_lsb_v)
        if span_lsb <= 1.0 + 1e-10:
            warnings.append("Raw waveform spans at most one ADC code; undithered detector noise may be unresolved.")
        if float(np.ptp(array[index])) == 0.0:
            quality.append("Raw waveform is constant; detector noise is unresolved at this acquisition setting.")
        if mismatch:
            quality.append("Calibration acquisition settings do not match this capture (including analogue setup ID).")
        metrics = {"band_rms_mv": float(rms[index]), "band_mean_abs_mv": float(mean_abs[index]),
                   "smoothed_abs_final_mv": float(smoothed[index])}
        verdict = "NO_LIMIT"
        calibration_id = "PENDING" if calibration is None else calibration.calibration_id
        if calibration is None:
            warnings.append("Paired legacy readings and same-setup background qualification are missing; noise acceptance is pending.")
        elif not quality:
            measured = metrics[calibration.metric]
            if measured <= calibration.minimum_resolvable_metric_mv:
                quality.append("Noise is at or below the calibrated resolution floor; a low-noise detector failure cannot be distinguished.")
            elif measured < calibration.low_mv and not math.isclose(measured, calibration.low_mv, rel_tol=1e-12, abs_tol=1e-12):
                verdict = "LOW"
            elif measured > calibration.high_mv and not math.isclose(measured, calibration.high_mv, rel_tol=1e-12, abs_tol=1e-12):
                verdict = "HIGH"
            else:
                verdict = "PASS"
        if quality:
            verdict = "NOT_MEASURED"
        results.append(LegacyNoiseAnalysis(
            channel=int(channel_ids[index]), position=str(positions[index]),
            **metrics, nominal_meter_estimate_mv=float(smoothed[index] * 10000.0),
            duration_s=duration, analysis_duration_s=usable.shape[1]/rate,
            adc_lsb_mv=config.adc_lsb_v*1000.0, raw_span_lsb=span_lsb,
            estimated_white_quantization_rms_mv=theoretical_quantization,
            clipped_samples=clipped, verdict=verdict,
            quality_status="INVALID" if quality else ("UNQUALIFIED" if calibration is None else "QUALIFIED"),
            quality_reasons=tuple(quality), warnings=tuple(warnings), calibration_id=calibration_id,
            calibrated_metric=None if calibration is None else calibration.metric,
            calibrated_low_mv=None if calibration is None else calibration.low_mv,
            calibrated_high_mv=None if calibration is None else calibration.high_mv,
            acquisition_json=signature_json,
            calibration_json=json.dumps(calibration.as_dict(), sort_keys=True) if calibration else "null",
        ))
    return results, filtered, rate


__all__ = ["METHOD_ID", "CENTER_HZ", "FILTER_Q", "SMOOTH_TAU_S", "FILTER_SETTLE_S",
           "MIN_CAPTURE_S", "LEGACY_METER_LOW_MV", "LEGACY_METER_HIGH_MV", "LegacyNoiseConfig",
           "LegacyNoiseCalibration", "LegacyNoiseAnalysis", "load_noise_calibration",
           "bandpass_coefficients", "analyze_legacy_noise"]
