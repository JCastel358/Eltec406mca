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


def with_timestamps(samples, *, period_us=1000, start_us=0):
    """Attach contiguous firmware timestamps; delete slices to fake a gap."""
    return [
        SimpleNamespace(
            volts=sample.volts,
            sync=sample.sync,
            timestamp_us=start_us + index * period_us,
        )
        for index, sample in enumerate(samples)
    ]


class StreamGapFillerTests(unittest.TestCase):
    """Ported from the 405 M22 with the tolerance machinery (2026-09-03)."""

    @staticmethod
    def sample(timestamp_us, volts, sync=0):
        return SimpleNamespace(timestamp_us=timestamp_us, volts=volts, sync=sync)

    def test_contiguous_chunk_passes_through_untouched(self):
        filler = app.StreamGapFiller(1000.0)
        chunk = [self.sample(index * 1000, 1.0) for index in range(5)]
        out = filler.extend(chunk)
        self.assertEqual(out, chunk)
        self.assertEqual(filler.filled_count, 0)
        self.assertFalse(filler.saw_gaps)

    def test_micro_gap_is_filled_with_interpolated_volts(self):
        filler = app.StreamGapFiller(1000.0)
        first = filler.extend([self.sample(0, 0.700), self.sample(1000, 0.700)])
        self.assertEqual(len(first), 2)
        # Next timestamp jumps 3 ms: two slots are missing.
        out = filler.extend([self.sample(4000, 0.706)])
        self.assertEqual(len(out), 3)
        self.assertEqual([entry.timestamp_us for entry in out[:2]], [2000, 3000])
        self.assertAlmostEqual(out[0].volts, 0.702, places=6)
        self.assertAlmostEqual(out[1].volts, 0.704, places=6)
        self.assertEqual(filler.gap_count, 1)
        self.assertEqual(filler.filled_count, 2)
        self.assertTrue(filler.saw_gaps)

    def test_sync_transition_inside_a_gap_lands_at_the_midpoint(self):
        filler = app.StreamGapFiller(1000.0)
        filler.extend([self.sample(0, 1.0, sync=0)])
        out = filler.extend([self.sample(3000, 1.0, sync=1)])
        self.assertEqual([entry.sync for entry in out], [0, 1, 1])

    def test_oversize_gap_is_counted_but_never_fabricated(self):
        filler = app.StreamGapFiller(1000.0)
        filler.extend([self.sample(0, 1.0)])
        jump_us = (app.STREAM_MAX_MISSING_SAMPLES + 5) * 1000
        out = filler.extend([self.sample(jump_us, 1.0)])
        self.assertEqual(len(out), 1)
        self.assertEqual(filler.filled_count, 0)
        self.assertEqual(filler.oversize_gap_count, 1)
        self.assertTrue(filler.saw_gaps)

    def test_untimestamped_samples_pass_through(self):
        filler = app.StreamGapFiller(1000.0)
        chunk = [SimpleNamespace(volts=1.0, sync=0) for _ in range(3)]
        self.assertEqual(filler.extend(chunk), chunk)
        self.assertFalse(filler.saw_gaps)

    def test_uint32_timestamp_wraparound_is_not_a_gap(self):
        filler = app.StreamGapFiller(1000.0)
        filler.extend([self.sample(0xFFFFFFFF - 500, 1.0)])
        out = filler.extend([self.sample((0xFFFFFFFF + 500) & 0xFFFFFFFF, 1.0)])
        self.assertEqual(len(out), 1)
        self.assertFalse(filler.saw_gaps)


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
        # 25 gaps / ~25 missing samples exceed the micro-gap budget by every
        # measure, so the capture is rejected with nothing recorded and the
        # dedicated subclass marks the failure as a transient transport
        # problem, letting production callers retry the reading (2026-09-03,
        # ported from the 405 M22; a single gap used to reject here).
        rig = FakeLowLevelRig(gap_count=25)
        with self.assertRaisesRegex(app.StreamIntegrityError, "timestamp gaps"):
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

    def test_bounded_micro_gap_is_tolerated_with_a_note(self):
        # A couple of milliseconds lost at the USB-scheduling level (the
        # 2026-08-31 bench failure was exactly one 2-sample gap in a 12 s
        # capture) must not reject a whole multi-second capture.
        rig = FakeLowLevelRig(gap_count=2)
        waveform, _sync, _rate, analysis = rig.read_waveform_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic() + 1.0,
        )
        self.assertTrue(analysis.report.measurement_complete)
        self.assertGreater(len(waveform), 0)
        self.assertIsNotNone(rig.last_stream_tolerance_note)
        self.assertIn("2 serial micro-gap", rig.last_stream_tolerance_note)

    def test_reference_micro_gap_is_refilled_so_sync_validation_passes(self):
        # A 2-sample drop inside a 100-sample 10 Hz cycle used to read as
        # 1000/98 = 10.204 Hz and fail the ±0.1 Hz sync check as a fake
        # "check firmware and GPIO33" rig error. The firmware timestamps
        # expose the gap, and the filler rebuilds the missing slots before
        # any index-based analysis runs.
        rig = FakeLowLevelRig("Known good")
        bank = with_timestamps(stream_samples_for_peaks([0.700] * 15))
        del bank[130:132]  # mid-cycle, away from any sync edge
        rig._samples = bank

        _waveform, _sync, _rate, analysis = rig.read_reference_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic(),
        )

        self.assertTrue(analysis.report.measurement_complete)
        self.assertEqual(len(analysis.measurement_cycles), 5)

    def test_gap_on_a_sync_edge_is_a_retryable_stream_error_not_a_rig_fault(self):
        # A gap too large to refill (or one that swallows a sync edge) breaks
        # the validation cadence for real - but it is a transport problem, so
        # it must surface as retryable StreamIntegrityError, not as the
        # non-retried "check firmware and GPIO33" HardwareNotReadyError.
        rig = FakeLowLevelRig("Known good")
        bank = with_timestamps(stream_samples_for_peaks([0.700] * 15))
        del bank[190:190 + app.STREAM_MAX_MISSING_SAMPLES + 10]
        rig._samples = bank

        with self.assertRaisesRegex(
            app.StreamIntegrityError, "sync validation window"
        ):
            rig.read_reference_until_stable(
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                settings=SETTINGS,
                pwm_started_monotonic=time.monotonic(),
            )
        self.assertEqual(rig.stop_calls, 1)

    def test_driven_stream_that_goes_silent_is_a_retryable_attributed_stall(self):
        # The bank ends 0.3 s in, so read_stream() returns nothing: the
        # capture stops the stream itself, reads the STREAM,END count and
        # raises the retryable, attributed error (2026-09-03).
        rig = FakeLowLevelRig()
        rig._samples = rig._samples[:300]
        with self.assertRaises(app.StreamStalledError) as caught:
            rig.read_waveform_until_stable(
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                settings=SETTINGS,
                pwm_started_monotonic=time.monotonic(),
            )
        self.assertEqual(rig.stop_calls, 1)
        self.assertFalse(rig.is_streaming)
        self.assertIsInstance(caught.exception, app.StreamIntegrityError)
        self.assertIn("waveform stream stalled after 300/", str(caught.exception))
        # FakeDiagnostics answers STREAM,STOP with the host count: the board
        # is alive but produced nothing more.
        self.assertEqual(caught.exception.kind, app.STALL_KIND_BOARD_SILENT)

        frame_rig = FakeLowLevelRig()
        frame_rig._samples = frame_rig._samples[:100]
        with self.assertRaises(app.StreamStalledError):
            frame_rig.read_waveform_frame(3, app.WAVEFORM_INPUT_RANGE_V)
        self.assertEqual(frame_rig.stop_calls, 1)


