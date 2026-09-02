from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tkinter import ttk

import numpy as np


V6_1_DIR = Path(__file__).resolve().parents[1]
if str(V6_1_DIR) not in sys.path:
    sys.path.insert(0, str(V6_1_DIR))

import eltec_406mca_esp32_tester as app  # noqa: E402
from stability_analysis import analyze_stability, load_stability_settings  # noqa: E402


SETTINGS = load_stability_settings()


@dataclass
class FakeDiagnostics:
    received_samples: int
    measured_rate_hz: float = app.DEFAULT_SAMPLE_RATE_HZ
    torn_lines: int = 0
    timestamp_gap_count: int = 0
    estimated_missing_samples: int = 0
    duplicate_timestamps: int = 0
    reordered_timestamps: int = 0
    firmware_samples_sent: int | None = None
    firmware_adc_overruns: int = 0
    expected_rate_hz: float = app.DEFAULT_SAMPLE_RATE_HZ

    def __post_init__(self):
        if self.firmware_samples_sent is None:
            self.firmware_samples_sent = self.received_samples

    @property
    def count_matches_firmware(self):
        return self.received_samples == self.firmware_samples_sent

    @property
    def rate_error_percent(self):
        return (
            (self.measured_rate_hz - self.expected_rate_hz)
            / self.expected_rate_hz
            * 100.0
        )


def prepared_capture(case_name: str):
    waveform, sync, rate, offset = app.simulate_v6_startup_capture(
        app.DEFAULT_FILTER_SETUP,
        case_name,
    )
    dut_settings = app.dut_stability_settings(SETTINGS)
    analysis = analyze_stability(
        waveform,
        sync,
        rate,
        dut_settings,
        stability_deadline_s=app.STABILITY_TIMEOUT_S,
        measurement_cycles_required=app.SENSITIVITY_MEASUREMENT_CYCLES,
        enforce_measurement_stability=True,
        max_measurement_attempts=app.MAX_MEASUREMENT_ATTEMPTS,
        data_source="test",
    )
    if analysis.report.measurement_complete:
        cut = analysis.measurement_cycles[-1].end_index + 1
    else:
        cut = int(app.STABILITY_TIMEOUT_S * rate) + 1
    waveform = waveform[:cut]
    sync = sync[:cut]
    analysis = analyze_stability(
        waveform,
        sync,
        rate,
        dut_settings,
        stability_deadline_s=app.STABILITY_TIMEOUT_S,
        measurement_cycles_required=app.SENSITIVITY_MEASUREMENT_CYCLES,
        enforce_measurement_stability=True,
        max_measurement_attempts=app.MAX_MEASUREMENT_ATTEMPTS,
        data_source="test",
    )
    return waveform, sync, rate, offset, analysis


def metrics_for_sensitivity(raw_sensitivity_mv: float) -> app.WaveformMetrics:
    return app.WaveformMetrics(
        sensitivity_mv=raw_sensitivity_mv,
        sensitivity_amplified_mv=raw_sensitivity_mv,
        polarity=app.POSITIVE_POLARITY,
        measured_frequency_hz=app.EXPECTED_FREQUENCY_HZ,
        cycles_used=app.SENSITIVITY_MEASUREMENT_CYCLES,
        stabilized=True,
    )


def stream_samples_for_peaks(peaks_v: list[float], *, samples_per_cycle: int = 100):
    """Build a 10 Hz stream whose robust upper peak follows ``peaks_v``."""

    waveform = [peaks_v[0] - 0.020]
    sync = [0.0]
    half = samples_per_cycle // 2
    for peak in peaks_v:
        waveform.extend([peak] * half + [peak - 0.020] * half)
        sync.extend([1.0] * half + [0.0] * half)
    waveform.append(peaks_v[-1])
    sync.append(1.0)
    return [
        SimpleNamespace(volts=float(volts), sync=int(sync_value))
        for volts, sync_value in zip(waveform, sync)
    ]


class FakeLowLevelRig(app.EmitterEsp32Rig):
    STREAM_CHUNK_SAMPLES = 1000

    def __init__(self, case_name="Known good", *, sync_broken=False, gap_count=0):
        waveform, sync, _rate, _offset = app.simulate_v6_startup_capture(
            app.DEFAULT_FILTER_SETUP,
            case_name,
        )
        if sync_broken:
            sync = np.zeros_like(sync)
        self._samples = [
            SimpleNamespace(volts=float(volts), sync=int(sync_value))
            for volts, sync_value in zip(waveform, sync)
        ]
        self._cursor = 0
        self._active = False
        self._diagnostics = None
        self._drained_samples = []
        self.started_channels = []
        self.gap_count = gap_count
        self.stop_calls = 0

    @property
    def is_streaming(self):
        return self._active

    @property
    def stream_diagnostics(self):
        return self._diagnostics

    def connect(self):
        return None

    def start_stream(self, channel="sensor"):
        self._active = True
        self._cursor = 0
        self._diagnostics = None
        self.started_channels.append(channel)
        return SimpleNamespace(sample_rate_hz=1000.0, channel=channel.upper())

    def read_stream(self, max_samples=None, *, timeout_s=1.0):
        del timeout_s
        amount = len(self._samples) if max_samples is None else int(max_samples)
        end = min(len(self._samples), self._cursor + amount)
        chunk = self._samples[self._cursor:end]
        self._cursor = end
        return chunk

    def stop_stream(self, *, timeout_s=2.0, raise_on_timeout=True):
        del timeout_s, raise_on_timeout
        self.stop_calls += 1
        self._active = False
        self._diagnostics = FakeDiagnostics(
            received_samples=self._cursor,
            timestamp_gap_count=self.gap_count,
            estimated_missing_samples=self.gap_count,
        )
        return self._diagnostics


