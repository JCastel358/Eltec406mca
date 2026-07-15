from __future__ import annotations

import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

from tech_app.v6_esp32 import esp32_backend as backend


class ScriptedSerial:
    """Small pyserial stand-in that releases replies when commands are sent."""

    def __init__(self, scripts=None, initial=(), **serial_options):
        self.scripts = scripts or {}
        self.lines = deque(self._as_bytes(line) for line in initial)
        self.serial_options = serial_options
        self.writes: list[str] = []
        self.is_open = True
        self.closed = False
        self.flushed = False
        self.output_reset = False
        self.dtr = True
        self.rts = True

    @staticmethod
    def _as_bytes(line):
        if isinstance(line, bytes):
            return line
        return (str(line) + "\n").encode("ascii")

    def write(self, data):
        command = bytes(data).decode("ascii").strip()
        self.writes.append(command)
        replies = self.scripts.get(command, ())
        if callable(replies):
            replies = replies(self)
        self.lines.extend(self._as_bytes(line) for line in replies)
        return len(data)

    def readline(self):
        if self.lines:
            return self.lines.popleft()
        return b""

    def reset_output_buffer(self):
        self.output_reset = True

    def flush(self):
        self.flushed = True

    def close(self):
        self.closed = True
        self.is_open = False


class SerialFactory:
    def __init__(self, builders):
        self.builders = builders
        self.created: list[ScriptedSerial] = []

    def __call__(self, **options):
        serial_port = self.builders[options["port"]](**options)
        self.created.append(serial_port)
        return serial_port


def rig_for(serial_port: ScriptedSerial, port="/dev/ttyUSB0") -> backend.Esp32Rig:
    return backend.Esp32Rig(
        port,
        serial_factory=lambda **_options: serial_port,
        connection_attempts=1,
        handshake_probes=1,
        handshake_response_timeout_s=0.01,
        boot_settle_s=0,
        sleep=lambda _seconds: None,
    )


class DiscoveryTests(unittest.TestCase):
    def test_discovery_uses_known_vid_pid_only_and_prioritizes_cp210x(self):
        ports = [
            SimpleNamespace(
                device="/dev/ttyACM9",
                vid=0x9999,
                pid=0x0001,
                description="Unrelated USB serial",
                serial_number=None,
            ),
            SimpleNamespace(
                device="/dev/ttyUSB1",
                vid=0x0403,
                pid=0x6001,
                description="FTDI",
                serial_number="F1",
            ),
            SimpleNamespace(
                device="/dev/ttyUSB0",
                vid=0x10C4,
                pid=0xEA60,
                description="CP2102 USB to UART",
                serial_number="0001",
            ),
            SimpleNamespace(
                device="/dev/ttyS0",
                vid=None,
                pid=None,
                description="UART",
                serial_number=None,
            ),
        ]

        candidates = backend.discover_candidate_ports(lambda: ports)

        self.assertEqual(
            [candidate.device for candidate in candidates],
            ["/dev/ttyUSB0", "/dev/ttyUSB1"],
        )
        self.assertEqual(candidates[0].bridge, "Silicon Labs CP210x")
        self.assertEqual(candidates[0].serial_number, "0001")

    def test_no_candidate_is_a_normal_exception(self):
        with self.assertRaises(backend.Esp32NotFoundError):
            backend.find_port(lambda: [])


