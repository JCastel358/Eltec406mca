from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np


CALIBRATION_DIR = Path(__file__).resolve().parents[1]
if str(CALIBRATION_DIR) not in sys.path:
    sys.path.insert(0, str(CALIBRATION_DIR))

import eltec_406mca_esp32_tester as app  # noqa: E402
import esp32_backend as backend  # noqa: E402
from stability_analysis import analyze_stability, load_stability_settings  # noqa: E402


SETTINGS = load_stability_settings()


def final_result(*, passed: bool, reasons: list[str] | None = None) -> app.FinalResult:
    return app.FinalResult(
        passed=passed,
        offset_v=0.7,
        sensitivity_mv=8.0 if passed else 1.0,
        polarity=app.POSITIVE_POLARITY,
        fail_reasons=list(reasons or []),
        warnings=[],
        waveform_metrics=None,
    )


def review_kwargs(final: app.FinalResult) -> dict:
    return {
        "final_result": final,
        "app_suggested_failure_mode": "" if final.passed else "LS - Low sensitivity",
        "specimen_id": "S-01",
        "ground_truth_verdict": "FAIL",
        "ground_truth_failure_mode": "LS - Low sensitivity",
        "failure_reason_assessment": "CORRECT",
        "ground_truth_basis": "Independent bench / scope test",
        "confidence": "HIGH",
        "known_physical_root_cause": "Documented detector sensitivity loss",
        "notes": "Independent scope test confirmed the stated condition.",
        "reviewer": "Technician A",
        "reviewed_at_utc": "2026-07-16T15:30:00+00:00",
    }


def prepared_capture() -> tuple[
    np.ndarray,
    np.ndarray,
    float,
    float,
    app.StabilityAnalysis,
    app.WaveformMetrics,
    app.FinalResult,
    app.StabilityCaptureReport,
]:
    """Create the same deterministic synthetic capture used by the GUI tests."""
    waveform, sync, rate, offset = app.simulate_v6_startup_capture(
        app.DEFAULT_FILTER_SETUP,
        "Known good",
    )
    settings = app.dut_stability_settings(SETTINGS)
    full_analysis = analyze_stability(
        waveform,
        sync,
        rate,
        settings,
        stability_deadline_s=app.STABILITY_TIMEOUT_S,
        measurement_cycles_required=app.SENSITIVITY_MEASUREMENT_CYCLES,
        enforce_measurement_stability=True,
        max_measurement_attempts=app.MAX_MEASUREMENT_ATTEMPTS,
        data_source="simulator",
    )
    cut_sample = full_analysis.measurement_cycles[-1].end_index + 1
    waveform = waveform[:cut_sample]
    sync = sync[:cut_sample]
    analysis = analyze_stability(
        waveform,
        sync,
        rate,
        settings,
        stability_deadline_s=app.STABILITY_TIMEOUT_S,
        measurement_cycles_required=app.SENSITIVITY_MEASUREMENT_CYCLES,
        enforce_measurement_stability=True,
        max_measurement_attempts=app.MAX_MEASUREMENT_ATTEMPTS,
        data_source="simulator",
    )
    metrics = app.analyze_v6_stable_measurement(
        waveform,
        sync,
        rate,
        analysis,
        offset_v=offset,
        input_range_v=app.WAVEFORM_INPUT_RANGE_V,
    )
    final = app.evaluate_result(offset, metrics, app.DEFAULT_FILTER_SETUP)
    report = app.StabilityCaptureReport.from_analysis(
        analysis,
        data_source="simulator",
    )
    return waveform, sync, rate, offset, analysis, metrics, final, report


