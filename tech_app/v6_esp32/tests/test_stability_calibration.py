from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tech_app.v6_esp32 import esp32_backend as backend
from tech_app.v6_esp32 import stability_calibration as calibration
from tech_app.v6_esp32.stability_analysis import StabilitySettings


def stable_stream(sample_count: int = 802) -> list[backend.StreamSample]:
    """Return 10 Hz square-wave samples with rising edges at 1, 101, ..."""

    samples: list[backend.StreamSample] = []
    for index in range(sample_count):
        phase = index % 100
        sync = 1 if 1 <= phase <= 50 else 0
        volts = 0.700 if sync else 0.680
        samples.append(
            backend.StreamSample(
                timestamp_us=index * 1000,
                raw=1_000_000 + index,
                volts=volts,
                sync=sync,
            )
        )
    return samples


class FakeRig:
    def __init__(
        self,
        *,
        samples: list[backend.StreamSample] | None = None,
        battery_v: float = 6.2,
        offset_v: float = 0.7,
        gap_count: int = 0,
        read_error: BaseException | None = None,
        port: str | None = None,
    ) -> None:
        self.samples = list(samples or stable_stream())
        self.battery_v = battery_v
        self.offset_v = offset_v
        self.gap_count = gap_count
        self.read_error = read_error
        self.requested_port = port
        self.port_name: str | None = None
        self.identity = None
        self.events: list[str] = []
        self.delivered: list[backend.StreamSample] = []
        self._active = False
        self._diagnostics: backend.StreamDiagnostics | None = None
        self._read_failed = False

    @property
    def is_streaming(self) -> bool:
        return self._active

    @property
    def stream_diagnostics(self) -> backend.StreamDiagnostics | None:
        return self._diagnostics

    @property
    def drained_samples(self) -> tuple[backend.StreamSample, ...]:
        return ()

    def connect(self) -> None:
        self.events.append("connect")
        self.port_name = self.requested_port or "/dev/ttyUSB-cal"
        self.identity = backend.FirmwareIdentity(
            "ELTEC-ESP32-ADS1256,v1.7.2", (1, 7, 2), True
        )

    def read_battery_voltage(self) -> float:
        self.events.append("battery")
        return self.battery_v

    def read_offset_voltage(self) -> float:
        self.events.append("offset")
        return self.offset_v

    def enable_emitter_pwm(self, _channel: str) -> None:
        self.events.append("pwm_on")

    def disable_emitter_pwm(self, _channel: str) -> None:
        self.events.append("pwm_off")

    def start_stream(self, _channel: str) -> backend.StreamHeader:
        self.events.append("stream_start")
        self._active = True
        return backend.StreamHeader(1000.0, "SENSOR")

    def read_stream(self, max_samples: int, *, timeout_s: float):
        del timeout_s
        self.events.append("stream_read")
        if self.read_error is not None and not self._read_failed:
            self._read_failed = True
            raise self.read_error
        start = len(self.delivered)
        chunk = self.samples[start : start + max_samples]
        self.delivered.extend(chunk)
        return chunk

    def stop_stream(self, *, timeout_s: float) -> backend.StreamDiagnostics:
        del timeout_s
        self.events.append("stream_stop")
        self._active = False
        count = len(self.delivered)
        diagnostics = backend.StreamDiagnostics(
            expected_rate_hz=1000.0,
            channel="SENSOR",
            started_monotonic=1.0,
            stopped_monotonic=2.0,
            received_samples=count,
            timestamp_gap_count=self.gap_count,
            estimated_missing_samples=self.gap_count,
            first_timestamp_us=(self.delivered[0].timestamp_us if count else None),
            last_timestamp_us=(self.delivered[-1].timestamp_us if count else None),
            firmware_samples_sent=count,
            firmware_adc_overruns=0,
            stop_marker_seen=True,
        )
        if count > 1:
            diagnostics._elapsed_device_us = (
                self.delivered[-1].timestamp_us - self.delivered[0].timestamp_us
            )
            diagnostics._valid_intervals_us = [1000] * (count - 1)
        self._diagnostics = diagnostics
        return diagnostics

    def close(self, *, disable_pwm: bool = True) -> None:
        self.events.append(f"close:{disable_pwm}")


class Factory:
    def __init__(self, rig: FakeRig) -> None:
        self.rig = rig
        self.ports: list[str | None] = []

    def __call__(self, *, port: str | None = None) -> FakeRig:
        self.ports.append(port)
        self.rig.requested_port = port
        return self.rig