class ConnectionTests(unittest.TestCase):
    def test_auto_connect_validates_candidates_and_is_idempotent(self):
        ports = [
            SimpleNamespace(
                device="/dev/ttyUSB0", vid=0x10C4, pid=0xEA60,
                description="CP210x", serial_number="wrong",
            ),
            SimpleNamespace(
                device="/dev/ttyUSB1", vid=0x10C4, pid=0xEA60,
                description="CP210x", serial_number="rig",
            ),
        ]
        factory = SerialFactory(
            {
                "/dev/ttyUSB0": lambda **_options: ScriptedSerial(
                    {"IDN?": ["NOT-AN-ELTEC-BOARD"]}
                ),
                "/dev/ttyUSB1": lambda **_options: ScriptedSerial(
                    {"IDN?": ["ELTEC-ESP32-ADS1256,v1.7.2"]},
                    initial=["READY,ELTEC-ESP32-ADS1256"],
                ),
            }
        )
        rig = backend.Esp32Rig(
            serial_factory=factory,
            port_lister=lambda: ports,
            connection_attempts=1,
            handshake_probes=1,
            handshake_response_timeout_s=0.01,
            boot_settle_s=0,
            sleep=lambda _seconds: None,
        )

        rig.connect()
        rig.connect()

        self.assertEqual(len(factory.created), 2)
        self.assertTrue(factory.created[0].closed)
        self.assertIs(rig.ser, factory.created[1])
        self.assertEqual(rig.port_name, "/dev/ttyUSB1")
        self.assertEqual(rig.identity.version, (1, 7, 2))
        self.assertTrue(rig.identity.ready_banner_seen)
        self.assertFalse(rig.ser.dtr)
        self.assertFalse(rig.ser.rts)
        self.assertTrue(rig.ser.output_reset)

        selected_serial = rig.ser
        rig.close()
        self.assertIn("PWM,OFF", selected_serial.writes)
        self.assertTrue(selected_serial.flushed)
        self.assertTrue(selected_serial.closed)
        self.assertFalse(rig.connected)

    def test_old_firmware_is_rejected_with_flash_guidance(self):
        serial_port = ScriptedSerial(
            {"IDN?": ["ELTEC-ESP32-ADS1256,v1.6"]}
        )
        rig = rig_for(serial_port)

        with self.assertRaisesRegex(backend.UnsupportedFirmwareError, "flash v1.7"):
            rig.connect()
        self.assertTrue(serial_port.closed)

    def test_missing_pyserial_does_not_exit_process(self):
        with mock.patch.object(backend, "_serial_module", None), mock.patch.object(
            backend, "_SERIAL_IMPORT_ERROR", None
        ):
            rig = backend.Esp32Rig(
                "/dev/ttyUSB0",
                connection_attempts=1,
                boot_settle_s=0,
            )
            with self.assertRaisesRegex(
                backend.PySerialUnavailableError, "python3-serial"
            ):
                rig.connect()

    def test_probe_contract_returns_bool_and_message(self):
        fake_rig = mock.Mock()
        fake_rig.identity = backend.FirmwareIdentity(
            "ELTEC-ESP32-ADS1256,v1.7", (1, 7, 0), True
        )
        fake_rig.port_name = "/dev/ttyUSB0"
        with mock.patch.object(backend, "Esp32Rig", return_value=fake_rig):
            result = backend.probe_esp32_status()

        self.assertEqual(
            result,
            (True, "ELTEC-ESP32-ADS1256,v1.7 connected on /dev/ttyUSB0."),
        )
        self.assertIsInstance(result[0], bool)
        fake_rig.connect.assert_called_once_with()
        fake_rig.close.assert_called_once_with()

    def test_usb_io_error_invalidates_port_and_allows_reconnect(self):
        for operation in ("write", "read"):
            with self.subTest(operation=operation):
                first = ScriptedSerial(
                    {"IDN?": ["ELTEC-ESP32-ADS1256,v1.7"]}
                )
                second = ScriptedSerial(
                    {"IDN?": ["ELTEC-ESP32-ADS1256,v1.7"]}
                )
                serial_ports = deque((first, second))
                rig = backend.Esp32Rig(
                    "/dev/ttyUSB0",
                    serial_factory=lambda **_options: serial_ports.popleft(),
                    connection_attempts=1,
                    handshake_probes=1,
                    handshake_response_timeout_s=0.01,
                    boot_settle_s=0,
                    sleep=lambda _seconds: None,
                )
                rig.connect()

                def disconnected(*_args, **_kwargs):
                    raise OSError("USB disconnected")

                if operation == "write":
                    first.write = disconnected
                    action = lambda: rig._send("STATUS?")
                else:
                    first.readline = disconnected
                    action = rig._readline

                with self.assertRaises(backend.Esp32ConnectionError):
                    action()
                self.assertIsNone(rig.ser)
                self.assertTrue(first.closed)

                rig.connect()
                self.assertIs(rig.ser, second)
                rig.close()