class CalibrationReviewTests(unittest.TestCase):
    def test_build_calibration_review_classifies_supported_combinations(self):
        passing = final_result(passed=True)
        failing = final_result(
            passed=False,
            reasons=["Sensitivity too low: 1.00 mV, minimum is 4.00 mV."],
        )
        cases = (
            (
                "pass_exact",
                passing,
                "",
                "PASS",
                app.GOOD_GROUND_TRUTH_MODE,
                "NOT APPLICABLE",
                "CORRECT",
                "EXACT_MATCH",
            ),
            (
                "fail_exact",
                failing,
                "LS - Low sensitivity",
                "FAIL",
                "LS - Low sensitivity",
                "CORRECT",
                "CORRECT",
                "EXACT_MATCH",
            ),
            (
                "partial_reason",
                failing,
                "LS - Low sensitivity",
                "FAIL",
                "O - No sensitivity",
                "PARTLY CORRECT",
                "CORRECT",
                "PARTIAL_REASON_MATCH",
            ),
            (
                "wrong_reason",
                failing,
                "LS - Low sensitivity",
                "FAIL",
                "O - No sensitivity",
                "INCORRECT",
                "CORRECT",
                "FAILURE_REASON_MISMATCH",
            ),
            (
                "false_pass",
                passing,
                "",
                "FAIL",
                "O - No sensitivity",
                "INCORRECT",
                "INCORRECT",
                "VERDICT_MISMATCH",
            ),
            (
                "false_fail",
                failing,
                "LS - Low sensitivity",
                "PASS",
                app.GOOD_GROUND_TRUTH_MODE,
                "INCORRECT",
                "INCORRECT",
                "VERDICT_MISMATCH",
            ),
            (
                "inconclusive",
                failing,
                "LS - Low sensitivity",
                "UNSURE",
                app.UNKNOWN_GROUND_TRUTH_MODE,
                "UNSURE",
                "UNSURE",
                "INCONCLUSIVE",
            ),
        )

        for (
            name,
            final,
            suggestion,
            truth_verdict,
            truth_mode,
            reason_assessment,
            verdict_assessment,
            classification,
        ) in cases:
            with self.subTest(name=name):
                values = review_kwargs(final)
                values.update(
                    app_suggested_failure_mode=suggestion,
                    ground_truth_verdict=truth_verdict,
                    ground_truth_failure_mode=truth_mode,
                    failure_reason_assessment=reason_assessment,
                )
                if truth_verdict == "UNSURE":
                    values["confidence"] = "LOW"
                review = app.build_calibration_review(**values)

                self.assertEqual(review.verdict_assessment, verdict_assessment)
                self.assertEqual(review.review_classification, classification)
                self.assertEqual(review.to_dict()["specimen_id"], "S-01")

    def test_build_calibration_review_rejects_inconsistent_or_incomplete_input(self):
        passing = final_result(passed=True)
        failing = final_result(passed=False, reasons=["Sensitivity too low."])
        cases = (
            (
                "missing_specimen",
                failing,
                {"specimen_id": ""},
                "physical specimen ID",
            ),
            (
                "missing_notes",
                failing,
                {"notes": ""},
                "brief explanation",
            ),
            (
                "pass_with_failure_mode",
                passing,
                {
                    "app_suggested_failure_mode": "",
                    "ground_truth_verdict": "PASS",
                    "ground_truth_failure_mode": "LS - Low sensitivity",
                    "failure_reason_assessment": "NOT APPLICABLE",
                },
                "ground-truth PASS",
            ),
            (
                "false_pass_reason_not_incorrect",
                passing,
                {
                    "app_suggested_failure_mode": "",
                    "ground_truth_verdict": "FAIL",
                    "ground_truth_failure_mode": "O - No sensitivity",
                    "failure_reason_assessment": "CORRECT",
                },
                "false PASS",
            ),
            (
                "correct_but_different_modes",
                failing,
                {
                    "ground_truth_failure_mode": "O - No sensitivity",
                    "failure_reason_assessment": "CORRECT",
                },
                "differs from the app suggestion",
            ),
            (
                "incorrect_but_identical_modes",
                failing,
                {
                    "ground_truth_failure_mode": "LS - Low sensitivity",
                    "failure_reason_assessment": "INCORRECT",
                },
                "matches the app suggestion",
            ),
            (
                "unsure_with_known_mode",
                failing,
                {
                    "ground_truth_verdict": "UNSURE",
                    "ground_truth_failure_mode": "LS - Low sensitivity",
                    "failure_reason_assessment": "UNSURE",
                },
                "UNSURE verdict",
            ),
            (
                "unsure_with_high_confidence",
                failing,
                {
                    "ground_truth_verdict": "UNSURE",
                    "ground_truth_failure_mode": app.UNKNOWN_GROUND_TRUTH_MODE,
                    "failure_reason_assessment": "UNSURE",
                    "confidence": "HIGH",
                },
                "cannot be recorded with HIGH confidence",
            ),
            (
                "fail_without_suggestion",
                failing,
                {
                    "app_suggested_failure_mode": "",
                    "failure_reason_assessment": "INCORRECT",
                },
                "Choose a failure mode",
            ),
            (
                "pass_with_suggestion",
                passing,
                {
                    "app_suggested_failure_mode": "LS - Low sensitivity",
                    "ground_truth_verdict": "PASS",
                    "ground_truth_failure_mode": app.GOOD_GROUND_TRUTH_MODE,
                    "failure_reason_assessment": "NOT APPLICABLE",
                },
                "PASS must not carry",
            ),
        )

        for name, final, changes, message in cases:
            with self.subTest(name=name):
                values = review_kwargs(final)
                values.update(changes)
                with self.assertRaisesRegex(ValueError, message):
                    app.build_calibration_review(**values)

    def test_review_json_round_trip_preserves_annotation_and_links(self):
        final = final_result(passed=True)
        values = review_kwargs(final)
        values.update(
            app_suggested_failure_mode="",
            ground_truth_verdict="FAIL",
            ground_truth_failure_mode="O - No sensitivity",
            failure_reason_assessment="INCORRECT",
        )
        review = app.build_calibration_review(**values)

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            evidence = [run_dir / "dut.png", run_dir / "dut_samples.csv"]
            with mock.patch.object(app, "calibration_run_dir", return_value=run_dir):
                review_path = app.save_calibration_review_record(
                    batch_number="LOT-1",
                    sensor_id="LOT-1-1",
                    run_id="RUN-001",
                    review=review,
                    is_synthetic=True,
                    production_failure_mode="",
                    operator_comment="retested independently",
                    related_run_ids=["RUN-000", "RUN-001"],
                    evidence_paths=evidence,
                )
            payload = json.loads(review_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["review"], review.to_dict())
        self.assertEqual(payload["selected_run_id"], "RUN-001")
        self.assertEqual(payload["related_run_ids"], ["RUN-000", "RUN-001"])
        self.assertEqual(payload["evidence_paths"], [str(path) for path in evidence])
        self.assertEqual(
            payload["calibration_schema_version"],
            app.CALIBRATION_SCHEMA_VERSION,
        )


