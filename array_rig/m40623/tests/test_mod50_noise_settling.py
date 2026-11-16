"""Noise startup uses baseline, clipping and drift; records remain simultaneous."""
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mod50_check as check
import mod50_hardware as hw
from mod50_settling import NoiseSettlingMonitor, NOISE_POLICY


class NoiseReadiness(unittest.TestCase):
    def monitor(self, count=1, **kwargs):
        return NoiseSettlingMonitor(1000, hw.daq.lsb_volts(hw.NOISE_RANGE), hw.POSITIONS[:count], **kwargs)

    def test_early_start_requires_five_full_windows_and_80_percent_rounds_up(self):
        for count, ready, expected in ((50, 40, True), (50, 39, False), (3, 2, False), (3, 3, True)):
            with self.subTest(count=count, ready=ready):
                monitor = self.monitor(count)
                values = np.zeros((count, 5000))
                values[ready:] = .020
                self.assertFalse(monitor.feed(values[:, :-1]))
                self.assertEqual(monitor.feed(values[:, -1:]), expected)
                evidence = monitor.evidence()
                self.assertEqual(evidence["stable_count"], ready)
                self.assertEqual(evidence["policy"], NOISE_POLICY)
                self.assertFalse(evidence["acceptance_limit"])

    def test_changing_noise_amplitude_does_not_require_stable_rms(self):
        monitor = self.monitor()
        for second in range(5):
            tone = (.003 + .01 * second) * np.sin(2 * np.pi * 20 * np.arange(1000) / 1000)
            monitor.feed(tone[None, :])
        self.assertTrue(monitor.complete)
        self.assertEqual(monitor.channels[0]["state"], "ready")

    def test_zero_offset_ramps_and_clipping_have_distinct_states(self):
        t = np.arange(5000) / 1000
        for values, expected in ((np.full(5000, .02), "above_zero"), (np.full(5000, -.02), "below_zero"),
                                 (.0006 * t, "rising"), (-.0006 * t, "falling"),
                                 (np.full(5000, .9), "clipped")):
            with self.subTest(state=expected):
                monitor = self.monitor()
                self.assertFalse(monitor.feed(values[None, :]))
                self.assertEqual(monitor.channels[0]["state"], expected)

    def test_coherent_ramp_is_not_hidden_by_large_raw_noise_amplitude(self):
        monitor = self.monitor()
        t = np.arange(5000) / 1000
        values = .0004 * t + .10 * np.sin(2 * np.pi * 20 * t)
        self.assertFalse(monitor.feed(values[None, :]))
        self.assertEqual(monitor.channels[0]["state"], "rising")
        self.assertTrue(monitor.channels[0]["trend_consistent"])
        self.assertTrue(monitor.channels[0]["trend_significant"])

    def test_measured_window_mean_reversals_are_noise_not_a_startup_ramp(self):
        # First five 1 s means measured on top 1-1, 2026-11-16. The old
        # 1 mV span + raw/sqrt(1000) rule never became ready in 170 s.
        means = np.array([-.7161153, -6.2374471, -3.4476929, -3.1573792, -8.2076009]) / 1000
        carrier = .006 * np.sin(2 * np.pi * 20 * np.arange(1000) / 1000)
        values = np.concatenate([m + carrier for m in means])[None, :]
        monitor = self.monitor()
        self.assertTrue(monitor.feed(values))
        self.assertEqual(monitor.channels[0]["state"], "ready")
        self.assertAlmostEqual(monitor.channels[0]["mean_v"], means.mean())
        self.assertEqual(monitor.evidence()["first_ready_s"], 5.)

    def test_centered_noise_may_cross_zero_band_in_one_window(self):
        monitor = self.monitor()
        values = np.repeat(np.array([-.012, .004, -.003, .006, -.004]), 1000)[None, :]
        self.assertTrue(monitor.feed(values))
        self.assertTrue(monitor.channels[0]["near_zero"])
        self.assertGreater(monitor.channels[0]["baseline_span_v"], .01)

    def test_monotonic_step_cannot_hide_in_fit_residual_variation(self):
        for sign in (1, -1):
            monitor = self.monitor()
            values = np.repeat(sign * np.array([0., 0., .008, .008, .008]), 1000)[None, :]
            self.assertFalse(monitor.feed(values))
            self.assertTrue(monitor.channels[0]["trend_consistent"])
            self.assertEqual(monitor.channels[0]["state"], "rising" if sign == 1 else "falling")
            self.assertTrue(monitor.feed(np.full((1, 5000), sign * .008)))

    def test_first_ready_survives_later_drift_within_same_delivery_block(self):
        monitor = self.monitor()
        values = np.concatenate([np.zeros(5000), .002 * np.arange(5000) / 1000])[None, :]
        self.assertFalse(monitor.feed(values, stop_when_ready=False))
        self.assertEqual(monitor.evidence()["first_ready_s"], 5.)
        self.assertEqual(monitor.channels[0]["state"], "rising")

    def test_retained_converter_clipping_blocks_even_when_mean_is_near_zero(self):
        monitor = self.monitor()
        values = np.zeros((1, 5000))
        clipping = np.zeros_like(values, dtype=bool)
        clipping[0, -1] = True
        self.assertFalse(monitor.feed(values, clipping))
        self.assertEqual(monitor.channels[0]["state"], "clipped")
        self.assertFalse(monitor.feed(np.zeros((1, 4000))))
        self.assertTrue(monitor.feed(np.zeros((1, 1000))))

    def test_callback_partition_does_not_change_recovery_or_bounded_live_history(self):
        t = np.arange(12000) / 1000
        values = (np.maximum(0., .03 - .005 * t) + .001 * np.sin(2 * np.pi * 20 * t))[None, :]
        traces = []
        for chunk in (137, 1000, 6000):
            monitor = self.monitor(trace_limit=12)
            for start in range(0, len(t), chunk):
                monitor.feed(values[:, start:start + chunk], stop_when_ready=False)
            traces.append(monitor.evidence()["trace"])
            self.assertTrue(monitor.complete)
            self.assertEqual(monitor.ever_stable, {"1-1"})
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(traces[0], traces[2])
        bounded = self.monitor(trace_limit=2)
        bounded.feed(np.zeros((1, 9000)), stop_when_ready=False)
        self.assertEqual(len(bounded.trace), 2)
        self.assertEqual(bounded.evidence()["waited_s"], 9)

    def test_nonfinite_or_mismatched_samples_refused(self):
        monitor = self.monitor()
        for values, clipping in ((np.array([[np.nan]]), None), (np.zeros((2, 1000)), None),
                                 (np.zeros((1, 1000)), np.zeros((1, 999), bool))):
            with self.assertRaises(ValueError):
                monitor.feed(values, clipping)


