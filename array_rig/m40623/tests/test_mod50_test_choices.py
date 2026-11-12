"""Selected stages, simultaneous 80% settling, and linked retest accounting."""
import copy
import csv
import json
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
from mod50_settling import SettlingMonitor, NoiseWarmup
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

    def test_noise_warmup_uses_elapsed_samples_regardless_of_fluctuations(self):
        for rate in (1000., 999.9):
            for varying in (False, True):
                with self.subTest(rate=rate, varying=varying):
                    monitor = NoiseWarmup(rate, ['1-1'])
                    count = round(90 * rate)
                    t = np.arange(count) / rate
                    values = ((.001 + .0001 * t) * np.sin(2 * np.pi * 3 * t) + .0001 * t
                              if varying else np.zeros(count))
                    self.assertFalse(monitor.feed(values[None, :-1]))
                    self.assertTrue(monitor.feed(values[None, -1:]))
                    evidence = monitor.evidence()
                    self.assertAlmostEqual(evidence['waited_s'], 90.)
                    self.assertFalse(evidence['noise_stability_required'])
                    self.assertFalse(evidence['acceptance_limit'])


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
        record = result["records"][0]
        self.assertEqual(record["settling"]["waited_s"], 90.)
        self.assertEqual(record["settling"]["criterion"], "elapsed_time_only")
        self.assertNotIn("warning", record["settling"])
        self.assertNotIn("stability_observation", record)
        self.assertFalse(any("settled" in flag for row in result["rows"] for flag in row["flags"]))

    def test_stopping_noise_warmup_preserves_offset_without_a_noise_result(self):
        worker = self.worker()
        update = worker.update
        def stop(**values):
            update(**values)
            if values.get("phase") == "Warming up NOISE":
                worker.stop()
        worker.update = stop
        with patch.object(check, "DC_SECONDS", 1):
            worker.run()
        dc, noise = worker.result["records"]
        self.assertTrue(dc["valid_data"])
        self.assertFalse(noise["settling"]["complete"])
        self.assertFalse(noise["valid_data"])
        self.assertEqual(noise["samples"], 0)
        self.assertFalse(worker.result["data_complete"])
        self.assertTrue(all(row["legacy_equivalent_mv"] is None for row in worker.result["rows"]))

    def test_delivery_failure_during_noise_warmup_requires_a_fresh_full_wait(self):
        worker = self.worker(selected_tests=["noise"])
        def factory(**kwargs):
            rig = hw.Mod50Rig(**kwargs)
            read = rig.device.read_stream
            delivered = 0
            def fail_once(**options):
                nonlocal delivered
                delivered += 1
                if delivered == 4:
                    raise hw.daq.StreamTimeoutError("Injected noise warm-up timeout")
                return read(**options)
            rig.device.read_stream = fail_once
            return rig
        worker.rig_factory = factory
        with patch.object(check, "NOISE_SECONDS", 1):
            worker.run()
        self.assertTrue(worker.result["data_complete"], worker.result["errors"])
        first, retry = worker.result["records"]
        self.assertFalse(first["settling"]["complete"])
        self.assertEqual(first["samples"], 0)
        self.assertEqual(retry["settling"]["waited_s"], 90.)
        self.assertNotIn("reused_unchanged_path", retry["settling"])
        self.assertTrue((worker.directory / first["settling"]["raw_file"]).exists())

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

    def test_retest_all_after_daq_connection_failure_preserves_setup_and_part_counts(self):
        def unavailable(**kwargs):
            raise OSError("DAQ is not turned on")

        for positions, tests in ((hw.POSITIONS, ["dc", "noise"]), (("1-1", "3-4", "5-10"), ["noise"])):
            with self.subTest(count=len(positions), tests=tests):
                worker = self.worker(selected_tests=tests, installed_positions=positions,
                                     box_side="bottom", rig_factory=unavailable)
                worker.run()
                source = worker.result
                original = (worker.directory / "summary.json").read_bytes()
                self.assertFalse(source["data_complete"])
                self.assertEqual(source["records"], [])
                self.assertEqual(source["tray_summary"]["incomplete"], len(positions))
                reopened = web.Dashboard(simulate=True, output_root=self.tmp.name)
                self.assertEqual(reopened.saved_result["run_id"], source["run_id"])
                with patch.object(check.TrayCheck, "start", check.TrayCheck.run), \
                     patch.object(check, "DC_SECONDS", 1), patch.object(check, "NOISE_SECONDS", 1):
                    reopened.retest({"source_run_id": source["run_id"], "confirm_moved": True,
                                     "retest_mode": "all", "selected_tests": ["dc"],
                                     "lot": "DIFFERENT", "operator": "Other", "box_side": "top", "simulate": False,
                                     "moves": [{"part_id": "unknown", "position": "2-5"}]})
                repeated = reopened.worker.result
                self.assertTrue(repeated["data_complete"], repeated["errors"])
                self.assertEqual(repeated["installed_positions"], list(positions))
                self.assertEqual(repeated["selected_tests"], tests)
                self.assertEqual([r["mode"] for r in repeated["records"]], tests)
                self.assertEqual(repeated["part_mapping"], {p: source["run_id"] + ":" + p for p in positions})
                for key in ("lot", "operator", "tray", "box_side", "simulated", "family_id"):
                    self.assertEqual(repeated[key], source[key], key)
                self.assertEqual(repeated["tray_summary"]["tracked_parts"], len(positions))
                self.assertEqual(repeated["tray_summary"]["attempts"], 2 * len(positions))
                self.assertEqual((worker.directory / "summary.json").read_bytes(), original)
                self.assertNotEqual(reopened.worker.directory, worker.directory)

    def test_bottom_labels_survive_exports_reopening_and_retest_without_remapping_channels(self):
        worker = self.worker(selected_tests=["dc"], box_side="bottom")
        with patch.object(check, "DC_SECONDS", 1):
            worker.run()
        result = worker.result
        self.assertTrue(result["data_complete"], result["errors"])
        self.assertEqual(result["channel_map"], hw.CHANNEL_MAP)
        self.assertEqual([r["position"] for r in result["rows"]], list(hw.POSITIONS))
        labels = [f"{i // 10 + 6}-{i % 10 + 1}" for i in range(50)]
        self.assertEqual([r["detector_position"] for r in result["rows"]], labels)
        with (worker.directory / "results.csv").open(encoding="utf-8-sig", newline="") as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual([r["detector_position"] for r in exported], labels)
        self.assertTrue(all(r["box_side"] == "bottom" for r in exported))
        saved = json.loads((worker.directory / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["box_side"], "bottom")
        with np.load(worker.directory / "dc_raw.npz") as raw:
            metadata = json.loads(raw["metadata_json"].item())
            self.assertEqual(metadata["box_side"], "bottom")
            self.assertEqual(metadata["logical_to_physical"], list(hw.MAPPING))
        reopened = web.Dashboard(simulate=True, output_root=self.tmp.name, worker_factory=FakeWorker)
        self.assertEqual(reopened.snapshot()["run_metadata"]["box_side"], "bottom")
        part = result["current_parts"][0]
        self.assertEqual(part["origin_detector_position"], "6-1")
        reopened.retest({"source_run_id": result["run_id"], "confirm_moved": True,
                         "box_side": "top", "moves": [{"part_id": part["part_id"], "position": "5-10"}]})
        self.assertEqual(reopened.worker.options["box_side"], "bottom", "A retest must keep the saved side")
        self.assertEqual(reopened.worker.options["part_mapping"], {"5-10": part["part_id"]})

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

    def test_top_and_bottom_slot_warnings_and_repairs_stay_separate(self):
        for i in range(3):
            self.save(saved_run(f"TOP-{i}"))  # Older records without a side retain their 1-1 labels.
        for i in range(2):
            self.save({**saved_run(f"BOTTOM-{i}"), "box_side": "bottom"})
        self.assertEqual(self.history.warnings("bottom"), [])
        self.save({**saved_run("BOTTOM-2"), "box_side": "bottom"})
        warning = self.history.warnings("bottom")[0]
        self.assertEqual((warning["position"], warning["detector_position"]), ("1-1", "6-1"))
        self.history.verify_repairs(["1-1"], "Tech", "bottom")
        self.assertEqual(self.history.warnings("bottom"), [])
        self.assertEqual(len(self.history.warnings("top")), 1)
        dashboard = web.Dashboard(simulate=True, output_root=self.tmp.name, worker_factory=FakeWorker)
        self.assertEqual(dashboard.preflight({"simulate": True, "box_side": "bottom"})["slot_warnings"], [])
        dashboard.start({"lot": "NEW", "operator": "Tech", "simulate": True, "box_side": "bottom"})
        self.assertEqual(dashboard.worker.options["installed_positions"], hw.POSITIONS)

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

    def test_retest_all_after_individual_move_repeats_last_run_including_no_signal(self):
        original = saved_run("A", failure=False)
        original["rows"].append({**original["rows"][0], "position": "1-2"})
        self.save(original)
        moved = saved_run("B", failure=False, no_signal=True, position="2-5",
                          mapping={"2-5": "A:1-1"}, family="A")
        moved["installed_positions"] = ["2-5"]
        self.save(moved)
        dashboard = web.Dashboard(simulate=True, output_root=self.tmp.name, worker_factory=FakeWorker)
        dashboard.saved_result = moved
        dashboard.retest({"source_run_id": "B", "confirm_moved": True, "retest_mode": "all"})
        self.assertEqual(dashboard.worker.options["part_mapping"], {"2-5": "A:1-1"})
        self.assertEqual(dashboard.worker.options["installed_positions"], ("2-5",))
        self.assertEqual(dashboard.worker.options["family_id"], "A")
        self.assertEqual(len(self.history.current_parts("LOT", "A")), 2)

    def test_retest_all_preserves_confirmation_staleness_and_slot_checks(self):
        for run_id in ("A", "B", "C"):
            self.save(saved_run(run_id))
        dashboard = web.Dashboard(simulate=True, output_root=self.tmp.name, worker_factory=FakeWorker)
        dashboard.saved_result = saved_run("C")
        payload = {"source_run_id": "C", "confirm_moved": True, "retest_mode": "all"}
        for changes, error in (({"confirm_moved": False}, "Confirm"),
                               ({"source_run_id": "A"}, "no longer current"),
                               ({"retest_mode": "unknown"}, "Choose"),
                               ({}, "warning"),
                               ({"slot_warning_acknowledged": True}, "different retest slot")):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, error):
                dashboard.retest({**payload, **changes})
            self.assertIsNone(dashboard.worker)
            self.assertEqual(dashboard.saved_result["run_id"], "C")
        dashboard.retest({**payload, "slot_warning_acknowledged": True, "repair_positions": ["1-1"]})
        self.assertEqual(dashboard.worker.options["part_mapping"], {"1-1": "C:1-1"})

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
