"""Signal, resolution and acceptance regressions for the 40623 3 Hz method."""

from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import legacy_noise as ln


RATE = 100.0
CONFIG = ln.LegacyNoiseConfig(setup_id="test-buffer-board")


def waveform(frequency=3.0, amplitude=0.001, duration=60.0, offset=0.7):
    t = np.arange(round(RATE*duration)) / RATE
    return offset + amplitude * np.sin(2*math.pi*frequency*t)


def calibration_data():
    return {
        "schema_version": 1, "method_id": ln.METHOD_ID,
        "calibration_id": "synthetic-test-only", "provenance": "Synthetic regression, not real qualification",
        "metric": "band_rms_mv", "low_mv": 0.2, "high_mv": 1.0,
        "background_metric_mv": 0.02, "minimum_resolvable_metric_mv": 0.05,
        "acquisition": CONFIG.acquisition_signature(RATE),
    }


def analyze(raw, *, config=CONFIG, calibration=None):
    return ln.analyze_legacy_noise(raw, RATE, positions=["1-1"] if np.ndim(raw)==1 else [f"1-{i+1}" for i in range(len(raw))],
                                   config=config, calibration=calibration)


class FilterEvidenceTests(unittest.TestCase):
    def test_centre_rms_and_rectified_magnitude_preserve_signal(self):
        results, trace, rate = analyze(waveform())
        result = results[0]
        self.assertEqual(rate, RATE)
        self.assertEqual(trace.shape, (1, 6000))
        self.assertAlmostEqual(result.band_rms_mv, 1/math.sqrt(2), places=5)
        self.assertAlmostEqual(result.band_mean_abs_mv, 2/math.pi, delta=0.001)
        self.assertAlmostEqual(result.smoothed_abs_final_mv, 2/math.pi, delta=0.004)
        self.assertLess(abs(trace[0, 500:].mean()), 1e-8)
        self.assertEqual(result.verdict, "NO_LIMIT")
        self.assertEqual(result.quality_status, "UNQUALIFIED")
        self.assertEqual(result.calibration_id, "PENDING")

    def test_bandpass_rejects_slow_drift_and_fast_interference(self):
        centre = analyze(waveform())[0][0].band_rms_mv
        for frequency in (0.1, 20.0):
            measured = analyze(waveform(frequency=frequency))[0][0].band_rms_mv
            self.assertLess(measured, centre * 0.06)

    def test_bilinear_response_at_centre_and_analogue_band_edges(self):
        b, a = ln.bandpass_coefficients(1000.0)
        edges = [(math.sqrt(37)-1)/2, 3.0, (math.sqrt(37)+1)/2]
        expected = [1/math.sqrt(2), 1.0, 1/math.sqrt(2)]
        for frequency, target in zip(edges, expected):
            powers = np.exp(-2j*math.pi*frequency/1000.0*np.arange(3))
            self.assertAlmostEqual(abs(np.dot(b,powers)/np.dot(a,powers)), target, delta=0.0001)

    def test_constant_offset_never_appears_as_valid_noise(self):
        result = analyze(np.full(6000, 0.7))[0][0]
        self.assertEqual(result.band_rms_mv, 0.0)
        self.assertEqual(result.verdict, "NOT_MEASURED")
        self.assertIn("constant", " ".join(result.quality_reasons))

    def test_input_gain_only_changes_detector_referred_values(self):
        baseline = analyze(waveform())[0][0]
        amplified = analyze(waveform(), config=ln.LegacyNoiseConfig(input_gain=10))[0][0]
        self.assertAlmostEqual(amplified.band_rms_mv, baseline.band_rms_mv/10)
        self.assertEqual(amplified.adc_lsb_mv, baseline.adc_lsb_mv)
        self.assertAlmostEqual(amplified.adc_lsb_mv, 0.0762939453125)

    def test_50_channels_at_actual_rate_remain_independent(self):
        t = np.arange(60000)/1000.0
        amplitudes = np.linspace(0.0001, 0.002, 50)
        raw = 0.7 + amplitudes[:, None]*np.sin(2*math.pi*3*t)
        results, _, _ = ln.analyze_legacy_noise(raw, 1000.0, positions=[str(i) for i in range(50)])
        np.testing.assert_allclose([r.band_rms_mv for r in results], amplitudes*1000/math.sqrt(2), rtol=1e-6)


