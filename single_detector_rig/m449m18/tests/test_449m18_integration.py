"""Integration tests for the 449 M18 (TP443 frequency-tracking) tester.

Covers the model-specific layer on top of the shared rig machinery: the
TP443 policy constants, the two-frequency evaluation (specs 1-4, gate on and
off), the hardware sequence (offset -> 5 Hz -> 18 Hz -> settled offset),
the simulator cases, the batch CSV, the launchers and a Tk smoke test.
"""

from __future__ import annotations

import csv
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import eltec_449m18_esp32_tester as app  # noqa: E402
from stability_analysis import analyze_stability, load_stability_settings  # noqa: E402


SETTINGS = load_stability_settings()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def prepared_capture(case_name: str, frequency_hz: float, *, offset_v: float | None = None):
    """A simulated drive capture, cut where the production reader would stop."""
    waveform, sync, rate, offset = app.simulate_v6_startup_capture(
        app.DEFAULT_FILTER_SETUP, case_name, frequency_hz=frequency_hz, offset_v=offset_v
    )
    dut_settings = app.dut_stability_settings(SETTINGS)
    blocks = app.measurement_blocks_for(frequency_hz)

    def analyze(wf, sy):
        return analyze_stability(
            wf,
            app.stability_sync_for(sy, frequency_hz),
            rate,
            dut_settings,
            stability_deadline_s=app.STABILITY_TIMEOUT_S,
            measurement_cycles_required=blocks,
            enforce_measurement_stability=True,
            max_measurement_attempts=app.MAX_MEASUREMENT_ATTEMPTS,
            data_source="test",
        )

    analysis = analyze(waveform, sync)
    if analysis.report.measurement_complete:
        cut = analysis.measurement_cycles[-1].end_index + 1
    else:
        cut = int(app.STABILITY_TIMEOUT_S * rate) + 1
    waveform = waveform[:cut]
    sync = sync[:cut]
    return waveform, sync, rate, offset, analyze(waveform, sync)


def metrics_for(
    raw_mv: float,
    frequency_hz: float,
    *,
    polarity: str = app.POSITIVE_POLARITY,
    snr: float | None = 20.0,
    stabilized: bool = True,
    warnings: list[str] | None = None,
) -> app.WaveformMetrics:
    return app.WaveformMetrics(
        sensitivity_mv=raw_mv,
        sensitivity_amplified_mv=raw_mv,
        polarity=polarity,
        measured_frequency_hz=frequency_hz,
        cycles_used=app.measurement_cycles_for(frequency_hz),
        stabilized=stabilized,
        signal_to_noise_ratio=snr,
        signal_to_noise_db=None if snr is None else 20.0 * math.log10(snr),
        warnings=list(warnings or []),
    )


class FakeMeasurementDevice:
    """Stand-in for EmitterEsp32Rig: one simulated capture per drive."""

    def __init__(
        self,
        *,
        offset=1.2,
        offset_sequence=None,
        case_name="Known good",
        case_by_frequency=None,
        error=None,
        reference_mv=100.0,
        capture_errors=None,
    ):
        self.offset = offset
        # Exceptions raised by successive read_waveform_until_stable() calls
        # before a capture succeeds (a stall or integrity failure the app
        # restarts by itself).
        self.capture_errors = list(capture_errors) if capture_errors else []
        self.offset_sequence = list(offset_sequence) if offset_sequence else []
        self.case_name = case_name
        self.case_by_frequency = dict(case_by_frequency or {})
        self.error = error
        self.reference_mv = reference_mv
        self.calls: list[str] = []
        self.configure_kwargs: list[dict] = []
        self.capture_kwargs: list[dict] = []

    def disable_emitter_pwm(self, channel):
        del channel
        self.calls.append("pwm_off")
        return 10.0

    def read_battery_voltage(self):
        self.calls.append("battery")
        return 6.2

    def read_offset_voltage(self, *, waveform_range_v):
        del waveform_range_v
        self.calls.append("offset")
        if self.offset_sequence:
            self.offset = self.offset_sequence.pop(0)
        return self.offset

    def configure_emitter_pwm(self, **kwargs):
        self.calls.append("pwm_on")
        self.configure_kwargs.append(dict(kwargs))
        return 5.0

    def read_waveform_until_stable(self, **kwargs):
        self.calls.append("capture")
        self.capture_kwargs.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if self.capture_errors:
            raise self.capture_errors.pop(0)
        frequency_hz = kwargs["expected_frequency_hz"]
        case = self.case_by_frequency.get(frequency_hz, self.case_name)
        waveform, sync, rate, _offset, analysis = prepared_capture(case, frequency_hz)
        if kwargs.get("progress"):
            kwargs["progress"](analysis)
        if kwargs.get("preview"):
            kwargs["preview"](waveform[-500:], sync[-500:])
        return waveform, sync, rate, analysis


class MeasurementHarness:
    """Bare stand-in for EmitterTesterApp driving the real measurement code."""

    def __init__(self, device):
        self.device = device
        self.hardware_lock = threading.Lock()
        self.measure_token = 7
        self.last_capture_report = None
        self.last_metrics_high = None
        self.last_capture_report_high = None
        self.last_tracking_report = None
        self.last_reference_check_mv = None
        self.last_offset_initial_v = None
        self.reference_calibration_error = None
        self.reference_calibration = app.build_reference_calibration(
            [100.0] * app.REFERENCE_CALIBRATION_READINGS
        )
        self.stability_settings = SETTINGS
        self.callback_events = []
        self.progress_events = []
        self.status_texts = []
        self.preview_count = 0
        self.stream_retries = []
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
        return device.reference_mv

    def on_battery_update(self, value, error=None):
        self.callback_events.append(("battery", value, error))

    def on_initial_offset(self, token, value):
        self.last_offset_initial_v = value
        self.callback_events.append(("initial_offset", token, value))

    def on_offset_update(self, token, value):
        self.callback_events.append(("offset", token, value))

    def set_measure_status(self, token, text):
        self.status_texts.append(text)

    def set_measure_progress(self, token, step, total, label, fraction):
        self.progress_events.append((step, total, label, fraction))

    def on_preview_frame(self, token, waveform, sync):
        self.preview_count += 1

    def _store_measurement(self, **kwargs):
        app.EmitterTesterApp._store_measurement(self, **kwargs)

    def _finish_drive(self, *args, **kwargs):
        return app.EmitterTesterApp._finish_drive(self, *args, **kwargs)


def run_hardware(device):
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


def run_simulator(case_name: str):
    harness = MeasurementHarness(FakeMeasurementDevice())
    result = app.EmitterTesterApp._simulate_measurement(
        harness,
        app.DEFAULT_FILTER_SETUP,
        case_name,
        False,
        app.WAVEFORM_INPUT_RANGE_V,
        False,
        harness.measure_token,
        lambda callback: callback(),
    )
    return harness, result


GATED_FACTORS = {app.LOW_FREQUENCY_HZ: 2.0, app.HIGH_FREQUENCY_HZ: 3.0}


def gated(factors=None):
    """Patch the sensitivity gate ON with explicit per-frequency factors."""
    return mock.patch.multiple(
        app,
        SENSITIVITY_GATE_ENABLED=True,
        SENSITIVITY_LEGACY_EQUIVALENT_FACTORS=dict(factors or GATED_FACTORS),
    )