class StallRig(app.EmitterEsp32Rig):
    """EmitterEsp32Rig whose STREAM,STOP reply is scripted, for _stall_error."""

    def __init__(self, diagnostics, *, streaming=True):
        self._diagnostics = diagnostics
        self._active = streaming
        self.stop_calls = 0
        self.stop_kwargs = None

    @property
    def is_streaming(self):
        return self._active

    @property
    def stream_diagnostics(self):
        return self._diagnostics

    def stop_stream(self, *, timeout_s=2.0, raise_on_timeout=True):
        self.stop_calls += 1
        self.stop_kwargs = dict(timeout_s=timeout_s, raise_on_timeout=raise_on_timeout)
        self._active = False
        return self._diagnostics


def stall_diagnostics(**overrides):
    """What the backend hands back after STREAM,STOP: a healthy board by default."""
    fields = dict(
        received_samples=37624,
        drained_samples=0,
        firmware_samples_sent=37624,
        ignored_lines=0,
        ignored_line_samples=[],
        last_sample_monotonic=None,
        expected_rate_hz=1000.0,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class StreamStallAttributionTests(unittest.TestCase):
    """_stall_error (2026-09-03): the STREAM,STOP reply says which side went quiet."""

    def stall(self, diagnostics, **kwargs):
        rig = StallRig(diagnostics)
        error = rig._stall_error(37624, **kwargs)
        # The stream is stopped BEFORE the error is built (that is where the
        # numbers come from), without raising on a missing STREAM,END.
        self.assertEqual(rig.stop_calls, 1)
        self.assertFalse(rig.stop_kwargs["raise_on_timeout"])
        self.assertFalse(rig.is_streaming)
        # Retryable: the bounded restart replaces the technician's Re-measure.
        self.assertIsInstance(error, app.StreamIntegrityError)
        self.assertEqual(error.received, 37624)
        self.assertIn(f"[{error.kind}]", str(error))
        return error

    def test_no_stop_reply_means_the_board_or_link_is_gone(self):
        error = self.stall(stall_diagnostics(firmware_samples_sent=None))
        self.assertEqual(error.kind, app.STALL_KIND_NO_REPLY)
        self.assertIn("ESP32 waveform stream stalled after 37624 samples", str(error))
        self.assertIn("did not answer STREAM,STOP", str(error))

    def test_restarted_counter_or_ready_banner_means_the_board_rebooted(self):
        error = self.stall(
            stall_diagnostics(
                firmware_samples_sent=0,
                ignored_lines=3,
                ignored_line_samples=["???rst", "READY,ELTEC-ESP32-ADS1256"],
            )
        )
        self.assertEqual(error.kind, app.STALL_KIND_BOARD_RESET)
        self.assertIn("restarted during the capture", str(error))
        self.assertIn("READY banner arrived mid-stream", str(error))
        error = self.stall(stall_diagnostics(firmware_samples_sent=0))
        self.assertEqual(error.kind, app.STALL_KIND_BOARD_RESET)
        self.assertIn("came back at 0", str(error))

    def test_backlog_or_firmware_ahead_means_the_computer_froze(self):
        error = self.stall(
            stall_diagnostics(
                received_samples=39900, drained_samples=2276, firmware_samples_sent=39905
            )
        )
        self.assertEqual(error.kind, app.STALL_KIND_HOST)
        self.assertIn("firmware 39905 vs host 37624", str(error))
        self.assertIn("2276 samples = 2.3 s were still waiting", str(error))
        self.assertIn("the rig did not stop", str(error))
        error = self.stall(stall_diagnostics(firmware_samples_sent=40100))
        self.assertEqual(error.kind, app.STALL_KIND_HOST)
        self.assertIn("computer stopped reading", str(error))

    def test_matching_counts_mean_the_board_went_silent(self):
        error = self.stall(
            stall_diagnostics(
                received_samples=37626, drained_samples=2, firmware_samples_sent=37626
            ),
            target=40310,
        )
        self.assertEqual(error.kind, app.STALL_KIND_BOARD_SILENT)
        self.assertIn("stalled after 37624/40310 samples", str(error))
        self.assertIn("data-ready signal stopped", str(error))

    def test_silence_is_measured_against_the_last_live_sample(self):
        error = self.stall(
            stall_diagnostics(last_sample_monotonic=time.monotonic() - 2.2)
        )
        self.assertRegex(str(error), r"no sample for 2\.[0-9] s")

    def test_a_dead_port_is_not_a_stall(self):
        rig = StallRig(stall_diagnostics())

        def dead(**_kwargs):
            raise app.Esp32BackendError("Could not send STREAM,STOP: port gone")

        rig.stop_stream = dead
        with self.assertRaisesRegex(app.Esp32BackendError, "port gone"):
            rig._stall_error(10)


class StreamRetryHelperTests(unittest.TestCase):
    def test_stall_is_retried_like_any_integrity_failure_and_reported(self):
        stalls: list[int] = []
        seen: list[tuple[int, str]] = []

        def stalled_once(attempt: int) -> float:
            stalls.append(attempt)
            if len(stalls) == 1:
                raise app.StreamStalledError(
                    "ESP32 waveform stream stalled after 3000/23001 samples. [host-stall]",
                    kind=app.STALL_KIND_HOST,
                    received=3000,
                )
            return 6.5

        self.assertEqual(
            app.call_with_stream_retries(
                stalled_once,
                on_retry=lambda attempt, exc: seen.append((attempt, exc.kind)),
            ),
            6.5,
        )
        self.assertEqual(stalls, [0, 1])
        self.assertEqual(seen, [(1, app.STALL_KIND_HOST)])

        # A persistent stream problem still fails after the bounded retries.
        persistent: list[int] = []

        def always_bad(attempt: int) -> float:
            persistent.append(attempt)
            raise app.StreamIntegrityError("80 duplicate timestamps")

        with self.assertRaises(app.StreamIntegrityError):
            app.call_with_stream_retries(always_bad, on_retry=lambda _n, _e: None)
        self.assertEqual(len(persistent), app.REFERENCE_READING_STREAM_RETRIES + 1)

        # Non-transport errors (hardware faults, cancellation) never retry.
        other: list[int] = []

        def cancelled(attempt: int) -> float:
            other.append(attempt)
            raise app.Esp32BackendError("Measurement was cancelled.")

        with self.assertRaisesRegex(app.Esp32BackendError, "cancelled"):
            app.call_with_stream_retries(
                cancelled, on_retry=lambda attempt, _exc: other.append(attempt)
            )
        self.assertEqual(other, [0])


class StreamIntegrityBudgetTests(unittest.TestCase):
    """_validate_stream_diagnostics: bounded micro-gap loss vs corruption."""

    @staticmethod
    def validate(**kwargs):
        rig = app.EmitterEsp32Rig.__new__(app.EmitterEsp32Rig)
        minimum = kwargs.pop("minimum_samples", 0)
        diagnostics = FakeDiagnostics(**kwargs)
        rig._validate_stream_diagnostics(diagnostics, minimum_samples=minimum)
        return rig.last_stream_tolerance_note

    def test_perfectly_clean_stream_has_no_note(self):
        note = self.validate(received_samples=12000)
        self.assertIsNone(note)

    def test_single_micro_gap_with_matching_count_deficit_is_tolerated(self):
        # The exact 2026-08-31 bench failure: 1 gap, ~2 missing samples,
        # host/firmware 12047/12049. This must record, not error.
        note = self.validate(
            received_samples=12047,
            timestamp_gap_count=1,
            estimated_missing_samples=2,
            firmware_samples_sent=12049,
        )
        self.assertIsNotNone(note)
        self.assertIn("1 serial micro-gap", note)
        self.assertIn("~2 of 12047", note)

    def test_too_many_gaps_or_samples_still_reject(self):
        with self.assertRaisesRegex(app.StreamIntegrityError, "timestamp gaps"):
            self.validate(
                received_samples=12000,
                timestamp_gap_count=app.STREAM_MAX_MICRO_GAPS + 1,
                estimated_missing_samples=4,
            )
        with self.assertRaisesRegex(app.StreamIntegrityError, "timestamp gaps"):
            self.validate(
                received_samples=12000,
                timestamp_gap_count=1,
                estimated_missing_samples=app.STREAM_MAX_MISSING_SAMPLES + 1,
            )

    def test_duplicates_are_never_tolerated_even_with_small_gaps(self):
        # The historical Windows driver-overflow signature (gaps + duplicate
        # re-delivery) must keep failing loudly.
        with self.assertRaisesRegex(app.StreamIntegrityError, "duplicate"):
            self.validate(
                received_samples=12000,
                timestamp_gap_count=1,
                estimated_missing_samples=6,
                duplicate_timestamps=80,
            )

    def test_count_deficit_beyond_budget_and_surplus_both_reject(self):
        with self.assertRaisesRegex(app.StreamIntegrityError, "counts differ"):
            self.validate(
                received_samples=12000,
                firmware_samples_sent=12000 + app.STREAM_MAX_MISSING_SAMPLES + 1,
            )
        # More received than sent means duplicated/corrupted records.
        with self.assertRaisesRegex(app.StreamIntegrityError, "counts differ"):
            self.validate(
                received_samples=12010,
                firmware_samples_sent=12000,
            )


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
        offset_sequence=None,
        case_name="Known good",
        activation_time=None,
        deactivation_time=None,
        configure_error=None,
        error=None,
        reference_mv=100.0,
        capture_errors=None,
        reference_errors=None,
        front_end_reverted=False,
    ):
        self.battery = battery
        self.offset = offset
        # Successive OFFSET? readings for the settled-offset wait; the last
        # value repeats once the list runs out. None = a level that holds.
        self.offset_sequence = list(offset_sequence) if offset_sequence else []
        self.case_name = case_name
        # Exceptions raised by successive captures before one succeeds (a
        # stall or integrity failure the app restarts by itself).
        self.capture_errors = list(capture_errors) if capture_errors else []
        self.reference_errors = list(reference_errors) if reference_errors else []
        # The pre-measurement front-end re-check (2026-09-03). Recorded
        # apart from ``calls`` so the exact call-order assertions stay as
        # they are; a position is how many calls had been made when the
        # check ran (1 = right after the opening pwm_off).
        self.front_end_reverted = front_end_reverted
        self.front_end_checks = 0
        self.front_end_check_positions: list[int] = []
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
        if self.offset_sequence:
            self.offset = self.offset_sequence.pop(0)
        return self.offset

    def configure_emitter_pwm(self, **kwargs):
        del kwargs
        self.calls.append("pwm_on")
        if self.configure_error is not None:
            raise self.configure_error
        return time.monotonic() if self.activation_time is None else self.activation_time

    def ensure_qualified_front_end(self):
        self.front_end_checks += 1
        self.front_end_check_positions.append(len(self.calls))
        reverted = self.front_end_reverted
        self.front_end_reverted = False
        return reverted

    def read_waveform_until_stable(self, **kwargs):
        self.calls.append("capture")
        if self.error is not None:
            raise self.error
        if self.capture_errors:
            raise self.capture_errors.pop(0)
        waveform, sync, rate, _offset, analysis = prepared_capture(self.case_name)
        if kwargs.get("progress"):
            kwargs["progress"](analysis)
        if kwargs.get("preview"):
            kwargs["preview"](waveform[-500:], sync[-500:])
        return waveform, sync, rate, analysis


