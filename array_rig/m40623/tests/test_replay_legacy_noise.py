"""Saved capture replay preserves acquisition evidence and original files."""

import csv
import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import legacy_noise as ln
import replay_legacy_noise as replay


def save_capture(path, *, actual_rate=100.0, omit=(), simulated=False):
    t = np.arange(6000)/100.0
    arrays = {
        "waveform_v": np.array([0.7+0.001*np.sin(2*math.pi*3*t), np.full(6000,0.7)], dtype=np.float32),
        "sample_rate_hz": 101.0, "actual_timer_hz": str(actual_rate),
        "positions": np.array(["1-1","1-2"]), "channels": np.array([0,1]),
        "occupancy": np.array(["LOADED","EMPTY"]), "sensor_numbers": np.array([1,0]),
        "range_code": "2", "oversample": "3", "drop_conversions": "1", "daq_serial": "TEST-DAQ",
        "simulated": str(simulated),
    }
    np.savez_compressed(path, **{key:value for key,value in arrays.items() if key not in omit})


class ReplayTests(unittest.TestCase):
    def test_waveform_viewer_metadata_is_supported_without_guessing_occupancy(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"waveform.npz"
            save_capture(source)
            with np.load(source, allow_pickle=False) as saved:
                arrays = {key: saved[key] for key in saved.files if key not in ("occupancy", "sensor_numbers")}
            arrays["drop_first"] = arrays.pop("drop_conversions")
            np.savez_compressed(source, **arrays)
            report = replay.replay_capture(source)
            self.assertEqual(report["acquisition"]["drop_first"], 1)
            self.assertEqual(report["results"][0]["verdict"], "OCCUPANCY_UNKNOWN")
            self.assertFalse(report["results"][0]["detector_decision_applicable"])

    def test_replays_actual_rate_and_preserves_empty_occupancy(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"raw.npz"
            save_capture(source)
            original = source.read_bytes()
            report = replay.replay_capture(source)
            self.assertEqual(report["sample_rate_hz"],100)
            self.assertEqual(report["acquisition"]["setup_id"],"TEST-DAQ:buffer-1x:drop-1")
            self.assertAlmostEqual(report["results"][0]["band_rms_mv"],1/math.sqrt(2),places=5)
            self.assertEqual(report["results"][0]["verdict"],"NO_LIMIT")
            self.assertEqual(report["results"][1]["verdict"],"EMPTY")
            self.assertFalse(report["results"][1]["detector_decision_applicable"])
            path = replay.write_replay_report(report, Path(folder)/"report.json")
            self.assertEqual(json.loads(path.read_text())["calibration_id"],"PENDING")
            self.assertEqual(source.read_bytes(),original)
            with self.assertRaises(FileExistsError):
                replay.write_replay_report(report,path)

    def test_csv_and_json_export_equivalent_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"raw.npz"
            save_capture(source)
            report = replay.replay_capture(source)
            path = replay.write_replay_report(report,Path(folder)/"report.csv")
            with path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows),2)
            self.assertAlmostEqual(float(rows[0]["band_rms_mv"]),report["results"][0]["band_rms_mv"])
            self.assertEqual(json.loads(rows[0]["acquisition"]),report["acquisition"])

    def test_missing_actual_rate_warns_but_missing_setup_metadata_rejects(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"raw.npz"
            save_capture(source,omit=("actual_timer_hz",))
            report = replay.replay_capture(source)
            self.assertEqual(report["sample_rate_hz"],101)
            self.assertFalse(report["actual_timer_recorded"])
            self.assertIn("nominal",report["warnings"][0])
            save_capture(source,omit=("oversample",))
            with self.assertRaisesRegex(ValueError,"oversample"):
                replay.replay_capture(source)

    def test_cli_requires_new_explicit_output_and_never_overwrites_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"raw.npz"
            output = Path(folder)/"report.json"
            save_capture(source)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(replay.main([str(source),"--output",str(output)]),0)
            with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
                replay.main([str(source),"--output",str(output)])
            self.assertEqual(error.exception.code,2)
            with self.assertRaises(ValueError):
                replay.write_replay_report(replay.replay_capture(source),source)

    def test_bad_actual_rate_rejected_and_simulation_marked(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"raw.npz"
            save_capture(source,actual_rate="nan")
            with self.assertRaises(ValueError):
                replay.replay_capture(source)
            save_capture(source,simulated=True)
            report = replay.replay_capture(source)
            self.assertTrue(report["simulated"])
            self.assertFalse(report["results"][0]["detector_decision_applicable"])

    def test_calibration_replay_requires_real_capture_with_actual_timer(self):
        calibration = ln.LegacyNoiseCalibration.from_dict({
            "schema_version":1, "method_id":ln.METHOD_ID, "calibration_id":"synthetic-test-only",
            "provenance":"Synthetic replay test; not real qualification", "metric":"band_rms_mv",
            "low_mv":0.2, "high_mv":1.0, "background_metric_mv":0.01,
            "minimum_resolvable_metric_mv":0.05,
            "acquisition":ln.LegacyNoiseConfig(setup_id="TEST-DAQ:buffer-1x:drop-1").acquisition_signature(100.0),
        })
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"raw.npz"
            save_capture(source)
            report = replay.replay_capture(source,calibration=calibration)
            self.assertEqual(report["results"][0]["verdict"],"PASS")
            self.assertEqual(report["results"][1]["verdict"],"EMPTY")
            save_capture(source,omit=("actual_timer_hz",))
            with self.assertRaisesRegex(ValueError,"actual_timer_hz"):
                replay.replay_capture(source,calibration=calibration)
            save_capture(source,simulated=True)
            with self.assertRaisesRegex(ValueError,"simulated"):
                replay.replay_capture(source,calibration=calibration)


if __name__ == "__main__":
    unittest.main()
