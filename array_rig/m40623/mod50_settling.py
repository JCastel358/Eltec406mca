"""Sample-clock settling evidence for scheduling/review, never acceptance limits."""
from collections import deque

import numpy as np

POLICY = "mod50_dc_80percent_five_1s_windows_1mv_v3"
LEGACY_POLICY = "mod50_80percent_five_1s_windows_mean_and_rms_v2"
WINDOW_SECONDS = 1.0
WINDOWS = 5
MAX_SECONDS = 90.0
# DC permits a 1 mV spread across five one-second means (2026-11-12 user
# request). The previous two-code floor was 0.305 mV on +/-5 V and flagged
# a measured 0.835 mV start-to-end change over a 10 s record. Keep the
# two-code floor and finite-window uncertainty allowance. This is a
# provisional scheduling/review tolerance, not an offset acceptance limit.
DC_TOLERANCE_V = .001
# Retain the superseded RMS estimator for historical diagnostics only.
BASE_TOLERANCE_V = .0001
RMS_RELATIVE_TOLERANCE = .10
READY_FRACTION = .80
NOISE_WARMUP_SECONDS = 90.0
NOISE_WARMUP_POLICY = "mod50_noise_fixed90_sample_clock_warmup_v1"
NOISE_POLICY = "mod50_noise_centered_variation_trend_80percent_5to90_v2"
NOISE_ZERO_TOLERANCE_V = .010
NOISE_BASELINE_TOLERANCE_V = .001
NOISE_TREND_NOISE_FACTOR = 3.0
NOISE_SLOPE_TOLERANCE_V_PER_S = .00025
NOISE_HEADROOM_V = .8