class IdentityAndCsvTests(unittest.TestCase):
    def test_v6_1_identity_and_result_namespace_are_isolated(self):
        self.assertEqual(app.results_root_dir().name, "v6_1_esp32")
        self.assertNotIn("capture_mode", app.CSV_FIELDS)
        self.assertNotIn("fast_match", app.CSV_FIELDS)
        self.assertIn("stabilization_seconds", app.CSV_FIELDS)
        self.assertIn("pwm_on_seconds", app.CSV_FIELDS)
        self.assertIn("reference_calibration_mv", app.CSV_FIELDS)
        self.assertIn("reference_check_mv", app.CSV_FIELDS)
        self.assertIn("reference_drift_pct", app.CSV_FIELDS)
        self.assertIn("failure_mode_tag", app.CSV_FIELDS)
        self.assertIn("failure_mode_reason", app.CSV_FIELDS)
        self.assertIn("sensitivity_raw_mv", app.CSV_FIELDS)
        self.assertIn("sensitivity_legacy_equivalent_mv", app.CSV_FIELDS)
        self.assertIn("sensitivity_correction_factor", app.CSV_FIELDS)
        self.assertIn("sensitivity_calibration_id", app.CSV_FIELDS)
        self.assertIn("sensitivity_gate_outcome", app.CSV_FIELDS)
        self.assertIn("sensitivity_raw_fail_below_mv", app.CSV_FIELDS)
        self.assertIn("sensitivity_raw_pass_above_mv", app.CSV_FIELDS)
        self.assertIn("measurement_attempt", app.CSV_FIELDS)
        self.assertIn("measurement_failures", app.CSV_FIELDS)
        self.assertIn("active_stability_required_deltas", app.CSV_FIELDS)
        self.assertIn("active_measurement_cycles_required", app.CSV_FIELDS)
        self.assertIn(app.UNSTABLE_FAILURE_MODE, app.FAILURE_MODE_CHOICES)
        # A near-limit sensitivity is a PASS with a warning, never a failure mode.
        self.assertFalse(
            any("RETEST" in choice or "guard band" in choice for choice in app.FAILURE_MODE_CHOICES)
        )
        self.assertEqual(app.MAX_MEASUREMENT_ATTEMPTS, 3)
        self.assertEqual(app.DUT_STABILITY_CONFIRMATION_DELTAS, 10)
        self.assertEqual(app.SENSITIVITY_MEASUREMENT_CYCLES, 20)
        self.assertEqual(SETTINGS.peak_delta_threshold_mv, 0.100)
        self.assertEqual(SETTINGS.consecutive_deltas_required, 5)

    def test_launcher_installation_uses_only_v6_1_identities(self):
        installer = V6_1_DIR / "install_xubuntu_launcher.sh"
        run_script = V6_1_DIR / "run_eltec_406mca_esp32_tester.sh"
        self.assertIn(
            "eltec-406mca-esp32-v6-1",
            run_script.read_text(encoding="utf-8"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            data_home = home / ".local" / "share"
            desktop = home / "Desktop"
            applications = data_home / "applications"
            desktop.mkdir(parents=True)
            applications.mkdir(parents=True)
            old_menu = applications / "com.eltec.406mca-esp32-tester.desktop"
            old_desktop = desktop / "Eltec 406MCA ESP32 Tester.desktop"
            old_menu.write_text("v5 menu sentinel\n", encoding="utf-8")
            old_desktop.write_text("v5 desktop sentinel\n", encoding="utf-8")
            environment = {
                **os.environ,
                "HOME": str(home),
                "XDG_DATA_HOME": str(data_home),
            }

            subprocess.run(
                [str(installer)],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )
            v6_1_menu = applications / "com.eltec.406mca-esp32-tester-v6-1.desktop"
            v6_1_desktop = desktop / "Eltec 406MCA ESP32 Tester v6.1.desktop"
            self.assertTrue(v6_1_menu.exists())
            self.assertTrue(v6_1_desktop.exists())
            self.assertIn(
                "Name=Eltec 406MCA ESP32 Tester v6.1",
                v6_1_menu.read_text(encoding="utf-8"),
            )
            self.assertEqual(old_menu.read_text(encoding="utf-8"), "v5 menu sentinel\n")
            self.assertEqual(
                old_desktop.read_text(encoding="utf-8"),
                "v5 desktop sentinel\n",
            )

            subprocess.run(
                [str(installer), "--uninstall"],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertFalse(v6_1_menu.exists())
            self.assertFalse(v6_1_desktop.exists())
            self.assertTrue(old_menu.exists())
            self.assertTrue(old_desktop.exists())

    def test_timeout_csv_has_no_official_sensitivity_or_polarity(self):
        waveform, sync, rate, offset, analysis = prepared_capture("Never stabilizes")
        metrics, final = app.build_stability_timeout_result(
            waveform,
            sync,
            rate,
            analysis,
            offset_v=offset,
            input_range_v=app.WAVEFORM_INPUT_RANGE_V,
        )
        report = app.StabilityCaptureReport.from_analysis(
            analysis,
            data_source="simulator",
        )
        self.assertFalse(metrics.stabilized)
        self.assertIsNone(final.sensitivity_mv)
        self.assertEqual(final.polarity, "")
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "batch.csv"
            app.append_result_csv(
                csv_path,
                batch_number="B1",
                sensor_number=1,
                sensor_id="B1-1",
                tester_name="Operator",
                filter_setup=app.DEFAULT_FILTER_SETUP,
                pwm_channel=app.EMITTER_PWM_CHANNEL,
                pwm_hz=app.EMITTER_PWM_FREQUENCY_HZ,
                pwm_duty=app.EMITTER_PWM_DUTY_CYCLE,
                final_result=final,
                comment="",
                snapshot_paths=[],
                capture_report=report,
            )
            with csv_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["sensitivity_mv"], "")
            self.assertEqual(row["polarity"], "")
            self.assertEqual(row["polarity_good_bad"], "")
            self.assertEqual(row["pass_fail"], "FAIL")
            self.assertEqual(row["failure_mode_tag"], "Unstable")
            self.assertEqual(row["failure_mode_reason"], "Unstable")
            self.assertTrue(row["fail_reasons"].startswith("Unstable:"))
            self.assertEqual(row["stability_timeout"], "YES")
            self.assertEqual(row["stability_threshold_mv"], "0.100000")

    def test_failure_mode_is_required_and_unstable_timeout_is_suggested(self):
        waveform, sync, rate, offset, analysis = prepared_capture("Never stabilizes")
        _metrics, final = app.build_stability_timeout_result(
            waveform,
            sync,
            rate,
            analysis,
            offset_v=offset,
            input_range_v=app.WAVEFORM_INPUT_RANGE_V,
        )

        self.assertEqual(app.suggest_failure_mode(final), app.UNSTABLE_FAILURE_MODE)
        self.assertEqual(
            app.split_failure_mode(app.UNSTABLE_FAILURE_MODE),
            ("Unstable", "Unstable"),
        )
        with self.assertRaisesRegex(ValueError, "Choose a failure mode"):
            app.split_failure_mode("")

    def test_failure_mode_priority_uses_offset_then_coherent_response(self):
        def failed(offset_v, *reasons):
            return SimpleNamespace(
                passed=False,
                offset_v=offset_v,
                fail_reasons=list(reasons),
            )

        self.assertEqual(
            app.suggest_failure_mode(
                failed(1.334, "Offset out of range", "Unstable waveform")
            ),
            "HO - High offset",
        )
        self.assertEqual(
            app.suggest_failure_mode(failed(0.0001, "Offset out of range", "Sensitivity too low")),
            "D - No offset",
        )
        self.assertEqual(
            app.suggest_failure_mode(failed(0.200, "Offset out of range")),
            "LO - Low offset",
        )
        self.assertEqual(
            app.suggest_failure_mode(
                failed(0.700, "Sensitivity too low", "Signal-to-noise too low")
            ),
            "GO/D - Good offset/no signal",
        )
        self.assertEqual(
            app.suggest_failure_mode(failed(0.700, "Sensitivity too low")),
            "LS - Low sensitivity",
        )

    def test_timeout_diagnostic_sidecars_retain_full_stream_and_cycle_deltas(self):
        waveform, sync, rate, offset, analysis = prepared_capture("Never stabilizes")
        metrics, _final = app.build_stability_timeout_result(
            waveform,
            sync,
            rate,
            analysis,
            offset_v=offset,
            input_range_v=app.WAVEFORM_INPUT_RANGE_V,
        )
        report = app.StabilityCaptureReport.from_analysis(
            analysis,
            data_source="simulator",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "timeout.png"
            samples_path, cycles_path = app.save_stability_diagnostic_csvs(
                base,
                batch_number="B1",
                sensor_id="B1-1",
                metrics=metrics,
                report=report,
            )
            with samples_path.open(newline="", encoding="utf-8") as handle:
                sample_rows = list(csv.DictReader(handle))
            with cycles_path.open(newline="", encoding="utf-8") as handle:
                cycle_rows = list(csv.DictReader(handle))

        self.assertEqual(len(sample_rows), len(waveform))
        self.assertEqual(len(cycle_rows), len(analysis.cycles))
        self.assertEqual(sample_rows[-1]["voltage_v"], f"{float(waveform[-1]):.12g}")
        self.assertEqual(
            float(cycle_rows[-1]["absolute_peak_delta_mv"]),
            analysis.cycles[-1].absolute_peak_delta_mv,
        )
        self.assertEqual(
            float(cycle_rows[-1]["robust_peak_v"]),
            analysis.cycles[-1].robust_peak_v,
        )

    def test_reopening_batch_appends_without_rewriting_old_row(self):
        waveform, sync, rate, offset, analysis = prepared_capture("Known good")
        metrics = app.analyze_v6_stable_measurement(
            waveform,
            sync,
            rate,
            analysis,
            offset_v=offset,
            input_range_v=app.WAVEFORM_INPUT_RANGE_V,
        )
        final = app.evaluate_result(offset, metrics, app.DEFAULT_FILTER_SETUP)
        common = dict(
            batch_number="B2",
            tester_name="Operator",
            filter_setup=app.DEFAULT_FILTER_SETUP,
            pwm_channel=app.EMITTER_PWM_CHANNEL,
            pwm_hz=app.EMITTER_PWM_FREQUENCY_HZ,
            pwm_duty=app.EMITTER_PWM_DUTY_CYCLE,
            final_result=final,
            comment="",
            snapshot_paths=[],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "batch.csv"
            app.append_result_csv(path, sensor_number=1, sensor_id="B2-1", **common)
            original = path.read_bytes()
            app.append_result_csv(path, sensor_number=2, sensor_id="B2-2", **common)
            self.assertTrue(path.read_bytes().startswith(original))
            self.assertEqual(app.next_sensor_number_for_batch(path), 3)

    def test_sensitivity_factor_and_raw_guard_band_boundaries(self):
        self.assertTrue(app.LOW_SENSITIVITY_FAILURE_ENABLED)
        self.assertEqual(app.SENSITIVITY_LEGACY_EQUIVALENT_FACTOR, 1.582)
        self.assertEqual(
            app.sensitivity_raw_limits_mv(app.DEFAULT_FILTER_SETUP),
            (2.43, 2.63),
        )
        self.assertAlmostEqual(
            app.legacy_equivalent_sensitivity_mv(3.3), 5.2206, places=6
        )

        # Inside the +/-0.10 mV band the sensor still PASSES (it is within the
        # conversion factor's margin of error) but carries a re-measure warning.
        cases = (
            (2.429, app.OUTCOME_FAIL, app.OUTCOME_FAIL),
            (2.430, app.SENSITIVITY_NEAR_LIMIT, app.OUTCOME_PASS),
            (2.530, app.SENSITIVITY_NEAR_LIMIT, app.OUTCOME_PASS),
            (2.630, app.SENSITIVITY_NEAR_LIMIT, app.OUTCOME_PASS),
            (2.631, app.OUTCOME_PASS, app.OUTCOME_PASS),
        )
        for raw_mv, gate, expected in cases:
            with self.subTest(raw_mv=raw_mv):
                metrics = metrics_for_sensitivity(raw_mv)
                final = app.evaluate_result(0.7, metrics, app.DEFAULT_FILTER_SETUP)
                self.assertEqual(
                    app.sensitivity_gate_outcome(raw_mv, app.DEFAULT_FILTER_SETUP),
                    gate,
                )
                self.assertEqual(app.result_outcome(final), expected)
                self.assertEqual(final.passed, expected == app.OUTCOME_PASS)
                self.assertEqual(
                    app.is_sensitivity_near_limit(final),
                    gate == app.SENSITIVITY_NEAR_LIMIT,
                )

        failed = app.evaluate_result(
            0.7, metrics_for_sensitivity(2.429), app.DEFAULT_FILTER_SETUP
        )
        self.assertTrue(
            any(reason.startswith("Sensitivity too low:") for reason in failed.fail_reasons)
        )
        near = app.evaluate_result(
            0.7, metrics_for_sensitivity(2.53), app.DEFAULT_FILTER_SETUP
        )
        self.assertTrue(near.passed)
        self.assertEqual(near.fail_reasons, [])
        self.assertTrue(
            any(w.startswith(app.SENSITIVITY_NEAR_LIMIT_WARNING_PREFIX) for w in near.warnings)
        )
        self.assertEqual(app.suggest_failure_mode(near), "")

    def test_sensitivity_gate_preserves_the_unscaled_offset_gate(self):
        final = app.evaluate_result(
            0.18, metrics_for_sensitivity(3.0), app.DEFAULT_FILTER_SETUP
        )

        self.assertFalse(final.passed)
        self.assertEqual(final.offset_v, 0.18)
        self.assertTrue(any("Offset out of range" in reason for reason in final.fail_reasons))
        self.assertFalse(any("Sensitivity too low" in reason for reason in final.fail_reasons))

        borderline_with_bad_offset = app.evaluate_result(
            0.18, metrics_for_sensitivity(2.53), app.DEFAULT_FILTER_SETUP
        )
        self.assertEqual(app.result_outcome(borderline_with_bad_offset), app.OUTCOME_FAIL)
        self.assertEqual(
            app.suggest_failure_mode(borderline_with_bad_offset), "LO - Low offset"
        )

    def test_near_limit_csv_preserves_raw_and_records_legacy_equivalent_value(self):
        final = app.evaluate_result(
            0.7, metrics_for_sensitivity(2.53), app.DEFAULT_FILTER_SETUP
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "batch.csv"
            app.append_result_csv(
                csv_path,
                batch_number="B3",
                sensor_number=1,
                sensor_id="B3-1",
                tester_name="Operator",
                filter_setup=app.DEFAULT_FILTER_SETUP,
                pwm_channel=app.EMITTER_PWM_CHANNEL,
                pwm_hz=app.EMITTER_PWM_FREQUENCY_HZ,
                pwm_duty=app.EMITTER_PWM_DUTY_CYCLE,
                final_result=final,
                comment="",
                snapshot_paths=[],
            )
            with csv_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["sensitivity_mv"], "2.530000")
        self.assertEqual(row["sensitivity_raw_mv"], "2.530000")
        self.assertEqual(row["sensitivity_legacy_equivalent_mv"], "4.002460")
        self.assertEqual(row["sensitivity_correction_factor"], "1.582000")
        self.assertEqual(row["sensitivity_calibration_id"], "lot_520_paired_v1")
        self.assertEqual(row["sensitivity_gate_outcome"], app.SENSITIVITY_NEAR_LIMIT)
        self.assertEqual(row["sensitivity_raw_fail_below_mv"], "2.430000")
        self.assertEqual(row["sensitivity_raw_pass_above_mv"], "2.630000")
        # Near the limit is still a PASS row: no failure mode is recorded.
        self.assertEqual(row["pass_fail"], app.OUTCOME_PASS)
        self.assertEqual(row["failure_mode_tag"], "")
        self.assertEqual(row["failure_mode_reason"], "")

    def test_official_signal_math_uses_only_twenty_post_stability_cycles(self):
        waveform = [0.69]
        sync = [0.0]
        for cycle_number in range(1, 32):
            low = 0.60 if cycle_number <= 11 else 0.69
            waveform.extend([0.70] * 50 + [low] * 50)
            sync.extend([1.0] * 50 + [0.0] * 50)
        waveform.append(0.70)
        sync.append(1.0)
        waveform_np = np.asarray(waveform, dtype=float)
        sync_np = np.asarray(sync, dtype=float)
        analysis = analyze_stability(
            waveform_np,
            sync_np,
            1000.0,
            app.dut_stability_settings(SETTINGS),
            measurement_cycles_required=app.SENSITIVITY_MEASUREMENT_CYCLES,
            enforce_measurement_stability=True,
            max_measurement_attempts=app.MAX_MEASUREMENT_ATTEMPTS,
        )
        metrics = app.analyze_v6_stable_measurement(
            waveform_np,
            sync_np,
            1000.0,
            analysis,
            offset_v=0.695,
            input_range_v=app.WAVEFORM_INPUT_RANGE_V,
        )
        self.assertEqual(analysis.report.stabilization_cycle, 11)
        self.assertEqual(metrics.cycles_used, 20)
        self.assertEqual(len(metrics.cycle_pp_mv), 20)
        self.assertEqual(metrics.noise_cycles_used, 20)
        self.assertEqual(metrics.polarity, "NEGATIVE")
        self.assertAlmostEqual(metrics.sensitivity_mv, 10.0, places=6)


class ContinuousCaptureTests(unittest.TestCase):
    def test_stable_stream_stops_after_twenty_fresh_cycles_and_drives_preview(self):
        rig = FakeLowLevelRig("Known good")
        progress = []
        previews = []
        waveform, sync, rate, analysis = rig.read_waveform_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic(),
            progress=progress.append,
            preview=lambda wf, sy: previews.append((len(wf), len(sy))),
        )
        self.assertTrue(analysis.report.measurement_complete)
        self.assertFalse(analysis.report.timed_out)
        self.assertEqual(len(analysis.measurement_cycles), 20)
        self.assertGreater(analysis.report.stabilization_elapsed_s, 9.0)
        self.assertGreater(len(waveform), 10000)
        self.assertEqual(len(waveform), len(sync))
        self.assertEqual(rate, 1000.0)
        self.assertGreater(len(progress), 1)
        self.assertTrue(previews)
        self.assertLessEqual(previews[-1][0], app.STREAM_PREVIEW_MAX_SAMPLES)
        self.assertEqual(rig.stop_calls, 1)
        self.assertFalse(rig.is_streaming)

    def test_production_reader_retries_with_the_same_ten_twenty_windows(self):
        rig = FakeLowLevelRig()
        # Attempt 1 qualifies on cycle 11, then gets kicked by cycle 16.
        # Attempt 2 requalifies over cycles 17-26 and measures cycles 27-46.
        peaks = [0.7000] * 15 + [0.7002] + [0.7002] * 30
        rig._samples = stream_samples_for_peaks(peaks)
        progress = []

        waveform, sync, rate, analysis = rig.read_waveform_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic(),
            progress=progress.append,
        )
        metrics = app.analyze_v6_stable_measurement(
            waveform,
            sync,
            rate,
            analysis,
            offset_v=0.700,
            input_range_v=app.WAVEFORM_INPUT_RANGE_V,
        )

        self.assertTrue(analysis.report.measurement_complete)
        self.assertEqual(analysis.report.measurement_attempt, 2)
        self.assertEqual(analysis.report.measurement_failures, 1)
        self.assertEqual(analysis.report.active_confirmation_count, 10)
        self.assertEqual(analysis.report.measurement_cycles_required, 20)
        self.assertEqual(len(analysis.measurement_cycles), 20)
        self.assertEqual(metrics.cycles_used, 20)
        self.assertEqual(len(metrics.cycle_pp_mv), 20)
        self.assertEqual(analysis.report.data_source, "esp32_v6_1")
        self.assertTrue(any(item.report.measurement_attempt == 2 for item in progress))
        self.assertEqual(rig.stop_calls, 1)

    def test_reference_stream_uses_dedicated_delta_then_five_fresh_cycles(self):
        rig = FakeLowLevelRig("Known good")

        _waveform, _sync, _rate, analysis = rig.read_reference_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic(),
        )

        self.assertTrue(analysis.report.measurement_complete)
        self.assertEqual(SETTINGS.peak_delta_threshold_mv, 0.100)
        self.assertEqual(analysis.report.configured_threshold_mv, 0.250)
        self.assertEqual(analysis.report.configured_confirmation_count, 5)
        self.assertEqual(len(analysis.measurement_cycles), 5)
        self.assertEqual(analysis.report.data_source, "esp32_reference")
        self.assertEqual(rig.started_channels, ["ref"])

    def test_never_stable_stream_times_out_at_twenty_seconds(self):
        rig = FakeLowLevelRig("Never stabilizes")
        waveform, _sync, _rate, analysis = rig.read_waveform_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic() + 1.0,
        )
        self.assertFalse(analysis.report.stabilized)
        self.assertTrue(analysis.report.timed_out)
        self.assertEqual(analysis.report.measurement_cycle_count, 0)
        self.assertGreaterEqual(len(waveform), 20000)
        self.assertLess(len(waveform), 21000)
        self.assertEqual(rig.stop_calls, 1)

    def test_missing_sync_is_a_rig_error_not_a_part_verdict(self):
        rig = FakeLowLevelRig(sync_broken=True)
        with self.assertRaisesRegex(app.HardwareNotReadyError, "sync did not toggle"):
            rig.read_waveform_until_stable(
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                settings=SETTINGS,
                pwm_started_monotonic=time.monotonic(),
            )
        self.assertEqual(rig.stop_calls, 1)

    def test_single_transition_and_wrong_frequency_are_rig_errors(self):
        single_transition = FakeLowLevelRig()
        for index, sample in enumerate(single_transition._samples):
            sample.sync = 0 if index < 50 else 1
        with self.assertRaisesRegex(app.HardwareNotReadyError, "complete rising-edge cycles"):
            single_transition.read_waveform_until_stable(
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                settings=SETTINGS,
                pwm_started_monotonic=time.monotonic(),
            )
        self.assertEqual(single_transition.stop_calls, 1)

        wrong_frequency = FakeLowLevelRig()
        for index, sample in enumerate(wrong_frequency._samples):
            sample.sync = 1 if (index % 200) < 100 else 0
        with self.assertRaisesRegex(app.HardwareNotReadyError, "frequency is 5.000 Hz"):
            wrong_frequency.read_waveform_until_stable(
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                settings=SETTINGS,
                pwm_started_monotonic=time.monotonic(),
            )
        self.assertEqual(wrong_frequency.stop_calls, 1)

    def test_streaming_accepts_stability_closing_exactly_at_twenty_seconds(self):
        rig = FakeLowLevelRig()
        rig.STREAM_CHUNK_SAMPLES = 100
        sample_count = 23_001
        rig._samples = []
        for index in range(sample_count):
            physical_cycle = index // 100
            if physical_cycle < 189:
                peak_v = 0.700 if physical_cycle % 2 == 0 else 0.701
            else:
                peak_v = 0.701
            rig._samples.append(
                SimpleNamespace(
                    volts=peak_v,
                    sync=1 if (index % 100) < 50 else 0,
                )
            )

        waveform, _sync, _rate, analysis = rig.read_waveform_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic() + 1.0,
        )
        self.assertTrue(analysis.report.measurement_complete)
        self.assertAlmostEqual(analysis.report.stabilization_elapsed_s, 20.0)
        self.assertEqual(analysis.report.measurement_cycle_count, 20)
        self.assertGreater(len(waveform), 21_900)

    def test_integrity_error_and_cancellation_both_stop_stream(self):
        rig = FakeLowLevelRig(gap_count=1)
        with self.assertRaisesRegex(app.Esp32BackendError, "timestamp gaps"):
            rig.read_waveform_until_stable(
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                settings=SETTINGS,
                pwm_started_monotonic=time.monotonic(),
            )
        self.assertEqual(rig.stop_calls, 1)

        cancelled = FakeLowLevelRig()
        with self.assertRaisesRegex(app.Esp32BackendError, "cancelled"):
            cancelled.read_waveform_until_stable(
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                settings=SETTINGS,
                pwm_started_monotonic=time.monotonic(),
                cancelled=lambda: True,
            )
        self.assertEqual(cancelled.stop_calls, 1)