class NoiseDecisionTests(unittest.TestCase):
    def setUp(self):
        self.calibration = ln.LegacyNoiseCalibration.from_dict(calibration_data())

    def test_explicit_calibrated_limits_allow_low_pass_and_high(self):
        for rms_mv, expected in ((0.1,"LOW"), (0.5,"PASS"), (1.5,"HIGH")):
            result = analyze(waveform(amplitude=rms_mv*math.sqrt(2)/1000), calibration=self.calibration)[0][0]
            self.assertEqual(result.verdict, expected)
            self.assertEqual(result.quality_status, "QUALIFIED")

    def test_instrument_floor_cannot_be_called_low_detector_noise(self):
        result = analyze(waveform(amplitude=0.01*math.sqrt(2)/1000), calibration=self.calibration)[0][0]
        self.assertEqual(result.verdict, "NOT_MEASURED")
        self.assertIn("resolution floor", " ".join(result.quality_reasons))

    def test_short_engineering_capture_reports_metric_without_acceptance(self):
        result = analyze(waveform(duration=20), calibration=self.calibration)[0][0]
        self.assertGreater(result.band_rms_mv, 0.5)
        self.assertEqual(result.verdict, "NOT_MEASURED")
        self.assertIn("at least 60 s", " ".join(result.quality_reasons))

    def test_both_rails_block_noise_verdict(self):
        for clipped_value in (0.0, 5.0):
            raw = waveform()
            raw[-50] = clipped_value
            result = analyze(raw, calibration=self.calibration)[0][0]
            self.assertEqual(result.verdict, "NOT_MEASURED")
            self.assertEqual(result.clipped_samples, 1)

    def test_nominal_chain_gain_never_enables_legacy_acceptance(self):
        result = analyze(waveform(amplitude=4e-6))[0][0]
        self.assertGreater(result.nominal_meter_estimate_mv, ln.LEGACY_METER_LOW_MV)
        self.assertLess(result.nominal_meter_estimate_mv, ln.LEGACY_METER_HIGH_MV)
        self.assertEqual(result.verdict, "NO_LIMIT")
        self.assertIn("not an acceptance", " ".join(result.warnings))

    def test_mismatched_acquisition_cannot_reuse_calibration(self):
        for changed in ({"input_gain":2}, {"oversample_extra":2}, {"drop_first":0}, {"setup_id":"other-board"}):
            config = ln.LegacyNoiseConfig(**{**ln.asdict(CONFIG), **changed})
            result = analyze(waveform(), config=config, calibration=self.calibration)[0][0]
            self.assertEqual(result.verdict, "NOT_MEASURED")
            self.assertIn("do not match", " ".join(result.quality_reasons))


class CalibrationRecordTests(unittest.TestCase):
    def test_json_roundtrip_and_immutable_acquisition(self):
        data = calibration_data()
        calibration = ln.LegacyNoiseCalibration.from_dict(data)
        data["acquisition"]["input_gain"] = 400
        self.assertEqual(calibration.as_dict()["acquisition"]["input_gain"], 1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"calibration.json"
            path.write_text(json.dumps(calibration.as_dict()), encoding="utf-8")
            loaded = ln.load_noise_calibration(path)
        self.assertEqual(loaded, calibration)

    def test_bad_or_incomplete_records_fail_loudly(self):
        modifications = (
            {"schema_version":2}, {"method_id":"old-method"}, {"metric":"pkpk_mv"},
            {"calibration_id":"PENDING"}, {"provenance":""}, {"high_mv":0.1},
            {"background_metric_mv":0.1}, {"minimum_resolvable_metric_mv":0.3},
        )
        for change in modifications:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    ln.LegacyNoiseCalibration.from_dict({**calibration_data(), **change})
        bad = calibration_data()
        del bad["acquisition"]["oversample_extra"]
        with self.assertRaises(ValueError):
            ln.LegacyNoiseCalibration.from_dict(bad)

    def test_missing_or_unqualified_analogue_setup_is_rejected(self):
        bad = copy.deepcopy(calibration_data())
        bad["acquisition"]["setup_id"] = "UNQUALIFIED"
        with self.assertRaises(ValueError):
            ln.LegacyNoiseCalibration.from_dict(bad)

    def test_invalid_samples_rates_and_shapes_are_rejected(self):
        for raw in (np.array([0.7, np.nan]), np.array([0.7, np.inf]), np.array([0.7])):
            with self.assertRaises(ValueError):
                analyze(raw)
        for rate in (0, 6, 19, float("nan")):
            with self.assertRaises(ValueError):
                ln.bandpass_coefficients(rate)


if __name__ == "__main__":
    unittest.main()