class NoiseSettlingMonitor:
    """Watch startup baseline recovery without requiring stable noise amplitude.

    Thresholds refer to amplified DAQ output volts and are provisional scheduling
    tolerances. Judge the center and persistent trend across five one-second
    means, rather than treating correlated low-frequency noise as 1000
    independent samples/second. A coherent ramp or step still blocks startup;
    random baseline reversals and changing RMS amplitude do not.
    """
    def __init__(self, rate, lsb, positions, max_seconds=MAX_SECONDS, trace_limit=None):
        if not np.isfinite(rate) or rate <= 0 or not np.isfinite(lsb) or lsb <= 0:
            raise ValueError("Expected a positive sample rate and ADC code size")
        self.rate, self.positions, self.max_seconds = float(rate), list(positions), float(max_seconds)
        self.window_size = max(2, round(rate * WINDOW_SECONDS))
        self.tolerance = max(NOISE_BASELINE_TOLERANCE_V, 2 * lsb)
        self.pending = np.empty((len(self.positions), 0))
        self.pending_clipping = np.empty((len(self.positions), 0), dtype=bool)
        self.windows = deque(maxlen=WINDOWS)
        self.trace = deque(maxlen=trace_limit)
        self.samples, self.complete, self.ever_stable = 0, False, set()
        self.first_ready_s = None
        self.channels = []

    def feed(self, volts, clipping=None, *, stop_when_ready=True):
        values = np.asarray(volts, dtype=float)
        if values.ndim != 2 or values.shape[0] != len(self.positions) or not np.all(np.isfinite(values)):
            raise ValueError("Invalid noise settling samples")
        clipped = np.zeros(values.shape, dtype=bool) if clipping is None else np.asarray(clipping, dtype=bool)
        if clipped.shape != values.shape:
            raise ValueError("Clipping evidence must match noise samples")
        self.pending = np.concatenate((self.pending, values), axis=1)
        self.pending_clipping = np.concatenate((self.pending_clipping, clipped), axis=1)
        while self.pending.shape[1] >= self.window_size:
            window = self.pending[:, :self.window_size]
            rail = self.pending_clipping[:, :self.window_size]
            self.pending = self.pending[:, self.window_size:]
            self.pending_clipping = self.pending_clipping[:, self.window_size:]
            means, rms = window.mean(axis=1), window.std(axis=1)
            half = self.window_size // 2
            drift = window[:, half:].mean(axis=1) - window[:, :half].mean(axis=1)
            clipped_now = rail.any(axis=1) | (np.abs(window) > NOISE_HEADROOM_V).any(axis=1)
            self.windows.append((means, rms, drift, clipped_now))
            self.samples += self.window_size
            levels, amplitudes, drifts, clips = (np.stack([w[i] for w in self.windows]) for i in range(4))
            baseline_span = np.ptp(levels, axis=0)
            center = levels.mean(axis=0)
            near_zero = np.abs(center) <= NOISE_ZERO_TOLERANCE_V
            clipped_recent = clips.any(axis=0)
            t = np.arange(len(self.windows), dtype=float) * self.window_size / self.rate
            t -= t.mean()
            slope = ((t[:, None] * levels).sum(axis=0) / np.dot(t, t)
                     if len(t) > 1 else np.zeros(len(self.positions)))
            full = len(self.windows) == WINDOWS
            residual = levels - center - t[:, None] * slope
            # Estimate variation from the window means themselves. Dividing
            # raw RMS by sqrt(1000) falsely assumes independent samples and
            # mistook the measured 1-1 noise for a never-ending startup ramp.
            slope_variation = (np.sqrt(np.sum(residual ** 2, axis=0) / (len(t) - 2) / np.dot(t, t))
                               if len(t) >= 3 else np.zeros(len(self.positions)))
            trend_threshold = NOISE_SLOPE_TOLERANCE_V_PER_S + NOISE_TREND_NOISE_FACTOR * slope_variation
            significant = np.abs(slope) > trend_threshold
            # Also catch monotonic recovery/steps whose curved shape inflates
            # fit residuals. Ignore reversals smaller than the 1 mV/code floor.
            consistent = ((np.abs(slope) > NOISE_SLOPE_TOLERANCE_V_PER_S)
                          & (baseline_span > self.tolerance)
                          & np.all(np.diff(levels, axis=0) * np.sign(slope) >= -self.tolerance, axis=0))
            drifting = full & (significant | consistent)
            rising, falling = drifting & (slope > 0), drifting & (slope < 0)
            stable = full & near_zero & ~clipped_recent & ~drifting
            self.channels = []
            for i, position in enumerate(self.positions):
                state = ("clipped" if clipped_recent[i] else "rising" if rising[i] else "falling" if falling[i]
                         else "above_zero" if not near_zero[i] and center[i] >= 0 else "below_zero" if not near_zero[i]
                         else "ready" if stable[i] else "collecting")
                self.channels.append({"position": position, "state": state, "mean_v": float(center[i]),
                    "latest_window_mean_v": float(means[i]),
                    "rms_v": float(rms[i]), "slope_v_per_s": float(slope[i]), "baseline_span_v": float(baseline_span[i]),
                    "baseline_tolerance_v": self.tolerance, "trend_threshold_v_per_s": float(trend_threshold[i]),
                    "trend_significant": bool(significant[i]), "trend_consistent": bool(consistent[i]),
                    "clipped": bool(clipped_recent[i]),
                    "near_zero": bool(near_zero[i]), "stable": bool(stable[i])})
            stable_positions = [p for p, ok in zip(self.positions, stable) if ok]
            self.ever_stable.update(stable_positions)
            self.trace.append({"end_s": self.samples / self.rate, "mean_v": means.tolist(), "rms_v": rms.tolist(),
                "half_window_drift_v": drift.tolist(), "slope_v_per_s": slope.tolist(),
                "center_v": center.tolist(), "trend_threshold_v_per_s": trend_threshold.tolist(),
                "clipped_positions": [p for p, clip in zip(self.positions, clipped_recent) if clip],
                "stable_positions": stable_positions})
            self.complete = bool(full and len(stable_positions) >= np.ceil(READY_FRACTION * len(self.positions)))
            if self.complete and self.first_ready_s is None:
                self.first_ready_s = self.samples / self.rate
            if self.complete and stop_when_ready:
                return True
        return self.complete

    def evidence(self):
        stable = {c["position"] for c in self.channels if c["stable"]}
        return {"policy": NOISE_POLICY, "complete": self.complete, "waited_s": self.samples / self.rate,
            "max_seconds": self.max_seconds, "window_seconds": self.window_size / self.rate, "windows_required": WINDOWS,
            "criterion": "centered_unclipped_no_persistent_trend", "noise_stability_required": False,
            "zero_tolerance_v": NOISE_ZERO_TOLERANCE_V, "base_tolerance_v": self.tolerance,
            "trend_noise_factor": NOISE_TREND_NOISE_FACTOR, "slope_tolerance_v_per_s": NOISE_SLOPE_TOLERANCE_V_PER_S,
            "first_ready_s": self.first_ready_s,
            "noise_headroom_v": NOISE_HEADROOM_V, "ready_fraction": READY_FRACTION,
            "stable_count": len(stable), "required_count": int(np.ceil(READY_FRACTION * len(self.positions))),
            "positions": self.positions, "unstable_positions": [p for p in self.positions if p not in stable],
            "channels": list(self.channels), "trace": list(self.trace), "clock": "sample_clock",
            "voltage_reference": "DAQ_OUTPUT", "acceptance_limit": False}