class CalibrationCsvTests(unittest.TestCase):
    @staticmethod
    def append_common(csv_path: Path, final: app.FinalResult, **kwargs) -> None:
        app.append_result_csv(
            csv_path,
            batch_number="LOT-1",
            sensor_number=1,
            sensor_id="LOT-1-1",
            tester_name="Technician A",
            filter_setup=app.DEFAULT_FILTER_SETUP,
            pwm_channel=app.EMITTER_PWM_CHANNEL,
            pwm_hz=app.EMITTER_PWM_FREQUENCY_HZ,
            pwm_duty=app.EMITTER_PWM_DUTY_CYCLE,
            final_result=final,
            comment="",
            snapshot_paths=[],
            **kwargs,
        )

    def test_csv_freezes_app_suggestion_separately_from_human_choices(self):
        final = final_result(passed=False, reasons=["Sensitivity too low."])
        values = review_kwargs(final)
        values.update(
            ground_truth_failure_mode="O - No sensitivity",
            failure_reason_assessment="INCORRECT",
        )
        review = app.build_calibration_review(**values)

        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "study.csv"
            self.append_common(
                csv_path,
                final,
                failure_mode="O - No sensitivity",
                calibration_review=review,
                app_suggested_failure_mode="LS - Low sensitivity",
                session_id="SESSION-1",
                run_id="RUN-1",
                measurement_run_number=2,
                specimen_repeat_number=3,
                is_synthetic=False,
            )
            with csv_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["failure_mode_tag"], "O")
        self.assertEqual(row["app_suggested_failure_mode_tag"], "LS")
        self.assertEqual(row["app_suggested_failure_mode_reason"], "Low sensitivity")
        self.assertEqual(row["ground_truth_failure_mode_tag"], "O")
        self.assertEqual(row["failure_reason_assessment"], "INCORRECT")
        self.assertEqual(row["review_classification"], "FAILURE_REASON_MISMATCH")
        self.assertEqual(row["app_verdict"], "FAIL")
        self.assertEqual(row["run_id"], "RUN-1")
        self.assertEqual(row["specimen_repeat_number"], "3")

    def test_csv_rejects_a_stale_or_mixed_schema_without_appending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "old.csv"
            original = "timestamp,pass_fail\nold,PASS\n"
            csv_path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "schema/version mismatch"):
                self.append_common(csv_path, final_result(passed=True))

            self.assertEqual(csv_path.read_text(encoding="utf-8"), original)

    def test_csv_preflight_requires_the_exact_current_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "study.csv"

            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(app.CSV_FIELDS)
            app.validate_calibration_csv_schema(csv_path)

            invalid_headers = (
                app.CSV_FIELDS[:-1],
                [app.CSV_FIELDS[1], app.CSV_FIELDS[0], *app.CSV_FIELDS[2:]],
                [*app.CSV_FIELDS, "unexpected_future_field"],
            )
            for header in invalid_headers:
                with self.subTest(header_length=len(header)):
                    with csv_path.open("w", newline="", encoding="utf-8") as handle:
                        csv.writer(handle).writerow(header)
                    with self.assertRaisesRegex(ValueError, "older or different CSV schema"):
                        app.validate_calibration_csv_schema(csv_path)

            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=app.CSV_FIELDS)
                writer.writeheader()
                row = {field: "" for field in app.CSV_FIELDS}
                row.update(
                    batch_number="LOT A",
                    calibration_app_version=app.CALIBRATION_APP_VERSION,
                    calibration_ruleset_id=app.CALIBRATION_RULESET_ID,
                )
                writer.writerow(row)
            app.validate_calibration_csv_schema(
                csv_path,
                expected_batch_number="LOT A",
            )
            with self.assertRaisesRegex(ValueError, "different raw batch number"):
                app.validate_calibration_csv_schema(
                    csv_path,
                    expected_batch_number="LOT/A",
                )

    def test_repeat_number_counts_physical_specimen_case_insensitively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "study.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["specimen_id"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"specimen_id": "S-01"},
                        {"specimen_id": " s-01 "},
                        {"specimen_id": "S-02"},
                        {"specimen_id": ""},
                    ]
                )

            self.assertEqual(app.next_specimen_repeat_number(csv_path, "S-01"), 3)
            self.assertEqual(app.next_specimen_repeat_number(csv_path, "s-02"), 2)
            self.assertEqual(app.next_specimen_repeat_number(csv_path, "S-03"), 1)
            self.assertEqual(
                app.next_specimen_repeat_number(Path(temp_dir) / "missing.csv", "S-01"),
                1,
            )


