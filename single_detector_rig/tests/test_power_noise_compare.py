"""Dark-noise calculations and acquisition failures, without bench hardware."""
import csv
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

import numpy as np

PATH = Path(__file__).resolve().parents[2] / "engineer_tools/power_noise/power_noise_compare.py"
spec = importlib.util.spec_from_file_location("power_noise_compare", PATH)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class FakeRig:
    def __init__(self, port=None, fault=None, model="406MCA"):
        self.port_name = port or "COM_TEST"
        self.identity = SimpleNamespace(text="ELTEC-ESP32-ADS1256,v3.4", version=(3, 4, 0))
        self.commands = []
        self.is_streaming = False
        self.stream_diagnostics = None
        self.drained_samples = []
        self.count = 0
        self.fault = fault
        self.model = model
        self.closed = False
        self.battery_reads = 0
        self.full_scale = 5.0 if model == "405M22" else 2.5

    def connect(self):
        pass

    def disable_emitter_pwm(self):
        self.commands.append("PWM,OFF")

    def _command(self, command, prefix):
        self.commands.append(command)
        if command == "STATUS?":
            return "STATUS,pwm=0,streaming=0,vref=2.500,rate=1000"
        profile = "gain=1,buf=0" if self.model == "405M22" else "gain=2,buf=1"
        if self.fault == "profile":
            profile = "gain=9,buf=9"
        return prefix + profile + (f",fs={self.full_scale:.3f}" if command == "FE?" else "")

    def read_battery_voltage(self):
        assert not self.is_streaming, "AIN7 was read during AIN0 streaming"
        self.commands.append("BAT?")
        self.battery_reads += 1
        if self.fault == "voltage":
            raise RuntimeError("BAT? timeout")
        rail_voltage = 6.3 if self.battery_reads == 1 else 6.299
        return rail_voltage / app.SUPPLY_DIVIDER_RATIO * app.FIRMWARE_DIVIDER_RATIO

    def start_stream(self, channel):
        self.commands.append("STREAM,START")
        self.is_streaming = True
        self.stream_diagnostics = app.load_backend(self.model).StreamDiagnostics(1000, "SENSOR", 0)
        return SimpleNamespace(sample_rate_hz=1000, channel="SENSOR")

    def read_stream(self, max_samples, timeout_s):
        if self.fault == "stall":
            self.is_streaming = False
            return []
        samples = []
        for _ in range(max_samples):
            value = 1.2 + 0.001 * np.sin(2 * np.pi * self.count / 100)
            if self.fault == "flat":
                value = 1.2
            raw = int(round(value / self.full_scale * app.ADC_POSITIVE_FULL_SCALE))
            if self.fault == "clip":
                raw = app.ADC_POSITIVE_FULL_SCALE
            if self.fault == "tiny":
                raw = 4_026_531 + self.count % 2
            reported = float(f"{raw * self.full_scale / app.ADC_POSITIVE_FULL_SCALE:.6f}")
            if self.fault == "scale":
                reported *= 2
            samples.append(SimpleNamespace(timestamp_us=self.count * 1000,
                                          raw=raw, volts=reported, sync=int(self.fault == "gate")))
            self.count += 1
        return samples

    def stop_stream(self):
        self.commands.append("STREAM,STOP")
        self.is_streaming = False
        d = self.stream_diagnostics
        d.received_samples = self.count
        d.firmware_samples_sent = self.count + int(self.fault == "count")
        d.stop_marker_seen = self.fault != "stop"
        d._valid_intervals_us = [1000]
        d.firmware_adc_overruns = int(self.fault == "overrun")
        d.timestamp_gap_count = int(self.fault == "gap")
        d.driver_rx_overflow_events = int(self.fault == "overflow")
        d.ignored_lines = int(self.fault == "reset")
        return d

    def close(self):
        self.closed = True