class NoiseWarmup:
    """Wait for startup recovery without judging stochastic noise stability."""
    def __init__(self, rate, positions, seconds=NOISE_WARMUP_SECONDS):
        self.rate, self.positions, self.seconds = rate, list(positions), seconds
        self.samples = 0
        self.complete = False

    def feed(self, volts):
        values = np.asarray(volts, dtype=float)
        if values.ndim != 2 or values.shape[0] != len(self.positions) or not np.all(np.isfinite(values)):
            raise ValueError("Invalid warm-up samples")
        self.samples += values.shape[1]
        self.complete = self.samples >= round(self.seconds * self.rate)
        return self.complete

    def evidence(self):
        return {"policy": NOISE_WARMUP_POLICY, "complete": self.complete,
                "waited_s": self.samples / self.rate, "required_seconds": self.seconds,
                "positions": self.positions, "clock": "sample_clock",
                "criterion": "elapsed_time_only", "noise_stability_required": False,
                "acceptance_limit": False}


class SettlingMonitor:
    """DC drift monitor; the legacy noise estimator is for diagnostics only."""
    def __init__(self, rate, lsb, positions, mode):
        self.rate, self.mode = rate, mode
        self.positions = list(positions)
        self.window_size = max(1, round(rate * WINDOW_SECONDS))
        self.tolerance = max(DC_TOLERANCE_V if mode == "dc" else BASE_TOLERANCE_V, 2 * lsb)
        self.pending = np.empty((len(positions), 0))
        self.windows = deque(maxlen=WINDOWS)
        self.trace = []
        self.samples = 0
        self.complete = False
        self.ever_stable = set()

    def feed(self, volts, *, stop_when_ready=True):
        values = np.asarray(volts, dtype=float)
        if values.ndim != 2 or values.shape[0] != len(self.positions) or not np.all(np.isfinite(values)):
            raise ValueError("Invalid settling samples")
        self.pending = np.concatenate((self.pending, values), axis=1)
        while self.pending.shape[1] >= self.window_size:
            window = self.pending[:, :self.window_size]
            self.pending = self.pending[:, self.window_size:]
            means = window.mean(axis=1)
            rms = window.std(axis=1)
            # Half-window means prevent a ramp hidden by a repeatable average.
            half = self.window_size // 2
            drift = np.abs(window[:, :half].mean(axis=1) - window[:, half:].mean(axis=1))
            self.samples += self.window_size
            self.windows.append((means, rms, drift))
            stable = np.zeros(len(self.positions), dtype=bool)
            if len(self.windows) == WINDOWS:
                levels, amplitudes, drifts = (np.stack([w[i] for w in self.windows]) for i in range(3))
                noise_allowance = 3 * amplitudes.max(axis=0) / np.sqrt(self.window_size)
                level_tolerance = np.maximum(self.tolerance, noise_allowance)
                stable = (np.ptp(levels, axis=0) <= level_tolerance) & (drifts.max(axis=0) <= 2 * level_tolerance)
                if self.mode == "noise":
                    envelope_tolerance = np.maximum(self.tolerance, RMS_RELATIVE_TOLERANCE * amplitudes.mean(axis=0))
                    stable &= np.ptp(amplitudes, axis=0) <= envelope_tolerance
            self.trace.append({"end_s": self.samples / self.rate, "mean_v": means.tolist(),
                               "rms_v": rms.tolist(), "half_window_drift_v": drift.tolist(),
                               "stable_positions": [p for p, ok in zip(self.positions, stable) if ok]})
            self.ever_stable.update(self.trace[-1]["stable_positions"])
            self.complete = bool(len(self.windows) == WINDOWS and np.count_nonzero(stable) >= np.ceil(READY_FRACTION * len(stable)))
            if self.complete and stop_when_ready:
                return True
        return self.complete

    def evidence(self):
        stable = set(self.trace[-1]["stable_positions"]) if self.trace else set()
        return {"policy": POLICY if self.mode == "dc" else LEGACY_POLICY,
                "complete": self.complete, "waited_s": self.samples / self.rate,
                "max_seconds": MAX_SECONDS, "window_seconds": WINDOW_SECONDS,
                "windows_required": WINDOWS, "base_tolerance_v": self.tolerance,
                "rms_relative_tolerance": RMS_RELATIVE_TOLERANCE,
                "ready_fraction": READY_FRACTION, "stable_count": len(stable),
                "required_count": int(np.ceil(READY_FRACTION * len(self.positions))),
                "positions": self.positions, "unstable_positions": [p for p in self.positions if p not in stable],
                "trace": list(self.trace), "clock": "sample_clock", "acceptance_limit": False}
