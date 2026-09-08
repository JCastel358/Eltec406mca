"""The reference-unit candidate qualifier (engineer_tools/reference_unit/).

The tool imports this model's production tester headless, so it is exercised
from the 406 MCA suite: the pure analysis on synthetic series, the production
settle-rule replay against ``wait_for_settled_offset`` itself, the ranking
with disqualifiers, and a simulated end-to-end capture -> compare round trip
in a temporary directory (never under ``Documents``).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
MODEL_DIR = TESTS_DIR.parent
REPO_ROOT = MODEL_DIR.parents[1]
TOOL_PATH = REPO_ROOT / "engineer_tools" / "reference_unit" / "reference_candidate_qualifier.py"
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

import eltec_406mca_esp32_tester as app  # noqa: E402


def load_tool():
    spec = importlib.util.spec_from_file_location("reference_candidate_qualifier", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules[__module__]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load_tool()


def exponential_series(start_v: float, final_v: float, tau_s: float, seconds: float, poll_s: float = 1.0):
    t = np.arange(0.0, seconds + 1e-9, poll_s)
    return t, final_v + (start_v - final_v) * np.exp(-t / tau_s)


class OffsetSettleMetricsTests(unittest.TestCase):
    def test_exponential_settle_is_timed_against_the_final_level(self):
        t, v = exponential_series(1.0, 0.66, 4.0, 40.0)
        m = tool.offset_settle_metrics(t, v, tolerance_mv=10.0)
        self.assertTrue(m["settled"])
        self.assertAlmostEqual(m["final_v"], 0.66, places=3)
        # 0.34 V step with tau 4 s is within 10 mV after 4*ln(34) ~ 14.1 s
        self.assertEqual(m["settle_s"], 15.0)
        self.assertEqual(m["time_to_band_s"], 0.0)
        self.assertTrue(m["final_in_band"])
        self.assertAlmostEqual(m["total_move_mv"], 340.0, places=0)

    def test_time_to_band_and_never_in_band(self):
        t, v = exponential_series(1.6, 0.9, 10.0, 60.0)
        m = tool.offset_settle_metrics(t, v)
        # 0.9 + 0.7 exp(-t/10) <= 1.2 when t >= 10 ln(0.7/0.3) = 8.47 s
        self.assertEqual(m["time_to_band_s"], 9.0)
        t, v = exponential_series(2.0, 1.5, 5.0, 40.0)
        self.assertIsNone(tool.offset_settle_metrics(t, v)["time_to_band_s"])

    def test_a_part_still_moving_at_the_end_is_not_settled(self):
        t, v = exponential_series(1.6, 0.7, 40.0, 30.0)
        m = tool.offset_settle_metrics(t, v, tolerance_mv=10.0)
        self.assertFalse(m["settled"])
        self.assertIsNone(m["settle_s"])
        self.assertGreater(m["tail_span_mv"], 10.0)

    def test_production_replay_matches_wait_for_settled_offset(self):
        t, v = exponential_series(1.5, 0.7, 6.0, 40.0)
        m = tool.offset_settle_metrics(t, v)
        # Independent replay with the same virtual clock semantics.
        reads = iter(v[1:])
        state = {"i": 0}

        def monotonic():
            return float(t[min(state["i"], len(t) - 1)])

        def read():
            state["i"] += 1
            return float(next(reads))

        report = app.wait_for_settled_offset(read, start_v=float(v[0]), monotonic=monotonic, sleep=lambda _s: None)
        self.assertEqual(m["production_settle_s"], report.elapsed_s)
        self.assertEqual(m["production_in_band"], report.in_band)
        self.assertEqual(m["production_final_v"], report.final_v)
        self.assertTrue(m["production_in_band"])
        # 0.7 + 0.8 exp(-t/6) <= 1.2 at t >= 6 ln(1.6) = 2.8 s -> the rule exits on the 3 s read
        self.assertEqual(m["production_settle_s"], 3.0)

    def test_production_replay_extends_a_short_series_flat(self):
        # Out of band for good and quiet: production rule needs two quiet
        # non-improving reads; a 3-read series must not raise.
        m = tool.offset_settle_metrics([0.0, 1.0, 2.0], [1.5, 1.5, 1.5])
        self.assertFalse(m["production_in_band"])
        self.assertTrue(m["production_settled"])


class CycleAndHoldTests(unittest.TestCase):
    def test_cycle_series_uses_production_rising_edges(self):
        rate = 1000.0
        t = np.arange(0, 3.0, 1.0 / rate)
        sync = ((t * 10.0) % 1.0 < 0.5).astype(float)
        waveform = 0.66 + 0.002 * np.sin(2 * np.pi * 10.0 * t)
        starts, pp = tool.cycle_peak_to_peak_series(waveform, sync, rate)
        # 30 drive periods; the production edge rule needs a low->high transition
        # (none at index 0) and drops the partial cycle after the last edge.
        self.assertEqual(len(starts), 28)
        self.assertTrue(np.allclose(pp, 4.0, atol=0.05))
        self.assertAlmostEqual(starts[1] - starts[0], 0.1, places=3)

    def test_hold_trend_slope_and_cv(self):
        t = np.arange(0, 20.0, 0.1)
        pp = 3.5 * (1.0 + 0.02 * t / 60.0)  # +2 %/min
        trend = tool.hold_trend(t, pp)
        self.assertEqual(trend["hold_cycles"], 200)
        # slope / mean of a 20 s ramp: 2 % per min over the mean level (not the start level)
        self.assertAlmostEqual(trend["hold_drift_percent_per_min"], 2.0, places=1)
        self.assertLess(trend["hold_cv_percent"], 0.5)
        self.assertIsNone(tool.hold_trend([0.0, 0.1], [3.5, 3.5])["hold_mean_mv"])

    def test_empty_capture_gives_empty_series(self):
        starts, pp = tool.cycle_peak_to_peak_series(np.zeros(0), np.zeros(0), 1000.0)
        self.assertEqual(len(starts), 0)
        self.assertEqual(len(pp), 0)


def synthetic_run(label, run, *, settle_s=10.0, settled=True, band_s=0.0, recovery_s=3.0, stab_s=8.0,
                  sensitivity=3.6, complete=True, passed=True, snr=5.0, polarity="POSITIVE", drift=0.5, fail_reasons=None):
    return {
        "tool": tool.TOOL_NAME, "label": label, "run": run, "recorded_at": f"2026-09-04T10:0{run}:00", "channel": "sensor",
        "offset_pre": {"first_v": 0.9, "final_v": 0.66, "settled": settled, "settle_s": settle_s if settled else None,
                       "time_to_band_s": band_s, "production_settle_s": 1.0, "t_s": [0.0, 1.0], "v": [0.9, 0.66]},
        "offset_post": {"first_v": 0.65, "final_v": 0.655, "settled": True, "settle_s": recovery_s, "time_to_band_s": 0.0,
                        "production_settle_s": 1.0, "t_s": [0.0, 1.0], "v": [0.65, 0.655]},
        "drive": {"measurement_complete": complete, "stabilization_elapsed_s": stab_s if complete else None,
                  "sensitivity_mv": sensitivity if complete else None, "cycle_cv_percent": 1.5 if complete else None,
                  "signal_to_noise_ratio": snr if complete else None, "polarity": polarity if complete else None,
                  "production_passed": passed, "fail_reasons": fail_reasons or ([] if passed else ["Sensitivity too low: x"]),
                  "warnings": [], "cycle_t_s": [0.1, 0.2], "cycle_pp_series_mv": [3.5, 3.6]},
        "hold": {"hold_drift_percent_per_min": drift, "hold_cv_percent": 1.0, "hold_pwm_elapsed_offset_s": 12.0,
                 "cycle_t_s": [0.0, 0.1], "cycle_pp_series_mv": [3.6, 3.6]},
    }


class AggregateAndRankTests(unittest.TestCase):
    def test_aggregate_medians_and_run_cv(self):
        runs = [synthetic_run("a", 1, settle_s=8.0, sensitivity=3.6), synthetic_run("a", 2, settle_s=12.0, sensitivity=3.7),
                synthetic_run("a", 3, settle_s=10.0, sensitivity=3.65)]
        c = tool.aggregate_candidate("a", runs)
        self.assertEqual(c["runs"], 3)
        self.assertEqual(c["offset_settle_s"], 10.0)
        self.assertEqual(c["offset_settle_worst_s"], 12.0)
        self.assertAlmostEqual(c["sensitivity_mean_mv"], 3.65, places=6)
        self.assertAlmostEqual(c["sensitivity_run_cv_percent"], 100 * np.std([3.6, 3.7, 3.65], ddof=1) / 3.65, places=6)
        self.assertEqual(c["disqualified"], [])
        self.assertEqual(c["production_passed_runs"], 3)

    def test_unsettled_run_counts_as_the_maximum_wait(self):
        c = tool.aggregate_candidate("a", [synthetic_run("a", 1, settled=False), synthetic_run("a", 2, settle_s=5.0)])
        self.assertEqual(c["offset_settle_worst_s"], tool.OFFSET_MAX_WAIT_S)
        self.assertEqual(c["never_settled_runs"], [1])
        self.assertEqual(c["disqualified"], [])

    def test_disqualifiers(self):
        never_band = tool.aggregate_candidate("b", [synthetic_run("b", 1, band_s=None, passed=False,
                                                                    fail_reasons=["Offset out of range: 1.4 V"])])
        self.assertEqual(len(never_band["disqualified"]), 2)
        self.assertIn("never entered", never_band["disqualified"][0])
        self.assertIn("Offset out of range", never_band["disqualified"][1])
        self.assertNotIn("1.4 V", never_band["disqualified"][1])
        unstable = tool.aggregate_candidate("c", [synthetic_run("c", 1, complete=False, passed=False, fail_reasons=["response did not stabilize: deadline"])])
        self.assertTrue(any("never stabilized" in d for d in unstable["disqualified"]))
        flip = tool.aggregate_candidate("d", [synthetic_run("d", 1, polarity="POSITIVE"), synthetic_run("d", 2, polarity="NEGATIVE")])
        self.assertTrue(any("polarity" in d for d in flip["disqualified"]))

    def test_ranking_prefers_fast_repeatable_and_sorts_disqualified_last(self):
        fast = tool.aggregate_candidate("fast", [synthetic_run("fast", 1, settle_s=6.0, sensitivity=3.60, stab_s=6.0),
                                                 synthetic_run("fast", 2, settle_s=7.0, sensitivity=3.61, stab_s=6.5)])
        slow = tool.aggregate_candidate("slow", [synthetic_run("slow", 1, settle_s=40.0, sensitivity=3.9, stab_s=15.0, drift=2.0),
                                                 synthetic_run("slow", 2, settle_s=45.0, sensitivity=3.6, stab_s=16.0, drift=2.5)])
        bad = tool.aggregate_candidate("bad", [synthetic_run("bad", 1, settle_s=1.0, complete=False, passed=False)])
        ranked = tool.rank_candidates([slow, bad, fast])
        self.assertEqual([c["label"] for c in ranked], ["fast", "slow", "bad"])
        self.assertEqual(ranked[0]["place"], 1)
        self.assertIsNone(ranked[2]["place"])
        self.assertIsNone(ranked[2]["score"])
        self.assertLess(ranked[0]["score"], ranked[1]["score"])
        self.assertEqual(ranked[0]["metric_ranks"]["offset_settle_s"], 1.0)
        text = tool.recommendation_text(ranked)
        self.assertIn("Recommended reference unit: fast", text)
        self.assertIn("Runner-up: slow", text)

    def test_ties_share_the_average_rank_and_missing_values_rank_worst(self):
        a = tool.aggregate_candidate("a", [synthetic_run("a", 1)])
        b = tool.aggregate_candidate("b", [synthetic_run("b", 1)])
        ranked = tool.rank_candidates([a, b], weights={"offset_settle_s": 1.0})
        self.assertEqual([c["score"] for c in ranked], [1.5, 1.5])
        b["offset_settle_s"] = None
        ranked = tool.rank_candidates([a, b], weights={"offset_settle_s": 1.0})
        self.assertEqual(ranked[0]["label"], "a")
        self.assertEqual(ranked[1]["metric_ranks"]["offset_settle_s"], 2.0)

    def test_no_eligible_candidate_is_said_plainly(self):
        bad = tool.aggregate_candidate("bad", [synthetic_run("bad", 1, complete=False, passed=False)])
        self.assertIn("No candidate is eligible", tool.recommendation_text(tool.rank_candidates([bad])))


class OutputGuardTests(unittest.TestCase):
    def test_production_evidence_folders_are_refused(self):
        with self.assertRaises(SystemExit):
            tool.guard_output_root(Path.home() / "Documents" / "Eltec_406MCA_Test_Results" / "v6_1_esp32" / "candidates")
        self.assertTrue(tool.guard_output_root(Path(tempfile.gettempdir()) / "Eltec_ReferenceCandidates").is_absolute())


class SimulatedRoundTripTests(unittest.TestCase):
    """capture --simulate for several personalities, then compare, in a temp dir."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "candidates"
        cls.output = io.StringIO()
        with redirect_stdout(cls.output):
            for profile in ("fast", "slow", "high_offset", "never_stable"):
                argv = ["capture", "--simulate", "--label", f"c_{profile}", "--sim-profile", profile, "--runs", "2",
                        "--hold-s", "10", "--out", str(cls.root)]
                assert tool.main(argv) == 0, profile
            cls.compare_rc = tool.main(["compare", "--out", str(cls.root), "--no-plot"])
        cls.text = cls.output.getvalue()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_each_run_writes_a_json_and_a_replayable_npz(self):
        jsons = sorted(self.root.rglob("*.json"))
        self.assertEqual(len(jsons), 8)
        record = json.loads(jsons[0].read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], tool.RUN_SCHEMA)
        for key in ("offset_pre", "drive", "hold", "offset_post", "drive_setup", "timing", "files"):
            self.assertIn(key, record)
        self.assertEqual(record["drive_setup"]["frequency_hz"], 10.0)
        self.assertEqual(record["drive_setup"]["duty_percent"], 50.0)
        self.assertEqual(record["drive_setup"]["measurement_cycles"], app.SENSITIVITY_MEASUREMENT_CYCLES)
        npz = np.load(jsons[0].parent / record["files"]["npz"])
        self.assertIn("waveform_v", npz.files)
        self.assertIn("sample_rate_hz", npz.files)
        self.assertGreater(npz["waveform_v"].size, 1000)
        self.assertEqual(npz["hold_waveform_v"].size, 10001)  # 10 s hold at 1000 SPS + the closing sample

    def test_fast_candidate_passes_production_and_is_recommended(self):
        fast = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.root / "c_fast").glob("*.json"))]
        self.assertTrue(all(r["drive"]["production_passed"] for r in fast))
        self.assertTrue(all(r["drive"]["measurement_complete"] for r in fast))
        self.assertTrue(all(r["offset_pre"]["settled"] for r in fast))
        self.assertLess(max(r["offset_pre"]["settle_s"] for r in fast), 20.0)
        self.assertEqual(self.compare_rc, 0)
        self.assertIn("Recommended reference unit: c_fast", self.text)

    def test_high_offset_and_wobbling_candidates_are_disqualified(self):
        self.assertIn("DISQUALIFIED c_high_offset", self.text)
        self.assertIn("never entered", self.text)
        self.assertIn("DISQUALIFIED c_never_stable", self.text)
        never = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.root / "c_never_stable").glob("*.json"))]
        self.assertTrue(all(not r["drive"]["measurement_complete"] for r in never))

    def test_slow_candidate_is_eligible_but_ranked_below_fast(self):
        slow = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.root / "c_slow").glob("*.json"))]
        self.assertTrue(all(r["drive"]["production_passed"] for r in slow))
        self.assertGreater(min(r["offset_pre"]["settle_s"] or 999 for r in slow), 20.0)
        self.assertTrue(all(r["drive"]["measurement_complete"] for r in slow))
        runs = tool.load_runs(self.root)
        ranked = tool.rank_candidates([tool.aggregate_candidate(l, r) for l, r in runs.items()])
        places = {c["label"]: c["place"] for c in ranked}
        self.assertEqual(places["c_fast"], 1)
        self.assertEqual(places["c_slow"], 2)
        self.assertIsNone(places["c_high_offset"])

    def test_comparison_csv_is_written_with_weights(self):
        csvs = list(self.root.glob("comparison_*.csv"))
        self.assertEqual(len(csvs), 1)
        content = csvs[0].read_text(encoding="utf-8")
        self.assertIn("c_fast", content)
        self.assertIn("offset_settle_s=3", content)

    def test_compare_filters_by_label_and_reports_nothing_found(self):
        with redirect_stdout(io.StringIO()) as out:
            rc = tool.main(["compare", "nothing_here", "--out", str(self.root), "--no-plot"])
        self.assertEqual(rc, 1)
        self.assertIn("no reference_candidate_qualifier runs", out.getvalue())


