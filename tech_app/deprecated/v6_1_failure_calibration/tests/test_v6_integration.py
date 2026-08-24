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


CALIBRATION_DIR = Path(__file__).resolve().parents[1]
if str(CALIBRATION_DIR) not in sys.path:
    sys.path.insert(0, str(CALIBRATION_DIR))

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
    def test_failure_calibration_identity_and_result_namespace_are_isolated(self):
        self.assertEqual(
            app.results_root_dir().name,
            "v6_1_failure_calibration",
        )
        self.assertNotIn("capture_mode", app.CSV_FIELDS)
        self.assertNotIn("fast_match", app.CSV_FIELDS)
        self.assertIn("stabilization_seconds", app.CSV_FIELDS)
        self.assertIn("pwm_on_seconds", app.CSV_FIELDS)
        self.assertIn("reference_calibration_mv", app.CSV_FIELDS)
        self.assertIn("reference_check_mv", app.CSV_FIELDS)
        self.assertIn("reference_drift_pct", app.CSV_FIELDS)
        self.assertIn("failure_mode_tag", app.CSV_FIELDS)
        self.assertIn("failure_mode_reason", app.CSV_FIELDS)
        self.assertIn("measurement_attempt", app.CSV_FIELDS)
        self.assertIn("measurement_failures", app.CSV_FIELDS)
        self.assertIn("active_stability_required_deltas", app.CSV_FIELDS)
        self.assertIn("active_measurement_cycles_required", app.CSV_FIELDS)
        self.assertIn(app.UNSTABLE_FAILURE_MODE, app.FAILURE_MODE_CHOICES)
        self.assertEqual(app.MAX_MEASUREMENT_ATTEMPTS, 3)
        self.assertEqual(app.DUT_STABILITY_CONFIRMATION_DELTAS, 10)
        self.assertEqual(app.SENSITIVITY_MEASUREMENT_CYCLES, 20)
        self.assertEqual(SETTINGS.peak_delta_threshold_mv, 0.100)
        self.assertEqual(SETTINGS.consecutive_deltas_required, 5)

    def test_launcher_installation_uses_only_failure_calibration_identities(self):
        installer = CALIBRATION_DIR / "install_xubuntu_launcher.sh"
        run_script = CALIBRATION_DIR / "run_eltec_406mca_esp32_tester.sh"
        self.assertIn(
            "eltec-406mca-failure-cal-v6-1",
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
            calibration_menu = (
                applications / "com.eltec.406mca-failure-cal-v6-1.desktop"
            )
            calibration_desktop = (
                desktop / "Eltec 406MCA Failure Calibration — v6.1 Base.desktop"
            )
            self.assertTrue(calibration_menu.exists())
            self.assertTrue(calibration_desktop.exists())
            self.assertIn(
                "Name=Eltec 406MCA Failure Calibration — v6.1 Base",
                calibration_menu.read_text(encoding="utf-8"),
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
            self.assertFalse(calibration_menu.exists())
            self.assertFalse(calibration_desktop.exists())
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
        self.assertEqual(
            analysis.report.data_source,
            "esp32_v6_1_failure_calibration",
        )
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

    def test_missing_local_calibration_reads_compatible_baseline_without_copying_it(self):
        calibration = app.build_reference_calibration([100.0] * 5)
        with tempfile.TemporaryDirectory() as temp_dir:
            local = Path(temp_dir) / "v6_1" / "reference.json"
            legacy = Path(temp_dir) / "v6" / "reference.json"
            app.save_reference_calibration(calibration, legacy)
            missing_v6_1 = Path(temp_dir) / "v6_1_missing" / "reference.json"
            with mock.patch.object(
                app,
                "reference_calibration_path",
                return_value=local,
            ), mock.patch.object(
                app,
                "v6_1_reference_calibration_path",
                return_value=missing_v6_1,
            ), mock.patch.object(
                app,
                "v6_reference_calibration_path",
                return_value=legacy,
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
    def run_hardware(
        self,
        device,
        *,
        show_live=False,
        offset_plausibility_override=False,
    ):
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
            offset_plausibility_override=offset_plausibility_override,
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
                "pwm_off", "battery", "pwm_on", "reference", "pwm_off",
                "offset", "pwm_on", "capture", "pwm_off",
            ],
        )
        self.assertEqual(metrics.cycles_used, 20)
        self.assertTrue(metrics.stabilized)
        self.assertTrue(final.passed)
        self.assertEqual(offset, 0.72)
        self.assertEqual(harness.preview_count, 1)
        self.assertEqual(
            harness.last_capture_report.data_source,
            "esp32_v6_1_failure_calibration",
        )
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
            ["pwm_off", "battery", "pwm_on"]
            + ["reference"] * app.REFERENCE_CALIBRATION_READINGS
            + ["pwm_off"],
        )
        save.assert_called_once_with(calibration)

    def test_out_of_window_reference_invalidates_gate_before_ain0(self):
        device = FakeMeasurementDevice(reference_mv=126.0)
        with mock.patch.object(app, "save_reference_calibration") as save:
            with self.assertRaisesRegex(app.ReferenceCheckFailedError, "sensor under test was not read"):
                self.run_hardware(device)

        self.assertEqual(
            device.calls,
            ["pwm_off", "battery", "pwm_on", "reference", "pwm_off"],
        )
        self.assertFalse(save.call_args.args[0].valid)

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

    def test_low_battery_and_implausible_offset_block_before_pwm(self):
        low = FakeMeasurementDevice(battery=5.7)
        with self.assertRaises(app.BatteryTooLowError):
            self.run_hardware(low)
        self.assertEqual(low.calls, ["pwm_off", "battery"])

        missing = FakeMeasurementDevice(offset=3.0)
        with self.assertRaises(app.HardwareNotReadyError):
            self.run_hardware(missing)
        self.assertEqual(
            missing.calls,
            ["pwm_off", "battery", "pwm_on", "reference", "pwm_off", "offset"],
        )

    def test_implausible_offset_override_is_explicit_and_recorded(self):
        blocked = FakeMeasurementDevice(offset=3.0)
        with self.assertRaises(app.HardwareNotReadyError):
            self.run_hardware(blocked)
        self.assertNotIn("capture", blocked.calls)

        overridden = FakeMeasurementDevice(offset=3.0)
        harness, (metrics, final, offset) = self.run_hardware(
            overridden,
            offset_plausibility_override=True,
        )

        self.assertIn("capture", overridden.calls)
        self.assertTrue(harness.offset_plausibility_override_applied)
        self.assertEqual(offset, 3.0)
        self.assertEqual(metrics.offset_v, 3.0)
        self.assertFalse(final.passed)
        self.assertTrue(
            any("Offset out of range" in reason for reason in final.fail_reasons)
        )
        self.assertTrue(
            any(
                event[0] == "status" and "Diagnostic override recorded" in event[2]
                for event in harness.callback_events
            )
        )

    def test_capture_exception_still_turns_pwm_off(self):
        device = FakeMeasurementDevice(error=RuntimeError("serial lost"))
        with self.assertRaisesRegex(RuntimeError, "serial lost"):
            self.run_hardware(device)
        self.assertEqual(
            device.calls,
            [
                "pwm_off", "battery", "pwm_on", "reference", "pwm_off",
                "offset", "pwm_on", "capture", "pwm_off",
            ],
        )

    def test_pwm_activation_error_still_turns_pwm_off(self):
        device = FakeMeasurementDevice(configure_error=RuntimeError("PWM acknowledgement lost"))
        with self.assertRaisesRegex(RuntimeError, "PWM acknowledgement lost"):
            self.run_hardware(device)
        self.assertEqual(
            device.calls,
            ["pwm_off", "battery", "pwm_on", "pwm_off"],
        )

    def test_app_close_waits_for_capture_lock_before_serial_shutdown(self):
        events = []
        scheduled = []
        lock = threading.Lock()

        class ClosingDevice:
            def disable_emitter_pwm(self, _channel):
                self.assert_locked = lock.locked()
                events.append("pwm_off")

            def close(self):
                events.append("close")

        harness = SimpleNamespace(
            closing=False,
            measure_token=4,
            animator=SimpleNamespace(cancel_all=lambda: events.append("cancel")),
            status_var=SimpleNamespace(set=lambda value: events.append(("status", value))),
            evidence_saving=True,
            hardware_lock=lock,
            device=ClosingDevice(),
            last_result=None,
            result_saved=True,
            after=lambda delay, callback: scheduled.append((delay, callback)),
            destroy=lambda: events.append("destroy"),
        )
        harness._finish_close_when_hardware_idle = lambda: (
            app.EmitterTesterApp._finish_close_when_hardware_idle(harness)
        )
        lock.acquire()
        app.EmitterTesterApp.on_close(harness)

        self.assertTrue(harness.closing)
        self.assertEqual(harness.measure_token, 5)
        self.assertEqual(events[0], "cancel")
        self.assertEqual(scheduled[0][0], 50)
        self.assertNotIn("pwm_off", events)

        harness.evidence_saving = False
        scheduled.pop(0)[1]()
        self.assertEqual(scheduled[0][0], 50)
        self.assertNotIn("pwm_off", events)

        lock.release()
        scheduled.pop(0)[1]()

        self.assertTrue(harness.device.assert_locked)
        self.assertEqual(events[-3:], ["pwm_off", "close", "destroy"])
        self.assertEqual(scheduled, [])

    def test_close_refuses_to_lose_unsaved_review_or_incomplete_evidence(self):
        events = []
        initial = SimpleNamespace(
            closing=False,
            last_result=object(),
            result_saved=False,
            autosave_error="disk full",
            write_autosave=lambda _stage: False,
        )
        with mock.patch.object(app.messagebox, "showerror") as showerror:
            app.EmitterTesterApp.on_close(initial)
        self.assertFalse(initial.closing)
        showerror.assert_called_once()

        scheduled = []
        evidence = SimpleNamespace(
            closing=True,
            evidence_saving=False,
            last_result=object(),
            result_saved=False,
            last_run_evidence_error="artifact write failed",
            _ensure_current_run_evidence=lambda: False,
            after=lambda delay, callback: scheduled.append((delay, callback)),
            _drain_ui_queue=lambda: None,
            hardware_lock=threading.Lock(),
        )
        with mock.patch.object(app.messagebox, "showerror") as showerror:
            app.EmitterTesterApp._finish_close_when_hardware_idle(evidence)
        self.assertFalse(evidence.closing)
        self.assertEqual(scheduled[0][0], 20)
        showerror.assert_called_once()