# --------------------------------------------------------------------------- #
# Identity and TP443 policy constants
# --------------------------------------------------------------------------- #
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
        # The restarted counter alone is decisive - the boot text arrives
        # garbled (ROM output at 115200 baud) and may not include READY.
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
                    "ESP32 waveform stream stalled after 3000/72001 samples. [host-stall]",
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

        # A persistent stall still fails after the bounded restarts.
        persistent: list[int] = []

        def always_stalled(attempt: int) -> float:
            persistent.append(attempt)
            raise app.StreamStalledError(
                "stalled", kind=app.STALL_KIND_BOARD_SILENT, received=0
            )

        with self.assertRaises(app.StreamStalledError):
            app.call_with_stream_retries(always_stalled, on_retry=lambda _n, _e: None)
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


class IdentityAndPolicyTests(unittest.TestCase):
    def test_results_namespace_and_model(self):
        self.assertEqual(app.MODEL_NAME, "449M18")
        self.assertEqual(app.results_root_dir().name, "449m18_esp32")
        self.assertEqual(app.results_root_dir().parent.name, "Eltec_449M18_Test_Results")
        self.assertTrue(app.batch_results_path("L1").name.startswith("449m18_esp32_lot_"))
        self.assertIs(app.FILTER_SPECS_MV, app._shared_engine.FILTER_SPECS_MV)
        self.assertEqual(app.FILTER_SPECS_MV, {"-25 filter": 1200.0})
        self.assertEqual(app.DEFAULT_FILTER_SETUP, "-25 filter")
        self.assertEqual(app._shared_engine.EXPECTED_FREQUENCY_HZ, 5.0)
        self.assertEqual(app._shared_engine.MODEL_NAME, "449M18")

    def test_tp443_drives_and_limits(self):
        self.assertEqual((app.LOW_FREQUENCY_HZ, app.HIGH_FREQUENCY_HZ), (5.0, 18.0))
        self.assertEqual(app.EMITTER_PWM_FREQUENCY_HZ, 5.0)
        self.assertEqual(app.EMITTER_PWM_HIGH_FREQUENCY_HZ, 18.0)
        self.assertEqual(app.EMITTER_PWM_DUTY_CYCLE, 20.0)
        self.assertEqual(app.REFERENCE_PWM_FREQUENCY_HZ, 10.0)
        self.assertEqual(app.REFERENCE_PWM_DUTY_CYCLE, 50.0)
        self.assertEqual(app.EMITTER_PWM_CHANNEL, "GPIO33")
        self.assertEqual(app.TP443_MIN_LEGACY_LOW_MV, 1200.0)
        self.assertEqual(app.TP443_MIN_LEGACY_HIGH_MV, 720.0)
        self.assertEqual((app.TP443_RATIO_MIN, app.TP443_RATIO_MAX), (0.70, 1.30))
        self.assertEqual(app.TP443_TRAY_RATIO_THRESHOLD, 0.72)
        self.assertEqual(app.SYNC_FREQUENCY_TOLERANCE_HZ, {5.0: 0.05, 18.0: 0.18})
        self.assertEqual(app.sync_tolerance_for(5.0), 0.05)
        self.assertEqual(app.sync_tolerance_for(18.0), 0.18)
        self.assertEqual(app.sync_tolerance_for(10.0), 0.1)
        self.assertEqual(app.MINIMUM_FIRMWARE_VERSION, (3, 2, 0))

    def test_capture_policy_per_drive(self):
        self.assertEqual(app.STABILITY_TIMEOUT_S, 30.0)
        self.assertEqual(app.DUT_STABILITY_CONFIRMATION_DELTAS, 5)
        self.assertEqual(app.MAX_MEASUREMENT_ATTEMPTS, 3)
        self.assertEqual(app.SYNC_VALIDATION_CYCLES, 5)
        self.assertEqual(app.measurement_cycles_for(5.0), 20)
        self.assertEqual(app.measurement_cycles_for(18.0), 36)
        self.assertEqual(app.cycles_per_block_for(5.0), 1)
        self.assertEqual(app.cycles_per_block_for(18.0), 9)
        self.assertEqual(app.cycles_per_block_for(10.0), 1)
        self.assertEqual(app.measurement_blocks_for(5.0), 20)
        self.assertEqual(app.measurement_blocks_for(18.0), 4)
        self.assertEqual(SETTINGS.peak_delta_threshold_mv, 0.100)
        self.assertEqual(app.REFERENCE_PEAK_DELTA_THRESHOLD_MV, 0.250)
        # 9 cycles at 18 Hz / 1000 SPS = exactly 500 samples, so the
        # sample-grid phase pattern repeats every block.
        self.assertAlmostEqual(9 * 1000.0 / 18.0, 500.0)

    def test_calibration_is_pending_by_default(self):
        self.assertFalse(app.SENSITIVITY_GATE_ENABLED)
        self.assertEqual(app.SENSITIVITY_LEGACY_EQUIVALENT_FACTORS, {5.0: 1.0, 18.0: 1.0})
        self.assertEqual(app.sensitivity_factor(5.0), 1.0)
        self.assertEqual(app.sensitivity_factor(18.0), 1.0)
        with self.assertRaises(ValueError):
            app.sensitivity_factor(10.0)
        self.assertIn("uncalibrated", app.SENSITIVITY_CALIBRATION_ID)
        self.assertFalse(app.OFFSET_GATE_ENABLED)
        self.assertTrue(app.POLARITY_GATE_ENABLED)
        self.assertFalse(app.BATTERY_MONITORING_ENABLED)
        self.assertFalse(app.REFERENCE_GATE_ENABLED)

    def test_csv_fields_carry_both_frequencies_and_no_noise_columns(self):
        for column in (
            "low_hz", "high_hz", "pwm_duty",
            "sensitivity_mv",
            "sensitivity_5hz_raw_mv", "sensitivity_5hz_legacy_equivalent_mv",
            "sensitivity_5hz_factor", "sensitivity_5hz_min_legacy_mv", "sensitivity_5hz_outcome",
            "sensitivity_18hz_raw_mv", "sensitivity_18hz_legacy_equivalent_mv",
            "sensitivity_18hz_factor", "sensitivity_18hz_min_legacy_mv", "sensitivity_18hz_outcome",
            "ratio_18_over_5_raw", "ratio_18_over_5_corrected", "ratio_min", "ratio_max",
            "ratio_outcome", "ratio_tray_100_percent_flag",
            "sensitivity_calibration_id", "sensitivity_gate_enabled",
            "polarity", "polarity_good_bad", "polarity_5hz", "polarity_18hz",
            "snr_5hz_db", "snr_18hz_db", "measured_5hz_sync_hz", "measured_18hz_sync_hz",
            "stab5_stabilized", "stab5_pwm_on_seconds", "stab5_data_source",
            "stab18_stabilized", "stab18_measurement_cycles", "stab18_data_source",
            "reference_check_mv", "measure_attempts", "number_attempt", "offset_initial_v",
        ):
            self.assertIn(column, app.CSV_FIELDS)
        self.assertEqual(len(app.CSV_FIELDS), len(set(app.CSV_FIELDS)))
        self.assertFalse(any(name.startswith("noise_") for name in app.CSV_FIELDS))
        self.assertNotIn("pwm_hz", app.CSV_FIELDS)
        self.assertIn(app.FREQUENCY_TRACKING_FAILURE_MODE, app.FAILURE_MODE_CHOICES)
        self.assertIn(app.UNSTABLE_FAILURE_MODE, app.FAILURE_MODE_CHOICES)
        self.assertFalse(hasattr(app, "NOISE_TEST_ENABLED"))
        self.assertFalse(hasattr(app, "NoiseCaptureReport"))

    def test_tp443_helpers(self):
        self.assertEqual(app.tp443_minimum_legacy_mv("-25 filter", 5.0), 1200.0)
        self.assertEqual(app.tp443_minimum_legacy_mv("-25 filter", 18.0), 720.0)
        with self.assertRaises(ValueError):
            app.tp443_minimum_legacy_mv("-25 filter", 10.0)
        with self.assertRaises(ValueError):
            app.tp443_minimum_legacy_mv("-625 filter", 5.0)
        self.assertAlmostEqual(app.ratio_18_over_5(1000.0, 720.0), 0.72)
        self.assertIsNone(app.ratio_18_over_5(None, 720.0))
        self.assertIsNone(app.ratio_18_over_5(1000.0, None))
        self.assertIsNone(app.ratio_18_over_5(0.0, 720.0))
        self.assertIsNone(app.ratio_18_over_5(float("nan"), 720.0))
        self.assertEqual(app.ratio_outcome(0.70), app.OUTCOME_PASS)
        self.assertEqual(app.ratio_outcome(1.30), app.OUTCOME_PASS)
        self.assertEqual(app.ratio_outcome(0.699), app.OUTCOME_FAIL)
        self.assertEqual(app.ratio_outcome(1.301), app.OUTCOME_FAIL)
        self.assertEqual(app.ratio_outcome(None), "")
        self.assertEqual(app.sensitivity_outcome(1200.0, 1200.0), app.OUTCOME_PASS)
        self.assertEqual(app.sensitivity_outcome(1199.9, 1200.0), app.OUTCOME_FAIL)
        self.assertEqual(app.sensitivity_outcome(None, 1200.0), "")
        self.assertEqual(app.frequency_label(18.0), "18 Hz")
        self.assertEqual(app.legacy_equivalent_sensitivity_mv(2.0, 5.0), 2.0)
        with gated():
            self.assertEqual(app.legacy_equivalent_sensitivity_mv(2.0, 5.0), 4.0)
            self.assertEqual(app.legacy_equivalent_sensitivity_mv(2.0, 18.0), 6.0)

    def test_simulator_targets_cover_every_tp443_branch(self):
        low = app.TP443_MIN_LEGACY_LOW_MV
        self.assertAlmostEqual(app.simulated_legacy_sensitivity_mv("Known good", 5.0), low * 1.35)
        self.assertAlmostEqual(app.simulated_legacy_sensitivity_mv("Known good", 18.0), low * 1.35 * 0.95)
        self.assertAlmostEqual(app.simulated_legacy_sensitivity_mv("Low sensitivity", 5.0), low * 0.62)
        self.assertAlmostEqual(app.simulated_legacy_sensitivity_mv("Borderline sensitivity", 5.0), low * 1.02)
        self.assertAlmostEqual(
            app.simulated_legacy_sensitivity_mv("Borderline sensitivity", 18.0), low * 1.02 * 0.72
        )
        self.assertLess(app.simulated_legacy_sensitivity_mv("Low 18 Hz sensitivity", 18.0), 720.0)
        self.assertLess(app.simulated_legacy_sensitivity_mv("Low 18 Hz ratio", 18.0) / (low * 1.35), 0.70)
        self.assertGreater(app.simulated_legacy_sensitivity_mv("High 18 Hz ratio", 18.0) / (low * 1.35), 1.30)
        for case in app.SIM_TRACKING_CASES:
            self.assertIn(case, app.SIM_CASES)

    def test_simulated_capture_has_twenty_percent_sync_and_pulse_shape(self):
        waveform, sync, rate, offset = app.simulate_v6_startup_capture(
            app.DEFAULT_FILTER_SETUP, "Known good", frequency_hz=18.0, duration_s=2.0, offset_v=1.0
        )
        self.assertEqual(rate, 1000.0)
        self.assertEqual(offset, 1.0)
        self.assertEqual(len(waveform), 2000)
        self.assertAlmostEqual(float(np.mean(sync)), 0.20, places=2)
        # The response peaks at the end of the ON interval (positive polarity).
        edges = app.rising_edge_indices(sync)
        self.assertGreater(len(edges), 30)
        first, second = edges[0], edges[1]
        cycle = waveform[first:second]
        self.assertLess(int(np.argmax(cycle)), int(round(0.3 * len(cycle))))