class SimulatedRigContractTests(unittest.TestCase):
    """The fake rig honours the backend contract the tool relies on."""

    def test_offset_read_is_refused_while_streaming(self):
        rig = tool.SimulatedRig(tool.profile_for_label("x", "fast"), tool.VirtualClock())
        rig.is_streaming = True
        with self.assertRaises(tool.StreamStateError):
            rig.read_offset_voltage()

    def test_reference_channel_reader_falls_back_to_the_raw_command(self):
        class Bare:
            is_streaming = False

            def _command(self, command, expect):
                assert command == "REF?" and expect == "REF,"
                return "REF,0.6543"

            @staticmethod
            def _parse_scalar_response(line, prefix):
                return float(line.split(",")[1])

        self.assertAlmostEqual(tool.make_offset_reader(Bare(), "ref")(), 0.6543)
        rig = tool.SimulatedRig(tool.profile_for_label("x", "fast"), tool.VirtualClock())
        self.assertIsInstance(tool.make_offset_reader(rig, "ref")(), float)

    def test_profile_choice_is_deterministic_per_label(self):
        self.assertEqual(tool.profile_for_label("refA")["name"], tool.profile_for_label("refA")["name"])
        self.assertEqual(tool.profile_for_label("refA", "slow")["name"], "slow")


