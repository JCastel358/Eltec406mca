"""Empty-looking socket inference, overrides, wake-up and audit evidence.

Only the simulator and deterministic offset-read doubles are used. Every
result and attempt log is directed to an explicit temporary directory.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

MODEL_DIR = Path(__file__).resolve().parents[1]
for entry in (str(MODEL_DIR), str(MODEL_DIR.parent)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import array_analysis as aa
import daq_backend as daq
import eltec_40623_array_tester as app
import tray_history


class EmptyDetectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eltec-array-empty-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def controller(self, *, range_code=2, profile=None):
        device = daq.SimulatedDaq(
            profile=profile or daq.SimProfile(settle_drop_v=0.0), real_time=False,
        )
        controller = app.TrayController(
            device, lot="empty-inference", tray_number=1, tester_name="Test Technician",
            results_root=self.root, plan=app.CapturePlan(range_code=range_code),
        )
        controller.start()
        self.addCleanup(controller.close)
        return controller, device

    @staticmethod
    def readings(values=None, *, default=0.7):
        volts = np.full(daq.CHANNEL_COUNT, default, dtype=np.float64)
        for position, value in (values or {}).items():
            volts[daq.channel_for_position(position)] = value
        return volts

    def measure(self, controller, device, initial, recheck=None):
        scans = [initial, initial if recheck is None else recheck]
        with patch.object(app, "read_offset_snapshot", side_effect=scans):
            return controller.measure_offsets()

    @staticmethod
    def last_detail(controller):
        return json.loads(tray_history.read_tray_events(controller.attempts_path)[-1].detail)

    def test_mixed_simulation_keeps_dead_low_and_railed_detectors_loaded(self):
        controller, _ = self.controller(profile=daq.default_sim_profile())
        controller.measure_offsets()
        self.assertEqual(controller._auto_empty_positions, frozenset({"1-10", "5-10"}))
        self.assertEqual(controller.offset_bad_positions(), ("2-4", "3-1", "4-7", "5-2"))
        self.assertEqual(len(controller.offset_good_positions()), 44)
        for position in ("2-4", "3-1", "4-7", "5-2"):
            self.assertIs(controller.effective_occupancy(position), aa.Occupancy.LOADED)
        self.assertIs(controller.live_tile_state("5-2"), aa.TileState.OFFSET_FAIL)

    def test_one_adc_code_boundary_is_inclusive_and_uses_absolute_voltage(self):
        controller, device = self.controller(range_code=3)
        threshold = daq.lsb_volts(controller.plan.range_code)
        volts = self.readings({
            "1-1": 0.0, "1-2": threshold, "1-3": -threshold,
            "1-4": np.nextafter(threshold, np.inf),
            "1-5": np.nextafter(-threshold, -np.inf),
            "1-6": 0.02, "1-7": 4.95,
        })
        self.measure(controller, device, volts)
        self.assertEqual(controller._auto_empty_positions, frozenset({"1-1", "1-2", "1-3"}))
        self.assertEqual(controller.offset_bad_positions(), ("1-4", "1-5", "1-6", "1-7"))
        for position in ("1-1", "1-2", "1-3"):
            self.assertIs(controller.effective_occupancy(position), aa.Occupancy.EMPTY)

    def test_empty_threshold_tracks_the_configured_adc_range(self):
        # 100 microvolts exceeds one code at 0-5 V but fits within one code
        # at 0-10 V; this distinguishes range scaling from a fixed cutoff.
        for range_code, expected in ((2, aa.Occupancy.LOADED), (0, aa.Occupancy.EMPTY)):
            with self.subTest(range_code=range_code):
                controller, device = self.controller(range_code=range_code)
                self.measure(controller, device, self.readings({"1-1": 0.0001}))
                self.assertIs(controller.effective_occupancy("1-1"), expected)

    def test_both_reads_must_be_near_zero_and_waking_detector_is_kept(self):
        controller, device = self.controller()
        initial = self.readings({"1-1": 0.0, "1-2": 0.0})
        recheck = self.readings({"1-2": 0.0, "1-3": 0.0})
        measured = self.measure(controller, device, initial, recheck)
        self.assertEqual(controller._auto_empty_positions, frozenset({"1-2"}))
        self.assertIn("1-1", controller.offset_good_positions())
        self.assertIn("1-3", controller.offset_bad_positions())
        self.assertIs(controller.effective_occupancy("1-3"), aa.Occupancy.LOADED)
        self.assertEqual(measured[0], 0.7)
        detail = self.last_detail(controller)["readings"]["1-1"]
        self.assertEqual(detail["offset_initial_v"], 0.0)
        self.assertEqual(detail["offset_v"], 0.7)

    def test_forced_loaded_zero_remains_a_failure_after_rechecks(self):
        controller, device = self.controller()
        volts = self.readings({"1-1": 0.0, "1-2": 0.0})
        self.measure(controller, device, volts)
        controller.set_occupancy("1-1", aa.Occupancy.LOADED)
        self.assertFalse(controller.offset_checked)
        for _ in range(2):
            self.measure(controller, device, volts)
            self.assertIs(controller.effective_occupancy("1-1"), aa.Occupancy.LOADED)
            self.assertEqual(controller.offset_bad_positions(), ("1-1",))
            self.assertIs(controller.effective_occupancy("1-2"), aa.Occupancy.EMPTY)
            self.assertEqual(self.last_detail(controller)["readings"]["1-1"]["occupancy_source"], "manual_loaded")
        with self.assertRaisesRegex(ValueError, "Recheck, replace"):
            controller.prepare_noise()

    def test_manual_empty_choice_is_preserved_with_nonzero_signal(self):
        controller, device = self.controller()
        controller.set_occupancy("1-1", aa.Occupancy.EMPTY)
        self.measure(controller, device, self.readings({"1-1": 4.95}))
        self.assertIs(controller.effective_occupancy("1-1"), aa.Occupancy.EMPTY)
        self.assertNotIn("1-1", controller.offset_bad_positions())
        detail = self.last_detail(controller)["readings"]["1-1"]
        self.assertEqual(detail["occupancy_source"], "manual_empty")
        self.assertEqual(detail["offset_v"], 4.95)
        self.assertEqual(detail["loaded_offset_class"], aa.OffsetClass.HO_RAILED.value)

    def test_newly_inserted_detector_is_detected_without_an_occupancy_click(self):
        controller, device = self.controller()
        empty = self.readings({"1-1": 0.0})
        self.measure(controller, device, empty)
        self.assertIs(controller.effective_occupancy("1-1"), aa.Occupancy.EMPTY)
        self.measure(controller, device, self.readings())
        self.assertIs(controller.effective_occupancy("1-1"), aa.Occupancy.LOADED)
        self.assertIn("1-1", controller.offset_good_positions())
        self.assertNotIn("1-1", controller.occupancy_choice)
        self.measure(controller, device, empty)
        self.assertIs(controller.effective_occupancy("1-1"), aa.Occupancy.EMPTY)
        self.assertEqual(controller.offset_measurement_count, 3)

    def test_all_zero_tray_is_empty_and_cannot_start_noise_or_spend_numbers(self):
        controller, device = self.controller()
        self.measure(controller, device, self.readings(default=0.0))
        self.assertTrue(controller.offset_checked)
        self.assertEqual(controller.offset_good_positions(), ())
        self.assertEqual(controller.offset_bad_positions(), ())
        self.assertTrue(all(controller.effective_occupancy(p) is aa.Occupancy.EMPTY for p in daq.POSITIONS))
        with self.assertRaisesRegex(ValueError, "at least one"):
            controller.prepare_noise()
        self.assertIsNone(controller.state.lock)
        self.assertFalse(controller.csv_path.exists())
        events = tray_history.read_tray_events(controller.attempts_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].loaded_count, 0)
        self.assertEqual((events[0].first_sensor_number, events[0].last_sensor_number), (0, 0))

    def test_invalid_recheck_invalidates_readiness_without_recording_a_check(self):
        controller, device = self.controller()
        self.measure(controller, device, self.readings())
        initial = self.readings({"1-1": 0.0})
        for invalid in (np.zeros(49), self.readings({"1-2": np.nan})):
            with self.subTest(shape=invalid.shape):
                with self.assertRaisesRegex(ValueError, "fifty finite"):
                    self.measure(controller, device, initial, invalid)
                self.assertFalse(controller.offset_checked)
                with self.assertRaisesRegex(ValueError, "Measure offset"):
                    controller.prepare_noise()
                self.assertEqual(controller.offset_measurement_count, 1)
                self.assertEqual(len(tray_history.read_tray_events(controller.attempts_path)), 1)

    def test_hardware_mode_waits_two_seconds_before_rechecking_and_logs_actual_wait(self):
        controller, device = self.controller()
        # This is still SimulatedDaq. Only its metadata is changed so the
        # controller's timed path is exercised without opening any hardware.
        device.info = replace(device.info, simulated=False)
        elapsed = [0.0]
        read_times = []

        def advance(seconds):
            elapsed[0] += seconds

        def read_scan(*args, **kwargs):
            read_times.append(elapsed[0])
            return self.readings({"1-1": 0.0})

        with patch.object(app.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(app.time, "sleep", side_effect=advance), \
                patch.object(app, "read_offset_snapshot", side_effect=read_scan):
            controller.measure_offsets()
        self.assertEqual(len(read_times), 2)
        self.assertEqual(read_times[0], 0.0)
        self.assertAlmostEqual(read_times[1], app.EMPTY_RECHECK_S)
        self.assertAlmostEqual(self.last_detail(controller)["empty_recheck_wait_s"], app.EMPTY_RECHECK_S)
        self.assertIs(controller.effective_occupancy("1-1"), aa.Occupancy.EMPTY)

    def test_cancel_during_empty_recheck_keeps_previous_audit_and_blocks_noise(self):
        controller, device = self.controller()
        self.measure(controller, device, self.readings())
        device.info = replace(device.info, simulated=False)
        elapsed = [0.0]

        def advance(seconds):
            elapsed[0] += seconds

        with patch.object(app.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(app.time, "sleep", side_effect=advance), \
                patch.object(app, "read_offset_snapshot", return_value=self.readings({"1-1": 0.0})) as read:
            with self.assertRaises(app.CaptureCancelled):
                controller.measure_offsets(cancelled=lambda: elapsed[0] >= 0.1)
        self.assertEqual(read.call_count, 1)
        self.assertFalse(controller.offset_checked)
        self.assertEqual(controller.offset_measurement_count, 1)
        self.assertEqual(len(tray_history.read_tray_events(controller.attempts_path)), 1)
        with self.assertRaisesRegex(ValueError, "Measure offset"):
            controller.prepare_noise()

    def test_audit_preserves_inferred_empty_and_loaded_failure_interpretations(self):
        controller, device = self.controller()
        controller.set_occupancy("1-2", aa.Occupancy.LOADED)
        controller.set_occupancy("1-3", aa.Occupancy.EMPTY)
        volts = self.readings({"1-1": 0.0, "1-2": 0.0, "1-3": 0.7, "1-4": 0.02})
        self.measure(controller, device, volts)
        detail = self.last_detail(controller)
        self.assertEqual(detail["empty_detection_policy"], app.EMPTY_DETECTION_POLICY)
        self.assertEqual(detail["empty_threshold_v"], daq.lsb_volts(controller.plan.range_code))
        self.assertEqual(detail["empty_recheck_wait_s"], 0.0)
        self.assertEqual(detail["inferred_empty_positions"], ["1-1"])
        readings = detail["readings"]
        expected_sources = {
            "1-1": "inferred_near_zero", "1-2": "manual_loaded",
            "1-3": "manual_empty", "1-4": "measured_signal",
        }
        for position, source in expected_sources.items():
            self.assertEqual(readings[position]["occupancy_source"], source)
            self.assertEqual(readings[position]["offset_initial_v"], volts[daq.channel_for_position(position)])
            self.assertEqual(readings[position]["offset_v"], volts[daq.channel_for_position(position)])
        self.assertEqual(readings["1-1"]["occupancy"], aa.Occupancy.EMPTY.value)
        self.assertEqual(readings["1-1"]["offset_class"], aa.OffsetClass.EMPTY.value)
        self.assertEqual(readings["1-1"]["loaded_offset_class"], aa.OffsetClass.DEAD.value)
        self.assertEqual(readings["1-2"]["offset_class"], aa.OffsetClass.DEAD.value)
        self.assertEqual(readings["1-4"]["offset_class"], aa.OffsetClass.DEAD.value)
        self.assertEqual(detail["bad_positions"], ["1-2", "1-4"])


if __name__ == "__main__":
    unittest.main()
