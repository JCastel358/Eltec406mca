"""Tests for daq_noise_floor_check.py - the "can this DAQ resolve the part's noise?" bench check.

Synthetic captures with known noise decide the arithmetic (the 3 Hz band RMS
of a sine, quadrature excess, the break-even chain gain, the range table);
an injected ``SimulatedDaq(real_time=False)`` proves the live path (roles,
the onboard-reference capture restoring the normal configuration, files only
at explicit paths); saved-capture replay proves both NPZ layouts. A guard
checks nothing landed under the technician's Documents folder, and the
evidence-folder refusal is exercised on a path that only looks like one.
"""

from __future__ import annotations

import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
MODEL_DIR = TESTS_DIR.parent
for entry in (str(MODEL_DIR), str(MODEL_DIR.parent)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import array_analysis as aa  # noqa: E402
import daq_backend as daq  # noqa: E402
import daq_noise_floor_check as floor  # noqa: E402
import daq_rig_readout as readout  # noqa: E402
import eltec_40623_array_tester as app  # noqa: E402
import legacy_noise as ln  # noqa: E402

HOME_RESULTS = Path.home() / "Documents" / "Eltec_40623_Test_Results"
SETTLED_T = 200.0  # the simulator's offsets have settled by this virtual time (no ramp to leak)


def home_signature() -> set[str]:
    if not HOME_RESULTS.exists():
        return set()
    return {str(p) for p in HOME_RESULTS.rglob("*")}


_HOME_BEFORE = home_signature()

RATE = 1000.0
SECONDS = 60.0
N = int(RATE * SECONDS)


def sine_rms_uv(amplitude_uv: float) -> float:
    return amplitude_uv / math.sqrt(2.0)


def synthetic_capture(noise_uv: dict[int, float] | None = None, *, seconds: float = SECONDS, level_v: float = 0.7,
                      levels_v: dict[int, float] | None = None, ramps_mv: dict[int, float] | None = None,
                      tone: tuple[float, float] | None = None, channels: int = 50, seed: int = 3,
                      occupancy: tuple[str, ...] | None = None, source: str = "synthetic") -> floor.FloorCapture:
    """White noise at a per-channel rms (uV) on a DC level; a 3 Hz sine where the value is negative (amplitude).

    ``tone`` = (Hz, amplitude uV) adds an out-of-band sine to every channel:
    the way to give a very quiet channel a raw span above one ADC code (the
    tool rightly calls sub-code white noise unresolved) without touching the
    3 Hz band.
    """

    rng = np.random.default_rng(seed)
    n = int(round(seconds * RATE))
    t = np.arange(n) / RATE
    volts = np.empty((channels, n))
    for c in range(channels):
        level = (levels_v or {}).get(c, level_v)
        spec = (noise_uv or {}).get(c, 3.0)
        if spec < 0:
            volts[c] = level + (-spec) * 1e-6 * np.sin(2 * math.pi * ln.CENTER_HZ * t)
        else:
            volts[c] = level + rng.normal(0.0, spec * 1e-6, n)
        if ramps_mv and c in ramps_mv:
            volts[c] += ramps_mv[c] * 1e-3 * t / seconds
        if tone is not None:
            volts[c] += tone[1] * 1e-6 * np.sin(2 * math.pi * tone[0] * t)
    return floor.FloorCapture(
        volts=volts, rate_hz=RATE, nominal_rate_hz=RATE, actual_timer_recorded=True,
        channels=tuple(range(channels)), positions=tuple(daq.POSITIONS[:channels]), range_code=2, oversample=3,
        drop_first=1, daq_serial="TEST", simulated=False, source=source, occupancy=occupancy,
    )


def run_cli(argv: list[str], device=None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = floor.main(argv, device=device)
        except SystemExit as exc:  # argparse's exit(2) on a tool failure
            code = int(exc.code) if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


class HomeGuardMixin:
    def assert_home_untouched(self) -> None:
        self.assertEqual(home_signature(), _HOME_BEFORE, "the tests wrote into the technician's Documents folder")


# ----------------------------------------------------------------------
# Roles and paths
# ----------------------------------------------------------------------
class RoleTests(unittest.TestCase):
    def test_tokens_in_every_form_and_commas(self):
        roles = floor.assign_roles(range(50), loaded=["1-1,CH1", " 2 "], grounded=["5-10"], quiet_source=[49 - 1],
                                   excluded=["3-3"])
        self.assertEqual(roles[0], floor.ROLE_LOADED)
        self.assertEqual(roles[1], floor.ROLE_LOADED)
        self.assertEqual(roles[2], floor.ROLE_LOADED)
        self.assertEqual(roles[49], floor.ROLE_GROUNDED)
        self.assertEqual(roles[48], floor.ROLE_QUIET_SOURCE)
        self.assertEqual(roles[22], floor.ROLE_EXCLUDED)
        self.assertEqual(sum(1 for r in roles.values() if r == floor.ROLE_EMPTY), 44)

    def test_default_roles_from_the_tester_map_and_overrides(self):
        defaults = {0: floor.ROLE_LOADED, 1: floor.ROLE_EXCLUDED, 2: floor.ROLE_EMPTY}
        roles = floor.assign_roles(range(4), grounded=["1-1"], default_roles=defaults)
        self.assertEqual(roles[0], floor.ROLE_GROUNDED)    # the option wins over the saved map
        self.assertEqual(roles[1], floor.ROLE_EXCLUDED)
        self.assertEqual(roles[2], floor.ROLE_EMPTY)
        self.assertEqual(roles[3], floor.ROLE_EMPTY)

    def test_conflicts_and_unknown_channels_are_errors(self):
        with self.assertRaisesRegex(ValueError, "two roles"):
            floor.assign_roles(range(50), loaded=["1-1"], grounded=["CH0"])
        with self.assertRaisesRegex(ValueError, "not in this capture"):
            floor.assign_roles(range(10), loaded=["5-5"])
        with self.assertRaises(ValueError):
            floor.assign_roles(range(50), loaded=["row-col"])
        # the same channel twice in the same role is fine
        self.assertEqual(floor.assign_roles(range(50), loaded=["1-1", "CH0"])[0], floor.ROLE_LOADED)

    def test_evidence_folder_detection_and_output_path_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            evidence = root / "Documents" / "Eltec_40623_Test_Results" / "x" / "report.json"
            self.assertTrue(floor.is_evidence_folder(evidence))
            self.assertFalse(floor.is_evidence_folder(root / "Eltec_40623_Test_Results.json"))  # a file name, not a folder
            with self.assertRaisesRegex(ValueError, "evidence folder"):
                floor.check_output_path(evidence, suffixes=(".json",))
            with self.assertRaisesRegex(ValueError, "must end in"):
                floor.check_output_path(root / "report.txt", suffixes=(".json",))
            existing = root / "report.json"
            existing.write_text("{}")
            with self.assertRaisesRegex(ValueError, "already exists"):
                floor.check_output_path(existing, suffixes=(".json",))
            self.assertEqual(floor.check_output_path(root / "new.json", suffixes=(".json",)), root / "new.json")


# ----------------------------------------------------------------------
# Arithmetic
# ----------------------------------------------------------------------
class ArithmeticTests(unittest.TestCase):
    def test_window_and_quantization_formulas(self):
        low = floor.window_rms_uv(ln.LEGACY_METER_LOW_MV)
        self.assertAlmostEqual(low, 10.0 * 1000 / 10000 / math.sqrt(2 / math.pi), places=9)
        self.assertAlmostEqual(floor.window_rms_uv(37.9), 3.79 / math.sqrt(2 / math.pi), places=9)
        # the same q/sqrt(12) x sqrt(2 ENBW/fs) model legacy_noise reports per channel
        q = floor.quantization_rms_uv(2, RATE)
        enbw = math.pi * 3.0 / 6.0
        self.assertAlmostEqual(q, 76.2939453125 / math.sqrt(12) * math.sqrt(2 * enbw / RATE), places=9)
        self.assertAlmostEqual(floor.quantization_rms_uv(4, RATE), q * 2 / 5, places=9)   # 0-2 V
        self.assertAlmostEqual(floor.quantization_rms_uv(0, RATE), q * 2, places=9)       # 0-10 V
        # averaging N dithered conversions per scan divides the ideal figure by sqrt(N) (production: 3 of 4 kept)
        self.assertAlmostEqual(floor.quantization_rms_uv(2, RATE, averaged=3), q / math.sqrt(3), places=9)
        with self.assertRaises(ValueError):
            floor.quantization_rms_uv(2, RATE, averaged=0)
        self.assertEqual(floor.averaged_conversions(3, 1), 3)
        self.assertEqual(floor.averaged_conversions(0, 0), 1)
        self.assertEqual(floor.averaged_conversions(0, 1), 1)      # never below one
        # the narrowest range that still holds a DC-coupled 0.3-1.2 V offset is 0-2 V (code 4)
        self.assertEqual(floor.tightest_offset_range(2).code, 4)
        self.assertEqual(floor.tightest_offset_range(4).code, 4)
        self.assertIn("0.85-22 Hz", floor.judged_label(1000.0))
        self.assertIn("500 SPS", floor.judged_label(500.0))
        command, oversample, hz = floor.suggested_experiment(4, 50)
        self.assertEqual((oversample, hz), (15, 500.0))            # 50 ch x 16 conversions x 500 scans/s = 400 kS/s
        self.assertIn("--range 4 --hz 500 --oversample 15 --daq-reference", command)
        self.assertEqual(floor.suggested_experiment(4, 1)[1], daq.MAX_OVERSAMPLE)   # capped by the config byte

    def test_quadrature_excess_and_settling_leak(self):
        self.assertAlmostEqual(floor.quadrature_excess(5.0, 3.0), 4.0)
        self.assertEqual(floor.quadrature_excess(2.0, 3.0), 0.0)
        # a 17 mV ramp over 60 s: slope 283 uV/s, / (Q w0 = 3 x 2 pi 3) = 5.0 uV
        self.assertAlmostEqual(floor.settling_leak_uv(17.0, 60.0), 17000 / 60 / (3 * 2 * math.pi * 3), places=9)
        self.assertEqual(floor.settling_leak_uv(1.0, 0.0), 0.0)

    def test_band_rms_of_a_3hz_sine_and_the_filter_leak_of_a_ramp(self):
        # CH0: 100 uV amplitude sine at the filter centre -> 70.7 uV rms; CH1: 3 uV white (sub-code: unresolved,
        # not usable, but its numbers are still computed); CH2: white + 17 mV ramp
        cap = synthetic_capture({0: -100.0, 1: 3.0, 2: 3.0}, ramps_mv={2: 17.0}, channels=3)
        rows = floor.analyze_channels(cap, {0: floor.ROLE_LOADED, 1: floor.ROLE_EMPTY, 2: floor.ROLE_LOADED})
        self.assertAlmostEqual(rows[0].band_rms_uv, sine_rms_uv(100.0), delta=0.5)
        self.assertTrue(rows[0].usable)
        self.assertFalse(rows[1].usable)
        self.assertIn("one ADC code", rows[1].notes[0])
        self.assertAlmostEqual(rows[0].mean_v, 0.7, places=4)
        self.assertAlmostEqual(rows[0].nominal_meter_mv, rows[0].smoothed_abs_uv * 1e-3 * 10000, places=6)
        # 3 uV white per sample at 1000 SPS through a 1.57 Hz ENBW: 3 x sqrt(1.57/500) = 0.17 uV
        self.assertAlmostEqual(rows[1].band_rms_uv, 3.0 * math.sqrt(math.pi * 3 / 6 / 500), delta=0.05)
        self.assertLess(rows[1].drift_mv, 0.05)
        # the ramp: drift ~17 mV, leak ~5 uV, most of the reading -> flagged SETTLING and not usable
        self.assertAlmostEqual(rows[2].drift_mv, 17.0 * (N - RATE) / N, delta=0.3)
        self.assertGreater(rows[2].settling_leak_uv, 4.0)
        self.assertFalse(rows[2].usable)
        self.assertIn("SETTLING", rows[2].notes[0])
        # judged-band figures exist for a 60 s capture
        self.assertTrue(math.isfinite(rows[1].judged_pp_median_uv))
        self.assertGreaterEqual(rows[1].judged_pp_worst_uv, rows[1].judged_pp_median_uv)

    def test_rail_and_span_rules(self):
        # CH0 grounded at exactly 0 V: constant -> clipped at the bottom rail; CH1 at 3 codes above 0 with noise ->
        # within RAIL_MARGIN_CODES -> clipped; CH2 1.3 codes under 5 V -> top rail; CH3 constant mid-range -> span <= 1 code
        lsb = daq.lsb_volts(2)
        cap = synthetic_capture({0: 0.0, 1: 50.0, 2: 0.0, 3: 0.0}, levels_v={0: 0.0, 1: 3 * lsb, 2: 4.9999, 3: 0.7},
                                channels=4)
        rows = floor.analyze_channels(cap, {c: floor.ROLE_GROUNDED for c in range(4)})
        self.assertTrue(rows[0].bottom_rail and not rows[0].usable and "CLIPPED" in rows[0].notes[0])
        self.assertTrue(rows[1].bottom_rail and not rows[1].usable)
        self.assertTrue(rows[2].top_rail and not rows[2].usable and "top rail" in rows[2].notes[0])
        self.assertFalse(rows[3].bottom_rail or rows[3].top_rail)
        self.assertFalse(rows[3].usable)
        self.assertIn("one ADC code", rows[3].notes[0])

    def test_loaded_socket_reading_like_an_empty_one_is_not_evidence(self):
        cap = synthetic_capture({0: 50.0, 1: 50.0, 2: 50.0}, levels_v={0: 0.02, 1: 1.5, 2: 0.7}, channels=3)
        rows = floor.analyze_channels(cap, {c: floor.ROLE_LOADED for c in range(3)})
        self.assertFalse(rows[0].usable)
        self.assertIn("like an empty socket", rows[0].notes[0])
        self.assertTrue(rows[1].usable)
        self.assertIn("outside TP120", rows[1].notes[0])
        self.assertTrue(rows[2].usable)
        self.assertEqual(rows[2].notes, ())

    def test_short_capture_has_no_judged_window_and_a_warning(self):
        cap = synthetic_capture({0: 3.0}, seconds=0.5, channels=1)
        check = floor.run_check(cap, {0: floor.ROLE_EMPTY})
        self.assertTrue(math.isnan(check.rows[0].judged_pp_median_uv))
        self.assertTrue(any("exploratory" in w for w in check.warnings))
        report = floor.json_safe(check.report())
        self.assertIsNone(report["channels"][0]["judged_pp_median_uv"])
        json.dumps(report, allow_nan=False)


# ----------------------------------------------------------------------
# The assessment
# ----------------------------------------------------------------------
class AssessmentTests(unittest.TestCase):
    def check(self, noise_uv, roles, **kwargs):
        cap = synthetic_capture(noise_uv, channels=len(roles), **{k: v for k, v in kwargs.items() if k != "margin"})
        return floor.run_check(cap, roles, margin=kwargs.get("margin", floor.DEFAULT_MARGIN))

    def test_not_resolvable_when_the_floor_alone_reads_above_the_low_limit(self):
        # empties at ~3 uV band rms need ~53 uV white per sample (x sqrt(1.57/500)); parts a little above
        white = 3.0 / math.sqrt(math.pi * 3 / 6 / 500)
        noise = {c: white for c in range(6)}
        noise.update({6: white * 1.25, 7: white * 1.25})
        roles = {c: floor.ROLE_EMPTY for c in range(6)}
        roles.update({6: floor.ROLE_LOADED, 7: floor.ROLE_LOADED})
        check = self.check(noise, roles)
        a = check.assessment
        self.assertEqual(a.verdict, floor.VERDICT_NOT_RESOLVABLE)
        self.assertEqual(a.floor_role, floor.ROLE_EMPTY)
        self.assertAlmostEqual(a.floor_rms_uv, 3.0, delta=0.4)
        self.assertGreater(a.floor_over_low, 1.0)
        self.assertLess(a.floor_over_high, 1.0)
        self.assertAlmostEqual(a.required_floor_rms_uv, a.window_low_rms_uv / 3.0, places=9)
        # break-even chain gain = low_mV x 1000 / (0.798 x margin x floor)
        expected = 10.0 * 1000 / (math.sqrt(2 / math.pi) * 3.0 * a.floor_rms_uv)
        self.assertAlmostEqual(a.break_even_gain_low, expected, places=6)
        self.assertAlmostEqual(a.break_even_gain_high / a.break_even_gain_low, 3.79, places=6)
        self.assertAlmostEqual(a.min_front_end_gain, a.floor_rms_uv / a.required_floor_rms_uv, places=9)
        self.assertAlmostEqual(a.dc_coupled_gain_limit, 5.0 / 1.2, places=9)
        # quantization is reported at the capture's averaging (3 of 4 conversions kept) and per single conversion
        self.assertEqual(a.averaged_conversions, 3)
        self.assertAlmostEqual(a.quantization_rms_uv, floor.quantization_rms_uv(2, RATE, 3), places=9)
        self.assertAlmostEqual(a.quantization_single_conversion_uv, floor.quantization_rms_uv(2, RATE), places=9)
        self.assertGreater(a.quantization_over_required, 1.0)
        # the settings ceiling: kept conversions 3 x 1000 per channel per second against the aggregate over 8 channels
        self.assertEqual(a.kept_conversions_per_s, 3000.0)
        self.assertAlmostEqual(a.max_conversions_per_s, 500_000 / 8, places=9)
        self.assertAlmostEqual(a.oversampling_ceiling_gain, math.sqrt(500_000 / 8 / 3000), places=9)
        self.assertEqual(a.tightest_range_code, 4)
        self.assertAlmostEqual(a.range_ceiling_gain, 2.5, places=9)
        self.assertAlmostEqual(a.best_case_floor_uv, a.floor_rms_uv / (a.oversampling_ceiling_gain * 2.5), places=9)
        self.assertIn("--range 4 --hz 500 --oversample", a.suggested_experiment)
        self.assertEqual(a.loaded_count, 2)
        self.assertTrue(any("NOT_RESOLVABLE" in line for line in a.lines))
        self.assertTrue(any("break-even" in line for line in a.lines))
        self.assertTrue(any("best case without gain" in line for line in a.lines))
        self.assertTrue(any("the front end, not the code step" in line for line in a.lines))

    def test_settings_ceiling_on_fifty_channels_is_marginal_at_best(self):
        # the real rig: 50 channels, 3 of 4 conversions kept at 1000 scans/s -> at most sqrt(10000/3000) = 1.83 x from
        # oversampling / rate and 2.5 x from 0-2 V; a ~3 uV floor can only reach ~0.65 uV: MARGINAL, never RESOLVABLE
        white = 3.0 / math.sqrt(math.pi * 3 / 6 / 500)
        cap = synthetic_capture({c: white for c in range(50)}, seconds=20.0, channels=50)
        a = floor.run_check(cap, {c: floor.ROLE_EMPTY for c in range(50)}).assessment
        self.assertEqual(a.verdict, floor.VERDICT_NOT_RESOLVABLE)
        self.assertAlmostEqual(a.max_conversions_per_s, 10_000.0, places=9)
        self.assertAlmostEqual(a.oversampling_ceiling_gain, math.sqrt(10_000 / 3000), places=9)
        self.assertGreater(a.best_case_floor_uv, a.required_floor_rms_uv)
        self.assertLess(a.best_case_floor_uv, a.window_low_rms_uv)
        self.assertEqual(a.best_case_verdict, floor.VERDICT_MARGINAL)
        self.assertTrue(any("MARGINAL at best" in line for line in a.lines))

    def test_resolvable_and_marginal_follow_the_margin(self):
        # a 0.3 uV floor (5.3 uV white) is under low/3 = 0.42 -> RESOLVABLE; margin 5 makes it MARGINAL. White noise
        # that quiet spans under one code, so a 450 Hz tone (|H| ~ 0.0005 through the 3 Hz filter) dithers the span.
        white = 0.3 / math.sqrt(math.pi * 3 / 6 / 500)
        roles = {c: floor.ROLE_EMPTY for c in range(4)}
        tone = (450.0, 60.0)
        check = self.check({c: white for c in range(4)}, roles, tone=tone)
        self.assertEqual(check.assessment.verdict, floor.VERDICT_RESOLVABLE, check.assessment.lines)
        self.assertAlmostEqual(check.assessment.floor_rms_uv, 0.3, delta=0.08)
        check = self.check({c: white for c in range(4)}, roles, tone=tone, margin=5.0)
        self.assertEqual(check.assessment.verdict, floor.VERDICT_MARGINAL)
        with self.assertRaises(ValueError):
            self.check({c: white for c in range(4)}, roles, tone=tone, margin=0.5)

    def test_undetermined_without_a_usable_floor_and_the_floor_role_preference(self):
        # every empty socket grounded on the rail -> UNDETERMINED
        cap = synthetic_capture({0: 0.0, 1: 0.0}, levels_v={0: 0.0, 1: 0.0}, channels=2)
        check = floor.run_check(cap, {0: floor.ROLE_GROUNDED, 1: floor.ROLE_GROUNDED})
        self.assertEqual(check.assessment.verdict, floor.VERDICT_UNDETERMINED)
        self.assertIsNone(check.assessment.floor_role)
        self.assertTrue(any("ALL CLIPPED" in line for line in check.assessment.lines))
        # a quiet source stands in when no empty socket is usable; an empty one wins when present
        cap = synthetic_capture({0: 0.0, 1: 50.0, 2: 50.0}, levels_v={0: 0.0}, channels=3)
        check = floor.run_check(cap, {0: floor.ROLE_EMPTY, 1: floor.ROLE_QUIET_SOURCE, 2: floor.ROLE_LOADED})
        self.assertEqual(check.assessment.floor_role, floor.ROLE_QUIET_SOURCE)
        check = floor.run_check(cap, {0: floor.ROLE_QUIET_SOURCE, 1: floor.ROLE_EMPTY, 2: floor.ROLE_LOADED})
        self.assertEqual(check.assessment.floor_role, floor.ROLE_EMPTY)

    def test_grounded_comparison_wiring_versus_instrument(self):
        white = 3.0 / math.sqrt(math.pi * 3 / 6 / 500)
        # grounded sockets kept off the rail by an op-amp offset (0.02 V) at the same noise: not the wiring
        noise = {0: white, 1: white, 2: white, 3: white}
        cap = synthetic_capture(noise, levels_v={2: 0.02, 3: 0.02}, channels=4)
        roles = {0: floor.ROLE_EMPTY, 1: floor.ROLE_EMPTY, 2: floor.ROLE_GROUNDED, 3: floor.ROLE_GROUNDED}
        check = floor.run_check(cap, roles)
        self.assertTrue(any("not the socket wiring" in line for line in check.assessment.lines))
        # the wiring share is the quadrature difference of the two role medians (a same-noise draw gives ~1 uV of
        # pure scatter, which is why the sentence rule is grounded >= empty / sqrt(2), not a fixed number)
        self.assertAlmostEqual(check.assessment.wiring_rms_uv,
                               floor.quadrature_excess(check.summaries[floor.ROLE_EMPTY].band_rms_median_uv,
                                                       check.summaries[floor.ROLE_GROUNDED].band_rms_median_uv), places=9)
        self.assertLess(check.assessment.wiring_rms_uv, check.summaries[floor.ROLE_EMPTY].band_rms_median_uv / math.sqrt(2))
        # grounded much quieter than empty: the wiring contributes
        cap = synthetic_capture({0: white, 1: white, 2: white / 4, 3: white / 4}, levels_v={2: 0.02, 3: 0.02}, channels=4)
        check = floor.run_check(cap, roles)
        self.assertTrue(any("wiring contributes" in line for line in check.assessment.lines))
        self.assertGreater(check.assessment.wiring_rms_uv, 2.0)

    def test_reference_capture_gives_the_buffer_path_share(self):
        white = 3.0 / math.sqrt(math.pi * 3 / 6 / 500)
        cap = synthetic_capture({0: white, 1: white}, channels=2)
        reference = synthetic_capture({0: white / 2, 1: white / 2}, channels=2, source="onboard reference")
        check = floor.run_check(cap, {0: floor.ROLE_EMPTY, 1: floor.ROLE_EMPTY}, reference=reference)
        self.assertIn(floor.ROLE_REFERENCE, check.summaries)
        self.assertEqual(check.summaries[floor.ROLE_REFERENCE].count, 2)
        self.assertAlmostEqual(check.assessment.buffer_path_rms_uv,
                               floor.quadrature_excess(check.summaries[floor.ROLE_EMPTY].band_rms_median_uv,
                                                       check.summaries[floor.ROLE_REFERENCE].band_rms_median_uv), places=9)
        self.assertTrue(any("onboard reference" in line for line in check.assessment.lines))
        self.assertEqual(len(check.rows), 4)

    def test_range_table_marks_the_offset_band_and_the_current_range(self):
        options = floor.range_options(RATE, 0.42)
        by_code = {o.code: o for o in options}
        self.assertEqual(sorted(by_code), list(range(8)))
        self.assertTrue(by_code[2].fits_offset_band)
        self.assertTrue(by_code[4].fits_offset_band)
        self.assertFalse(by_code[6].fits_offset_band)
        self.assertFalse(by_code[7].fits_offset_band)
        self.assertAlmostEqual(by_code[4].quantization_rms_uv, by_code[2].quantization_rms_uv * 0.4, places=9)
        self.assertAlmostEqual(by_code[2].quantization_over_required, by_code[2].quantization_rms_uv / 0.42, places=9)

    def test_printed_output_and_report_round_trip(self):
        white = 3.0 / math.sqrt(math.pi * 3 / 6 / 500)
        cap = synthetic_capture({0: white, 1: white * 1.3}, channels=2)
        check = floor.run_check(cap, {0: floor.ROLE_EMPTY, 1: floor.ROLE_LOADED})
        out = io.StringIO()
        with redirect_stdout(out):
            floor.print_check(check)
        text = out.getvalue()
        self.assertIn("never a part verdict", text)
        self.assertIn("<- current", text)
        self.assertRegex(text, r"\n1-2\s+loaded\s")
        with tempfile.TemporaryDirectory() as folder:
            path = floor.write_report(check, Path(folder) / "sub" / "report.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["tool"], floor.TOOL_NAME)
            self.assertEqual(data["method_id"], ln.METHOD_ID)
            self.assertEqual(data["roles"], {"1-1": "empty", "1-2": "loaded"})
            self.assertEqual(data["acquisition"]["setup_id"], "TEST:buffer-1x:drop-1")
            self.assertEqual(data["assessment"]["verdict"], check.assessment.verdict)
            self.assertEqual(data["assessment"]["best_case_verdict"], check.assessment.best_case_verdict)
            self.assertEqual(data["criteria"]["margin"], 3.0)
            self.assertEqual(data["criteria"]["daq_aggregate_max_sps"], 500_000)
            self.assertIn("0.85-22 Hz", data["criteria"]["judged_band"])
            self.assertEqual(len(data["channels"]), 2)
            self.assertEqual(len(data["range_options"]), 8)
            with self.assertRaisesRegex(ValueError, "already exists"):
                floor.write_report(check, path)


# ----------------------------------------------------------------------
# Live path on the simulator, and saved-capture replay
# ----------------------------------------------------------------------
class LiveAndReplayTests(HomeGuardMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def settled_sim(self, profile: daq.SimProfile | None = None) -> daq.SimulatedDaq:
        sim = daq.SimulatedDaq(profile, real_time=False)
        sim._virtual_t = SETTLED_T
        return sim

    def test_capture_command_roles_files_and_reference_restore_the_configuration(self):
        profile = daq.SimProfile(empty_positions=frozenset({"1-10"}), noise_rms_uv={"1-1": 200.0})
        sim = self.settled_sim(profile)
        npz = self.root / "floor.npz"
        report = self.root / "floor.json"
        code, out, err = run_cli(["capture", "-s", "2", "--quiet-wait", "0.5", "--daq-reference", "--reference-seconds", "1",
                                  "--loaded", "1-1", "--grounded", "1-10", "--exclude", "5-5",
                                  "--npz", str(npz), "--report", str(report)], device=sim)
        self.assertEqual(code, 0, err)
        self.assertIn("assessment (of the acquisition setup", out)
        self.assertIn("report written to", out)
        self.assertTrue(npz.exists() and report.exists())
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["roles"]["1-1"], "loaded")
        self.assertEqual(data["roles"]["1-10"], "grounded")
        self.assertEqual(data["roles"]["5-5"], "excluded")
        self.assertTrue(data["simulated"])
        self.assertTrue(any("Simulated" in w for w in data["warnings"]))
        roles_in_rows = {row["role"] for row in data["channels"]}
        self.assertIn("reference", roles_in_rows)
        # the simulator's empty socket is 0 V: on the rail, so the grounded socket is CLIPPED, never a floor
        grounded = next(row for row in data["channels"] if row["position"] == "1-10" and row["role"] == "grounded")
        self.assertFalse(grounded["usable"])
        self.assertTrue(grounded["bottom_rail"])
        # the reference capture put the normal input configuration back
        self.assertEqual(sim.config.cal_code, daq.CAL_NORMAL)
        self.assertFalse(sim.is_streaming)
        # the archived capture replays in this tool and in the readout's layout
        with np.load(npz, allow_pickle=False) as saved:
            self.assertEqual(str(saved["source"]), readout.NPZ_SOURCE)
            self.assertEqual(saved["waveform_v"].shape, (50, 2000))
        code, out, _ = run_cli(["replay", str(npz), "--loaded", "1-1"])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"\n1-1\s+loaded\s")
        self.assert_home_untouched()

    def test_capture_command_refuses_a_bad_report_path_before_streaming(self):
        sim = self.settled_sim()
        existing = self.root / "taken.json"
        existing.write_text("{}")
        code, _, err = run_cli(["capture", "-s", "1", "--report", str(existing)], device=sim)
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(sim.stream_attempts, 0)
        evidence = self.root / "Eltec_40623_Test_Results" / "floor.json"
        code, _, err = run_cli(["capture", "-s", "1", "--report", str(evidence)], device=sim)
        self.assertEqual(code, 2)
        self.assertIn("evidence folder", err)
        self.assertEqual(sim.stream_attempts, 0)
        code, _, err = run_cli(["capture", "-s", "1", "--npz", str(self.root / "cap.csv")], device=sim)
        self.assertEqual(code, 2)
        self.assertIn("must end in .npz", err)

    def test_role_errors_exit_2_after_a_capture(self):
        sim = self.settled_sim()
        code, _, err = run_cli(["capture", "-s", "1", "--loaded", "1-1", "--grounded", "CH0"], device=sim)
        self.assertEqual(code, 2)
        self.assertIn("two roles", err)

    def test_reference_capture_uses_the_bipolar_zero_on_a_bipolar_range(self):
        sim = self.settled_sim()
        rig = readout.ArrayRig(device=sim, range_code=3, quiet=True)
        rig.connect()
        seen: list[int] = []
        original_start = sim.start_stream

        def spying_start(**kwargs):
            seen.append(sim.config.cal_code)
            return original_start(**kwargs)

        sim.start_stream = spying_start
        cap = floor.capture_reference(rig, seconds=0.5, quiet_wait_s=0.0)
        self.assertEqual(seen, [daq.CAL_BIPOLAR_ZERO])
        self.assertEqual(sim.config.cal_code, daq.CAL_NORMAL)
        self.assertEqual(cap.range_code, 3)
        rig.set_range(2)
        seen.clear()
        floor.capture_reference(rig, seconds=0.5, quiet_wait_s=0.0)
        self.assertEqual(seen, [daq.CAL_REF_UNIPOLAR])
        rig.close()

    def test_replay_of_a_tester_capture_takes_roles_from_the_saved_map(self):
        # a tester tray_<n>_raw.npz: position 1-10 EMPTY, the rest LOADED (the engineer-tools fixture's shape)
        rng = np.random.default_rng(1)
        n = int(20 * RATE)
        waveform = (0.7 + rng.normal(0.0, 60e-6, (50, n))).astype(np.float32)
        occupancy = [aa.Occupancy.LOADED] * 50
        occupancy[9] = aa.Occupancy.EMPTY
        numbers = app.assign_sensor_numbers(occupancy, 1)
        lock = app.LockSnapshot(occupancy=occupancy, sensor_numbers=numbers, offset_initial_v=np.full(50, 0.7),
                                ho_positions=(), start_number=1, locked_at="t")
        capture = app.TrayCapture(
            waveform_v=waveform, sample_rate_hz=RATE, actual_timer_hz=RATE,
            left_context_v=np.full((50, 310), 0.7, dtype=np.float32), right_context_v=np.full((50, 310), 0.7, dtype=np.float32),
            diagnostics=None, quiet_wait_s=3.0, quiet_settled=True, stabilisation_wait_s=0.0, started_at="t", attempts_used=1,
        )
        info = daq.DaqInfo(name="sim", serial_number="S", device_index=0, product_id=0x8145, dll_version="x", simulated=False)
        path = self.root / "tray_1_raw.npz"
        app.save_tray_raw_capture(path, capture, lock, lot="L", tray_number=1, tray_attempt=1, daq_info=info, plan=app.CapturePlan())
        before = path.read_bytes()
        loaded = floor.load_npz(path)
        self.assertEqual(loaded.occupancy[9], "EMPTY")
        self.assertEqual(loaded.drop_first, 1)
        self.assertEqual(loaded.daq_serial, "S")
        roles = floor.assign_roles(loaded.channels, default_roles=loaded.default_roles())
        self.assertEqual(roles[9], floor.ROLE_EMPTY)
        self.assertEqual(sum(1 for r in roles.values() if r == floor.ROLE_LOADED), 49)
        report = self.root / "tray.json"
        code, out, err = run_cli(["replay", str(path), "--exclude", "5-5", "--report", str(report)])
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"\n1-10\s+empty\s")
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["roles"]["5-5"], "excluded")
        self.assertEqual(data["summaries"]["loaded"]["count"], 48)
        self.assertEqual(data["source"], str(path.resolve()))
        self.assertEqual(path.read_bytes(), before)   # the capture is never modified
        self.assert_home_untouched()

    def test_replay_rejects_incomplete_files(self):
        path = self.root / "bad.npz"
        np.savez_compressed(path, waveform_v=np.zeros((2, 10), dtype=np.float32), sample_rate_hz=1000.0)
        with self.assertRaisesRegex(ValueError, "channels"):
            floor.load_npz(path)
        path2 = self.root / "bad2.npz"
        np.savez_compressed(path2, waveform_v=np.zeros((2, 10), dtype=np.float32), sample_rate_hz=1000.0,
                            channels=np.array([0, 1]), positions=np.array(["1-1", "1-2"]), range_code="2", oversample="3")
        with self.assertRaisesRegex(ValueError, "drop_first"):
            floor.load_npz(path2)
        code, _, err = run_cli(["replay", str(self.root / "missing.npz")])
        self.assertEqual(code, 2)
        self.assertIn("failed", err)

    def test_readout_layout_replays_with_roles_from_the_options(self):
        sim = self.settled_sim()
        rig = readout.ArrayRig(device=sim, quiet=True)
        rig.connect()
        cap = rig.capture(1.0, progress=False)
        rig.close()
        path = cap.to_npz(self.root / "readout.npz")
        loaded = floor.load_npz(path)
        self.assertIsNone(loaded.occupancy)
        self.assertEqual(loaded.default_roles(), {})
        self.assertTrue(loaded.actual_timer_recorded)
        check = floor.run_check(loaded, floor.assign_roles(loaded.channels, loaded=["2-4"]))
        self.assertEqual(check.summaries[floor.ROLE_LOADED].count, 1)
        self.assert_home_untouched()


if __name__ == "__main__":
    unittest.main()
