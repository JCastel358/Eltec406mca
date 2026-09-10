"""Array settling is bounded, observes waveform extrema, and keeps stream continuity."""

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


def loaded_mask(*channels):
    mask = np.zeros(50, dtype=bool)
    mask[list(channels)] = True
    return mask


class ScriptedDaq(daq.SimulatedDaq):
    """Deterministic signal with deliberately uneven callback sizes."""

    def __init__(self, signal, chunks=(160,)):
        super().__init__(real_time=False)
        self.signal = signal
        self.chunks = chunks
        self.chunk_index = 0
        self.sample_index = 0
        self.connect()
        self.configure(app.CapturePlan().config)

    def _synth(self, scans, scan_hz):
        indices = np.arange(self.sample_index, self.sample_index + scans)
        self.sample_index += scans
        self._advance(scans / scan_hz)
        return self.signal(indices)

    def read_stream(self, *, timeout_s=1.0):
        self.chunk_scans = self.chunks[self.chunk_index % len(self.chunks)]
        self.chunk_index += 1
        return super().read_stream(timeout_s=timeout_s)


def constant_signal(indices):
    return np.full((len(indices), 50), 0.7)


def changing_amplitude(indices):
    signal = constant_signal(indices)
    signal[:, 0] += (indices // 1000 + 1) * 0.001 * np.where(indices % 2, -1, 1)
    return signal


def ramp_signal(indices):
    return np.broadcast_to((0.7 + indices * 1e-8)[:, None], (len(indices), 50)).copy()


def expected_volts(signal, indices):
    return daq.counts_to_volts(daq.volts_to_counts(signal(indices), app.DAQ_RANGE_CODE), app.DAQ_RANGE_CODE)


def lock_for(*channels):
    occupancy = [aa.Occupancy.LOADED if c in channels else aa.Occupancy.EMPTY for c in range(50)]
    return app.LockSnapshot(
        occupancy=occupancy, sensor_numbers=app.assign_sensor_numbers(occupancy, 1),
        offset_initial_v=np.full(50, 0.7), ho_positions=(), start_number=1, locked_at="test",
    )


class ExtremaHelperTests(unittest.TestCase):
    def test_changing_amplitude_with_constant_mean_is_not_settled(self):
        maxima = np.array([[0.701], [0.702], [0.703]])
        minima = 1.4 - maxima
        self.assertTrue(aa.quiet_wait_settled((maxima + minima) / 2, [True], delta_mv=0.1, blocks_required=2))
        self.assertFalse(aa.quiet_wait_settled(maxima, [True], delta_mv=0.1, blocks_required=2, block_minima_v=minima))

    def test_minima_must_settle_even_if_maxima_are_fixed(self):
        self.assertFalse(aa.quiet_wait_settled(
            [[0.8], [0.8], [0.8]], [True], delta_mv=0.1, blocks_required=2,
            block_minima_v=[[0.7], [0.69], [0.68]],
        ))

    def test_two_consecutive_deltas_required_with_inclusive_boundary(self):
        maxima = [[0.801], [0.8011], [0.8012]]
        minima = [[0.7], [0.6999], [0.6998]]
        self.assertFalse(aa.quiet_wait_settled(maxima[:2], [True], delta_mv=0.1, blocks_required=2, block_minima_v=minima[:2]))
        self.assertTrue(aa.quiet_wait_settled(maxima, [True], delta_mv=0.1, blocks_required=2, block_minima_v=minima))

    def test_unloaded_channel_drift_does_not_delay_loaded_channel(self):
        self.assertTrue(aa.quiet_wait_settled(
            [[0.7, 1], [0.7, 2], [0.7, 3]], [True, False], delta_mv=0.1, blocks_required=2,
            block_minima_v=[[0.69, -1], [0.69, -2], [0.69, -3]],
        ))

    def test_nonfinite_loaded_extrema_never_claim_settled(self):
        self.assertFalse(aa.quiet_wait_settled([[0.7], [float("nan")], [0.7]], [True], delta_mv=0.1, blocks_required=2))
        with self.assertRaises(ValueError):
            aa.quiet_wait_settled([[0.7]] * 3, [True], delta_mv=-0.1, blocks_required=2)
        with self.assertRaises(ValueError):
            aa.quiet_wait_settled([[0.7]] * 3, [True], delta_mv=0.1, blocks_required=2, block_minima_v=[[0.6]])


class QuietWaitTests(unittest.TestCase):
    def run_wait(self, signal, *, mask=None, plan=None, chunks=(160,)):
        plan = plan or app.CapturePlan()
        sim = ScriptedDaq(signal, chunks)
        sim.start_stream(scan_hz=plan.scan_hz)
        diagnostics, remainder = {}, []
        try:
            result = app.run_quiet_wait(sim, plan, loaded_mask(0) if mask is None else mask,
                                        diagnostics=diagnostics, remainder_scans=remainder)
        finally:
            sim.stop_stream()
        return result, diagnostics, remainder

    def test_default_policy_is_three_to_twenty_seconds_and_full_minute_capture(self):
        plan = app.CapturePlan()
        self.assertEqual((plan.stabilisation_s, plan.quiet_min_s, plan.quiet_max_s, plan.capture_seconds), (0, 3, 20, 60))
        (tail, elapsed, settled), diagnostic, remainder = self.run_wait(constant_signal)
        self.assertEqual((elapsed, settled), (3, True))
        self.assertEqual(tail.shape, (50, 310))
        self.assertEqual(sum(len(chunk) for chunk in remainder), 40)
        self.assertEqual(diagnostic["stop_reason"], "settled")
        self.assertEqual(diagnostic["loaded_channels"], [0])
        self.assertEqual(len(diagnostic["windows"]), 3)
        self.assertEqual(diagnostic["windows"][-1]["max_delta_mv"], [0])

    def test_equal_means_with_changing_extrema_wait_until_deadline(self):
        (_, elapsed, settled), diagnostic, _ = self.run_wait(changing_amplitude)
        self.assertEqual((elapsed, settled), (20, False))
        self.assertEqual(diagnostic["stop_reason"], "deadline")
        self.assertEqual(len(diagnostic["windows"]), 20)
        self.assertGreater(diagnostic["windows"][-1]["max_delta_mv"][0], 0.1)
        self.assertGreater(diagnostic["windows"][-1]["min_delta_mv"][0], 0.1)

    def test_empty_channel_transients_do_not_delay_partial_tray(self):
        (_, elapsed, settled), diagnostic, _ = self.run_wait(changing_amplitude, mask=loaded_mask(1))
        self.assertEqual((elapsed, settled), (3, True))
        self.assertEqual(diagnostic["loaded_channels"], [1])

    def test_different_chunk_sizes_have_identical_settling_evidence(self):
        results = [self.run_wait(ramp_signal, chunks=chunks) for chunks in ((1,), (160,), (7, 1537, 311, 4000), (10000,))]
        for (tail, elapsed, settled), diagnostic, _ in results:
            self.assertEqual((elapsed, settled), (3, True))
            np.testing.assert_array_equal(tail, expected_volts(ramp_signal, np.arange(2690, 3000)).T)
            self.assertEqual(diagnostic, results[0][1])

    def test_deadline_can_stop_partway_through_block_and_chunk(self):
        plan = replace(app.CapturePlan(), quiet_min_s=3.25, quiet_max_s=3.25)
        (tail, elapsed, settled), diagnostic, remainder = self.run_wait(ramp_signal, plan=plan, chunks=(10000,))
        self.assertEqual((elapsed, settled), (3.25, False))
        self.assertEqual(diagnostic["elapsed_s"], 3.25)
        self.assertEqual(len(diagnostic["windows"]), 3)
        np.testing.assert_array_equal(tail, expected_volts(ramp_signal, np.arange(2940, 3250)).T)
        np.testing.assert_array_equal(remainder[0], expected_volts(ramp_signal, np.arange(3250, 10000)))

    def test_cancellation_can_interrupt_a_large_chunk(self):
        sim = ScriptedDaq(constant_signal, chunks=(10000,))
        sim.start_stream(scan_hz=1000)
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 3

        try:
            with self.assertRaises(app.CaptureCancelled):
                app.run_quiet_wait(sim, app.CapturePlan(), loaded_mask(0), cancelled=cancelled)
        finally:
            sim.stop_stream()


class CaptureContinuityTests(unittest.TestCase):
    def test_controller_duration_override_matches_csv_and_raw_capture_metadata(self):
        sim = ScriptedDaq(constant_signal)
        with tempfile.TemporaryDirectory() as directory:
            controller = app.TrayController(sim, lot="timing", tray_number=1, tester_name="test",
                                            results_root=Path(directory))
            try:
                controller.start()
                controller.measure_offsets()
                controller.prepare_noise()
                controller.run_noise_phase(stabilisation_wait_s=0, capture_seconds=1)
                with patch.object(app, "save_grid_snapshot", return_value=None):
                    outcome = controller.save_tray()
                with np.load(outcome["raw"]) as saved:
                    actual_seconds = saved["waveform_v"].shape[1] / float(saved["sample_rate_hz"])
                    self.assertEqual(actual_seconds, 1)
                    self.assertEqual(float(saved["capture_seconds"]), actual_seconds)
                with controller.csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(rows)
                self.assertTrue(all(float(row["capture_seconds"]) == actual_seconds for row in rows))
            finally:
                controller.close()

    def test_full_capture_is_contiguous_and_preserves_settling_log(self):
        plan = app.CapturePlan()
        sim = ScriptedDaq(ramp_signal, chunks=(7, 1537, 311, 4000))
        lock = lock_for(0)
        report = app.run_tray_capture(sim, plan, lock)
        self.assertIsNone(report.rig_fault)
        capture = report.capture
        self.assertEqual((capture.quiet_wait_s, capture.quiet_settled), (3, True))
        self.assertEqual(capture.waveform_v.shape, (50, 60000))
        np.testing.assert_array_equal(capture.left_context_v, expected_volts(ramp_signal, np.arange(2690, 3000)).T.astype(np.float32))
        np.testing.assert_array_equal(capture.waveform_v, expected_volts(ramp_signal, np.arange(3000, 63000)).T.astype(np.float32))
        np.testing.assert_array_equal(capture.right_context_v, expected_volts(ramp_signal, np.arange(63000, 63310)).T.astype(np.float32))
        with tempfile.TemporaryDirectory() as directory:
            path = app.save_tray_raw_capture(Path(directory) / "capture.npz", capture, lock,
                                            lot="test", tray_number=1, tray_attempt=1, daq_info=sim.info, plan=plan)
            with np.load(path) as saved:
                self.assertEqual(json.loads(str(saved["quiet_diagnostics_json"])), capture.quiet_diagnostics)
                self.assertEqual(str(saved["noise_timing_policy"]), app.NOISE_TIMING_POLICY)
                self.assertEqual(str(saved["quiet_stop_reason"]), "settled")
                self.assertEqual(float(saved["capture_seconds"]), 60)
        ctx = app.RowContext(lot="test", tray_number=1, tray_attempt=1, tester_name="test", daq_serial="test",
                             simulated=True, plan=plan, quiet_wait_s=capture.quiet_wait_s, quiet_settled=True,
                             quiet_diagnostics=capture.quiet_diagnostics)
        row = app.position_row(report.results[0], ctx)
        self.assertEqual(row["quiet_stop_reason"], "settled")
        self.assertEqual(row["quiet_settle_criterion"], app.NOISE_SETTLE_CRITERION)
        self.assertEqual(float(row["quiet_settle_delta_mv"]), 0.1)
        self.assertEqual(int(row["quiet_settle_blocks"]), 2)
        self.assertEqual(row["noise_timing_policy"], app.NOISE_TIMING_POLICY)

    def test_deadline_keeps_capture_duration_and_short_noise_remains_unqualified(self):
        sim = ScriptedDaq(changing_amplitude, chunks=(1667,))
        plan = replace(app.CapturePlan(), capture_seconds=2)
        report = app.run_tray_capture(sim, plan, lock_for(0))
        self.assertIsNone(report.rig_fault)
        self.assertEqual(report.capture.waveform_v.shape, (50, 2000))
        self.assertEqual((report.capture.quiet_wait_s, report.capture.quiet_settled), (20, False))
        self.assertIs(report.results[0].noise.verdict, aa.NoiseVerdict.NOT_MEASURED)
        self.assertIs(report.results[0].verdict, aa.PositionVerdict.NOT_MEASURED)
        self.assertTrue(any("at least 60" in reason for reason in report.results[0].noise.legacy.quality_reasons))
        self.assertTrue(any("settling deadline" in warning for warning in report.results[0].warnings))


if __name__ == "__main__":
    unittest.main()