class CaptureTests(unittest.TestCase):
    def test_capture_writes_raw_cycles_and_summary_from_one_stream(self):
        rig = FakeRig()
        clock_values = iter((10.0, 10.003, 10.804))
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "runs"
            settings_path = Path(tmp) / "settings.json"
            settings_text = json.dumps(
                {
                    "peak_delta_threshold_mv": 0.100,
                    "consecutive_deltas_required": 5,
                }
            )
            settings_path.write_text(settings_text, encoding="utf-8")
            artifacts = calibration.capture_calibration(
                calibration.CaptureConfig(
                    sensor_id="GOOD / 001",
                    port="/dev/ttyUSB9",
                    duration_s=0.801,
                    output_dir=output_dir,
                    settings_path=settings_path,
                ),
                rig_factory=Factory(rig),
                monotonic=lambda: next(clock_values),
                now_utc=lambda: datetime(
                    2026, 7, 14, 12, 30, 45, 123456, tzinfo=timezone.utc
                ),
            )

            self.assertEqual(
                artifacts.raw_csv.name,
                "GOOD_001_20260714T123045123456Z_raw.csv",
            )
            self.assertTrue(artifacts.cycle_csv.name.endswith("_cycles.csv"))
            self.assertTrue(artifacts.summary_json.name.endswith("_summary.json"))
            self.assertEqual(settings_path.read_text(encoding="utf-8"), settings_text)

            with artifacts.raw_csv.open(newline="", encoding="utf-8") as handle:
                raw_rows = list(csv.DictReader(handle))
            with artifacts.cycle_csv.open(newline="", encoding="utf-8") as handle:
                cycle_rows = list(csv.DictReader(handle))
            disk_summary = json.loads(artifacts.summary_json.read_text(encoding="utf-8"))

            self.assertEqual(len(raw_rows), 802)
            self.assertEqual(raw_rows[0]["timestamp_us"], "0")
            self.assertEqual(raw_rows[-1]["elapsed_s"], "0.801000000")
            self.assertEqual(raw_rows[0]["pwm_elapsed_s"], "0.003000000")
            self.assertEqual(raw_rows[-1]["pwm_elapsed_s"], "0.804000000")
            self.assertEqual(len(cycle_rows), 8)
            self.assertEqual(cycle_rows[0]["absolute_peak_delta_mv"], "")
            self.assertEqual(cycle_rows[5]["confirmation_run_length"], "5")
            self.assertAlmostEqual(
                float(cycle_rows[5]["peak_to_peak_v"]), 0.020
            )
            self.assertEqual(
                disk_summary["earliest_qualifying_stabilization"]["cycle_number"],
                6,
            )
            self.assertAlmostEqual(
                disk_summary["capture"]["pwm_to_stream_offset_s"], 0.003
            )
            self.assertEqual(disk_summary["hardware"]["port"], "/dev/ttyUSB9")
            self.assertAlmostEqual(
                disk_summary["capture"]["sync_validation"]["measured_frequency_hz"],
                10.0,
            )
            self.assertEqual(
                disk_summary["capture"]["sync_validation"]["cycles_validated"],
                3,
            )
            self.assertEqual(
                disk_summary["hardware"]["firmware_identity"],
                "ELTEC-ESP32-ADS1256,v1.7.2",
            )
            self.assertTrue(disk_summary["calibration_only"])
            self.assertNotIn("verdict", json.dumps(disk_summary).lower())

        self.assertEqual(rig.events[:4], ["connect", "pwm_off", "battery", "offset"])
        self.assertLess(rig.events.index("pwm_on"), rig.events.index("stream_start"))
        self.assertLess(
            rig.events.index("stream_stop"),
            len(rig.events) - 1 - rig.events[::-1].index("pwm_off"),
        )
        self.assertEqual(rig.events[-1], "close:True")

    def test_preflight_blocks_low_battery_before_pwm_and_still_requests_off(self):
        rig = FakeRig(battery_v=5.8)
        with tempfile.TemporaryDirectory() as tmp:
            config = calibration.CaptureConfig(
                sensor_id="GOOD-LOW",
                output_dir=Path(tmp) / "out",
            )
            with self.assertRaisesRegex(calibration.PreflightError, "too low"):
                calibration.capture_calibration(config, rig_factory=Factory(rig))

            self.assertNotIn("pwm_on", rig.events)
            self.assertIn("pwm_off", rig.events)
            self.assertEqual(rig.events[-1], "close:True")

    def test_stream_exception_stops_stream_and_always_disables_pwm(self):
        rig = FakeRig(read_error=RuntimeError("serial unplugged"))
        with tempfile.TemporaryDirectory() as tmp:
            config = calibration.CaptureConfig(
                sensor_id="GOOD-ERR",
                duration_s=0.2,
                output_dir=Path(tmp) / "out",
            )
            with self.assertRaisesRegex(RuntimeError, "serial unplugged"):
                calibration.capture_calibration(config, rig_factory=Factory(rig))

            self.assertIn("stream_stop", rig.events)
            self.assertIn("pwm_off", rig.events)
            self.assertEqual(rig.events[-1], "close:True")

    def test_bad_stream_diagnostics_are_not_written_as_calibration_evidence(self):
        rig = FakeRig(gap_count=1)
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            config = calibration.CaptureConfig(
                sensor_id="GOOD-GAP",
                duration_s=0.2,
                output_dir=output_dir,
            )
            with self.assertRaisesRegex(
                calibration.StreamIntegrityError, "timestamp gaps"
            ):
                calibration.capture_calibration(config, rig_factory=Factory(rig))

            self.assertFalse(output_dir.exists())
            self.assertIn("pwm_off", rig.events)

    def test_wrong_sync_frequency_is_not_written_as_calibration_evidence(self):
        samples = []
        for index, original in enumerate(stable_stream()):
            samples.append(
                backend.StreamSample(
                    timestamp_us=original.timestamp_us,
                    raw=original.raw,
                    volts=original.volts,
                    sync=1 if 1 <= (index % 200) <= 100 else 0,
                )
            )
        rig = FakeRig(samples=samples)
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            config = calibration.CaptureConfig(
                sensor_id="GOOD-WRONG-SYNC",
                duration_s=0.801,
                output_dir=output_dir,
            )
            with self.assertRaisesRegex(
                calibration.StreamIntegrityError,
                "frequency is 5.000 Hz",
            ):
                calibration.capture_calibration(config, rig_factory=Factory(rig))

            self.assertFalse(output_dir.exists())
            self.assertGreaterEqual(rig.events.count("pwm_off"), 2)


