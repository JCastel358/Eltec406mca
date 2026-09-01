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
    analyze_noise_capture,
    analyze_noise_capture_band_limited,
    analyze_stability,
    antialias_edge_context_samples,
    complete_cycle_segments,
    decimate_antialiased,
    decimate_boxcar,
    detrend_window_segments,
    fixed_window_segments,
    load_stability_settings,
    robust_upper_peak_v,
    validate_rising_sync_cycles,
    window_peak_to_peak_mv,
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
        # 449 M18: provisional 0.100 mV (the 406MCA's 10 Hz value) until the
        # first known-good captures at 5 Hz / 18 Hz say otherwise.
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

    def test_sync_validation_requires_five_complete_cycles_at_five_hz(self):
        # 449 M18 first drive: 5 Hz / 20 % at 1000 SPS -> 200 samples per
        # cycle, 40 of them high.
        sync = ([0.0] * 160 + [1.0] * 40) * 7
        result = validate_rising_sync_cycles(sync, 1000.0)
        self.assertEqual(result.cycles_validated, 5)
        self.assertAlmostEqual(result.measured_frequency_hz, 5.0)

        lone_transition = [0.0] * 500 + [1.0] * 4500
        with self.assertRaisesRegex(SyncValidationError, "complete rising-edge cycles"):
            validate_rising_sync_cycles(lone_transition, 1000.0)

        # The 406MCA's 10 Hz drive must be rejected as the wrong cadence.
        ten_hz = ([0.0] * 50 + [1.0] * 50) * 7
        with self.assertRaisesRegex(SyncValidationError, "frequency is 10.000 Hz"):
            validate_rising_sync_cycles(ten_hz, 1000.0)

        irregular_edges = [500, 700, 950, 1100, 1300, 1500]
        irregular = [0.0] * 1800
        for edge in irregular_edges:
            for index in range(edge, min(edge + 40, len(irregular))):
                irregular[index] = 1.0
        with self.assertRaisesRegex(SyncValidationError, "validation cycles span"):
            validate_rising_sync_cycles(irregular, 1000.0)

    def test_eighteen_hz_sync_tolerates_whole_sample_period_quantization(self):
        # 18 Hz at 1000 SPS is 55.56 samples per cycle: the firmware's edges
        # land on the sample grid as alternating 55/56-sample periods
        # (18.18 / 17.86 Hz individually), which TP443's +/-0.18 Hz would
        # reject cycle by cycle. The mean over the validation cycles is what
        # is judged; single cycles get a one-sample allowance.
        sync: list[float] = []
        for period in (56, 55, 56, 55, 56, 56, 55):
            sync.extend([1.0] * 11 + [0.0] * (period - 11))
        result = validate_rising_sync_cycles(
            sync, 1000.0, expected_frequency_hz=18.0, frequency_tolerance_hz=0.18
        )
        self.assertEqual(result.cycles_validated, 5)
        self.assertAlmostEqual(result.measured_frequency_hz, 1000.0 / 55.6, places=2)

        # A genuinely wrong cadence is still caught by the mean (17 Hz is
        # 58.8 samples per cycle: 0.99 Hz off, far beyond 0.18 + 0.32).
        wrong: list[float] = []
        for period in (59, 59, 59, 58, 59, 59, 59):
            wrong.extend([1.0] * 12 + [0.0] * (period - 12))
        with self.assertRaisesRegex(SyncValidationError, "expected 18.00"):
            validate_rising_sync_cycles(
                wrong, 1000.0, expected_frequency_hz=18.0, frequency_tolerance_hz=0.18
            )

        # A mean that sits just outside the tolerance fails even when each
        # cycle is within its own quantized allowance: 57-sample periods are
        # 17.54 Hz, 0.46 Hz off.
        slow: list[float] = []
        for _cycle in range(7):
            slow.extend([1.0] * 11 + [0.0] * 46)
        with self.assertRaisesRegex(SyncValidationError, "PWM sync frequency is 17.544 Hz"):
            validate_rising_sync_cycles(
                slow, 1000.0, expected_frequency_hz=18.0, frequency_tolerance_hz=0.18
            )

    def test_block_sync_keeps_every_kth_rising_edge(self):
        # Four 10-sample cycles, 3 samples high each.
        sync = [0.0] * 3
        for _cycle in range(4):
            sync.extend([1.0] * 3 + [0.0] * 7)
        from stability_analysis import block_sync_cycles, rising_edge_indices

        self.assertEqual(block_sync_cycles(sync, 1), sync)
        blocked = block_sync_cycles(sync, 2)
        self.assertEqual(len(blocked), len(sync))
        # Rising edges at 3, 13, 23, 33 -> blocks start at 3 and 23; each
        # block is high for its first real cycle only.
        self.assertEqual(rising_edge_indices(blocked), (3, 23))
        self.assertEqual(blocked[3:13], [1.0] * 10)
        self.assertEqual(blocked[13:23], [0.0] * 10)
        self.assertEqual(blocked[23:33], [1.0] * 10)
        self.assertEqual(blocked[33:], [0.0] * len(blocked[33:]))
        self.assertEqual(complete_cycle_segments(blocked), ((3, 23),))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            block_sync_cycles(sync, 0)


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


