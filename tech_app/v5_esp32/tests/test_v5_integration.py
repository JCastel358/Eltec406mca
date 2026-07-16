from __future__ import annotations

import csv
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np


V5_DIR = Path(__file__).resolve().parents[1]
if str(V5_DIR) not in sys.path:
    sys.path.insert(0, str(V5_DIR))

import eltec_406mca_esp32_tester as app  # noqa: E402


class ValueVar:
    """Tiny StringVar stand-in for tests that deliberately avoid creating Tk."""

    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


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
        if self.firmware_samples_sent is None:
            return None
        return self.received_samples == self.firmware_samples_sent

    @property
    def rate_error_percent(self):
        return (
            (self.measured_rate_hz - self.expected_rate_hz)
            / self.expected_rate_hz
            * 100.0
        )


def stream_samples(count: int):
    """Generate a stable DUT waveform with the firmware's digital 0/1 sync."""

    samples = []
    for index in range(count):
        sync = 1 if index % 100 < 50 else 0
        volts = 0.72 + (0.018 if sync else -0.018)
        samples.append(SimpleNamespace(volts=volts, sync=sync))
    return samples


class FakeLowLevelRig(app.EmitterEsp32Rig):
    """Exercise the real adapter while replacing only its serial primitives."""

    STREAM_CHUNK_SAMPLES = 500

    def __init__(self, sample_count: int, *, gap_count: int = 0):
        # Do not initialize or touch pyserial; the adapter methods under test use
        # the overridden primitives below.
        self._samples = stream_samples(sample_count)
        self._cursor = 0
        self._active = False
        self._diagnostics = None
        self.gap_count = gap_count
        self.connect_calls = 0
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def is_streaming(self):
        return self._active

    @property
    def stream_diagnostics(self):
        return self._diagnostics

    def connect(self):
        self.connect_calls += 1

    def start_stream(self, channel="sensor"):
        self.start_calls += 1
        self._active = True
        self._cursor = 0
        self._diagnostics = None
        return SimpleNamespace(sample_rate_hz=1000.0, channel=channel.upper())

    def read_stream(self, max_samples=None, *, timeout_s=1.0):
        del timeout_s
        if not self._active:
            raise AssertionError("read_stream called while stopped")
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


class FakeDecider:
    def __init__(self, *, decides: bool, cut_sample: int | None = None):
        self.decides = decides
        self.cut_sample = cut_sample
        self.update_calls = 0

    def update(self, waveform, sync, sample_rate):
        self.update_calls += 1
        self.last_lengths = (len(waveform), len(sync), sample_rate)
        return self.decides

    def fill_report(self, report):
        report.decided = self.decides
        report.cut_sample = self.cut_sample if self.decides else None
        report.stop_cycle = 15 if self.decides else None
        report.would_pass = True if self.decides else None


class IdentityAndSeparationTests(unittest.TestCase):
    def test_import_identity_constants_logo_and_results_are_v5_specific(self):
        self.assertEqual(Path(app.__file__).name, "eltec_406mca_esp32_tester.py")
        self.assertEqual(app.EMITTER_PWM_CHANNEL, "GPIO25")
        self.assertEqual(app.EMITTER_PWM_FREQUENCY_HZ, 10.0)
        self.assertEqual(app.EMITTER_PWM_DUTY_CYCLE, 50.0)
        self.assertEqual(app.WAVEFORM_INPUT_RANGE_V, 2.5)
        self.assertEqual(app.BATTERY_MIN_V, 5.8)
        self.assertEqual(app.BATTERY_WARN_V, 6.0)
        self.assertEqual(app.BATTERY_FAULT_MIN_V, 3.0)
        self.assertEqual(app.BATTERY_FAULT_MAX_V, 7.5)

        results = app.results_root_dir()
        self.assertEqual(results.name, "v5_esp32")
        self.assertNotIn("v4_emitter", str(results))
        batch_path = app.batch_results_path("LOT 42")
        self.assertEqual(batch_path.parent, results)
        self.assertEqual(batch_path.name, "406mca_esp32_lot_LOT_42.csv")

        logo = app.find_logo_path()
        self.assertIsNotNone(logo)
        self.assertTrue(logo.is_file())
        self.assertEqual(logo.name, "eltec_logo.png")

    def test_battery_boundaries_use_the_six_volt_sla_model(self):
        self.assertEqual(app.battery_state_for(None), "unknown")
        self.assertEqual(app.battery_state_for(2.99), "fault")
        self.assertEqual(app.battery_state_for(7.51), "fault")
        self.assertEqual(app.battery_state_for(5.8), "low")
        self.assertEqual(app.battery_state_for(5.9), "warn")
        self.assertEqual(app.battery_state_for(6.0), "warn")
        self.assertEqual(app.battery_state_for(6.1), "ok")


