"""Tests for the 40623 array tester: paths, CSV, sensor numbering, lock, noise phase, save, GUI smoke.

Everything runs against ``SimulatedDaq(real_time=False)`` with the results
root redirected to a temporary directory - nothing is ever written under
the technician's Documents folder (a guard test checks that).
"""

from __future__ import annotations

import csv
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
MODEL_DIR = TESTS_DIR.parent
RIG_DIR = MODEL_DIR.parent
for entry in (str(MODEL_DIR), str(RIG_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import array_analysis as aa  # noqa: E402
import daq_backend as daq  # noqa: E402
import eltec_40623_array_tester as app  # noqa: E402
import tray_history  # noqa: E402

HOME_RESULTS = Path.home() / "Documents" / app.RESULTS_ROOT_NAME
FAST_PLAN = app.CapturePlan(capture_seconds=20.0, stabilisation_s=0.0, quiet_min_s=1.0, quiet_max_s=4.0)


def make_controller(tmp: Path, *, profile: daq.SimProfile | None = None, plan: app.CapturePlan = FAST_PLAN, lot: str = "7",
                    tray: int = 1, noise_limits: aa.NoiseLimits = aa.NoiseLimits()) -> tuple[app.TrayController, daq.SimulatedDaq]:
    sim = daq.SimulatedDaq(profile, real_time=False)
    controller = app.TrayController(sim, lot=lot, tray_number=tray, tester_name="JC", results_root=tmp, plan=plan, noise_limits=noise_limits)
    controller.start()
    return controller, sim


def resolve_unknowns(controller: app.TrayController, empty: tuple[str, ...] = ("1-10", "5-10")) -> None:
    controller.poll_offsets()
    for position in controller.unknown_positions():
        controller.set_occupancy(position, aa.Occupancy.EMPTY if position in empty else aa.Occupancy.LOADED)


class HomeGuardMixin:
    def assert_home_untouched(self):
        self.assertFalse(HOME_RESULTS.exists() and any(HOME_RESULTS.rglob("*lot_7*")),
                         "the tests wrote into the technician's Documents folder")


class PathTests(unittest.TestCase):
    def test_results_root_is_the_documented_location(self):
        root = app.results_root_dir()
        self.assertEqual(root.parts[-2:], ("Eltec_40623_Test_Results", "40623_array_daq"))
        self.assertEqual(root.parents[1].name, "Documents")

    def test_results_root_env_override(self):
        import os
        previous = os.environ.get(app.RESULTS_ROOT_ENV)
        os.environ[app.RESULTS_ROOT_ENV] = str(Path("C:/tmp/array_results") if os.name == "nt" else Path("/tmp/array_results"))
        try:
            self.assertEqual(app.results_root_dir().name, "array_results")
        finally:
            if previous is None:
                del os.environ[app.RESULTS_ROOT_ENV]
            else:
                os.environ[app.RESULTS_ROOT_ENV] = previous

    def test_lot_paths_and_collision_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(app.lot_results_path("A/7", root).name, "40623_array_lot_A_7.csv")
            first = app.raw_capture_path("7", 1, root)
            self.assertEqual(first.name, "tray_1_raw.npz")
            first.parent.mkdir(parents=True)
            first.write_bytes(b"x")
            self.assertEqual(app.raw_capture_path("7", 1, root).name, "tray_1_raw_2.npz")
            self.assertEqual(app.grid_snapshot_path("7", 3, root).name, "tray_3.png")
            self.assertEqual(app.safe_filename_part("  "), "unnamed")

    def test_attempts_path_sits_next_to_the_lot_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = app.TrayController(daq.SimulatedDaq(real_time=False), lot="9", tray_number=1, tester_name="x", results_root=Path(tmp))
            self.assertEqual(controller.attempts_path.name, "40623_array_lot_9_attempts.csv")


class NumberingTests(unittest.TestCase):
    def test_row_major_over_loaded_only(self):
        occupancy = [aa.Occupancy.LOADED] * 50
        occupancy[1] = aa.Occupancy.EMPTY
        occupancy[49] = aa.Occupancy.EMPTY
        numbers = app.assign_sensor_numbers(occupancy, 13)
        self.assertEqual(numbers["1-1"], 13)
        self.assertNotIn("1-2", numbers)
        self.assertEqual(numbers["1-3"], 14)
        self.assertEqual(numbers["5-9"], 13 + 47)
        self.assertNotIn("5-10", numbers)
        self.assertEqual(len(numbers), 48)
        with self.assertRaises(ValueError):
            app.assign_sensor_numbers(occupancy, 0)

    def test_next_number_continues_the_lot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(app.next_sensor_number_for_lot("7", root), 1)
            csv_path = app.lot_results_path("7", root)
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["sensor_number"])
                writer.writeheader()
                writer.writerow({"sensor_number": "12"})
            self.assertEqual(app.next_sensor_number_for_lot("7", root), 13)


class CsvTests(unittest.TestCase):
    def make_result(self, **kw) -> aa.PositionResult:
        base = dict(position="2-3", channel=12, occupancy=aa.Occupancy.LOADED, sensor_number=5, sensor_id="7-5",
                    offset_initial_v=0.6, offset_v=0.7, offset_early_v=0.69,
                    noise=aa.ChannelNoiseAnalysis(12, "2-3", 20, None, None, 0.12, 0.09, (0.09,) * 20, 0, aa.NoiseVerdict.NO_LIMIT))
        base.update(kw)
        return aa.judge_position(**base)

    def ctx(self, **kw) -> app.RowContext:
        base = dict(lot="7", tray_number=1, tray_attempt=1, tester_name="JC", daq_serial="ABC", simulated=True, plan=FAST_PLAN)
        base.update(kw)
        return app.RowContext(**base)

    def test_row_contents_and_stamps(self):
        row = app.position_row(self.make_result(), self.ctx(quiet_wait_s=3.0, quiet_settled=True, capture_seconds=20.0, actual_timer_hz=1000.0,
                                                             pool_events=0, stream_attempts=1, raw_capture_path="x.npz"))
        self.assertEqual(set(row), set(app.CSV_FIELDS))
        self.assertEqual((row["row"], row["col"], row["daq_channel"]), ("2", "3", "12"))
        self.assertEqual(row["pass_fail"], "PASS")
        self.assertEqual(row["verdict"], "PASS")
        self.assertEqual(row["verdict_status"], "PROVISIONAL")
        self.assertEqual(row["calibration_status"], "PENDING")
        self.assertEqual(row["calibration_id"], "40623_array50_daq_PENDING")
        self.assertEqual(row["noise_pp_limit_low_mv"], "")
        self.assertEqual(row["noise_pp_limit_high_mv"], "")
        self.assertIn("not derived", row["noise_limit_provenance"])
        self.assertEqual(row["noise_verdict"], "NO_LIMIT")
        self.assertEqual(row["noise_windows_over"], "")
        self.assertEqual(row["offset_v"], "0.700000")
        self.assertEqual(row["offset_settle_delta_v"], "0.01000")
        self.assertEqual(row["daq_range_code"], "2")
        self.assertEqual(row["daq_oversample"], "3")
        self.assertEqual(row["quiet_settled"], "YES")
        self.assertEqual(row["simulated"], "YES")
        self.assertEqual(row["model"], "40623")
        self.assertIn("no pin-level limit", row["warnings"])

    def test_fail_row_carries_reason_and_tag(self):
        row = app.position_row(self.make_result(offset_v=1.3), self.ctx(), failure_tag="SH", comment="  bent   pin ")
        self.assertEqual(row["pass_fail"], "FAIL")
        self.assertEqual(row["offset_class"], "HO")
        self.assertIn("High offset", row["fail_reasons"])
        self.assertEqual(row["failure_mode_tag"], "SH")
        self.assertEqual(row["operator_comments"], "bent pin")

    def test_append_keeps_an_older_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lot.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "position", "pass_fail", "legacy_only"])
                writer.writeheader()
                writer.writerow({"timestamp": "t0", "position": "1-1", "pass_fail": "PASS", "legacy_only": "x"})
            written = app.append_position_rows(path, [app.position_row(self.make_result(), self.ctx())])
            self.assertEqual(written, 1)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0], ["timestamp", "position", "pass_fail", "legacy_only"])
            self.assertEqual(rows[2][1:3], ["2-3", "PASS"])
            self.assertEqual(rows[2][3], "")
            self.assertEqual(app.append_position_rows(path, []), 0)

    def test_fresh_file_gets_the_full_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new" / "lot.csv"
            app.append_position_rows(path, [app.position_row(self.make_result(), self.ctx())])
            with path.open(newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
            self.assertEqual(header, app.CSV_FIELDS)


class ControllerFlowTests(unittest.TestCase, HomeGuardMixin):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.assert_home_untouched()

    def test_start_configures_and_calibrates(self):
        controller, sim = make_controller(self.root)
        self.assertIs(controller.phase, app.Phase.LOAD_OFFSET)
        self.assertEqual(sim.config, FAST_PLAN.config)
        self.assertEqual(sim.calibrations, 1)
        self.assertIsNone(controller.drive)

    def test_live_states_and_unknown_tiles(self):
        controller, _ = make_controller(self.root)
        controller.poll_offsets()
        self.assertEqual(controller.unknown_positions(), ("1-10", "5-2", "5-10"))
        self.assertIs(controller.live_tile_state("2-4"), aa.TileState.OFFSET_FAIL)
        self.assertIs(controller.live_tile_state("3-1"), aa.TileState.OFFSET_FAIL)
        self.assertIs(controller.live_tile_state("1-10"), aa.TileState.UNKNOWN)
        self.assertIs(controller.live_tile_state("1-1"), aa.TileState.LOADED)
        self.assertIs(controller.toggle_occupancy("1-10"), aa.Occupancy.EMPTY)
        self.assertIs(controller.live_tile_state("1-10"), aa.TileState.EMPTY)
        self.assertIs(controller.toggle_occupancy("1-10"), aa.Occupancy.LOADED)
        controller.set_occupancy("1-10", None)
        self.assertIs(controller.live_tile_state("1-10"), aa.TileState.UNKNOWN)

    def test_lock_refuses_unknown_zero_volt_tiles(self):
        controller, _ = make_controller(self.root)
        controller.poll_offsets()
        with self.assertRaises(ValueError) as ctx:
            controller.lock_tray()
        self.assertIn("1-10", str(ctx.exception))
        self.assertFalse(controller.csv_path.exists())

    def test_lock_assigns_numbers_and_writes_only_ho_rows(self):
        controller, _ = make_controller(self.root)
        resolve_unknowns(controller)
        lock = controller.lock_tray()
        self.assertEqual(len(lock.loaded_positions), 48)
        self.assertEqual(lock.ho_positions, ("2-4", "3-1"))
        self.assertEqual(lock.sensor_numbers["1-1"], 1)
        self.assertEqual(lock.sensor_numbers["2-4"], 13)
        self.assertEqual(lock.sensor_numbers["5-9"], 48)
        self.assertNotIn("1-10", lock.sensor_numbers)
        self.assertEqual(len(lock.measured_positions), 46)
        with controller.csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([r["position"] for r in rows], ["2-4", "3-1"])
        self.assertEqual({r["pass_fail"] for r in rows}, {"FAIL"})
        self.assertEqual(rows[0]["offset_class"], "HO")
        self.assertEqual(rows[1]["offset_class"], "HO_RAILED")
        self.assertEqual(rows[0]["noise_verdict"], "")
        self.assertIn("Failed fast", rows[0]["warnings"])
        events = tray_history.read_tray_events(controller.attempts_path)
        self.assertEqual([e.event for e in events], ["locked"])
        self.assertEqual(events[0].ho_positions, "2-4 3-1")
        self.assertEqual((events[0].first_sensor_number, events[0].last_sensor_number), (1, 48))
        self.assertIs(controller.phase, app.Phase.LOCKED)

    def test_lock_start_number_continues_the_lot_or_is_editable(self):
        csv_path = app.lot_results_path("7", self.root)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["sensor_number"])
            writer.writeheader()
            writer.writerow({"sensor_number": "12"})
        controller, _ = make_controller(self.root)
        resolve_unknowns(controller)
        self.assertEqual(controller.lock_tray().sensor_numbers["1-1"], 13)
        controller2, _ = make_controller(self.root, tray=2)
        resolve_unknowns(controller2)
        self.assertEqual(controller2.lock_tray(start_number=40).sensor_numbers["1-1"], 40)

    def test_noise_phase_rows_npz_png_and_events(self):
        controller, sim = make_controller(self.root)
        resolve_unknowns(controller)
        sim._virtual_t = 200.0  # settled
        lock = controller.lock_tray()
        report = controller.run_noise_phase(stabilisation_wait_s=0.0)
        self.assertIsNone(report.rig_fault)
        self.assertEqual(report.attempts_used, 1)
        self.assertEqual(len(report.results), 46)
        self.assertEqual({r.position for r in report.results}, set(lock.measured_positions))
        by = report.by_position
        self.assertIs(by["1-1"].verdict, aa.PositionVerdict.PASS)
        self.assertIs(aa.tile_state_for(by["1-1"]), aa.TileState.NO_LIMIT)
        self.assertIs(by["4-7"].verdict, aa.PositionVerdict.FAIL_OFFSET)
        self.assertEqual(by["4-7"].fail_reasons[0].code, "LO")
        self.assertIs(by["5-2"].verdict, aa.PositionVerdict.FAIL_OFFSET)
        self.assertEqual(by["5-2"].fail_reasons[0].code, "D")
        self.assertIsNotNone(by["1-1"].noise)
        self.assertGreater(by["3-6"].noise.worst_pp_mv, by["1-1"].noise.worst_pp_mv)  # the bursty position
        capture = report.capture
        self.assertEqual(capture.waveform_v.shape, (50, 20_000))
        self.assertEqual(capture.waveform_v.dtype, np.float32)
        self.assertEqual(capture.left_context_v.shape, (50, 310))
        self.assertEqual(capture.right_context_v.shape, (50, 310))
        self.assertGreaterEqual(capture.quiet_wait_s, FAST_PLAN.quiet_min_s)
        outcome = controller.save_tray()
        self.assertEqual(outcome["rows"], 46)
        with controller.csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 48)
        self.assertEqual(rows[0]["position"], "2-4")
        self.assertEqual({r["calibration_status"] for r in rows}, {"PENDING"})
        self.assertEqual({r["verdict_status"] for r in rows}, {"PROVISIONAL"})
        self.assertEqual(rows[-1]["stream_attempts"], "1")
        self.assertEqual(rows[-1]["capture_seconds"], "20.0")
        self.assertTrue(rows[-1]["raw_capture_path"].endswith("tray_1_raw.npz"))
        data = np.load(outcome["raw"])
        self.assertEqual(data["waveform_v"].shape, (50, 20_000))
        self.assertEqual(data["waveform_v"].dtype, np.float32)
        self.assertEqual(data["left_context_v"].shape, (50, 310))
        self.assertEqual(float(data["sample_rate_hz"]), 1000.0)
        self.assertEqual(str(data["calibration_id"]), "40623_array50_daq_PENDING")
        self.assertEqual(int(data["sensor_numbers"][0]), 1)
        self.assertEqual(str(data["positions"][49]), "5-10")
        self.assertEqual(str(data["ho_positions"]), "2-4 3-1")
        if outcome["png"] is not None:
            self.assertTrue(Path(outcome["png"]).is_file())
        events = [e.event for e in tray_history.read_tray_events(controller.attempts_path)]
        self.assertEqual(events, ["locked", "capture_started", "stabilisation_shortened", "judged", "saved"])
        self.assertIs(controller.phase, app.Phase.SAVED)
        with self.assertRaises(RuntimeError):
            controller.save_tray()

    def test_stream_integrity_retry_then_success(self):
        profile = daq.SimProfile(gap_on_attempts=frozenset({1}), empty_positions=frozenset({"1-10"}))
        controller, _ = make_controller(self.root, profile=profile)
        resolve_unknowns(controller)
        controller.lock_tray()
        report = controller.run_noise_phase(stabilisation_wait_s=300.0)
        self.assertIsNone(report.rig_fault)
        self.assertEqual(report.attempts_used, 2)
        events = [e.event for e in tray_history.read_tray_events(controller.attempts_path)]
        self.assertIn("capture_retry", events)
        self.assertNotIn("stabilisation_shortened", events)
        controller.save_tray()
        with controller.csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[-1]["stream_attempts"], "2")
        self.assertEqual(rows[-1]["stabilisation_wait_s"], "300.0")

    def test_all_attempts_fail_gives_not_measured_rows(self):
        profile = daq.SimProfile(gap_on_attempts=frozenset({1, 2, 3}), empty_positions=frozenset({"1-10"}))
        controller, _ = make_controller(self.root, profile=profile)
        resolve_unknowns(controller)
        controller.lock_tray()
        report = controller.run_noise_phase(stabilisation_wait_s=0.0)
        self.assertIsNotNone(report.rig_fault)
        self.assertEqual(report.attempts_used, 3)
        self.assertEqual(len(report.results), 49)
        self.assertTrue(all(r.verdict is aa.PositionVerdict.NOT_MEASURED for r in report.results))
        self.assertTrue(all(r.failure_mode_tag == "NM" for r in report.results))
        outcome = controller.save_tray()
        self.assertEqual(outcome["raw"], "")
        with controller.csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[-1]["pass_fail"], "NOT MEASURED")
        self.assertEqual(rows[-1]["failure_mode_tag"], "NM")
        self.assertEqual(rows[-1]["noise_worst_pp_mv"], "")
        events = [e.event for e in tray_history.read_tray_events(controller.attempts_path)]
        self.assertIn("capture_error", events)

    def test_silent_stream_times_out_into_the_rig_fault_path(self):
        # 2026-09-02 bench finding: a stream whose pacing clock ticks with the
        # trigger bit cleared delivers nothing at all. The tray must fail into
        # the retry / NOT MEASURED path, not sit at "quiet wait" forever.
        class SilentDaq(daq.SimulatedDaq):
            def read_stream(self, *, timeout_s: float = 1.0):
                self._require_config()
                if not self._streaming:
                    raise daq.StreamStateError("No stream is running.")
                return None

        plan = app.CapturePlan(capture_seconds=1.0, stabilisation_s=0.0, quiet_min_s=0.5, quiet_max_s=1.0,
                               no_data_timeout_s=0.05, retry_limit=1)
        sim = SilentDaq(daq.SimProfile(empty_positions=frozenset({"1-10"})), real_time=False)
        controller = app.TrayController(sim, lot="7", tray_number=1, tester_name="JC", results_root=self.root, plan=plan)
        controller.start()
        resolve_unknowns(controller)
        controller.lock_tray()
        started = time.monotonic()
        report = controller.run_noise_phase(stabilisation_wait_s=0.0)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIsNotNone(report.rig_fault)
        self.assertIn("no data from the stream", report.rig_fault)
        self.assertEqual(report.attempts_used, 2)
        self.assertFalse(sim.is_streaming)
        self.assertTrue(all(r.verdict is aa.PositionVerdict.NOT_MEASURED for r in report.results))
        events = [e.event for e in tray_history.read_tray_events(controller.attempts_path)]
        self.assertIn("capture_retry", events)
        self.assertIn("capture_error", events)

    def test_capture_plan_carries_the_no_data_timeout(self):
        self.assertEqual(app.CapturePlan().no_data_timeout_s, app.STREAM_NO_DATA_TIMEOUT_S)
        self.assertGreater(app.STREAM_NO_DATA_TIMEOUT_S, 1.0)  # several callback buffers of slack

    def test_remeasure_increments_attempt_and_keeps_numbers(self):
        controller, sim = make_controller(self.root)
        resolve_unknowns(controller)
        sim._virtual_t = 200.0
        lock = controller.lock_tray()
        controller.run_noise_phase(stabilisation_wait_s=0.0)
        controller.save_tray()
        self.assertEqual(controller.remeasure(), 2)
        self.assertIs(controller.phase, app.Phase.LOCKED)
        self.assertIsNone(controller.state.report)
        report = controller.run_noise_phase(stabilisation_wait_s=0.0)
        self.assertEqual(report.results[0].sensor_number, lock.sensor_numbers[report.results[0].position])
        controller.save_tray()
        with controller.csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2 + 46 + 46)
        self.assertEqual(rows[-1]["tray_attempt"], "2")
        events = [e.event for e in tray_history.read_tray_events(controller.attempts_path)]
        self.assertEqual(events.count("remeasure"), 1)
        self.assertEqual(events.count("saved"), 2)

    def test_noise_limits_colour_only_when_defined(self):
        limits = aa.NoiseLimits(low_mv=0.005, high_mv=0.5)
        controller, sim = make_controller(self.root, noise_limits=limits)
        resolve_unknowns(controller)
        sim._virtual_t = 200.0
        controller.lock_tray()
        report = controller.run_noise_phase(stabilisation_wait_s=0.0)
        by = report.by_position
        self.assertIs(by["1-1"].verdict, aa.PositionVerdict.PASS)
        self.assertIs(aa.tile_state_for(by["1-1"]), aa.TileState.PASS)
        self.assertIs(by["3-6"].verdict, aa.PositionVerdict.FAIL_NOISE_HIGH)  # bursty: 3 mV bursts over 0.5 mV
        self.assertIs(aa.tile_state_for(by["3-6"]), aa.TileState.NOISE_FAIL)
        counts = controller.summary_counts()
        self.assertEqual(counts["fail_noise"], 1)
        self.assertEqual(counts["no_limit"], 0)

    def test_ho_part_still_present_is_not_rejudged(self):
        controller, sim = make_controller(self.root)
        resolve_unknowns(controller)
        sim._virtual_t = 200.0
        lock = controller.lock_tray()
        report = controller.run_noise_phase(stabilisation_wait_s=0.0)
        self.assertNotIn("2-4", report.by_position)  # its fail-fast row already exists
        self.assertEqual(len(controller.state.lock_results), 2)
        self.assertEqual(controller.summary_counts()["fail_offset"], 4)  # 2 HO at lock + LO + D

    def test_tile_texts(self):
        self.assertEqual(app.tile_texts(None, live_offset_v=0.7), ("0.700 V", ""))
        result = aa.judge_position(position="1-1", channel=0, occupancy=aa.Occupancy.LOADED, sensor_number=1, sensor_id="7-1",
                                   offset_initial_v=0.6, offset_v=0.7,
                                   noise=aa.ChannelNoiseAnalysis(0, "1-1", 20, None, None, 0.1234, 0.09, (0.09,) * 20, 0, aa.NoiseVerdict.NO_LIMIT))
        self.assertEqual(app.tile_texts(result), ("0.700 V", "123 uV pp (no limit)"))

    def test_capture_plan_config_matches_the_production_constants(self):
        plan = app.CapturePlan()
        self.assertEqual(plan.config, daq.AdcConfig(range_code=2, oversample=3))
        self.assertEqual(plan.capture_seconds, 60.0)
        self.assertEqual(plan.stabilisation_s, 300.0)
        self.assertEqual(plan.edge_context_samples, 310)
        self.assertEqual(app.STREAM_BUFFER_BYTES % 512, 0)
        self.assertEqual(app.STREAM_BUFFER_BYTES % plan.config.scan_bytes, 0)
        self.assertIn(("Sensitivity", False), app.STEP_RAIL)


