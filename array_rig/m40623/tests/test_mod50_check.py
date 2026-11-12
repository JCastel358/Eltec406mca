"""Current M25/HUB mapping, sequencing, settled recording and fault retention."""
from __future__ import annotations

import contextlib
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mod50_hardware as hw
import mod50_check as check
from test_daq_backend import FakeClock, make_device


class HardwareContract(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.device = hw.DemoDaq(real_time=False)
        self.rig = hw.Mod50Rig(simulate=True, device=self.device,
            monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.rig.connect()

    def tearDown(self):
        self.rig.close()

    def test_all_card_boundaries_and_unique_map(self):
        self.assertEqual(len(set(hw.MAPPING)), 50)
        self.assertEqual([hw.MAPPING[i] for i in (0, 15, 16, 24, 25, 40, 41, 49)],
                         [0, 15, 32, 40, 16, 31, 48, 56])
        self.assertFalse(set(range(41, 48)) & set(hw.MAPPING))
        self.assertEqual(hw.channel_identity(24)["daq_jack"], "J2")
        self.assertEqual(hw.channel_identity(25)["daq_jack"], "J1")
        self.assertEqual(self.rig.config.channels, 57)
        self.assertEqual(self.rig.scan_hz, 1000)
        self.assertEqual(self.rig.buffer_count, 64)

    def test_modes_hold_gain_off_and_repeat_settling(self):
        self.assertEqual(self.rig.range_code, hw.DC_RANGE)
        self.assertAlmostEqual(self.rig.settling_remaining_s, 90, delta=.1)
        self.clock.sleep(91)
        self.device.control_history.clear()
        self.rig.set_frontend_mode("noise")
        self.assertEqual(self.device.control_history, [(False, False), (True, False), (True, True)])
        self.assertIn(.2, self.clock.sleeps)
        self.assertEqual(self.rig.range_code, hw.NOISE_RANGE)
        self.assertAlmostEqual(self.rig.settling_remaining_s, 90)
        self.device.control_history.clear()
        self.rig.close()
        self.assertEqual(self.device.control_history, [(True, False), (False, False)])
        self.assertEqual(self.clock.sleeps[-1], .05)

    def test_each_ready_and_presence_loss_refuses_capture(self):
        for bit in (2, 5, 6, 7):
            with self.subTest(bit=bit):
                self.device.status_override = 1 << bit
                with self.assertRaises(hw.daq.StreamIntegrityError):
                    self.rig._verify((False, False))
        self.device.status_override = None

    def test_each_route_gain_mismatch_is_detected(self):
        for bit in (0, 1, 3, 4):
            self.device.status_override = 1 << bit
            with self.assertRaises(hw.daq.StreamIntegrityError):
                self.rig._verify((False, False))
        self.device.status_override = None

    def test_conversion_zero_discarded_and_clipped_retained_slots_detected(self):
        raw = np.full((10, 57, 4), 32768, dtype=np.uint16)
        raw[:, :, 0] = 0
        volts, clipped = hw.convert_chunk(raw, hw.NOISE_RANGE)
        self.assertTrue(np.all(volts == 0))
        self.assertFalse(np.any(clipped))
        raw[0, 56, 2] = 63
        _, clipped = hw.convert_chunk(raw, hw.NOISE_RANGE)
        self.assertTrue(clipped[56, 0])

    def test_malformed_scans_refused(self):
        for raw in (np.zeros((3, 50, 4)), np.zeros((0, 57, 4)), np.full((3, 57, 4), np.nan)):
            with self.assertRaises(hw.daq.StreamIntegrityError):
                hw.convert_chunk(raw, hw.NOISE_RANGE)

    def test_real_backend_retains_all_eight_status_bits(self):
        device, lib, clock = make_device()
        device.connect()
        lib.DIO_Configure = lambda *args: 0
        def read(index, data):
            data[1] = 0xD8
            return 0
        lib.DIO_ReadAll = read
        with self.assertRaises(hw.daq.StreamStateError):
            device.read_mod50_status()
        device.configure_r4_dio(dedicated_ports=True)
        self.assertEqual(device.read_mod50_status(), 0xD8)
        self.assertEqual(device.read_r4_controls(), (False, False, False))
        device.close()


class TraySequence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rig = None

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, **kwargs):
        self.rig = hw.Mod50Rig(**kwargs)
        return self.rig

    def worker(self, **kwargs):
        return check.TrayCheck(lot="TEST", operator="Test only", simulate=True,
                              output_root=self.tmp.name, rig_factory=self.factory, **kwargs)

    def run_short(self, worker):
        with patch.object(check, "DC_SECONDS", 1.), patch.object(check, "NOISE_SECONDS", 1.):
            worker.run()
        return worker.result

    def test_full_50_piece_run_preserves_raw_map_and_waits(self):
        worker = self.worker()
        worker.run()
        result = worker.result
        self.assertTrue(result["data_complete"], result["errors"])
        self.assertEqual(len(result["rows"]), 50)
        self.assertEqual([r["samples"] for r in result["records"]], [10000, 100000])
        self.assertTrue(all(r["settling"]["complete"] and 5 <= r["settling"]["waited_s"] < 90 for r in result["records"]))
        self.assertEqual(self.rig.device.controls, (False, False))
        self.assertTrue(all(r["result"] != "PASS" for r in result["rows"]))
        self.assertTrue(all(.59 < r["dc_v"] < .86 for r in result["rows"]))
        self.assertIsNone(result["noise_acceptance_limit"])
        for mode, n in (("dc", 10000), ("noise", 100000)):
            with np.load(worker.directory / f"{mode}_raw.npz", allow_pickle=False) as data:
                self.assertEqual(data["converter_counts"].shape, (n, 57, 4))
                meta = json.loads(str(data["metadata_json"].item()))
                self.assertEqual(meta["logical_to_physical"], list(hw.MAPPING))
                self.assertTrue(meta["simulated"])
                self.assertEqual(meta["voltage_reference"], "DAQ_OUTPUT")
        self.assertTrue((worker.directory / "report.html").exists())
        self.assertTrue((worker.directory / "results.csv").exists())

    def test_subset_preserves_scan_and_does_not_infer_empty(self):
        worker = self.worker(installed_positions=("1-1", "3-7", "5-10"))
        result = self.run_short(worker)
        self.assertTrue(result["data_complete"], result["errors"])
        self.assertEqual(sum(r["installed"] for r in result["rows"]), 3)
        self.assertEqual(result["rows"][1]["result"], "NOT_INSTALLED")
        with np.load(worker.directory / "dc_raw.npz") as data:
            self.assertEqual(data["converter_counts"].shape[1:], (57, 4))

    def test_stop_in_settling_does_not_start_stream(self):
        worker = self.worker()
        update = worker.update
        def stopping(**values):
            update(**values)
            if values.get("phase") == "Settling DC":
                worker.stop()
        worker.update = stopping
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertEqual(result["records"][0]["samples"], 0)
        self.assertEqual(self.rig.device.controls, (False, False))
        self.assertTrue((worker.directory / "summary.json").exists())

    def test_stop_in_noise_retains_partial_and_invalidates_noise(self):
        worker = self.worker()
        update = worker.update
        def stopping(**values):
            update(**values)
            if values.get("phase") == "Recording NOISE":
                worker.stop()
        worker.update = stopping
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertTrue(result["records"][0]["valid_data"])
        self.assertGreater(result["records"][1]["samples"], 0)
        self.assertTrue(all(r["daq_3hz_q3_rms_uv"] is None for r in result["rows"]))
        self.assertEqual(self.rig.device.controls, (False, False))

    def test_card_loss_after_delivery_keeps_evidence_and_stops(self):
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            original = rig.device.read_stream
            def read(**options):
                raw = original(**options)
                rig.device.power_ready = False
                return raw
            rig.device.read_stream = read
            return rig
        worker = self.worker()
        worker.rig_factory = factory
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertEqual(len(result["records"]), 1)
        self.assertGreater(result["records"][0]["settling"]["observed_samples"], 0)
        self.assertTrue((worker.directory / result["records"][0]["settling"]["raw_file"]).is_file())
        self.assertEqual(self.rig.device.controls, (False, False))

    def test_shutdown_failure_prevents_complete(self):
        worker = self.worker()
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            close = rig.close
            def fail_close():
                close()
                raise RuntimeError("injected shutdown failure")
            rig.close = fail_close
            return rig
        worker.rig_factory = factory
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertTrue(any("injected shutdown" in e for e in result["errors"]))

    def test_lease_failure_never_connects(self):
        @contextlib.contextmanager
        def unavailable():
            raise RuntimeError("DAQ already in use")
            yield
        worker = self.worker(lease_factory=unavailable)
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertIsNone(self.rig)

    def test_repeated_delivery_timeout_preserves_three_attempts_then_stops(self):
        worker = self.worker()
        calls = []
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            read = rig.device.read_stream
            def fail(**options):
                calls.append(1)
                if len(calls) > 1:
                    raise hw.daq.StreamTimeoutError("Injected stall")
                return read(**options)
            rig.device.read_stream = fail
            return rig
        worker.rig_factory = factory
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["recovery"]["attempts_by_stage"], {"dc": 3, "noise": 0})
        self.assertEqual(result["recovery"]["recovered_modes"], [])
        self.assertEqual([r["raw_file"] for r in result["records"]],
                         ["dc_raw.npz", "dc_attempt02_raw.npz", "dc_attempt03_raw.npz"])
        for record in result["records"]:
            path = worker.directory / record["raw_file"]
            self.assertTrue(path.exists())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), record["raw_sha256"])
        self.assertGreater(result["records"][0]["settling"]["observed_samples"], 0)
        self.assertEqual(result["records"][1]["samples"], 0)
        self.assertEqual(result["records"][1]["settling"]["waited_s"], 0)
        self.assertTrue(all(row["flags"] == [] for row in result["rows"]))
        self.assertTrue(all(row["acquisition_errors"] for row in result["rows"]))

    def test_noise_retry_keeps_dc_and_settling_and_never_joins_rejected_samples(self):
        worker = self.worker()
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            read = rig.device.read_stream
            delivered = 0
            def fail_once(**options):
                nonlocal delivered
                if rig.mode == "noise" and rig.device.stream_attempts == 2 and worker.records[-1]["settling"]["complete"]:
                    delivered += 1
                    if delivered == 2:
                        raise hw.daq.StreamTimeoutError("Injected NOISE delivery timeout")
                    raw = read(**options)
                    raw[:] = 60000  # unmistakable rejected samples, never valid noise
                    return raw
                return read(**options)
            rig.device.read_stream = fail_once
            return rig
        worker.rig_factory = factory
        result = self.run_short(worker)
        self.assertTrue(result["data_complete"], result["errors"])
        self.assertEqual(result["errors"], [])
        self.assertEqual([r["mode"] for r in result["records"]], ["dc", "noise", "noise"])
        self.assertEqual([r["valid_data"] for r in result["records"]], [True, False, True])
        self.assertGreaterEqual(result["records"][1]["settling"]["waited_s"], 5)
        self.assertEqual(result["records"][2]["settling"]["waited_s"], 0)
        self.assertEqual(self.rig.device.control_history.count((True, True)), 1)
        self.assertEqual(result["recovery"]["recovered_modes"], ["noise"])
        self.assertEqual(len(result["recovery"]["rejected_attempts"]), 1)
        with np.load(worker.directory / "noise_raw.npz") as rejected:
            self.assertTrue(np.all(rejected["converter_counts"] == 60000))
        with np.load(worker.directory / "noise_attempt02_raw.npz") as accepted:
            self.assertEqual(accepted["converter_counts"].shape, (1000, 57, 4))
            self.assertLess(accepted["converter_counts"].max(), 40000)
        self.assertTrue(all(row["acquisition_validity"] == {"dc": True, "noise": True} for row in result["rows"]))
        self.assertTrue(all(not row["acquisition_errors"] and not row["flags"] for row in result["rows"]))

    def test_explicit_driver_pool_interruption_retries_a_fresh_stage(self):
        worker = self.worker()
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            rig.device.profile = replace(rig.device.profile, pool_events_on_attempts=frozenset({2}))
            return rig
        worker.rig_factory = factory
        result = self.run_short(worker)
        self.assertTrue(result["data_complete"], result["errors"])
        self.assertEqual(result["records"][1]["failure_kind"], "delivery_interrupted")
        self.assertTrue(result["records"][1]["retry_eligible"])
        self.assertEqual(result["records"][2]["samples"], 1000)

    def test_rate_failure_alone_does_not_retry_or_weaken_acceptance(self):
        worker = self.worker()
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            rig.device.profile = replace(rig.device.profile, gap_on_attempts=frozenset({1}))
            return rig
        worker.rig_factory = factory
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertEqual(len(result["records"]), 1)
        self.assertFalse(result["records"][0]["retry_eligible"])
        self.assertTrue(any("scan rate" in message for message in result["errors"]))

    def test_timeout_never_retries_unconfirmed_shutdown_or_bad_timer(self):
        for field, value in (("clock_stop_status", 5), ("bulk_end_status", 6),
                             ("bulk_worker_status", 7), ("cleanup_error", "Worker remains alive"),
                             ("actual_timer_hz", 950.)):
            with self.subTest(field=field):
                worker = self.worker()
                def factory(**kwargs):
                    rig = self.factory(**kwargs)
                    def fail_read(**options):
                        raise hw.daq.StreamTimeoutError("Injected timeout")
                    rig.device.read_stream = fail_read
                    stop = rig.device.stop_stream
                    def bad_stop(**options):
                        diagnostics = stop(**options)
                        setattr(diagnostics, field, value)
                        return diagnostics
                    rig.device.stop_stream = bad_stop
                    return rig
                worker.rig_factory = factory
                result = self.run_short(worker)
                self.assertFalse(result["data_complete"])
                self.assertEqual(len(result["records"]), 1)
                self.assertFalse(result["records"][0]["retry_eligible"])

    def test_timeout_never_retries_after_card_or_signal_path_change(self):
        for change in ("power", "generation", "config"):
            with self.subTest(change=change):
                worker = self.worker()
                def factory(**kwargs):
                    rig = self.factory(**kwargs)
                    def changed_read(**options):
                        if change == "power":
                            rig.device.power_ready = False
                        elif change == "generation":
                            rig.generation += 1
                        else:
                            rig.device.config = replace(rig.device.config, range_code=7)
                        raise hw.daq.StreamTimeoutError("Timeout with changed fixture state")
                    rig.device.read_stream = changed_read
                    return rig
                worker.rig_factory = factory
                result = self.run_short(worker)
                self.assertFalse(result["data_complete"])
                self.assertEqual(len(result["records"]), 1)
                self.assertFalse(result["records"][0]["retry_eligible"])

    def test_stop_during_retry_backoff_never_starts_another_attempt(self):
        worker = self.worker()
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            def timeout(**options):
                raise hw.daq.StreamTimeoutError("Injected timeout")
            rig.device.read_stream = timeout
            return rig
        worker.rig_factory = factory
        update = worker.update
        def stop_retry(**values):
            update(**values)
            if values.get("phase", "").startswith("Retrying"):
                worker.stop()
        worker.update = stop_retry
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(self.rig.device.stream_attempts, 1)

    def resume_input(self, worker):
        record = next(r for r in reversed(worker.result["records"]) if r["mode"] == "dc")
        return {"record": copy.deepcopy(record), "raw_path": worker.directory / record["raw_file"],
                "source": {"directory": str(worker.directory), "daq_serial": "SIMULATED",
                           "profile": hw.PROFILE, "map_sha256": hw.MAP_SHA256,
                           "schema": check.SCHEMA, "timing_policy": check.TIMING_POLICY}}

    def test_resume_copies_dc_unchanged_and_only_records_new_settled_noise(self):
        original = self.worker()
        self.run_short(original)
        resume = self.resume_input(original)
        before = {p.name: p.read_bytes() for p in original.directory.iterdir()}
        worker = self.worker(resume_dc=resume)
        result = self.run_short(worker)
        self.assertTrue(result["data_complete"], result["errors"])
        self.assertNotEqual(worker.directory, original.directory)
        self.assertEqual(self.rig.device.stream_attempts, 1)
        self.assertEqual([r["mode"] for r in result["records"]], ["dc", "noise"])
        self.assertEqual(result["resumed_from"], resume["source"])
        self.assertIn("reused_from", result["records"][0])
        self.assertGreaterEqual(result["records"][1]["settling"]["waited_s"], 5)
        self.assertEqual((worker.directory / "dc_raw.npz").read_bytes(), before["dc_raw.npz"])
        self.assertEqual({p.name: p.read_bytes() for p in original.directory.iterdir()}, before)

    def test_resume_changed_source_hash_is_refused_before_hardware_connection(self):
        original = self.worker()
        self.run_short(original)
        resume = self.resume_input(original)
        resume["record"]["raw_sha256"] = "0" * 64
        self.rig = None
        worker = self.worker(resume_dc=resume)
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertIsNone(self.rig)
        self.assertTrue(any("changed after validation" in message for message in result["errors"]))

    def test_resume_preserves_accepted_second_attempt_filename_and_raw_metadata(self):
        original = self.worker()
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            rig.device.profile = replace(rig.device.profile, pool_events_on_attempts=frozenset({1}))
            return rig
        original.rig_factory = factory
        self.run_short(original)
        resume = self.resume_input(original)
        self.assertEqual(resume["record"]["raw_file"], "dc_attempt02_raw.npz")
        worker = self.worker(resume_dc=resume)
        result = self.run_short(worker)
        self.assertTrue(result["data_complete"], result["errors"])
        copied = result["records"][0]
        self.assertEqual(copied["raw_file"], "dc_attempt02_raw.npz")
        self.assertFalse((worker.directory / "dc_raw.npz").exists())
        self.assertEqual((worker.directory / copied["raw_file"]).read_bytes(), resume["raw_path"].read_bytes())
        with np.load(worker.directory / copied["raw_file"], allow_pickle=False) as data:
            metadata = json.loads(str(data["metadata_json"].item()))
            self.assertEqual(metadata["raw_file"], copied["raw_file"])
            self.assertEqual(metadata["attempt"], copied["attempt"])

    def test_resume_wrong_daq_or_profile_never_starts_noise(self):
        original = self.worker()
        self.run_short(original)
        for field in ("daq_serial", "profile", "map_sha256"):
            with self.subTest(field=field):
                resume = self.resume_input(original)
                resume["source"][field] = "WRONG"
                self.rig = None
                worker = self.worker(resume_dc=resume)
                result = self.run_short(worker)
                self.assertFalse(result["data_complete"])
                if self.rig is not None:
                    self.assertEqual(self.rig.device.stream_attempts, 0)

    def test_raw_save_failure_stops_before_noise_and_releases_controls(self):
        worker = self.worker()
        with patch.object(np, "savez_compressed", side_effect=OSError("Disk unavailable")):
            result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertEqual(len(result["records"]), 1)
        self.assertTrue(any("Disk unavailable" in e for e in result["errors"]))
        self.assertEqual(self.rig.device.controls, (False, False))

    def test_source_mismatch_is_refused_before_recording(self):
        worker = self.worker()
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            connect = rig.connect
            def mismatch():
                from dataclasses import replace
                return replace(connect(), simulated=False)
            rig.connect = mismatch
            return rig
        worker.rig_factory = factory
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertEqual(result["records"], [])

    def test_real_settling_reads_are_interruptible_before_next_driver_read(self):
        worker = self.worker()
        worker.simulate = False
        clock = FakeClock()
        rig = hw.Mod50Rig(simulate=True, monotonic=clock.monotonic, sleep=clock.sleep)
        rig.connect()
        record = {"mode": "dc", "range_code": hw.DC_RANGE, "status_samples": []}
        with patch.object(worker, "update", side_effect=lambda **values: worker.stop()), patch.object(rig.device, "read_stream") as read:
            with self.assertRaises(hw.daq.StreamIntegrityError):
                worker.wait_settled(rig, record, 0, .3, 1000, [])
        read.assert_not_called()
        self.assertFalse(record["settling"]["complete"])
        rig.close()

    def test_invalid_granted_clock_still_saves_a_readable_failure(self):
        worker = self.worker()
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            start = rig.device.start_stream
            def bad_rate(**options):
                from dataclasses import replace
                return replace(start(**options), actual_timer_hz=float("nan"))
            rig.device.start_stream = bad_rate
            return rig
        worker.rig_factory = factory
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertTrue((worker.directory / "summary.json").exists())
        self.assertTrue(any("clock" in e for e in result["errors"]))

    def test_unconfirmed_shutdown_requires_app_restart(self):
        worker = self.worker()
        def factory(**kwargs):
            rig = self.factory(**kwargs)
            close = rig.close
            def incomplete_close():
                rig.device._shutdown_failed = True
                close()
            rig.close = incomplete_close
            return rig
        worker.rig_factory = factory
        result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertTrue(result["restart_required"])

    def test_report_failure_does_not_commit_complete_summary(self):
        worker = self.worker()
        original = Path.write_text
        def fail_report(path, *args, **kwargs):
            if path.name == "report.html":
                raise OSError("Injected report write failure")
            return original(path, *args, **kwargs)
        with patch.object(Path, "write_text", fail_report):
            result = self.run_short(worker)
        self.assertFalse(result["data_complete"])
        self.assertFalse((worker.directory / "summary.json").exists())
        self.assertTrue((worker.directory / "noise_raw.npz").exists())

    def test_invalid_inputs_and_duplicate_positions(self):
        for options in ({"lot": ""}, {"operator": ""}, {"tray": 0},
                        {"installed_positions": ()}, {"installed_positions": ("1-1", "1-1")}):
            values = dict(lot="X", operator="Y", simulate=True, output_root=self.tmp.name)
            values.update(options)
            with self.assertRaises(ValueError):
                check.TrayCheck(**values)