class PrecisionReplayTests(unittest.TestCase):
    def test_reference_window_is_early_and_uses_the_production_average(self):
        rate = 1000.0
        t = np.arange(0, 5.001, 1.0 / rate)
        sync = ((np.arange(len(t)) % 100) < 50).astype(float)
        # Slow amplitude growth: the early reference and later DUT windows
        # have measurably different amplitudes even though deltas are quiet.
        waveform = 0.66 + (0.0015 + 0.00005 * t) * np.sin(2 * np.pi * 10 * t)
        replay = tool.reference_replay(waveform, sync, rate, 0.07)
        actual = tool.sa.analyze_stability(
            waveform, sync, rate, app.reference_stability_settings(app.load_stability_settings()),
            pwm_elapsed_offset_s=0.07, measurement_cycles_required=app.REFERENCE_MEASUREMENT_CYCLES,
            enforce_measurement_stability=False,
        )
        dut = tool.sa.analyze_stability(
            waveform, sync, rate, app.dut_stability_settings(app.load_stability_settings()),
            pwm_elapsed_offset_s=0.07, measurement_cycles_required=20, enforce_measurement_stability=True,
        )
        self.assertEqual(replay["reading_mv"], app.analyze_reference_stable_response_mv(actual))
        self.assertEqual(replay["ready_s"], actual.measurement_cycles[-1].end_elapsed_s)
        self.assertEqual(len(replay["cycle_pp_mv"]), 5)
        self.assertLess(replay["ready_s"], dut.measurement_cycles[-1].end_elapsed_s)
        late_first_five = np.mean([c.peak_to_peak_v * 1000 for c in dut.measurement_cycles[:5]])
        self.assertGreater(late_first_five - replay["reading_mv"], 0.02)

    def test_unstable_reference_never_gets_a_reading_or_a_ready_time(self):
        t = np.arange(0, 22.001, 0.001)
        sync = ((np.arange(len(t)) % 100) < 50).astype(float)
        waveform = 0.66 + 0.02 * t + 0.001 * np.sin(2 * np.pi * 10 * t)
        replay = tool.reference_replay(waveform, sync, 1000.0, 0.0)
        self.assertFalse(replay["measurement_complete"])
        self.assertIsNone(replay["reading_mv"])
        self.assertIsNone(replay["ready_s"])

    def test_timing_rounding_does_not_award_a_rank_for_submillisecond_jitter(self):
        candidates = [tool.aggregate_candidate("a", [synthetic_run("a", 1)]),
                      tool.aggregate_candidate("b", [synthetic_run("b", 1)])]
        candidates[0]["reference_ready_s"] = 2.9501
        candidates[1]["reference_ready_s"] = 2.9507
        ranked = tool.rank_candidates(candidates, {"reference_ready_s": 1.0}, precision=True)
        self.assertEqual([c["score"] for c in ranked], [1.5, 1.5])


class PrecisionCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def capture(self, label="p", *, runs=4):
        args = tool.build_parser().parse_args([
            "capture", "--precision", "--label", label, "--runs", str(runs),
            "--off-intervals-s", "2,10,60", "--hold-s", "3", "--out", str(self.root),
        ])
        clock = tool.VirtualClock()
        rig = tool.SimulatedRig({**tool.profile_for_label(label, "fast"), "noise_rms_mv": 0.005}, clock)
        prompts = []
        with redirect_stdout(io.StringIO()), mock.patch.object(rig, "reseat", wraps=rig.reseat) as reseat:
            written = tool.capture(args, rig_factory=lambda _port: rig, clock=clock, prompt=prompts.append)
            self.assertEqual(reseat.call_count, 0)  # operator seating only, never simulated/repeated seating
        self.assertEqual(len(prompts), 1)
        self.assertIn("LEAVE IT SEATED", prompts[0])
        return [json.loads(p.read_text()) for p in written]

    def test_repeated_readings_stay_seated_and_record_actual_off_intervals(self):
        records = self.capture()
        self.assertEqual(len(records), 4)
        self.assertEqual([r["precision"]["condition"] for r in records], ["initial", "off_2s", "off_10s", "off_60s"])
        self.assertGreater(len(records[0]["offset_pre"]["v"]), 1)
        for r in records[1:]:
            self.assertEqual(len(r["offset_pre"]["v"]), 1)
            self.assertGreaterEqual(r["precision"]["actual_off_s"], r["precision"]["requested_off_s"] - 1e-9)
        for r in records:
            self.assertEqual(r["protocol"], tool.PRECISION_PROTOCOL)
            self.assertTrue(r["reference"]["measurement_complete"])
            self.assertEqual(len(r["reference"]["cycle_pp_mv"]), 5)
            self.assertEqual(len(r["drive"]["cycle_pp_mv"]), 20)
            self.assertNotIn("_pwm_off_monotonic", r)
            with np.load(self.root / "precision" / "p" / r["files"]["npz"]) as z:
                replay = tool.reference_replay(z["waveform_v"], z["sync"], float(z["sample_rate_hz"]), r["drive"]["pwm_elapsed_offset_s"])
                self.assertEqual(replay, r["reference"])

    def test_compare_precision_excludes_screening_and_writes_precision_metrics(self):
        self.capture()
        screening = synthetic_run("p", 1)
        (self.root / "old.json").write_text(json.dumps(screening))
        self.assertEqual(len(tool.load_runs(self.root)["p"]), 1)
        with redirect_stdout(io.StringIO()) as out:
            rc = tool.main(["compare", "--precision", "--out", str(self.root), "--no-plot"])
        self.assertEqual(rc, 0)
        self.assertIn("ref CV %", out.getvalue())
        self.assertIn("Only 4 readings", out.getvalue())
        csv_path = next((self.root / "precision").glob("comparison_*.csv"))
        self.assertIn("reference_max_deviation_percent", csv_path.read_text())
        self.assertIn("reference_run_cv_percent=5", csv_path.read_text())

    def test_compare_rejects_different_test_protocols(self):
        records = self.capture()
        runs = {"p": records}
        tool.validate_precision_protocols(runs)
        records[1]["precision"]["off_intervals_s"] = [20.0]
        with self.assertRaisesRegex(ValueError, "different settings"):
            tool.validate_precision_protocols(runs)

    def test_different_simulated_candidates_can_be_compared_but_not_pooled_with_hardware(self):
        records = self.capture(runs=2)
        for index, record in enumerate(records):
            record["simulated"] = True
            record["firmware"] = f"SIMULATED-profile{index}"
        tool.validate_precision_protocols({"p": records})
        records[1]["simulated"] = False
        with self.assertRaisesRegex(ValueError, "different settings"):
            tool.validate_precision_protocols({"p": records})

    def test_one_reading_cannot_be_ranked_as_perfect_precision(self):
        records = self.capture(runs=1)
        result = tool.aggregate_precision_candidate("p", records)
        self.assertIsNone(result["reference_run_cv_percent"])
        self.assertTrue(result["disqualified"])

    def test_interrupted_session_cannot_win_on_only_its_completed_readings(self):
        records = self.capture(runs=3)
        result = tool.aggregate_precision_candidate("p", records[:2])
        self.assertIn("incomplete session", " ".join(result["disqualified"]))

    def test_unstable_reading_disqualifies_instead_of_disappearing_from_cv(self):
        records = self.capture(runs=3)
        records[1]["reference"].update(measurement_complete=False, reading_mv=None, ready_s=None)
        result = tool.aggregate_precision_candidate("p", records)
        self.assertEqual(result["reference_complete_runs"], 2)
        self.assertIn("incomplete in run(s) [2]", " ".join(result["disqualified"]))

    def test_spread_is_calculated_between_readings_not_individual_cycles(self):
        records = self.capture(runs=3)
        for r, mv in zip(records, [2.0, 2.02, 1.98]):
            r["reference"]["reading_mv"] = mv
        result = tool.aggregate_precision_candidate("p", records)
        self.assertAlmostEqual(result["reference_run_cv_percent"], 1.0)
        self.assertAlmostEqual(result["reference_max_deviation_percent"], 1.0)
        self.assertEqual(result["reference_complete_runs"], 3)

    def test_invalid_run_counts_and_nonfinite_delays_are_rejected(self):
        for option, value in [("--runs", "0"), ("--runs", "-2"), ("--hold-s", "nan"),
                              ("--off-intervals-s", "2,inf"), ("--off-intervals-s", "-1,2")]:
            with self.subTest(option=option, value=value), redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    tool.build_parser().parse_args(["capture", "--precision", "--label", "p", option + "=" + value])


if __name__ == "__main__":
    unittest.main()