class ReferenceCalibrationTests(unittest.TestCase):
    def test_five_readings_create_average_and_twenty_five_percent_window(self):
        calibration = app.build_reference_calibration([98.0, 99.0, 100.0, 101.0, 102.0])

        self.assertAlmostEqual(calibration.mean_mv, 100.0)
        self.assertAlmostEqual(calibration.lower_mv, 75.0)
        self.assertAlmostEqual(calibration.upper_mv, 125.0)
        self.assertTrue(calibration.accepts(75.0))
        self.assertTrue(calibration.accepts(125.0))
        self.assertFalse(calibration.accepts(74.99))

        # This spread was rejected by the former +/-10% calibration rule.
        wider_calibration = app.build_reference_calibration(
            [80.0, 100.0, 100.0, 100.0, 100.0]
        )
        self.assertAlmostEqual(wider_calibration.mean_mv, 96.0)

    def test_loading_old_calibration_upgrades_ten_percent_window(self):
        payload = app.build_reference_calibration([100.0] * 5).to_dict()
        payload["tolerance_percent"] = 10.0

        calibration = app.ReferenceCalibration.from_dict(payload)

        self.assertEqual(calibration.tolerance_percent, 25.0)
        self.assertAlmostEqual(calibration.lower_mv, 75.0)
        self.assertAlmostEqual(calibration.upper_mv, 125.0)

    def test_old_ten_percent_failure_inside_new_window_is_reenabled(self):
        payload = app.build_reference_calibration([100.0] * 5).to_dict()
        payload.update(
            tolerance_percent=10.0,
            valid=False,
            invalidated_at="2026-07-16T12:00:00",
            invalidation_reason="Reference was outside +/-10%.",
            failed_reading_mv=115.0,
        )

        calibration = app.ReferenceCalibration.from_dict(payload)

        self.assertTrue(calibration.valid)
        self.assertIsNone(calibration.invalidation_reason)
        self.assertIsNone(calibration.failed_reading_mv)

        payload["failed_reading_mv"] = 126.0
        still_invalid = app.ReferenceCalibration.from_dict(payload)
        self.assertFalse(still_invalid.valid)

    def test_unrepeatable_calibration_is_rejected(self):
        with self.assertRaisesRegex(app.ReferenceCalibrationError, "not repeatable"):
            app.build_reference_calibration([50.0, 100.0, 100.0, 100.0, 100.0])

    def test_calibration_round_trip_and_invalidation_persist(self):
        calibration = app.build_reference_calibration([100.0] * 5)
        invalid = calibration.invalidated("emitter check failed", 80.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "reference.json"
            app.save_reference_calibration(invalid, path)
            loaded = app.load_reference_calibration(path)

        self.assertFalse(loaded.valid)
        self.assertEqual(loaded.failed_reading_mv, 80.0)
        self.assertEqual(loaded.invalidation_reason, "emitter check failed")

    def test_missing_v6_1_calibration_reads_v6_baseline_without_copying_it(self):
        calibration = app.build_reference_calibration([100.0] * 5)
        with tempfile.TemporaryDirectory() as temp_dir:
            local = Path(temp_dir) / "v6_1" / "reference.json"
            legacy = Path(temp_dir) / "v6" / "reference.json"
            app.save_reference_calibration(calibration, legacy)
            with mock.patch.object(app, "reference_calibration_path", return_value=local), mock.patch.object(
                app, "v6_reference_calibration_path", return_value=legacy
            ):
                loaded = app.load_reference_calibration()

            self.assertEqual(loaded.mean_mv, 100.0)
            self.assertFalse(local.exists())

    def test_reference_response_averages_exactly_five_fresh_cycles(self):
        analysis = SimpleNamespace(
            report=SimpleNamespace(measurement_complete=True, timed_out=False),
            measurement_cycles=tuple(
                SimpleNamespace(peak_to_peak_v=value)
                for value in (0.004, 0.005, 0.006, 0.007, 0.008)
            ),
        )

        reading_mv = app.analyze_reference_stable_response_mv(analysis)

        self.assertAlmostEqual(reading_mv, 6.0)


class FakeMeasurementDevice:
    def __init__(
        self,
        *,
        battery=6.2,
        offset=0.72,
        case_name="Known good",
        activation_time=None,
        deactivation_time=None,
        configure_error=None,
        error=None,
        reference_mv=100.0,
    ):
        self.battery = battery
        self.offset = offset
        self.case_name = case_name
        self.activation_time = activation_time
        self.deactivation_time = deactivation_time
        self.configure_error = configure_error
        self.error = error
        self.reference_mv = reference_mv
        self.calls: list[str] = []

    def disable_emitter_pwm(self, channel):
        del channel
        self.calls.append("pwm_off")
        return (
            time.monotonic()
            if self.deactivation_time is None
            else self.deactivation_time
        )

    def read_battery_voltage(self):
        self.calls.append("battery")
        return self.battery

    def read_offset_voltage(self, *, waveform_range_v):
        del waveform_range_v
        self.calls.append("offset")
        return self.offset

    def configure_emitter_pwm(self, **kwargs):
        del kwargs
        self.calls.append("pwm_on")
        if self.configure_error is not None:
            raise self.configure_error
        return time.monotonic() if self.activation_time is None else self.activation_time

    def read_waveform_until_stable(self, **kwargs):
        self.calls.append("capture")
        if self.error is not None:
            raise self.error
        waveform, sync, rate, _offset, analysis = prepared_capture(self.case_name)
        if kwargs.get("progress"):
            kwargs["progress"](analysis)
        if kwargs.get("preview"):
            kwargs["preview"](waveform[-500:], sync[-500:])
        return waveform, sync, rate, analysis


class MeasurementHarness:
    def __init__(self, device):
        self.device = device
        self.hardware_lock = threading.Lock()
        self.measure_token = 7
        self.last_capture_report = None
        self.last_reference_check_mv = None
        self.reference_calibration_error = None
        self.reference_calibration = app.build_reference_calibration(
            [100.0] * app.REFERENCE_CALIBRATION_READINGS
        )
        self.stability_settings = SETTINGS
        self.callback_events = []
        self.preview_count = 0
        self.reference_progress_var = SimpleNamespace(
            set=lambda value: self.callback_events.append(("reference_progress", value))
        )
        self.status_var = SimpleNamespace(
            set=lambda value: self.callback_events.append(("status_var", value))
        )

    def ensure_connected(self):
        return None

    def _fresh_battery_reading(self):
        return None

    def _capture_reference_reading(self, device, **_kwargs):
        device.calls.append("reference")
        return device.reference_mv

    def on_battery_update(self, value, error=None):
        self.callback_events.append(("battery", value, error))

    def on_offset_update(self, token, value):
        self.callback_events.append(("offset", token, value))

    def set_measure_status(self, token, text):
        self.callback_events.append(("status", token, text))

    def on_preview_frame(self, token, waveform, sync):
        self.preview_count += 1
        self.callback_events.append(("preview", token, len(waveform), len(sync)))


class HardwareWorkflowTests(unittest.TestCase):
    """Full hardware sequence WITH the reference gate.

    Production ships REFERENCE_GATE_ENABLED = False since 2026-08-24 (the
    shared dual op-amp buffer lets the DUT couple into AIN1, exactly as on
    the 405 M22 build), but the gate machinery must keep working for the
    channel-isolated buffer board, so these tests force the gate on.
    ReferenceGateDisabledTests covers the shipping default.
    """

    def setUp(self):
        patcher = mock.patch.object(app, "REFERENCE_GATE_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_hardware(self, device, *, show_live=False):
        harness = MeasurementHarness(device)
        result = app.EmitterTesterApp._hardware_measurement(
            harness,
            app.DEFAULT_FILTER_SETUP,
            app.WAVEFORM_INPUT_RANGE_V,
            app.EMITTER_PWM_CHANNEL,
            app.EMITTER_PWM_FREQUENCY_HZ,
            app.EMITTER_PWM_DUTY_CYCLE,
            show_live,
            harness.measure_token,
            lambda callback: callback(),
        )
        return harness, result

    def test_one_continuous_capture_replaces_warmup_and_preview_streams(self):
        device = FakeMeasurementDevice()
        with mock.patch.object(app.time, "sleep") as sleep:
            harness, (metrics, final, offset) = self.run_hardware(device, show_live=True)
        sleep.assert_not_called()
        self.assertEqual(
            device.calls,
            [
                "pwm_off", "pwm_on", "reference", "pwm_off",
                "offset", "pwm_on", "capture", "pwm_off",
            ],
        )
        self.assertEqual(metrics.cycles_used, 20)
        self.assertTrue(metrics.stabilized)
        self.assertTrue(final.passed)
        self.assertEqual(offset, 0.72)
        self.assertEqual(harness.preview_count, 1)
        self.assertEqual(harness.last_capture_report.data_source, "esp32_v6_1")
        self.assertEqual(harness.last_reference_check_mv, 100.0)

    def test_calibration_collects_five_ain1_readings_and_saves_average(self):
        device = FakeMeasurementDevice(reference_mv=102.5)
        harness = MeasurementHarness(device)
        with mock.patch.object(app, "save_reference_calibration") as save:
            calibration = app.EmitterTesterApp._hardware_reference_calibration(
                harness,
                harness.measure_token,
                lambda callback: callback(),
            )

        self.assertAlmostEqual(calibration.mean_mv, 102.5)
        self.assertEqual(len(calibration.readings_mv), app.REFERENCE_CALIBRATION_READINGS)
        self.assertEqual(
            device.calls,
            ["pwm_off", "pwm_on"]
            + ["reference"] * app.REFERENCE_CALIBRATION_READINGS
            + ["pwm_off"],
        )
        save.assert_called_once_with(calibration)

    def test_high_reference_with_normal_offset_invalidates_gate_after_ain0_check(self):
        device = FakeMeasurementDevice(reference_mv=126.0)
        with mock.patch.object(app, "save_reference_calibration") as save:
            with self.assertRaisesRegex(
                app.ReferenceCheckFailedError,
                "AIN0 was checked at 0.720 V",
            ):
                self.run_hardware(device)

        self.assertEqual(
            device.calls,
            ["pwm_off", "pwm_on", "reference", "pwm_off", "offset"],
        )
        self.assertFalse(save.call_args.args[0].valid)

    def test_high_offset_dut_suppresses_high_reference_invalidation(self):
        device = FakeMeasurementDevice(reference_mv=150.0, offset=1.3)
        with mock.patch.object(app, "save_reference_calibration") as save:
            harness, (_metrics, final, offset) = self.run_hardware(device)

        save.assert_not_called()
        self.assertTrue(harness.reference_calibration.valid)
        self.assertEqual(harness.last_reference_check_mv, 150.0)
        self.assertEqual(offset, 1.3)
        self.assertFalse(final.passed)
        self.assertEqual(app.suggest_failure_mode(final), "HO - High offset")
        self.assertEqual(
            device.calls,
            [
                "pwm_off", "pwm_on", "reference", "pwm_off",
                "offset", "pwm_on", "capture", "pwm_off",
            ],
        )

    def test_low_reference_with_high_offset_still_invalidates_gate(self):
        device = FakeMeasurementDevice(reference_mv=74.0, offset=1.3)
        with mock.patch.object(app, "save_reference_calibration") as save:
            with self.assertRaises(app.ReferenceCheckFailedError):
                self.run_hardware(device)

        self.assertFalse(save.call_args.args[0].valid)
        self.assertEqual(
            device.calls,
            ["pwm_off", "pwm_on", "reference", "pwm_off", "offset"],
        )

    def test_missing_reference_calibration_blocks_without_hardware_access(self):
        device = FakeMeasurementDevice()
        harness = MeasurementHarness(device)
        harness.reference_calibration = None
        with self.assertRaisesRegex(app.ReferenceGateError, "sensor was not read"):
            app.EmitterTesterApp._hardware_measurement(
                harness,
                app.DEFAULT_FILTER_SETUP,
                app.WAVEFORM_INPUT_RANGE_V,
                app.EMITTER_PWM_CHANNEL,
                app.EMITTER_PWM_FREQUENCY_HZ,
                app.EMITTER_PWM_DUTY_CYCLE,
                False,
                harness.measure_token,
                lambda callback: callback(),
            )
        self.assertEqual(device.calls, [])

    def test_live_preview_setting_does_not_select_another_capture_path(self):
        hidden_device = FakeMeasurementDevice()
        visible_device = FakeMeasurementDevice()
        hidden_harness, (hidden_metrics, _hidden_final, _offset) = self.run_hardware(
            hidden_device,
            show_live=False,
        )
        visible_harness, (visible_metrics, _visible_final, _offset) = self.run_hardware(
            visible_device,
            show_live=True,
        )

        self.assertEqual(hidden_device.calls, visible_device.calls)
        self.assertEqual(hidden_metrics.cycles_used, visible_metrics.cycles_used)
        self.assertEqual(hidden_harness.preview_count, 1)
        self.assertEqual(visible_harness.preview_count, 1)

    def test_reported_pwm_duration_uses_activation_to_deactivation_clock(self):
        device = FakeMeasurementDevice(
            activation_time=100.0,
            deactivation_time=121.375,
        )
        harness, _result = self.run_hardware(device)
        self.assertAlmostEqual(harness.last_capture_report.pwm_on_seconds, 21.375)

    def test_timeout_is_saved_as_unstable_without_signal_metrics(self):
        harness, (metrics, final, _offset) = self.run_hardware(
            FakeMeasurementDevice(case_name="Never stabilizes")
        )
        self.assertFalse(final.passed)
        self.assertIsNone(final.sensitivity_mv)
        self.assertEqual(final.polarity, "")
        self.assertFalse(metrics.stabilized)
        self.assertTrue(harness.last_capture_report.timed_out)
        self.assertIn(
            "could not complete 10 consecutive stable deltas within 20.0 s",
            final.fail_reasons[-1].lower(),
        )

    def test_battery_is_never_read_and_implausible_offset_blocks_before_pwm(self):
        # Battery monitoring is disabled on the unified-rig fixture (nothing
        # measurable on AIN7): even a device reporting a dead battery is never
        # asked for it and the measurement proceeds normally.
        self.assertFalse(app.BATTERY_MONITORING_ENABLED)
        low = FakeMeasurementDevice(battery=5.7)
        _harness, (_metrics, final, _offset) = self.run_hardware(low)
        self.assertNotIn("battery", low.calls)
        self.assertTrue(final.passed)

        missing = FakeMeasurementDevice(offset=3.0)
        with self.assertRaises(app.HardwareNotReadyError):
            self.run_hardware(missing)
        self.assertEqual(
            missing.calls,
            ["pwm_off", "pwm_on", "reference", "pwm_off", "offset"],
        )

    def test_capture_exception_still_turns_pwm_off(self):
        device = FakeMeasurementDevice(error=RuntimeError("serial lost"))
        with self.assertRaisesRegex(RuntimeError, "serial lost"):
            self.run_hardware(device)
        self.assertEqual(
            device.calls,
            [
                "pwm_off", "pwm_on", "reference", "pwm_off",
                "offset", "pwm_on", "capture", "pwm_off",
            ],
        )

    def test_pwm_activation_error_still_turns_pwm_off(self):
        device = FakeMeasurementDevice(configure_error=RuntimeError("PWM acknowledgement lost"))
        with self.assertRaisesRegex(RuntimeError, "PWM acknowledgement lost"):
            self.run_hardware(device)
        self.assertEqual(
            device.calls,
            ["pwm_off", "pwm_on", "pwm_off"],
        )

    def test_app_close_waits_for_capture_lock_before_serial_shutdown(self):
        events = []
        lock = threading.Lock()

        class ClosingDevice:
            def disable_emitter_pwm(self, _channel):
                self.assert_locked = lock.locked()
                events.append("pwm_off")

            def close(self):
                events.append("close")

        harness = SimpleNamespace(
            measure_token=4,
            animator=SimpleNamespace(cancel_all=lambda: events.append("cancel")),
            hardware_lock=lock,
            device=ClosingDevice(),
            destroy=lambda: events.append("destroy"),
        )
        lock.acquire()
        closing = threading.Thread(
            target=app.EmitterTesterApp.on_close,
            args=(harness,),
        )
        closing.start()
        time.sleep(0.02)
        self.assertTrue(closing.is_alive())
        self.assertEqual(events, ["cancel"])
        lock.release()
        closing.join(timeout=1.0)

        self.assertFalse(closing.is_alive())
        self.assertTrue(harness.device.assert_locked)
        self.assertEqual(events, ["cancel", "pwm_off", "close", "destroy"])
        self.assertEqual(harness.measure_token, 5)


class ReferenceGateDisabledTests(unittest.TestCase):
    """Shipping default: REFERENCE_GATE_ENABLED = False (op-amp crosstalk)."""

    def run_hardware(self, device):
        harness = MeasurementHarness(device)
        result = app.EmitterTesterApp._hardware_measurement(
            harness,
            app.DEFAULT_FILTER_SETUP,
            app.WAVEFORM_INPUT_RANGE_V,
            app.EMITTER_PWM_CHANNEL,
            app.EMITTER_PWM_FREQUENCY_HZ,
            app.EMITTER_PWM_DUTY_CYCLE,
            False,
            harness.measure_token,
            lambda callback: callback(),
        )
        return harness, result

    def test_gate_is_off_and_never_reads_ain1(self):
        self.assertFalse(app.REFERENCE_GATE_ENABLED)
        device = FakeMeasurementDevice()
        harness = MeasurementHarness(device)
        harness.reference_calibration = None  # no calibration needed at all
        with mock.patch.object(app.time, "sleep"):
            harness, (metrics, final, offset) = self.run_hardware(device)
        self.assertNotIn("reference", device.calls)
        self.assertEqual(device.calls[:3], ["pwm_off", "offset", "pwm_on"])
        self.assertTrue(final.passed)
        self.assertIsNone(harness.last_reference_check_mv)
        self.assertEqual(offset, 0.72)

    def test_gate_ready_without_calibration(self):
        harness = SimpleNamespace(
            simulator_var=SimpleNamespace(get=lambda: False),
            reference_calibration=None,
        )
        self.assertTrue(app.EmitterTesterApp.reference_gate_ready(harness))


class NoSensorPromptTests(unittest.TestCase):
    """A floating AIN0 asks whether a sensor is loaded; yes = bad part."""

    def test_floating_ain0_raises_the_askable_error(self):
        with mock.patch.object(app, "REFERENCE_GATE_ENABLED", False):
            device = FakeMeasurementDevice(offset=0.0)
            harness = MeasurementHarness(device)
            with self.assertRaises(app.NoSensorDetectedError) as ctx:
                app.EmitterTesterApp._hardware_measurement(
                    harness, app.DEFAULT_FILTER_SETUP, app.WAVEFORM_INPUT_RANGE_V,
                    app.EMITTER_PWM_CHANNEL, app.EMITTER_PWM_FREQUENCY_HZ,
                    app.EMITTER_PWM_DUTY_CYCLE, False, harness.measure_token,
                    lambda callback: callback(),
                )
        self.assertIsInstance(ctx.exception, app.HardwareNotReadyError)
        self.assertEqual(ctx.exception.offset_v, 0.0)

    def test_bad_sensor_result_fails_with_no_offset_and_no_readings(self):
        metrics, final = app.build_no_output_sensor_result(0.012, input_range_v=app.WAVEFORM_INPUT_RANGE_V)
        self.assertFalse(final.passed)
        self.assertEqual(app.result_outcome(final), app.OUTCOME_FAIL)
        self.assertAlmostEqual(final.offset_v, 0.012)
        self.assertIsNone(final.sensitivity_mv)
        self.assertIn("No offset", final.fail_reasons[0])
        self.assertEqual(metrics.cycles_used, 0)
        self.assertIn(app.BAD_SENSOR_FAILURE_MODE, app.FAILURE_MODE_CHOICES)

    def _harness(self, answer):
        events = []
        h = SimpleNamespace(
            measure_token=3, measuring=True, busy=True, current_sensor_id="B1-4",
            last_metrics=None, last_result=None, last_offset_initial_v=None,
            preview_waveform=None, preview_sync=None,
            failure_mode_var=SimpleNamespace(set=lambda v: events.append(("mode", v))),
            status_var=SimpleNamespace(set=lambda v: events.append(("status", v))),
            measure_status_var=SimpleNamespace(set=lambda v: events.append(("mstatus", v))),
            write_autosave=lambda stage: events.append(("autosave", stage)),
            render_step=lambda: events.append(("render",)),
            _log_attempt=lambda event, **kw: events.append(("log", event)),
            _confirm_sensor_loaded=lambda exc: answer,
            events=events,
        )
        h.record_bad_sensor = lambda v: app.EmitterTesterApp.record_bad_sensor(h, v)
        return h

    def test_yes_records_a_failed_sensor_instead_of_a_wiring_error(self):
        h = self._harness(True)
        exc = app.NoSensorDetectedError("No sensor detected", 0.01)
        with mock.patch.object(app.messagebox, "showwarning") as warn:
            app.EmitterTesterApp.on_hardware_not_ready(h, 3, exc)
        warn.assert_not_called()
        self.assertFalse(h.measuring); self.assertFalse(h.busy)
        self.assertIsNotNone(h.last_result)
        self.assertFalse(h.last_result.passed)
        self.assertIn(("mode", app.BAD_SENSOR_FAILURE_MODE), h.events)
        self.assertIn(("log", app.attempt_history.EVENT_MEASURED), h.events)
        self.assertIn(("render",), h.events)

    def test_no_keeps_the_wiring_warning(self):
        h = self._harness(False)
        exc = app.NoSensorDetectedError("No sensor detected", 0.01)
        with mock.patch.object(app.messagebox, "showwarning") as warn:
            app.EmitterTesterApp.on_hardware_not_ready(h, 3, exc)
        warn.assert_called_once()
        self.assertIsNone(h.last_result)


class SimulatorAndGuiTests(unittest.TestCase):
    def test_live_toggle_rerenders_an_active_measurement(self):
        renders = []
        harness = SimpleNamespace(
            measuring=True,
            step="load",
            RESULT_STEP="result",
            render_step=lambda: renders.append("render"),
        )
        app.EmitterTesterApp.toggle_live_view(harness)
        self.assertEqual(renders, ["render"])

    def test_result_details_toggle_rerenders_finished_verdict(self):
        renders = []
        harness = SimpleNamespace(
            measuring=False,
            step="result",
            RESULT_STEP="result",
            render_step=lambda: renders.append("render"),
        )

        app.EmitterTesterApp.toggle_result_details(harness)

        self.assertEqual(renders, ["render"])

    def test_simulator_good_part_stabilizes_and_never_stable_case_times_out(self):
        harness = MeasurementHarness(FakeMeasurementDevice())
        metrics, final, _offset = app.EmitterTesterApp._simulate_measurement(
            harness,
            app.DEFAULT_FILTER_SETUP,
            "Known good",
            False,
            app.WAVEFORM_INPUT_RANGE_V,
            False,
            harness.measure_token,
            lambda callback: callback(),
        )
        self.assertTrue(metrics.stabilized)
        self.assertTrue(final.passed)
        self.assertGreater(harness.last_capture_report.stabilization_seconds, 9.0)
        self.assertEqual(harness.last_capture_report.measurement_cycles, 20)

        timeout_harness = MeasurementHarness(FakeMeasurementDevice())
        _metrics, timeout, _offset = app.EmitterTesterApp._simulate_measurement(
            timeout_harness,
            app.DEFAULT_FILTER_SETUP,
            "Never stabilizes",
            False,
            app.WAVEFORM_INPUT_RANGE_V,
            False,
            timeout_harness.measure_token,
            lambda callback: callback(),
        )
        self.assertFalse(timeout.passed)
        self.assertIsNone(timeout.sensitivity_mv)
        self.assertTrue(timeout_harness.last_capture_report.timed_out)

    def test_simulator_exercises_sensitivity_gate_and_wrong_polarity(self):
        expected_outcomes = {
            "Low sensitivity": app.OUTCOME_FAIL,
            "Borderline sensitivity": app.OUTCOME_PASS,
            "Wrong polarity": app.OUTCOME_FAIL,
        }
        for case_name, expected_outcome in expected_outcomes.items():
            with self.subTest(case_name=case_name):
                harness = MeasurementHarness(FakeMeasurementDevice())
                metrics, final, _offset = app.EmitterTesterApp._simulate_measurement(
                    harness,
                    app.DEFAULT_FILTER_SETUP,
                    case_name,
                    False,
                    app.WAVEFORM_INPUT_RANGE_V,
                    False,
                    harness.measure_token,
                    lambda callback: callback(),
                )
                self.assertTrue(metrics.stabilized)
                self.assertTrue(harness.last_capture_report.stabilized)
                self.assertFalse(harness.last_capture_report.timed_out)
                self.assertEqual(harness.last_capture_report.measurement_cycles, 20)
                self.assertIsNotNone(final.sensitivity_mv)
                self.assertTrue(final.polarity)
                self.assertEqual(app.result_outcome(final), expected_outcome)

    @unittest.skipUnless(os.environ.get("DISPLAY"), "requires an X11 display")
    def test_gui_title_and_config_load(self):
        root = None
        with mock.patch.object(app.EmitterTesterApp, "startup_probe", lambda self: None):
            try:
                root = app.EmitterTesterApp()
                root.withdraw()
                root.update_idletasks()
                self.assertEqual(root.title(), "Eltec 406MCA ESP32 Sensor Tester v6.1")
                self.assertIsNone(root.stability_config_error)
                self.assertIsNotNone(root.stability_settings)
                self.assertFalse(root.show_details_var.get())
            finally:
                if root is not None:
                    root.destroy()

    @unittest.skipUnless(os.environ.get("DISPLAY"), "requires an X11 display")
    def test_home_uses_simple_reference_wording_and_result_hides_metrics_by_default(self):
        root = None
        calibration = app.build_reference_calibration([5.0] * 5)

        def label_texts(widget):
            texts = []
            for child in widget.winfo_children():
                if isinstance(child, (tk.Label, ttk.Label)):
                    texts.append(str(child.cget("text")))
                texts.extend(label_texts(child))
            return texts

        def comboboxes(widget):
            found = []
            for child in widget.winfo_children():
                if isinstance(child, ttk.Combobox):
                    found.append(child)
                found.extend(comboboxes(child))
            return found

        with mock.patch.object(app, "load_reference_calibration", return_value=calibration), mock.patch.object(
            app.EmitterTesterApp, "startup_probe", lambda self: None
        ):
            try:
                root = app.EmitterTesterApp()
                root.withdraw()
                root.update_idletasks()
                setup_labels = label_texts(root.step_frame)
                self.assertIn(
                    "Reference unit calibrated"
                    if app.REFERENCE_GATE_ENABLED
                    else "Reference gate disabled (op-amp crosstalk)",
                    setup_labels,
                )
                self.assertFalse(any("AIN1 reference calibrated" in text for text in setup_labels))
                self.assertFalse(any("5.00 mV" in text for text in setup_labels))

                root.batch_number = "B1"
                root.current_sensor_id = "B1-1"
                root.result_saved = False
                root.last_result = app.FinalResult(
                    passed=True,
                    offset_v=0.7,
                    sensitivity_mv=10.0,
                    polarity=app.POSITIVE_POLARITY,
                    fail_reasons=[],
                    warnings=[],
                    waveform_metrics=None,
                )
                root.step = root.RESULT_STEP
                root.show_details_var.set(False)
                root.render_step()
                hidden_labels = label_texts(root.step_frame)
                self.assertNotIn("OFFSET", hidden_labels)
                self.assertNotIn("SENSITIVITY", hidden_labels)
                self.assertNotIn("POLARITY", hidden_labels)

                root.show_details_var.set(True)
                root.render_step()
                shown_labels = label_texts(root.step_frame)
                self.assertIn("OFFSET", shown_labels)
                self.assertIn("SENSITIVITY", shown_labels)
                self.assertIn("POLARITY", shown_labels)

                root.last_result = app.FinalResult(
                    passed=False,
                    offset_v=0.7,
                    sensitivity_mv=None,
                    polarity="",
                    fail_reasons=["Unstable: waveform peak did not stabilize."],
                    warnings=[],
                    waveform_metrics=None,
                )
                root.failure_mode_var.set(app.suggest_failure_mode(root.last_result))
                root.show_details_var.set(False)
                root.render_step()
                failed_labels = label_texts(root.step_frame)
                failure_combos = comboboxes(root.step_frame)
                self.assertIn("FAILURE MODE", failed_labels)
                self.assertEqual(root.failure_mode_var.get(), app.UNSTABLE_FAILURE_MODE)
                self.assertTrue(
                    any(app.UNSTABLE_FAILURE_MODE in combo.cget("values") for combo in failure_combos)
                )
            finally:
                if root is not None:
                    root.destroy()

    @unittest.skipUnless(os.environ.get("DISPLAY"), "requires an X11 display")
    def test_missing_settings_keep_gui_open_but_block_measurement(self):
        root = None
        missing = Path("/definitely/missing/v6-stability-settings.json")
        with mock.patch.object(app, "DEFAULT_SETTINGS_PATH", missing), mock.patch.object(
            app.EmitterTesterApp, "startup_probe", lambda self: None
        ), mock.patch.object(app.messagebox, "showerror") as showerror:
            try:
                root = app.EmitterTesterApp()
                root.withdraw()
                self.assertIsNotNone(root.stability_config_error)
                root.run_measurement()
                self.assertFalse(root.measuring)
                self.assertFalse(root.busy)
                showerror.assert_called_once()
            finally:
                if root is not None:
                    root.destroy()



class FooterFitTests(unittest.TestCase):
    """The action bar must never run past the edge of the window.

    Regression: with the v2.0 six-button footer at full size, "Save + Next
    Sensor (Enter)" needed more width than the content column has once the
    window was maximized, and was clipped off the right edge.
    """

    class FakeButton:
        def __init__(self, text):
            self._text = text
            self._footer_full_text = text

    def test_label_tiers_shorten_in_the_documented_order(self):
        label = app.EmitterTesterApp._footer_label
        save = self.FakeButton("Save + Next Sensor (Enter)")
        self.assertEqual(label(save, "full"), "Save + Next Sensor (Enter)")
        self.assertEqual(label(save, "nohint"), "Save + Next Sensor")
        self.assertEqual(label(save, "short"), "Save + Next")
        exit_batch = self.FakeButton("Save + Exit Batch (Esc)")
        self.assertEqual(label(exit_batch, "nohint"), "Save + Exit Batch")
        self.assertEqual(label(exit_batch, "short"), "Save + Exit")
        # The skipped counter has to survive the compact wording.
        skipped = self.FakeButton("Measure skipped (3)")
        self.assertEqual(label(skipped, "short"), "Skipped (3)")
        # A label with no hint and no compact form is left alone at every tier.
        plain = self.FakeButton("Skip part")
        for tier in ("full", "nohint", "short"):
            self.assertEqual(label(plain, tier), "Skip part")

    def test_variant_ladder_keeps_the_buttons_big_before_shrinking(self):
        variants = app.EmitterTesterApp.FOOTER_VARIANTS
        self.assertEqual(variants[0], ("xl", "lg", "full", 1))
        # Wrapping onto a second row at full size beats shrinking the buttons:
        # the technician asked for big, readable controls.
        first_wrap = next(i for i, v in enumerate(variants) if v[3] == 2)
        first_shrink = next(i for i, v in enumerate(variants) if v[0] != "xl")
        self.assertLess(first_wrap, first_shrink)
        # Labels only ever get shorter as the bar gets tighter.
        rank = {"full": 0, "nohint": 1, "short": 2}
        tiers = [rank[v[2]] for v in variants]
        self.assertEqual(tiers, sorted(tiers))

    # ----- live layout ----- #
    def _footer_overflow(self, root):
        """Pixels by which the worst-placed visible button overruns the bar."""
        bar = root.footer_bar
        worst = 0
        for group in (root.footer_left, root.footer_right):
            for button in group.winfo_children():
                if not button.winfo_manager():  # grid_remove()d
                    continue
                right = (button.winfo_rootx() - bar.winfo_rootx()) + button.winfo_reqwidth()
                worst = max(worst, right - bar.winfo_width())
        return worst

    def _drive_to_result(self, root):
        """Reach a result step with an unsaved verdict (all six buttons)."""
        root.batch_var.set("FIT")
        root.tester_var.set("tester")
        root.start_batch()
        root.show_step(root.RESULT_STEP)
        final = app.FinalResult(
            passed=True, fail_reasons=[], warnings=[], offset_v=1.2,
            sensitivity_mv=40.0, polarity="POSITIVE",
        )
        metrics = SimpleNamespace(
            waveform_v=np.zeros(8), sync_v=np.zeros(8),
            noise_rms_mv=None, signal_to_noise_db=None,
        )
        root.measuring = True
        root.busy = True
        root.on_measure_done(root.measure_token, metrics, final)
        root.update_idletasks()

    def _tester(self):
        results = tempfile.TemporaryDirectory()
        self.addCleanup(results.cleanup)
        patches = [
            mock.patch.object(app, "results_root_dir", lambda: Path(results.name)),
            mock.patch.object(app.EmitterTesterApp, "startup_probe", lambda self: None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            root = app.EmitterTesterApp()
        except Exception as exc:  # headless host without Tk
            self.skipTest(f"Tk unavailable: {exc}")

        def close():
            root.measure_token += 1
            root.animator.cancel_all()
            root.destroy()

        self.addCleanup(close)
        # Withdrawn windows still report real geometry once idle tasks run, so
        # the layout can be measured without flashing a window on the rig PC.
        root.withdraw()
        root.state("normal")
        return root

    def test_no_footer_button_is_clipped_at_any_window_width(self):
        root = self._tester()
        self._drive_to_result(root)
        narrow_variant = None
        for width in (1920, 1600, 1440, 1366, 1280):
            root.geometry(f"{width}x820")
            root.update_idletasks()
            root.update_navigation_state()
            root.update_idletasks()
            with self.subTest(width=width):
                self.assertLessEqual(self._footer_overflow(root), 0)
            narrow_variant = root._footer_fit
        # ...and the bar recovers its full size when the window grows again
        # (the tester maximizes itself after the first render).
        root.geometry("1920x820")
        root.update_idletasks()
        root.update_navigation_state()
        root.update_idletasks()
        variants = app.EmitterTesterApp.FOOTER_VARIANTS
        self.assertLessEqual(
            variants.index(root._footer_fit), variants.index(narrow_variant)
        )
        self.assertLessEqual(self._footer_overflow(root), 0)

    def test_every_step_fits_and_hidden_buttons_are_not_measured(self):
        root = self._tester()
        root.geometry("1366x820")
        root.update_idletasks()
        with self.subTest(step="setup"):
            self.assertLessEqual(self._footer_overflow(root), 0)
            # Only Back + the primary action exist on the setup step.
            self.assertFalse(root.skip_button.winfo_manager())
            self.assertFalse(root.remeasure_button.winfo_manager())
        self._drive_to_result(root)
        for step in ("load", "result"):
            root.show_step(root.LOAD_STEP if step == "load" else root.RESULT_STEP)
            root.update_idletasks()
            with self.subTest(step=step):
                self.assertLessEqual(self._footer_overflow(root), 0)


if __name__ == "__main__":
    unittest.main()