# --------------------------------------------------------------------------- #
# Evaluation: gate off (calibration pending) and gate on
# --------------------------------------------------------------------------- #
class EvaluationPendingTests(unittest.TestCase):
    def test_good_part_passes_with_calibration_pending_warning(self):
        low = metrics_for(30.0, 5.0)
        high = metrics_for(12.0, 18.0)
        final, report = app.evaluate_tracking_result(1.1, low, high, app.DEFAULT_FILTER_SETUP)
        self.assertTrue(final.passed)
        self.assertEqual(final.fail_reasons, [])
        self.assertTrue(app.is_calibration_pending(final))
        self.assertEqual(app.result_outcome(final), app.OUTCOME_PASS)
        self.assertEqual(final.sensitivity_mv, 30.0)
        self.assertEqual(final.polarity, app.POSITIVE_POLARITY)
        self.assertIs(final.waveform_metrics, low)
        self.assertFalse(report.gate_enabled)
        self.assertEqual((report.low_outcome, report.high_outcome, report.ratio_outcome), ("", "", ""))
        self.assertFalse(report.tray_100_percent)
        self.assertAlmostEqual(report.ratio_raw, 0.4)
        self.assertAlmostEqual(report.ratio_corrected, 0.4)
        self.assertEqual(report.legacy_low_mv, 30.0)
        self.assertEqual(report.legacy_high_mv, 12.0)
        self.assertEqual(report.polarity_high, app.POSITIVE_POLARITY)
        self.assertAlmostEqual(report.snr_low_db, 20.0 * math.log10(20.0))
        fields = report.csv_fields()
        self.assertEqual(fields["sensitivity_gate_enabled"], "NO")
        self.assertEqual(fields["ratio_tray_100_percent_flag"], "NO")
        self.assertEqual(fields["ratio_18_over_5_raw"], "0.400000")
        self.assertEqual(fields["sensitivity_5hz_outcome"], "")

    def test_unstable_low_capture_fails_with_its_reason_and_skips_polarity(self):
        low = metrics_for(
            0.0, 5.0, polarity="NOT MEASURED", snr=None, stabilized=False,
            warnings=["Unstable at 5 Hz: Attempt 1 could not complete 5 consecutive stable deltas within 30.0 s."],
        )
        final, report = app.evaluate_tracking_result(1.0, low, None, app.DEFAULT_FILTER_SETUP)
        self.assertFalse(final.passed)
        self.assertEqual(
            final.fail_reasons,
            [
                "Unstable at 5 Hz: Attempt 1 could not complete 5 consecutive stable deltas within 30.0 s.",
                "18 Hz: waveform was not measured.",
            ],
        )
        self.assertIsNone(report.raw_low_mv)
        self.assertIsNone(report.ratio_raw)
        self.assertEqual(app.suggest_failure_mode(final), app.UNSTABLE_FAILURE_MODE)
        self.assertFalse(any(str(w).startswith("5 Hz: Unstable") for w in final.warnings))

    def test_polarity_and_snr_gates_apply_per_frequency(self):
        low = metrics_for(30.0, 5.0)
        high = metrics_for(12.0, 18.0, polarity=app.NEGATIVE_POLARITY)
        final, _ = app.evaluate_tracking_result(1.0, low, high, app.DEFAULT_FILTER_SETUP)
        self.assertFalse(final.passed)
        self.assertEqual(len(final.fail_reasons), 1)
        self.assertTrue(final.fail_reasons[0].startswith("18 Hz: polarity is NEGATIVE"))
        self.assertEqual(app.suggest_failure_mode(final), "RP - Reversed polarity")

        quiet = metrics_for(30.0, 5.0, snr=1.0)
        final, _ = app.evaluate_tracking_result(1.0, quiet, metrics_for(12.0, 18.0), app.DEFAULT_FILTER_SETUP)
        self.assertFalse(final.passed)
        self.assertTrue(final.fail_reasons[0].startswith("5 Hz: signal-to-noise too low"))
        self.assertEqual(app.suggest_failure_mode(final), "GO/D - Good offset/no signal")

        unverified = metrics_for(30.0, 5.0, snr=None)
        final, _ = app.evaluate_tracking_result(1.0, unverified, metrics_for(12.0, 18.0), app.DEFAULT_FILTER_SETUP)
        self.assertFalse(final.passed)
        self.assertIn("SNR unavailable", final.fail_reasons[0])

        with mock.patch.object(app, "POLARITY_GATE_ENABLED", False):
            final, _ = app.evaluate_tracking_result(1.0, low, high, app.DEFAULT_FILTER_SETUP)
            self.assertTrue(final.passed)

    def test_offset_band_is_recorded_but_not_gated_until_enabled(self):
        low, high = metrics_for(30.0, 5.0), metrics_for(12.0, 18.0)
        final, _ = app.evaluate_tracking_result(5.0, low, high, app.DEFAULT_FILTER_SETUP)
        self.assertTrue(final.passed)
        self.assertEqual(final.offset_v, 5.0)
        final, _ = app.evaluate_tracking_result(None, low, high, app.DEFAULT_FILTER_SETUP)
        self.assertFalse(final.passed)
        self.assertEqual(final.fail_reasons[0], "Offset was not measured.")
        with mock.patch.object(app, "OFFSET_GATE_ENABLED", True):
            final, _ = app.evaluate_tracking_result(5.0, low, high, app.DEFAULT_FILTER_SETUP)
            self.assertFalse(final.passed)
            self.assertTrue(final.fail_reasons[0].startswith("Offset out of range: 5.000 V"))
            self.assertEqual(app.suggest_failure_mode(final), "HO - High offset")
            final, _ = app.evaluate_tracking_result(0.2, low, high, app.DEFAULT_FILTER_SETUP)
            self.assertEqual(app.suggest_failure_mode(final), "LO - Low offset")

    def test_not_measured_and_no_output_results_are_untouched(self):
        placeholder = app.build_not_measured_result("stream fault")
        self.assertEqual(app.result_outcome(placeholder), app.OUTCOME_NOT_MEASURED)
        self.assertEqual(app.suggest_failure_mode(placeholder), app.DEFAULT_NOT_MEASURED_REASON)
        metrics, final = app.build_no_output_sensor_result(0.01, input_range_v=5.0)
        self.assertFalse(final.passed)
        self.assertEqual(app.suggest_failure_mode(final), "D - No offset")
        self.assertIn("5 Hz and 18 Hz captures were not run", final.warnings[0])
        self.assertEqual(metrics.waveform_v.size, 0)