class MeasurementQuality(unittest.TestCase):
    def test_noise_rms_and_headroom(self):
        hz, n = 1000, 10000
        y = .01 * np.sin(2 * np.pi * 3 * np.arange(n) / hz)
        counts = np.rint((y + 1) * 32768).astype(np.uint16)
        raw = np.repeat(counts[:, None, None], 57, axis=1)
        raw = np.repeat(raw, 4, axis=2)
        rows = check.channel_statistics(raw, hw.NOISE_RANGE, hz, "noise")
        self.assertAlmostEqual(rows[0]["daq_3hz_q3_rms_uv"], .01 / np.sqrt(2) * 1e6, delta=1.)
        raw[:, 56, :] = 63
        rows = check.channel_statistics(raw, hw.NOISE_RANGE, hz, "noise")
        self.assertIsNone(rows[49]["daq_3hz_q3_rms_uv"])
        self.assertTrue(rows[49]["near_range_limit"])

    def test_default_entrypoint_selects_mod50_and_legacy_is_explicit(self):
        import eltec_40623_array_tester as legacy
        import mod50_array_tester as current
        with patch.object(current, "main", return_value=0) as run:
            self.assertEqual(legacy.main(["--simulate"]), 0)
        run.assert_called_once_with(["--simulate"])


if __name__ == "__main__":
    unittest.main()