class LauncherIdentityTests(unittest.TestCase):
    FILES = ("run_eltec_40623_array_tester.cmd", "run_eltec_40623_array_tester.sh", "install_windows_launcher.ps1", "install_xubuntu_launcher.sh")

    def test_launchers_exist_and_carry_this_models_identity(self):
        for name in self.FILES:
            path = MODEL_DIR / name
            with self.subTest(name=name):
                self.assertTrue(path.is_file())
                content = path.read_text(encoding="utf-8")
                self.assertIn("eltec_40623_array_tester.py", content)
                self.assertNotIn("405", content)
                self.assertNotIn("ESP32", content)
                self.assertNotIn("eltec-rig", content)
        xubuntu = (MODEL_DIR / "install_xubuntu_launcher.sh").read_text(encoding="utf-8")
        self.assertIn("com.eltec.40623-array-tester.desktop", xubuntu)
        self.assertIn("Name=Eltec 40623 Array Tester", xubuntu)
        windows = (MODEL_DIR / "install_windows_launcher.ps1").read_text(encoding="utf-8")
        self.assertIn("$DisplayName = 'Eltec 40623 Array Tester'", windows)
        self.assertIn("Eltec_40623_Test_Results\\40623_array_daq", windows)
        cmd = (MODEL_DIR / "run_eltec_40623_array_tester.cmd").read_text(encoding="utf-8")
        self.assertIn("eltec-40623-array", cmd)

    def test_icons_are_bundled(self):
        self.assertTrue((MODEL_DIR / "assets" / "eltec_desktop_icon.png").is_file())
        self.assertTrue((MODEL_DIR / "assets" / "eltec_desktop_icon.ico").is_file())


class GuiSmokeTests(unittest.TestCase):
    def test_app_builds_with_the_simulator(self):
        try:
            _grid, app_class = app.build_gui_classes()
            window = app_class(device=daq.SimulatedDaq(real_time=False), simulate=True)
        except Exception as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        try:
            window.update_idletasks()
            self.assertEqual(len(window.grid._items), 50)
            self.assertIn("PENDING", window.banner.cget("text"))
            self.assertTrue(any("Sensitivity" in label.cget("text") and "no emitter" in label.cget("text") for label in window.step_labels))
            self.assertIsNone(window.drive)
            window.grid.set_tile("2-4", state=aa.TileState.OFFSET_FAIL, headline="1.620 V", detail="HO", sensor_number=13)
            self.assertEqual(window.grid.itemcget(window.grid._items["2-4"]["number"], "text"), "#13")
        finally:
            window.on_close()


if __name__ == "__main__":
    unittest.main()