class AdapterFrameTests(unittest.TestCase):
    def test_frame_converts_samples_and_stops_stream(self):
        rig = FakeLowLevelRig(300)

        waveform, sync, rate = rig.read_waveform_frame(
            cycles=3,
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
        )

        self.assertEqual(rig.connect_calls, 1)
        self.assertEqual(rig.start_calls, 1)
        self.assertEqual(rig.stop_calls, 1)
        self.assertFalse(rig.is_streaming)
        self.assertEqual(waveform.shape, (300,))
        self.assertEqual(sync.shape, (300,))
        self.assertEqual(set(np.unique(sync)), {0.0, 1.0})
        self.assertAlmostEqual(waveform[0], 0.738)
        self.assertEqual(rate, 1000.0)

    def test_frame_strictly_rejects_a_timestamp_gap(self):
        rig = FakeLowLevelRig(300, gap_count=1)

        with self.assertRaisesRegex(app.Esp32BackendError, "timestamp gaps"):
            rig.read_waveform_frame(
                cycles=3,
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
            )

        self.assertEqual(rig.stop_calls, 1)
        self.assertFalse(rig.is_streaming)


class AdaptiveCaptureTests(unittest.TestCase):
    def run_capture(self, mode, decider):
        rig = FakeLowLevelRig(8000)
        progress = []
        with mock.patch.object(
            app.EmitterEsp32Rig, "_full_rule_stable", return_value=True
        ) as full_rule:
            waveform, sync, rate, report = rig.read_waveform_stream_decided(
                waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                mode=mode,
                decider=decider,
                progress=progress.append,
            )
        return rig, waveform, sync, rate, report, progress, full_rule

    def test_fast_stops_on_decision_and_trims_to_cut_sample(self):
        decider = FakeDecider(decides=True, cut_sample=1400)

        rig, waveform, sync, rate, report, progress, full_rule = self.run_capture(
            "fast", decider
        )

        self.assertEqual(decider.update_calls, 1)
        self.assertEqual(decider.last_lengths, (1500, 1500, 1000.0))
        self.assertEqual(len(waveform), 1400)
        self.assertEqual(len(sync), 1400)
        self.assertEqual(rate, 1000.0)
        self.assertTrue(report.decided)
        self.assertEqual(report.capture_cycles, 14)
        self.assertEqual(progress[-1], 15)
        full_rule.assert_not_called()
        self.assertEqual(rig.stop_calls, 1)

    def test_validation_logs_fast_decision_but_runs_full_rule(self):
        decider = FakeDecider(decides=True, cut_sample=1400)

        rig, waveform, _sync, _rate, report, progress, full_rule = self.run_capture(
            "validation", decider
        )

        self.assertGreater(decider.update_calls, 1)
        self.assertEqual(len(waveform), 4500)
        self.assertTrue(report.decided)
        self.assertEqual(report.capture_cycles, 45)
        self.assertEqual(progress[-1], 45)
        full_rule.assert_called_once()
        self.assertEqual(rig.stop_calls, 1)

    def test_full_mode_uses_stability_rule_without_fast_decider(self):
        rig, waveform, _sync, _rate, report, progress, full_rule = self.run_capture(
            "full", None
        )

        self.assertEqual(len(waveform), 4500)
        self.assertFalse(report.decided)
        self.assertEqual(report.capture_cycles, 45)
        self.assertEqual(progress[-1], 45)
        full_rule.assert_called_once()
        self.assertEqual(rig.stop_calls, 1)

    def test_fast_without_a_decision_falls_back_to_full_stability_rule(self):
        decider = FakeDecider(decides=False)

        rig, waveform, _sync, _rate, report, progress, full_rule = self.run_capture(
            "fast", decider
        )

        self.assertGreater(decider.update_calls, 1)
        self.assertEqual(len(waveform), 4500)
        self.assertFalse(report.decided)
        self.assertEqual(report.capture_cycles, 45)
        self.assertEqual(progress[-1], 45)
        full_rule.assert_called_once()
        self.assertEqual(rig.stop_calls, 1)


