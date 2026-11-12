"""Selected stages, simultaneous 80% settling, and linked retest accounting."""
import copy
import csv
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mod50_check as check
import mod50_hardware as hw
from mod50_history import History
from mod50_settling import SettlingMonitor
import mod50_web as web
from test_mod50_web import FakeWorker


class SettlingRule(unittest.TestCase):
    def monitor(self, mode="dc", count=50):
        return SettlingMonitor(1000, hw.daq.lsb_volts(hw.DC_RANGE if mode == "dc" else hw.NOISE_RANGE), hw.POSITIONS[:count], mode)

    def feed_mixed(self, monitor, stable_count, seconds=5):
        for second in range(seconds):
            t = (np.arange(1000) / 1000 + second)
            values = np.full((len(monitor.positions), 1000), .7)
            values[stable_count:] += .01 * t
            monitor.feed(values)
        return monitor.evidence()

    def test_40_of_50_starts_together_but_39_does_not(self):
        for good, expected in ((40, True), (39, False)):
            monitor = self.monitor()
            evidence = self.feed_mixed(monitor, good)
            self.assertEqual(evidence["complete"], expected)
            self.assertEqual(evidence["stable_count"], good)
            self.assertEqual(evidence["required_count"], 40)
            self.assertEqual(len(evidence["unstable_positions"]), 50 - good)

    def test_partial_population_rounds_up_and_requires_five_windows(self):
        monitor = self.monitor(count=3)
        evidence = self.feed_mixed(monitor, 3, 4)
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["required_count"], 3)
        monitor.feed(np.full((3, 1000), .7))
        self.assertTrue(monitor.complete)

    def test_changing_noise_amplitude_is_not_hidden_by_zero_mean(self):
        monitor = self.monitor("noise", 1)
        for second in range(8):
            y = (.001 + second * .002) * np.sin(2 * np.pi * 3 * np.arange(1000) / 1000)
            monitor.feed(y[None, :])
        self.assertFalse(monitor.complete)
        self.assertEqual(monitor.ever_stable, set())

    def test_stable_bad_offset_is_ready_but_not_accepted_as_good(self):
        monitor = self.monitor(count=1)
        monitor.feed(np.full((1, 5000), .2))
        self.assertTrue(monitor.complete)
        self.assertFalse(.3 <= .2 <= 1.2)


class SelectedStageRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def worker(self, **kwargs):
        return check.TrayCheck(lot="CHOICES", operator="Test", simulate=True, output_root=self.tmp.name, **kwargs)

    def test_noise_only_never_records_offset_or_infers_presence(self):
        worker = self.worker(selected_tests=["noise"])
        with patch.object(check, "NOISE_SECONDS", 1):
            worker.run()
        result = worker.result
        self.assertTrue(result["data_complete"], result["errors"])
        self.assertEqual([r["mode"] for r in result["records"]], ["noise"])
        self.assertFalse((worker.directory / "dc_raw.npz").exists())
        self.assertTrue(all(r["dc_check"] == "NOT_TAKEN" and r["dc_v"] is None and r["presence"] == "UNKNOWN" for r in result["rows"]))
        self.assertTrue(all(r["result"] in ("REVIEW_NO_NOISE_LIMIT", "CHECK") for r in result["rows"]))
        self.assertTrue(any(r["result"] == "REVIEW_NO_NOISE_LIMIT" for r in result["rows"]))
        with (worker.directory / "results.csv").open(encoding="utf-8-sig", newline="") as stream:
            saved = list(csv.DictReader(stream))
        self.assertEqual(saved[0]["dc_check"], "NOT_TAKEN")
        self.assertEqual(saved[0]["dc_v"], "")
        self.assertIn("NOT_TAKEN", saved[0]["test_status"])
        with np.load(worker.directory / "noise_raw.npz") as raw:
            self.assertEqual(raw["converter_counts"].shape, (1000, 57, 4))

    def test_offset_only_is_complete_without_noise_and_retest_replaces_same_part(self):
        worker = self.worker(selected_tests=["dc"], installed_positions=["1-1", "1-2"])
        with patch.object(check, "DC_SECONDS", 1):
            worker.run()
        first = worker.result
        self.assertTrue(first["data_complete"], first["errors"])
        self.assertEqual(first["rows"][0]["test_status"]["noise"], "NOT_TAKEN")
        self.assertEqual(first["tray_summary"]["tracked_parts"], 2)
        part_id = first["rows"][0]["part_id"]
        retest = self.worker(selected_tests=["dc"], installed_positions=["2-5"],
                             part_mapping={"2-5": part_id}, family_id=first["family_id"])
        original = (worker.directory / "summary.json").read_bytes()
        with patch.object(check, "DC_SECONDS", 1):
            retest.run()
        self.assertTrue(retest.result["data_complete"], retest.result["errors"])
        self.assertEqual(retest.result["tray_summary"]["tracked_parts"], 2)
        self.assertEqual(retest.result["tray_summary"]["attempts"], 3)
        self.assertEqual((worker.directory / "summary.json").read_bytes(), original)
        current = next(p for p in retest.result["current_parts"] if p["part_id"] == part_id)
        self.assertEqual((current["origin_position"], current["position"]), ("1-1", "2-5"))
        reopened = web.Dashboard(simulate=True, output_root=self.tmp.name, worker_factory=FakeWorker)
        restored = reopened.snapshot()["result"]
        self.assertEqual(restored["run_id"], retest.result["run_id"])
        self.assertEqual(restored["tray_summary"]["tracked_parts"], 2)

    def test_never_stable_falls_back_and_still_records_every_channel(self):
        class RampDaq(hw.DemoDaq):
            def _synth(self, scans, scan_hz):
                self._advance(scans / scan_hz)
                start = getattr(self, "sample_number", 0)
                self.sample_number = start + scans
                out = np.zeros((scans, 64))
                out[:, np.asarray(hw.MAPPING)] = (.2 + np.arange(start, start + scans)[:, None] * .00001)
                return out
        def factory(**kwargs):
            return hw.Mod50Rig(device=RampDaq(real_time=False), **kwargs)
        worker = self.worker(selected_tests=["dc"], rig_factory=factory)
        # Shortened test deadline verifies the fallback path, without claiming
        # this synthetic waveform qualifies the hardware or production limits.
        with patch.object(check, "SETTLING_MAX_SECONDS", 6), patch.object(check, "DC_SECONDS", 6):
            worker.run()
        result = worker.result
        self.assertTrue(result["data_complete"], result["errors"])
        record = result["records"][0]
        self.assertTrue(record["settling"]["deadline_fallback"])
        self.assertEqual(record["samples"], 6000)
        self.assertTrue(all(any("never settled" in f for f in r["flags"]) for r in result["rows"]))
        self.assertTrue(all(r["result"] == "CHECK" for r in result["rows"]))


def saved_run(run_id, *, failure=True, valid=True, position="1-1", mapping=None, family=None, lot="LOT", noise=False, no_signal=False):
    mode = "noise" if noise else "dc"
    row = {"position": position, "installed": True, "presence": "NO_SIGNAL" if no_signal else "UNKNOWN" if noise else "SIGNAL",
           "dc_check": "NOT_TAKEN" if noise else "OUTSIDE_WINDOW_PROVISIONAL" if failure else "IN_WINDOW_PROVISIONAL",
           "dc_v": None if noise else .2 if failure else .7,
           "acquisition_validity": {"dc": valid and not noise, "noise": valid and noise},
           "flags": [mode.upper() + ": signal problem"] if failure else []}
    return {"run_id": run_id, "family_id": family or run_id, "lot": lot, "created_at": "2026-11-11T12:00:00-05:00",
            "rows": [row], "selected_tests": [mode], "part_mapping": mapping or {}, "simulated": True,
            "tray": 1, "operator": "Tech", "data_complete": valid}


class RetestHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = History(self.tmp.name, True)

    def save(self, run):
        self.history.record(run, self.tmp.name)

    def test_fail_to_pass_to_fail_is_one_part_and_duplicate_import_is_idempotent(self):
        self.save(saved_run("A"))
        passed = saved_run("B", failure=False, position="3-5", mapping={"3-5": "A:1-1"}, family="A")
        self.save(passed); self.save(passed)
        summary = self.history.summary("LOT")
        self.assertEqual((summary["tracked_parts"], summary["failed"], summary["offset_pass"], summary["attempts"]), (1, 0, 1, 2))
        self.assertEqual(summary["screening_yield_percent"], 100)
        self.save(saved_run("C", mapping={"1-1": "A:1-1"}, family="A"))
        self.assertEqual(self.history.summary("LOT")["failed"], 1)
        self.assertEqual(self.history.summary("LOT")["tracked_parts"], 1)

    def test_invalid_retry_preserves_previous_assessment_and_stage_not_retested(self):
        self.save(saved_run("A"))
        self.save(saved_run("B", failure=False, valid=False, mapping={"1-1": "A:1-1"}, family="A"))
        self.assertEqual(self.history.summary("LOT")["failed"], 1)
        self.save(saved_run("C", failure=False, noise=True, mapping={"1-1": "A:1-1"}, family="A"))
        self.assertEqual(self.history.summary("LOT")["failed"], 1, "A noise retest cannot erase an offset failure")

    def test_slot_streak_persists_across_batches_ignores_invalid_and_requires_repair(self):
        self.save(saved_run("A", lot="FIRST")); self.save(saved_run("B", lot="SECOND"))
        self.save(saved_run("INVALID", failure=False, valid=False))
        self.save(saved_run("EMPTY", no_signal=True))
        self.assertEqual(self.history.warnings(), [])
        self.save(saved_run("C", lot="THIRD"))
        reopened = History(self.tmp.name, True)
        self.assertEqual([w["position"] for w in reopened.warnings()], ["1-1"])
        self.save(saved_run("GOOD", failure=False))
        self.assertEqual(len(reopened.warnings()), 1)
        reopened.verify_repairs(["1-1"], "Repair Tech")
        self.assertEqual(reopened.warnings(), [])
        self.assertFalse(History(self.tmp.name, False).path.exists())

    def test_nonfailure_breaks_streak_and_noise_review_never_becomes_pass(self):
        for name, fail in (("A", True), ("B", True), ("C", False), ("D", True), ("E", True)):
            self.save(saved_run(name, failure=fail, noise=True))
        self.assertEqual(self.history.warnings(), [])
        self.assertEqual(self.history.summary("LOT")["offset_pass"], 0)
        self.assertEqual(self.history.summary("LOT")["review"], 1)

    def test_noise_retest_clears_signal_failure_without_creating_an_acceptance_pass(self):
        self.save(saved_run("A", noise=True))
        self.save(saved_run("B", noise=True, failure=False, position="3-1", mapping={"3-1": "A:1-1"}, family="A"))
        summary = self.history.summary("LOT")
        self.assertEqual((summary["tracked_parts"], summary["failed"], summary["review"], summary["offset_pass"]), (1, 0, 1, 0))

    def test_preflight_excludes_suspect_slots_and_retest_checks_mapping(self):
        for run_id in ("A", "B", "C"):
            self.save(saved_run(run_id))
        dashboard = web.Dashboard(simulate=True, output_root=self.tmp.name, worker_factory=FakeWorker)
        payload = {"lot": "NEXT", "operator": "Tech", "simulate": True, "selected_tests": ["noise"]}
        with self.assertRaisesRegex(ValueError, "warning"):
            dashboard.start(payload)
        dashboard.start({**payload, "slot_warning_acknowledged": True})
        self.assertNotIn("1-1", dashboard.worker.options["installed_positions"])
        dashboard.worker.complete()
        dashboard.start({**payload, "slot_warning_acknowledged": True, "repair_positions": ["1-1"]})
        self.assertIn("1-1", dashboard.worker.options["installed_positions"])
        self.assertEqual(self.history.warnings(), [])
        dashboard.worker.complete()
        dashboard.saved_result = saved_run("A")
        dashboard.worker = None
        with self.assertRaises(ValueError):
            dashboard.retest({"confirm_moved": True, "source_run_id": "A", "moves": [
                {"part_id": "A:1-1", "position": "2-1"}, {"part_id": "A:1-1", "position": "2-2"}]})
        dashboard.retest({"confirm_moved": True, "source_run_id": "A", "selected_tests": ["dc"],
                          "moves": [{"part_id": "A:1-1", "position": "2-1"}]})
        self.assertEqual(dashboard.worker.options["part_mapping"], {"2-1": "A:1-1"})
        self.assertEqual(dashboard.worker.options["family_id"], "A")


if __name__ == "__main__":
    unittest.main()