class EvaluationGatedTests(unittest.TestCase):
    def setUp(self):
        patcher = gated()
        patcher.start()
        self.addCleanup(patcher.stop)

    def evaluate(self, raw_low, raw_high, offset=1.0):
        return app.evaluate_tracking_result(
            offset, metrics_for(raw_low, 5.0), metrics_for(raw_high, 18.0), app.DEFAULT_FILTER_SETUP
        )

    def test_factors_correct_each_frequency_and_the_ratio(self):
        # raw 700 x2 = 1400 mV @ 5 Hz, raw 350 x3 = 1050 mV @ 18 Hz -> ratio 0.75.
        final, report = self.evaluate(700.0, 350.0)
        self.assertTrue(final.passed)
        self.assertFalse(app.is_calibration_pending(final))
        self.assertEqual(report.legacy_low_mv, 1400.0)
        self.assertEqual(report.legacy_high_mv, 1050.0)
        self.assertAlmostEqual(report.ratio_raw, 0.5)
        self.assertAlmostEqual(report.ratio_corrected, 0.75)
        self.assertEqual((report.low_outcome, report.high_outcome, report.ratio_outcome), ("PASS", "PASS", "PASS"))
        self.assertFalse(report.tray_100_percent)
        fields = report.csv_fields()
        self.assertEqual(fields["sensitivity_5hz_factor"], "2.000000")
        self.assertEqual(fields["sensitivity_18hz_factor"], "3.000000")
        self.assertEqual(fields["sensitivity_gate_enabled"], "YES")
        self.assertEqual(fields["ratio_outcome"], "PASS")

    def test_spec_one_and_two_minimums(self):
        final, report = self.evaluate(599.9, 350.0)   # 1199.8 mV @ 5 Hz
        self.assertFalse(final.passed)
        self.assertEqual(report.low_outcome, app.OUTCOME_FAIL)
        self.assertTrue(final.fail_reasons[0].startswith("Sensitivity too low at 5 Hz"))
        self.assertTrue(report.tray_100_percent)
        self.assertEqual(app.suggest_failure_mode(final), "LS - Low sensitivity")

        final, report = self.evaluate(600.0, 239.9)   # 1200 @ 5 Hz, 719.7 @ 18 Hz
        self.assertFalse(final.passed)
        self.assertEqual(report.low_outcome, app.OUTCOME_PASS)
        self.assertEqual(report.high_outcome, app.OUTCOME_FAIL)
        self.assertTrue(final.fail_reasons[0].startswith("Sensitivity too low at 18 Hz"))
        # 719.7 / 1200 = 0.60 also fails the ratio.
        self.assertEqual(report.ratio_outcome, app.OUTCOME_FAIL)
        self.assertEqual(len(final.fail_reasons), 2)

    def test_spec_three_ratio_band_and_failure_mode(self):
        final, report = self.evaluate(700.0, 322.0)   # 1400 / 966 -> 0.69
        self.assertFalse(final.passed)
        self.assertEqual(report.ratio_outcome, app.OUTCOME_FAIL)
        self.assertEqual(len(final.fail_reasons), 1)
        self.assertTrue(final.fail_reasons[0].startswith("Frequency tracking ratio out of range"))
        self.assertEqual(app.suggest_failure_mode(final), app.FREQUENCY_TRACKING_FAILURE_MODE)
        self.assertTrue(report.tray_100_percent)

        final, report = self.evaluate(700.0, 612.0)   # 1400 / 1836 -> 1.311
        self.assertFalse(final.passed)
        self.assertEqual(report.ratio_outcome, app.OUTCOME_FAIL)
        self.assertEqual(app.suggest_failure_mode(final), app.FREQUENCY_TRACKING_FAILURE_MODE)

        final, report = self.evaluate(700.0, 606.0)   # 1400 / 1818 -> 1.2986
        self.assertTrue(final.passed)
        self.assertEqual(report.ratio_outcome, app.OUTCOME_PASS)

    def test_spec_four_tray_flag_at_or_below_point_seven_two(self):
        # 1400 / 1008 -> exactly 0.72: passes spec 3, flags the tray.
        final, report = self.evaluate(700.0, 336.0)
        self.assertTrue(final.passed)
        self.assertEqual(report.ratio_outcome, app.OUTCOME_PASS)
        self.assertTrue(report.tray_100_percent)
        self.assertTrue(any("measured 100 %" in w for w in final.warnings))
        self.assertEqual(report.csv_fields()["ratio_tray_100_percent_flag"], "YES")
        # 0.73 -> no flag.
        final, report = self.evaluate(700.0, 340.7)
        self.assertTrue(final.passed)
        self.assertFalse(report.tray_100_percent)
        self.assertFalse(any("measured 100 %" in w for w in final.warnings))

    def test_gate_reads_the_flags_at_call_time(self):
        # Turning the gate back off mid-test must restore the pending policy.
        with mock.patch.object(app, "SENSITIVITY_GATE_ENABLED", False):
            final, report = self.evaluate(10.0, 4.0)
            self.assertTrue(final.passed)
            self.assertTrue(app.is_calibration_pending(final))
            self.assertFalse(report.gate_enabled)


