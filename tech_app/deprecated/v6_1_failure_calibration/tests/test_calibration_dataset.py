from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tech_app.v6_1_failure_calibration import calibration_dataset as dataset


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    run_dir = root / "evidence" / "lot_LOT-1" / "S01" / "RUN-1"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "dut_samples.csv"
    artifact.write_bytes(b"sample,voltage\n1,0.700\n")

    manifest = {
        "calibration_schema_version": 1,
        "calibration_app_version": "cal-app-1",
        "ruleset_id": "rules-1",
        "session_id": "SESSION-1",
        "run_id": "RUN-1",
        "measurement_run_number": 1,
        "batch_number": "LOT-1",
        "sensor_id": "LOT-1-1",
        "specimen_id": "S01",
        "is_synthetic": False,
        "filter_setup": "FILTER-A",
        "app_prediction": {
            "verdict": "FAIL",
            "offset_v": 0.7,
            "sensitivity_mv": 1.25,
            "polarity": "POSITIVE",
            "suggested_failure_mode": "LS - Low sensitivity",
            "fail_reasons": ["Sensitivity too low."],
        },
        "battery_v": 6.2,
        "offset_plausibility_override": False,
        "metrics": {
            "noise_rms_mv": 0.012345,
            "signal_to_noise_db": 18.234,
        },
        "reference": {
            "calibrated_at": "2026-07-16T10:00:00+00:00",
            "baseline_mv": 100.123456,
            "lower_mv": 90.11111,
            "upper_mv": 110.22222,
            "check_mv": 101.98765,
            "drift_percent": 1.862345,
        },
        "stability_report": {
            "threshold_mv": 0.35,
            "required_deltas": 10,
            "stabilized": True,
            "timed_out": False,
            "phase": "measured",
            "measurement_attempt": 2,
            "measurement_failures": 1,
            "active_required_deltas": 10,
            "measurement_cycles_required": 20,
            "stabilization_cycle": 14,
            "stabilization_seconds": 12.3456,
            "confirming_max_delta_mv": 0.1234567,
            "last_peak_delta_mv": 0.1111114,
            "capture_cycles": 44,
            "measurement_cycles": 20,
            "pwm_on_seconds": 31.2345,
            "data_source": "esp32_v6_1_failure_calibration",
        },
        "policy": {
            "offset_min_v": 0.5,
            "offset_max_v": 2.5,
            "sensitivity_min_mv": 3.5,
            "min_signal_to_noise_ratio": 2.0,
            "stability_timeout_s": 45.0,
            "max_measurement_attempts": 3,
        },
        "artifacts": [
            {
                "path": "/old/home/v6_1_failure_calibration/dut_samples.csv",
                "relative_path": "dut_samples.csv",
                "bytes": artifact.stat().st_size,
                "sha256": sha256(artifact),
            }
        ],
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    review = {
        "calibration_schema_version": 1,
        "calibration_app_version": "cal-app-1",
        "ruleset_id": "rules-1",
        "batch_number": "LOT-1",
        "sensor_id": "LOT-1-1",
        "selected_run_id": "RUN-1",
        "related_run_ids": ["RUN-1"],
        "review": {
            "specimen_id": "S01",
            "app_verdict": "FAIL",
            "app_suggested_failure_mode": "LS - Low sensitivity",
            "app_suggested_failure_mode_tag": "LS",
            "app_suggested_failure_mode_reason": "Low sensitivity",
            "ground_truth_verdict": "FAIL",
            "ground_truth_failure_mode": "O - No sensitivity",
            "ground_truth_failure_mode_tag": "O",
            "ground_truth_failure_mode_reason": "No sensitivity",
            "verdict_assessment": "CORRECT",
            "failure_reason_assessment": "INCORRECT",
            "review_classification": "FAILURE_REASON_MISMATCH",
            "ground_truth_basis": "Independent bench / scope test",
            "review_confidence": "HIGH",
            "known_physical_root_cause": "open detector",
            "review_notes": "Scope confirms no response.",
            "reviewed_at_utc": "2026-07-16T12:00:00+00:00",
            "reviewer": "Technician A",
        },
        "production_failure_mode": "O - No sensitivity",
        "operator_comment": "independent scope check",
        "evidence_paths": ["/old/path/dut_samples.csv"],
    }
    review_path = run_dir / "review.json"
    review_path.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")

    fields = sorted(set(dataset.VERIFY_BATCH_FIELDS) | {"data_source"})
    row = {field: "" for field in fields}
    row.update(
        {
            "calibration_schema_version": "1",
            "calibration_app_version": "cal-app-1",
            "calibration_ruleset_id": "rules-1",
            "session_id": "SESSION-1",
            "run_id": "RUN-1",
            "measurement_run_number": "1",
            "batch_number": "LOT-1",
            "sensor_id": "LOT-1-1",
            "specimen_id": "S01",
            "filter_setup": "FILTER-A",
            "is_synthetic": "NO",
            "data_source": "esp32_v6_1_failure_calibration",
            "offset_v": "0.700000",
            "sensitivity_mv": "1.250000",
            "polarity": "POSITIVE",
            "noise_rms_mv": "0.0123",
            "snr_db": "18.23",
            "battery_v": "6.200",
            "reference_calibrated_at": "2026-07-16T10:00:00+00:00",
            "reference_calibration_mv": "100.1235",
            "reference_lower_mv": "90.1111",
            "reference_upper_mv": "110.2222",
            "reference_check_mv": "101.9877",
            "reference_drift_pct": "1.862",
            "stabilized": "YES",
            "stability_timeout": "NO",
            "stability_threshold_mv": "0.350000",
            "stability_required_deltas": "10",
            "stabilization_cycle": "14",
            "stabilization_seconds": "12.346",
            "stability_window_max_delta_mv": "0.123457",
            "last_peak_delta_mv": "0.111111",
            "capture_cycles": "44",
            "measurement_cycles": "20",
            "stability_phase": "measured",
            "measurement_attempt": "2",
            "measurement_failures": "1",
            "active_stability_required_deltas": "10",
            "active_measurement_cycles_required": "20",
            "pwm_on_seconds": "31.235",
            "policy_offset_min_v": "0.500000",
            "policy_offset_max_v": "2.500000",
            "policy_sensitivity_min_mv": "3.500000",
            "policy_min_snr_ratio": "2.000000",
            "policy_stability_timeout_s": "45.000",
            "policy_max_measurement_attempts": "3",
            "offset_plausibility_override": "NO",
            "app_verdict": "FAIL",
            "app_suggested_failure_mode_tag": "LS",
            "app_suggested_failure_mode_reason": "Low sensitivity",
            "fail_reasons": "Sensitivity too low.",
            "ground_truth_verdict": "FAIL",
            "ground_truth_failure_mode_tag": "O",
            "ground_truth_failure_mode_reason": "No sensitivity",
            "verdict_assessment": "CORRECT",
            "failure_reason_assessment": "INCORRECT",
            "review_classification": "FAILURE_REASON_MISMATCH",
            "ground_truth_basis": "Independent bench / scope test",
            "review_confidence": "HIGH",
            "known_physical_root_cause": "open detector",
            "review_notes": "Scope confirms no response.",
            "reviewed_at_utc": "2026-07-16T12:00:00+00:00",
            "reviewer": "Technician A",
            "failure_mode_tag": "O",
            "failure_mode_reason": "No sensitivity",
            "operator_comments": "independent scope check",
            "run_manifest_path": (
                "evidence/lot_LOT-1/S01/RUN-1/run_manifest.json"
            ),
            "run_manifest_sha256": sha256(manifest_path),
            "review_record_path": "evidence/lot_LOT-1/S01/RUN-1/review.json",
            "review_record_sha256": sha256(review_path),
            "related_run_ids": "RUN-1",
        }
    )
    batch_path = root / "406mca_failure_calibration_lot_LOT-1.csv"
    with batch_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)
    return batch_path, manifest_path, review_path, artifact


