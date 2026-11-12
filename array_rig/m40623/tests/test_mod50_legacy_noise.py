"""Physical-scale, time-response and no-verdict checks for the nominal meter."""
import csv
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mod50_check as check
import mod50_hardware as hw
import mod50_legacy_noise as meter


class NominalMeter(unittest.TestCase):
    def test_sine_scale_rectification_and_frequency_selectivity(self):
        rate = 1000.
        t = np.arange(round(100 * rate)) / rate
        frequencies = np.array([1., 3., 10.])
        amplitude = .002  # DAQ peak volts, already amplified by the M25
        results = meter.analyze(amplitude * np.sin(2 * np.pi * frequencies[:, None] * t), rate)
        for frequency, row in zip(frequencies, results):
            # Independent steady-state formula: full-wave sine mean = 2A/pi;
            # band-pass magnitude, with the bilinear frequency substitution.
            ratio = math.tan(math.pi * frequency / rate) / math.tan(math.pi * 3 / rate)
            magnitude = (ratio / 3) / math.sqrt((1 - ratio**2)**2 + (ratio / 3)**2)
            expected_mv = 1000 * 10 * 2 * amplitude / math.pi * magnitude
            self.assertAlmostEqual(row["legacy_equivalent_mv"], expected_mv, delta=expected_mv * .015)
            self.assertEqual(row["legacy_equivalent_status"], "UNVALIDATED_ESTIMATE")
        # x1000 has already happened in hardware. A second x1000 here would
        # produce ~12732 mV instead of the correct nominal ~12.7 mV.
        self.assertGreater(results[1]["legacy_equivalent_mv"], 12.6)
        self.assertLess(results[1]["legacy_equivalent_mv"], 12.9)

    def test_dc_and_polarity_invariance_and_linear_amplitude(self):
        rate = 200.
        t = np.arange(20000) / rate
        tone = .002 * np.sin(2 * np.pi * 3 * t + .7)
        rows = meter.analyze(np.array([tone, -tone + .37, 2 * tone - .12]), rate)
        for key in meter.VALUE_FIELDS[:3]:
            self.assertAlmostEqual(rows[0][key], rows[1][key], places=10)
            self.assertAlmostEqual(2 * rows[0][key], rows[2][key], places=10)

    def test_hold_keeps_burst_while_smoother_decays_and_new_record_resets(self):
        rate = 200.
        t = np.arange(20000) / rate
        burst = .01 * np.sin(2 * np.pi * 3 * t) * ((t >= 5) & (t < 25))
        row = meter.analyze(burst, rate)[0]
        detail = row["legacy_equivalent_detail"]
        self.assertGreater(row["legacy_equivalent_mv"], 40)
        self.assertEqual(row["legacy_equivalent_mv"], row["legacy_equivalent_60s_mv"])
        self.assertLess(row["legacy_smoothed_final_mv"], row["legacy_equivalent_mv"] * .002)
        self.assertTrue(np.all(np.diff(detail["held_mv"]) >= 0))
        self.assertTrue(np.all(np.array(detail["held_mv"]) >= detail["smoothed_mv"]))
        # With the input/filter settled to zero, the smoother follows exp(-t/10).
        times = np.array(detail["time_s"])
        at50, at70 = [np.argmin(abs(times - value)) for value in (50, 70)]
        expected = math.exp(-(times[at70] - times[at50]) / 10)
        self.assertAlmostEqual(detail["smoothed_mv"][at70] / detail["smoothed_mv"][at50], expected, places=8)
        self.assertEqual(meter.analyze(np.zeros(20000), rate)[0]["legacy_equivalent_mv"], 0)

    def test_60_second_readout_excludes_later_burst_and_hold_stops_at_100(self):
        rate = 100.
        t = np.arange(12000) / rate
        tone = .001 * np.sin(2 * np.pi * 3 * t)
        y = tone * np.where(t < 70, 1, np.where(t < 100, 5, 50))
        row = meter.analyze(y, rate)[0]
        self.assertLess(row["legacy_equivalent_60s_mv"], 7)
        self.assertGreater(row["legacy_equivalent_mv"], 25)
        self.assertLess(row["legacy_equivalent_mv"], 33)
        self.assertEqual(row["legacy_equivalent_detail"]["readout_elapsed_s"], 100)
        self.assertEqual(row["legacy_equivalent_detail"]["reference_readout_elapsed_s"], 60)
        self.assertEqual(row["legacy_equivalent_detail"]["recorded_seconds"], 120)

    def test_actual_noninteger_rate_and_minimum_record_length(self):
        rate = 99.987
        n60 = math.ceil(rate * 60)
        short = meter.analyze(np.zeros(n60 - 1), rate)[0]
        self.assertEqual(short["legacy_equivalent_status"], "TOO_SHORT")
        self.assertIsNone(short["legacy_equivalent_mv"])
        row = meter.analyze(np.zeros(n60), rate)[0]
        elapsed = row["legacy_equivalent_detail"]["reference_readout_elapsed_s"]
        self.assertGreaterEqual(elapsed, 60)
        self.assertLess(elapsed, 60 + 1 / rate)

    def test_nonfinite_or_malformed_data_and_invalid_clocks_are_rejected(self):
        for values in ([0, np.nan], [0, np.inf], [], np.zeros((2, 0)), np.zeros((2, 3, 4))):
            with self.assertRaises(ValueError):
                meter.analyze(values, 1000)
        for rate in (0, -1, 10, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                meter.analyze([0, 1], rate)

    def test_metadata_never_claims_calibration_or_invents_hold_decay(self):
        method = meter.method_metadata(1000)
        self.assertEqual(method["validation_status"], "UNVALIDATED")
        self.assertFalse(method["noise_pass_fail_enabled"])
        self.assertIsNone(method["acceptance_limits_mv"])
        self.assertIsNone(method["hold_decay_tau_s"])
        self.assertEqual(method["nominal_gain_ratio"], 1)
        self.assertEqual(method["filter"]["band_truncation"], None)
        self.assertEqual(len(method["filter"]["a"]), 3)
        json.dumps(method, allow_nan=False)


class MeterIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rate = 200.
        t = np.arange(20000) / cls.rate
        cls.positions = ("1-1", "3-6", "5-10")
        cls.excluded = set(hw.POSITIONS) - set(cls.positions)
        cls.raw = np.full((len(t), 57, 4), 32768, dtype=np.uint16)
        cls.raw[:, :, 0] = 65535  # First conversion must still be discarded.
        for position, amplitude in zip(cls.positions, (.0007, .0025, .015)):
            physical = hw.MAPPING[hw.POSITIONS.index(position)]
            codes = np.rint((1 + amplitude * np.sin(2 * np.pi * 3 * t)) * 65536 / 2).astype(np.uint16)
            cls.raw[:, physical, 1:] = codes[:, None]
        cls.measured = check.channel_statistics(cls.raw, hw.NOISE_RANGE, cls.rate, "noise",
                                                excluded_positions=cls.excluded)

    def test_mapped_channels_keep_rms_and_never_apply_old_meter_limits(self):
        record = {"mode": "noise", "valid_data": True, "rows": self.measured}
        rows = check.combine_rows([record], self.positions, True, ("noise",))
        observed = [rows[hw.POSITIONS.index(p)] for p in self.positions]
        self.assertLess(observed[0]["legacy_equivalent_mv"], 10)
        self.assertTrue(10 < observed[1]["legacy_equivalent_mv"] < 37.9)
        self.assertGreater(observed[2]["legacy_equivalent_mv"], 37.9)
        for row, amplitude in zip(observed, (.0007, .0025, .015)):
            self.assertEqual(row["result"], "REVIEW_NO_NOISE_LIMIT")
            self.assertEqual(row["flags"], [])
            expected_rms_uv = amplitude / math.sqrt(2) * 1e6
            self.assertAlmostEqual(row["daq_3hz_q3_rms_uv"], expected_rms_uv, delta=5)
            self.assertAlmostEqual(row["daq_1_100_hz_rms_uv"], expected_rms_uv, delta=5)
        self.assertIsNone(rows[1]["legacy_equivalent_mv"])
        self.assertEqual(rows[1]["result"], "NOT_INSTALLED")

    def test_invalid_and_excluded_signals_never_enter_meter_analysis(self):
        with patch.object(meter, "analyze", side_effect=AssertionError("Invalid meter analysis")):
            rows = check.channel_statistics(self.raw, hw.NOISE_RANGE, self.rate, "noise",
                                            valid_data=False, excluded_positions=self.excluded)
            self.assertIsNone(rows[0]["legacy_equivalent_mv"])
            self.assertEqual(rows[0]["legacy_equivalent_status"], "INVALID_ACQUISITION")
            raw = self.raw.copy()
            raw[:, hw.MAPPING[0], 1:] = 32768  # flat
            raw[0, hw.MAPPING[25], 1:] = 65535  # clipped
            raw[0, hw.MAPPING[49], 1:] = 62500  # headroom
            rows = check.channel_statistics(raw, hw.NOISE_RANGE, self.rate, "noise", excluded_positions=self.excluded)
            for i in (0, 25, 49):
                self.assertEqual(rows[i]["legacy_equivalent_status"], "INVALID_SIGNAL")
            self.assertEqual(rows[1]["legacy_equivalent_status"], "EXCLUDED")

    def test_incomplete_or_no_signal_results_cannot_publish_estimates(self):
        record = {"mode": "noise", "valid_data": False, "rows": self.measured, "errors": ["interrupted"]}
        row = check.combine_rows([record], self.positions, False, ("noise",))[0]
        self.assertIsNone(row["legacy_equivalent_mv"])
        self.assertEqual(row["result"], "INCOMPLETE")
        dc = [{"mean_v": 0, "near_range_limit": False, "flags": [], "presence": "NO_SIGNAL"} for _ in hw.POSITIONS]
        rows = check.combine_rows([{"mode": "dc", "valid_data": True, "rows": dc},
                                   {**record, "valid_data": True}], self.positions, True)
        self.assertEqual(rows[0]["result"], "NO_SIGNAL")
        self.assertIsNone(rows[0]["legacy_equivalent_mv"])
        self.assertEqual(rows[0]["legacy_equivalent_status"], "EXCLUDED")

    def test_exports_preserve_scale_method_timing_and_disabled_acceptance(self):
        rows = check.combine_rows([{"mode": "noise", "valid_data": True, "rows": self.measured}],
                                  self.positions, True, ("noise",))
        result = {"rows": rows, "lot": "MODEL TEST", "tray": 1, "operator": "Test", "simulated": True,
                  "selected_tests": ["noise"], "noise_acceptance_limit": None,
                  "legacy_noise_method": meter.method_metadata()}
        with tempfile.TemporaryDirectory() as tmp:
            check.write_report(tmp, result)
            saved = json.loads((Path(tmp) / "summary.json").read_text())
            self.assertEqual(saved["rows"][0]["legacy_equivalent_mv"], rows[0]["legacy_equivalent_mv"])
            self.assertFalse(saved["legacy_noise_method"]["noise_pass_fail_enabled"])
            with (Path(tmp) / "results.csv").open(encoding="utf-8-sig", newline="") as stream:
                csv_row = next(csv.DictReader(stream))
            self.assertEqual(float(csv_row["legacy_equivalent_mv"]), rows[0]["legacy_equivalent_mv"])
            self.assertEqual(csv_row["legacy_equivalent_method_id"], meter.METHOD_ID)
            self.assertEqual(float(csv_row["legacy_reference_readout_elapsed_s"]), 60)
            self.assertFalse(json.loads(csv_row["legacy_noise_method"])["noise_pass_fail_enabled"])
            self.assertEqual(csv_row["result"], "REVIEW_NO_NOISE_LIMIT")
            html = (Path(tmp) / "report.html").read_text(encoding="utf-8")
            self.assertIn("Legacy-equivalent estimate", html)
            self.assertIn("Disabled · compare old fixture", html)


if __name__ == "__main__":
    unittest.main()