class StrictRetryPolicyTests(unittest.TestCase):
    settings = StabilitySettings(consecutive_deltas_required=10)

    def analyze(self, peaks_v: list[float], **kwargs):
        waveform, sync = waveform_for_peaks(peaks_v)
        return analyze_stability(
            waveform,
            sync,
            sample_rate_hz=200.0,
            settings=self.settings,
            enforce_measurement_stability=True,
            max_measurement_attempts=3,
            measurement_cycles_required=20,
            **kwargs,
        )

    def test_first_attempt_requires_ten_stable_deltas_then_twenty_cycles(self):
        analysis = self.analyze([0.7000] * 31)

        self.assertTrue(analysis.report.measurement_complete)
        self.assertEqual(analysis.report.measurement_attempt, 1)
        self.assertEqual(analysis.report.measurement_failures, 0)
        self.assertEqual(analysis.report.active_confirmation_count, 10)
        self.assertEqual(analysis.report.measurement_cycles_required, 20)
        self.assertEqual(
            [cycle.cycle_number for cycle in analysis.measurement_cycles],
            list(range(12, 32)),
        )

    def test_first_kick_restarts_the_same_ten_twenty_windows(self):
        peaks = [0.7000] * 15 + [0.7002] + [0.7002] * 30
        analysis = self.analyze(peaks)

        self.assertTrue(analysis.report.measurement_complete)
        self.assertEqual(analysis.report.measurement_attempt, 2)
        self.assertEqual(analysis.report.measurement_failures, 1)
        self.assertEqual(analysis.report.active_confirmation_count, 10)
        self.assertEqual(analysis.report.stabilization_cycle, 26)
        self.assertEqual(analysis.report.measurement_cycles_required, 20)
        self.assertEqual(
            [cycle.cycle_number for cycle in analysis.measurement_cycles],
            list(range(27, 47)),
        )

    def test_second_kick_uses_the_same_ten_twenty_windows_for_attempt_three(self):
        peaks = (
            [0.7000] * 15
            + [0.7002]
            + [0.7002] * 14
            + [0.7004]
            + [0.7004] * 30
        )
        analysis = self.analyze(peaks)

        self.assertTrue(analysis.report.measurement_complete)
        self.assertEqual(analysis.report.measurement_attempt, 3)
        self.assertEqual(analysis.report.measurement_failures, 2)
        self.assertEqual(analysis.report.active_confirmation_count, 10)
        self.assertEqual(analysis.report.active_confirmation_run_length, 10)
        self.assertEqual(analysis.report.measurement_cycles_required, 20)
        self.assertEqual(analysis.report.stabilization_cycle, 41)
        self.assertEqual(
            [cycle.cycle_number for cycle in analysis.measurement_cycles],
            list(range(42, 62)),
        )

    def test_third_measurement_kick_is_immediately_unstable(self):
        peaks = (
            [0.7000] * 11
            + [0.7002]
            + [0.7002] * 10
            + [0.7004]
            + [0.7004] * 10
            + [0.7006]
        )
        analysis = self.analyze(peaks)

        self.assertFalse(analysis.report.measurement_complete)
        self.assertTrue(analysis.report.unstable)
        self.assertFalse(analysis.report.timed_out)
        self.assertEqual(analysis.report.phase, "unstable")
        self.assertEqual(analysis.report.measurement_attempt, 3)
        self.assertEqual(analysis.report.measurement_failures, 3)
        self.assertEqual(analysis.measurement_cycles, ())
        self.assertIn("attempt 3 of 3", analysis.report.unstable_reason)

    def test_retry_requalification_still_obeys_twenty_second_deadline(self):
        peaks = [0.7000] * 11 + [0.7002] + [0.7002] * 4
        analysis = self.analyze(peaks, stability_deadline_s=1.3)

        self.assertTrue(analysis.report.unstable)
        self.assertTrue(analysis.report.timed_out)
        self.assertEqual(analysis.report.measurement_attempt, 2)
        self.assertEqual(analysis.report.active_confirmation_count, 10)
        self.assertIn("within 1.3 s", analysis.report.unstable_reason)


