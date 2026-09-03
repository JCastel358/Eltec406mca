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

import eltec_405m22_esp32_tester as app  # noqa: E402
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


def stream_samples_for_peaks(peaks_v: list[float], *, samples_per_cycle: int = 1000):
    """Build a PWM stream whose robust upper peak follows ``peaks_v``.

    The default 1000 samples/cycle models the DUT's 1 Hz drive at 1000 S/s;
    pass ``samples_per_cycle=100`` for the reference unit's 10 Hz drive.
    """

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
        # Two filled slots: the first keeps the old level, the second takes
        # the new one, so the swallowed edge lands within gap/2 samples.
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
        self.assertEqual(app.results_root_dir().name, "405m22_esp32")
        self.assertEqual(
            app.results_root_dir().parent.name, "Eltec_405M22_Test_Results"
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
        for noise_column in (
            "noise_test_outcome",
            "noise_windows_total",
            "noise_windows_over",
            "noise_over_percent",
            "noise_worst_pp_mv",
            "noise_median_pp_mv",
            "noise_settle_s",
            "noise_capture_s",
            "noise_pp_limit_mv",
        ):
            self.assertIn(noise_column, app.CSV_FIELDS)
        self.assertIn(app.UNSTABLE_FAILURE_MODE, app.FAILURE_MODE_CHOICES)
        # A near-limit sensitivity is a PASS with a warning, never a failure mode.
        self.assertFalse(
            any("RETEST" in choice or "guard band" in choice for choice in app.FAILURE_MODE_CHOICES)
        )
        self.assertEqual(app.MAX_MEASUREMENT_ATTEMPTS, 3)
        # 405 M22 screening policy: 5-delta qualification and 10 measurement
        # cycles keep a 1 Hz test near half a minute instead of over one.
        self.assertEqual(app.DUT_STABILITY_CONFIRMATION_DELTAS, 5)
        self.assertEqual(app.SENSITIVITY_MEASUREMENT_CYCLES, 10)
        self.assertEqual(app.EMITTER_PWM_FREQUENCY_HZ, 1.0)
        self.assertEqual(app.EXPECTED_FREQUENCY_HZ, 1.0)
        self.assertEqual(app.STABILITY_TIMEOUT_S, 60.0)
        # The DUT screening delta was relaxed 0.100 -> 0.500 mV; the AIN1
        # reference unit keeps its own stricter 0.250 mV limit.
        self.assertEqual(SETTINGS.peak_delta_threshold_mv, 0.500)
        self.assertEqual(app.REFERENCE_PEAK_DELTA_THRESHOLD_MV, 0.250)
        self.assertEqual(SETTINGS.consecutive_deltas_required, 5)

    def test_tp412_offset_band_filters_and_battery_policy(self):
        # Firmware v2.0 (PGA gain 1, buffer off) unlocks the full TP412 band.
        self.assertEqual(app.OFFSET_MIN_V, 0.8)
        self.assertEqual(app.OFFSET_MAX_V, 3.0)
        self.assertEqual(app.WAVEFORM_INPUT_RANGE_V, 5.0)
        # No high-side plausibility rail: a railed AIN0 (~5 V full scale) is
        # the real high-offset failure signature and must record as HO, never
        # as a "no sensor" wiring error. Only near-0 V still means no sensor.
        self.assertFalse(hasattr(app, "SENSOR_OFFSET_MAX_PLAUSIBLE_V"))
        self.assertEqual(app.SENSOR_OFFSET_MIN_PLAUSIBLE_V, 0.05)
        # Exactly the three TP412 filter setups, min-max with max = 2 x min.
        self.assertEqual(
            app.FILTER_SPECS_MV,
            {"-625 filter": 5.99, "-628 filter": 4.22, "-629 filter": 4.92},
        )
        self.assertEqual(
            app.FILTER_RANGES_MV,
            {
                "-625 filter": (5.99, 11.98),
                "-628 filter": (4.22, 8.44),
                "-629 filter": (4.92, 9.84),
            },
        )
        self.assertEqual(app.DEFAULT_FILTER_SETUP, "-625 filter")
        # The shared engine must read the SAME dict object (in-place mutation),
        # or the combobox/simulator and the engine's gate would disagree.
        self.assertIs(app.FILTER_SPECS_MV, app._shared_engine.FILTER_SPECS_MV)
        # Battery monitoring is disabled: the 6.5 V battery drives the
        # emitters only, the 9 V sensor battery is not measurable on AIN7.
        self.assertFalse(app.BATTERY_MONITORING_ENABLED)
        # Noise-test policy constants.
        # TP412's 300 mV pk-pk sits behind the legacy bench amplifier chain;
        # the EFFECTIVE display factor was measured at ~700x by the
        # 2026-08-13 same-part cross-measurement (nominal sticker gain 4000
        # is demonstrably not the effective one), so the pin-level limit is
        # ~429 µV pk-pk, gated on the band-limited (1000/20 = 50 SPS) trace.
        # The allowed over-limit window fraction was tightened to 15% on
        # 2026-08-17 from the lot-500 fixture comparison (the old fixture's
        # one noise failure, 500-44, measured 4/20 windows over here).
        self.assertTrue(app.NOISE_TEST_ENABLED)
        self.assertEqual(app.NOISE_LEGACY_PP_LIMIT_MV, 300.0)
        self.assertEqual(app.NOISE_LEGACY_AMPLIFIER_GAIN, 4000.0)
        self.assertEqual(app.NOISE_EFFECTIVE_CHAIN_FACTOR, 700.0)
        self.assertAlmostEqual(app.NOISE_PP_LIMIT_MV, 300.0 / 700.0, places=9)
        self.assertEqual(app.NOISE_DECIMATION_FACTOR, 20)
        self.assertEqual(app.NOISE_MAX_OVER_FRACTION, 0.15)
        self.assertEqual(app.NOISE_CAPTURE_SECONDS, 20.0)

    @unittest.skipUnless(
        os.name == "posix", "runs the bash installer (Xubuntu production host)"
    )
    def test_launcher_installation_uses_only_v6_1_identities(self):
        installer = V6_1_DIR / "install_xubuntu_launcher.sh"
        run_script = V6_1_DIR / "run_eltec_405m22_esp32_tester.sh"
        self.assertIn(
            "eltec-405m22-esp32",
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
            v6_1_menu = applications / "com.eltec.405m22-esp32-tester.desktop"
            v6_1_desktop = desktop / "Eltec 405 M22 ESP32 Tester.desktop"
            self.assertTrue(v6_1_menu.exists())
            self.assertTrue(v6_1_desktop.exists())
            self.assertIn(
                "Name=Eltec 405 M22 ESP32 Tester",
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

    def test_windows_launchers_exist_and_use_only_405m22_identities(self):
        """The Windows launcher pair must mirror the Xubuntu one, isolated.

        Checked on every host (plain text reads) so a Xubuntu-only test run
        still catches a Windows launcher that drifted onto a 406MCA identity.
        """
        run_script = V6_1_DIR / "run_eltec_405m22_esp32_tester.cmd"
        installer = V6_1_DIR / "install_windows_launcher.ps1"
        self.assertTrue(run_script.is_file(), f"missing {run_script}")
        self.assertTrue(installer.is_file(), f"missing {installer}")

        run_text = run_script.read_text(encoding="utf-8")
        installer_text = installer.read_text(encoding="utf-8")

        self.assertIn("eltec_405m22_esp32_tester.py", run_text)
        self.assertIn("eltec-405m22-esp32", run_text)  # log dir, as on Xubuntu
        self.assertIn("ELTEC_PYTHON", run_text)  # same interpreter override
        self.assertIn("run_eltec_405m22_esp32_tester.cmd", installer_text)
        self.assertIn("Eltec 405 M22 ESP32 Tester", installer_text)

        # No 406MCA v6/v6.1 identity may leak into the 405 M22 launchers.
        for name, text in (("cmd", run_text), ("ps1", installer_text)):
            lowered = text.lower()
            for forbidden in ("406mca", "v6_esp32", "v6_1_esp32"):
                self.assertNotIn(forbidden, lowered, f"{forbidden} leaked into {name}")

    @unittest.skipUnless(sys.platform == "win32", "Windows-only launcher scripts")
    def test_windows_installer_script_parses(self):
        installer = V6_1_DIR / "install_windows_launcher.ps1"
        # Parse only - running it would touch the real Desktop/Start Menu.
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "$errors = $null;"
                "[void][System.Management.Automation.Language.Parser]::ParseFile("
                f"'{installer}', [ref]$null, [ref]$errors);"
                "if ($errors) { $errors | ForEach-Object { $_.Message }; exit 1 }",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only launcher scripts")
    def test_windows_launcher_reports_missing_interpreter(self):
        """A bad ELTEC_PYTHON must fail loudly and land in the launcher log."""
        run_script = V6_1_DIR / "run_eltec_405m22_esp32_tester.cmd"
        with tempfile.TemporaryDirectory() as temp_dir:
            local_appdata = Path(temp_dir)
            completed = subprocess.run(
                ["cmd", "/c", str(run_script)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "LOCALAPPDATA": str(local_appdata),
                    "ELTEC_PYTHON": str(local_appdata / "no-such-python.exe"),
                    # The message box blocks until dismissed; the log carries
                    # the same text, so suppress the dialog for the test.
                    "ELTEC_LAUNCHER_NO_DIALOG": "1",
                },
            )
            self.assertNotEqual(completed.returncode, 0)
            log_file = local_appdata / "eltec-405m22-esp32" / "launcher.log"
            self.assertTrue(log_file.is_file(), completed.stdout + completed.stderr)
            self.assertIn("ERROR:", log_file.read_text(encoding="utf-8"))

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
                noise_report=app.NoiseCaptureReport.skipped(
                    "DUT unstable - noise test skipped"
                ),
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
            self.assertEqual(row["stability_threshold_mv"], "0.500000")
            self.assertEqual(row["noise_test_outcome"], "SKIPPED")
            self.assertEqual(row["noise_windows_total"], "")
            self.assertEqual(row["noise_worst_pp_mv"], "")

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
                failed(3.2, "Offset out of range", "Unstable waveform")
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
                failed(1.2, "Sensitivity too low", "Signal-to-noise too low")
            ),
            "GO/D - Good offset/no signal",
        )
        self.assertEqual(
            app.suggest_failure_mode(failed(1.2, "Sensitivity too low")),
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

    def test_sensitivity_gate_uses_the_lot500_pairwise_calibration(self):
        # 2026-08-17 fixture comparison (lot 500, 46 paired parts): legacy/raw
        # ratio median 4.2973, regression 4.2853 -> factor 4.30. The gate is
        # enabled with the TP412 legacy limits unchanged.
        self.assertTrue(app.LOW_SENSITIVITY_FAILURE_ENABLED)
        self.assertEqual(app.SENSITIVITY_LEGACY_EQUIVALENT_FACTOR, 4.30)
        self.assertEqual(
            app.SENSITIVITY_CALIBRATION_ID, "405m22_tp412_lot500_pairwise_v1"
        )
        self.assertAlmostEqual(
            app.legacy_equivalent_sensitivity_mv(2.0), 8.6, places=6
        )
        # -625: legacy minimum 5.99 -> raw center 5.99/4.30 = 1.393, +/-0.10.
        self.assertEqual(
            app.sensitivity_raw_limits_mv(app.DEFAULT_FILTER_SETUP), (1.29, 1.49)
        )

        # Anchor cases from the comparison batch: 500-10 (raw 0.938, the old
        # fixture's low-sensitivity failure at 4.08 mV) must FAIL; a near-limit
        # reading still PASSES (it is inside the conversion factor's margin of
        # error) but carries a re-measure warning; a typical passer (raw ~2.0)
        # must PASS cleanly; a raw reading whose legacy equivalent tops the
        # TP412 max must FAIL high.
        cases = {
            0.938: (app.OUTCOME_FAIL, False),
            1.39: (app.OUTCOME_PASS, True),
            2.06: (app.OUTCOME_PASS, False),
            3.10: (app.OUTCOME_FAIL, False),  # 13.33 mV legacy > 11.98 TP412 max
        }
        for raw_mv, (expected, near_limit) in cases.items():
            with self.subTest(raw_mv=raw_mv):
                metrics = metrics_for_sensitivity(raw_mv)
                final = app.evaluate_result(1.2, metrics, app.DEFAULT_FILTER_SETUP)
                self.assertEqual(app.result_outcome(final), expected)
                self.assertEqual(app.is_sensitivity_near_limit(final), near_limit)
        near = app.evaluate_result(
            1.2, metrics_for_sensitivity(1.39), app.DEFAULT_FILTER_SETUP
        )
        self.assertTrue(near.passed)
        self.assertEqual(near.fail_reasons, [])
        self.assertEqual(
            app.sensitivity_gate_outcome(1.39, app.DEFAULT_FILTER_SETUP),
            app.SENSITIVITY_NEAR_LIMIT,
        )
        self.assertTrue(
            any(w.startswith(app.SENSITIVITY_NEAR_LIMIT_WARNING_PREFIX) for w in near.warnings)
        )
        self.assertEqual(app.suggest_failure_mode(near), "")
        low = app.evaluate_result(
            1.2, metrics_for_sensitivity(0.938), app.DEFAULT_FILTER_SETUP
        )
        self.assertEqual(app.suggest_failure_mode(low), "LS - Low sensitivity")
        self.assertTrue(
            any("legacy-equivalent 4.033" in reason for reason in low.fail_reasons)
        )

    def test_sensitivity_gate_preserves_the_unscaled_offset_gate(self):
        # 405 M22 TP412 offset band is 0.8-3.0 V; 0.18 V is a bad part
        # even though the sensitivity gate itself is disabled.
        final = app.evaluate_result(
            0.18, metrics_for_sensitivity(3.0), app.DEFAULT_FILTER_SETUP
        )

        self.assertFalse(final.passed)
        self.assertEqual(final.offset_v, 0.18)
        self.assertTrue(any("Offset out of range" in reason for reason in final.fail_reasons))
        self.assertFalse(any("Sensitivity too low" in reason for reason in final.fail_reasons))

        low_offset = app.evaluate_result(
            0.18, metrics_for_sensitivity(2.53), app.DEFAULT_FILTER_SETUP
        )
        self.assertEqual(app.result_outcome(low_offset), app.OUTCOME_FAIL)
        self.assertEqual(
            app.suggest_failure_mode(low_offset), "LO - Low offset"
        )

    def test_csv_records_raw_and_calibrated_sensitivity(self):
        final = app.evaluate_result(
            1.2, metrics_for_sensitivity(2.06), app.DEFAULT_FILTER_SETUP
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
                offset_initial_v=0.912,
            )
            with csv_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["pwm_hz"], "1")
        self.assertEqual(row["sensitivity_mv"], "2.060000")
        self.assertEqual(row["sensitivity_raw_mv"], "2.060000")
        # Factor 4.30 (lot-500 pairwise calibration): the legacy-equivalent
        # column carries raw x 4.30 and the guard-band outcome is recorded.
        self.assertEqual(row["sensitivity_legacy_equivalent_mv"], "8.858000")
        self.assertEqual(row["sensitivity_correction_factor"], "4.300000")
        self.assertEqual(
            row["sensitivity_calibration_id"], "405m22_tp412_lot500_pairwise_v1"
        )
        self.assertEqual(row["sensitivity_gate_outcome"], app.OUTCOME_PASS)
        self.assertEqual(row["sensitivity_raw_fail_below_mv"], "1.290000")
        self.assertEqual(row["sensitivity_raw_pass_above_mv"], "1.490000")
        self.assertEqual(row["offset_initial_v"], "0.91200")
        self.assertEqual(row["pass_fail"], app.OUTCOME_PASS)
        self.assertEqual(row["failure_mode_tag"], "")
        self.assertEqual(row["failure_mode_reason"], "")

    def test_official_signal_math_uses_only_ten_post_stability_cycles(self):
        # 1 Hz cycles at 1000 SPS = 1000 samples each. The robust peak is
        # constant, so qualification completes after the 5-delta run (cycle 6)
        # and the official window is the next 10 fresh cycles.
        waveform = [0.69]
        sync = [0.0]
        for cycle_number in range(1, 20):
            low = 0.60 if cycle_number <= 6 else 0.69
            waveform.extend([0.70] * 500 + [low] * 500)
            sync.extend([1.0] * 500 + [0.0] * 500)
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
            offset_v=1.2,
            input_range_v=app.WAVEFORM_INPUT_RANGE_V,
        )
        self.assertEqual(analysis.report.stabilization_cycle, 6)
        self.assertEqual(metrics.cycles_used, 10)
        self.assertEqual(len(metrics.cycle_pp_mv), 10)
        self.assertEqual(metrics.noise_cycles_used, 10)
        self.assertEqual(metrics.polarity, "NEGATIVE")
        self.assertAlmostEqual(metrics.sensitivity_mv, 10.0, places=6)