class PowerNoiseTests(unittest.TestCase):
    def test_known_sine_rms_excludes_dc(self):
        x = 2.7 + 0.003 * np.sin(2 * np.pi * np.arange(10000) / 100)
        result = app.metrics(x)
        self.assertAlmostEqual(result["rms_mv"], 3 / np.sqrt(2), places=10)
        self.assertAlmostEqual(result["peak_to_peak_mv"], 6, places=10)
        self.assertAlmostEqual(result["mean_v"], 2.7)

    def test_spike_and_drift_are_retained(self):
        x = np.linspace(1, 1.01, 10000)
        x[500] += 0.02
        result = app.metrics(x)
        self.assertGreater(result["peak_to_peak_mv"], 20)
        self.assertGreater(result["rms_mv"], 2.8)
        self.assertGreater(result["start_end_shift_mv"], 4.9)

    def test_invalid_data_has_no_quiet_verdict(self):
        for values in ([1], [1, 1], [1, float("nan")], [1, float("inf")]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                app.metrics(values)

    def test_direction_percent_and_conflicting_metrics(self):
        result = app.comparison({"rms_mv": 2, "peak_to_peak_mv": 8},
                                {"rms_mv": 1, "peak_to_peak_mv": 9})
        self.assertEqual(result["rms_mv"]["supply_change_percent"], -50)
        self.assertEqual(result["peak_to_peak_mv"]["supply_change_percent"], 12.5)
        self.assertIn("different ranking", result["headline"])

    def test_equal_and_zero_denominator(self):
        result = app.comparison({"rms_mv": 0, "peak_to_peak_mv": 0},
                                {"rms_mv": 0, "peak_to_peak_mv": 0})
        self.assertEqual(result["rms_mv"]["lower"], "equal")
        self.assertIsNone(result["rms_mv"]["supply_change_percent"])

    def test_both_profiles_and_legacy(self):
        for model, command in (("405M22", "FE,V20"), ("406MCA", "FE,V19")):
            rig = FakeRig(model=model)
            app.configure_dark(rig, model)
            self.assertEqual(rig.commands, ["PWM,OFF", command, "FE?"])
        for model, version in (("405M22", (2, 0, 0)), ("406MCA", (1, 9, 0))):
            rig = FakeRig(model=model)
            rig.identity.version = version
            app.configure_dark(rig, model)
            self.assertEqual(rig.commands, ["PWM,OFF"])
        rig = FakeRig()
        rig.identity.version = (2, 0, 0)
        with self.assertRaises(RuntimeError):
            app.configure_dark(rig, "406MCA")

    def test_capture_exact_length_excludes_settle_and_closes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, state = app.new_session(temp, "405M22", "part01", 10, 1)
            rig = FakeRig(model="405M22")
            data = app.record(folder, state, "battery", None, threading.Event(), lambda _: None,
                              rig_factory=lambda **_: rig)
            self.assertEqual(data["samples"], 10000)
            self.assertEqual(rig.count, 11000)
            self.assertAlmostEqual(data["rms_mv"], 1 / np.sqrt(2), delta=0.0001)
            self.assertTrue(rig.closed)
            self.assertFalse(any("ON" in cmd for cmd in rig.commands))
            with (folder / data["raw_csv"]).open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(sum(row["phase"] == "settling" for row in rows), 1000)
            self.assertEqual(sum(row["phase"] == "measurement" for row in rows), 10000)
            # Independently reproduce both measurements from saved integer codes.
            counts = [int(row["raw_code"]) for row in rows if row["phase"] == "measurement"]
            mean_count = sum(counts) / len(counts)
            reference_rms = (sum((x - mean_count) ** 2 for x in counts) / len(counts)) ** 0.5
            reference_pp = max(counts) - min(counts)
            self.assertAlmostEqual(data["rms_mv"], reference_rms * 5 / 8388607 * 1000, places=10)
            self.assertAlmostEqual(data["peak_to_peak_mv"], reference_pp * 5 / 8388607 * 1000, places=10)

    def test_rejected_captures_preserve_evidence_and_allow_retry(self):
        for fault in ("profile", "flat", "clip", "gate", "count", "stop", "overrun", "gap", "overflow", "reset", "stall", "scale"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                folder, state = app.new_session(temp, "406MCA", "test", 10, 0)
                rig = FakeRig(fault=fault)
                with self.assertRaises((RuntimeError, ValueError)):
                    app.record(folder, state, "battery", None, threading.Event(), lambda _: None,
                               rig_factory=lambda **_: rig)
                self.assertEqual(state["captures"], {})
                self.assertEqual(state["attempts"][0]["status"], "rejected")
                self.assertTrue(rig.closed)
                self.assertEqual(len(list(folder.glob("battery_*.json"))), 1)

    def test_cancel_excludes_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, state = app.new_session(temp, "406MCA", "test", 10, 0)
            cancelled = threading.Event()
            cancelled.set()
            rig = FakeRig()
            with self.assertRaisesRegex(RuntimeError, "Stopped"):
                app.record(folder, state, "battery", None, cancelled, lambda _: None,
                           rig_factory=lambda **_: rig)
            self.assertEqual(state["captures"], {})
            self.assertTrue(rig.closed)

    def test_report_round_trip_and_html_escaping(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, state = app.new_session(temp, "406MCA", "<part&1>", 10, 0)
            for source in app.SOURCES:
                rig = FakeRig()
                app.record(folder, state, source, None, threading.Event(), lambda _: None,
                           rig_factory=lambda **_: rig)
            app.save_report(folder, state)
            report = (folder / "report.html").read_text(encoding="utf-8")
            self.assertIn("&lt;part&amp;1&gt;", report)
            self.assertIn("not power-source noise alone", report)
            self.assertTrue((folder / "summary.csv").exists())
            state["captures"]["supply"]["front_end"] = "changed"
            with self.assertRaisesRegex(ValueError, "front_end"):
                app.save_report(folder, state)

    def test_simulation_labelled_and_unique(self):
        with tempfile.TemporaryDirectory() as temp:
            first = app.simulate(temp, "405M22", 10, 0)
            second = app.simulate(temp, "405M22", 10, 0)
            self.assertNotEqual(first, second)
            self.assertIn("DEMONSTRATION", (first / "report.html").read_text(encoding="utf-8"))

    def test_no_production_result_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                app.new_session(Path(temp) / "Eltec_406MCA_Test_Results", "406MCA", "test", 10, 0)

    def test_rms_hand_calculation_uses_n_not_n_minus_one(self):
        # [-3, -1, +1, +3] mV about a 2 V offset: mean square = 5 mV^2.
        result = app.metrics([1.997, 1.999, 2.001, 2.003])
        self.assertAlmostEqual(result["rms_mv"], 5 ** 0.5)
        self.assertAlmostEqual(result["peak_to_peak_mv"], 6)
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                app.metrics([1, 2], invalid)

    def test_full_counts_retain_noise_hidden_by_decimal_rounding(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, state = app.new_session(temp, "406MCA", "quiet", 10, 0)
            rig = FakeRig(fault="tiny")
            result = app.record(folder, state, "battery", None, threading.Event(), lambda _: None,
                                rig_factory=lambda **_: rig)
            lsb_mv = 2.5 / 8388607 * 1000
            self.assertAlmostEqual(result["rms_mv"], lsb_mv / 2, places=10)
            self.assertAlmostEqual(result["peak_to_peak_mv"], lsb_mv, places=10)
            with (folder / result["raw_csv"]).open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len({row["firmware_volts"] for row in rows}), 1)
            self.assertEqual(len({row["volts"] for row in rows}), 2)

    def test_voltage_corrects_divider_once_before_and_after(self):
        for model in app.MODELS:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as temp:
                folder, state = app.new_session(temp, model, "test", 10, 0)
                rig = FakeRig(model=model)
                result = app.record(folder, state, "battery", None, threading.Event(), lambda _: None,
                                    rig_factory=lambda **_: rig)
                self.assertAlmostEqual(result["supply_voltage_before"]["volts"], 6.3)
                self.assertAlmostEqual(result["supply_voltage_after"]["volts"], 6.299)
                self.assertEqual(result["supply_voltage_before"]["status"], "estimated" if model == "406MCA" else "indicative")
                self.assertEqual(result["supply_voltage_before"]["physical_divider_ratio"], 4)
                queries = [i for i, command in enumerate(rig.commands) if command == "BAT?"]
                self.assertEqual(len(queries), 2)
                self.assertLess(queries[0], rig.commands.index("STREAM,START"))
                self.assertGreater(queries[1], rig.commands.index("STREAM,STOP"))
                saved = json.loads((folder / "session.json").read_text())
                self.assertAlmostEqual(saved["captures"]["battery"]["supply_voltage_before"]["volts"], 6.3)

    def test_voltage_query_failure_keeps_valid_noise(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, state = app.new_session(temp, "406MCA", "test", 10, 0)
            rig = FakeRig(fault="voltage")
            result = app.record(folder, state, "battery", None, threading.Event(), lambda _: None,
                                rig_factory=lambda **_: rig)
            self.assertEqual(result["status"], "accepted")
            self.assertIsNone(result["supply_voltage_after"]["volts"])
            self.assertIn("unavailable", app.supply_text(result))
            self.assertGreater(result["rms_mv"], 0)

    def test_adc_reference_and_gain_scaling_legacy_and_modern(self):
        for model, full_scale, legacy in (("405M22", 5, (2, 0, 0)), ("406MCA", 2.5, (1, 9, 0))):
            for version in ((3, 4, 0), legacy):
                rig = FakeRig(model=model)
                rig.identity.version = version
                front_end = app.configure_dark(rig, model)
                scale = app.read_adc_scale(rig, model, front_end)
                self.assertEqual(scale["adc_full_scale_v"], full_scale)
                self.assertAlmostEqual(scale["volts_per_count"], full_scale / 8388607)
        rig = FakeRig()
        with self.assertRaisesRegex(RuntimeError, "disagree"):
            app.read_adc_scale(rig, "406MCA", "FE,gain=2,buf=1,fs=5.000")

    def test_voltage_never_used_to_normalize_noise(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, state = app.new_session(temp, "406MCA", "test", 10, 0)
            for source, voltage in (("battery", 6.3), ("supply", 5.05)):
                rig = FakeRig()
                rig.read_battery_voltage = lambda value=voltage: value / 4 * app.FIRMWARE_DIVIDER_RATIO
                app.record(folder, state, source, None, threading.Event(), lambda _: None,
                           rig_factory=lambda **_: rig)
            result = app.save_report(folder, state)
            self.assertEqual(result["rms_mv"]["supply_change_percent"], 0)
            with (folder / "summary.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertAlmostEqual(float(rows[0]["supply_voltage_before_v"]), 6.3)
            self.assertAlmostEqual(float(rows[1]["supply_voltage_before_v"]), 5.05)
            self.assertEqual(rows[0]["supply_voltage_before_status"], "estimated")

    def test_reported_values_corrected_once_and_fault_range_rejected(self):
        rig = FakeRig()
        for reported in (3.3013, 7.4333):
            rig.read_battery_voltage = lambda value=reported: value
            snapshot = app.supply_snapshot(rig)
            expected = reported / ((99700 + 99600) / 99600) * 4
            self.assertEqual(snapshot["firmware_reported_v"], reported)
            self.assertAlmostEqual(snapshot["corrected_estimate_v"], expected)
            data = {"supply_voltage_before": snapshot, "supply_voltage_after": snapshot}
            if reported > 7:
                self.assertEqual(snapshot["status"], "out_of_range")
                self.assertIsNone(snapshot["volts"])
                self.assertIn("out of range", app.supply_text(data))
            else:
                self.assertEqual(snapshot["status"], "estimated")
                self.assertAlmostEqual(snapshot["volts"], 6.5992871048670345)
                self.assertIn("4:1 estimate", app.supply_text(data))
        legacy = {"volts": 3.3013, "status": "app_reading"}
        self.assertIn("old scale; unverified", app.supply_text({"supply_voltage_before": legacy}))

    def test_known_4_to_1_divider_recovers_meter_values(self):
        rig = FakeRig()
        for actual in (5.05, 6.69):
            rig.read_battery_voltage = lambda value=actual: value / 4 * app.FIRMWARE_DIVIDER_RATIO
            snapshot = app.supply_snapshot(rig)
            self.assertAlmostEqual(snapshot["volts"], actual)
            self.assertAlmostEqual(snapshot["adc_pin_indication_v"], actual / 4)

    def test_out_of_range_stops_before_noise_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, state = app.new_session(temp, "406MCA", "test", 10, 0)
            rig = FakeRig()
            rig.read_battery_voltage = lambda: 7.4333
            with self.assertRaisesRegex(RuntimeError, "outside its measurement range"):
                app.record(folder, state, "supply", None, threading.Event(), lambda _: None,
                           rig_factory=lambda **_: rig)
            self.assertNotIn("STREAM,START", rig.commands)
            self.assertEqual(state["captures"], {})
            self.assertTrue(rig.closed)
            self.assertAlmostEqual(state["attempts"][0]["supply_voltage_before"]["corrected_estimate_v"], 14.859140592072253)

    def test_out_of_range_final_read_rejects_capture_but_keeps_raw(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, state = app.new_session(temp, "406MCA", "test", 10, 0)
            rig = FakeRig()
            readings = iter([3.3013, 7.4333])
            rig.read_battery_voltage = lambda: next(readings)
            with self.assertRaisesRegex(RuntimeError, "outside its measurement range"):
                app.record(folder, state, "supply", None, threading.Event(), lambda _: None,
                           rig_factory=lambda **_: rig)
            self.assertEqual(state["captures"], {})
            self.assertTrue(list(folder.glob("supply_*.csv")))

    def test_voltage_invalid_and_streaming_guards(self):
        rig = FakeRig()
        for value in (float("nan"), float("inf"), -1, 0):
            rig.read_battery_voltage = lambda value=value: value
            result = app.supply_snapshot(rig)
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["volts"])
        rig.is_streaming = True
        with self.assertRaisesRegex(RuntimeError, "outside"):
            app.supply_snapshot(rig)


if __name__ == "__main__":
    unittest.main()
