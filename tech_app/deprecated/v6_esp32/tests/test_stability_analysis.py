from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


V6_DIR = Path(__file__).resolve().parents[1]
if str(V6_DIR) not in sys.path:
    sys.path.insert(0, str(V6_DIR))

from stability_analysis import (  # noqa: E402
    DEFAULT_SETTINGS_PATH,
    StabilitySettings,
    StabilitySettingsError,
    SyncValidationError,
    analyze_stability,
    complete_cycle_segments,
    load_stability_settings,
    robust_upper_peak_v,
    validate_rising_sync_cycles,
)


def waveform_for_peaks(
    peaks_v: list[float], *, samples_per_cycle: int = 20
) -> tuple[list[float], list[float]]:
    """Build complete cycles bracketed by observed rising sync edges."""

    if samples_per_cycle < 10 or samples_per_cycle % 2:
        raise ValueError("test cycles need an even sample count of at least 10")
    waveform = [peaks_v[0] - 0.020]
    sync = [0.0]
    half = samples_per_cycle // 2
    for peak in peaks_v:
        waveform.extend([peak] * half)
        waveform.extend([peak - 0.020] * half)
        sync.extend([1.0] * half)
        sync.extend([0.0] * half)
    # This final transition closes the last requested complete cycle.
    waveform.append(peaks_v[-1])
    sync.append(1.0)
    return waveform, sync


