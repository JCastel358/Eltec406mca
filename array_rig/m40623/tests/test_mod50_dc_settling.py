"""Small measured DC trends settle; larger drift and offset faults still flag."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mod50_check as check
import mod50_hardware as hw
from mod50_settling import SettlingMonitor, POLICY


def small_dc_trend(t):
    # Representative of measured bottom 9-6, run 20261112_142519_46c0ba4f:
    # 0.702447 V mean, 0.0923 mV/s slope, roughly 0.22 mV window RMS.
    return .702 + .0000923 * t + .00031 * np.sin(2 * np.pi * 50 * t)


class DcSettlingTolerance(unittest.TestCase):
    def monitor(self):
        return SettlingMonitor(1000, hw.daq.lsb_volts(hw.DC_RANGE), ["4-6"], "dc")

    def test_small_recorded_trend_settles_independently_of_callback_size(self):
        values = small_dc_trend(np.arange(10000) / 1000)[None, :]
        traces = []
        for chunk_size in (137, 1000, 6000):
            with self.subTest(chunk_size=chunk_size):
                monitor = self.monitor()
                for start in range(0, 10000, chunk_size):
                    monitor.feed(values[:, start:start + chunk_size], stop_when_ready=False)
                evidence = monitor.evidence()
                self.assertTrue(evidence["complete"])
                self.assertEqual(monitor.ever_stable, {"4-6"})
                self.assertEqual(evidence["trace"][4]["stable_positions"], ["4-6"])
                self.assertEqual(evidence["policy"], POLICY)
                self.assertEqual(evidence["base_tolerance_v"], .001)
                self.assertFalse(evidence["acceptance_limit"])
                traces.append(evidence["trace"])
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(traces[0], traces[2])

    def test_half_millivolt_per_second_drift_still_never_settles(self):
        monitor = self.monitor()
        t = np.arange(10000) / 1000
        monitor.feed((.7 + .0005 * t)[None, :], stop_when_ready=False)
        self.assertFalse(monitor.complete)
        self.assertEqual(monitor.ever_stable, set())

    def test_repeatable_average_does_not_hide_large_within_window_ramp(self):
        monitor = self.monitor()
        ramp = .7 + .01 * np.arange(1000) / 1000
        monitor.feed(np.tile(ramp, 10)[None, :], stop_when_ready=False)
        self.assertFalse(monitor.complete)
        self.assertEqual(monitor.ever_stable, set())

    def test_millivolt_jumps_between_windows_still_do_not_settle(self):
        monitor = self.monitor()
        levels = np.array([.7, .703] * 5)
        monitor.feed(np.repeat(levels, 1000)[None, :], stop_when_ready=False)
        self.assertFalse(monitor.complete)
        self.assertEqual(monitor.ever_stable, set())

    def test_simulated_capture_saves_tolerance_and_preserves_offset_limits(self):
        class SmallTrendDaq(hw.DemoDaq):
            def _synth(self, scans, scan_hz):
                self._advance(scans / scan_hz)
                start = getattr(self, "sample_number", 0)
                self.sample_number = start + scans
                t = np.arange(start, start + scans) / scan_hz
                out = np.zeros((scans, 64))
                out[:, hw.MAPPING[hw.POSITIONS.index("4-6")]] = small_dc_trend(t)
                out[:, hw.MAPPING[hw.POSITIONS.index("4-7")]] = .2 + .00031 * np.sin(2 * np.pi * 50 * t)
                return out

        def factory(**kwargs):
            return hw.Mod50Rig(device=SmallTrendDaq(real_time=False), **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            worker = check.TrayCheck(lot="DC_TOLERANCE", operator="Test", box_side="bottom",
                                    installed_positions=["4-6", "4-7"], selected_tests=["dc"],
                                    simulate=True, output_root=directory, rig_factory=factory)
            worker.run()
            result = worker.result
            self.assertTrue(result["data_complete"], result["errors"])
            record = result["records"][0]
            self.assertEqual(record["settling"]["waited_s"], 5.)
            self.assertEqual(record["stability_observation"]["never_settled_positions"], [])
            rows = {row["detector_position"]: row for row in result["rows"]}
            self.assertEqual(rows["9-6"]["result"], "PASS_OFFSET_ONLY")
            self.assertEqual(rows["9-6"]["flags"], [])
            self.assertEqual(rows["9-7"]["dc_check"], "OUTSIDE_WINDOW_PROVISIONAL")
            self.assertEqual(rows["9-7"]["result"], "CHECK")
            self.assertIn("DC: outside provisional 0.3-1.2 V window", rows["9-7"]["flags"])
            with np.load(worker.directory / "dc_raw.npz", allow_pickle=False) as raw:
                self.assertEqual(raw["converter_counts"].shape, (10000, 57, 4))
                metadata = json.loads(str(raw["metadata_json"]))
            for key in ("settling", "stability_observation"):
                self.assertEqual(metadata[key]["policy"], POLICY)
                self.assertEqual(metadata[key]["base_tolerance_v"], .001)
            saved = json.loads((worker.directory / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["records"][0]["stability_observation"], metadata["stability_observation"])


if __name__ == "__main__":
    unittest.main()