def noise_windows(pp_per_window_v, *, offset_v=1.2, window_samples=100):
    """Emitter-off waveform whose window pk-pk follows ``pp_per_window_v``."""

    waveform: list[float] = []
    for pp_v in pp_per_window_v:
        amplitude = pp_v / 2.0
        for index in range(window_samples):
            waveform.append(offset_v + (amplitude if index % 2 == 0 else -amplitude))
    return waveform


class NoiseAnalysisTests(unittest.TestCase):
    RATE = 100.0        # 100 samples per 1 s window keeps fixtures small
    WINDOW_S = 1.0

    def analyze(self, pp_per_window_v, **kwargs):
        waveform = noise_windows(pp_per_window_v)
        kwargs.setdefault("window_s", self.WINDOW_S)
        kwargs.setdefault("threshold_mv", 300.0)
        kwargs.setdefault("max_over_fraction", 0.20)
        kwargs.setdefault("clip_limit_v", 4.9)
        return analyze_noise_capture(waveform, self.RATE, **kwargs)

    def test_fixed_windows_drop_the_partial_tail(self):
        self.assertEqual(fixed_window_segments(10, 3), ((0, 3), (3, 6), (6, 9)))
        self.assertEqual(fixed_window_segments(9, 3), ((0, 3), (3, 6), (6, 9)))
        self.assertEqual(fixed_window_segments(2, 3), ())
        with self.assertRaises(ValueError):
            fixed_window_segments(10, 0)

    def test_window_peak_to_peak_is_reported_in_millivolts(self):
        waveform = noise_windows([0.100, 0.250], window_samples=100)
        peaks = window_peak_to_peak_mv(waveform, fixed_window_segments(len(waveform), 100))
        self.assertEqual(len(peaks), 2)
        self.assertAlmostEqual(peaks[0], 100.0, places=6)
        self.assertAlmostEqual(peaks[1], 250.0, places=6)

    def test_exactly_twenty_percent_over_passes_and_one_more_fails(self):
        # 4 of 20 windows over 300 mV = exactly the allowed 20%.
        passing = self.analyze([0.400] * 4 + [0.050] * 16)
        self.assertTrue(passing.passed)
        self.assertEqual(passing.windows_total, 20)
        self.assertEqual(passing.windows_over, 4)
        self.assertAlmostEqual(passing.worst_pp_mv, 400.0, places=6)

        failing = self.analyze([0.400] * 5 + [0.050] * 15)
        self.assertFalse(failing.passed)
        self.assertEqual(failing.windows_over, 5)

    def test_pp_exactly_at_the_limit_is_not_over(self):
        analysis = self.analyze([0.300] * 20)
        self.assertTrue(analysis.passed)
        self.assertEqual(analysis.windows_over, 0)

    def test_clipped_window_counts_as_over_even_with_small_pp(self):
        # One window rides at the positive rail: tiny pk-pk, but the true
        # excursion is unknowable, so it must count against the sensor.
        quiet = noise_windows([0.050] * 19)
        clipped = [4.95] * 100
        analysis = analyze_noise_capture(
            clipped + quiet,
            self.RATE,
            window_s=self.WINDOW_S,
            threshold_mv=300.0,
            max_over_fraction=0.0,
            clip_limit_v=4.9,
        )
        self.assertFalse(analysis.passed)
        self.assertEqual(analysis.windows_over, 1)
        self.assertEqual(analysis.clipped_windows, 1)

    def test_zero_complete_windows_raises(self):
        with self.assertRaisesRegex(ValueError, "no complete window"):
            analyze_noise_capture([1.2] * 5, self.RATE, window_s=self.WINDOW_S)