class FakeMeasurementDevice:
    def __init__(self, *, battery=6.2, offset=0.72, capture_error=None):
        self.battery = battery
        self.offset = offset
        self.capture_error = capture_error
        self.calls: list[str] = []

    def disable_emitter_pwm(self, channel):
        self.calls.append("pwm_off")

    def read_battery_voltage(self):
        self.calls.append("battery")
        return self.battery

    def read_offset_voltage(self, *, waveform_range_v):
        self.calls.append("offset")
        return self.offset

    def configure_emitter_pwm(self, **_kwargs):
        self.calls.append("pwm_on")

    def read_waveform_frame(self, cycles, waveform_range_v):
        del cycles, waveform_range_v
        self.calls.append("frame")
        return (
            np.asarray([0.70, 0.74], dtype=float),
            np.asarray([0.0, 1.0], dtype=float),
            1000.0,
        )

    def read_waveform_stream_decided(self, **kwargs):
        self.calls.append("capture")
        self.capture_kwargs = kwargs
        if self.capture_error is not None:
            raise self.capture_error
        waveform = np.asarray([0.70, 0.74] * 50, dtype=float)
        sync = np.asarray([0.0, 1.0] * 50, dtype=float)
        return waveform, sync, 1000.0, app.CaptureReport(mode=kwargs["mode"])


class MeasurementHarness:
    def __init__(self, device, mode=app.CAPTURE_MODE_FULL):
        self.device = device
        self.hardware_lock = threading.Lock()
        self.capture_mode_var = ValueVar(mode)
        self.measure_token = 7
        self.last_capture_report = None
        self.callback_events = []
        self.preview_count = 0
        self.ensure_count = 0

    def ensure_connected(self):
        self.ensure_count += 1

    def _fresh_battery_reading(self):
        return None

    def on_battery_update(self, value, error=None):
        self.callback_events.append(("battery", value, error))

    def on_offset_update(self, token, value):
        self.callback_events.append(("offset", token, value))

    def set_measure_status(self, token, text):
        self.callback_events.append(("status", token, text))

    def on_preview_frame(self, token, waveform, sync):
        self.preview_count += 1
        self.callback_events.append(("preview", token, len(waveform), len(sync)))


def dummy_result():
    metrics = app.WaveformMetrics(
        sensitivity_mv=36.0,
        sensitivity_amplified_mv=36.0,
        polarity=app.POSITIVE_POLARITY,
        measured_frequency_hz=10.0,
        cycles_used=20,
        signal_to_noise_ratio=10.0,
        waveform_v=np.asarray([0.70, 0.74]),
        sync_v=np.asarray([0.0, 1.0]),
    )
    final = app.FinalResult(
        passed=True,
        offset_v=0.72,
        sensitivity_mv=36.0,
        polarity=app.POSITIVE_POLARITY,
        fail_reasons=[],
        warnings=[],
        waveform_metrics=metrics,
    )
    return metrics, final