# The settled-offset verification after the capture (2026-09-03): the
# re-read, then the confirmation poll(s). An in-band level is confirmed once;
# an out-of-band level is held for two quiet polls before the app gives up on
# it. See the OFFSET_SETTLE_* constants.
SETTLED_OFFSET_CALLS = ["offset", "offset"]
SETTLED_OFFSET_CALLS_OUT_OF_BAND = ["offset", "offset", "offset"]


class MeasurementHarness:
    def __init__(self, device):
        self.device = device
        self.hardware_lock = threading.Lock()
        self.measure_token = 7
        self.last_capture_report = None
        self.last_offset_initial_v = None
        self.last_offset_settle = None
        self.last_reference_check_mv = None
        self.reference_calibration_error = None
        self.reference_calibration = app.build_reference_calibration(
            [100.0] * app.REFERENCE_CALIBRATION_READINGS
        )
        self.stability_settings = SETTINGS
        self.callback_events = []
        self.preview_count = 0
        self.stream_retries = []
        self.rig_notes = []
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

    def _log_stream_retry(self, phase, attempt, exc):
        self.stream_retries.append((phase, attempt, exc))

    def _log_rig_note(self, text):
        self.rig_notes.append(text)

    def _capture_reference_reading(self, device, **_kwargs):
        device.calls.append("reference")
        errors = getattr(device, "reference_errors", None)
        if errors:
            raise errors.pop(0)
        return device.reference_mv

    def on_battery_update(self, value, error=None):
        self.callback_events.append(("battery", value, error))

    def on_initial_offset(self, token, value):
        self.callback_events.append(("initial_offset", token, value))
        self.last_offset_initial_v = value

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
        # The settle wait's timing is covered by SettledOffsetTests with a
        # fake clock; here it only has to run, so the poll costs nothing.
        poll_patcher = mock.patch.object(app, "OFFSET_SETTLE_POLL_S", 0.0)
        poll_patcher.start()
        self.addCleanup(poll_patcher.stop)

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
                *SETTLED_OFFSET_CALLS,
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

    def test_calibration_reading_that_stalls_is_restarted_and_logged(self):
        # 2026-09-03: one stalled reading out of five restarts that reading
        # only (the emitter stays on), with the attributed error shown on
        # the calibration card and written to the attempts log.
        stall = app.StreamStalledError(
            "ESP32 waveform stream stalled after 1200/23001 samples. [board-reset]",
            kind=app.STALL_KIND_BOARD_RESET,
            received=1200,
        )
        device = FakeMeasurementDevice(reference_mv=102.5, reference_errors=[stall])
        harness = MeasurementHarness(device)
        with mock.patch.object(app, "save_reference_calibration"):
            calibration = app.EmitterTesterApp._hardware_reference_calibration(
                harness,
                harness.measure_token,
                lambda callback: callback(),
            )

        self.assertAlmostEqual(calibration.mean_mv, 102.5)
        self.assertEqual(
            device.calls,
            ["pwm_off", "pwm_on"]
            + ["reference"] * (app.REFERENCE_CALIBRATION_READINGS + 1)
            + ["pwm_off"],
        )
        self.assertEqual(device.front_end_checks, 1)
        self.assertEqual(
            harness.stream_retries, [("reference calibration reading 1", 1, stall)]
        )
        progress = [
            event[1] for event in harness.callback_events
            if event[0] == "reference_progress"
        ]
        self.assertTrue(
            any(text.startswith("Stream stalled in reading 1") for text in progress),
            progress,
        )

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
                # 1.3 V never comes back into band, so the wait ends on the
                # second quiet confirmation instead of the deadline.
                *SETTLED_OFFSET_CALLS_OUT_OF_BAND,
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

    def test_battery_is_never_read_and_only_a_floating_ain0_blocks_before_pwm(self):
        # Battery monitoring is disabled on the unified-rig fixture (nothing
        # measurable on AIN7): even a device reporting a dead battery is never
        # asked for it and the measurement proceeds normally.
        self.assertFalse(app.BATTERY_MONITORING_ENABLED)
        low = FakeMeasurementDevice(battery=5.7)
        _harness, (_metrics, final, _offset) = self.run_hardware(low)
        self.assertNotIn("battery", low.calls)
        self.assertTrue(final.passed)

        missing = FakeMeasurementDevice(offset=0.02)
        with self.assertRaises(app.HardwareNotReadyError):
            self.run_hardware(missing)
        self.assertEqual(
            missing.calls,
            ["pwm_off", "pwm_on", "reference", "pwm_off", "offset"],
        )

    def test_railed_ain0_is_measured_as_a_part_not_reported_as_no_sensor(self):
        # 2026-09-03: the high side of the plausibility band is no longer an
        # abort. A part sitting at the ADC rail is a real sensor whose offset
        # has not settled - it runs the full test and records HO from the
        # settled re-read instead of a "no sensor" wiring error.
        railed = FakeMeasurementDevice(offset=3.0)
        _harness, (_metrics, final, offset) = self.run_hardware(railed)
        self.assertFalse(final.passed)
        self.assertEqual(offset, 3.0)
        self.assertEqual(app.suggest_failure_mode(final), "HO - High offset")
        self.assertIn("capture", railed.calls)

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

    def test_stalled_capture_is_restarted_by_the_app_and_logged(self):
        # 2026-09-03 (ported from the 405 M22): a stall used to be a dead
        # end (the technician pressed Re-measure); it is now a
        # StreamIntegrityError and gets the same bounded restart as a
        # micro-gap failure, with the attributed error on the status line
        # and in the attempts log.
        stall = app.StreamStalledError(
            "ESP32 waveform stream stalled after 3000/23001 samples (no sample for 2.1 s). "
            "The board kept sampling [host-stall]",
            kind=app.STALL_KIND_HOST,
            received=3000,
        )
        device = FakeMeasurementDevice(capture_errors=[stall])
        harness, (_metrics, final, _offset) = self.run_hardware(device)
        self.assertTrue(final.passed)
        self.assertEqual(device.calls.count("capture"), 2)
        self.assertEqual(device.calls.count("pwm_on"), 3)  # reference + 2 drives
        statuses = [
            event[2] for event in harness.callback_events if event[0] == "status"
        ]
        self.assertTrue(
            any(text.startswith("Stream stalled during the sensor capture") for text in statuses),
            statuses,
        )
        self.assertEqual(harness.stream_retries, [("sensor capture", 1, stall)])

    def test_persistent_stall_fails_after_the_bounded_restarts_with_pwm_off(self):
        stall = app.StreamStalledError(
            "ESP32 waveform stream stalled after 3000/23001 samples. [board-silent]",
            kind=app.STALL_KIND_BOARD_SILENT,
            received=3000,
        )
        device = FakeMeasurementDevice(error=stall)
        with self.assertRaises(app.StreamStalledError):
            self.run_hardware(device)
        self.assertEqual(
            device.calls.count("capture"), app.REFERENCE_READING_STREAM_RETRIES + 1
        )
        self.assertEqual(device.calls[-1], "pwm_off")

    def test_front_end_is_rechecked_before_every_measurement(self):
        # 2026-09-03: FE,V19 is session state on the board. The worker
        # re-checks it right after the opening PWM-off, before anything is
        # read; a board that still matches costs one read-back and no note.
        device = FakeMeasurementDevice()
        harness, (_metrics, final, _offset) = self.run_hardware(device)
        self.assertTrue(final.passed)
        self.assertEqual(device.front_end_checks, 1)
        self.assertEqual(device.front_end_check_positions, [1])
        self.assertEqual(harness.rig_notes, [])

    def test_reverted_front_end_is_reapplied_shown_and_logged(self):
        # The only way the front end reverts with the port open is an ESP32
        # restart: the app re-applies FE,V19 (the backend verifies it), says
        # so on the status line and leaves a rig_note in the attempts log.
        device = FakeMeasurementDevice(front_end_reverted=True)
        harness, (_metrics, final, _offset) = self.run_hardware(device)
        self.assertTrue(final.passed)
        self.assertEqual(harness.rig_notes, [app.FRONT_END_REVERTED_TEXT])
        statuses = [
            event[2] for event in harness.callback_events if event[0] == "status"
        ]
        self.assertIn(app.FRONT_END_REVERTED_TEXT, statuses)
        self.assertIn("board restarted", app.FRONT_END_REVERTED_TEXT)

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

    def setUp(self):
        poll_patcher = mock.patch.object(app, "OFFSET_SETTLE_POLL_S", 0.0)
        poll_patcher.start()
        self.addCleanup(poll_patcher.stop)

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
    def setUp(self):
        poll_patcher = mock.patch.object(app, "OFFSET_SETTLE_POLL_S", 0.0)
        poll_patcher.start()
        self.addCleanup(poll_patcher.stop)

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
    window was maximized, and was clipped off the right edge. The bar is two
    buttons now (Stop, Next), but the blocked-measure labels are still long
    enough to need the same ladder.
    """

    class FakeButton:
        def __init__(self, text):
            self._text = text
            self._footer_full_text = text

    def test_label_tiers_shorten_in_the_documented_order(self):
        label = app.EmitterTesterApp._footer_label
        nxt = self.FakeButton("Next sensor (Enter)")
        self.assertEqual(label(nxt, "full"), "Next sensor (Enter)")
        self.assertEqual(label(nxt, "nohint"), "Next sensor")
        self.assertEqual(label(nxt, "short"), "Next")
        stop = self.FakeButton("Stop batch (Esc)")
        self.assertEqual(label(stop, "nohint"), "Stop batch")
        self.assertEqual(label(stop, "short"), "Stop")
        blocked = self.FakeButton("Calibrate reference unit to test")
        self.assertEqual(label(blocked, "short"), "Calibrate reference first")
        # A label with no hint and no compact form is left alone at every tier.
        plain = self.FakeButton("Stop")
        for tier in ("full", "nohint", "short"):
            self.assertEqual(label(plain, tier), "Stop")

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
        """Reach a result step with an unsaved verdict.

        start_batch now reads the sensor straight away, so the measurement
        is stubbed out - this test is about the footer geometry.
        """
        root.batch_var.set("FIT")
        root.tester_var.set("tester")
        with mock.patch.object(app.EmitterTesterApp, "run_measurement", lambda self: None):
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
            # Stop has no meaning before a batch starts.
            self.assertFalse(root.stop_button.winfo_manager())
        self._drive_to_result(root)
        root.update_idletasks()
        with self.subTest(step="result"):
            self.assertLessEqual(self._footer_overflow(root), 0)
            # ...and both buttons are on the bar once a batch is running.
            self.assertTrue(root.stop_button.winfo_manager())
            self.assertTrue(root.primary_button.winfo_manager())


class FakeVar:
    """Stand-in for a tk.StringVar in headless harness tests."""

    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value



class SensorNumberingTests(unittest.TestCase):
    """2026-09-02: a sensor number is only spent by a PASS.

    A failed part is put aside on the bench and another one takes its place,
    so the replacement is tested under the same number until one passes.
    The batch CSV therefore holds one row per TEST.
    """

    def _row(self, csv_path, number, passed, outcome=None):
        final = app.FinalResult(
            passed=passed,
            offset_v=1.2,
            sensitivity_mv=40.0 if passed else 2.0,
            polarity=app.POSITIVE_POLARITY,
            fail_reasons=[] if passed else ["Sensitivity too low: raw 2.000 mV"],
            warnings=[],
            waveform_metrics=None,
        )
        app.append_result_csv(
            csv_path,
            batch_number="B9",
            sensor_number=number,
            sensor_id=f"B9-{number}",
            tester_name="Operator",
            filter_setup=app.DEFAULT_FILTER_SETUP,
            pwm_channel=app.EMITTER_PWM_CHANNEL,
            pwm_hz=app.EMITTER_PWM_FREQUENCY_HZ,
            pwm_duty=app.EMITTER_PWM_DUTY_CYCLE,
            final_result=final,
            comment="",
            snapshot_paths=[],
            failure_mode="" if passed else "LS - Low sensitivity",
            number_attempt=app.number_attempt_for_batch(csv_path, f"B9-{number}"),
        )

    def test_a_failed_part_leaves_its_number_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "B9.csv"
            self.assertEqual(app.next_sensor_number_for_batch(path), 1)
            self._row(path, 1, passed=True)
            self.assertEqual(app.next_sensor_number_for_batch(path), 2)
            # Two bad parts in a row: 2 stays the number on offer.
            self._row(path, 2, passed=False)
            self.assertEqual(app.next_sensor_number_for_batch(path), 2)
            self._row(path, 2, passed=False)
            self.assertEqual(app.next_sensor_number_for_batch(path), 2)
            # ...and the part that finally passes earns it.
            self._row(path, 2, passed=True)
            self.assertEqual(app.next_sensor_number_for_batch(path), 3)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        ids = [row["sensor_id"] for row in rows]
        self.assertEqual(ids, ["B9-1", "B9-2", "B9-2", "B9-2"])
        self.assertEqual(
            [row["pass_fail"] for row in rows],
            [
                app.OUTCOME_PASS,
                app.OUTCOME_FAIL,
                app.OUTCOME_FAIL,
                app.OUTCOME_PASS,
            ],
        )
        # number_attempt keeps the repeated ids readable without row order.
        self.assertEqual(
            [row["number_attempt"] for row in rows], ["1", "1", "2", "3"]
        )

    def test_batches_written_before_the_rule_are_not_renumbered(self):
        # Numbers used to be handed out per row, so a pre-2026-09-02 file can
        # hold a FAIL at a number ABOVE its last pass. Continuing from the
        # highest pass (not from a count of passes) never re-uses one of them.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.csv"
            self._row(path, 1, passed=True)
            self._row(path, 2, passed=False)
            self._row(path, 3, passed=True)
            self.assertEqual(app.next_sensor_number_for_batch(path), 4)

    def test_number_attempt_counts_the_parts_tried_under_one_number(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "B9.csv"
            self.assertEqual(app.number_attempt_for_batch(path, "B9-4"), 1)
            self._row(path, 4, passed=False)
            self.assertEqual(app.number_attempt_for_batch(path, "B9-4"), 2)
            self.assertEqual(app.number_attempt_for_batch(path, "B9-5"), 1)
            self.assertEqual(app.number_attempt_for_batch(path, ""), 1)

    def test_row_sensor_number_reads_either_column(self):
        self.assertEqual(app.row_sensor_number({"sensor_number": "12"}), 12)
        self.assertEqual(app.row_sensor_number({"sensor_id": "B9-7"}), 7)
        self.assertIsNone(app.row_sensor_number({"sensor_id": "no-number-x"}))
        self.assertIsNone(app.row_sensor_number({}))


class StopAndNextTests(unittest.TestCase):
    """The two-button action bar: Stop is always live, Next always reads."""

    def _harness(self, **overrides):
        harness = SimpleNamespace(
            SETUP_STEP=app.EmitterTesterApp.SETUP_STEP,
            RESULT_STEP=app.EmitterTesterApp.RESULT_STEP,
            step=app.EmitterTesterApp.RESULT_STEP,
            busy=False,
            measuring=False,
            result_saved=False,
            last_result=None,
            last_metrics=None,
            last_measure_error=None,
            measure_token=7,
            batch_number="B9",
            current_sensor_number=3,
            current_sensor_id="B9-3",
            preview_waveform=np.zeros(4),
            preview_sync=np.zeros(4),
            measure_status_var=FakeVar(""),
            status_var=FakeVar(""),
            calls=[],
        )
        harness._log_attempt = lambda event, **kw: harness.calls.append(("log", event))
        harness._reset_measure_progress = lambda: harness.calls.append(("reset",))
        harness.render_step = lambda: harness.calls.append(("render",))
        harness.run_measurement = lambda: harness.calls.append(("measure",))
        harness.abort_measurement = lambda: harness.calls.append(("abort",))
        harness._end_batch = lambda: harness.calls.append(("end",))
        harness._advance_to_next_sensor = lambda: harness.calls.append(("advance",))
        harness.start_batch = lambda: harness.calls.append(("start",))
        harness.save_current_sensor = lambda: (
            harness.calls.append(("save",)) or True
        )
        for name, value in overrides.items():
            setattr(harness, name, value)
        return harness

    # ----- Next ----- #
    def test_next_saves_a_verdict_then_reads_the_replacement(self):
        harness = self._harness(last_result=SimpleNamespace(passed=False))
        app.EmitterTesterApp.go_next(harness)
        self.assertEqual(harness.calls, [("save",), ("advance",)])

    def test_next_does_not_advance_when_the_write_fails(self):
        harness = self._harness(
            last_result=SimpleNamespace(passed=False),
            save_current_sensor=lambda: False,
        )
        app.EmitterTesterApp.go_next(harness)
        self.assertEqual(harness.calls, [])

    def test_next_with_nothing_to_save_reads_the_same_number_again(self):
        harness = self._harness()
        app.EmitterTesterApp.go_next(harness)
        self.assertEqual(harness.calls, [("measure",)])
        self.assertEqual(harness.current_sensor_id, "B9-3")

    def test_next_is_inert_while_a_capture_is_running(self):
        harness = self._harness(measuring=True)
        app.EmitterTesterApp.go_next(harness)
        self.assertEqual(harness.calls, [])

    # ----- Stop ----- #
    def test_stop_during_a_capture_aborts_it(self):
        harness = self._harness(measuring=True)
        app.EmitterTesterApp.stop(harness)
        self.assertEqual(harness.calls, [("abort",)])

    def test_abort_records_nothing_and_keeps_the_number(self):
        harness = self._harness(
            measuring=True, busy=True, last_result=SimpleNamespace(passed=True)
        )
        app.EmitterTesterApp.abort_measurement(harness)
        # The token bump is what stops the capture: every callback the worker
        # posts from here on is ignored, and the loops raise at their next
        # chunk boundary (cancelled=lambda: token != self.measure_token).
        self.assertEqual(harness.measure_token, 8)
        self.assertFalse(harness.measuring)
        self.assertFalse(harness.busy)
        self.assertIsNone(harness.last_result)
        self.assertEqual(harness.current_sensor_id, "B9-3")
        self.assertIn(("log", app.attempt_history.EVENT_STOPPED), harness.calls)

    def test_stop_when_idle_ends_the_batch(self):
        harness = self._harness()
        app.EmitterTesterApp.stop(harness)
        self.assertEqual(harness.calls, [("end",)])

    def test_stop_saves_an_unsaved_verdict_without_asking(self):
        # 2026-09-03: Stop used to ask "save it before ending the batch?".
        # The part has already been judged, so it is written and the batch
        # ends - no confirmation to mis-answer.
        harness = self._harness(last_result=SimpleNamespace(passed=True))
        with mock.patch.object(app.messagebox, "askyesnocancel") as prompt:
            app.EmitterTesterApp.stop(harness)
        prompt.assert_not_called()
        self.assertEqual(harness.calls, [("save",), ("end",)])

    def test_stop_keeps_the_batch_running_when_the_write_fails(self):
        # save_current_sensor has already shown its own error dialog; the
        # verdict stays on screen instead of leaving with the batch.
        harness = self._harness(
            last_result=SimpleNamespace(passed=True),
            save_current_sensor=lambda: False,
        )
        app.EmitterTesterApp.stop(harness)
        self.assertEqual(harness.calls, [])

    def test_stop_does_nothing_before_a_batch_starts(self):
        harness = self._harness(step=app.EmitterTesterApp.SETUP_STEP)
        app.EmitterTesterApp.stop(harness)
        self.assertEqual(harness.calls, [])

    # ----- the loop between parts ----- #
    def test_advance_takes_the_next_number_and_reads_immediately(self):
        harness = self._harness()
        harness.prepare_current_sensor = lambda: harness.calls.append(("prepare",))
        harness.show_step = lambda step: harness.calls.append(("step", step))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "B9.csv"
            with mock.patch.object(app, "batch_results_path", return_value=path), \
                    mock.patch.object(
                        app, "next_sensor_number_for_batch", return_value=3
                    ):
                app.EmitterTesterApp._advance_to_next_sensor(harness)
        # No load screen in between: the part is already in the rig.
        self.assertEqual(
            harness.calls,
            [("prepare",), ("step", app.EmitterTesterApp.RESULT_STEP), ("measure",)],
        )
        self.assertEqual(harness.current_sensor_number, 3)

class FakeClock:
    """Deterministic clock for the settled-offset wait."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class SettledOffsetTests(unittest.TestCase):
    """The offset the verdict uses is re-read AFTER the capture (2026-09-03).

    These parts keep moving for tens of seconds after they are seated, so the
    insertion read was failing HO on parts that are inside 0.3-1.2 V once
    settled. The wait's rules live in the OFFSET_SETTLE_* constants.
    """

    def wait(self, readings, start_v, **kwargs):
        clock = FakeClock()
        values = iter(readings)
        return app.wait_for_settled_offset(
            lambda: next(values),
            start_v=start_v,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            **kwargs,
        )

    def test_policy_constants_are_the_agreed_numbers(self):
        self.assertEqual(app.OFFSET_SETTLE_MAX_WAIT_S, 20.0)
        self.assertEqual(app.OFFSET_SETTLE_POLL_S, 1.0)
        self.assertEqual(app.OFFSET_SETTLE_DELTA_FRACTION, 0.10)
        self.assertEqual(app.OFFSET_SETTLE_QUIET_READS, 2)

    def test_in_band_level_is_confirmed_once_and_costs_one_poll(self):
        report = self.wait([0.624] * 5, 0.624)
        self.assertEqual(report.reads, 1)
        self.assertAlmostEqual(report.elapsed_s, app.OFFSET_SETTLE_POLL_S)
        self.assertTrue(report.settled)
        self.assertTrue(report.in_band)
        self.assertFalse(report.drifting)
        self.assertFalse(report.timed_out)
        self.assertAlmostEqual(report.final_v, 0.624)

    def test_settled_out_of_band_part_fails_without_burning_the_deadline(self):
        report = self.wait([1.38] * 30, 1.38)
        self.assertEqual(report.reads, app.OFFSET_SETTLE_QUIET_READS)
        self.assertLess(report.elapsed_s, app.OFFSET_SETTLE_MAX_WAIT_S)
        self.assertTrue(report.settled)
        self.assertFalse(report.in_band)
        self.assertFalse(report.timed_out)

    def test_part_that_comes_back_into_band_is_judged_on_the_settled_value(self):
        report = self.wait([1.34, 1.26, 1.18], 1.35)
        self.assertEqual(report.reads, 3)
        self.assertTrue(report.in_band)
        self.assertAlmostEqual(report.final_v, 1.18)
        final = app.evaluate_result(
            report.final_v, None, app.DEFAULT_FILTER_SETUP
        )
        self.assertNotIn(
            "Offset out of range",
            "; ".join(final.fail_reasons),
        )

    def test_improving_part_keeps_the_whole_deadline_even_when_quiet(self):
        # Steps well inside the 10 % quiet band, but every one moves toward
        # the band: "it changed for the better" must never end the wait early.
        readings = [1.35 - 0.004 * step for step in range(1, 60)]
        report = self.wait(readings, 1.35)
        self.assertTrue(report.timed_out)
        self.assertFalse(report.settled)
        self.assertAlmostEqual(report.elapsed_s, app.OFFSET_SETTLE_MAX_WAIT_S)
        self.assertFalse(report.in_band)

    def test_part_drifting_away_from_the_band_settles_out_immediately(self):
        readings = [1.30 + 0.005 * step for step in range(1, 30)]
        report = self.wait(readings, 1.30)
        self.assertEqual(report.reads, app.OFFSET_SETTLE_QUIET_READS)
        self.assertTrue(report.settled)
        self.assertFalse(report.in_band)

    def test_quiet_is_ten_percent_of_the_current_reading(self):
        # The band is relative to the CURRENT reading: 1.287 -> 1.170 moves
        # exactly 10 % of 1.170 and is quiet, while the larger 1.300 -> 1.170
        # step is 11 % of it and is not.
        quiet = self.wait([1.170], 1.287)
        self.assertTrue(quiet.settled)
        loud = self.wait([1.170], 1.300)
        self.assertFalse(loud.settled)
        self.assertEqual(loud.reads, 1)

    def test_stop_during_the_wait_raises_like_the_capture_loops(self):
        with self.assertRaisesRegex(app.Esp32BackendError, "cancelled"):
            self.wait([1.38] * 5, 1.38, cancelled=lambda: True)

    def test_in_band_but_moving_passes_with_a_warning_and_never_fails(self):
        report = self.wait([0.90], 0.60)
        self.assertTrue(report.in_band)
        self.assertTrue(report.drifting)
        final = app.FinalResult(
            passed=True,
            offset_v=report.final_v,
            sensitivity_mv=1.0,
            polarity=app.POSITIVE_POLARITY,
            fail_reasons=[],
            warnings=[],
        )
        app.apply_offset_settle_warning(final, report)
        self.assertTrue(final.passed)
        self.assertEqual(final.fail_reasons, [])
        self.assertTrue(app.is_offset_still_settling(final))
        self.assertIn("still moving", app.offset_settle_warning_text(final))

    def test_deadline_note_says_the_level_never_settled(self):
        readings = [1.35 - 0.004 * step for step in range(1, 60)]
        report = self.wait(readings, 1.35)
        final = app.FinalResult(
            passed=False,
            offset_v=report.final_v,
            sensitivity_mv=None,
            polarity="",
            fail_reasons=["Offset out of range"],
            warnings=[],
        )
        app.apply_offset_settle_warning(final, report)
        self.assertIn("never stopped moving", app.offset_settle_warning_text(final))

    def test_settled_report_csv_fields(self):
        report = self.wait([1.34, 1.26, 1.18], 1.35)
        fields = report.csv_fields()
        self.assertEqual(fields["offset_settled"], "YES")
        self.assertEqual(fields["offset_settle_reads"], "3")
        self.assertAlmostEqual(float(fields["offset_settle_s"]), 3.0)
        self.assertAlmostEqual(float(fields["offset_settle_delta_v"]), 0.08, places=5)