class ScalarAndPwmTests(unittest.TestCase):
    def setUp(self):
        self.serial_port = ScriptedSerial(
            {
                "IDN?": ["ELTEC-ESP32-ADS1256,v1.7"],
                "BAT?": ["BAT,6.1110"],
                "OFFSET?": ["OFFSET,0.71825"],
                "PIN,25": ["OK,PIN,25"],
                "PWM,ON": ["OK,PWM,ON"],
                "PWM,OFF": ["OK,PWM,OFF"],
            }
        )
        self.rig = rig_for(self.serial_port)
        self.rig.connect()

    def tearDown(self):
        self.rig.close()

    def test_scalar_reads_and_gui_compatible_pwm_signatures(self):
        self.assertAlmostEqual(
            self.rig.read_battery_voltage(samples=12, delay_s=0.005), 6.1110
        )
        self.assertAlmostEqual(
            self.rig.read_offset_voltage(
                waveform_range_v=2.5, samples=24, delay_s=0.003
            ),
            0.71825,
        )

        activation_time = self.rig.configure_emitter_pwm("DIO0", 10.0, 50.0)
        self.assertTrue(self.rig.pwm_enabled)
        self.assertEqual(
            activation_time,
            self.rig.last_pwm_activation_monotonic,
        )
        self.assertEqual(
            self.serial_port.writes[-2:], ["PIN,25", "PWM,ON"]
        )

        deactivation_time = self.rig.disable_emitter_pwm("DIO0")
        self.assertFalse(self.rig.pwm_enabled)
        self.assertEqual(
            deactivation_time,
            self.rig.last_pwm_deactivation_monotonic,
        )
        self.assertGreaterEqual(deactivation_time, activation_time)
        self.assertEqual(self.serial_port.writes[-1], "PWM,OFF")

    def test_non_fixed_pwm_is_rejected_before_hardware_command(self):
        writes_before = list(self.serial_port.writes)
        with self.assertRaisesRegex(ValueError, "fixed at 10 Hz"):
            self.rig.configure_emitter_pwm("DIO0", 12.0, 50.0)
        with self.assertRaisesRegex(ValueError, "fixed at 50%"):
            self.rig.configure_emitter_pwm("GPIO25", 10.0, 40.0)
        self.assertEqual(self.serial_port.writes, writes_before)

    def test_firmware_errors_are_exceptions(self):
        self.serial_port.scripts["BAT?"] = ["ERR,ADS1256 timeout"]
        with self.assertRaisesRegex(backend.Esp32ProtocolError, "ADS1256 timeout"):
            self.rig.read_battery_voltage()


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.serial_port = ScriptedSerial(
            {
                "IDN?": ["ELTEC-ESP32-ADS1256,v1.7"],
                "STREAM,START": ["STREAM,BEGIN,1000,SENSOR"],
                "STREAM,START,REF": ["STREAM,BEGIN,1000,REF"],
                "STREAM,STOP": [
                    "D,5000,104,0.504000,0",
                    "STREAM,END,5,0",
                ],
            }
        )
        self.rig = rig_for(self.serial_port)
        self.rig.connect()

    def tearDown(self):
        self.rig.close()

    def test_stream_parsing_gap_torn_count_rate_and_clean_drain(self):
        header = self.rig.start_stream("sensor")
        self.assertEqual(header.channel, "SENSOR")
        self.assertEqual(header.sample_rate_hz, 1000.0)
        self.serial_port.lines.extend(
            ScriptedSerial._as_bytes(line)
            for line in (
                "D,1000,100,0.500000,0",
                "D,2000,101,0.501000,1",
                "D,3000,not-an-int,0.502000,1",  # torn record
                "boot noise",                    # ignored non-data line
                "D,4000,103,0.503000,0",         # one missing timestamp
            )
        )

        samples = self.rig.read_stream(max_samples=3, timeout_s=0.02)
        self.assertEqual([sample.timestamp_us for sample in samples], [1000, 2000, 4000])
        self.assertEqual(samples[0].t_us, 1000)
        self.assertEqual(samples[1].raw, 101)
        self.assertAlmostEqual(samples[2].volts, 0.503)
        self.assertEqual(samples[1].sync, 1)

        diagnostics = self.rig.stop_stream(timeout_s=0.02)
        self.assertFalse(self.rig.is_streaming)
        self.assertEqual(
            [sample.timestamp_us for sample in self.rig.drained_samples], [5000]
        )
        self.assertEqual(diagnostics.received_samples, 4)
        self.assertEqual(diagnostics.drained_samples, 1)
        self.assertEqual(diagnostics.torn_lines, 1)
        self.assertEqual(diagnostics.ignored_lines, 1)
        self.assertEqual(diagnostics.timestamp_gap_count, 1)
        self.assertEqual(diagnostics.estimated_missing_samples, 1)
        self.assertEqual(diagnostics.firmware_samples_sent, 5)
        self.assertEqual(diagnostics.firmware_adc_overruns, 0)
        self.assertEqual(diagnostics.count_difference, 1)
        self.assertFalse(diagnostics.count_matches_firmware)
        self.assertAlmostEqual(diagnostics.measured_rate_hz, 1000.0)
        self.assertAlmostEqual(diagnostics.received_rate_hz, 750.0)
        self.assertFalse(diagnostics.healthy)  # the detected torn line/gap matters
        self.assertIn("1 torn", diagnostics.summary())
        self.assertEqual(self.serial_port.writes[-1], "STREAM,STOP")

    def test_reference_stream_selects_ain1_firmware_channel(self):
        header = self.rig.start_stream("reference")

        self.assertEqual(header.channel, "REF")
        self.assertEqual(header.sample_rate_hz, 1000.0)
        self.assertEqual(self.serial_port.writes[-1], "STREAM,START,REF")
        self.rig.stop_stream(timeout_s=0.02)
        self.assertEqual(self.serial_port.writes[-1], "STREAM,STOP")

    def test_timestamp_wraparound_is_not_a_gap_or_reordering(self):
        self.serial_port.scripts["STREAM,STOP"] = ["STREAM,END,2,0"]
        self.rig.start_stream()
        first = 0xFFFFFF00
        second = (first + 1000) & 0xFFFFFFFF
        self.serial_port.lines.extend(
            [
                ScriptedSerial._as_bytes(f"D,{first},1,0.1,0"),
                ScriptedSerial._as_bytes(f"D,{second},2,0.2,1"),
            ]
        )

        samples = self.rig.read_stream(max_samples=2, timeout_s=0.02)
        diagnostics = self.rig.stop_stream(timeout_s=0.02)

        self.assertEqual(len(samples), 2)
        self.assertEqual(diagnostics.timestamp_gap_count, 0)
        self.assertEqual(diagnostics.reordered_timestamps, 0)
        self.assertTrue(diagnostics.count_matches_firmware)
        self.assertAlmostEqual(diagnostics.measured_rate_hz, 1000.0)

    def test_close_stops_stream_before_forcing_pwm_off(self):
        self.serial_port.scripts["STREAM,STOP"] = ["STREAM,END,0,0"]
        self.rig.start_stream()

        self.rig.close()

        self.assertLess(
            self.serial_port.writes.index("STREAM,STOP"),
            self.serial_port.writes.index("PWM,OFF"),
        )
        self.assertTrue(self.serial_port.closed)

    def test_firmware_adc_overrun_is_exposed_as_unhealthy(self):
        self.serial_port.scripts["STREAM,STOP"] = ["STREAM,END,0,2"]
        self.rig.start_stream()

        diagnostics = self.rig.stop_stream(timeout_s=0.02)

        self.assertEqual(diagnostics.firmware_adc_overruns, 2)
        self.assertFalse(diagnostics.healthy)
        self.assertIn("2 ADC overruns", diagnostics.summary())

    def test_missing_end_marker_has_diagnostics_and_timeout_exception(self):
        self.serial_port.scripts["STREAM,STOP"] = []
        self.rig.start_stream()

        with self.assertRaises(backend.StreamTimeoutError):
            self.rig.stop_stream(timeout_s=0.01)
        self.assertFalse(self.rig.is_streaming)
        self.assertFalse(self.rig.stream_diagnostics.stop_marker_seen)


if __name__ == "__main__":
    unittest.main()