class BatchPersistenceTests(unittest.TestCase):
    def test_reopening_exact_batch_appends_without_changing_old_data(self):
        _metrics, final = dummy_result()
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "406mca_esp32_lot_BATCH_7.csv"
            common = {
                "batch_number": "BATCH 7",
                "tester_name": "Operator",
                "filter_setup": app.DEFAULT_FILTER_SETUP,
                "pwm_channel": app.EMITTER_PWM_CHANNEL,
                "pwm_hz": app.EMITTER_PWM_FREQUENCY_HZ,
                "pwm_duty": app.EMITTER_PWM_DUTY_CYCLE,
                "final_result": final,
                "comment": "",
                "snapshot_paths": [],
            }

            app.append_result_csv(
                csv_path,
                sensor_number=1,
                sensor_id="BATCH 7-1",
                **common,
            )
            original_bytes = csv_path.read_bytes()

            self.assertEqual(app.next_sensor_number_for_batch(csv_path), 2)
            app.append_result_csv(
                csv_path,
                sensor_number=2,
                sensor_id="BATCH 7-2",
                **common,
            )

            combined_bytes = csv_path.read_bytes()
            self.assertTrue(combined_bytes.startswith(original_bytes))
            with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))
            self.assertEqual([row["sensor_number"] for row in rows], ["1", "2"])
            self.assertEqual([row["sensor_id"] for row in rows], ["BATCH 7-1", "BATCH 7-2"])
            self.assertEqual(app.count_existing_batch_rows(csv_path), 2)
            self.assertEqual(app.next_sensor_number_for_batch(csv_path), 3)

    def test_snapshot_path_never_reuses_an_existing_filename(self):
        fixed_now = mock.Mock()
        fixed_now.strftime.return_value = "20260714_120000_123456"
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(app, "datetime") as fake_datetime:
            fake_datetime.now.return_value = fixed_now
            snapshot_dir = Path(temp_dir)
            first = app.unused_snapshot_path(snapshot_dir, "BATCH 7-1", "snapshot")
            first.touch()
            second = app.unused_snapshot_path(snapshot_dir, "BATCH 7-1", "snapshot")

            self.assertNotEqual(first, second)
            self.assertEqual(second.stem, first.stem + "_2")