class ContinuousCaptureTests(unittest.TestCase):
    def test_stable_stream_stops_after_ten_fresh_cycles_and_drives_preview(self):
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
        self.assertEqual(len(analysis.measurement_cycles), 10)
        # The simulated startup drift needs ~12 one-second cycles to fall
        # below the 0.5 mV threshold, plus the 5-delta confirmation run.
        self.assertGreater(analysis.report.stabilization_elapsed_s, 15.0)
        self.assertGreater(len(waveform), 25000)
        self.assertEqual(len(waveform), len(sync))
        self.assertEqual(rate, 1000.0)
        self.assertGreater(len(progress), 1)
        self.assertTrue(previews)
        self.assertLessEqual(previews[-1][0], app.STREAM_PREVIEW_MAX_SAMPLES)
        self.assertEqual(rig.stop_calls, 1)
        self.assertFalse(rig.is_streaming)

    def test_production_reader_retries_with_the_same_five_ten_windows(self):
        rig = FakeLowLevelRig()
        # Attempt 1 qualifies on cycle 6, then gets kicked by cycle 10.
        # Attempt 2 requalifies over cycles 11-15 and measures cycles 16-25.
        # The 1.0 mV step is twice the tracked 0.500 mV DUT threshold.
        peaks = [0.7000] * 9 + [0.7010] * 16
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
            offset_v=1.2,
            input_range_v=app.WAVEFORM_INPUT_RANGE_V,
        )

        self.assertTrue(analysis.report.measurement_complete)
        self.assertEqual(analysis.report.measurement_attempt, 2)
        self.assertEqual(analysis.report.measurement_failures, 1)
        self.assertEqual(analysis.report.active_confirmation_count, 5)
        self.assertEqual(analysis.report.measurement_cycles_required, 10)
        self.assertEqual(len(analysis.measurement_cycles), 10)
        self.assertEqual(metrics.cycles_used, 10)
        self.assertEqual(len(metrics.cycle_pp_mv), 10)
        self.assertEqual(analysis.report.data_source, "esp32_405m22")
        self.assertTrue(any(item.report.measurement_attempt == 2 for item in progress))
        self.assertEqual(rig.stop_calls, 1)

    def test_reference_stream_uses_dedicated_delta_then_five_fresh_cycles(self):
        rig = FakeLowLevelRig("Known good")
        # The AIN1 reference unit is a 406MCA sensor driven at its qualified
        # 10 Hz (100-sample cycles at 1000 S/s), not the DUT's 1 Hz.
        rig._samples = stream_samples_for_peaks(
            [0.700] * 15, samples_per_cycle=100
        )

        _waveform, _sync, _rate, analysis = rig.read_reference_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic(),
        )

        self.assertTrue(analysis.report.measurement_complete)
        # The reference gate ignores the (relaxed) tracked DUT threshold.
        self.assertEqual(SETTINGS.peak_delta_threshold_mv, 0.500)
        self.assertEqual(analysis.report.configured_threshold_mv, 0.250)
        self.assertEqual(analysis.report.configured_confirmation_count, 5)
        self.assertEqual(len(analysis.measurement_cycles), 5)
        self.assertEqual(analysis.report.data_source, "esp32_reference")
        self.assertEqual(rig.started_channels, ["ref"])

    def test_reference_micro_gap_is_refilled_so_sync_validation_passes(self):
        # A 2-sample drop inside a 100-sample 10 Hz cycle used to read as
        # 1000/98 = 10.204 Hz and fail the ±0.1 Hz sync check as a fake
        # "check firmware and GPIO25" rig error (seen on the Windows host,
        # especially with the laptop charger's EMI on the USB link). The
        # firmware timestamps expose the gap, and the filler rebuilds the
        # missing slots before any index-based analysis runs.
        rig = FakeLowLevelRig("Known good")
        bank = with_timestamps(
            stream_samples_for_peaks([0.700] * 15, samples_per_cycle=100)
        )
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
        # non-retried "check firmware and GPIO25" HardwareNotReadyError.
        rig = FakeLowLevelRig("Known good")
        bank = with_timestamps(
            stream_samples_for_peaks([0.700] * 15, samples_per_cycle=100)
        )
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

    def test_never_stable_stream_times_out_at_sixty_seconds(self):
        rig = FakeLowLevelRig("Never stabilizes")
        waveform, _sync, _rate, analysis = rig.read_waveform_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic() + 1.0,
        )
        self.assertFalse(analysis.report.stabilized)
        self.assertTrue(analysis.report.timed_out)
        self.assertEqual(analysis.report.measurement_cycle_count, 0)
        self.assertGreaterEqual(len(waveform), 60000)
        self.assertLess(len(waveform), 62000)
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

    def test_streaming_accepts_stability_closing_exactly_at_sixty_seconds(self):
        rig = FakeLowLevelRig()
        rig.STREAM_CHUNK_SAMPLES = 100
        # 1 Hz cycles of 1000 samples. Peaks alternate by 1 mV until cycle 54;
        # the 5-delta confirmation run then closes exactly at the 60.0 s
        # deadline (cycle ending at sample 60,000) and must be accepted.
        sample_count = 70_001
        rig._samples = []
        for index in range(sample_count):
            physical_cycle = index // 1000
            if physical_cycle < 54:
                peak_v = 0.700 if physical_cycle % 2 == 0 else 0.701
            else:
                # 0.700 differs from the last alternating cycle (odd -> 0.701),
                # so the first small delta is the (54, 55) pair and the 5-delta
                # run closes exactly on the 60.0 s deadline.
                peak_v = 0.700
            rig._samples.append(
                SimpleNamespace(
                    volts=peak_v,
                    sync=1 if (index % 1000) < 500 else 0,
                )
            )

        waveform, _sync, _rate, analysis = rig.read_waveform_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            settings=SETTINGS,
            pwm_started_monotonic=time.monotonic() + 1.0,
        )
        self.assertTrue(analysis.report.measurement_complete)
        self.assertAlmostEqual(analysis.report.stabilization_elapsed_s, 60.0)
        self.assertEqual(analysis.report.measurement_cycle_count, 10)
        self.assertGreater(len(waveform), 69_900)

    def test_integrity_error_and_cancellation_both_stop_stream(self):
        # 25 gaps / ~25 missing samples exceed the micro-gap budget by every
        # measure, so the capture is rejected with nothing recorded and the
        # dedicated subclass marks the failure as a transient transport
        # problem, letting production callers retry the reading.
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
        # residual Windows CP210x behavior after the buffer/drain fix) must
        # not reject a whole multi-second capture.
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

    def test_noise_stream_that_goes_silent_is_a_retryable_attributed_stall(self):
        # The bank ends 2.5 s in - before the 3 s minimum quiet wait - so
        # read_stream() returns nothing: the 2026-09-02 bench symptom. The
        # capture stops the stream itself, reads the STREAM,END count and
        # raises the retryable, attributed error (2026-09-03).
        rig = FakeLowLevelRig()
        rig._samples = self.flat_noise_bank(2500)
        with self.assertRaises(app.StreamStalledError) as caught:
            rig.read_noise_capture()
        self.assertEqual(rig.stop_calls, 1)
        self.assertFalse(rig.is_streaming)
        self.assertIsInstance(caught.exception, app.StreamIntegrityError)
        self.assertEqual(caught.exception.received, 2500)
        self.assertIn("noise stream stalled after 2500 samples", str(caught.exception))
        # FakeDiagnostics answers STREAM,STOP with the host count: the board
        # is alive but produced nothing more.
        self.assertEqual(caught.exception.kind, app.STALL_KIND_BOARD_SILENT)

    def test_driven_stream_that_goes_silent_is_a_retryable_attributed_stall(self):
        rig = FakeLowLevelRig()
        rig._samples = rig._samples[:3000]  # ends long before stability
        with self.assertRaises(app.StreamStalledError) as caught:
            rig.read_waveform_until_stable(
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                settings=SETTINGS,
                pwm_started_monotonic=time.monotonic(),
            )
        self.assertEqual(rig.stop_calls, 1)
        self.assertIn("waveform stream stalled after 3000/", str(caught.exception))
        self.assertEqual(caught.exception.kind, app.STALL_KIND_BOARD_SILENT)

        frame_rig = FakeLowLevelRig()
        frame_rig._samples = frame_rig._samples[:500]
        with self.assertRaises(app.StreamStalledError):
            frame_rig.read_waveform_frame(3, app.WAVEFORM_INPUT_RANGE_V)
        self.assertEqual(frame_rig.stop_calls, 1)

    @staticmethod
    def flat_noise_bank(samples: int, *, offset_v: float = 1.2):
        """A settled bank: constant offset plus a ±10 µV alternation."""
        return [
            SimpleNamespace(
                volts=offset_v + (0.00001 if index % 2 == 0 else -0.00001),
                sync=0,
            )
            for index in range(samples)
        ]

    def test_noise_capture_starts_at_min_wait_when_level_is_settled(self):
        rig = FakeLowLevelRig()
        rig._samples = self.flat_noise_bank(30000)
        progress_flags: list[tuple[bool, float]] = []
        (noise, left_context, right_context, rate, wait_s, elapsed_s,
         settled) = rig.read_noise_capture(
            progress=lambda capturing, elapsed, delta=None: progress_flags.append(
                (capturing, elapsed)
            ),
        )
        self.assertEqual(rate, 1000.0)
        self.assertTrue(settled)
        self.assertEqual(wait_s, app.NOISE_WAIT_BEFORE_CAPTURE_S)
        self.assertEqual(len(noise), int(app.NOISE_CAPTURE_SECONDS * 1000))
        self.assertGreaterEqual(
            elapsed_s, wait_s + app.NOISE_CAPTURE_SECONDS - 1e-9
        )
        # The retained window starts exactly after the discarded quiet wait.
        wait_samples = int(app.NOISE_WAIT_BEFORE_CAPTURE_S * 1000)
        expected = np.asarray(
            [
                sample.volts
                for sample in rig._samples[
                    wait_samples : wait_samples + len(noise)
                ]
            ]
        )
        np.testing.assert_array_equal(np.asarray(noise), expected)
        # 2026-08-31: the FIR's edge context comes back alongside - the
        # quiet-wait tail on the left, extra streamed samples on the right.
        self.assertEqual(len(left_context), app.NOISE_EDGE_CONTEXT_SAMPLES)
        self.assertEqual(len(right_context), app.NOISE_EDGE_CONTEXT_SAMPLES)
        np.testing.assert_array_equal(
            np.asarray(left_context),
            np.asarray(
                [
                    sample.volts
                    for sample in rig._samples[
                        wait_samples
                        - app.NOISE_EDGE_CONTEXT_SAMPLES : wait_samples
                    ]
                ]
            ),
        )
        # Progress reports the quiet wait first, then the capture phase.
        self.assertFalse(progress_flags[0][0])
        self.assertTrue(progress_flags[-1][0])
        self.assertEqual(rig.stop_calls, 1)

    def test_noise_capture_waits_for_a_settling_dc_level(self):
        # 2 mV/s ramp for the first 4 s, then flat: the capture must not
        # start until the per-second mean deltas fall inside the settle
        # threshold, and the retained window must exclude the ramp.
        rig = FakeLowLevelRig()
        bank = self.flat_noise_bank(30000)
        for index in range(4000):
            bank[index] = SimpleNamespace(
                volts=1.2 + 0.002 * (4000 - index) / 1000.0, sync=0
            )
        rig._samples = bank
        (noise, _left, _right, _rate, wait_s, _elapsed_s,
         settled) = rig.read_noise_capture()
        self.assertTrue(settled)
        self.assertGreater(wait_s, app.NOISE_WAIT_BEFORE_CAPTURE_S)
        self.assertLess(wait_s, app.NOISE_WAIT_MAX_S)
        self.assertEqual(len(noise), int(app.NOISE_CAPTURE_SECONDS * 1000))
        # Everything retained comes from the settled region.
        self.assertGreaterEqual(wait_s * 1000, 4000)

    def test_noise_capture_deadline_measures_anyway_without_failing(self):
        # A DC level that never stops moving (1 mV/s throughout): the wait
        # caps at NOISE_WAIT_MAX_S, the capture happens, and the flag says
        # the baseline never settled - nothing raises.
        rig = FakeLowLevelRig()
        rig._samples = [
            SimpleNamespace(volts=1.2 + 0.001 * index / 1000.0, sync=0)
            for index in range(45000)
        ]
        (noise, _left, _right, _rate, wait_s, _elapsed_s,
         settled) = rig.read_noise_capture()
        self.assertFalse(settled)
        self.assertEqual(wait_s, app.NOISE_WAIT_MAX_S)
        self.assertEqual(len(noise), int(app.NOISE_CAPTURE_SECONDS * 1000))


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
        error = self.stall(stall_diagnostics(firmware_samples_sent=None), what="noise")
        self.assertEqual(error.kind, app.STALL_KIND_NO_REPLY)
        self.assertIn("ESP32 noise stream stalled after 37624 samples", str(error))
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
        # The restarted counter alone is decisive - the boot text arrives
        # garbled (ROM output at 115200 baud) and may not include READY.
        error = self.stall(stall_diagnostics(firmware_samples_sent=0))
        self.assertEqual(error.kind, app.STALL_KIND_BOARD_RESET)
        self.assertIn("came back at 0", str(error))

    def test_backlog_or_firmware_ahead_means_the_computer_froze(self):
        # The samples were queued in the OS buffer all along: draining the
        # stop reply collected 2.3 s of them.
        error = self.stall(
            stall_diagnostics(
                received_samples=39900, drained_samples=2276, firmware_samples_sent=39905
            )
        )
        self.assertEqual(error.kind, app.STALL_KIND_HOST)
        self.assertIn("firmware 39905 vs host 37624", str(error))
        self.assertIn("2276 samples = 2.3 s were still waiting", str(error))
        self.assertIn("the rig did not stop", str(error))
        # Or the buffer overflowed and they are gone - the board still
        # counted them, which is what matters for the attribution.
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
        note = self.validate(received_samples=20000)
        self.assertIsNone(note)

    def test_single_micro_gap_with_matching_count_deficit_is_tolerated(self):
        # The exact field failure from the bench: 1 gap, ~6 missing samples,
        # host/firmware 16533/16539. This must record, not error.
        note = self.validate(
            received_samples=16533,
            timestamp_gap_count=1,
            estimated_missing_samples=6,
            firmware_samples_sent=16539,
        )
        self.assertIsNotNone(note)
        self.assertIn("1 serial micro-gap", note)
        self.assertIn("~6 of 16533", note)

    def test_too_many_gaps_or_samples_still_reject(self):
        with self.assertRaisesRegex(app.StreamIntegrityError, "timestamp gaps"):
            self.validate(
                received_samples=20000,
                timestamp_gap_count=app.STREAM_MAX_MICRO_GAPS + 1,
                estimated_missing_samples=4,
            )
        with self.assertRaisesRegex(app.StreamIntegrityError, "timestamp gaps"):
            self.validate(
                received_samples=20000,
                timestamp_gap_count=1,
                estimated_missing_samples=app.STREAM_MAX_MISSING_SAMPLES + 1,
            )

    def test_duplicates_are_never_tolerated_even_with_small_gaps(self):
        # The historical Windows driver-overflow signature (gaps + duplicate
        # re-delivery) must keep failing loudly.
        with self.assertRaisesRegex(app.StreamIntegrityError, "duplicate"):
            self.validate(
                received_samples=14648,
                timestamp_gap_count=1,
                estimated_missing_samples=6,
                duplicate_timestamps=80,
            )

    def test_count_deficit_beyond_budget_and_surplus_both_reject(self):
        with self.assertRaisesRegex(app.StreamIntegrityError, "counts differ"):
            self.validate(
                received_samples=20000,
                firmware_samples_sent=20000 + app.STREAM_MAX_MISSING_SAMPLES + 1,
            )
        # More received than sent means duplicated/corrupted records.
        with self.assertRaisesRegex(app.StreamIntegrityError, "counts differ"):
            self.validate(
                received_samples=20010,
                firmware_samples_sent=20000,
            )


