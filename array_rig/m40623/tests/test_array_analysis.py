"""Tests for array_analysis: golden parity of the numpy noise port, offset classes, verdicts.

The oracle is ``golden_noise_reference.py`` in this directory - a frozen
verbatim copy of the 405 M22 build's pure-Python noise functions. A drift
test compares that copy with the live single-rig module so a change on
either side is noticed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
MODEL_DIR = TESTS_DIR.parent
REPO_ROOT = MODEL_DIR.parents[1]
for entry in (str(MODEL_DIR), str(TESTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import array_analysis as aa  # noqa: E402
import golden_noise_reference as golden  # noqa: E402

RATE = 1000.0
FACTOR = 20
WINDOW = 50  # filtered samples per 1 s window at 50 SPS


def positions(count: int) -> list[str]:
    return [aa_position(i) for i in range(count)]


def aa_position(index: int) -> str:
    return f"{index // 10 + 1}-{index % 10 + 1}"


def random_tray(channels: int, samples: int, *, seed: int = 3, offset: float = 0.7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    walk = np.cumsum(rng.normal(0.0, 3e-6, (channels, samples)), axis=1)
    white = rng.normal(0.0, 5e-5, (channels, samples))
    return offset + walk + white


def golden_decimate(raw: np.ndarray, left=None, right=None) -> np.ndarray:
    return np.array([
        golden.decimate_antialiased(
            raw[c], FACTOR,
            left_context_v=None if left is None else left[c],
            right_context_v=None if right is None else right[c],
        )
        for c in range(raw.shape[0])
    ])


def synthetic_channel(*, windows: int, noisy_windows: int = 0, quiet_pp_v: float = 20e-6, noisy_pp_v: float = 2e-3,
                      offset_v: float = 0.7, clipped_window: int | None = None, drift_v_per_s: float = 0.0) -> np.ndarray:
    """One 1000 SPS channel whose per-window pk-pk is controlled (port of the 405 fixture).

    Each window is a square wave that is high in its first and last quarter
    so the per-window least-squares detrend fits a zero slope and keeps the
    full excursion.
    """

    samples_per_window = int(RATE)
    out = np.empty(windows * samples_per_window)
    for w in range(windows):
        pp = noisy_pp_v if w < noisy_windows else quiet_pp_v
        block = np.full(samples_per_window, offset_v - pp / 2)
        quarter = samples_per_window // 4
        block[:quarter] += pp
        block[-quarter:] += pp
        if clipped_window is not None and w == clipped_window:
            block[quarter + 5 : quarter + 15] = 4.95
        out[w * samples_per_window : (w + 1) * samples_per_window] = block
    if drift_v_per_s:
        out += drift_v_per_s * np.arange(out.size) / RATE
    return out


# ----------------------------------------------------------------------
# Golden parity
# ----------------------------------------------------------------------
class GoldenDriftTests(unittest.TestCase):
    LIVE = REPO_ROOT / "single_detector_rig" / "m405m22" / "stability_analysis.py"

    def test_frozen_copy_matches_the_live_405_module(self):
        if not self.LIVE.is_file():
            self.skipTest("single_detector_rig is not present in this checkout")
        live = self.LIVE.read_text(encoding="utf-8").replace("\r\n", "\n")
        frozen = (TESTS_DIR / "golden_noise_reference.py").read_text(encoding="utf-8").replace("\r\n", "\n")
        start = live.index("def fixed_window_segments(")
        end = live.index("__all__ = [")
        section = live[start:end].rstrip()
        self.assertIn(section, frozen, "golden_noise_reference.py no longer matches the live 405 noise section - "
                                        "re-freeze it deliberately (and re-run the parity tests) or restore it")
        threshold_start = live.index("def _at_or_below_threshold(")
        threshold_end = live.index("def _at_or_before_deadline(")
        self.assertIn(live[threshold_start:threshold_end].rstrip(), frozen)


class FirDesignTests(unittest.TestCase):
    def test_taps_identical_to_golden(self):
        ours = aa.design_antialias_lowpass_fir(FACTOR)
        theirs = np.array(golden.design_antialias_lowpass_fir(FACTOR))
        self.assertEqual(len(ours), 621)
        np.testing.assert_array_equal(ours, theirs)
        np.testing.assert_allclose(ours, ours[::-1])
        self.assertAlmostEqual(float(ours.sum()), 1.0, places=15)

    def test_capped_taps_identical_to_golden(self):
        for cap in (399, 400, 3, 4):
            ours = aa.design_antialias_lowpass_fir(FACTOR, max_taps=cap)
            theirs = np.array(golden.design_antialias_lowpass_fir(FACTOR, max_taps=cap))
            np.testing.assert_array_equal(ours, theirs)

    def test_edge_context_is_310(self):
        self.assertEqual(aa.antialias_edge_context_samples(FACTOR), 310)
        self.assertEqual(aa.antialias_edge_context_samples(FACTOR), golden.antialias_edge_context_samples(FACTOR))

    def test_factor_validation(self):
        with self.assertRaises(ValueError):
            aa.design_antialias_lowpass_fir(1)
        with self.assertRaises(ValueError):
            aa.design_antialias_lowpass_fir(True)


class DecimationParityTests(unittest.TestCase):
    def setUp(self):
        self.raw = random_tray(5, 3000)
        rng = np.random.default_rng(11)
        self.left = 0.7 + rng.normal(0.0, 5e-5, (5, 400))
        self.right = 0.7 + rng.normal(0.0, 5e-5, (5, 400))

    def test_no_context_matches_golden(self):
        ours = aa.decimate_antialiased_multi(self.raw, FACTOR)
        np.testing.assert_allclose(ours, golden_decimate(self.raw), rtol=1e-9, atol=1e-12)
        self.assertEqual(ours.shape, (5, 150))

    def test_short_context_matches_golden(self):
        left, right = self.left[:, -100:], self.right[:, :100]
        ours = aa.decimate_antialiased_multi(self.raw, FACTOR, left_context=left, right_context=right)
        np.testing.assert_allclose(ours, golden_decimate(self.raw, left, right), rtol=1e-9, atol=1e-12)

    def test_full_context_matches_golden_and_differs_from_reflection(self):
        ours = aa.decimate_antialiased_multi(self.raw, FACTOR, left_context=self.left, right_context=self.right)
        np.testing.assert_allclose(ours, golden_decimate(self.raw, self.left, self.right), rtol=1e-9, atol=1e-12)
        no_context = aa.decimate_antialiased_multi(self.raw, FACTOR)
        self.assertFalse(np.allclose(ours[:, 0], no_context[:, 0], atol=1e-15))
        np.testing.assert_allclose(ours[:, 40:110], no_context[:, 40:110])  # the middle is context-independent

    def test_short_input_tap_cap_matches_golden(self):
        short = self.raw[:, :200]
        ours = aa.decimate_antialiased_multi(short, FACTOR)
        np.testing.assert_allclose(ours, golden_decimate(short), rtol=1e-9, atol=1e-12)
        self.assertEqual(ours.shape, (5, 10))

    def test_output_is_centred_on_each_block(self):
        raw = np.zeros((1, 2000))
        raw[0, 7 * FACTOR + FACTOR // 2] = 1.0  # impulse at the centre of block 7
        out = aa.decimate_antialiased_multi(raw, FACTOR)[0]
        self.assertEqual(int(np.argmax(out)), 7)

    def test_factor_one_is_pass_through_and_too_short_is_empty(self):
        raw = random_tray(2, 30)
        np.testing.assert_array_equal(aa.decimate_antialiased_multi(raw, 1), raw)
        self.assertEqual(aa.decimate_antialiased_multi(raw[:, :10], FACTOR).shape, (2, 0))

    def test_one_dimensional_input_is_accepted(self):
        out = aa.decimate_antialiased_multi(self.raw[0], FACTOR)
        self.assertEqual(out.shape, (1, 150))

    def test_non_finite_and_bad_context_rejected(self):
        bad = self.raw.copy()
        bad[1, 5] = np.nan
        with self.assertRaises(ValueError):
            aa.decimate_antialiased_multi(bad, FACTOR)
        with self.assertRaises(ValueError):
            aa.decimate_antialiased_multi(self.raw, FACTOR, left_context=self.left[:3])


class DetrendAndPeakParityTests(unittest.TestCase):
    def test_detrend_matches_golden(self):
        filtered = aa.decimate_antialiased_multi(random_tray(4, 5000), FACTOR)
        segments = golden.fixed_window_segments(filtered.shape[1], WINDOW)
        ours = aa.detrend_window_segments_multi(filtered, WINDOW)
        theirs = np.array([golden.detrend_window_segments(filtered[c], segments) for c in range(4)])
        np.testing.assert_allclose(ours, theirs, rtol=1e-9, atol=1e-15)
        self.assertEqual(ours.shape, (4, 250))

    def test_detrend_removes_each_windows_own_line(self):
        t = np.arange(150, dtype=float)
        line = np.stack([0.5 + 0.01 * t, 1.0 - 0.02 * t])
        residual = aa.detrend_window_segments_multi(line, WINDOW)
        np.testing.assert_allclose(residual, 0.0, atol=1e-12)

    def test_window_pp_matches_golden(self):
        judged = random_tray(3, 500, seed=9)
        segments = golden.fixed_window_segments(500, WINDOW)
        ours = aa.window_peak_to_peak_mv_multi(judged, WINDOW)
        theirs = np.array([golden.window_peak_to_peak_mv(judged[c], segments) for c in range(3)])
        np.testing.assert_allclose(ours, theirs, rtol=1e-12)
        self.assertEqual(ours.shape, (3, 10))

    def test_fixed_window_segments_drop_partial_tail(self):
        self.assertEqual(aa.fixed_window_segments(120, 50), ((0, 50), (50, 100)))
        self.assertEqual(aa.fixed_window_segments(120, 50), golden.fixed_window_segments(120, 50))
        with self.assertRaises(ValueError):
            aa.fixed_window_segments(120, 0)

    def test_at_or_below_threshold_boundary(self):
        self.assertTrue(aa.at_or_below_threshold(0.1, 0.1))
        self.assertTrue(aa.at_or_below_threshold(0.1 + 1e-15, 0.1))
        self.assertFalse(aa.at_or_below_threshold(0.1 + 1e-9, 0.1))
        np.testing.assert_array_equal(aa.at_or_below_threshold(np.array([0.05, 0.1, 0.2]), 0.1), [True, True, False])


class TrayNoiseParityTests(unittest.TestCase):
    HIGH_MV = 0.43
    LOW_MV = 0.005  # 5 uV: under the 20 uV quiet fixture, over the 1 uV 'dead' fixture

    def build_tray(self):
        channels = [
            synthetic_channel(windows=20),                                   # quiet
            synthetic_channel(windows=20, noisy_windows=3),                  # 3/20 over = 15 % -> passes
            synthetic_channel(windows=20, noisy_windows=4),                  # 4/20 over -> HIGH
            synthetic_channel(windows=20, clipped_window=5),                 # one clipped window
            synthetic_channel(windows=20, drift_v_per_s=2e-3),               # settling drift, quiet
            synthetic_channel(windows=20, quiet_pp_v=1e-6),                  # nearly dead -> LOW
        ]
        return np.stack(channels)

    def test_matches_golden_per_channel(self):
        raw = self.build_tray()
        limits = aa.NoiseLimits(low_mv=self.LOW_MV, high_mv=self.HIGH_MV, max_over_fraction=0.15, clip_limit_v=4.9)
        results, judged, rate = aa.analyze_tray_noise(raw, RATE, positions=positions(6), limits=limits)
        self.assertEqual(rate, 50.0)
        self.assertEqual(judged.shape, (6, 1000))
        expected_verdicts = [
            aa.NoiseVerdict.PASS, aa.NoiseVerdict.PASS, aa.NoiseVerdict.HIGH,
            aa.NoiseVerdict.PASS, aa.NoiseVerdict.PASS, aa.NoiseVerdict.LOW,
        ]
        for c, result in enumerate(results):
            ref, _, _ = golden.analyze_noise_capture_band_limited(
                raw[c], RATE, decimation_factor=FACTOR, threshold_mv=self.HIGH_MV, max_over_fraction=0.15, clip_limit_v=4.9,
            )
            self.assertEqual(result.windows_total, ref.windows_total)
            self.assertEqual(result.windows_over_high, ref.windows_over)
            self.assertAlmostEqual(result.over_fraction, ref.over_fraction)
            self.assertAlmostEqual(result.worst_pp_mv, ref.worst_pp_mv, places=9)
            self.assertAlmostEqual(result.median_pp_mv, ref.median_pp_mv, places=9)
            self.assertEqual(result.clipped_windows, ref.clipped_windows)
            np.testing.assert_allclose(result.window_pp_mv, ref.window_pp_mv, rtol=1e-9)
            self.assertEqual(result.verdict is aa.NoiseVerdict.HIGH, not ref.passed, f"channel {c}")
            self.assertIs(result.verdict, expected_verdicts[c], f"channel {c}")
            self.assertEqual(result.position, aa_position(c))
        self.assertEqual(results[3].clipped_windows, 1)
        self.assertEqual(results[1].windows_over_high, 3)
        self.assertEqual(results[2].windows_over_high, 4)

    def test_limits_none_never_fails(self):
        raw = self.build_tray()
        results, _, _ = aa.analyze_tray_noise(raw, RATE, positions=positions(6), limits=aa.NoiseLimits())
        for result in results:
            self.assertIs(result.verdict, aa.NoiseVerdict.NO_LIMIT)
            self.assertIsNone(result.windows_over_high)
            self.assertIsNone(result.over_fraction)
            self.assertGreater(result.worst_pp_mv, 0.0)
        self.assertEqual(results[3].clipped_windows, 1)  # still reported

    def test_high_only_and_low_only_limits(self):
        raw = self.build_tray()
        high_only = aa.NoiseLimits(low_mv=None, high_mv=self.HIGH_MV)
        results, _, _ = aa.analyze_tray_noise(raw, RATE, positions=positions(6), limits=high_only)
        self.assertIs(results[5].verdict, aa.NoiseVerdict.PASS)  # low side not judged
        self.assertIs(results[2].verdict, aa.NoiseVerdict.HIGH)
        low_only = aa.NoiseLimits(low_mv=self.LOW_MV, high_mv=None)
        results, _, _ = aa.analyze_tray_noise(raw, RATE, positions=positions(6), limits=low_only)
        self.assertIs(results[2].verdict, aa.NoiseVerdict.PASS)
        self.assertIsNone(results[2].windows_over_high)
        self.assertIs(results[5].verdict, aa.NoiseVerdict.LOW)

    def test_low_rule_uses_the_median_so_one_bang_does_not_revive_a_dead_part(self):
        dead = synthetic_channel(windows=20, quiet_pp_v=1e-6)
        dead[3000:3050] += 2e-3  # a single bang in window 3
        results, _, _ = aa.analyze_tray_noise(
            dead[None, :], RATE, positions=["1-1"], limits=aa.NoiseLimits(low_mv=self.LOW_MV, high_mv=self.HIGH_MV)
        )
        self.assertIs(results[0].verdict, aa.NoiseVerdict.LOW)
        self.assertGreater(results[0].worst_pp_mv, self.LOW_MV)

    def test_high_takes_precedence_over_low(self):
        noisy_dead = synthetic_channel(windows=20, noisy_windows=5, quiet_pp_v=1e-6)
        results, _, _ = aa.analyze_tray_noise(
            noisy_dead[None, :], RATE, positions=["1-1"], limits=aa.NoiseLimits(low_mv=self.LOW_MV, high_mv=self.HIGH_MV)
        )
        self.assertIs(results[0].verdict, aa.NoiseVerdict.HIGH)

    def test_contexts_are_history_only(self):
        raw = self.build_tray()
        rng = np.random.default_rng(5)
        left = 0.7 + rng.normal(0.0, 2e-5, (6, 310))
        right = 0.7 + rng.normal(0.0, 2e-5, (6, 310))
        with_ctx, judged_ctx, _ = aa.analyze_tray_noise(raw, RATE, positions=positions(6), left_context=left, right_context=right)
        without, judged, _ = aa.analyze_tray_noise(raw, RATE, positions=positions(6))
        self.assertEqual(judged_ctx.shape, judged.shape)
        self.assertEqual(with_ctx[0].windows_total, without[0].windows_total)

    def test_band_limited_window_pp_helper(self):
        raw = self.build_tray()
        pp = aa.band_limited_window_pp_mv(raw, RATE)
        self.assertEqual(pp.shape, (6, 20))
        results, _, _ = aa.analyze_tray_noise(raw, RATE, positions=positions(6))
        np.testing.assert_allclose(pp[2], results[2].window_pp_mv)

    def test_validation(self):
        raw = self.build_tray()
        with self.assertRaises(ValueError):
            aa.analyze_tray_noise(raw, RATE, positions=positions(5))
        with self.assertRaises(ValueError):
            aa.analyze_tray_noise(raw[:, :500], RATE, positions=positions(6))
        with self.assertRaises(ValueError):
            aa.analyze_tray_noise(raw, RATE, positions=positions(6), limits=aa.NoiseLimits(max_over_fraction=1.0))
        with self.assertRaises(ValueError):
            aa.analyze_tray_noise(raw, 0.0, positions=positions(6))

    def test_full_tray_runs(self):
        raw = random_tray(50, 20_000, seed=2)
        results, judged, _ = aa.analyze_tray_noise(raw, RATE, positions=positions(50))
        self.assertEqual(len(results), 50)
        self.assertEqual(judged.shape, (50, 1000))
        self.assertEqual(results[49].channel, 49)
        self.assertEqual(results[49].position, "5-10")


# ----------------------------------------------------------------------
# Offsets and verdicts
# ----------------------------------------------------------------------
class OffsetClassificationTests(unittest.TestCase):
    def classify(self, volts, occupancy=aa.Occupancy.LOADED):
        return aa.classify_offset(volts, occupancy=occupancy)

    def test_tp120_band_and_floors(self):
        L = aa.OffsetClass
        self.assertIs(self.classify(0.7), L.OK)
        self.assertIs(self.classify(1.2), L.OK)
        self.assertIs(self.classify(1.2 + 1e-13), L.OK)      # exact boundary forgiven
        self.assertIs(self.classify(1.2000001), L.HO)
        self.assertIs(self.classify(1.25), L.HO)
        self.assertIs(self.classify(4.9), L.HO_RAILED)
        self.assertIs(self.classify(4.95), L.HO_RAILED)
        self.assertIs(self.classify(0.3), L.OK)
        self.assertIs(self.classify(0.2999), L.LO)
        self.assertIs(self.classify(0.2), L.LO)
        self.assertIs(self.classify(0.051), L.LO)
        self.assertIs(self.classify(0.05), L.LO)
        self.assertIs(self.classify(0.049), L.DEAD)
        self.assertIs(self.classify(0.01), L.DEAD)
        self.assertIs(self.classify(0.01, aa.Occupancy.EMPTY), L.EMPTY)
        self.assertIs(self.classify(1.5, aa.Occupancy.EMPTY), L.EMPTY)
        self.assertIs(self.classify(0.01, aa.Occupancy.UNKNOWN), L.LO)  # unknown sockets are not called dead

    def test_only_high_offsets_fail_fast(self):
        self.assertTrue(aa.offset_is_fail_fast(aa.OffsetClass.HO))
        self.assertTrue(aa.offset_is_fail_fast(aa.OffsetClass.HO_RAILED))
        for cls in (aa.OffsetClass.OK, aa.OffsetClass.LO, aa.OffsetClass.DEAD, aa.OffsetClass.EMPTY):
            self.assertFalse(aa.offset_is_fail_fast(cls))

    def test_settle_warning_over_0_05_V(self):
        self.assertIsNone(aa.settle_warning(0.70, 0.74))
        self.assertIsNone(aa.settle_warning(0.70, 0.75))
        self.assertIn("settling", aa.settle_warning(0.70, 0.76))
        self.assertIn("-0.060", aa.settle_warning(0.76, 0.70))
        self.assertIsNone(aa.settle_warning(None, 0.7))

    def test_live_tile_states(self):
        T = aa.TileState
        loaded = aa.Occupancy.LOADED
        self.assertIs(aa.tile_state_for_live_offset(0.7, occupancy=loaded), T.LOADED)
        self.assertIs(aa.tile_state_for_live_offset(1.3, occupancy=loaded), T.OFFSET_FAIL)
        self.assertIs(aa.tile_state_for_live_offset(4.95, occupancy=aa.Occupancy.UNKNOWN), T.OFFSET_FAIL)
        self.assertIs(aa.tile_state_for_live_offset(0.2, occupancy=loaded), T.SETTLING)
        self.assertIs(aa.tile_state_for_live_offset(0.01, occupancy=loaded), T.SETTLING)
        self.assertIs(aa.tile_state_for_live_offset(0.01, occupancy=aa.Occupancy.UNKNOWN), T.UNKNOWN)
        self.assertIs(aa.tile_state_for_live_offset(0.01, occupancy=aa.Occupancy.EMPTY), T.EMPTY)


def noise_result(verdict: aa.NoiseVerdict, *, worst=0.1, median=0.08, over=0, total=60) -> aa.ChannelNoiseAnalysis:
    return aa.ChannelNoiseAnalysis(
        channel=0, position="1-1", windows_total=total,
        windows_over_high=None if verdict is aa.NoiseVerdict.NO_LIMIT else over,
        over_fraction=None if verdict is aa.NoiseVerdict.NO_LIMIT else over / total,
        worst_pp_mv=worst, median_pp_mv=median, window_pp_mv=(median,) * total, clipped_windows=0, verdict=verdict,
    )


class JudgePositionTests(unittest.TestCase):
    def judge(self, **kw):
        base = dict(
            position="1-1", channel=0, occupancy=aa.Occupancy.LOADED, sensor_number=7, sensor_id="L1-7",
            offset_initial_v=0.6, offset_v=0.7, offset_early_v=0.69, noise=noise_result(aa.NoiseVerdict.NO_LIMIT),
        )
        base.update(kw)
        return aa.judge_position(**base)

    def test_pass_with_no_limit_is_provisional_and_warned(self):
        result = self.judge()
        self.assertIs(result.verdict, aa.PositionVerdict.PASS)
        self.assertTrue(result.passed)
        self.assertEqual(result.pass_fail_text, "PASS")
        self.assertTrue(result.provisional)
        self.assertEqual(result.calibration_status, "PENDING")
        self.assertEqual(result.calibration_id, "40623_array50_daq_PENDING")
        self.assertEqual(result.verdict_status, "PROVISIONAL")
        self.assertTrue(any("no pin-level limit" in w for w in result.warnings))
        self.assertIs(aa.tile_state_for(result), aa.TileState.NO_LIMIT)
        self.assertAlmostEqual(result.offset_settle_delta_v, 0.01)
        self.assertEqual(result.failure_mode_tag, "")

    def test_pass_with_limits_is_green(self):
        result = self.judge(noise=noise_result(aa.NoiseVerdict.PASS))
        self.assertIs(aa.tile_state_for(result), aa.TileState.PASS)
        self.assertFalse(any("no pin-level limit" in w for w in result.warnings))

    def test_empty_short_circuits(self):
        result = self.judge(occupancy=aa.Occupancy.EMPTY, offset_v=0.0, noise=None)
        self.assertIs(result.verdict, aa.PositionVerdict.EMPTY)
        self.assertIs(result.offset_class, aa.OffsetClass.EMPTY)
        self.assertEqual(result.pass_fail_text, "")
        self.assertIs(aa.tile_state_for(result), aa.TileState.EMPTY)

    def test_rig_fault_is_not_measured(self):
        result = self.judge(rig_fault="stream integrity failed twice", noise=None, offset_v=None)
        self.assertIs(result.verdict, aa.PositionVerdict.NOT_MEASURED)
        self.assertEqual(result.failure_mode_tag, "NM")
        self.assertEqual(result.fail_reasons[0].code, "NM")
        self.assertEqual(result.pass_fail_text, "NOT MEASURED")
        self.assertIs(aa.tile_state_for(result), aa.TileState.NOT_MEASURED)

    def test_offset_failures_take_precedence_over_noise(self):
        result = self.judge(offset_v=1.3, noise=noise_result(aa.NoiseVerdict.HIGH, over=20))
        self.assertIs(result.verdict, aa.PositionVerdict.FAIL_OFFSET)
        self.assertEqual([r.code for r in result.fail_reasons], ["HO", "N"])
        self.assertEqual(result.failure_mode_tag, "HO")
        self.assertIs(aa.tile_state_for(result), aa.TileState.OFFSET_FAIL)
        self.assertEqual(result.pass_fail_text, "FAIL")
        for volts, code in ((4.95, "HO"), (0.2, "LO"), (0.01, "D")):
            result = self.judge(offset_v=volts, noise=noise_result(aa.NoiseVerdict.PASS))
            self.assertIs(result.verdict, aa.PositionVerdict.FAIL_OFFSET, volts)
            self.assertEqual(result.fail_reasons[0].code, code)
            self.assertEqual(result.failure_mode_tag, code)

    def test_noise_high_and_low_verdicts(self):
        high = self.judge(noise=noise_result(aa.NoiseVerdict.HIGH, over=12), noise_limits=aa.NoiseLimits(low_mv=0.05, high_mv=0.43))
        self.assertIs(high.verdict, aa.PositionVerdict.FAIL_NOISE_HIGH)
        self.assertEqual(high.fail_reasons[0].code, "N")
        self.assertIn("12 of 60 windows", high.fail_reasons[0].text)
        self.assertEqual(high.failure_mode_tag, "N")
        self.assertIs(aa.tile_state_for(high), aa.TileState.NOISE_FAIL)
        low = self.judge(noise=noise_result(aa.NoiseVerdict.LOW, median=0.01), noise_limits=aa.NoiseLimits(low_mv=0.05, high_mv=0.43))
        self.assertIs(low.verdict, aa.PositionVerdict.NOISE_LOW)
        self.assertEqual(low.fail_reasons[0].code, "NL")
        self.assertEqual(low.failure_mode_tag, "NL")
        self.assertIs(aa.tile_state_for(low), aa.TileState.NOISE_LOW)
        self.assertEqual(low.pass_fail_text, "FAIL")

    def test_settle_warning_is_attached(self):
        result = self.judge(offset_early_v=0.60, offset_v=0.70)
        self.assertTrue(any("settling" in w for w in result.warnings))
        self.assertIs(result.verdict, aa.PositionVerdict.PASS)

    def test_missing_settled_offset_is_not_measured(self):
        result = self.judge(offset_v=None, offset_early_v=None)
        self.assertIs(result.verdict, aa.PositionVerdict.NOT_MEASURED)

    def test_fail_reasons_are_structured(self):
        result = self.judge(offset_v=1.3, noise=None)
        reason = result.fail_reasons[0]
        self.assertEqual(reason.code, "HO")
        self.assertAlmostEqual(reason.value, 1.3)
        self.assertAlmostEqual(reason.limit, 1.2)
        self.assertIn("1.300 V", str(reason))

    def test_failure_mode_choices_follow_tp120(self):
        codes = [choice.split(" - ")[0] for choice in aa.FAILURE_MODE_CHOICES]
        for code in ("HO", "LO", "SH", "D", "N", "NL", "NM", "Drop"):
            self.assertIn(code, codes)


class QuietWaitTests(unittest.TestCase):
    def test_needs_enough_blocks(self):
        means = np.array([[0.7, 0.7]])
        self.assertFalse(aa.quiet_wait_settled(means, [True, True], delta_mv=0.1, blocks_required=2))

    def test_settled_when_loaded_channels_are_flat(self):
        means = np.array([[0.70, 0.30], [0.7002, 0.35], [0.70025, 0.40], [0.70028, 0.45]])
        self.assertTrue(aa.quiet_wait_settled(means, [True, False], delta_mv=0.1, blocks_required=2))
        self.assertFalse(aa.quiet_wait_settled(means, [True, True], delta_mv=0.1, blocks_required=2))

    def test_no_loaded_channels_is_settled(self):
        self.assertTrue(aa.quiet_wait_settled(np.zeros((5, 3)), [False] * 3, delta_mv=0.1, blocks_required=2))

    def test_boundary_and_validation(self):
        means = np.array([[0.7], [0.7001], [0.7002]])
        self.assertTrue(aa.quiet_wait_settled(means, [True], delta_mv=0.1, blocks_required=2))
        with self.assertRaises(ValueError):
            aa.quiet_wait_settled(means, [True], delta_mv=0.1, blocks_required=0)
        with self.assertRaises(ValueError):
            aa.quiet_wait_settled(means, [True, True], delta_mv=0.1, blocks_required=1)


class ConstantsTests(unittest.TestCase):
    def test_tp120_constants(self):
        self.assertEqual(aa.OFFSET_MIN_V, 0.3)
        self.assertEqual(aa.OFFSET_MAX_V, 1.2)
        self.assertEqual(aa.OFFSET_SETTLE_DELTA_V, 0.05)
        self.assertEqual(aa.NOISE_LEGACY_PP_LIMIT_LOW_MV, 10.0)
        self.assertEqual(aa.NOISE_LEGACY_PP_LIMIT_HIGH_MV, 37.9)
        self.assertIsNone(aa.NOISE_PP_LIMIT_LOW_MV)
        self.assertIsNone(aa.NOISE_PP_LIMIT_HIGH_MV)
        self.assertEqual(aa.NOISE_DECIMATION_FACTOR, 20)
        self.assertEqual(aa.NOISE_MAX_OVER_FRACTION, 0.15)
        self.assertEqual(aa.CALIBRATION_STATUS, "PENDING")
        self.assertFalse(aa.NoiseLimits().defined)

    def test_module_has_no_io_or_tk(self):
        source = (MODEL_DIR / "array_analysis.py").read_text(encoding="utf-8")
        for forbidden in ("import tkinter", "from tkinter", "import csv", "\nopen(", " open(",
                          "import daq_backend", "from daq_backend", "single_detector_rig"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
