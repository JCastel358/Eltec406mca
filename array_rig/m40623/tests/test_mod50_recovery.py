"""Saved-stage recovery eligibility and immutable raw-evidence validation."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mod50_check as check
import mod50_hardware as hw
from mod50_recovery import load_noise_recovery


class SavedNoiseRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "saved-check"
        self.folder.mkdir()
        self.raw = np.full((10000, 57, 4), 37000, dtype=np.uint16)
        clean = {"nominal_scan_hz": 1000., "actual_timer_hz": 1000., "started_monotonic": 100.,
                 "stopped_monotonic": 110., "scans_received": 10000, "pool_too_small_events": 0,
                 "ended_early": False, "callback_error": None, "cleanup_error": None,
                 "clock_stop_status": 0, "bulk_end_status": 0, "bulk_worker_status": 0}
        self.dc = {"mode": "dc", "requested_seconds": 10., "errors": [], "range_code": hw.DC_RANGE,
                   "settling": {"complete": True, "waited_s": 90., "required_at_start_s": 90.},
                   "valid_data": True, "status_before": hw.decode_status(0), "status_after": hw.decode_status(0),
                   "status_samples": [{"samples": 10000, "raw_byte": 0}], "diagnostics": clean,
                   "started_at": "2026-11-04T13:05:43-05:00", "samples": 10000, "target_samples": 10000,
                   "actual_rate_hz": 1000., "recorded_seconds": 10., "raw_file": "dc_raw.npz",
                   "rows": check.channel_statistics(self.raw, hw.DC_RANGE, 1000., "dc", valid_data=True)}
        self.noise = copy.deepcopy(self.dc)
        self.noise.update(mode="noise", requested_seconds=100., range_code=hw.NOISE_RANGE, valid_data=False,
                          status_before=hw.decode_status(27), status_after=hw.decode_status(27),
                          status_samples=[{"samples": 4070, "raw_byte": 27}], samples=4070, target_samples=100000,
                          recorded_seconds=4.07, raw_file="noise_raw.npz", rows=[],
                          errors=["No fresh DAQ samples for two seconds",
                                  "scan rate 665.6/s differs from the nominal 1000.0/s by -33.44% (limit 1.0%)",
                                  "Incomplete: 4070 of 100000 scans"])
        self.noise["diagnostics"].update(scans_received=4070, stopped_monotonic=106.1146681)
        self.summary = {"schema": check.SCHEMA, "profile": hw.PROFILE, "application_revision": "2026-11-04.2",
                        "timing_policy": check.TIMING_POLICY, "created_at": "2026-11-04T13:07:31-05:00",
                        "lot": "TEST", "operator": "Test technician", "tray": 1, "simulated": False,
                        "data_complete": False, "restart_required": False, "shutdown_status": hw.decode_status(0),
                        "installed_positions": list(hw.POSITIONS), "channel_map": hw.CHANNEL_MAP,
                        "daq": {"simulated": False, "serial_number": "TEST-SERIAL", "product_id": 0x8145},
                        "records": [self.dc, self.noise], "errors": ["NOISE: " + e for e in self.noise["errors"]]}
        self.metadata = {key: copy.deepcopy(value) for key, value in self.dc.items() if key != "rows"}
        self.metadata.update(schema=check.SCHEMA, profile=hw.PROFILE, application_revision="2026-11-04.2",
                             timing_policy=check.TIMING_POLICY, lot="TEST", operator="Test technician", tray=1,
                             simulated=False, daq_serial="TEST-SERIAL", installed_positions=list(hw.POSITIONS),
                             channel_map=hw.CHANNEL_MAP, map_sha256=hw.MAP_SHA256, physical_channels=list(range(57)),
                             logical_to_physical=list(hw.MAPPING), nominal_rate_hz=1000., oversample_extra=3,
                             drop_first=1, voltage_reference="DAQ_OUTPUT", range_limit_guard_counts=hw.RAIL_GUARD_COUNTS)
        self.save_raw()

    def save_raw(self):
        destination = self.folder / self.dc["raw_file"]
        np.savez_compressed(destination, converter_counts=self.raw,
                            metadata_json=np.array(json.dumps(self.metadata)))
        self.dc["raw_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        self.save_summary()

    def save_summary(self):
        (self.folder / "summary.json").write_text(json.dumps(self.summary), encoding="utf-8")

    def load(self, **kwargs):
        self.save_summary()
        return load_noise_recovery(self.folder, simulate=kwargs.get("simulate", False))

    def test_old_timeout_format_is_eligible_without_modifying_source(self):
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        result = load_noise_recovery(self.folder, simulate=False)
        self.assertEqual(result["raw_path"], (self.folder / "dc_raw.npz").resolve())
        self.assertEqual(result["source"]["daq_serial"], "TEST-SERIAL")
        self.assertEqual(result["source"]["recorded_noise_seconds"], 4.07)
        self.assertEqual(result["source"]["summary_sha256"], hashlib.sha256(before["summary.json"]).hexdigest())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.folder.iterdir()})
        original_mean = self.dc["rows"][0]["mean_v"]
        result["record"]["rows"][0]["mean_v"] = 9
        self.assertEqual(self.dc["rows"][0]["mean_v"], original_mean)

    def test_legacy_meter_revision_with_adaptive_timing_remains_recoverable(self):
        self.summary["application_revision"] = self.metadata["application_revision"] = "2026-11-12.1"
        self.summary["timing_policy"] = self.metadata["timing_policy"] = check.ADAPTIVE_TIMING_POLICY
        self.save_raw()
        self.assertEqual(self.load()["record"]["samples"], 10000)

    def test_bottom_recovery_requires_matching_side_in_raw_evidence(self):
        self.summary["box_side"] = self.metadata["box_side"] = "bottom"
        self.save_raw()
        self.assertEqual(self.load()["source"]["box_side"], "bottom")
        self.summary["box_side"] = "top"
        with self.assertRaisesRegex(ValueError, "detector-box side"):
            self.load()

    def test_previous_revision_without_side_keeps_original_top_labels(self):
        self.summary["application_revision"] = self.metadata["application_revision"] = "2026-11-12.2"
        self.save_raw()
        self.assertEqual(self.load()["source"]["box_side"], "top")

    def test_reconnect_fix_preserves_recovery_of_previous_and_current_revision(self):
        for revision in ("2026-11-12.3", "2026-11-12.4", hw.APP_REVISION):
            with self.subTest(revision=revision):
                self.summary["application_revision"] = self.metadata["application_revision"] = revision
                self.summary["box_side"] = self.metadata["box_side"] = "bottom"
                self.save_raw()
                result = self.load()
                self.assertEqual(result["source"]["box_side"], "bottom")
                self.assertEqual(result["record"]["samples"], 10000)

    def test_pre_legacy_meter_revision_still_recovers_unchanged_dc(self):
        self.summary["application_revision"] = "2026-11-11.1"
        self.metadata["application_revision"] = "2026-11-11.1"
        self.save_raw()
        self.save_summary()
        resumed = self.load()
        self.assertTrue(resumed["record"]["valid_data"])
        self.assertEqual(resumed["record"]["mode"], "dc")

    def test_finite_ordered_summary_statistics_cannot_override_raw_counts(self):
        row = self.dc["rows"][0]
        row.update(min_v=1.25, mean_v=1.30, max_v=1.35)
        with self.assertRaisesRegex(ValueError, "disagree with the raw counts"):
            self.load()

    def test_presence_and_quality_flags_must_match_raw_evidence(self):
        row = self.dc["rows"][0]
        for key, bad in (("presence", "NO_SIGNAL"), ("flags", ["invented flag"]),
                         ("near_range_limit", True), ("presence_evidence", {})):
            with self.subTest(key=key):
                original = copy.deepcopy(row[key])
                row[key] = bad
                with self.assertRaisesRegex(ValueError, "disagree with the raw counts"):
                    self.load()
                row[key] = original

    def test_display_trace_is_rebuilt_from_counts(self):
        expected = copy.deepcopy(self.dc["rows"][0]["dc_detail"])
        self.dc["rows"][0]["dc_detail"] = {"volts": [99], "time_s": [0]}
        result = self.load()
        self.assertEqual(result["record"]["rows"][0]["dc_detail"], expected)

    def test_current_retry_schema_uses_last_dc_and_retains_history(self):
        rejected_dc = copy.deepcopy(self.noise)
        rejected_dc.update(mode="dc", raw_file="dc_raw.npz", attempt=1,
                           failure_kind="delivery_timeout", retry_eligible=True)
        self.dc.update(raw_file="dc_attempt02_raw.npz", attempt=2)
        self.metadata.update(raw_file="dc_attempt02_raw.npz", attempt=2)
        self.noise.update(attempt=3, failure_kind="delivery_timeout", retry_eligible=True)
        self.summary["records"] = [rejected_dc, self.dc, self.noise]
        self.summary["application_revision"] = hw.APP_REVISION
        self.save_raw()
        result = self.load()
        self.assertEqual(result["record"]["attempt"], 2)
        self.assertEqual(result["raw_path"].name, "dc_attempt02_raw.npz")

    def test_source_mismatch_and_unsupported_provenance_are_rejected(self):
        mutations = [("simulated", True), ("schema", "OLD"), ("profile", "OTHER"),
                     ("timing_policy", "short-waits"), ("application_revision", "unknown"),
                     ("data_complete", True), ("restart_required", True),
                     ("shutdown_status", hw.decode_status(27)), ("channel_map", []),
                     ("installed_positions", ["1-1", "1-1"])]
        for key, value in mutations:
            with self.subTest(key=key):
                prior = self.summary[key]
                self.summary[key] = value
                with self.assertRaises(ValueError): self.load()
                self.summary[key] = prior

    def test_actual_and_demo_require_consistent_metadata(self):
        with self.assertRaises(ValueError): self.load(simulate=True)
        self.summary["simulated"] = self.summary["daq"]["simulated"] = self.metadata["simulated"] = True
        for record in (self.dc, self.noise):
            for field in ("clock_stop_status", "bulk_end_status", "bulk_worker_status"):
                record["diagnostics"][field] = None
        self.dc["diagnostics"]["stopped_monotonic"] = 100.5
        self.metadata["diagnostics"] = copy.deepcopy(self.dc["diagnostics"])
        self.save_raw()
        self.assertTrue(self.load(simulate=True)["source"]["simulated"])
        with self.assertRaises(ValueError): self.load()

    def test_stop_power_save_and_shutdown_faults_are_never_resumable(self):
        for fault in ("Stopped by operator", "DAQ shutdown: failed", "Saving report: disk full",
                      "UPPER not ready/present", "pacing clock granted 900 Hz for 1000 Hz"):
            with self.subTest(fault=fault):
                original = list(self.summary["errors"])
                self.summary["errors"].append(fault)
                with self.assertRaises(ValueError): self.load()
                self.summary["errors"] = original
        self.noise["errors"] = ["Stopped by operator; the check is incomplete.", "Incomplete: 4070 of 100000 scans"]
        self.summary["errors"] = ["NOISE: " + e for e in self.noise["errors"]]
        with self.assertRaises(ValueError): self.load()

    def test_rate_warning_alone_or_unverified_cleanup_is_not_a_retry_reason(self):
        primary = self.noise["errors"].pop(0)
        self.summary["errors"] = ["NOISE: " + e for e in self.noise["errors"]]
        with self.assertRaises(ValueError): self.load()
        self.noise["errors"].insert(0, primary)
        self.summary["errors"] = ["NOISE: " + e for e in self.noise["errors"]]
        for field in ("clock_stop_status", "bulk_end_status", "bulk_worker_status"):
            self.noise["diagnostics"][field] = 1
            with self.assertRaises(ValueError): self.load()
            self.noise["diagnostics"][field] = 0
        self.noise["status_samples"][0]["raw_byte"] = 31
        with self.assertRaises(ValueError): self.load()

    def test_bad_dc_clock_count_settling_or_delivery_prevents_reuse(self):
        for key, bad in (("actual_rate_hz", 900.), ("samples", 9999), ("target_samples", 1),
                         ("valid_data", False), ("recorded_seconds", 9.9), ("range_code", 7),
                         ("settling", {"complete": False})):
            with self.subTest(key=key):
                before = self.dc[key]
                self.dc[key] = bad
                with self.assertRaises(ValueError): self.load()
                self.dc[key] = before
        self.dc["diagnostics"]["pool_too_small_events"] = 1
        with self.assertRaises(ValueError): self.load()

    def test_raw_hash_shape_dtype_and_pickle_are_checked(self):
        path = self.folder / self.dc["raw_file"]
        with path.open("ab") as stream: stream.write(b"changed")
        with self.assertRaises(ValueError): self.load()
        for raw in (np.zeros((9999, 57, 4), np.uint16), np.zeros((10000, 56, 4), np.uint16),
                    np.zeros((10000, 57, 4), np.float32), np.array([object()], dtype=object)):
            with self.subTest(shape=raw.shape, dtype=str(raw.dtype)):
                self.raw = raw
                self.save_raw()
                with self.assertRaises(ValueError): self.load()

    def test_metadata_identity_mapping_revision_and_record_mismatches_are_rejected(self):
        for key, bad in (("lot", "OTHER"), ("operator", "OTHER"), ("tray", 2), ("daq_serial", "OTHER"),
                         ("map_sha256", "wrong"), ("logical_to_physical", []), ("physical_channels", []),
                         ("application_revision", "unknown"), ("actual_rate_hz", 999.), ("drop_first", 0)):
            with self.subTest(key=key):
                before = self.metadata[key]
                self.metadata[key] = bad
                self.save_raw()
                with self.assertRaises(ValueError): self.load()
                self.metadata[key] = before

    def test_raw_filename_cannot_escape_folder(self):
        for name in ("../dc_raw.npz", "sub/dc_raw.npz", "C:\\dc_raw.npz", "noise_raw.npz", "dc_attemptX_raw.npz"):
            with self.subTest(name=name):
                self.dc["raw_file"] = name
                with self.assertRaises(ValueError): self.load()

    def test_corrupt_archive_with_matching_hash_still_raises_value_error(self):
        path = self.folder / self.dc["raw_file"]
        path.write_bytes(b"not an npz file")
        self.dc["raw_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError): self.load()

    def test_earlier_power_fault_cannot_be_hidden_by_later_valid_dc(self):
        rejected = copy.deepcopy(self.dc)
        rejected.update(valid_data=False, errors=["UPPER not ready/present"])
        self.summary["records"].insert(0, rejected)
        with self.assertRaises(ValueError): self.load()

    def test_missing_and_malformed_evidence_raise_value_error(self):
        for summary in ({}, {"records": []}, {**self.summary, "created_at": None},
                        {**self.summary, "application_revision": []}):
            (self.folder / "summary.json").write_text(json.dumps(summary))
            with self.assertRaises(ValueError): load_noise_recovery(self.folder, simulate=False)
        (self.folder / "summary.json").write_text("{")
        with self.assertRaises(ValueError): load_noise_recovery(self.folder, simulate=False)
        with self.assertRaises(ValueError): load_noise_recovery(self.folder / "missing", simulate=False)


if __name__ == "__main__":
    unittest.main()