class SettingsTests(unittest.TestCase):
    def test_tracked_settings_load_with_provisional_defaults(self):
        settings = load_stability_settings()
        self.assertEqual(DEFAULT_SETTINGS_PATH.name, "stability_settings.json")
        self.assertEqual(settings.peak_delta_threshold_mv, 0.100)
        self.assertEqual(settings.consecutive_deltas_required, 5)

    def test_missing_and_invalid_settings_raise_clear_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            with self.assertRaisesRegex(StabilitySettingsError, "cannot read"):
                load_stability_settings(missing)

            malformed = Path(tmp) / "malformed.json"
            malformed.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(StabilitySettingsError, "invalid JSON"):
                load_stability_settings(malformed)

            invalid = Path(tmp) / "invalid.json"
            invalid.write_text(
                json.dumps(
                    {
                        "peak_delta_threshold_mv": -0.1,
                        "consecutive_deltas_required": 5,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(StabilitySettingsError, "positive finite"):
                load_stability_settings(invalid)


class PeakAndCycleTests(unittest.TestCase):
    def test_robust_peak_rejects_a_single_adc_spike(self):
        samples = [0.8] * 99 + [4.9]
        self.assertEqual(robust_upper_peak_v(samples), 0.8)

    def test_robust_peak_requires_five_samples(self):
        with self.assertRaisesRegex(ValueError, "at least 5"):
            robust_upper_peak_v([0.1, 0.2, 0.3, 0.4])

    def test_only_cycles_between_rising_edges_are_complete(self):
        sync = [1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0]
        self.assertEqual(complete_cycle_segments(sync), ((4, 8),))

    def test_sync_validation_requires_three_complete_cycles_at_ten_hz(self):
        sync = ([0.0] * 50 + [1.0] * 50) * 5
        result = validate_rising_sync_cycles(sync, 1000.0)
        self.assertEqual(result.cycles_validated, 3)
        self.assertAlmostEqual(result.measured_frequency_hz, 10.0)

        lone_transition = [0.0] * 50 + [1.0] * 450
        with self.assertRaisesRegex(SyncValidationError, "complete rising-edge cycles"):
            validate_rising_sync_cycles(lone_transition, 1000.0)

        five_hz = ([0.0] * 100 + [1.0] * 100) * 5
        with self.assertRaisesRegex(SyncValidationError, "frequency is 5.000 Hz"):
            validate_rising_sync_cycles(five_hz, 1000.0)

        irregular_edges = [50, 100, 200, 350]
        irregular = [0.0] * 400
        for edge in irregular_edges:
            for index in range(edge, min(edge + 20, len(irregular))):
                irregular[index] = 1.0
        with self.assertRaisesRegex(SyncValidationError, "validation cycles span"):
            validate_rising_sync_cycles(irregular, 1000.0)


class StabilityRuleTests(unittest.TestCase):
    settings = StabilitySettings()

    def analyze(self, peaks_v: list[float], **kwargs):
        waveform, sync = waveform_for_peaks(peaks_v)
        return analyze_stability(
            waveform,
            sync,
            sample_rate_hz=200.0,
            settings=self.settings,
            **kwargs,
        )

    def test_rising_and_falling_trends_have_signed_adjacent_deltas(self):
        rising = self.analyze([0.7000, 0.7002, 0.7004])
        falling = self.analyze([0.7004, 0.7002, 0.7000])

        self.assertAlmostEqual(rising.cycles[1].signed_peak_delta_mv, 0.2)
        self.assertAlmostEqual(rising.cycles[1].absolute_peak_delta_mv, 0.2)
        self.assertFalse(rising.cycles[1].within_threshold)
        self.assertAlmostEqual(falling.cycles[1].signed_peak_delta_mv, -0.2)
        self.assertAlmostEqual(falling.cycles[1].absolute_peak_delta_mv, 0.2)
        self.assertFalse(falling.cycles[1].within_threshold)

    def test_threshold_equality_qualifies(self):
        analysis = self.analyze(
            [0.7000, 0.7001, 0.7002, 0.7003, 0.7004, 0.7005]
        )
        self.assertTrue(analysis.report.stabilized)
        self.assertEqual(analysis.report.stabilization_cycle, 6)
        self.assertEqual(analysis.cycles[-1].confirmation_run_length, 5)
        self.assertAlmostEqual(analysis.report.confirming_window_max_delta_mv, 0.1)

    def test_out_of_tolerance_delta_resets_confirmation_run(self):
        peaks = [
            0.70000,
            0.70005,
            0.70010,
            0.70030,
            0.70035,
            0.70040,
            0.70045,
            0.70050,
            0.70055,
        ]
        analysis = self.analyze(peaks)
        self.assertEqual(
            [cycle.confirmation_run_length for cycle in analysis.cycles],
            [0, 1, 2, 0, 1, 2, 3, 4, 5],
        )
        self.assertEqual(analysis.report.stabilization_cycle, 9)

    def test_fewer_than_five_qualifying_deltas_is_not_stable(self):
        analysis = self.analyze([0.7, 0.70005, 0.70010, 0.70015, 0.70020])
        self.assertFalse(analysis.report.stabilized)
        self.assertFalse(analysis.report.timed_out)
        self.assertEqual(analysis.cycles[-1].confirmation_run_length, 4)

    def test_small_bidirectional_jitter_stabilizes(self):
        analysis = self.analyze(
            [0.70000, 0.70004, 0.69998, 0.70003, 0.69999, 0.70002]
        )
        self.assertTrue(analysis.report.stabilized)
        self.assertEqual(analysis.report.stabilization_cycle, 6)
        self.assertAlmostEqual(
            analysis.report.confirming_window_max_delta_mv, 0.06
        )

    def test_next_ten_fresh_cycles_are_selected_after_stabilization(self):
        peaks = [0.7] * 16
        peaks[-1] = 0.7002
        analysis = self.analyze(peaks)
        self.assertEqual(analysis.report.stabilization_cycle, 6)
        self.assertTrue(analysis.report.measurement_complete)
        self.assertEqual(
            [cycle.cycle_number for cycle in analysis.measurement_cycles],
            list(range(7, 17)),
        )
        self.assertEqual(len(analysis.measurement_segments), 10)
        self.assertAlmostEqual(analysis.report.last_delta_mv, 0.2)

    def test_stability_exactly_at_deadline_succeeds(self):
        waveform, sync = waveform_for_peaks([0.7] * 6, samples_per_cycle=10)
        # Rising edges are 1, 11, ..., 61.  Cycle six closes at sample 61;
        # 19.39 seconds of PWM-on time before the first sample makes it 20.0.
        analysis = analyze_stability(
            waveform,
            sync,
            sample_rate_hz=100.0,
            settings=self.settings,
            pwm_elapsed_offset_s=19.39,
            stability_deadline_s=20.0,
        )
        self.assertTrue(analysis.report.stabilized)
        self.assertFalse(analysis.report.timed_out)
        self.assertAlmostEqual(analysis.report.stabilization_elapsed_s, 20.0)

    def test_late_stability_finishes_ten_fresh_cycles_after_deadline(self):
        waveform, sync = waveform_for_peaks([0.7] * 16, samples_per_cycle=10)
        analysis = analyze_stability(
            waveform,
            sync,
            sample_rate_hz=100.0,
            settings=self.settings,
            pwm_elapsed_offset_s=19.39,
            stability_deadline_s=20.0,
            measurement_cycles_required=10,
        )
        self.assertAlmostEqual(analysis.report.stabilization_elapsed_s, 20.0)
        self.assertTrue(analysis.report.measurement_complete)
        self.assertEqual(len(analysis.measurement_cycles), 10)
        self.assertGreater(analysis.measurement_cycles[-1].end_elapsed_s, 20.9)

    def test_stability_after_deadline_times_out(self):
        waveform, sync = waveform_for_peaks([0.7] * 6, samples_per_cycle=10)
        analysis = analyze_stability(
            waveform,
            sync,
            sample_rate_hz=100.0,
            settings=self.settings,
            pwm_elapsed_offset_s=19.390001,
            stability_deadline_s=20.0,
        )
        self.assertFalse(analysis.report.stabilized)
        self.assertTrue(analysis.report.timed_out)
        self.assertIsNone(analysis.report.stabilization_cycle)
        self.assertEqual(analysis.report.measurement_cycle_count, 0)


if __name__ == "__main__":
    unittest.main()