class CalibrationDatasetVerifyTests(unittest.TestCase):
    def test_verify_checks_finalized_chain_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "copied_study"
            write_fixture(root)
            simulator_run = root / "simulator" / "evidence" / "SIM"
            simulator_run.mkdir(parents=True)
            (simulator_run / "run_manifest.json").write_text(
                "not JSON", encoding="utf-8"
            )
            simulator_csv = (
                root / "simulator" / "406mca_failure_calibration_lot_SIM.csv"
            )
            simulator_csv.write_text("not,a,study\n", encoding="utf-8")
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            report = dataset.verify(root)

            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertTrue(report["valid"], report)
            self.assertEqual(report["linked_records_ok"], 1)
            self.assertEqual(report["reviews_read"], 1)
            self.assertEqual(report["artifacts_ok"], 1)
            self.assertEqual(report["excluded_simulator_manifests"], 1)
            self.assertEqual(report["excluded_simulator_csv_files"], 1)
            self.assertEqual(before, after)

    def test_verify_detects_review_hash_and_field_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "study"
            _batch, _manifest, review_path, _artifact = write_fixture(root)
            review = json.loads(review_path.read_text(encoding="utf-8"))
            review["review"]["reviewer"] = "Technician B"
            review_path.write_text(
                json.dumps(review, indent=2) + "\n", encoding="utf-8"
            )

            report = dataset.verify(root)

            self.assertFalse(report["valid"])
            self.assertTrue(
                any(
                    item.get("kind") == "review_record_hash"
                    for item in report["mismatch"]
                )
            )
            self.assertTrue(
                any(
                    item.get("field") == "reviewer"
                    for item in report["mismatch"]
                )
            )

    def test_verify_detects_csv_numeric_audit_field_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "study"
            batch_path, _manifest, _review, _artifact = write_fixture(root)
            with batch_path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fields = list(reader.fieldnames or ())
                row = next(reader)
            row["policy_sensitivity_min_mv"] = "9.999999"
            with batch_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)

            report = dataset.verify(root)

            self.assertFalse(report["valid"])
            self.assertTrue(
                any(
                    item.get("kind") == "field_mismatch"
                    and item.get("field") == "policy_sensitivity_min_mv"
                    for item in report["mismatch"]
                ),
                report,
            )

    def test_verify_requires_one_row_and_sibling_review_per_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "study"
            batch_path, _manifest, review_path, _artifact = write_fixture(root)
            with batch_path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fields = list(reader.fieldnames or ())
                row = next(reader)
            with batch_path.open("a", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=fields).writerow(row)
            review_path.unlink()

            report = dataset.verify(root)

            self.assertFalse(report["valid"])
            self.assertTrue(
                any(
                    item.get("kind") == "duplicate_batch_run_id"
                    for item in report["mismatch"]
                )
            )
            self.assertTrue(
                any(
                    item.get("kind") == "review_json"
                    for item in report["missing"]
                )
            )


if __name__ == "__main__":
    unittest.main()