class BandLimitedNoiseAnalysisTests(unittest.TestCase):
    """decimate_boxcar + the band-limited pk-pk rule with raw clip checks."""

    def test_decimate_boxcar_averages_blocks_and_drops_the_tail(self):
        waveform = [0.0, 2.0, 4.0, 6.0, 1.0, 3.0, 9.9]  # tail sample dropped
        self.assertEqual(decimate_boxcar(waveform, 2), (1.0, 5.0, 2.0))
        self.assertEqual(decimate_boxcar([1.5, 2.5], 1), (1.5, 2.5))
        with self.assertRaises(ValueError):
            decimate_boxcar([1.0], 0)

    def test_high_frequency_noise_is_removed_low_frequency_kept(self):
        # 1000 SPS, 20:1 decimation. A ±200 µV square alternating every
        # sample (500 Hz) is deep in the FIR's stopband (>= 60 dB) and all
        # but vanishes — only the odd-reflection edge padding leaves a small
        # bounded artifact in the first/last windows, well under the limit.
        # A ±200 µV window-centered slow square survives through decimation
        # AND the per-window detrend; the FIR's flat passband keeps its
        # in-band harmonics at full strength (the old boxcar drooped them),
        # so with band-edge Gibbs ripple it reads slightly ABOVE its raw
        # 0.400 mV pk-pk rather than exactly at it.
        fast = [1.2 + (0.0002 if index % 2 == 0 else -0.0002) for index in range(4000)]
        slow = [
            1.2
            + (
                0.0002
                if (index % 1000 < 250 or index % 1000 >= 750)
                else -0.0002
            )
            for index in range(4000)
        ]

        fast_analysis, _filtered, rate = analyze_noise_capture_band_limited(
            fast,
            1000.0,
            decimation_factor=20,
            threshold_mv=0.075,
            max_over_fraction=0.20,
        )
        slow_analysis, _filtered, _rate = analyze_noise_capture_band_limited(
            slow,
            1000.0,
            decimation_factor=20,
            threshold_mv=0.075,
            max_over_fraction=0.20,
        )

        self.assertEqual(rate, 50.0)
        self.assertTrue(fast_analysis.passed)
        self.assertLess(fast_analysis.worst_pp_mv, 0.075)
        self.assertFalse(slow_analysis.passed)
        self.assertGreaterEqual(slow_analysis.worst_pp_mv, 0.400)
        self.assertLess(slow_analysis.worst_pp_mv, 0.500)

    def test_antialias_decimation_rejects_folding_frequencies(self):
        # THE aliasing fix (2026-08-20). After 20:1 decimation the new
        # Nyquist is 25 Hz; the old boxcar's -13 dB sidelobes let 60 Hz
        # mains fold to 10 Hz at only -16 dB and read as part noise
        # (measured at 41% of the in-band signal on an interference-heavy
        # bench capture). The FIR must attenuate foldable tones >= 60 dB
        # while passing the part's real 0.5-5 Hz noise band untouched, and
        # a linear settling ramp must come through exactly (the odd-
        # reflection padding) so the per-window detrend can remove it.
        import math as m

        rate, factor = 1000.0, 20
        n = 20000

        def tone_pp_after(freq_hz):
            tone = [1.2 + 0.001 * m.sin(2 * m.pi * freq_hz * i / rate)
                    for i in range(n)]
            filtered = decimate_antialiased(tone, factor)
            core = filtered[100:-100]  # steady state, away from edges
            return max(core) - min(core)

        # In-band tones pass at full amplitude (2 mV pk-pk in -> ~2 mV out).
        for freq in (1.0, 3.0, 10.0):
            self.assertAlmostEqual(tone_pp_after(freq), 0.002, delta=0.0001)
        # Foldable tones (alias targets 10 Hz / 20 Hz) are crushed >= 60 dB:
        # 2 mV in -> <= 2 µV out. Under the boxcar, 60 Hz kept ~16% (314 µV).
        for freq in (40.0, 60.0, 120.0):
            self.assertLess(tone_pp_after(freq), 0.000002)
        # Unity DC gain and exact linear-ramp transparency at the edges.
        ramp = [1.2 + 0.0005 * i / rate for i in range(n)]
        filtered = decimate_antialiased(ramp, factor)
        worst = max(
            abs(value - (1.2 + 0.0005 * (k * factor + factor // 2) / rate))
            for k, value in enumerate(filtered)
        )
        self.assertLess(worst, 1e-12)
        # Same output timeline as the boxcar it replaced.
        self.assertEqual(len(filtered), n // factor)

    def test_edge_context_seats_the_filter_and_no_context_stays_identical(self):
        # 2026-08-31: reflection padding is exact for a linear trend but
        # wrong for oscillatory content, so out-of-band interference leaked
        # into the FIRST and LAST judged windows at only ~11-21 dB (vs
        # >= 60 dB interior). Real neighbour samples as filter history must
        # (1) leave the no-context output bit-identical (archived captures
        # replay unchanged), (2) crush an out-of-band tone at the edges
        # like the interior, (3) change nothing away from the edges, and
        # (4) keep a settling ramp exact when its context continues it.
        import math as m

        rate, factor = 1000.0, 20
        edge = antialias_edge_context_samples(factor)
        self.assertEqual(edge, 310)  # the production 621-tap design
        n = 20000

        def window_pp(window):
            return max(window) - min(window)

        worst_plain = 0.0
        worst_seated = 0.0
        for phase in (0.0, 1.3, 2.9, 4.4):
            full = [
                1.2 + 0.001 * m.sin(2 * m.pi * 60.0 * i / rate + phase)
                for i in range(-edge, n + edge)
            ]
            left, capture, right = (
                full[:edge], full[edge:edge + n], full[edge + n:]
            )
            plain = decimate_antialiased(capture, factor)
            seated = decimate_antialiased(
                capture, factor, left_context_v=left, right_context_v=right
            )
            self.assertEqual(len(seated), len(plain))
            # No-context call forms are all bit-identical (the replay path).
            self.assertEqual(
                plain,
                decimate_antialiased(
                    capture, factor, left_context_v=None, right_context_v=None
                ),
            )
            self.assertEqual(
                plain,
                decimate_antialiased(
                    capture, factor, left_context_v=[], right_context_v=[]
                ),
            )
            # Context only changes outputs whose FIR footprint reaches
            # beyond the capture: the interior is untouched.
            interior = slice(edge // factor + 1, -(edge // factor) - 1)
            self.assertEqual(plain[interior], seated[interior])
            worst_plain = max(
                worst_plain, window_pp(plain[:50]), window_pp(plain[-50:])
            )
            worst_seated = max(
                worst_seated, window_pp(seated[:50]), window_pp(seated[-50:])
            )
        # The defect: reflection leaks the 60 Hz tone into edge windows...
        self.assertGreater(worst_plain, 0.00002)
        # ...and real context restores interior-grade (>= 60 dB) rejection.
        self.assertLess(worst_seated, 0.000002)

        # A settling ramp whose context continues the line is still exact.
        ramp_full = [1.2 + 0.0005 * i / rate for i in range(-edge, n + edge)]
        filtered = decimate_antialiased(
            ramp_full[edge:edge + n],
            factor,
            left_context_v=ramp_full[:edge],
            right_context_v=ramp_full[edge + n:],
        )
        worst = max(
            abs(value - (1.2 + 0.0005 * (k * factor + factor // 2) / rate))
            for k, value in enumerate(filtered)
        )
        self.assertLess(worst, 1e-12)

    def test_detrend_removes_each_windows_own_line(self):
        # A pure line becomes ~zero; a line plus a center-symmetric square
        # keeps the square's full pk-pk (the fit sees zero extra slope).
        line = [0.5 + 0.01 * index for index in range(100)]
        segments = ((0, 50), (50, 100))
        residual = detrend_window_segments(line, segments)
        self.assertLess(max(abs(value) for value in residual), 1e-9)

        square = [
            value
            + (0.001 if (index % 50 < 12 or index % 50 >= 38) else -0.001)
            for index, value in enumerate(line)
        ]
        residual = detrend_window_segments(square, segments)
        self.assertAlmostEqual(
            max(residual) - min(residual), 0.002, delta=1e-6
        )

    def test_settling_drift_passes_only_with_window_detrending(self):
        # 0.5 mV/s of residual DC settling and no real noise: the slope
        # alone puts ~0.5 mV of pk-pk into every raw window. With each
        # window judged against its own baseline the part passes; without
        # detrending it would fail every window.
        drift = [1.2 + 0.0005 * index / 1000.0 for index in range(20000)]
        detrended, _trace, _rate = analyze_noise_capture_band_limited(
            drift,
            1000.0,
            decimation_factor=20,
            threshold_mv=300.0 / 700.0,
            max_over_fraction=0.20,
        )
        raw_rule, _trace, _rate = analyze_noise_capture_band_limited(
            drift,
            1000.0,
            decimation_factor=20,
            threshold_mv=300.0 / 700.0,
            max_over_fraction=0.20,
            detrend_windows=False,
        )
        self.assertTrue(detrended.passed)
        self.assertLess(detrended.worst_pp_mv, 0.01)
        self.assertFalse(raw_rule.passed)
        self.assertGreater(raw_rule.worst_pp_mv, 0.4)

    def test_raw_clipping_still_fails_a_window_the_average_would_hide(self):
        # One window rides at the +4.95 V rail: the filtered trace is a
        # quiet DC level there (tiny filtered pk-pk), but the raw samples
        # prove the input railed, so the window must count as over-limit.
        # The 3.75 V rail-to-quiet step also rings through the anti-alias
        # FIR into the neighboring window (the transition spans ~0.3 s each
        # side), so that window's filtered pk-pk is over as well — honest
        # contamination from a railed input, and irrelevant in production
        # because any clipped window already fails the capture on its own.
        quiet = [1.2] * 19_000
        clipped = [4.95] * 1000
        analysis, filtered, _rate = analyze_noise_capture_band_limited(
            clipped + quiet,
            1000.0,
            decimation_factor=20,
            threshold_mv=0.075,
            max_over_fraction=0.0,
            clip_limit_v=4.9,
        )
        self.assertEqual(len(filtered), 1000)
        self.assertFalse(analysis.passed)
        self.assertEqual(analysis.clipped_windows, 1)
        self.assertEqual(analysis.windows_over, 2)


if __name__ == "__main__":
    unittest.main()