# --------------------------------------------------------------------------- #
# Batch CSV
# --------------------------------------------------------------------------- #
class CsvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(app, "results_root_dir", lambda: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _capture_report(self, frequency_hz, case="Known good"):
        _wf, _sy, _rate, _off, analysis = prepared_capture(case, frequency_hz)
        return app.StabilityCaptureReport.from_analysis(analysis, data_source="test", pwm_on_seconds=12.5)

    def test_row_carries_both_readings_ratio_and_telemetry(self):
        low, high = metrics_for(30.0, 5.0), metrics_for(12.0, 18.0)
        final, report = app.evaluate_tracking_result(1.1, low, high, app.DEFAULT_FILTER_SETUP)
        path = app.batch_results_path("L7")
        app.append_result_csv(
            path,
            batch_number="L7", sensor_number=3, sensor_id="L7-3", tester_name="Tech",
            filter_setup=app.DEFAULT_FILTER_SETUP, pwm_channel=app.EMITTER_PWM_CHANNEL,
            pwm_duty=app.EMITTER_PWM_DUTY_CYCLE, final_result=final, comment=" ok ",
            snapshot_paths=[], tracking_report=report,
            capture_report=self._capture_report(5.0), capture_report_high=self._capture_report(18.0),
            offset_initial_v=0.95, measure_attempts=2, number_attempt=1,
        )
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, app.CSV_FIELDS)
            row = next(reader)
        self.assertEqual(row["model"], "449M18")
        self.assertEqual(row["pass_fail"], "PASS")
        self.assertEqual((row["low_hz"], row["high_hz"], row["pwm_duty"]), ("5", "18", "20"))
        self.assertEqual(row["sensitivity_mv"], "30.000000")
        self.assertEqual(row["sensitivity_5hz_raw_mv"], "30.000000")
        self.assertEqual(row["sensitivity_18hz_raw_mv"], "12.000000")
        self.assertEqual(row["ratio_18_over_5_raw"], "0.400000")
        self.assertEqual(row["ratio_18_over_5_corrected"], "0.400000")
        self.assertEqual(row["sensitivity_gate_enabled"], "NO")
        self.assertEqual(row["sensitivity_calibration_id"], app.SENSITIVITY_CALIBRATION_ID)
        self.assertEqual(row["ratio_tray_100_percent_flag"], "NO")
        self.assertEqual(row["polarity"], "POSITIVE")
        self.assertEqual(row["polarity_good_bad"], "GOOD")
        self.assertEqual(row["polarity_18hz"], "POSITIVE")
        self.assertEqual(row["offset_v"], "1.100000")
        self.assertEqual(row["offset_initial_v"], "0.95000")
        self.assertEqual(row["operator_comments"], "ok")
        self.assertEqual((row["measure_attempts"], row["number_attempt"]), ("2", "1"))
        self.assertEqual(row["stab5_stabilized"], "YES")
        self.assertEqual(row["stab5_measurement_cycles"], "20")
        self.assertEqual(row["stab18_stabilized"], "YES")
        self.assertEqual(row["stab18_measurement_cycles"], "4")   # blocks of 9 cycles
        self.assertEqual(row["stab18_pwm_on_seconds"], "12.500")
        self.assertEqual(row["stab5_data_source"], "test")
        self.assertEqual(row["failure_mode_tag"], "")
        self.assertTrue(row["snr_5hz_db"])

    def test_failed_row_requires_a_failure_mode_and_keeps_old_headers(self):
        low = metrics_for(30.0, 5.0, polarity=app.NEGATIVE_POLARITY)
        final, report = app.evaluate_tracking_result(1.0, low, metrics_for(12.0, 18.0), app.DEFAULT_FILTER_SETUP)
        path = app.batch_results_path("L8")
        kwargs = dict(
            batch_number="L8", sensor_number=1, sensor_id="L8-1", tester_name="T",
            filter_setup=app.DEFAULT_FILTER_SETUP, pwm_channel=app.EMITTER_PWM_CHANNEL,
            pwm_duty=20.0, final_result=final, comment="", snapshot_paths=[], tracking_report=report,
        )
        with self.assertRaises(ValueError):
            app.append_result_csv(path, failure_mode="bogus", **kwargs)
        app.append_result_csv(path, **kwargs)   # suggested mode is used
        with path.open(newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["pass_fail"], "FAIL")
        self.assertEqual(row["failure_mode_tag"], "RP")
        self.assertIn("polarity", row["fail_reasons"])
        # A pre-existing header wins: only its columns are written.
        legacy = self.root / "legacy.csv"
        legacy.write_text("sensor_id,pass_fail\n", encoding="utf-8")
        app.append_result_csv(legacy, **kwargs)
        lines = legacy.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "sensor_id,pass_fail")
        self.assertEqual(lines[1], "L8-1,FAIL")

    def test_summary_reader_shows_both_readings_and_tray_flags(self):
        low, high = metrics_for(30.0, 5.0), metrics_for(12.0, 18.0)
        final, report = app.evaluate_tracking_result(1.1, low, high, app.DEFAULT_FILTER_SETUP)
        path = app.batch_results_path("L9")
        app.append_result_csv(
            path, batch_number="L9", sensor_number=1, sensor_id="L9-1", tester_name="T",
            filter_setup=app.DEFAULT_FILTER_SETUP, pwm_channel="GPIO33", pwm_duty=20.0,
            final_result=final, comment="", snapshot_paths=[], tracking_report=report,
        )
        with gated():
            final2, report2 = app.evaluate_tracking_result(
                1.0, metrics_for(700.0, 5.0), metrics_for(336.0, 18.0), app.DEFAULT_FILTER_SETUP
            )
            app.append_result_csv(
                path, batch_number="L9", sensor_number=2, sensor_id="L9-2", tester_name="T",
                filter_setup=app.DEFAULT_FILTER_SETUP, pwm_channel="GPIO33", pwm_duty=20.0,
                final_result=final2, comment="", snapshot_paths=[], tracking_report=report2,
            )
        harness = SimpleNamespace(
            _fmt=app.EmitterTesterApp._fmt.__get__(SimpleNamespace(), SimpleNamespace),
        )
        harness._summary_reading = lambda equivalent, raw: app.EmitterTesterApp._summary_reading(harness, equivalent, raw)
        rows = app.EmitterTesterApp._read_summary_rows(harness, path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "L9-1")
        self.assertEqual(rows[0][2], "30.00 mV")
        self.assertEqual(rows[0][3], "12.00 mV")
        self.assertEqual(rows[0][4], "0.400")
        self.assertEqual(rows[0][-1], "PASS")
        self.assertEqual(rows[1][2], "1400.0 mV eq (700.00 raw)")
        self.assertEqual(rows[1][4], "0.720")
        self.assertEqual(app.EmitterTesterApp._read_tray_flags(harness, path), ["L9-2"])
        outcomes = app.summarize_batch_outcomes([row[-1] for row in rows])
        self.assertEqual((outcomes["tested"], outcomes["passed"]), (2, 2))


