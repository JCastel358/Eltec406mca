"""A DAQ start/stop failure must use the same retry policy as a read failure."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

MODEL_DIR = Path(__file__).resolve().parents[1]
for entry in (str(MODEL_DIR), str(MODEL_DIR.parent)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import array_analysis as aa
import daq_backend as daq
import eltec_40623_array_tester as app
import tray_history


class EdgeFailureDaq(daq.SimulatedDaq):
    def __init__(self, *, start_failures=(), stop_failures=()):
        super().__init__(daq.SimProfile(settle_drop_v=0.0), real_time=False)
        self.start_failures = set(start_failures)
        self.stop_failures = set(stop_failures)
        self.start_calls = 0
        self.stop_calls = 0

    def start_stream(self, **kwargs):
        self.start_calls += 1
        if self.start_calls in self.start_failures:
            raise daq.DaqStatusError("ADC_BulkContinuousCallbackStart", 13)
        return super().start_stream(**kwargs)

    def _synth(self, scans, scan_hz):
        values = super()._synth(scans, scan_hz)
        # Make a failed first capture unmistakable if it leaks into the retry.
        if self.is_streaming and self.start_calls == 1 and 1 in self.stop_failures:
            values += 1.0
        return values

    def stop_stream(self, **kwargs):
        self.stop_calls += 1
        diagnostics = super().stop_stream(**kwargs)
        if self.stop_calls in self.stop_failures:
            raise daq.DaqStatusError("ADC_BulkContinuousEnd", 13)
        return diagnostics


class CaptureRetryEdgesTests(unittest.TestCase):
    def controller(self, device):
        temporary = tempfile.TemporaryDirectory(prefix="eltec-retry-edges-")
        self.addCleanup(temporary.cleanup)
        controller = app.TrayController(
            device, lot="retry-edges", tray_number=1, tester_name="Test",
            results_root=Path(temporary.name),
            plan=app.CapturePlan(capture_seconds=60.0, quiet_min_s=1.0, quiet_max_s=1.0, retry_limit=2),
        )
        controller.start()
        self.addCleanup(controller.close)
        # The engineering lock reads DC without starting a bulk capture, so
        # start/stop faults below occur during the noise attempt under test.
        controller.lock_tray()
        return controller

    def test_start_failure_retries_and_unqualified_success_cannot_pass(self):
        device = EdgeFailureDaq(start_failures=(1,))
        controller = self.controller(device)
        report = controller.run_noise_phase(stabilisation_wait_s=0.0)
        self.assertIsNone(report.rig_fault)
        self.assertEqual(report.attempts_used, 2)
        self.assertEqual(device.start_calls, 2)
        self.assertFalse(any(result.passed for result in report.results))
        retries = [event for event in tray_history.read_tray_events(controller.attempts_path)
                   if event.event == tray_history.EVENT_CAPTURE_RETRY]
        self.assertEqual(len(retries), 1)
        self.assertIn("ADC_BulkContinuousCallbackStart", retries[0].detail)
        self.assertFalse(device.is_streaming)

    def test_stop_failure_retries_without_retaining_the_bad_capture(self):
        device = EdgeFailureDaq(stop_failures=(1,))
        controller = self.controller(device)
        report = controller.run_noise_phase(stabilisation_wait_s=0.0)
        self.assertIsNone(report.rig_fault)
        self.assertEqual(report.attempts_used, 2)
        self.assertLess(np.max(report.capture.waveform_v), 1.0)
        self.assertFalse(any(result.passed for result in report.results))
        self.assertFalse(device.is_streaming)
        self.assertEqual(device.stop_calls, 2)

    def test_exhausted_start_and_stop_failures_save_not_measured_rows(self):
        for faults, function in (({"start_failures": (1, 2, 3)}, "ADC_BulkContinuousCallbackStart"),
                                 ({"stop_failures": (1, 2, 3)}, "ADC_BulkContinuousEnd")):
            with self.subTest(function=function):
                device = EdgeFailureDaq(**faults)
                controller = self.controller(device)
                report = controller.run_noise_phase(stabilisation_wait_s=0.0)
                self.assertIn(function, report.rig_fault)
                self.assertEqual(report.attempts_used, 3)
                self.assertIsNone(report.capture)
                self.assertEqual(report.noise, [])
                self.assertTrue(all(result.verdict is aa.PositionVerdict.NOT_MEASURED for result in report.results))
                saved = controller.save_tray()
                self.assertEqual(saved["raw"], "")
                with controller.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 50)
                self.assertEqual({row["pass_fail"] for row in rows}, {"NOT MEASURED"})
                self.assertEqual({row["noise_verdict"] for row in rows}, {""})
                self.assertFalse(device.is_streaming)


if __name__ == "__main__":
    unittest.main()