class ReferenceCalibrationTests(unittest.TestCase):
    def test_stream_retry_helper_retries_transient_integrity_failures_only(self):
        # One transient glitch: the reading succeeds on the retry.
        attempts: list[int] = []

        def flaky(attempt: int) -> float:
            attempts.append(attempt)
            if len(attempts) == 1:
                raise app.StreamIntegrityError("17 timestamp gaps")
            return 5.5

        notified: list[tuple[int, str]] = []
        result = app.call_with_stream_retries(
            flaky, on_retry=lambda attempt, exc: notified.append((attempt, str(exc)))
        )
        self.assertEqual(result, 5.5)
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(notified, [(1, "17 timestamp gaps")])

        # A stall (2026-09-03) is a StreamIntegrityError too: restarted the
        # same bounded way, and the callback receives the attributed error.
        stalls: list[int] = []
        seen: list[Exception] = []

        def stalled_once(attempt: int) -> float:
            stalls.append(attempt)
            if len(stalls) == 1:
                raise app.StreamStalledError(
                    "ESP32 noise stream stalled after 37624 samples. [host-stall]",
                    kind=app.STALL_KIND_HOST,
                    received=37624,
                )
            return 6.5

        self.assertEqual(
            app.call_with_stream_retries(
                stalled_once, on_retry=lambda _attempt, exc: seen.append(exc)
            ),
            6.5,
        )
        self.assertEqual(stalls, [0, 1])
        self.assertEqual([exc.kind for exc in seen], [app.STALL_KIND_HOST])

        # A persistent stream problem still fails after the bounded retries.
        persistent: list[int] = []

        def always_bad(attempt: int) -> float:
            persistent.append(attempt)
            raise app.StreamIntegrityError("80 duplicate timestamps")

        with self.assertRaises(app.StreamIntegrityError):
            app.call_with_stream_retries(always_bad, on_retry=lambda _n, _e: None)
        self.assertEqual(
            len(persistent), app.REFERENCE_READING_STREAM_RETRIES + 1
        )

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

    def test_missing_405m22_calibration_never_reads_406mca_baselines(self):
        # The 406MCA v6/v6.1 baselines were captured with a 10 Hz chop; the
        # 405 M22 build must require its own 1 Hz calibration instead of
        # silently reading one of them.
        self.assertFalse(hasattr(app, "v6_reference_calibration_path"))
        with tempfile.TemporaryDirectory() as temp_dir:
            local = Path(temp_dir) / "405m22" / "reference.json"
            with mock.patch.object(
                app, "reference_calibration_path", return_value=local
            ):
                loaded = app.load_reference_calibration()

            self.assertIsNone(loaded)
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