# --------------------------------------------------------------------------- #
# Hardware sequence
# --------------------------------------------------------------------------- #
class HardwareFlowTests(unittest.TestCase):
    def test_sequence_is_offset_then_five_then_eighteen_then_settled_offset(self):
        device = FakeMeasurementDevice(offset_sequence=[0.9, 1.15])
        with mock.patch.object(app.time, "sleep") as sleep:
            harness, (metrics, final, offset) = run_hardware(device)
        sleep.assert_not_called()
        self.assertEqual(
            device.calls,
            ["pwm_off", "offset", "pwm_on", "capture", "pwm_off", "pwm_on", "capture", "pwm_off", "offset"],
        )
        self.assertEqual(
            [(k["frequency_hz"], k["duty_cycle_percent"], k["channel"]) for k in device.configure_kwargs],
            [(5.0, 20.0, "GPIO33"), (18.0, 20.0, "GPIO33")],
        )
        self.assertEqual([k["expected_frequency_hz"] for k in device.capture_kwargs], [5.0, 18.0])
        self.assertEqual([k["measurement_cycles"] for k in device.capture_kwargs], [20, 36])
        self.assertEqual([k["sync_frequency_tolerance_hz"] for k in device.capture_kwargs], [0.05, 0.18])
        self.assertTrue(final.passed)
        self.assertTrue(app.is_calibration_pending(final))
        self.assertEqual(offset, 1.15)                      # settled re-read is the verdict
        self.assertEqual(harness.last_offset_initial_v, 0.9)
        self.assertEqual(metrics.cycles_used, 20)
        self.assertEqual(harness.last_metrics_high.cycles_used, 36)
        self.assertEqual(harness.last_capture_report.data_source, "esp32_449m18")
        self.assertEqual(harness.last_capture_report_high.data_source, "esp32_449m18")
        self.assertEqual(harness.last_capture_report_high.measurement_cycles, 4)
        self.assertAlmostEqual(harness.last_capture_report.pwm_on_seconds, 5.0)
        tracking = harness.last_tracking_report
        self.assertIsNotNone(tracking.ratio_raw)
        self.assertAlmostEqual(tracking.ratio_raw, 0.94, delta=0.02)
        self.assertEqual(tracking.polarity_low, app.POSITIVE_POLARITY)
        self.assertEqual(tracking.polarity_high, app.POSITIVE_POLARITY)
        self.assertGreaterEqual(harness.preview_count, 2)
        steps = sorted({(step, total, label) for step, total, label, _fraction in harness.progress_events})
        self.assertEqual(steps, [(1, 3, "Offset"), (2, 3, "5 Hz sensitivity"), (3, 3, "18 Hz sensitivity")])
        self.assertIsNone(harness.last_reference_check_mv)
        self.assertNotIn("reference", device.calls)
        self.assertTrue(any("not gated" in text for text in harness.status_texts))

    def test_unstable_five_hz_capture_skips_eighteen_hz(self):
        device = FakeMeasurementDevice(case_name="Never stabilizes")
        harness, (metrics, final, offset) = run_hardware(device)
        self.assertEqual(device.calls, ["pwm_off", "offset", "pwm_on", "capture", "pwm_off", "offset"])
        self.assertFalse(final.passed)
        self.assertTrue(final.fail_reasons[0].startswith("Unstable at 5 Hz"))
        self.assertIn("18 Hz: waveform was not measured.", final.fail_reasons)
        self.assertTrue(any("18 Hz capture skipped" in w for w in final.warnings))
        self.assertIsNone(harness.last_metrics_high)
        self.assertIsNone(harness.last_capture_report_high)
        self.assertTrue(harness.last_capture_report.unstable)
        self.assertFalse(metrics.stabilized)
        self.assertIsNone(final.sensitivity_mv)
        self.assertEqual(app.suggest_failure_mode(final), app.UNSTABLE_FAILURE_MODE)

    def test_unstable_eighteen_hz_capture_fails_the_part(self):
        device = FakeMeasurementDevice(case_by_frequency={18.0: "Never stabilizes"})
        harness, (metrics, final, offset) = run_hardware(device)
        self.assertEqual(device.calls.count("capture"), 2)
        self.assertFalse(final.passed)
        self.assertTrue(metrics.stabilized)
        self.assertTrue(final.fail_reasons[0].startswith("Unstable at 18 Hz"))
        self.assertTrue(harness.last_capture_report_high.unstable)
        self.assertIsNone(harness.last_tracking_report.raw_high_mv)
        self.assertIsNone(harness.last_tracking_report.ratio_raw)

    def test_wrong_polarity_and_ratio_cases_reach_the_verdict(self):
        device = FakeMeasurementDevice(case_by_frequency={18.0: "Low 18 Hz ratio"})
        harness, (metrics, final, offset) = run_hardware(device)
        self.assertTrue(final.passed)   # gate off: recorded, not judged
        self.assertLess(harness.last_tracking_report.ratio_raw, 0.60)
        with gated({5.0: 1.0, 18.0: 1.0}):
            device = FakeMeasurementDevice(case_by_frequency={18.0: "Low 18 Hz ratio"})
            harness, (metrics, final, offset) = run_hardware(device)
            self.assertFalse(final.passed)
            self.assertEqual(harness.last_tracking_report.ratio_outcome, app.OUTCOME_FAIL)
            self.assertEqual(app.suggest_failure_mode(final), app.FREQUENCY_TRACKING_FAILURE_MODE)

    def test_missing_sensor_gets_a_wake_up_poll_then_the_askable_error(self):
        device = FakeMeasurementDevice(offset=0.01)
        with mock.patch.object(app.time, "sleep") as sleep, mock.patch.object(
            app.time, "monotonic", side_effect=[0.0, 0.0, 2.0, 6.0, 6.0, 6.0]
        ):
            with self.assertRaises(app.NoSensorDetectedError) as raised:
                run_hardware(device)
        self.assertEqual(raised.exception.offset_v, 0.01)
        self.assertIn("449 M18", str(raised.exception))
        sleep.assert_called()
        self.assertNotIn("pwm_on", device.calls)

    def test_capture_exception_still_turns_pwm_off(self):
        device = FakeMeasurementDevice(error=app.Esp32BackendError("stream stalled"))
        with self.assertRaisesRegex(app.Esp32BackendError, "stream stalled"):
            run_hardware(device)
        self.assertEqual(device.calls[-1], "pwm_off")
        self.assertEqual(device.calls.count("pwm_on"), 1)

    def test_stream_integrity_errors_are_retried_per_drive(self):
        class FlakyDevice(FakeMeasurementDevice):
            def __init__(self):
                super().__init__()
                self.failures_left = 1

            def read_waveform_until_stable(self, **kwargs):
                if self.failures_left:
                    self.failures_left -= 1
                    self.calls.append("capture")
                    raise app.StreamIntegrityError("micro-gap")
                return super().read_waveform_until_stable(**kwargs)

        device = FlakyDevice()
        harness, (metrics, final, offset) = run_hardware(device)
        self.assertTrue(final.passed)
        self.assertEqual(device.calls.count("capture"), 3)
        self.assertEqual(device.calls.count("pwm_on"), 3)
        self.assertTrue(any("glitch" in text for text in harness.status_texts))

    def test_stalled_capture_is_restarted_by_the_app_and_logged(self):
        # 2026-09-03: a stall used to be a dead end (the technician pressed
        # Re-measure); it is now a StreamIntegrityError and gets the same
        # bounded restart as a micro-gap failure, with the attributed error
        # on the status line and in the attempts log.
        stall = app.StreamStalledError(
            "ESP32 waveform stream stalled after 3000/72001 samples (no sample for 2.1 s). "
            "The board kept sampling [host-stall]",
            kind=app.STALL_KIND_HOST,
            received=3000,
        )
        device = FakeMeasurementDevice(capture_errors=[stall])
        harness, (_metrics, final, _offset) = run_hardware(device)
        self.assertTrue(final.passed)
        self.assertEqual(device.calls.count("capture"), 3)
        self.assertTrue(
            any(text.startswith("Stream stalled during the") for text in harness.status_texts),
            harness.status_texts,
        )
        self.assertEqual(len(harness.stream_retries), 1)
        phase, attempt, exc = harness.stream_retries[0]
        self.assertIn("capture", phase)
        self.assertEqual((attempt, exc), (1, stall))

    def test_persistent_stall_fails_after_the_bounded_restarts_with_pwm_off(self):
        stall = app.StreamStalledError(
            "ESP32 waveform stream stalled after 3000/72001 samples. [board-silent]",
            kind=app.STALL_KIND_BOARD_SILENT,
            received=3000,
        )
        device = FakeMeasurementDevice(error=stall)
        with self.assertRaises(app.StreamStalledError):
            run_hardware(device)
        self.assertEqual(
            device.calls.count("capture"), app.REFERENCE_READING_STREAM_RETRIES + 1
        )
        self.assertEqual(device.calls[-1], "pwm_off")

    def test_reference_gate_when_enabled_runs_at_ten_hz_fifty_percent(self):
        with mock.patch.object(app, "REFERENCE_GATE_ENABLED", True):
            device = FakeMeasurementDevice()
            harness, (metrics, final, offset) = run_hardware(device)
            self.assertEqual(
                device.calls,
                ["pwm_off", "offset", "pwm_on", "reference", "pwm_off",
                 "pwm_on", "capture", "pwm_off", "pwm_on", "capture", "pwm_off", "offset"],
            )
            self.assertEqual(
                [(k["frequency_hz"], k["duty_cycle_percent"]) for k in device.configure_kwargs],
                [(10.0, 50.0), (5.0, 20.0), (18.0, 20.0)],
            )
            self.assertEqual(harness.last_reference_check_mv, 100.0)
            self.assertTrue(final.passed)
            steps = sorted({(step, total) for step, total, _l, _f in harness.progress_events})
            self.assertEqual(steps, [(1, 4), (2, 4), (3, 4), (4, 4)])

    def test_high_offset_fails_fast_only_while_the_offset_gate_is_on(self):
        device = FakeMeasurementDevice(offset=4.9)
        harness, (metrics, final, offset) = run_hardware(device)
        self.assertTrue(final.passed)   # not gated: both captures still run
        self.assertEqual(device.calls.count("capture"), 2)
        with mock.patch.object(app, "OFFSET_GATE_ENABLED", True):
            device = FakeMeasurementDevice(offset=4.9)
            harness, (metrics, final, offset) = run_hardware(device)
            self.assertEqual(device.calls, ["pwm_off", "offset"])
            self.assertFalse(final.passed)
            self.assertTrue(final.fail_reasons[0].startswith("Offset out of range"))
            self.assertIsNone(harness.last_tracking_report)
            self.assertEqual(app.suggest_failure_mode(final), "HO - High offset")


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #
class SimulatorTests(unittest.TestCase):
    def test_every_case_completes_both_drives_with_the_pending_policy(self):
        expected = {
            "Random good sensor": True,
            "Known good": True,
            "Low sensitivity": True,           # gate off: recorded only
            "Wrong polarity": False,
            "Low offset": True,                # offset band not gated
            "High offset": True,
            "Borderline sensitivity": True,
            "Never stabilizes": False,
            "Low 18 Hz sensitivity": True,
            "Low 18 Hz ratio": True,
            "High 18 Hz ratio": True,
        }
        self.assertEqual(set(expected), set(app.SIM_CASES))
        for case, should_pass in expected.items():
            with self.subTest(case=case):
                harness, (metrics, final, offset) = run_simulator(case)
                self.assertEqual(final.passed, should_pass, final.fail_reasons)
                self.assertEqual(harness.last_capture_report.data_source, "simulator")
                if case == "Never stabilizes":
                    self.assertTrue(harness.last_capture_report.timed_out)
                    self.assertIsNone(harness.last_metrics_high)
                    continue
                self.assertTrue(metrics.stabilized)
                self.assertEqual(metrics.cycles_used, 20)
                self.assertEqual(harness.last_metrics_high.cycles_used, 36)
                self.assertEqual(harness.last_capture_report_high.measurement_cycles, 4)
                self.assertLess(harness.last_capture_report.stabilization_seconds, 30.0)
                self.assertLess(harness.last_capture_report_high.stabilization_seconds, 30.0)
                tracking = harness.last_tracking_report
                self.assertIsNotNone(tracking.ratio_raw)
                self.assertTrue(app.is_calibration_pending(final) or not final.passed)

    def test_gated_simulator_cases_exercise_every_spec(self):
        with gated({5.0: 1.0, 18.0: 1.0}):
            outcomes = {}
            for case in ("Known good", "Low sensitivity", "Low 18 Hz sensitivity",
                         "Low 18 Hz ratio", "High 18 Hz ratio", "Borderline sensitivity"):
                harness, (metrics, final, offset) = run_simulator(case)
                tracking = harness.last_tracking_report
                outcomes[case] = (final.passed, tracking.low_outcome, tracking.high_outcome,
                                  tracking.ratio_outcome, tracking.tray_100_percent)
        self.assertEqual(outcomes["Known good"], (True, "PASS", "PASS", "PASS", False))
        self.assertEqual(outcomes["Low sensitivity"][:2], (False, "FAIL"))
        self.assertEqual(outcomes["Low 18 Hz sensitivity"][:3], (False, "PASS", "FAIL"))
        self.assertEqual(outcomes["Low 18 Hz ratio"], (False, "PASS", "PASS", "FAIL", True))
        self.assertEqual(outcomes["High 18 Hz ratio"], (False, "PASS", "PASS", "FAIL", True))
        # Exactly on the 0.72 tray threshold: spec 3 passes, spec 4 flags.
        passed, low, high, ratio, tray = outcomes["Borderline sensitivity"]
        self.assertTrue(passed)
        self.assertEqual((low, high, ratio), ("PASS", "PASS", "PASS"))
        self.assertTrue(tray)

    def test_simulator_reuses_one_offset_for_both_drives(self):
        harness, (metrics, final, offset) = run_simulator("Known good")
        self.assertEqual(metrics.offset_v, offset)
        self.assertEqual(harness.last_metrics_high.offset_v, offset)
        self.assertEqual(harness.last_offset_initial_v, offset)