class SettledOffsetWorkflowTests(unittest.TestCase):
    """The settled re-read inside the shipping (gate-off) measurement flow."""

    def setUp(self):
        poll_patcher = mock.patch.object(app, "OFFSET_SETTLE_POLL_S", 0.0)
        poll_patcher.start()
        self.addCleanup(poll_patcher.stop)

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

    def test_part_that_reads_high_at_insertion_passes_once_it_settles(self):
        # The exact bench complaint: HO at insertion, inside the band by the
        # time the capture is done. The insertion value is kept for the CSV.
        device = FakeMeasurementDevice(offset_sequence=[1.31, 1.24, 1.18])
        harness, (_metrics, final, offset) = self.run_hardware(device)
        self.assertAlmostEqual(harness.last_offset_initial_v, 1.31)
        self.assertAlmostEqual(offset, 1.18)
        self.assertAlmostEqual(final.offset_v, 1.18)
        self.assertTrue(final.passed)
        self.assertTrue(harness.last_offset_settle.in_band)
        self.assertTrue(harness.last_offset_settle.settled)
        self.assertFalse(app.is_offset_still_settling(final))

    def test_still_high_after_the_capture_still_fails(self):
        device = FakeMeasurementDevice(offset=1.31)
        harness, (_metrics, final, offset) = self.run_hardware(device)
        self.assertAlmostEqual(offset, 1.31)
        self.assertFalse(final.passed)
        self.assertEqual(app.suggest_failure_mode(final), "HO - High offset")
        self.assertTrue(harness.last_offset_settle.settled)

    def test_in_band_but_moving_is_recorded_as_a_pass_with_the_warning(self):
        device = FakeMeasurementDevice(offset_sequence=[0.70, 0.62, 0.50])
        harness, (_metrics, final, offset) = self.run_hardware(device)
        self.assertAlmostEqual(offset, 0.50)
        self.assertTrue(final.passed)
        self.assertTrue(app.is_offset_still_settling(final))
        self.assertFalse(harness.last_offset_settle.settled)

    def test_csv_row_carries_the_insertion_read_and_the_settle_telemetry(self):
        device = FakeMeasurementDevice(offset_sequence=[1.31, 1.24, 1.18])
        harness, (_metrics, final, _offset) = self.run_hardware(device)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "batch.csv"
            app.append_result_csv(
                path,
                batch_number="B7",
                sensor_number=1,
                sensor_id="B7-1",
                tester_name="Operator",
                filter_setup=app.DEFAULT_FILTER_SETUP,
                pwm_channel=app.EMITTER_PWM_CHANNEL,
                pwm_hz=app.EMITTER_PWM_FREQUENCY_HZ,
                pwm_duty=app.EMITTER_PWM_DUTY_CYCLE,
                final_result=final,
                comment="",
                snapshot_paths=[],
                offset_initial_v=harness.last_offset_initial_v,
                offset_settle=harness.last_offset_settle,
            )
            rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(float(row["offset_v"]), 1.18)
        self.assertAlmostEqual(float(row["offset_initial_v"]), 1.31)
        self.assertEqual(row["offset_settled"], "YES")
        self.assertEqual(row["offset_settle_reads"], "1")
        self.assertAlmostEqual(float(row["offset_settle_delta_v"]), 0.06, places=5)

    def test_batch_started_before_the_new_columns_keeps_its_header(self):
        # Existing batch CSVs must stay aligned when a column is added.
        device = FakeMeasurementDevice()
        harness, (_metrics, final, _offset) = self.run_hardware(device)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "old_batch.csv"
            old_header = [
                name for name in app.CSV_FIELDS
                if not name.startswith("offset_initial")
                and not name.startswith("offset_settle")
            ]
            with path.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=old_header).writeheader()
            app.append_result_csv(
                path,
                batch_number="B8",
                sensor_number=1,
                sensor_id="B8-1",
                tester_name="Operator",
                filter_setup=app.DEFAULT_FILTER_SETUP,
                pwm_channel=app.EMITTER_PWM_CHANNEL,
                pwm_hz=app.EMITTER_PWM_FREQUENCY_HZ,
                pwm_duty=app.EMITTER_PWM_DUTY_CYCLE,
                final_result=final,
                comment="",
                snapshot_paths=[],
                offset_initial_v=harness.last_offset_initial_v,
                offset_settle=harness.last_offset_settle,
            )
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.reader(handle)
                header = next(reader)
                row = next(reader)
        self.assertEqual(header, old_header)
        self.assertEqual(len(row), len(old_header))

    def test_simulator_settling_case_starts_high_and_ends_in_band(self):
        self.assertIn(app.SIM_CASE_OFFSET_SETTLES, app.SIM_CASES)
        harness = MeasurementHarness(FakeMeasurementDevice())
        _metrics, final, offset = app.EmitterTesterApp._simulate_measurement(
            harness,
            app.DEFAULT_FILTER_SETUP,
            app.SIM_CASE_OFFSET_SETTLES,
            False,
            app.WAVEFORM_INPUT_RANGE_V,
            False,
            harness.measure_token,
            lambda callback: callback(),
        )
        self.assertAlmostEqual(harness.last_offset_initial_v, app.SIM_OFFSET_SETTLE_START_V)
        self.assertGreater(harness.last_offset_initial_v, app.OFFSET_MAX_V)
        self.assertLessEqual(offset, app.OFFSET_MAX_V)
        self.assertTrue(final.passed)


if __name__ == "__main__":
    unittest.main()
