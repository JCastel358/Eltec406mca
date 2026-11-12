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
# Retain the superseded noise estimator for historical diagnostics only.
# Actual noise acquisition uses NoiseWarmup, with no stability criterion.
BASE_TOLERANCE_V = .0001
RMS_RELATIVE_TOLERANCE = .10
READY_FRACTION = .80
NOISE_WARMUP_SECONDS = 90.0
NOISE_WARMUP_POLICY = "mod50_noise_fixed90_sample_clock_warmup_v1"


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