class CalibrationNamespaceTests(unittest.TestCase):
    def test_simulator_artifacts_are_isolated_from_hardware_study_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "v6_1_failure_calibration"
            with mock.patch.object(app, "results_root_dir", return_value=root):
                hardware_csv = app.batch_results_path("LOT / 1")
                simulator_csv = app.batch_results_path("LOT / 1", is_synthetic=True)
                simulator_autosave = app.batch_autosave_path(
                    "LOT / 1",
                    is_synthetic=True,
                )
                simulator_run = app.calibration_run_dir(
                    "LOT / 1",
                    "S / 01",
                    "RUN / 01",
                    is_synthetic=True,
                )

        self.assertEqual(hardware_csv.parent, root)
        self.assertEqual(simulator_csv.parent, root / "simulator")
        self.assertEqual(simulator_autosave.parents[1], root / "simulator")
        self.assertEqual(simulator_run.parents[3], root / "simulator")
        self.assertNotIn("simulator", hardware_csv.parts)


class CalibrationEvidenceTests(unittest.TestCase):
    def test_evidence_bundle_preserves_raw_samples_cycles_and_manifest_hashes(self):
        (
            waveform,
            sync,
            rate,
            offset,
            analysis,
            metrics,
            final,
            report,
        ) = prepared_capture()
        raw_samples = tuple(
            backend.StreamSample(
                timestamp_us=500_000 + index * 1_000,
                raw=int(round(float(voltage) * 1_000_000)),
                volts=float(voltage),
                sync=int(sync[index]),
            )
            for index, voltage in enumerate(waveform)
        )
        diagnostics = backend.StreamDiagnostics(
            expected_rate_hz=rate,
            channel="SENSOR",
            started_monotonic=10.0,
            stopped_monotonic=22.0,
            received_samples=len(raw_samples),
            first_timestamp_us=raw_samples[0].timestamp_us,
            last_timestamp_us=raw_samples[-1].timestamp_us,
            firmware_samples_sent=len(raw_samples),
            firmware_adc_overruns=0,
            stop_marker_seen=True,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "evidence" / "RUN-RAW-1"
            with mock.patch.object(app, "calibration_run_dir", return_value=run_dir):
                paths, manifest_path = app.save_calibration_run_evidence(
                    batch_number="LOT-RAW",
                    sensor_id="LOT-RAW-1",
                    specimen_id="S-RAW-1",
                    tester_name="Technician A",
                    filter_setup=app.DEFAULT_FILTER_SETUP,
                    session_id="SESSION-RAW",
                    run_id="RUN-RAW-1",
                    measurement_run_number=1,
                    is_synthetic=True,
                    metrics=metrics,
                    final_result=final,
                    report=report,
                    battery_v=6.2,
                    reference_calibration=None,
                    reference_check_mv=5.0,
                    dut_raw_samples=raw_samples,
                    dut_stream_diagnostics=diagnostics,
                )

            png_path = next(path for path in paths if path.suffix == ".png")
            samples_path = next(
                path for path in paths if path.name.endswith("_samples.csv")
            )
            cycles_path = next(
                path for path in paths if path.name.endswith("_cycles.csv")
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            with samples_path.open(newline="", encoding="utf-8") as handle:
                sample_reader = csv.DictReader(handle)
                self.assertEqual(sample_reader.fieldnames, list(app.STABILITY_SAMPLE_DIAGNOSTIC_FIELDS))
                sample_rows = list(sample_reader)
            with cycles_path.open(newline="", encoding="utf-8") as handle:
                cycle_reader = csv.DictReader(handle)
                self.assertEqual(cycle_reader.fieldnames, list(app.STABILITY_CYCLE_DIAGNOSTIC_FIELDS))
                cycle_rows = list(cycle_reader)

            artifact_by_path = {
                Path(item["path"]): item for item in manifest["artifacts"]
            }
            for artifact_path in (png_path, samples_path, cycles_path):
                with self.subTest(artifact=artifact_path.name):
                    record = artifact_by_path[artifact_path]
                    self.assertEqual(record["bytes"], artifact_path.stat().st_size)
                    self.assertEqual(record["sha256"], app.sha256_file(artifact_path))
                    self.assertEqual(len(record["sha256"]), 64)

            manifest_hash = app.sha256_file(manifest_path)
            png_header = png_path.read_bytes()[:8]

        self.assertEqual(png_header, b"\x89PNG\r\n\x1a\n")
        self.assertEqual(len(paths), 4)
        self.assertEqual(paths[-1], manifest_path)
        self.assertEqual(len(manifest_hash), 64)
        self.assertEqual(len(sample_rows), len(waveform))
        self.assertEqual(sample_rows[0]["device_timestamp_us"], "500000")
        self.assertEqual(sample_rows[0]["device_elapsed_s"], "0.000000000")
        self.assertEqual(sample_rows[17]["device_elapsed_s"], "0.017000000")
        self.assertEqual(
            sample_rows[17]["raw_count"],
            str(raw_samples[17].raw),
        )
        self.assertEqual(sample_rows[17]["voltage_v"], f"{float(waveform[17]):.12g}")
        self.assertEqual(sample_rows[17]["sync"], str(int(sync[17])))
        self.assertEqual(sample_rows[17]["channel"], "AIN0")
        self.assertEqual(sample_rows[17]["specimen_id"], "S-RAW-1")
        self.assertEqual(sample_rows[17]["run_id"], "RUN-RAW-1")
        self.assertEqual(len(cycle_rows), len(analysis.cycles))
        self.assertEqual(cycle_rows[0]["channel"], "AIN0")
        self.assertEqual(cycle_rows[-1]["run_id"], "RUN-RAW-1")
        self.assertEqual(manifest["run_id"], "RUN-RAW-1")
        self.assertTrue(manifest["is_synthetic"])
        self.assertEqual(manifest["app_prediction"]["verdict"], "PASS")
        self.assertEqual(manifest["stability_report"]["data_source"], "simulator")
        self.assertEqual(
            manifest["dut_stream_diagnostics"]["received_samples"],
            len(raw_samples),
        )
        self.assertAlmostEqual(manifest["metrics"]["offset_v"], offset)

    def test_manifest_validation_recovers_result_and_rejects_tampering(self):
        (
            waveform,
            sync,
            _rate,
            _offset,
            _analysis,
            metrics,
            final,
            report,
        ) = prepared_capture()

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "relocated-study" / "RUN-RECOVER-1"
            with mock.patch.object(app, "calibration_run_dir", return_value=run_dir):
                paths, manifest_path = app.save_calibration_run_evidence(
                    batch_number="LOT-RECOVER",
                    sensor_id="LOT-RECOVER-1",
                    specimen_id="S-RECOVER-1",
                    tester_name="Technician A",
                    filter_setup=app.DEFAULT_FILTER_SETUP,
                    session_id="SESSION-RECOVER",
                    run_id="RUN-RECOVER-1",
                    measurement_run_number=4,
                    is_synthetic=True,
                    metrics=metrics,
                    final_result=final,
                    report=report,
                    battery_v=6.2,
                    reference_calibration=None,
                    reference_check_mv=None,
                )

            manifest = app.validate_calibration_run_manifest(
                manifest_path,
                expected_run_id="RUN-RECOVER-1",
                expected_specimen_id="S-RECOVER-1",
                expected_app_verdict="PASS",
            )
            (
                recovered_manifest,
                recovered_metrics,
                recovered_final,
                recovered_report,
                recovered_reference,
            ) = app.recover_calibration_run_from_manifest(manifest_path)

            samples_path = next(
                path for path in paths if path.name.endswith("_samples.csv")
            )
            samples_path.write_bytes(samples_path.read_bytes() + b"tampered\n")
            with self.assertRaisesRegex(ValueError, "evidence size changed"):
                app.validate_calibration_run_manifest(manifest_path)

        self.assertEqual(manifest, recovered_manifest)
        self.assertEqual(recovered_manifest["run_id"], "RUN-RECOVER-1")
        self.assertTrue(recovered_manifest["is_synthetic"])
        self.assertIsNone(recovered_manifest["reference"])
        self.assertIsNone(recovered_reference)
        self.assertEqual(recovered_final.passed, final.passed)
        self.assertAlmostEqual(
            recovered_metrics.sensitivity_mv,
            metrics.sensitivity_mv,
        )
        self.assertEqual(len(recovered_metrics.waveform_v), len(waveform))
        np.testing.assert_allclose(recovered_metrics.waveform_v, waveform)
        np.testing.assert_allclose(recovered_metrics.sync_v, sync)
        self.assertIsNotNone(recovered_report)
        self.assertEqual(recovered_report.data_source, "simulator")

    def test_recovery_preserves_unmeasured_unstable_fields(self):
        _waveform, _sync, _rate, _offset, _analysis, metrics, final, report = (
            prepared_capture()
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "RUN-UNSTABLE-1"
            with mock.patch.object(app, "calibration_run_dir", return_value=run_dir):
                _paths, manifest_path = app.save_calibration_run_evidence(
                    batch_number="LOT-UNSTABLE",
                    sensor_id="LOT-UNSTABLE-1",
                    specimen_id="S-UNSTABLE-1",
                    tester_name="Technician A",
                    filter_setup=app.DEFAULT_FILTER_SETUP,
                    session_id="SESSION-UNSTABLE",
                    run_id="RUN-UNSTABLE-1",
                    measurement_run_number=1,
                    is_synthetic=True,
                    metrics=metrics,
                    final_result=final,
                    report=report,
                    battery_v=6.2,
                    reference_calibration=None,
                    reference_check_mv=None,
                )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["metrics"].update(
                sensitivity_mv=0.0,
                polarity="NOT MEASURED",
                stabilized=False,
            )
            manifest["app_prediction"].update(
                verdict="FAIL",
                sensitivity_mv=None,
                polarity="",
                fail_reasons=["Sensor waveform was unstable."],
                suggested_failure_mode=app.UNSTABLE_FAILURE_MODE,
            )
            manifest["stability_report"]["unstable"] = True
            app.atomic_write_json(manifest_path, manifest)
            _payload, recovered_metrics, recovered_final, _report, _reference = (
                app.recover_calibration_run_from_manifest(manifest_path)
            )

        self.assertEqual(recovered_metrics.sensitivity_mv, 0.0)
        self.assertEqual(recovered_metrics.polarity, "NOT MEASURED")
        self.assertIsNone(recovered_final.sensitivity_mv)
        self.assertEqual(recovered_final.polarity, "")
        self.assertFalse(recovered_final.passed)

    def test_synthetic_evidence_never_receives_hardware_reference_state(self):
        (
            _waveform,
            _sync,
            _rate,
            _offset,
            _analysis,
            metrics,
            final,
            report,
        ) = prepared_capture()
        hardware_reference = app.build_reference_calibration([100.0] * 5)
        reference_capture = object()
        captured: dict = {}

        def fake_save(**kwargs):
            captured.update(kwargs)
            manifest_path = Path("/synthetic/evidence/run_manifest.json")
            return [manifest_path], manifest_path

        harness = SimpleNamespace(
            last_run_manifest_path=None,
            last_metrics=metrics,
            last_result=final,
            active_run_id="RUN-SYNTHETIC-1",
            batch_number="LOT-SYNTHETIC",
            current_sensor_id="LOT-SYNTHETIC-1",
            measured_specimen_id="S-SYNTHETIC-1",
            tester_name="Technician A",
            filter_setup=app.DEFAULT_FILTER_SETUP,
            session_id="SESSION-SYNTHETIC",
            measurement_run_number=1,
            batch_is_synthetic=True,
            last_capture_report=report,
            battery_v=6.2,
            last_reference_calibration=hardware_reference,
            last_reference_check_mv=100.0,
            last_reference_capture=reference_capture,
            last_dut_raw_samples=(),
            last_dut_stream_diagnostics=None,
            offset_plausibility_override_applied=False,
            last_rig_metadata={"port": "must-not-imply-hardware-reference"},
            last_run_evidence_paths=[],
            last_run_evidence_error="stale",
            stability_diagnostics_saved=False,
            run_history=[],
        )

        with mock.patch.object(app, "save_calibration_run_evidence", side_effect=fake_save):
            saved = app.EmitterTesterApp._ensure_current_run_evidence(harness)

        self.assertTrue(saved)
        self.assertTrue(captured["is_synthetic"])
        self.assertIsNone(captured["reference_calibration"])
        self.assertIsNone(captured["reference_check_mv"])
        self.assertIsNone(captured["reference_capture"])


if __name__ == "__main__":
    unittest.main()
