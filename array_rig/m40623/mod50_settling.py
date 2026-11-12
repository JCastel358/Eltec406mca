"""Sample-clock settling evidence; scheduling only, never acceptance limits."""
from collections import deque

import numpy as np

POLICY = "mod50_80percent_five_1s_windows_mean_and_rms_v2"
WINDOW_SECONDS = 1.0
WINDOWS = 5
MAX_SECONDS = 90.0
# Carry forward the array's 0.1 mV scheduling tolerance, floored at two
# converter codes. Noise means include their finite-window uncertainty;
# the RMS envelope must also stabilize. These are provisional scheduling
# settings, not detector limits or a claim of TP120 timing qualification.
BASE_TOLERANCE_V = .0001
RMS_RELATIVE_TOLERANCE = .10
READY_FRACTION = .80


class SettlingMonitor:
    def __init__(self, rate, lsb, positions, mode):
        self.rate, self.mode = rate, mode
        self.positions = list(positions)
        self.window_size = max(1, round(rate * WINDOW_SECONDS))
        self.tolerance = max(BASE_TOLERANCE_V, 2 * lsb)
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
        return {"policy": POLICY, "complete": self.complete, "waited_s": self.samples / self.rate,
                "max_seconds": MAX_SECONDS, "window_seconds": WINDOW_SECONDS,
                "windows_required": WINDOWS, "base_tolerance_v": self.tolerance,
                "rms_relative_tolerance": RMS_RELATIVE_TOLERANCE,
                "ready_fraction": READY_FRACTION, "stable_count": len(stable),
                "required_count": int(np.ceil(READY_FRACTION * len(self.positions))),
                "positions": self.positions, "unstable_positions": [p for p in self.positions if p not in stable],
                "trace": list(self.trace), "clock": "sample_clock", "acceptance_limit": False}