class HardwareWorkflowTests(unittest.TestCase):
    def run_hardware(self, device, *, show_live):
        harness = MeasurementHarness(device)
        metrics, final = dummy_result()
        immediate = lambda callback: callback()
        with mock.patch.object(app.time, "sleep", return_value=None), mock.patch.object(
            app, "analyze_esp32_waveform", return_value=metrics
        ), mock.patch.object(app, "evaluate_result", return_value=final), mock.patch.object(
            app, "apply_signal_quality_gate", return_value=final
        ):
            result = app.EmitterTesterApp._hardware_measurement(
                harness,
                app.DEFAULT_FILTER_SETUP,
                app.WAVEFORM_INPUT_RANGE_V,
                app.EMITTER_PWM_CHANNEL,
                app.EMITTER_PWM_FREQUENCY_HZ,
                app.EMITTER_PWM_DUTY_CYCLE,
                show_live,
                harness.measure_token,
                immediate,
            )
        return harness, result

    def test_call_order_includes_digital_sync_preflight_and_six_live_frames(self):
        device = FakeMeasurementDevice()

        harness, (metrics, final, offset) = self.run_hardware(
            device, show_live=True
        )

        self.assertEqual(harness.ensure_count, 1)
        self.assertEqual(
            device.calls,
            ["pwm_off", "battery", "offset", "pwm_on"]
            + ["frame"] * (1 + app.PREVIEW_FRAMES)
            + ["capture", "pwm_off"],
        )
        self.assertEqual(harness.preview_count, app.PREVIEW_FRAMES)
        self.assertEqual(metrics.sensitivity_mv, 36.0)
        self.assertTrue(final.passed)
        self.assertEqual(offset, 0.72)
        self.assertEqual(harness.last_capture_report.data_source, "esp32")
        self.assertIsNone(device.capture_kwargs["decider"])

    def test_low_battery_blocks_before_offset_and_pwm_on(self):
        device = FakeMeasurementDevice(battery=5.7)
        harness = MeasurementHarness(device)

        with self.assertRaises(app.BatteryTooLowError):
            app.EmitterTesterApp._hardware_measurement(
                harness,
                app.DEFAULT_FILTER_SETUP,
                app.WAVEFORM_INPUT_RANGE_V,
                app.EMITTER_PWM_CHANNEL,
                10.0,
                50.0,
                False,
                harness.measure_token,
                lambda callback: callback(),
            )

        self.assertEqual(device.calls, ["pwm_off", "battery"])

    def test_implausible_offset_blocks_before_pwm_on(self):
        device = FakeMeasurementDevice(offset=3.0)
        harness = MeasurementHarness(device)

        with self.assertRaises(app.HardwareNotReadyError):
            app.EmitterTesterApp._hardware_measurement(
                harness,
                app.DEFAULT_FILTER_SETUP,
                app.WAVEFORM_INPUT_RANGE_V,
                app.EMITTER_PWM_CHANNEL,
                10.0,
                50.0,
                False,
                harness.measure_token,
                lambda callback: callback(),
            )

        self.assertEqual(device.calls, ["pwm_off", "battery", "offset"])

    def test_capture_error_still_turns_pwm_off(self):
        device = FakeMeasurementDevice(capture_error=RuntimeError("serial lost"))
        harness = MeasurementHarness(device)

        with mock.patch.object(app.time, "sleep", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "serial lost"):
                app.EmitterTesterApp._hardware_measurement(
                    harness,
                    app.DEFAULT_FILTER_SETUP,
                    app.WAVEFORM_INPUT_RANGE_V,
                    app.EMITTER_PWM_CHANNEL,
                    10.0,
                    50.0,
                    False,
                    harness.measure_token,
                    lambda callback: callback(),
                )

        self.assertEqual(
            device.calls,
            [
                "pwm_off",
                "battery",
                "offset",
                "pwm_on",
                "frame",
                "capture",
                "pwm_off",
            ],
        )


class SimulatorAndGuiSmokeTests(unittest.TestCase):
    def test_simulator_full_mode_marks_report_source(self):
        harness = MeasurementHarness(FakeMeasurementDevice())
        pushed = []

        def immediate(callback):
            pushed.append(callback)
            callback()

        np.random.seed(1)
        metrics, final, offset = app.EmitterTesterApp._simulate_measurement(
            harness,
            app.DEFAULT_FILTER_SETUP,
            "Known good",
            False,
            app.WAVEFORM_INPUT_RANGE_V,
            False,
            harness.measure_token,
            immediate,
        )

        self.assertGreater(metrics.waveform_v.size, 0)
        self.assertIsNotNone(final)
        self.assertGreater(offset, 0.0)
        self.assertGreater(len(pushed), 0)
        self.assertEqual(harness.last_capture_report.mode, "full")
        self.assertEqual(harness.last_capture_report.data_source, "simulator")

    @unittest.skipUnless(os.environ.get("DISPLAY"), "requires an X11 display")
    def test_gui_constructs_with_logo_then_withdraws_and_destroys(self):
        root = None
        with mock.patch.object(app.EmitterTesterApp, "startup_probe", lambda self: None):
            try:
                try:
                    root = app.EmitterTesterApp()
                except app.tk.TclError as exc:
                    self.skipTest(f"X11 display is unavailable: {exc}")
                root.withdraw()
                root.update_idletasks()
                self.assertEqual(root.title(), "Eltec 406MCA ESP32 Emitter Tester v5")
                self.assertIsNotNone(root.logo_image)
                self.assertEqual(root.status_var.get(), "Checking ESP32 rig...")
            finally:
                if root is not None:
                    root.destroy()


if __name__ == "__main__":
    unittest.main()
