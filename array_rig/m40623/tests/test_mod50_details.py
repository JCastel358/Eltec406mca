"""Waveform evidence, conservative absence inference and offline report safety."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mod50_check as check
import mod50_hardware as hw


def raw_from_dc(values):
    values = np.asarray(values)
    codes = np.rint((values + 5) * 65536 / 10).clip(0, 65535).astype(np.uint16)
    return np.broadcast_to(codes[:, None, None], (len(codes), 57, 4)).copy()


class WaveformEvidence(unittest.TestCase):
    def test_reduction_retains_narrow_spikes_and_original_time(self):
        y = np.zeros(10000)
        y[237], y[7998] = 2., -3.
        out = check.display_trace(y, 1000, first_sample=5000, max_points=400)
        self.assertLessEqual(len(out["volts"]), 400)
        self.assertIn(2., out["volts"])
        self.assertIn(-3., out["volts"])
        self.assertIn(5.237, out["time_s"])
        self.assertEqual(out["time_s"][-1], 14.999)
        self.assertTrue(all(a < b for a, b in zip(out["time_s"], out["time_s"][1:])))

    def test_dc_slope_and_tentative_projection(self):
        t = np.arange(10000) / 1000
        y = .1 + .01 * t
        detail = check.dc_detail(y, 1000)
        self.assertAlmostEqual(detail["slope_v_per_s"], .01)
        self.assertEqual(detail["trend"], "rising")
        self.assertGreater(detail["estimated_seconds_to_window"], 10.)
        self.assertLess(detail["estimated_seconds_to_window"], 11.)
        self.assertEqual(detail["min_v"], float(y.min()))
        self.assertEqual(detail["max_v"], float(y.max()))
        self.assertIsNone(check.dc_detail(.2 - .01 * t, 1000)["estimated_seconds_to_window"])
        self.assertIsNone(check.dc_detail(np.full(10000, .7), 1000)["estimated_seconds_to_window"])

    def test_noise_excerpt_targets_first_violating_sample(self):
        y = np.zeros(100000)
        y[81003] = .91
        clip = np.zeros(100000, dtype=bool)
        detail = check.noise_detail(y, clip, 1000)
        self.assertEqual(detail["event_time_s"], 81.003)
        self.assertIn(.91, detail["volts"])
        self.assertIn(81.003, detail["time_s"])
        self.assertGreater(detail["start_s"], 78.)
        self.assertLess(detail["end_s"] - detail["start_s"], 4.)
        self.assertIn("violation", detail["selection_reason"])

    def test_representative_excerpt_and_partial_record_tail(self):
        y = np.sin(np.arange(10000) / 1000) * .01
        detail = check.noise_detail(y, np.zeros(len(y), bool), 1000)
        self.assertAlmostEqual(detail["start_s"], 3.)
        self.assertIn("Representative", detail["selection_reason"])
        partial = check.noise_detail(y, np.zeros(len(y), bool), 1000, valid_data=False)
        self.assertEqual(partial["end_s"], 9.999)
        self.assertIn("incomplete", partial["selection_reason"])


class PresenceEvidence(unittest.TestCase):
    def test_zero_in_two_separated_windows_is_no_signal_not_dead_diagnosis(self):
        state, evidence = check.presence_evidence(np.zeros(10000), 1000, hw.DC_RANGE,
                                                 valid_data=True, clipped=False)
        self.assertEqual(state, "NO_SIGNAL")
        self.assertEqual(len(evidence["windows"]), 2)
        self.assertEqual(evidence["windows"][1]["start_s"], 9.)
        self.assertEqual(evidence["threshold_v"], hw.daq.lsb_volts(hw.DC_RANGE))

    def test_invalid_or_short_or_oscillating_data_never_infers_empty(self):
        zeros = np.zeros(10000)
        for valid, clipped in ((False, False), (True, True)):
            state, _ = check.presence_evidence(zeros, 1000, hw.DC_RANGE, valid_data=valid, clipped=clipped)
            self.assertEqual(state, "UNKNOWN")
        state, _ = check.presence_evidence(zeros[:1000], 1000, hw.DC_RANGE, valid_data=True, clipped=False)
        self.assertEqual(state, "UNKNOWN")
        oscillation = .01 * np.sin(2 * np.pi * np.arange(10000) / 1000)
        state, _ = check.presence_evidence(oscillation, 1000, hw.DC_RANGE, valid_data=True, clipped=False)
        self.assertNotEqual(state, "NO_SIGNAL")

    def test_dc_waking_up_by_last_window_is_retained(self):
        y = np.zeros(10000)
        y[-1000:] = .7
        state, _ = check.presence_evidence(y, 1000, hw.DC_RANGE, valid_data=True, clipped=False)
        self.assertEqual(state, "SIGNAL")

    def test_no_signal_has_clickable_dc_evidence_and_no_noise_verdict(self):
        dc = {"mode": "dc", "valid_data": True,
              "rows": check.channel_statistics(raw_from_dc(np.zeros(10000)), hw.DC_RANGE, 1000, "dc")}
        rows = check.combine_rows([dc], hw.POSITIONS, False)
        self.assertTrue(all(row["presence"] == "NO_SIGNAL" for row in rows))
        self.assertTrue(all(row["result"] == "NO_SIGNAL" for row in rows))
        self.assertTrue(rows[0]["dc_detail"]["time_s"])
        self.assertTrue(rows[0]["noise_analysis_excluded"])
        self.assertIn("may be dead", rows[0]["flags"][-1])
        dc["valid_data"] = False
        invalid = check.combine_rows([dc], hw.POSITIONS, False)
        self.assertEqual(invalid[0]["presence"], "UNKNOWN")
        self.assertFalse(invalid[0]["dc_detail"]["valid_acquisition"])

    def test_worker_publishes_immutable_offset_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = check.TrayCheck(lot="TEST", operator="TEST", simulate=True, output_root=directory)
            snapshot = worker.status()
            snapshot["rows"][0]["position"] = "changed"
            self.assertEqual(worker.status()["rows"][0]["position"], "1-1")

    def test_noise_exclusion_retains_waveform_but_does_not_run_fft(self):
        raw = np.full((4000, 57, 4), 32768, dtype=np.uint16)
        raw[900, :, :] = 65535
        with patch.object(np.fft, "rfft", side_effect=AssertionError("Excluded channel analyzed")):
            rows = check.channel_statistics(raw, hw.NOISE_RANGE, 1000, "noise", excluded_positions=hw.POSITIONS)
        self.assertIsNone(rows[0]["daq_1_100_hz_rms_uv"])
        self.assertTrue(rows[0]["noise_detail"]["time_s"])
        self.assertTrue(rows[0]["near_range_limit"])
        self.assertTrue(rows[0]["frontend_headroom_exceeded"])
        self.assertIn("Outside +/-0.8 V noise headroom", rows[0]["flags"])


class OfflineReport(unittest.TestCase):
    def test_report_is_self_contained_and_script_data_cannot_break_out(self):
        payload = {"lot": "</script><script>alert(1)</script>", "rows": []}
        html = check.interactive_report(payload)
        self.assertIn("window.ELTEC_REPORT", html)
        self.assertNotIn('src="app.js"', html)
        self.assertNotIn('href="styles.css"', html)
        self.assertIn("data:image/webp;base64,", html)
        self.assertIn("\\u003c/script>", html)
        self.assertNotIn(payload["lot"], html)


if __name__ == "__main__":
    unittest.main()