class NoiseRuns(unittest.TestCase):
    def test_fractional_clock_still_records_a_full_60seconds_for_legacy_readout(self):
        def factory(**kwargs):
            rig = hw.Mod50Rig(**kwargs)
            start = rig.device.start_stream
            def fractional_clock(**options):
                header = start(**options)
                rig.device._diagnostics.actual_timer_hz = 999.991
                return replace(header, actual_timer_hz=999.991)
            rig.device.start_stream = fractional_clock
            return rig
        with tempfile.TemporaryDirectory() as directory:
            worker = check.TrayCheck(lot="CLOCK", operator="Test", selected_tests=["noise"],
                installed_positions=["1-1"], simulate=True, output_root=directory, rig_factory=factory)
            worker.run()
            self.assertTrue(worker.result["data_complete"], worker.result["errors"])
            record = worker.result["records"][0]
            self.assertEqual(record["samples"], 60000)
            self.assertGreaterEqual(record["recorded_seconds"], 60.)
            self.assertEqual(worker.result["rows"][0]["legacy_equivalent_status"], "UNVALIDATED_ESTIMATE")

    def run_case(self, ready, *, fallback=False):
        class Baselines(hw.DemoDaq):
            def _synth(self, scans, scan_hz):
                self._advance(scans / scan_hz)
                start = getattr(self, "sample_number", 0)
                self.sample_number = start + scans
                t = np.arange(start, start + scans) / scan_hz
                out = np.zeros((scans, 64))
                out[:, hw.MAPPING] = (.003 * np.sin(2 * np.pi * 20 * t))[:, None]
                out[:, hw.MAPPING[ready:]] += .02
                return out
        with tempfile.TemporaryDirectory() as directory:
            worker = check.TrayCheck(lot="BASELINE", operator="Test", selected_tests=["noise"], simulate=True,
                output_root=directory, rig_factory=lambda **kw: hw.Mod50Rig(device=Baselines(real_time=False), **kw))
            with patch.object(check, "NOISE_SECONDS", 6.), patch.object(check, "SETTLING_MAX_SECONDS", 7.):
                worker.run()
            result = worker.result
            self.assertTrue(result["data_complete"], result["errors"])
            record = result["records"][0]
            self.assertEqual(record["settling"]["waited_s"], 7. if fallback else 5.)
            self.assertEqual(record["settling"].get("deadline_fallback", False), fallback)
            self.assertEqual(record["settling"]["stable_count"], ready)
            self.assertEqual(record["stability_observation"]["never_settled_positions"], list(hw.POSITIONS[ready:]))
            for row in result["rows"][ready:]:
                self.assertTrue(any("Baseline not ready" in flag for flag in row["flags"]))
                self.assertTrue(any("never settled" in flag for flag in row["flags"]))
            with np.load(worker.directory / "noise_raw.npz") as raw:
                self.assertEqual(raw["converter_counts"].shape, (6000, 57, 4))
                metadata = json.loads(str(raw["metadata_json"]))
                self.assertEqual(metadata["timing_policy"], check.TIMING_POLICY)
                self.assertEqual(metadata["settling"]["stable_count"], ready)
            self.assertTrue((worker.directory / record["settling"]["raw_file"]).exists())

    def test_80_percent_starts_all_channels_and_flags_remaining_baselines(self):
        self.run_case(40)

    def test_deadline_records_all_channels_with_readiness_warning(self):
        self.run_case(39, fallback=True)


if __name__ == "__main__":
    unittest.main()