# --------------------------------------------------------------------------- #
# Snapshots and autosave
# --------------------------------------------------------------------------- #
class SnapshotTests(unittest.TestCase):
    def test_detail_lines_name_the_drive_and_ratio(self):
        _wf, _sy, _rate, _off, analysis = prepared_capture("Known good", 18.0)
        report = app.StabilityCaptureReport.from_analysis(analysis, data_source="test")
        metrics = metrics_for(12.0, 18.0)
        metrics.offset_v = 1.0
        tracking = app.FrequencyTrackingReport.from_metrics(
            metrics_for(30.0, 5.0), metrics, filter_setup=app.DEFAULT_FILTER_SETUP
        )
        lines = app.snapshot_detail_lines("B", "B-1", metrics, "note", report, frequency_hz=18.0, tracking=tracking)
        text = "\n".join(lines)
        self.assertIn("Drive: 18 Hz / 20% duty", text)
        self.assertIn("Sensitivity 18 Hz: 12.0 mV legacy-equivalent", text)
        self.assertIn("CALIBRATION PENDING", text)
        self.assertIn("Raw sensitivity 18 Hz: 12.000 mV raw ESP32", text)
        self.assertIn("Ratio 18/5 Hz: raw 0.400, corrected 0.400", text)
        self.assertIn("Stability: stabilized", text)
        self.assertIn("Comment: note", text)

    def test_unstable_bundle_is_saved_per_drive_before_the_row(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            app, "results_root_dir", lambda: Path(tmp)
        ), mock.patch.object(app.messagebox, "showerror") as showerror:
            harness = MeasurementHarness(FakeMeasurementDevice(case_by_frequency={18.0: "Never stabilizes"}))
            metrics, final, offset = app.EmitterTesterApp._hardware_measurement(
                harness, app.DEFAULT_FILTER_SETUP, app.WAVEFORM_INPUT_RANGE_V, app.EMITTER_PWM_CHANNEL,
                5.0, 20.0, False, harness.measure_token, lambda cb: cb(),
            )
            harness.last_metrics = metrics
            harness.last_result = final
            harness.batch_number = "B2"
            harness.current_sensor_number = 1
            harness.current_sensor_id = "B2-1"
            harness.tester_name = "T"
            harness.filter_setup = app.DEFAULT_FILTER_SETUP
            harness.snapshot_paths = []
            harness.stability_diagnostics_saved = False
            harness.stability_diagnostics_high_saved = False
            harness.notes_var = SimpleNamespace(get=lambda: "")
            harness.failure_mode_var = SimpleNamespace(get=lambda: app.UNSTABLE_FAILURE_MODE)
            harness.battery_v = None
            harness.measure_attempts = 1
            harness.number_attempt = 1
            harness.result_saved = False
            harness.delete_autosave = lambda: None
            harness._log_attempt = lambda *a, **k: None
            harness.update_navigation_state = lambda: None
            self.assertTrue(app.EmitterTesterApp.save_current_sensor(harness))
            showerror.assert_not_called()
            self.assertTrue(harness.stability_diagnostics_high_saved)
            self.assertFalse(harness.stability_diagnostics_saved)   # 5 Hz was stable
            names = [path.name for path in harness.snapshot_paths]
            self.assertTrue(any("unstable_18hz" in name and name.endswith(".png") for name in names), names)
            with app.batch_results_path("B2").open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["pass_fail"], "FAIL")
            self.assertEqual(row["failure_mode_tag"], "Unstable")
            self.assertEqual(row["stab18_stabilized"], "NO")
            self.assertEqual(row["stab5_stabilized"], "YES")
            self.assertIn("unstable_18hz", row["waveform_snapshot_paths"])