def synthetic_noise_waveform(
    *,
    offset_v=1.2,
    windows=20,
    noisy_windows=0,
    rate_hz=1000.0,
    window_s=1.0,
    quiet_pp_v=0.000020,
    noisy_pp_v=0.002000,
):
    """Emitter-off capture: ``noisy_windows`` leading 1 s windows exceed the
    ~429 µV pin-level limit (legacy 300 mV / ~700x effective chain).

    Quiet windows are 20 µV pk-pk, noisy windows 2 mV pk-pk. The square is
    high for the first and last window quarters and low in between: its
    fundamental is deep inside the anti-alias FIR's ~22 Hz passband (full
    pk-pk survives, read ~18% above raw by the flat passband + band-edge
    Gibbs ripple) and the pattern is symmetric about the window center, so
    the per-window least-squares detrend fits a zero slope.
    """
    window_samples = int(round(window_s * rate_hz))
    chunks = []
    positions = np.arange(window_samples)
    high = (positions < window_samples // 4) | (
        positions >= 3 * window_samples // 4
    )
    for index in range(windows):
        amplitude = (noisy_pp_v if index < noisy_windows else quiet_pp_v) / 2.0
        square = np.where(high, amplitude, -amplitude)
        chunks.append(offset_v + square)
    return np.concatenate(chunks)


class FakeMeasurementDevice:
    def __init__(
        self,
        *,
        battery=6.2,
        offset=1.2,
        offset_sequence=None,
        case_name="Known good",
        activation_time=None,
        deactivation_time=None,
        configure_error=None,
        error=None,
        reference_mv=100.0,
        reference_error=None,
        noisy_windows=0,
        noise_errors=None,
    ):
        self.battery = battery
        # Exceptions raised by successive read_noise_capture() calls before
        # a capture succeeds (a stall or integrity failure the app restarts).
        self.noise_errors = list(noise_errors) if noise_errors else []
        self.offset = offset
        # Successive read_offset_voltage() values (last one repeats), for
        # parts whose offset drifts/rails between the offset gate and the
        # reference-failure AIN0 re-check.
        self.offset_sequence = list(offset_sequence) if offset_sequence else []
        self.case_name = case_name
        self.activation_time = activation_time
        self.deactivation_time = deactivation_time
        self.configure_error = configure_error
        self.error = error
        self.reference_mv = reference_mv
        self.reference_error = reference_error
        self.noisy_windows = noisy_windows
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

    def read_noise_capture(self, **kwargs):
        self.calls.append("noise")
        if self.noise_errors:
            raise self.noise_errors.pop(0)
        rate = 1000.0
        wait_s = app.NOISE_WAIT_BEFORE_CAPTURE_S
        capture_seconds = float(
            kwargs.get("capture_seconds", app.NOISE_CAPTURE_SECONDS)
        )
        self.noise_capture_seconds_seen = capture_seconds
        if kwargs.get("progress"):
            # Once during the adaptive quiet wait, once while capturing.
            kwargs["progress"](False, wait_s / 2.0, 0.5)
            kwargs["progress"](True, wait_s + 5.0, 0.01)
        waveform = synthetic_noise_waveform(
            offset_v=self.offset,
            windows=int(round(capture_seconds)),
            noisy_windows=self.noisy_windows,
        )
        if kwargs.get("preview"):
            kwargs["preview"](waveform[-500:], np.zeros(500))
        # Edge context: one quiet window continues the synthetic pattern on
        # both sides (period 1000 samples), like the real rig's quiet-wait
        # tail and post-capture stream (2026-08-31).
        edge = app.NOISE_EDGE_CONTEXT_SAMPLES
        quiet = synthetic_noise_waveform(
            offset_v=self.offset, windows=1, noisy_windows=0
        )
        return (
            waveform,
            quiet[-edge:],
            quiet[:edge],
            rate,
            wait_s,
            wait_s + len(waveform) / rate,
            True,
        )


class MeasurementHarness:
    def __init__(self, device):
        self.device = device
        self.hardware_lock = threading.Lock()
        self.measure_token = 7
        self.last_capture_report = None
        self.last_noise_report = None
        self.last_noise_metrics = None
        self.last_reference_check_mv = None
        self.last_offset_initial_v = None
        self.noise_raw_auto_saved = False
        self.reference_calibration_error = None
        self.reference_calibration = app.build_reference_calibration(
            [100.0] * app.REFERENCE_CALIBRATION_READINGS
        )
        self.stability_settings = SETTINGS
        self.callback_events = []
        self.progress_events = []
        self.preview_count = 0
        self.stream_retries = []
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

    def _capture_reference_reading(self, device, **_kwargs):
        device.calls.append("reference")
        if getattr(device, "reference_error", None) is not None:
            raise device.reference_error
        return device.reference_mv

    def on_battery_update(self, value, error=None):
        self.callback_events.append(("battery", value, error))

    def auto_save_noise_capture(self, token, report):
        # Recorder only - the real app writes the raw capture to disk here.
        self.noise_raw_auto_saved = True
        self.callback_events.append(
            ("auto_noise_capture", token, report.windows_over)
        )

    def on_initial_offset(self, token, value):
        self.last_offset_initial_v = value
        self.callback_events.append(("initial_offset", token, value))

    def on_offset_update(self, token, value):
        self.callback_events.append(("offset", token, value))

    def set_measure_status(self, token, text):
        self.callback_events.append(("status", token, text))

    def set_measure_progress(self, token, step, total, label, fraction):
        self.progress_events.append((step, total, label, fraction))

    def set_preview_display(self, token, *, noise):
        self.callback_events.append(("preview_display", token, noise))

    def on_preview_frame(self, token, waveform, sync):
        self.preview_count += 1
        self.callback_events.append(("preview", token, len(waveform), len(sync)))