class SimulatorAndGuiTests(unittest.TestCase):
    def test_worker_callbacks_are_queued_until_the_ui_poller_runs(self):
        import queue

        events = []
        scheduled = []
        harness = SimpleNamespace(
            closing=False,
            ui_queue=queue.Queue(),
            after=lambda delay, callback: scheduled.append((delay, callback)),
        )
        harness._drain_ui_queue = lambda: app.EmitterTesterApp._drain_ui_queue(
            harness
        )
        callback = lambda: events.append("callback")

        app.EmitterTesterApp._post(harness, callback)
        self.assertEqual(events, [])
        self.assertEqual(harness.ui_queue.qsize(), 1)

        app.EmitterTesterApp._drain_ui_queue(harness)
        self.assertEqual(events, ["callback"])
        self.assertEqual(scheduled[0][0], 20)

        harness.status_var = SimpleNamespace(set=lambda value: events.append(value))
        harness.ui_queue.put(lambda: (_ for _ in ()).throw(ValueError("bad callback")))
        harness.ui_queue.put(lambda: events.append("callback after error"))
        app.EmitterTesterApp._drain_ui_queue(harness)
        self.assertIn("Internal completion callback failed: bad callback", events)
        self.assertIn("callback after error", events)
        self.assertEqual(scheduled[-1][0], 20)

        harness.closing = True
        app.EmitterTesterApp._post(harness, lambda: events.append("late callback"))
        app.EmitterTesterApp._drain_ui_queue(harness)
        self.assertNotIn("late callback", events)
        self.assertTrue(harness.ui_queue.empty())

    def test_simulator_mode_is_frozen_for_an_active_batch(self):
        harness = SimpleNamespace(
            batch_active=False,
            batch_is_synthetic=False,
            simulator_var=SimpleNamespace(get=lambda: True),
        )
        self.assertTrue(app.EmitterTesterApp.simulator_mode(harness))

        harness.batch_active = True
        self.assertFalse(app.EmitterTesterApp.simulator_mode(harness))

        harness.batch_is_synthetic = True
        harness.simulator_var = SimpleNamespace(get=lambda: False)
        self.assertTrue(app.EmitterTesterApp.simulator_mode(harness))

    def test_stale_or_synthetic_battery_reading_cannot_authorize_hardware(self):
        harness = SimpleNamespace(
            batch_active=True,
            batch_is_synthetic=False,
            simulator_var=SimpleNamespace(get=lambda: False),
            battery_request_generation=2,
            battery_checking=True,
            battery_v=None,
            battery_state="unknown",
            battery_read_time=None,
            battery_read_is_synthetic=None,
        )
        harness.simulator_mode = lambda: app.EmitterTesterApp.simulator_mode(harness)
        app.EmitterTesterApp.on_battery_update(
            harness,
            app.SIM_BATTERY_OK_V,
            request_generation=1,
            source_is_synthetic=True,
        )
        self.assertIsNone(harness.battery_v)

        harness.battery_v = app.SIM_BATTERY_OK_V
        harness.battery_state = "ok"
        harness.battery_read_time = time.monotonic()
        harness.battery_read_is_synthetic = True
        self.assertIsNone(app.EmitterTesterApp._fresh_battery_reading(harness))

    def test_run_measurement_blocks_replacing_an_unsaved_run(self):
        prior_result = app.FinalResult(
            passed=False,
            offset_v=0.7,
            sensitivity_mv=1.0,
            polarity=app.POSITIVE_POLARITY,
            fail_reasons=["Sensitivity too low."],
            warnings=[],
            waveform_metrics=None,
        )
        harness = SimpleNamespace(
            busy=False,
            measuring=False,
            specimen_id_var=SimpleNamespace(get=lambda: "S-UNSAVED-1"),
            last_result=prior_result,
            result_saved=False,
            active_run_id="RUN-UNSAVED-1",
            measurement_run_number=7,
        )

        with mock.patch.object(app.messagebox, "showinfo") as showinfo, mock.patch.object(
            app.threading,
            "Thread",
        ) as thread:
            app.EmitterTesterApp.run_measurement(harness)

        showinfo.assert_called_once()
        self.assertEqual(showinfo.call_args.args[0], "Save this run before repeating")
        thread.assert_not_called()
        self.assertIs(harness.last_result, prior_result)
        self.assertEqual(harness.active_run_id, "RUN-UNSAVED-1")
        self.assertEqual(harness.measurement_run_number, 7)

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

    def test_simulator_bad_signal_cases_stabilize_then_use_normal_gates(self):
        for case_name in ("Low sensitivity", "Wrong polarity"):
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
                self.assertFalse(final.passed)

    @unittest.skipUnless(os.environ.get("DISPLAY"), "requires an X11 display")
    def test_gui_title_and_config_load(self):
        root = None
        with mock.patch.object(app.EmitterTesterApp, "startup_probe", lambda self: None):
            try:
                root = app.EmitterTesterApp()
                root.withdraw()
                root.update_idletasks()
                self.assertEqual(root.title(), app.CALIBRATION_DISPLAY_NAME)
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
                self.assertIn("Reference unit calibrated", setup_labels)
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


if __name__ == "__main__":
    unittest.main()