# --------------------------------------------------------------------------- #
# Launchers
# --------------------------------------------------------------------------- #
class LauncherTests(unittest.TestCase):
    def test_launchers_carry_only_the_449_identity(self):
        texts = {
            name: (APP_DIR / name).read_text(encoding="utf-8")
            for name in (
                "run_eltec_449m18_esp32_tester.cmd",
                "run_eltec_449m18_esp32_tester.sh",
                "install_windows_launcher.ps1",
                "install_xubuntu_launcher.sh",
            )
        }
        for name, text in texts.items():
            with self.subTest(name=name):
                lowered = text.lower()
                self.assertIn("449", text)
                for forbidden in ("405m22", "405 m22", "406mca", "v6_esp32", "v6_1_esp32", "1 hz"):
                    self.assertNotIn(forbidden, lowered, f"{forbidden} leaked into {name}")
        self.assertIn("eltec_449m18_esp32_tester.py", texts["run_eltec_449m18_esp32_tester.cmd"])
        self.assertIn("eltec-449m18-esp32", texts["run_eltec_449m18_esp32_tester.cmd"])
        self.assertIn("ELTEC_PYTHON", texts["run_eltec_449m18_esp32_tester.cmd"])
        self.assertIn("eltec-449m18-esp32", texts["run_eltec_449m18_esp32_tester.sh"])
        self.assertIn("run_eltec_449m18_esp32_tester.cmd", texts["install_windows_launcher.ps1"])
        self.assertIn("Eltec 449 M18 ESP32 Tester", texts["install_windows_launcher.ps1"])
        self.assertIn("com.eltec.449m18-esp32-tester.desktop", texts["install_xubuntu_launcher.sh"])
        self.assertIn("Name=Eltec 449 M18 ESP32 Tester", texts["install_xubuntu_launcher.sh"])
        self.assertIn("Eltec_449M18_Test_Results", texts["install_windows_launcher.ps1"])

    @unittest.skipUnless(sys.platform == "win32", "Windows-only launcher scripts")
    def test_windows_installer_script_parses(self):
        installer = APP_DIR / "install_windows_launcher.ps1"
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
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
        run_script = APP_DIR / "run_eltec_449m18_esp32_tester.cmd"
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
                    "ELTEC_LAUNCHER_NO_DIALOG": "1",
                },
            )
            self.assertNotEqual(completed.returncode, 0)
            log_file = local_appdata / "eltec-449m18-esp32" / "launcher.log"
            self.assertTrue(log_file.is_file(), completed.stdout + completed.stderr)
            self.assertIn("ERROR:", log_file.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Tk smoke test (runs wherever a display exists; skipped headless)
# --------------------------------------------------------------------------- #
class GuiSmokeTests(unittest.TestCase):
    def _make_app(self):
        with mock.patch.object(app.EmitterTesterApp, "startup_probe", lambda self: None):
            try:
                root = app.EmitterTesterApp()
            except Exception as exc:  # no display (headless CI)
                self.skipTest(f"Tk unavailable: {exc}")
        root.withdraw()
        return root

    def test_title_and_every_step_renders_with_a_two_frequency_result(self):
        root = self._make_app()
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                app, "results_root_dir", lambda: Path(tmp)
            ):
                self.assertEqual(root.title(), "Eltec 449 M18 ESP32 Tester (5/18 Hz)")
                self.assertIsNone(root.stability_config_error)
                self.assertIn("TP443", root.filter_hint_var.get())
                self.assertIn("GATE DISABLED", root.filter_hint_var.get())
                root.update_idletasks()
                root.batch_var.set("B1")
                root.tester_var.set("Tech")
                # Start reads the sensor straight away now, so the
                # measurement is stubbed out: this is a rendering smoke test.
                with mock.patch.object(
                    app.EmitterTesterApp, "run_measurement", lambda self: None
                ):
                    root.start_batch()
                self.assertEqual(root.step, root.RESULT_STEP)
                root.update_idletasks()
                # Fake a finished two-frequency measurement and render it.
                harness, (metrics, final, offset) = run_simulator("Known good")
                root.step = root.RESULT_STEP
                root.last_metrics = metrics
                root.last_result = final
                root.last_capture_report = harness.last_capture_report
                root.last_metrics_high = harness.last_metrics_high
                root.last_capture_report_high = harness.last_capture_report_high
                root.last_tracking_report = harness.last_tracking_report
                root.show_details_var.set(True)
                root.show_live_var.set(True)
                root.render_step()
                root.update_idletasks()
                self.assertIsNotNone(root.wave_canvas)
                self.assertIsNotNone(root.wave_canvas_high)
                self.assertEqual(root.wave_canvas_high.waveform.size, harness.last_metrics_high.waveform_v.size)
                # A gated verdict with the tray flag renders its card too.
                with gated({5.0: 1.0, 18.0: 1.0}):
                    harness2, (metrics2, final2, offset2) = run_simulator("Borderline sensitivity")
                root.last_metrics = metrics2
                root.last_result = final2
                root.last_capture_report = harness2.last_capture_report
                root.last_metrics_high = harness2.last_metrics_high
                root.last_capture_report_high = harness2.last_capture_report_high
                root.last_tracking_report = harness2.last_tracking_report
                self.assertTrue(harness2.last_tracking_report.tray_100_percent)
                root.render_step()
                root.update_idletasks()
                # Advanced options dialog and batch summary window build.
                root.open_advanced_options()
                root.update_idletasks()
                root._advanced_dialog.destroy()
                root.show_batch_summary_window("B1", app.batch_results_path("B1"))
                root.update_idletasks()
        finally:
            root.destroy()


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


if __name__ == "__main__":
    unittest.main()
