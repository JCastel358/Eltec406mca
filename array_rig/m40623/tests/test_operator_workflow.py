"""Explicit offset/replacement/noise workflow, with no hardware or home writes."""

from __future__ import annotations

import csv
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


class OperatorWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def controller(self, profile=None, *, noise_limits=aa.NoiseLimits()):
        device = daq.SimulatedDaq(profile=profile, real_time=False)
        controller = app.TrayController(
            device, lot="demo", tray_number=1, tester_name="Technician", results_root=self.root,
            plan=app.CapturePlan(capture_seconds=20.0, stabilisation_s=0.0, quiet_min_s=1.0, quiet_max_s=4.0),
            noise_limits=noise_limits,
        )
        controller.start()
        self.addCleanup(controller.close)
        return controller, device

    def test_offset_check_uses_full_limits_and_defaults_every_socket_to_loaded(self):
        controller, _ = self.controller()
        measured = controller.measure_offsets()
        self.assertEqual(measured.shape, (50,))
        self.assertTrue(controller.offset_checked)
        self.assertEqual(controller.unknown_positions(), ())
        self.assertEqual(len([p for p in daq.POSITIONS if controller.effective_occupancy(p) is aa.Occupancy.LOADED]), 50)
        self.assertEqual(controller.offset_bad_positions(), ("1-10", "2-4", "3-1", "4-7", "5-2", "5-10"))
        self.assertEqual(len(controller.offset_good_positions()), 44)
        for position in controller.offset_bad_positions():
            self.assertIs(controller.live_tile_state(position), aa.TileState.OFFSET_FAIL)
        self.assertIsNone(controller.state.lock)
        self.assertFalse(controller.csv_path.exists())
        self.assertEqual(app.next_sensor_number_for_lot("demo", self.root), 1)

    def test_noise_requires_a_successful_offset_check(self):
        controller, _ = self.controller(daq.SimProfile(settle_drop_v=0.0))
        controller.poll_offsets()
        with self.assertRaisesRegex(ValueError, "Measure offset"):
            controller.prepare_noise()

    def test_bad_offsets_must_be_replaced_rechecked_or_removed(self):
        controller, _ = self.controller()
        controller.measure_offsets()
        with self.assertRaisesRegex(ValueError, "Recheck, replace"):
            controller.prepare_noise()
        self.assertIsNone(controller.state.lock)
        self.assertFalse(controller.csv_path.exists())

    def test_noise_locks_exactly_the_last_checked_readings_without_reading_again(self):
        controller, device = self.controller(daq.SimProfile(settle_drop_v=0.0))
        measured = controller.measure_offsets()
        expected = measured.copy()
        measured[:] = 4.9  # Returning the array must not expose the stored snapshot.
        with patch.object(device, "read_scan_volts_median", side_effect=AssertionError("Unexpected read")):
            lock = controller.prepare_noise()
        np.testing.assert_array_equal(lock.offset_initial_v, expected)
        self.assertEqual(len(lock.loaded_positions), 50)
        self.assertEqual(lock.ho_positions, ())
        self.assertEqual(lock.sensor_numbers["5-10"], 50)
        self.assertIs(controller.phase, app.Phase.LOCKED)

    def test_removed_failures_allow_partial_tray_and_keep_offset_evidence(self):
        controller, _ = self.controller()
        measured = controller.measure_offsets()
        bad = controller.offset_bad_positions()
        for position in bad:
            controller.set_occupancy(position, aa.Occupancy.EMPTY)
        self.assertTrue(controller.offset_checked)
        self.assertEqual(controller.offset_bad_positions(), ())
        lock = controller.prepare_noise(start_number=12)
        self.assertEqual(len(lock.loaded_positions), 44)
        self.assertEqual(sorted(lock.sensor_numbers.values()), list(range(12, 56)))
        for position in bad:
            self.assertNotIn(position, lock.sensor_numbers)
        evidence = json.loads(tray_history.read_tray_events(controller.attempts_path)[0].detail)
        self.assertEqual(evidence["bad_positions"], list(bad))
        for position in bad:
            self.assertEqual(evidence["readings"][position]["occupancy"], "LOADED")
            self.assertEqual(evidence["readings"][position]["offset_v"], measured[daq.channel_for_position(position)])

    def test_adding_a_detector_requires_a_new_measurement(self):
        controller, _ = self.controller(daq.SimProfile(settle_drop_v=0.0))
        controller.set_occupancy("1-1", aa.Occupancy.EMPTY)
        controller.measure_offsets()
        controller.set_occupancy("1-1", aa.Occupancy.LOADED)
        self.assertFalse(controller.offset_checked)
        self.assertNotIn("1-1", controller.offset_good_positions())
        with self.assertRaisesRegex(ValueError, "Measure offset"):
            controller.prepare_noise()
        controller.measure_offsets()
        self.assertEqual(len(controller.prepare_noise().loaded_positions), 50)

    def test_empty_tray_cannot_start_noise(self):
        controller, _ = self.controller()
        controller.measure_offsets()
        for position in daq.POSITIONS:
            controller.set_occupancy(position, aa.Occupancy.EMPTY)
        with self.assertRaisesRegex(ValueError, "at least one"):
            controller.prepare_noise()

    def test_failed_or_incomplete_offset_read_invalidates_previous_readiness(self):
        controller, device = self.controller(daq.SimProfile(settle_drop_v=0.0))
        controller.measure_offsets()
        with patch.object(device, "read_scan_volts_median", return_value=np.zeros(49)):
            with self.assertRaisesRegex(ValueError, "fifty finite"):
                controller.measure_offsets()
        self.assertFalse(controller.offset_checked)
        with self.assertRaisesRegex(ValueError, "Measure offset"):
            controller.prepare_noise()
        self.assertEqual(len(tray_history.read_tray_events(controller.attempts_path)), 1)

    def test_repeated_replacements_are_audited_without_consuming_sensor_numbers(self):
        controller, device = self.controller()
        controller.measure_offsets()
        initial_bad = controller.offset_bad_positions()
        device.replace_simulated_positions(initial_bad[:2])
        controller.measure_offsets()
        self.assertEqual(len(controller.offset_bad_positions()), 4)
        device.replace_simulated_positions(controller.offset_bad_positions())
        controller.measure_offsets()
        self.assertEqual(controller.offset_bad_positions(), ())
        self.assertEqual(len(controller.offset_good_positions()), 50)
        self.assertFalse(controller.csv_path.exists())
        events = tray_history.read_tray_events(controller.attempts_path)
        self.assertEqual([e.event for e in events], [tray_history.EVENT_OFFSET_MEASURED] * 3)
        self.assertEqual([json.loads(e.detail)["offset_measurement"] for e in events], [1, 2, 3])
        self.assertEqual([len(json.loads(e.detail)["bad_positions"]) for e in events], [6, 4, 0])
        self.assertTrue(all(e.first_sensor_number == e.last_sensor_number == 0 for e in events))
        self.assertEqual(controller.prepare_noise().sensor_numbers["1-1"], 1)

    def test_simulation_replacements_are_isolated_and_validate_before_changing(self):
        profile = daq.default_sim_profile()
        device = daq.SimulatedDaq(profile)
        untouched = daq.SimulatedDaq(profile)
        with self.assertRaises(ValueError):
            device.replace_simulated_positions(["2-4", "6-1"])
        self.assertIs(device.profile, profile)
        device.replace_simulated_positions(["2-4", "5-2", "1-10"])
        self.assertNotIn("2-4", device.profile.offsets_v)
        self.assertNotIn("5-2", device.profile.dead_positions)
        self.assertNotIn("1-10", device.profile.empty_positions)
        self.assertIn("3-6", device.profile.burst_positions)
        self.assertIs(untouched.profile, profile)
        self.assertIn("2-4", profile.offsets_v)

    def test_simulated_full_run_has_pass_high_and_low_noise_and_saves_once(self):
        production_limits = aa.NoiseLimits()
        limits = aa.NoiseLimits(low_mv=0.01, high_mv=0.3, provenance="SIMULATION ONLY")
        profile = replace(daq.default_sim_profile(), settle_drop_v=0.0, noise_rms_uv={"3-6": 1800.0, "4-3": 0.0})
        controller, device = self.controller(profile, noise_limits=limits)
        controller.measure_offsets()
        device.replace_simulated_positions(controller.offset_bad_positions())
        controller.measure_offsets()
        controller.prepare_noise()
        report = controller.run_noise_phase(stabilisation_wait_s=0.0)
        self.assertIsNone(report.rig_fault)
        results = {r.position: r for r in report.results}
        self.assertIs(results["1-1"].noise.verdict, aa.NoiseVerdict.PASS)
        self.assertIs(results["3-6"].noise.verdict, aa.NoiseVerdict.HIGH)
        self.assertIs(results["4-3"].noise.verdict, aa.NoiseVerdict.LOW)
        outcome = controller.save_tray()
        self.assertEqual(outcome["rows"], 50)
        self.assertTrue(Path(outcome["raw"]).is_file())
        with controller.csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 50)
        self.assertEqual({r["simulated"] for r in rows}, {"YES"})
        self.assertEqual(aa.NoiseLimits(), production_limits)
        self.assertFalse(production_limits.defined)
        with self.assertRaisesRegex(RuntimeError, "already saved"):
            controller.save_tray()

    def test_locked_positions_cannot_change_and_legacy_lock_routes_to_snapshot(self):
        controller, device = self.controller(daq.SimProfile(settle_drop_v=0.0))
        controller.measure_offsets()
        with patch.object(device, "read_scan_volts_median", side_effect=AssertionError("Unexpected read")):
            controller.lock_tray()
        with self.assertRaisesRegex(RuntimeError, "cannot change"):
            controller.toggle_occupancy("1-1")
        with self.assertRaisesRegex(RuntimeError, "before starting"):
            controller.measure_offsets()

    def captured_controller(self):
        controller, _ = self.controller(daq.SimProfile(settle_drop_v=0.0))
        controller.measure_offsets()
        controller.prepare_noise()
        controller.run_noise_phase(stabilisation_wait_s=0.0)
        return controller

    def test_retry_save_after_partial_csv_append_preserves_existing_rows_and_header(self):
        csv_path = app.lot_results_path("demo", self.root)
        existing = b"sensor_number,simulated,lot_number,custom_column\r\n999,NO,older,preserved\r\n"
        csv_path.write_bytes(existing)
        controller = self.captured_controller()
        append_rows = app.append_position_rows

        def fail_after_two_rows(path, rows):
            append_rows(path, rows[:2])
            raise OSError("disk full during CSV append")

        with patch.object(app, "save_grid_snapshot", return_value=None), \
                patch.object(app, "save_tray_raw_capture", wraps=app.save_tray_raw_capture) as raw_writer:
            with patch.object(app, "append_position_rows", side_effect=fail_after_two_rows):
                with self.assertRaisesRegex(OSError, "disk full"):
                    controller.save_tray()
            self.assertFalse(controller.state.saved)
            self.assertEqual(csv_path.read_bytes(), existing)
            self.assertEqual(list(self.root.glob("*.tmp")), [])
            outcome = controller.save_tray()
            self.assertEqual(raw_writer.call_count, 1)
        self.assertEqual(outcome["rows"], 50)
        self.assertTrue(csv_path.read_bytes().startswith(existing))
        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 51)
        self.assertEqual(rows[0]["custom_column"], "preserved")
        self.assertEqual(len({r["sensor_number"] for r in rows}), 51)

    def test_retry_save_after_failed_atomic_replace_does_not_expose_partial_results(self):
        controller = self.captured_controller()
        with patch.object(app, "save_grid_snapshot", return_value=None), \
                patch.object(app, "save_tray_raw_capture", wraps=app.save_tray_raw_capture) as raw_writer:
            with patch.object(app.os, "replace", side_effect=OSError("file locked")):
                with self.assertRaisesRegex(OSError, "file locked"):
                    controller.save_tray()
            self.assertFalse(controller.csv_path.exists())
            self.assertFalse(controller.state.saved)
            self.assertEqual(list(self.root.glob("*.tmp")), [])
            controller.save_tray()
            self.assertEqual(raw_writer.call_count, 1)
        with controller.csv_path.open(newline="", encoding="utf-8") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 50)

    def test_retry_save_after_partial_history_append_does_not_duplicate_csv_or_artifacts(self):
        controller = self.captured_controller()
        existing_history = controller.attempts_path.read_bytes()
        append_event = tray_history.append_tray_event

        def fail_after_event(path, **kwargs):
            append_event(path, **kwargs)
            raise OSError("disk full during history append")

        with patch.object(app, "save_grid_snapshot", return_value=None), \
                patch.object(app, "save_tray_raw_capture", wraps=app.save_tray_raw_capture) as raw_writer, \
                patch.object(app, "append_position_rows", wraps=app.append_position_rows) as row_writer:
            with patch.object(tray_history, "append_tray_event", side_effect=fail_after_event):
                with self.assertRaisesRegex(OSError, "history append"):
                    controller.save_tray()
            csv_after_failure = controller.csv_path.read_bytes()
            self.assertEqual(controller.attempts_path.read_bytes(), existing_history)
            self.assertFalse(controller.state.saved)
            controller.save_tray()
            self.assertEqual(raw_writer.call_count, 1)
            self.assertEqual(row_writer.call_count, 1)
        self.assertEqual(controller.csv_path.read_bytes(), csv_after_failure)
        events = tray_history.read_tray_events(controller.attempts_path)
        self.assertEqual(sum(e.event == tray_history.EVENT_SAVED for e in events), 1)
        self.assertTrue(controller.state.saved)
        self.assertEqual(list(self.root.rglob("*.npz")), [Path(controller._pending_save["raw"])])

    def test_retry_save_reuses_reserved_raw_path_after_raw_writer_error(self):
        controller = self.captured_controller()
        write_raw = app.save_tray_raw_capture

        def fail_after_raw(path, *args, **kwargs):
            write_raw(path, *args, **kwargs)
            raise OSError("raw write completion failed")

        with patch.object(app, "save_grid_snapshot", return_value=None):
            with patch.object(app, "save_tray_raw_capture", side_effect=fail_after_raw):
                with self.assertRaisesRegex(OSError, "raw write"):
                    controller.save_tray()
            raw_files = list(self.root.rglob("*.npz"))
            self.assertEqual(len(raw_files), 1)
            self.assertFalse(controller.csv_path.exists())
            outcome = controller.save_tray()
        self.assertEqual(list(self.root.rglob("*.npz")), raw_files)
        self.assertEqual(Path(outcome["raw"]), raw_files[0])


if __name__ == "__main__":
    unittest.main()