class HardwareWorkflowTests(unittest.TestCase):
    """Full hardware sequence WITH the reference gate.

    Production currently ships REFERENCE_GATE_ENABLED = False (the shared
    dual op-amp buffer lets the DUT couple into AIN1), but the gate machinery
    must stay working for the channel-isolated buffer board, so these tests
    run with the gate forced on. ReferenceGateDisabledTests covers the
    shipping default.
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
        # Battery is never read (monitoring disabled); TP412 order: the
        # quick offset check runs FIRST (emitter off), then the reference
        # gate, then the emitter-off noise test, then the driven sensitivity
        # capture, then the settled-offset re-read that carries the verdict.
        self.assertEqual(
            device.calls,
            [
                "pwm_off", "offset", "pwm_on", "reference", "pwm_off",
                "noise", "pwm_on", "capture", "pwm_off", "offset",
            ],
        )
        self.assertEqual(metrics.cycles_used, 10)
        self.assertTrue(metrics.stabilized)
        self.assertTrue(final.passed)
        self.assertEqual(offset, 1.2)
        self.assertEqual(harness.last_offset_initial_v, 1.2)
        self.assertGreaterEqual(harness.preview_count, 1)
        self.assertEqual(harness.last_capture_report.data_source, "esp32_405m22")
        self.assertEqual(harness.last_reference_check_mv, 100.0)
        self.assertEqual(harness.last_noise_report.outcome, app.OUTCOME_PASS)
        self.assertEqual(harness.last_noise_report.windows_total, 20)
        self.assertEqual(harness.last_noise_report.windows_over, 0)
        self.assertIsNotNone(harness.last_noise_metrics)

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
                "AIN0 was checked at 1.200 V",
            ):
                self.run_hardware(device)

        # Offset gate first (in-band), then the reference gate fails and
        # AIN0 is re-read before the calibration is invalidated.
        self.assertEqual(
            device.calls,
            ["pwm_off", "offset", "pwm_on", "reference", "pwm_off", "offset"],
        )
        self.assertFalse(save.call_args.args[0].valid)

    def test_high_offset_fails_immediately_without_touching_the_reference(self):
        # The real high-offset signature: AIN0 railed at the ~5 V ADC full
        # scale (also covers merely-above-band values). The part fails HO on
        # the spot — the reference gate, noise and sensitivity never run, so
        # the reference calibration cannot be invalidated by a bad part.
        for railed_offset in (3.2, 3.8, 5.0):
            with self.subTest(offset=railed_offset):
                device = FakeMeasurementDevice(offset=railed_offset)
                with mock.patch.object(app, "save_reference_calibration") as save:
                    harness, (_metrics, final, offset) = self.run_hardware(device)

                save.assert_not_called()
                self.assertTrue(harness.reference_calibration.valid)
                self.assertIsNone(harness.last_reference_check_mv)
                self.assertEqual(offset, railed_offset)
                self.assertFalse(final.passed)
                self.assertIsNone(final.sensitivity_mv)
                self.assertEqual(app.suggest_failure_mode(final), "HO - High offset")
                self.assertEqual(device.calls, ["pwm_off", "offset"])
                self.assertIsNone(harness.last_capture_report)
                self.assertEqual(harness.last_noise_report.outcome, "SKIPPED")
                self.assertIn(
                    "offset out of range", harness.last_noise_report.skip_reason
                )

    def test_high_offset_recheck_suppresses_high_reference_invalidation(self):
        # The part passes the offset gate at 1.2 V but rails to 5 V by the
        # time the reference gate fails high: the re-check pins the blame on
        # the part (HO FAIL), and the calibration survives.
        device = FakeMeasurementDevice(
            reference_mv=150.0, offset_sequence=[1.2, 5.0]
        )
        with mock.patch.object(app, "save_reference_calibration") as save:
            harness, (_metrics, final, offset) = self.run_hardware(device)

        save.assert_not_called()
        self.assertTrue(harness.reference_calibration.valid)
        self.assertEqual(harness.last_reference_check_mv, 150.0)
        self.assertEqual(offset, 5.0)
        self.assertFalse(final.passed)
        self.assertIsNone(final.sensitivity_mv)
        self.assertEqual(app.suggest_failure_mode(final), "HO - High offset")
        self.assertEqual(
            device.calls,
            ["pwm_off", "offset", "pwm_on", "reference", "pwm_off", "offset"],
        )
        self.assertIsNone(harness.last_capture_report)
        self.assertEqual(harness.last_noise_report.outcome, "SKIPPED")

    def test_high_offset_recheck_also_spares_calibration_on_low_reference(self):
        # 2026-08-13 policy: ANY reference-gate failure with a high-offset
        # AIN0 re-check condemns the part, not the fixture — the reference
        # gate re-runs on the next part, so a genuine emitter fault is still
        # caught one part later.
        device = FakeMeasurementDevice(
            reference_mv=74.0, offset_sequence=[1.2, 5.0]
        )
        with mock.patch.object(app, "save_reference_calibration") as save:
            harness, (_metrics, final, offset) = self.run_hardware(device)

        save.assert_not_called()
        self.assertTrue(harness.reference_calibration.valid)
        self.assertEqual(offset, 5.0)
        self.assertEqual(app.suggest_failure_mode(final), "HO - High offset")
        self.assertEqual(
            device.calls,
            ["pwm_off", "offset", "pwm_on", "reference", "pwm_off", "offset"],
        )

    def test_high_offset_recheck_spares_calibration_on_unstable_reference(self):
        # An AIN1 capture that cannot stabilize while the part has railed
        # high is the same interference pattern: fail the part, keep the
        # calibration.
        device = FakeMeasurementDevice(
            reference_error=app.ReferenceCaptureError("no five stable cycles"),
            offset_sequence=[1.2, 5.0],
        )
        with mock.patch.object(app, "save_reference_calibration") as save:
            harness, (_metrics, final, offset) = self.run_hardware(device)

        save.assert_not_called()
        self.assertTrue(harness.reference_calibration.valid)
        self.assertIsNone(harness.last_reference_check_mv)
        self.assertEqual(offset, 5.0)
        self.assertEqual(app.suggest_failure_mode(final), "HO - High offset")
        self.assertEqual(
            device.calls,
            ["pwm_off", "offset", "pwm_on", "reference", "pwm_off", "offset"],
        )

    def test_unstable_reference_with_normal_offset_still_invalidates_gate(self):
        device = FakeMeasurementDevice(
            reference_error=app.ReferenceCaptureError("no five stable cycles"),
        )
        with mock.patch.object(app, "save_reference_calibration") as save:
            with self.assertRaisesRegex(
                app.ReferenceGateError,
                "could not establish a stable five-cycle reading",
            ):
                self.run_hardware(device)

        self.assertFalse(save.call_args.args[0].valid)
        self.assertEqual(
            device.calls,
            ["pwm_off", "offset", "pwm_on", "reference", "pwm_off", "offset"],
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
        # One preview frame from the driven capture plus one from the
        # emitter-off noise capture - identical regardless of the toggle.
        self.assertEqual(hidden_harness.preview_count, 2)
        self.assertEqual(visible_harness.preview_count, 2)

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
            "could not complete 5 consecutive stable deltas within 60.0 s",
            final.fail_reasons[-1].lower(),
        )

    def test_battery_is_never_read_and_implausible_offset_blocks(self):
        # Battery monitoring is disabled (nothing measurable on AIN7 since the
        # battery isolation): even a device reporting a dead battery is never
        # asked for it and the measurement completes.
        self.assertFalse(app.BATTERY_MONITORING_ENABLED)
        low = FakeMeasurementDevice(battery=5.7)
        harness, (_metrics, final, _offset) = self.run_hardware(low)
        self.assertTrue(final.passed)
        self.assertNotIn("battery", low.calls)

        # Only a near-0 V float still reads as "no sensor" (through the
        # buffer a missing part sits near ground). High readings are never
        # blocked — they are real high-offset failures (covered elsewhere).
        # The wake-up poll window is zeroed here; the wake behavior itself is
        # covered by test_below_floor_offset_gets_a_wake_up_poll_window.
        missing = FakeMeasurementDevice(offset=0.02)
        with mock.patch.object(app, "OFFSET_WAKE_TIMEOUT_S", 0.0):
            with self.assertRaisesRegex(app.HardwareNotReadyError, "No sensor detected"):
                self.run_hardware(missing)
        self.assertEqual(missing.calls, ["pwm_off", "offset"])

    def test_below_floor_offset_gets_a_wake_up_poll_window(self):
        # Lot 500 (2026-08-17): a just-inserted part can read below the
        # no-sensor floor for a few seconds while its offset wakes up
        # (500-44's first attempt was wrongly rejected as an empty slot).
        # The check now polls before declaring the slot empty.
        device = FakeMeasurementDevice(offset_sequence=[0.02, 0.03, 1.2])
        with mock.patch.object(app.time, "sleep") as sleep:
            harness, (_metrics, final, _offset) = self.run_hardware(device)
        self.assertTrue(final.passed)
        self.assertGreaterEqual(sleep.call_count, 2)
        self.assertEqual(harness.last_offset_initial_v, 1.2)

    def test_capture_exception_still_turns_pwm_off(self):
        device = FakeMeasurementDevice(error=RuntimeError("serial lost"))
        with self.assertRaisesRegex(RuntimeError, "serial lost"):
            self.run_hardware(device)
        # The noise test already ran (new order); the failed driven capture
        # propagates right after the guaranteed PWM-off.
        self.assertEqual(
            device.calls,
            [
                "pwm_off", "offset", "pwm_on", "reference", "pwm_off",
                "noise", "pwm_on", "capture", "pwm_off",
            ],
        )

    def test_stalled_capture_is_restarted_by_the_app_and_logged(self):
        # 2026-09-03: a stall used to be a dead end (the technician pressed
        # Re-measure); it is now a StreamIntegrityError and gets the same
        # bounded restart as a micro-gap failure, with the attributed error
        # on the status line and in the attempts log.
        stall = app.StreamStalledError(
            "ESP32 noise stream stalled after 37624 samples (no sample for 2.3 s). "
            "The board kept sampling [host-stall]",
            kind=app.STALL_KIND_HOST,
            received=37624,
        )
        device = FakeMeasurementDevice(noise_errors=[stall])
        harness, (_metrics, final, _offset) = self.run_hardware(device)
        self.assertTrue(final.passed)
        self.assertEqual(device.calls.count("noise"), 2)
        statuses = [
            event[2] for event in harness.callback_events if event[0] == "status"
        ]
        self.assertTrue(
            any(text.startswith("Stream stalled during the noise capture") for text in statuses),
            statuses,
        )
        self.assertEqual(harness.stream_retries, [("noise capture", 1, stall)])

    def test_persistent_stall_fails_after_the_bounded_restarts_with_pwm_off(self):
        stall = app.StreamStalledError(
            "ESP32 waveform stream stalled after 3000/72001 samples. [board-silent]",
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

    def test_pwm_activation_error_still_turns_pwm_off(self):
        device = FakeMeasurementDevice(configure_error=RuntimeError("PWM acknowledgement lost"))
        with self.assertRaisesRegex(RuntimeError, "PWM acknowledgement lost"):
            self.run_hardware(device)
        self.assertEqual(
            device.calls,
            ["pwm_off", "offset", "pwm_on", "pwm_off"],
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


class NoiseWorkflowTests(unittest.TestCase):
    """TP412 emitter-off noise test: order, gate, fail-fast, progress ladder.

    Runs with the reference gate forced on (see HardwareWorkflowTests) so the
    historical four-step ladder assertions keep covering that configuration.
    """

    def setUp(self):
        patcher = mock.patch.object(app, "REFERENCE_GATE_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_noisy_sensor_fails_fast_without_a_sensitivity_capture(self):
        # 5 of 20 windows over the limit = 25% > the 15% allowance. The noise
        # test runs before the driven capture, so the noisy part is rejected
        # without spending any stabilization attempts on it.
        device = FakeMeasurementDevice(noisy_windows=5)
        harness, (_metrics, final, _offset) = self.run_hardware(device)

        self.assertFalse(final.passed)
        self.assertIsNone(final.sensitivity_mv)
        self.assertNotIn("capture", device.calls)
        self.assertIsNone(harness.last_capture_report)
        self.assertEqual(harness.last_noise_report.outcome, app.OUTCOME_FAIL)
        self.assertEqual(harness.last_noise_report.windows_over, 5)
        self.assertEqual(harness.last_noise_report.windows_total, 20)
        noise_reasons = [
            reason
            for reason in final.fail_reasons
            if reason.startswith(app.NOISE_FAIL_REASON_PREFIX)
        ]
        self.assertEqual(len(noise_reasons), 1)
        self.assertIn("5 of 20", noise_reasons[0])
        self.assertEqual(app.suggest_failure_mode(final), "N - Noisy")
        # The capture is preserved for the noisy-part snapshot.
        self.assertIsNotNone(harness.last_noise_metrics)

    def test_noise_boundary_three_of_twenty_passes_and_four_fails(self):
        # The 15% allowance (lot-500 calibration, 2026-08-17) puts the
        # boundary between 3 and 4 windows of 20: the legacy fixture's one
        # noise failure (500-44) measured 4/20 over here and must fail, while
        # isolated 1-2 window environmental spikes stay tolerated.
        clean_enough = FakeMeasurementDevice(noisy_windows=3)
        harness, (_metrics, final, _offset) = self.run_hardware(clean_enough)
        self.assertTrue(final.passed)
        self.assertEqual(harness.last_noise_report.outcome, app.OUTCOME_PASS)
        self.assertEqual(harness.last_noise_report.windows_over, 3)

        like_500_44 = FakeMeasurementDevice(noisy_windows=4)
        harness, (_metrics, final, _offset) = self.run_hardware(like_500_44)
        self.assertFalse(final.passed)
        self.assertEqual(harness.last_noise_report.outcome, app.OUTCOME_FAIL)
        self.assertEqual(harness.last_noise_report.windows_over, 4)
        self.assertNotIn("capture", like_500_44.calls)
        self.assertEqual(app.suggest_failure_mode(final), "N - Noisy")

    def test_noise_uses_fixed_wait_and_runs_before_the_driven_capture(self):
        device = FakeMeasurementDevice()
        harness, (_metrics, final, _offset) = self.run_hardware(device)

        self.assertTrue(final.passed)
        self.assertLess(
            device.calls.index("noise"), device.calls.index("capture")
        )
        # The recorded settle time is the fixed quiet wait, not an adaptive
        # settle point.
        self.assertEqual(
            harness.last_noise_report.settle_s, app.NOISE_WAIT_BEFORE_CAPTURE_S
        )
        # The live scope was switched into noise-range mode and back.
        display_events = [
            noise
            for kind, _token, noise in (
                event
                for event in harness.callback_events
                if event[0] == "preview_display"
            )
        ]
        self.assertEqual(display_events, [True, False])

    def test_high_offset_fails_immediately_but_low_defers_to_settled_reread(self):
        # HIGH offsets are real on the first read and still fail fast without
        # spending the noise + sensitivity time (covered elsewhere too). LOW
        # first reads are NOT a verdict since 2026-08-17: lot 500 showed the
        # offset rising for tens of seconds after insertion (500-5 read 0.6 V,
        # then 1.3 V on the immediate re-measure), so a low part now runs the
        # full sequence and is judged on the settled re-read.
        low_that_settles = FakeMeasurementDevice(
            offset_sequence=[0.55, 1.3]  # insertion read, settled re-read
        )
        harness, (_metrics, final, offset) = self.run_hardware(low_that_settles)
        self.assertTrue(final.passed)
        self.assertEqual(offset, 1.3)
        self.assertEqual(final.offset_v, 1.3)
        self.assertEqual(harness.last_offset_initial_v, 0.55)
        self.assertIn("capture", low_that_settles.calls)

        genuinely_low = FakeMeasurementDevice(offset=0.55)
        harness, (_metrics, final, offset) = self.run_hardware(genuinely_low)
        self.assertEqual(offset, 0.55)
        self.assertFalse(final.passed)
        # The full sequence ran; the verdict came from the settled re-read.
        self.assertIn("noise", genuinely_low.calls)
        self.assertIn("capture", genuinely_low.calls)
        self.assertTrue(
            any(reason.startswith("Offset out of range") for reason in final.fail_reasons)
        )
        self.assertEqual(app.suggest_failure_mode(final), "LO - Low offset")

    def test_unstable_capture_still_records_the_measured_noise(self):
        # The noise test now precedes the driven capture, so an unstable part
        # keeps its real (passing) noise measurement instead of a SKIPPED
        # placeholder.
        device = FakeMeasurementDevice(case_name="Never stabilizes")
        harness, (_metrics, final, _offset) = self.run_hardware(device)

        self.assertFalse(final.passed)
        self.assertLess(
            device.calls.index("noise"), device.calls.index("capture")
        )
        # PWM off, then the settled-offset re-read that carries the verdict.
        self.assertEqual(device.calls[-2:], ["pwm_off", "offset"])
        self.assertEqual(harness.last_noise_report.outcome, app.OUTCOME_PASS)
        self.assertEqual(app.suggest_failure_mode(final), app.UNSTABLE_FAILURE_MODE)

    def test_progress_ladder_covers_all_four_steps_in_order(self):
        device = FakeMeasurementDevice()
        harness, (_metrics, final, _offset) = self.run_hardware(device)

        self.assertTrue(final.passed)
        events = harness.progress_events
        self.assertTrue(events)
        steps = [step for step, _total, _label, _fraction in events]
        self.assertEqual(sorted(set(steps)), [1, 2, 3, 4])
        self.assertEqual(steps, sorted(steps))  # never steps backward
        self.assertTrue(all(total == 4 for _s, total, _l, _f in events))
        self.assertTrue(all(0.0 <= f <= 1.0 for _s, _t, _l, f in events))
        labels = {step: label for step, _total, label, _fraction in events}
        self.assertEqual(labels[1], "Offset")
        self.assertEqual(labels[2], "Reference check")
        self.assertEqual(labels[3], "Noise (emitter off)")
        self.assertEqual(labels[4], "Sensitivity")

    def test_noise_report_csv_round_trip_for_a_failing_capture(self):
        device = FakeMeasurementDevice(noisy_windows=6)
        harness, (_metrics, final, _offset) = self.run_hardware(device)
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "batch.csv"
            app.append_result_csv(
                csv_path,
                batch_number="B9",
                sensor_number=1,
                sensor_id="B9-1",
                tester_name="Operator",
                filter_setup=app.DEFAULT_FILTER_SETUP,
                pwm_channel=app.EMITTER_PWM_CHANNEL,
                pwm_hz=app.EMITTER_PWM_FREQUENCY_HZ,
                pwm_duty=app.EMITTER_PWM_DUTY_CYCLE,
                final_result=final,
                comment="",
                snapshot_paths=[],
                capture_report=harness.last_capture_report,
                noise_report=harness.last_noise_report,
            )
            with csv_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["pass_fail"], "FAIL")
        self.assertEqual(row["failure_mode_tag"], "N")
        self.assertEqual(row["noise_test_outcome"], "FAIL")
        self.assertEqual(row["noise_windows_total"], "20")
        self.assertEqual(row["noise_windows_over"], "6")
        self.assertEqual(row["noise_over_percent"], "30.0")
        # The pin-referred TP412 limit: legacy 300 mV / ~700x effective chain.
        self.assertEqual(row["noise_pp_limit_mv"], "0.428571")
        # The 2 mV pk-pk synthetic square reads ~2.36 mV through the
        # anti-alias FIR: its flat passband keeps the square's in-band
        # harmonics at full strength (the old boxcar drooped them) and the
        # band edge adds Gibbs ripple. Same windows-over count either way.
        self.assertAlmostEqual(float(row["noise_worst_pp_mv"]), 2.356, delta=0.05)
        self.assertEqual(row["noise_analysis_rate_hz"], "50.0")
        self.assertEqual(row["noise_baseline_settled"], "YES")
        # The historical noise_settle_s column records the adaptive wait.
        self.assertEqual(row["noise_settle_s"], "3.000")
        # No driven capture happened (fail-fast), so the stability columns
        # are blank but the row still writes cleanly.
        self.assertEqual(row["sensitivity_mv"], "")

    def test_quiet_sensor_passes_and_records_noise_stats(self):
        device = FakeMeasurementDevice()
        harness, (_metrics, final, _offset) = self.run_hardware(device)

        self.assertTrue(final.passed)
        report = harness.last_noise_report
        self.assertEqual(report.outcome, app.OUTCOME_PASS)
        self.assertEqual(report.windows_over, 0)
        # 20 µV pk-pk quiet square, preserved through the band limiting
        # (the anti-alias FIR's flat passband + band-edge Gibbs ripple read
        # a square's pk-pk ~18% above its raw amplitude; same shape factor
        # as the noisy fixture, far from the 429 µV limit either way).
        self.assertAlmostEqual(report.worst_pp_mv, 0.0236, delta=0.001)
        self.assertAlmostEqual(report.capture_s, 20.0, delta=0.01)
        self.assertEqual(report.pp_limit_mv, app.NOISE_PP_LIMIT_MV)
        self.assertAlmostEqual(report.pp_limit_mv, 300.0 / 700.0, places=6)
        self.assertAlmostEqual(
            report.analysis_rate_hz,
            1000.0 / app.NOISE_DECIMATION_FACTOR,
            places=3,
        )


class NoiseSoakTests(unittest.TestCase):
    """Per-part 60 s soak: 3x observation, allowed count held at 3 ABSOLUTE."""

    def run_hardware(self, device, *, noise_soak=False):
        harness = MeasurementHarness(device)
        harness.reference_calibration = None  # gate disabled in production
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
            noise_soak=noise_soak,
        )
        return harness, result

    def test_soak_constants_hold_the_absolute_allowance(self):
        self.assertEqual(app.NOISE_SOAK_CAPTURE_SECONDS, 60.0)
        # 60 windows x soak fraction = the SAME absolute 3 windows as 20 x 15%.
        self.assertEqual(
            int(60 * app.NOISE_SOAK_MAX_OVER_FRACTION + 1e-9),
            int(20 * app.NOISE_MAX_OVER_FRACTION + 1e-9),
        )

    def test_default_run_uses_the_standard_20s_capture(self):
        device = FakeMeasurementDevice()
        harness, _result = self.run_hardware(device)
        self.assertEqual(device.noise_capture_seconds_seen, 20.0)
        self.assertEqual(harness.last_noise_report.windows_total, 20)

    def test_soak_triples_the_capture_and_does_not_relax_the_allowance(self):
        # 5 noisy windows in a 60 s soak: a fraction-scaled allowance would
        # tolerate int(60 * 0.15) = 9 and PASS this part; the absolute
        # 3-window allowance must FAIL it - that is the whole point of the
        # soak (intermittent bursts accumulate windows over a longer watch,
        # one environmental transient still spans only 1-3).
        device = FakeMeasurementDevice(noisy_windows=5)
        harness, (_metrics, final, _offset) = self.run_hardware(
            device, noise_soak=True
        )
        self.assertEqual(device.noise_capture_seconds_seen, 60.0)
        self.assertEqual(harness.last_noise_report.windows_total, 60)
        self.assertFalse(final.passed)
        self.assertEqual(harness.last_noise_report.outcome, app.OUTCOME_FAIL)
        # 2026-08-31: the report records the allowance actually judged with
        # and the FAIL reason quotes it (the old message said "allowed 9").
        self.assertAlmostEqual(
            harness.last_noise_report.max_over_percent, 5.0, places=9
        )
        self.assertTrue(
            any("(allowed 3)" in reason for reason in final.fail_reasons),
            final.fail_reasons,
        )

    def test_soak_still_tolerates_a_single_transient(self):
        device = FakeMeasurementDevice(noisy_windows=3)
        harness, (_metrics, final, _offset) = self.run_hardware(
            device, noise_soak=True
        )
        self.assertTrue(final.passed)
        self.assertEqual(harness.last_noise_report.windows_over, 3)

    def test_any_over_window_pushes_the_automatic_raw_capture(self):
        noisy_but_passing = FakeMeasurementDevice(noisy_windows=2)
        harness, (_metrics, final, _offset) = self.run_hardware(noisy_but_passing)
        self.assertTrue(final.passed)
        events = [e for e in harness.callback_events if e[0] == "auto_noise_capture"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][2], 2)  # windows_over passed with the report

        clean = FakeMeasurementDevice()
        harness, _result = self.run_hardware(clean)
        self.assertFalse(
            any(e[0] == "auto_noise_capture" for e in harness.callback_events)
        )

    def test_app_auto_save_writes_once_and_sets_the_flag(self):
        waveform = 1.2 + 0.0005 * np.sin(np.linspace(0.0, 40.0, 2000))
        holder = SimpleNamespace(
            measure_token=7,
            noise_raw_auto_saved=False,
            last_noise_raw_waveform=waveform,
            last_noise_raw_rate_hz=1000.0,
            batch_number="500",
            current_sensor_id="500-44",
            snapshot_paths=[],
            noise_capture_status_var=FakeVar(""),
        )
        report = SimpleNamespace(
            windows_over=4, windows_total=20, outcome="FAIL", worst_pp_mv=0.482
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(
                app, "results_root_dir", return_value=Path(temp_dir)
            ):
                app.EmitterTesterApp.auto_save_noise_capture(holder, 7, report)
                self.assertTrue(holder.noise_raw_auto_saved)
                self.assertEqual(len(holder.snapshot_paths), 2)
                # A second push (e.g. a re-render race) must not double-save.
                app.EmitterTesterApp.auto_save_noise_capture(holder, 7, report)
                self.assertEqual(len(holder.snapshot_paths), 2)
                # A stale token must be ignored entirely.
                holder.noise_raw_auto_saved = False
                app.EmitterTesterApp.auto_save_noise_capture(holder, 99, report)
                self.assertEqual(len(holder.snapshot_paths), 2)
        self.assertIn("auto-saved", holder.noise_capture_status_var.get())


class RawNoiseCaptureTests(unittest.TestCase):
    """Opt-in raw noise-capture retention for offline spike analysis."""

    def test_saver_writes_csv_and_npz_with_unique_stems(self):
        waveform = 1.2 + 0.0005 * np.sin(np.linspace(0.0, 40.0, 2000))
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(
                app, "results_root_dir", return_value=Path(temp_dir)
            ):
                first = app.save_raw_noise_capture(
                    "500", "500-7", waveform, 1000.0,
                    metadata={"noise_outcome": "PASS"},
                )
                second = app.save_raw_noise_capture(
                    "500", "500-7", waveform, 1000.0
                )
            self.assertEqual([p.suffix for p in first], [".csv", ".npz"])
            self.assertIn("noise_captures", str(first[0]))
            self.assertIn("lot_500", str(first[0]))
            # Same part saved twice gets distinct stems, nothing overwritten.
            self.assertNotEqual(first[0].name, second[0].name)

            with first[0].open(encoding="utf-8") as handle:
                meta_line = handle.readline()
                self.assertTrue(meta_line.startswith("#"))
                self.assertIn("sample_rate_hz=1000.0", meta_line)
                self.assertIn("noise_outcome=PASS", meta_line)
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2000)
            self.assertAlmostEqual(float(rows[7]["volts"]), waveform[7], places=6)
            self.assertAlmostEqual(float(rows[1]["t_s"]), 0.001, places=6)

            with np.load(first[1]) as loaded:
                np.testing.assert_allclose(loaded["waveform_v"], waveform)
                self.assertEqual(float(loaded["sample_rate_hz"]), 1000.0)

    def test_empty_capture_saves_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(
                app, "results_root_dir", return_value=Path(temp_dir)
            ):
                self.assertEqual(
                    app.save_raw_noise_capture(
                        "500", "500-1", np.array([], dtype=float), 1000.0
                    ),
                    [],
                )

    def test_hardware_run_retains_the_raw_capture_for_the_current_part(self):
        device = FakeMeasurementDevice()
        harness = MeasurementHarness(device)
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
        self.assertIsNotNone(harness.last_noise_raw_waveform)
        self.assertEqual(harness.last_noise_raw_rate_hz, 1000.0)
        # Full-rate record, not the 50 SPS band-limited verdict trace.
        self.assertEqual(
            len(harness.last_noise_raw_waveform),
            int(app.NOISE_CAPTURE_SECONDS * 1000.0),
        )

    def test_noise_fail_save_attaches_the_raw_capture_automatically(self):
        device = FakeMeasurementDevice(noisy_windows=5)
        measure = MeasurementHarness(device)
        _metrics, final, _offset = app.EmitterTesterApp._hardware_measurement(
            measure,
            app.DEFAULT_FILTER_SETUP,
            app.WAVEFORM_INPUT_RANGE_V,
            app.EMITTER_PWM_CHANNEL,
            app.EMITTER_PWM_FREQUENCY_HZ,
            app.EMITTER_PWM_DUTY_CYCLE,
            False,
            measure.measure_token,
            lambda callback: callback(),
        )
        self.assertFalse(final.passed)
        saver = SimpleNamespace(
            last_result=final,
            last_metrics=None,
            measure_attempts=1,
            number_attempt=1,
            _log_attempt=lambda *args, **kwargs: None,
            last_capture_report=None,
            last_noise_report=measure.last_noise_report,
            last_noise_metrics=measure.last_noise_metrics,
            last_noise_raw_waveform=measure.last_noise_raw_waveform,
            last_noise_raw_rate_hz=measure.last_noise_raw_rate_hz,
            last_reference_check_mv=None,
            last_offset_initial_v=1.2,
            stability_diagnostics_saved=False,
            noise_diagnostics_saved=False,
            noise_raw_auto_saved=False,
            reference_calibration=None,
            battery_v=None,
            snapshot_paths=[],
            batch_number="500",
            current_sensor_number=44,
            current_sensor_id="500-44",
            tester_name="Operator",
            filter_setup=app.DEFAULT_FILTER_SETUP,
            failure_mode_var=FakeVar("N - Noisy"),
            notes_var=FakeVar(""),
            status_var=FakeVar(""),
        )
        saver.delete_autosave = lambda: None
        saver.update_navigation_state = lambda: None
        saver.noise_snapshot_detail_lines = lambda: ["Batch: 500"]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with mock.patch.object(app, "results_root_dir", return_value=root), \
                    mock.patch.object(
                        app, "batch_results_path", return_value=root / "500.csv"
                    ):
                self.assertTrue(app.EmitterTesterApp.save_current_sensor(saver))
            raw_files = sorted(
                p.name for p in (root / "noise_captures" / "lot_500").iterdir()
            )
        self.assertEqual(
            raw_files, ["500-44_noise_raw.csv", "500-44_noise_raw.npz"]
        )
        self.assertTrue(
            any("noise_raw" in str(p) for p in saver.snapshot_paths)
        )


class ReferenceGateDisabledTests(unittest.TestCase):
    """Shipping default: REFERENCE_GATE_ENABLED = False (op-amp crosstalk).

    The shared dual op-amp buffer couples the DUT into AIN1, so the reference
    reading tracks the loaded sensor instead of the emitter. Until the
    channel-isolated buffer board is installed, a test must run WITHOUT any
    reference capture and without requiring a reference calibration.
    """

    def run_hardware(self, device):
        harness = MeasurementHarness(device)
        # No calibration exists (and none is required) while the gate is off.
        harness.reference_calibration = None
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

    def test_default_flag_is_disabled(self):
        self.assertFalse(app.REFERENCE_GATE_ENABLED)

    def test_measurement_runs_without_reference_capture_or_calibration(self):
        device = FakeMeasurementDevice()
        harness, (metrics, final, offset) = self.run_hardware(device)
        # AIN1 is never streamed and the emitter never runs for a reference
        # phase: offset (emitter off) -> noise (emitter off) -> capture ->
        # settled-offset re-read.
        self.assertEqual(
            device.calls,
            ["pwm_off", "offset", "noise", "pwm_on", "capture", "pwm_off", "offset"],
        )
        self.assertTrue(final.passed)
        self.assertIsNone(harness.last_reference_check_mv)
        # The CSV reference columns stay empty rather than recording a
        # crosstalk-contaminated reading.

    def test_progress_ladder_shrinks_to_three_steps(self):
        device = FakeMeasurementDevice()
        harness, _result = self.run_hardware(device)
        steps = [event[0] for event in harness.progress_events]
        totals = {event[1] for event in harness.progress_events}
        labels = [event[2] for event in harness.progress_events]
        self.assertEqual(sorted(set(steps)), [1, 2, 3])
        self.assertEqual(totals, {3})
        self.assertNotIn("Reference check", labels)
        self.assertEqual(labels[-1], "Sensitivity")

    def test_gate_reports_ready_without_any_calibration(self):
        # reference_gate_ready() must not block Measure while the gate is off,
        # even with no calibration loaded at all.
        harness = SimpleNamespace(
            simulator_var=SimpleNamespace(get=lambda: False),
            reference_calibration=None,
        )
        self.assertTrue(app.EmitterTesterApp.reference_gate_ready(harness))


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
        # The simulated 1 Hz startup drift stabilizes after ~17 cycles plus
        # the 5-delta confirmation run.
        self.assertGreater(harness.last_capture_report.stabilization_seconds, 15.0)
        self.assertEqual(harness.last_capture_report.measurement_cycles, 10)

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

    def test_simulator_exercises_enabled_gate_and_wrong_polarity(self):
        # With the lot-500 calibration the sensitivity gate is live: the low
        # case fails, the borderline case (exactly the legacy minimum) lands
        # in the near-limit band and passes with a re-measure warning, and
        # wrong polarity still fails on its own gate.
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
                self.assertEqual(harness.last_capture_report.measurement_cycles, 10)
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
                self.assertEqual(root.title(), "Eltec 405 M22 ESP32 Tester (1 Hz)")
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


class FakeVar:
    """Stand-in for a tk.StringVar in headless harness tests."""

    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class NoSensorPromptTests(unittest.TestCase):
    """A floating AIN0 asks whether a sensor is loaded; yes = bad part."""

    def test_floating_ain0_raises_the_askable_error(self):
        device = FakeMeasurementDevice(offset=0.02)
        harness = MeasurementHarness(device)
        with mock.patch.object(app, "OFFSET_WAKE_TIMEOUT_S", 0.0):
            with self.assertRaises(app.NoSensorDetectedError) as ctx:
                app.EmitterTesterApp._hardware_measurement(
                    harness, app.DEFAULT_FILTER_SETUP, app.WAVEFORM_INPUT_RANGE_V,
                    app.EMITTER_PWM_CHANNEL, app.EMITTER_PWM_FREQUENCY_HZ,
                    app.EMITTER_PWM_DUTY_CYCLE, False, harness.measure_token,
                    lambda callback: callback(),
                )
        self.assertIsInstance(ctx.exception, app.HardwareNotReadyError)
        self.assertAlmostEqual(ctx.exception.offset_v, 0.02)

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
            last_measure_error=None, last_noise_report=None, last_noise_metrics=None,
            preview_waveform=None, preview_sync=None,
            failure_mode_var=FakeVar(""),
            status_var=FakeVar(""),
            measure_status_var=FakeVar(""),
            write_autosave=lambda stage: events.append(("autosave", stage)),
            render_step=lambda: events.append(("render",)),
            _reset_measure_progress=lambda: events.append(("progress_reset",)),
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
        self.assertIsNone(h.last_measure_error)
        self.assertEqual(h.failure_mode_var.get(), app.BAD_SENSOR_FAILURE_MODE)
        self.assertIn(("log", app.attempt_history.EVENT_MEASURED), h.events)
        self.assertIn(("render",), h.events)

    def test_no_keeps_the_wiring_warning(self):
        h = self._harness(False)
        exc = app.NoSensorDetectedError("No sensor detected", 0.01)
        with mock.patch.object(app.messagebox, "showwarning") as warn:
            app.EmitterTesterApp.on_hardware_not_ready(h, 3, exc)
        warn.assert_called_once()
        self.assertIsNone(h.last_result)
        self.assertIn("No sensor detected", h.last_measure_error)
        self.assertIn(("log", app.attempt_history.EVENT_MEASURE_ERROR), h.events)


class NotMeasuredSkipTests(unittest.TestCase):
    """A sensor the rig could not read is recorded, not silently retried."""

    STREAM_ERROR = (
        "ESP32 waveform stream was not reliable; nothing was recorded: "
        "6 timestamp gaps (~52 missing samples); 11 duplicate timestamps."
    )

    def skip_harness(self, **overrides):
        harness = SimpleNamespace(
            step="result",
            RESULT_STEP="result",
            busy=False,
            measuring=False,
            result_saved=False,
            last_result=None,
            last_measure_error=self.STREAM_ERROR,
            last_metrics=None,
            last_capture_report=None,
            last_noise_report=None,
            last_noise_metrics=None,
            last_reference_check_mv=None,
            stability_diagnostics_saved=False,
            noise_diagnostics_saved=False,
            reference_calibration=None,
            battery_v=None,
            snapshot_paths=[],
            batch_number="B1",
            current_sensor_number=3,
            current_sensor_id="B1-3",
            tester_name="Operator",
            filter_setup=app.DEFAULT_FILTER_SETUP,
            last_offset_initial_v=None,
            measure_attempts=1,
            number_attempt=1,
            _log_attempt=lambda *args, **kwargs: None,
            failure_mode_var=FakeVar(""),
            notes_var=FakeVar(""),
            status_var=FakeVar(""),
            renders=[],
        )
        harness.render_step = lambda: harness.renders.append("render")
        harness.delete_autosave = lambda: None
        harness.update_navigation_state = lambda: None
        harness.save_current_sensor = (
            lambda: app.EmitterTesterApp.save_current_sensor(harness)
        )
        harness.can_record_not_measured = (
            lambda: app.EmitterTesterApp.can_record_not_measured(harness)
        )
        for name, value in overrides.items():
            setattr(harness, name, value)
        return harness

    def test_not_measured_is_its_own_outcome_and_never_a_failure_mode(self):
        final = app.build_not_measured_result(self.STREAM_ERROR)
        self.assertTrue(app.is_not_measured(final))
        self.assertEqual(app.result_outcome(final), app.OUTCOME_NOT_MEASURED)
        self.assertIsNone(final.offset_v)
        self.assertIsNone(final.sensitivity_mv)
        self.assertEqual(final.polarity, "")
        self.assertEqual(
            app.suggest_failure_mode(final), app.DEFAULT_NOT_MEASURED_REASON
        )
        # The NM reasons stay out of the production taxonomy offered for a
        # sensor that WAS measured, but are still valid to write.
        for reason in app.NOT_MEASURED_REASON_CHOICES:
            self.assertNotIn(reason, app.FAILURE_MODE_CHOICES)
            self.assertEqual(app.split_failure_mode(reason)[0], app.NOT_MEASURED_TAG)

    def test_not_measured_row_carries_a_reason_and_no_measurements(self):
        harness = self.skip_harness()
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "B1.csv"
            with mock.patch.object(app, "batch_results_path", return_value=csv_path):
                saved = app.EmitterTesterApp.save_not_measured_sensor(
                    harness, app.DEFAULT_NOT_MEASURED_REASON, "swapped the USB cable"
                )
            self.assertTrue(saved)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
        self.assertEqual(row["sensor_id"], "B1-3")
        self.assertEqual(row["pass_fail"], app.OUTCOME_NOT_MEASURED)
        self.assertEqual(row["offset_v"], "")
        self.assertEqual(row["sensitivity_mv"], "")
        self.assertEqual(row["sensitivity_raw_mv"], "")
        self.assertEqual(row["polarity"], "")
        self.assertEqual(row["polarity_good_bad"], "")
        self.assertEqual(row["failure_mode_tag"], app.NOT_MEASURED_TAG)
        self.assertTrue(row["fail_reasons"].startswith(app.NOT_MEASURED_REASON_PREFIX))
        self.assertIn("timestamp gaps", row["fail_reasons"])
        self.assertEqual(row["operator_comments"], "swapped the USB cable")
        self.assertEqual(row["number_attempt"], "1")
        self.assertTrue(harness.result_saved)

    def test_not_measured_is_refused_once_a_real_verdict_exists(self):
        measured = app.FinalResult(
            passed=False,
            offset_v=0.7,
            sensitivity_mv=None,
            polarity="",
            fail_reasons=["Unstable: waveform peak did not stabilize."],
            warnings=[],
            waveform_metrics=None,
        )
        self.assertFalse(
            app.EmitterTesterApp.can_record_not_measured(
                self.skip_harness(last_result=measured)
            )
        )
        # ...and with no failed attempt behind it there is nothing to record.
        self.assertFalse(
            app.EmitterTesterApp.can_record_not_measured(
                self.skip_harness(last_measure_error=None)
            )
        )
        self.assertTrue(
            app.EmitterTesterApp.can_record_not_measured(self.skip_harness())
        )

    def test_a_failed_write_leaves_the_sensor_unsaved_and_measurable(self):
        harness = self.skip_harness()
        with mock.patch.object(
            app, "append_result_csv", side_effect=OSError("batch CSV is open in Excel")
        ), mock.patch.object(app, "batch_results_path", return_value=Path("B1.csv")), \
                mock.patch.object(app.messagebox, "showerror") as showerror:
            saved = app.EmitterTesterApp.save_not_measured_sensor(
                harness, app.DEFAULT_NOT_MEASURED_REASON, ""
            )
        self.assertFalse(saved)
        self.assertFalse(harness.result_saved)
        self.assertIsNone(harness.last_result)
        self.assertEqual(harness.failure_mode_var.get(), "")
        self.assertEqual(harness.notes_var.get(), "")
        showerror.assert_called_once()

    def test_batch_summary_keeps_not_measured_sensors_out_of_the_yield(self):
        counts = app.summarize_batch_outcomes(
            [
                app.OUTCOME_PASS,
                app.OUTCOME_FAIL,
                app.OUTCOME_PASS,
                app.OUTCOME_NOT_MEASURED,
            ]
        )
        self.assertEqual(counts["recorded"], 4)
        self.assertEqual(counts["tested"], 3)
        self.assertEqual(counts["passed"], 2)
        self.assertNotIn("retest", counts)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["not_measured"], 1)
        self.assertAlmostEqual(counts["yield_pct"], 200.0 / 3)
        # A batch where nothing could be measured reports no yield at all
        # instead of a 0% that reads like every sensor failed.
        blocked = app.summarize_batch_outcomes([app.OUTCOME_NOT_MEASURED] * 2)
        self.assertEqual(blocked["tested"], 0)
        self.assertEqual(blocked["failed"], 0)
        self.assertEqual(blocked["yield_pct"], 0.0)



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
        if outcome == app.OUTCOME_NOT_MEASURED:
            final = app.build_not_measured_result("stream stalled")
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
            failure_mode=(
                "" if passed else app.suggest_failure_mode(final)
            ),
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
            # NOT MEASURED is not a pass either.
            self._row(path, 2, passed=False, outcome=app.OUTCOME_NOT_MEASURED)
            self.assertEqual(app.next_sensor_number_for_batch(path), 2)
            # ...and the part that finally passes earns it.
            self._row(path, 2, passed=True)
            self.assertEqual(app.next_sensor_number_for_batch(path), 3)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        ids = [row["sensor_id"] for row in rows]
        self.assertEqual(ids, ["B9-1", "B9-2", "B9-2", "B9-2", "B9-2"])
        self.assertEqual(
            [row["pass_fail"] for row in rows],
            [
                app.OUTCOME_PASS,
                app.OUTCOME_FAIL,
                app.OUTCOME_FAIL,
                app.OUTCOME_NOT_MEASURED,
                app.OUTCOME_PASS,
            ],
        )
        # number_attempt keeps the repeated ids readable without row order.
        self.assertEqual(
            [row["number_attempt"] for row in rows], ["1", "1", "2", "3", "4"]
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
        harness = self._harness(last_measure_error="stream stalled")
        app.EmitterTesterApp.go_next(harness)
        self.assertEqual(harness.calls, [("measure",)])
        self.assertEqual(harness.current_sensor_id, "B9-3")

    def test_next_is_inert_while_a_capture_is_running(self):
        app.EmitterTesterApp.go_next(self._harness(measuring=True, calls=[]))
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
        self.assertIsNone(harness.last_measure_error)
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


if __name__ == "__main__":
    unittest.main()
