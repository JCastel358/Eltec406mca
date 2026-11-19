"""Continuous preview ownership, shared settling, and bounded display evidence."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mod50_hardware as hw
import mod50_live as live
from test_daq_backend import FakeClock


class Envelope(unittest.TestCase):
    def test_failed_ready_connection_retains_battery_guidance_evidence(self):
        device = hw.DemoDaq(real_time=False)
        device.status_override = 0x24
        worker = live.LiveNoiseViewer(simulate=True, installed_positions=["1-1"],
            rig_factory=lambda **kw: hw.Mod50Rig(device=device, **kw), pace_demo=False)
        worker.run()
        self.assertTrue(worker.result["power_readiness_failed"])
        self.assertFalse(worker.result["restart_required"])
        self.assertIn("READY inactive", worker.result["errors"][0])
        self.assertFalse(device.is_streaming)

    def test_failed_connection_keeps_readiness_error_without_false_shutdown_fault(self):
        class StrictClosedDevice(hw.DemoDaq):
            status_override = 255

            def write_r4_controls(self, *args):
                if self.info is None:
                    raise hw.daq.DaqError("Not connected - call connect() first.")
                return super().write_r4_controls(*args)

        device = StrictClosedDevice(real_time=False)
        worker = live.LiveNoiseViewer(simulate=True, installed_positions=["1-1"],
            rig_factory=lambda **kw: hw.Mod50Rig(device=device, **kw), pace_demo=False)
        worker.run()
        self.assertFalse(worker.result["restart_required"])
        self.assertEqual(len(worker.result["errors"]), 1)
        self.assertIn("UPPER card not detected", worker.result["errors"][0])
        self.assertFalse(worker.result["power_readiness_failed"])
        self.assertNotIn("shutdown", worker.result["errors"][0])
        self.assertIsNone(device.info)

    def test_brief_peak_is_retained_and_points_stay_bounded_and_ordered(self):
        signal = np.zeros(10000)
        signal[4567], signal[9876] = .9, -.8
        trace = live.waveform_envelope(signal, 1000., 20000)
        self.assertLessEqual(len(trace["time_s"]), live.TRACE_POINTS)
        self.assertEqual(trace["time_s"], sorted(trace["time_s"]))
        self.assertIn(.9, trace["voltage_v"])
        self.assertIn(-.8, trace["voltage_v"])
        self.assertGreaterEqual(min(trace["time_s"]), 20.)
        self.assertLess(max(trace["time_s"]), 30.)


class LiveWorker(unittest.TestCase):
    def make_worker(self, *, seconds=7, baseline=0., clipped=False, fault=None, positions=("1-1",)):
        clock = FakeClock()
        owner = self
        class Device(hw.DemoDaq):
            def _synth(self, scans, scan_hz):
                self._advance(scans / scan_hz)
                values = np.full((scans, 64), baseline)
                # The last detector checks the split M25 physical map.
                values[:, 56] = .002
                return values

            def read_stream(self, **kwargs):
                raw = super().read_stream(**kwargs)
                if clipped:
                    raw[:, 0, 2] = 0
                if fault == "readback" and self._stream_scans >= 2000:
                    self.status_override = 255
                if fault == "delivery" and self._stream_scans >= 2000:
                    self._diagnostics.ended_early = True
                if self._stream_scans >= round(seconds * hw.SAMPLE_HZ):
                    owner.worker.stop()
                return raw

            def stop_stream(self, **kwargs):
                result = super().stop_stream(**kwargs)
                if fault == "shutdown":
                    self._shutdown_failed = True
                    result.cleanup_error = "Injected cleanup failure"
                if fault == "clock_stop":
                    result.clock_stop_status = 7
                return result

        self.device = Device(real_time=False, chunk_scans=int(hw.SAMPLE_HZ))
        self.rig = hw.Mod50Rig(simulate=True, device=self.device,
                             monotonic=clock.monotonic, sleep=clock.sleep)
        self.worker = live.LiveNoiseViewer(simulate=True, installed_positions=positions,
            rig_factory=lambda **kwargs: self.rig, pace_demo=False)
        return self.worker

    def test_same_settling_policy_ready_at_five_seconds_and_stream_continues(self):
        worker = self.make_worker(seconds=12, positions=("1-1", "5-10"))
        worker.run()
        result = worker.result
        self.assertTrue(worker.finished.is_set())
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["first_ready_s"], 5.)
        self.assertEqual(result["elapsed_s"], 12.)
        self.assertEqual(result["recording_start_s"], 5.)
        self.assertEqual(result["recording_remaining_s"], 53.)
        self.assertTrue(result["settling"]["complete"])
        self.assertEqual(result["settling"]["stable_count"], 2)
        self.assertEqual(len(result["rows"]), 50)
        self.assertAlmostEqual(result["rows"][-1]["mean_v"], .002, delta=.00004)
        self.assertEqual(result["rows"][1]["state"], "excluded")
        self.assertGreaterEqual(result["rows"][0]["trace"]["time_s"][0], 2.)
        self.assertEqual(self.device.controls, (False, False))
        self.assertFalse(self.device.is_streaming)
        self.assertIsNone(worker.directory)
        self.assertFalse(result["saved"])
        json.dumps(result, allow_nan=False)

    def test_unsettled_baseline_waits_until_90_and_viewer_remains_bounded(self):
        worker = self.make_worker(seconds=153, baseline=.2)
        worker.run()
        result = worker.result
        self.assertEqual(result["errors"], [])
        self.assertIsNone(result["first_ready_s"])
        self.assertEqual(result["recording_start_s"], 90.)
        self.assertTrue(result["deadline_fallback"])
        self.assertTrue(result["preview_complete"])
        self.assertFalse(result["settling"]["complete"])
        self.assertEqual(result["rows"][0]["state"], "above_zero")
        self.assertLessEqual(len(result["settling"]["trace"]), 90)
        self.assertLessEqual(len(result["rows"][0]["trace"]["time_s"]), live.TRACE_POINTS)

    def test_retained_conversion_clipping_prevents_ready(self):
        worker = self.make_worker(clipped=True)
        worker.run()
        result = worker.result
        self.assertTrue(result["rows"][0]["clipped"])
        self.assertEqual(result["rows"][0]["state"], "clipped")
        self.assertIsNone(result["first_ready_s"])
        self.assertFalse(result["would_record"])

    def test_transport_and_power_faults_stop_and_restore_dc(self):
        for fault in ("readback", "delivery"):
            with self.subTest(fault=fault):
                worker = self.make_worker(fault=fault)
                worker.run()
                self.assertTrue(worker.result["errors"])
                self.assertFalse(worker.result["ready"])
                self.assertEqual(worker.result["phase"], "Live viewer error")
                self.assertFalse(self.device.is_streaming)
                self.assertEqual(self.device.controls, (False, False))

    def test_uncertain_shutdown_requires_restart(self):
        for fault in ("shutdown", "clock_stop"):
            with self.subTest(fault=fault):
                worker = self.make_worker(fault=fault)
                worker.run()
                self.assertTrue(worker.result["restart_required"])
                self.assertTrue(worker.result["errors"])

    def test_stop_before_start_does_not_open_device(self):
        calls = []
        worker = live.LiveNoiseViewer(simulate=True, rig_factory=lambda **kwargs: calls.append(kwargs))
        worker.stop()
        worker.run()
        self.assertEqual(calls, [])
        self.assertTrue(worker.finished.is_set())
        self.assertEqual(worker.result["errors"], [])

    def test_demo_is_paced_and_stop_is_responsive(self):
        worker = live.LiveNoiseViewer(simulate=True, installed_positions=("1-1",))
        started = time.monotonic()
        worker.start()
        time.sleep(.65)
        worker.stop()
        worker.join(3.)
        self.assertFalse(worker.is_alive())
        self.assertTrue(worker.finished.is_set())
        self.assertEqual(worker.result["errors"], [])
        self.assertLessEqual(worker.result["elapsed_s"], time.monotonic() - started + .2)

    def test_empty_or_duplicate_loaded_positions_are_rejected(self):
        for positions in ([], ["1-1", "1-1"]):
            with self.assertRaises(ValueError):
                live.LiveNoiseViewer(simulate=True, installed_positions=positions)


if __name__ == "__main__":
    unittest.main()