def write_cycle_csv(path: Path, run_id: str, sensor_id: str, deltas: list[float | None]):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=calibration.CYCLE_CSV_FIELDS)
        writer.writeheader()
        for cycle_number, delta in enumerate(deltas, start=1):
            writer.writerow(
                {
                    "run_id": run_id,
                    "sensor_id": sensor_id,
                    "cycle_number": cycle_number,
                    "end_elapsed_s": cycle_number / 10.0,
                    "absolute_peak_delta_mv": "" if delta is None else delta,
                }
            )


class SummarizeTests(unittest.TestCase):
    def test_summarize_recomputes_rule_and_combined_percentiles(self):
        settings = StabilitySettings(0.100, 5)
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first_cycles.csv"
            second = Path(tmp) / "second_cycles.csv"
            write_cycle_csv(
                first,
                "run-1",
                "GOOD-1",
                [None, 0.1, 0.1, 0.2, 0.1, 0.1, 0.1, 0.1, 0.1],
            )
            write_cycle_csv(
                second,
                "run-2",
                "GOOD-2",
                [None, 0.05, 0.06, 0.07, 0.08, 0.09],
            )

            summary = calibration.summarize_cycle_csvs([first, second], settings)

        self.assertEqual(summary["input_file_count"], 2)
        self.assertEqual(summary["part_run_count"], 2)
        self.assertEqual(summary["combined_delta_count"], 13)
        percentiles = summary["combined_absolute_peak_delta_mv"]
        self.assertAlmostEqual(percentiles["p50"], 0.1)
        self.assertAlmostEqual(percentiles["p90"], 0.1)
        self.assertAlmostEqual(percentiles["p95"], 0.14)
        self.assertAlmostEqual(percentiles["p99"], 0.188)
        self.assertAlmostEqual(percentiles["max"], 0.2)
        self.assertEqual(summary["combined_post_stabilization_delta_count"], 2)
        self.assertEqual(summary["post_stabilization_contributing_run_count"], 2)
        steady_state = summary[
            "combined_post_stabilization_absolute_peak_delta_mv"
        ]
        self.assertAlmostEqual(steady_state["p50"], 0.095)
        self.assertAlmostEqual(steady_state["p90"], 0.099)
        self.assertAlmostEqual(steady_state["p95"], 0.0995)
        self.assertAlmostEqual(steady_state["p99"], 0.0999)
        self.assertAlmostEqual(steady_state["max"], 0.1)
        self.assertIn(
            "stabilization-confirming cycle",
            summary["post_stabilization_definition"],
        )
        self.assertEqual(
            summary["parts"][0]["post_stabilization_delta_count"], 1
        )
        self.assertEqual(
            summary["parts"][1]["post_stabilization_delta_count"], 1
        )
        self.assertEqual(
            summary["parts"][0]["earliest_qualifying_stabilization"],
            {"cycle_number": 9, "elapsed_s": 0.9},
        )
        self.assertEqual(
            summary["parts"][1]["earliest_qualifying_stabilization"],
            {"cycle_number": 6, "elapsed_s": 0.6},
        )

    def test_post_stabilization_region_includes_endpoint_and_later_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            cycles = Path(tmp) / "steady_state_cycles.csv"
            write_cycle_csv(
                cycles,
                "run-steady",
                "GOOD-STEADY",
                [None, 0.01, 0.02, 0.03, 0.04, 0.05, 0.20, 0.06],
            )

            summary = calibration.summarize_cycle_csvs(
                [cycles], StabilitySettings(0.1, 5)
            )

        # Cycle 6 is the confirming endpoint. Cycles 7 and 8 remain in the
        # region so a later excursion is visible instead of being filtered out.
        self.assertEqual(summary["combined_post_stabilization_delta_count"], 3)
        steady_state = summary[
            "combined_post_stabilization_absolute_peak_delta_mv"
        ]
        self.assertAlmostEqual(steady_state["p50"], 0.06)
        self.assertAlmostEqual(steady_state["p90"], 0.172)
        self.assertAlmostEqual(steady_state["p95"], 0.186)
        self.assertAlmostEqual(steady_state["p99"], 0.1972)
        self.assertAlmostEqual(steady_state["max"], 0.20)

    def test_no_stabilized_run_reports_empty_steady_state_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            cycles = Path(tmp) / "never_stable_cycles.csv"
            write_cycle_csv(
                cycles,
                "run-unstable",
                "GOOD-CANDIDATE",
                [None, 0.2, 0.3, 0.2],
            )

            summary = calibration.summarize_cycle_csvs(
                [cycles], StabilitySettings(0.1, 5)
            )

        self.assertEqual(summary["combined_delta_count"], 3)
        self.assertEqual(summary["combined_post_stabilization_delta_count"], 0)
        self.assertEqual(summary["post_stabilization_contributing_run_count"], 0)
        self.assertEqual(
            summary["combined_post_stabilization_absolute_peak_delta_mv"],
            {"p50": None, "p90": None, "p95": None, "p99": None, "max": None},
        )

    def test_summarize_cli_prints_json_and_does_not_modify_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cycles = Path(tmp) / "known_good_cycles.csv"
            settings_path = Path(tmp) / "candidate.json"
            write_cycle_csv(
                cycles,
                "run-cli",
                "GOOD-CLI",
                [None, 0.01, 0.02, 0.03, 0.04, 0.05],
            )
            original = json.dumps(
                {
                    "peak_delta_threshold_mv": 0.1,
                    "consecutive_deltas_required": 5,
                }
            )
            settings_path.write_text(original, encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = calibration.main(
                ["summarize", str(cycles), "--settings", str(settings_path)],
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            parsed = json.loads(stdout.getvalue())
            self.assertEqual(parsed["parts"][0]["sensor_id"], "GOOD-CLI")
            self.assertEqual(settings_path.read_text(encoding="utf-8"), original)
            self.assertEqual(stderr.getvalue(), "")

    def test_invalid_cycle_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalid = Path(tmp) / "invalid.csv"
            invalid.write_text("sensor_id,delta\nGOOD,0.1\n", encoding="utf-8")
            with self.assertRaisesRegex(
                calibration.CalibrationDataError, "missing required columns"
            ):
                calibration.summarize_cycle_csvs(
                    [invalid], StabilitySettings(0.1, 5)
                )


class ParserTests(unittest.TestCase):
    def test_capture_parser_exposes_requested_interface_and_defaults(self):
        args = calibration.build_parser().parse_args(
            ["capture", "--sensor-id", "GOOD-123"]
        )
        self.assertEqual(args.sensor_id, "GOOD-123")
        self.assertIsNone(args.port)
        self.assertEqual(args.duration, 20.0)
        self.assertEqual(args.output_dir, calibration.DEFAULT_OUTPUT_DIR)
        self.assertEqual(args.settings, calibration.DEFAULT_SETTINGS_PATH)


if __name__ == "__main__":
    unittest.main()
