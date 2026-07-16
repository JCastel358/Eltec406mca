"""Serial backend for the Eltec ESP32 + ADS1256 emitter-test rig.

This module intentionally contains no Tkinter or NumPy dependencies.  It is
the small hardware boundary used by the Xubuntu GUI: pyserial discovers and
opens the board, this class validates the firmware protocol, and the GUI turns
the returned samples into NumPy arrays for the existing analysis engine.

The firmware protocol is defined in ``Arduino/Eltec/Eltec.ino``. Firmware
v1.7 is the first supported production build because it verifies the ADC's
1000 SPS configuration and emits exactly one sample for each real ADS1256
DRDY edge. Importing this module is safe when
pyserial is absent; a
normal, actionable exception is raised only when serial hardware is requested.
"""

from __future__ import annotations

import math
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

try:  # Keep simulator/test imports working on machines without pyserial.
    import serial as _serial_module
    from serial.tools import list_ports as _serial_list_ports
except ImportError as _serial_import_error:  # pragma: no cover - environment dependent
    _serial_module = None
    _serial_list_ports = None
    _SERIAL_IMPORT_ERROR: BaseException | None = _serial_import_error
else:
    _SERIAL_IMPORT_ERROR = None


BAUD_RATE = 500_000
SAMPLE_RATE_HZ = 1_000.0
STREAM_READ_BLOCK_BYTES = 1_024
PWM_FREQUENCY_HZ = 10.0
PWM_DUTY_CYCLE_PERCENT = 50.0
PWM_GPIO = 25
MINIMUM_FIRMWARE_VERSION = (1, 7, 0)
EXPECTED_FIRMWARE_PREFIX = "ELTEC-ESP32-ADS1256,v"

# USB bridges used by ESP32 development boards.  Auto-discovery deliberately
# stays on known VID/PID pairs; every candidate is then validated with IDN?.
KNOWN_USB_IDS: dict[tuple[int, int], str] = {
    (0x10C4, 0xEA60): "Silicon Labs CP210x",
    (0x10C4, 0xEA70): "Silicon Labs CP2105",
    (0x10C4, 0xEA71): "Silicon Labs CP2108",
    (0x1A86, 0x7523): "WCH CH340",
    (0x1A86, 0x55D4): "WCH CH9102",
    (0x0403, 0x6001): "FTDI FT232",
    (0x303A, 0x1001): "Espressif native USB",
}

_IDENTITY_RE = re.compile(
    r"^ELTEC-ESP32-ADS1256,v(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?$"
)
_READY_PREFIX = "READY,ELTEC-ESP32-ADS1256"
_UINT32_MASK = (1 << 32) - 1


class Esp32BackendError(RuntimeError):
    """Base class for expected backend failures."""


class PySerialUnavailableError(Esp32BackendError):
    """Raised when hardware access is requested without pyserial installed."""


class Esp32NotFoundError(Esp32BackendError):
    """Raised when no known ESP32 USB-serial bridge is present."""


class Esp32ConnectionError(Esp32BackendError):
    """Raised when serial candidates cannot complete the protocol handshake."""


class Esp32ProtocolError(Esp32BackendError):
    """Raised for a firmware error or malformed command response."""


class UnsupportedFirmwareError(Esp32ProtocolError):
    """Raised when the connected Eltec board predates the required protocol."""


class StreamStateError(Esp32BackendError):
    """Raised for an invalid stream start/read/stop state transition."""


class StreamTimeoutError(Esp32BackendError):
    """Raised when STREAM,STOP is not acknowledged with STREAM,END."""


@dataclass(frozen=True)
class PortCandidate:
    """A USB serial port whose VID/PID matches a supported bridge."""

    device: str
    vid: int
    pid: int
    bridge: str
    description: str = ""
    serial_number: str | None = None


@dataclass(frozen=True)
class FirmwareIdentity:
    """Validated response to the firmware ``IDN?`` command."""

    text: str
    version: tuple[int, int, int]
    ready_banner_seen: bool = False

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)


@dataclass(frozen=True)
class StreamHeader:
    """The parsed ``STREAM,BEGIN,<rate>,<channel>`` response."""

    sample_rate_hz: float
    channel: str

    @property
    def rate_hz(self) -> float:
        """Short alias useful to callers displaying the header."""

        return self.sample_rate_hz


@dataclass(frozen=True)
class StreamSample:
    """One ADS1256 sample and the contemporaneous digital PWM state."""

    timestamp_us: int
    raw: int
    volts: float
    sync: int

    @property
    def t_us(self) -> int:
        """Compatibility alias used by the earlier command-line wrapper."""

        return self.timestamp_us


@dataclass
class StreamDiagnostics:
    """Integrity and timing information accumulated for one stream.

    ``measured_rate_hz`` estimates the source cadence after accounting for
    timestamp gaps.  ``received_rate_hz`` is deliberately lower when records
    were lost and therefore describes host-side delivered throughput.
    """

    expected_rate_hz: float
    channel: str
    started_monotonic: float
    stopped_monotonic: float | None = None
    received_samples: int = 0
    drained_samples: int = 0
    torn_lines: int = 0
    ignored_lines: int = 0
    timestamp_gap_count: int = 0
    estimated_missing_samples: int = 0
    duplicate_timestamps: int = 0
    reordered_timestamps: int = 0
    first_timestamp_us: int | None = None
    last_timestamp_us: int | None = None
    firmware_samples_sent: int | None = None
    firmware_adc_overruns: int | None = None
    stop_marker_seen: bool = False
    _elapsed_device_us: int = field(default=0, repr=False)
    _valid_intervals_us: list[int] = field(default_factory=list, repr=False)

    @property
    def device_span_seconds(self) -> float | None:
        if self.received_samples < 2 or self._elapsed_device_us <= 0:
            return None
        return self._elapsed_device_us / 1_000_000.0

    @property
    def received_rate_hz(self) -> float | None:
        span = self.device_span_seconds
        if span is None or span <= 0:
            return None
        return max(0, self.received_samples - 1) / span

    @property
    def measured_rate_hz(self) -> float | None:
        """Estimate source cadence from timestamp intervals.

        The median normal interval is resistant to a few host-side dropped
        records.  If every interval is a gap, the inferred missing-count/span
        calculation still provides a useful estimate.
        """

        if self._valid_intervals_us:
            median_interval = float(statistics.median(self._valid_intervals_us))
            if median_interval > 0:
                return 1_000_000.0 / median_interval
        span = self.device_span_seconds
        if span is None or span <= 0:
            return None
        source_intervals = (
            max(0, self.received_samples - 1) + self.estimated_missing_samples
        )
        return source_intervals / span

    @property
    def rate_error_percent(self) -> float | None:
        measured = self.measured_rate_hz
        if measured is None or self.expected_rate_hz <= 0:
            return None
        return 100.0 * (measured - self.expected_rate_hz) / self.expected_rate_hz

    @property
    def count_difference(self) -> int | None:
        if self.firmware_samples_sent is None:
            return None
        return self.firmware_samples_sent - self.received_samples

    @property
    def count_matches_firmware(self) -> bool | None:
        difference = self.count_difference
        return None if difference is None else difference == 0

    @property
    def healthy(self) -> bool:
        count_ok = self.count_matches_firmware
        rate_error = self.rate_error_percent
        return (
            self.torn_lines == 0
            and self.timestamp_gap_count == 0
            and self.duplicate_timestamps == 0
            and self.reordered_timestamps == 0
            and (self.firmware_adc_overruns or 0) == 0
            and count_ok is not False
            and (rate_error is None or abs(rate_error) <= 2.0)
        )

    def summary(self) -> str:
        rate = self.measured_rate_hz
        rate_text = "unknown rate" if rate is None else f"{rate:.1f} Hz"
        count_text = str(self.received_samples)
        if self.firmware_samples_sent is not None:
            count_text += f"/{self.firmware_samples_sent} records"
        else:
            count_text += " records"
        return (
            f"{count_text}, {rate_text}, {self.torn_lines} torn, "
            f"{self.timestamp_gap_count} gaps "
            f"(~{self.estimated_missing_samples} missing), "
            f"{self.firmware_adc_overruns or 0} ADC overruns"
        )


def _require_pyserial() -> None:
    if _serial_module is None:
        message = (
            "pyserial is required for the ESP32 rig. Install Ubuntu's "
            "python3-serial package or run: python3 -m pip install pyserial"
        )
        if _SERIAL_IMPORT_ERROR is None:
            raise PySerialUnavailableError(message)
        raise PySerialUnavailableError(message) from _SERIAL_IMPORT_ERROR


def discover_candidate_ports(
    port_lister: Callable[[], Iterable[Any]] | None = None,
) -> list[PortCandidate]:
    """Return deterministic, known-VID/PID serial candidates.

    A description containing "USB" is not enough to select a device: printers,
    modems, and UPS units commonly expose serial ports too.  ``Esp32Rig.connect``
    validates each returned candidate with the Eltec ``IDN?`` response.
    """

    if port_lister is None:
        _require_pyserial()
        assert _serial_list_ports is not None
        port_lister = _serial_list_ports.comports

    candidates: list[PortCandidate] = []
    for port in port_lister():
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        device = str(getattr(port, "device", "") or "")
        if not device or vid is None or pid is None:
            continue
        try:
            numeric_id = (int(vid), int(pid))
        except (TypeError, ValueError):
            continue
        bridge = KNOWN_USB_IDS.get(numeric_id)
        if bridge is None:
            continue
        candidates.append(
            PortCandidate(
                device=device,
                vid=numeric_id[0],
                pid=numeric_id[1],
                bridge=bridge,
                description=str(getattr(port, "description", "") or ""),
                serial_number=getattr(port, "serial_number", None),
            )
        )

    # Stable ordering makes multiple attached adapters predictable.  CP210x
    # (the production board) naturally sorts before the fallback bridge types.
    priority = {usb_id: index for index, usb_id in enumerate(KNOWN_USB_IDS)}
    return sorted(
        candidates,
        key=lambda item: (priority[(item.vid, item.pid)], item.device),
    )


def find_port(port_lister: Callable[[], Iterable[Any]] | None = None) -> str:
    """Return the first known USB candidate (connection still validates it)."""

    candidates = discover_candidate_ports(port_lister)
    if not candidates:
        raise Esp32NotFoundError(
            "No supported ESP32 USB serial adapter was found. Plug in the rig "
            "or pass its explicit port (for example /dev/ttyUSB0)."
        )
    return candidates[0].device


class Esp32Rig:
    """Low-level, cross-platform interface to the Eltec ESP32 firmware.

    Parameters used for serial construction and timing are injectable so the
    protocol can be tested without opening real hardware.
    """

    def __init__(
        self,
        port: str | None = None,
        *,
        baud_rate: int = BAUD_RATE,
        read_timeout_s: float = 0.25,
        write_timeout_s: float = 1.0,
        connection_attempts: int = 2,
        handshake_probes: int = 7,
        handshake_response_timeout_s: float = 0.65,
        boot_settle_s: float = 0.12,
        serial_factory: Callable[..., Any] | None = None,
        port_lister: Callable[[], Iterable[Any]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.requested_port = str(port) if port else None
        self.port_name: str | None = None
        self.baud_rate = int(baud_rate)
        self.read_timeout_s = max(0.01, float(read_timeout_s))
        self.write_timeout_s = max(0.01, float(write_timeout_s))
        self.connection_attempts = max(1, int(connection_attempts))
        self.handshake_probes = max(1, int(handshake_probes))
        self.handshake_response_timeout_s = max(
            0.01, float(handshake_response_timeout_s)
        )
        self.boot_settle_s = max(0.0, float(boot_settle_s))
        self._serial_factory = serial_factory
        self._port_lister = port_lister
        self._monotonic = monotonic
        self._sleep = sleep

        self.ser: Any | None = None
        self.identity: FirmwareIdentity | None = None
        self.pwm_enabled = False
        self.last_pwm_activation_monotonic: float | None = None
        self.last_pwm_deactivation_monotonic: float | None = None
        self._stream_active = False
        self._stream_header: StreamHeader | None = None
        self._stream_diagnostics: StreamDiagnostics | None = None
        self._last_sample_timestamp_us: int | None = None
        self._drained_samples: list[StreamSample] = []
        # pyserial.readline() commonly performs one OS read per byte. At 1,000
        # ASCII sample lines/second that can consume the CP210x backlog more
        # slowly than the ESP32 produces it during a full timeout capture.
        # Keep a user-space buffer so one bulk read supplies many parsed lines.
        self._read_buffer = bytearray()

    @property
    def connected(self) -> bool:
        if self.ser is None:
            return False
        return bool(getattr(self.ser, "is_open", True))

    @property
    def is_streaming(self) -> bool:
        return self._stream_active

    @property
    def stream_header(self) -> StreamHeader | None:
        return self._stream_header

    @property
    def stream_diagnostics(self) -> StreamDiagnostics | None:
        return self._stream_diagnostics

    @property
    def drained_samples(self) -> tuple[StreamSample, ...]:
        """Samples encountered while draining after STREAM,STOP."""

        return tuple(self._drained_samples)

    def __enter__(self) -> "Esp32Rig":
        self.connect()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Discovery and connection
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Connect and validate firmware v1.7 or newer.

        The operation is idempotent.  Auto-discovery tries every known USB
        bridge and accepts only a board returning the exact Eltec identity.
        Opening a CP210x commonly resets an ESP32, so IDN is probed repeatedly
        while READY/boot lines are consumed; failed opens are retried too.
        """

        if self.connected:
            return
        if self.ser is not None:
            self._discard_serial()

        if self.requested_port:
            port_names = [self.requested_port]
        else:
            candidates = discover_candidate_ports(self._port_lister)
            if not candidates:
                raise Esp32NotFoundError(
                    "No supported ESP32 USB serial adapter was found. Plug in "
                    "the rig, check the USB cable, or provide an explicit port."
                )
            port_names = [candidate.device for candidate in candidates]

        errors: list[str] = []
        unsupported: UnsupportedFirmwareError | None = None
        for port_name in port_names:
            for attempt in range(1, self.connection_attempts + 1):
                try:
                    self._open_and_handshake(port_name)
                    return
                except UnsupportedFirmwareError as exc:
                    unsupported = exc
                    errors.append(f"{port_name}: {exc}")
                    self._discard_serial()
                    # A valid but old Eltec identity will not improve merely by
                    # reopening; move to the next auto-discovery candidate.
                    break
                except PySerialUnavailableError:
                    self._discard_serial()
                    raise
                except Exception as exc:
                    errors.append(
                        f"{port_name} (attempt {attempt}/{self.connection_attempts}): {exc}"
                    )
                    self._discard_serial()
                    if attempt < self.connection_attempts:
                        self._sleep(0.15)

        if unsupported is not None and len(port_names) == 1:
            raise unsupported
        details = "; ".join(errors) if errors else "no candidate responded"
        raise Esp32ConnectionError(
            "Could not connect to a compatible Eltec ESP32 rig. " + details
        )

    def _open_and_handshake(self, port_name: str) -> None:
        factory = self._serial_factory
        if factory is None:
            _require_pyserial()
            assert _serial_module is not None
            factory = _serial_module.Serial

        serial_options = dict(
            port=port_name,
            baudrate=self.baud_rate,
            timeout=self.read_timeout_s,
            write_timeout=self.write_timeout_s,
        )
        # Xubuntu is the production host. An exclusive tty lock prevents a
        # Serial Monitor or a second tester process from silently consuming a
        # subset of waveform records from the same USB receive queue.
        if os.name == "posix":
            serial_options["exclusive"] = True
        serial_port = factory(**serial_options)
        self.ser = serial_port
        self.port_name = port_name
        self._read_buffer.clear()

        # Release both modem-control lines after open.  The open itself supplies
        # the CP210x reset edge on the production DevKit; leaving either asserted
        # can otherwise hold EN or GPIO0 in an unwanted state on some clones.
        self._release_modem_control_lines(serial_port)
        try:
            reset_output = getattr(serial_port, "reset_output_buffer", None)
            if callable(reset_output):
                reset_output()
        except Exception:
            pass
        if self.boot_settle_s:
            self._sleep(self.boot_settle_s)

        identity = self._handshake()
        self.identity = identity
        self.pwm_enabled = False  # a serial open/reset always starts safe.

    @staticmethod
    def _release_modem_control_lines(serial_port: Any) -> None:
        for attribute, method_name in (("dtr", "setDTR"), ("rts", "setRTS")):
            try:
                setattr(serial_port, attribute, False)
                continue
            except Exception:
                pass
            try:
                method = getattr(serial_port, method_name, None)
                if callable(method):
                    method(False)
            except Exception:
                pass

    def _handshake(self) -> FirmwareIdentity:
        ready_seen = False
        last_interesting_line = ""
        for _probe in range(self.handshake_probes):
            self._send("IDN?")
            deadline = self._monotonic() + self.handshake_response_timeout_s
            empty_reads = 0
            max_empty_reads = max(
                2,
                int(math.ceil(self.handshake_response_timeout_s / self.read_timeout_s))
                + 2,
            )
            while self._monotonic() < deadline and empty_reads < max_empty_reads:
                line = self._readline()
                if not line:
                    empty_reads += 1
                    continue
                empty_reads = 0
                if line.startswith(_READY_PREFIX):
                    ready_seen = True
                    last_interesting_line = line
                    continue
                if line.startswith("ERR,"):
                    # An ADS startup failure is actionable and repeated READY
                    # probes will not repair wiring or DRDY.
                    if "ADS1256" in line.upper():
                        raise Esp32ProtocolError(line)
                    last_interesting_line = line
                    continue
                if line.startswith(EXPECTED_FIRMWARE_PREFIX):
                    return self._validate_identity(line, ready_seen)
                # Boot ROM output and bytes from a non-Eltec serial device are
                # tolerated until all probes have been exhausted.
                last_interesting_line = line
            self._sleep(0.08)

        suffix = (
            f" Last response: {last_interesting_line!r}."
            if last_interesting_line
            else ""
        )
        raise Esp32ConnectionError(
            "Timed out waiting for ELTEC-ESP32-ADS1256 identity after the "
            f"USB reset/startup period.{suffix}"
        )

    @staticmethod
    def _validate_identity(line: str, ready_seen: bool) -> FirmwareIdentity:
        match = _IDENTITY_RE.fullmatch(line.strip())
        if match is None:
            raise Esp32ProtocolError(f"Malformed ESP32 identity: {line!r}")
        version = (
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch") or 0),
        )
        if version < MINIMUM_FIRMWARE_VERSION:
            minimum = ".".join(str(part) for part in MINIMUM_FIRMWARE_VERSION[:2])
            actual = ".".join(str(part) for part in version[:2])
            raise UnsupportedFirmwareError(
                f"Eltec firmware v{actual} is too old; flash v{minimum} or newer "
                "so the ADS1256 sample rate is verified and each conversion is "
                "consumed exactly once."
            )
        return FirmwareIdentity(line.strip(), version, ready_seen)

    def close(self, disable_pwm: bool = True) -> None:
        """Best-effort safe shutdown; never masks the caller's real error."""

        serial_port = self.ser
        if serial_port is None:
            return
        try:
            if self._stream_active:
                try:
                    self.stop_stream(timeout_s=1.0, raise_on_timeout=False)
                except Exception:
                    self._stream_active = False
            if disable_pwm:
                try:
                    self._send("PWM,OFF")
                    flush = getattr(serial_port, "flush", None)
                    if callable(flush):
                        flush()
                except Exception:
                    pass
        finally:
            try:
                serial_port.close()
            except Exception:
                pass
            self.ser = None
            self.port_name = None
            self.identity = None
            self.pwm_enabled = False
            self._stream_active = False
            self._read_buffer.clear()

    def _discard_serial(self) -> None:
        serial_port = self.ser
        self.ser = None
        self.port_name = None
        self.identity = None
        self._stream_active = False
        self.pwm_enabled = False
        self._read_buffer.clear()
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Protocol helpers and scalar operations
    # ------------------------------------------------------------------
    def _require_connected(self) -> Any:
        if not self.connected:
            raise Esp32ConnectionError("ESP32 rig is not connected.")
        return self.ser

    def _send(self, command: str) -> None:
        serial_port = self._require_connected()
        try:
            serial_port.write((command.rstrip("\r\n") + "\n").encode("ascii"))
        except Exception as exc:
            # A USB unplug can leave pyserial's is_open flag true even though
            # the file descriptor is dead. Invalidate it so the GUI's next
            # retry performs discovery and opens a fresh device.
            self._discard_serial()
            raise Esp32ConnectionError(f"Could not send {command!r}: {exc}") from exc

    def _readline(self) -> str:
        serial_port = self._require_connected()
        while True:
            newline_index = self._read_buffer.find(b"\n")
            if newline_index >= 0:
                raw_line = bytes(self._read_buffer[:newline_index])
                del self._read_buffer[: newline_index + 1]
                return raw_line.decode("ascii", errors="replace").strip()

            try:
                read = getattr(serial_port, "read", None)
                if callable(read):
                    # Drain everything the driver already has in one syscall.
                    # During streaming, wait for a modest block when the driver
                    # is empty; scalar commands retain the low-latency one-byte
                    # behavior. Pyserial's timeout bounds either operation.
                    waiting = int(getattr(serial_port, "in_waiting", 0) or 0)
                    read_size = waiting
                    if read_size <= 0:
                        read_size = (
                            STREAM_READ_BLOCK_BYTES if self._stream_active else 1
                        )
                    raw = read(read_size)
                else:
                    # Compatibility for small test doubles and serial-like
                    # adapters that only expose readline().
                    raw = serial_port.readline()
            except Exception as exc:
                self._discard_serial()
                raise Esp32ConnectionError(f"Serial read failed: {exc}") from exc
            if not raw:
                return ""
            if isinstance(raw, str):
                raw = raw.encode("ascii", errors="replace")
            self._read_buffer.extend(bytes(raw))

    def _command(
        self,
        command: str,
        expect_prefix: str,
        *,
        timeout_s: float = 3.0,
    ) -> str:
        self._send(command)
        deadline = self._monotonic() + max(0.01, float(timeout_s))
        empty_reads = 0
        max_empty_reads = max(
            2, int(math.ceil(max(0.01, timeout_s) / self.read_timeout_s)) + 2
        )
        while self._monotonic() < deadline and empty_reads < max_empty_reads:
            line = self._readline()
            if not line:
                empty_reads += 1
                continue
            empty_reads = 0
            if line.startswith("D,") or line.startswith(_READY_PREFIX):
                continue
            if line.startswith("ERR,"):
                raise Esp32ProtocolError(f"{command} -> {line}")
            if line.startswith(expect_prefix):
                return line
        raise Esp32ProtocolError(
            f"Timed out waiting for {expect_prefix!r} after {command!r}."
        )

    @staticmethod
    def _parse_scalar_response(line: str, prefix: str) -> float:
        fields = line.split(",")
        if len(fields) != 2 or fields[0] != prefix:
            raise Esp32ProtocolError(f"Malformed {prefix} response: {line!r}")
        try:
            value = float(fields[1])
        except ValueError as exc:
            raise Esp32ProtocolError(f"Malformed {prefix} voltage: {line!r}") from exc
        if not math.isfinite(value):
            raise Esp32ProtocolError(f"Non-finite {prefix} voltage: {line!r}")
        return value

    def read_battery_voltage(
        self,
        samples: int | None = None,
        delay_s: float | None = None,
    ) -> float:
        """Read the firmware-filtered, divider-corrected 6 V battery voltage.

        ``samples`` and ``delay_s`` are accepted for v4 GUI compatibility; the
        firmware owns the median count and settling delay on this rig.
        """

        del samples, delay_s
        if self._stream_active:
            raise StreamStateError("Stop the waveform stream before reading the battery.")
        return self._parse_scalar_response(self._command("BAT?", "BAT,"), "BAT")

    def read_offset_voltage(
        self,
        waveform_range_v: float | None = None,
        samples: int | None = None,
        delay_s: float | None = None,
    ) -> float:
        """Read the firmware-filtered DUT DC offset on ADS1256 AIN0."""

        del waveform_range_v, samples, delay_s
        if self._stream_active:
            raise StreamStateError("Stop the waveform stream before reading offset.")
        return self._parse_scalar_response(
            self._command("OFFSET?", "OFFSET,"), "OFFSET"
        )

    def configure_analog_inputs(self, waveform_range_v: float | None = None) -> None:
        """Compatibility no-op: ADS1256 range/channel setup lives in firmware."""

        del waveform_range_v
        self._require_connected()

    @staticmethod
    def _validate_fixed_pwm(
        channel: str | int | None,
        frequency_hz: float,
        duty_cycle_percent: float,
    ) -> None:
        accepted_channels = {None, 25, "25", "GPIO25", "D25", "DIO0", "FIO0"}
        normalized: str | int | None = channel
        if isinstance(channel, str):
            normalized = channel.strip().upper()
        if normalized not in accepted_channels:
            raise ValueError(
                "The ESP32 rig has a fixed emitter output on GPIO25 "
                "(the GUI's legacy DIO0 name is also accepted)."
            )
        if not math.isclose(float(frequency_hz), PWM_FREQUENCY_HZ, abs_tol=1e-6):
            raise ValueError("The ESP32 emitter PWM is fixed at 10 Hz.")
        if not math.isclose(
            float(duty_cycle_percent), PWM_DUTY_CYCLE_PERCENT, abs_tol=1e-6
        ):
            raise ValueError("The ESP32 emitter PWM is fixed at 50% duty cycle.")

    def configure_emitter_pwm(
        self,
        channel: str | int | None = "DIO0",
        frequency_hz: float = PWM_FREQUENCY_HZ,
        duty_cycle_percent: float = PWM_DUTY_CYCLE_PERCENT,
    ) -> float:
        """Select GPIO25 and enable the fixed 10 Hz / 50% emitter drive."""

        self._validate_fixed_pwm(channel, frequency_hz, duty_cycle_percent)
        self._command(f"PIN,{PWM_GPIO}", f"OK,PIN,{PWM_GPIO}")
        # PIN selection can take a serial round trip while the emitter is still
        # off. Anchor the production deadline to the PWM,ON command itself.
        self.last_pwm_activation_monotonic = self._monotonic()
        self._command("PWM,ON", "OK,PWM,ON")
        self.pwm_enabled = True
        return self.last_pwm_activation_monotonic

    def enable_emitter_pwm(
        self,
        channel: str | int | None = "DIO0",
        frequency_hz: float = PWM_FREQUENCY_HZ,
        duty_cycle_percent: float = PWM_DUTY_CYCLE_PERCENT,
    ) -> float:
        """Alias retained for the earlier ESP32 command-line wrapper."""

        return self.configure_emitter_pwm(
            channel, frequency_hz, duty_cycle_percent
        )

    def disable_emitter_pwm(self, channel: str | int | None = "DIO0") -> float:
        """Disable emitter PWM; ``channel`` is accepted for v4 compatibility."""

        del channel
        self.last_pwm_deactivation_monotonic = self._monotonic()
        if not self.connected:
            self.pwm_enabled = False
            return self.last_pwm_deactivation_monotonic
        try:
            self._command("PWM,OFF", "OK,PWM,OFF")
        finally:
            self.pwm_enabled = False
        return self.last_pwm_deactivation_monotonic

    # ------------------------------------------------------------------
    # Streaming primitives
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_stream_channel(channel: str) -> tuple[str, str]:
        normalized = str(channel).strip().lower()
        if normalized in {"sensor", "dut", "ain0"}:
            return "STREAM,START", "SENSOR"
        if normalized in {"ref", "reference", "ain1"}:
            return "STREAM,START,REF", "REF"
        raise ValueError("Stream channel must be 'sensor' (AIN0) or 'ref' (AIN1).")

    @staticmethod
    def _parse_stream_header(line: str, expected_channel: str) -> StreamHeader:
        fields = line.split(",")
        if len(fields) != 4 or fields[:2] != ["STREAM", "BEGIN"]:
            raise Esp32ProtocolError(f"Malformed stream header: {line!r}")
        try:
            rate_hz = float(fields[2])
        except ValueError as exc:
            raise Esp32ProtocolError(f"Malformed stream rate: {line!r}") from exc
        channel = fields[3].strip().upper()
        if not math.isfinite(rate_hz) or rate_hz <= 0:
            raise Esp32ProtocolError(f"Invalid stream rate: {line!r}")
        if channel != expected_channel:
            raise Esp32ProtocolError(
                f"Firmware started {channel!r}, expected {expected_channel!r}."
            )
        return StreamHeader(rate_hz, channel)

    def start_stream(self, channel: str = "sensor") -> StreamHeader:
        """Start one firmware stream and parse its advertised rate/channel."""

        self._require_connected()
        if self._stream_active:
            raise StreamStateError("A waveform stream is already active.")
        command, expected_channel = self._normalize_stream_channel(channel)
        line = self._command(command, "STREAM,BEGIN", timeout_s=3.0)
        try:
            header = self._parse_stream_header(line, expected_channel)
        except Exception:
            # If the begin marker itself was malformed, still make a best-effort
            # attempt to leave firmware streaming state before surfacing it.
            try:
                self._send("STREAM,STOP")
            except Exception:
                pass
            raise

        self._stream_active = True
        self._stream_header = header
        self._stream_diagnostics = StreamDiagnostics(
            expected_rate_hz=header.sample_rate_hz,
            channel=header.channel,
            started_monotonic=self._monotonic(),
        )
        self._last_sample_timestamp_us = None
        self._drained_samples = []
        return header

    @staticmethod
    def _parse_stream_sample(line: str) -> StreamSample:
        fields = line.split(",")
        if len(fields) != 5 or fields[0] != "D":
            raise ValueError("not a complete D record")
        timestamp_us = int(fields[1])
        raw = int(fields[2])
        volts = float(fields[3])
        sync = int(fields[4])
        if timestamp_us < 0 or timestamp_us > _UINT32_MASK:
            raise ValueError("timestamp is outside uint32 range")
        if raw < -8_388_608 or raw > 8_388_607:
            raise ValueError("ADS1256 raw count is outside signed 24-bit range")
        if not math.isfinite(volts):
            raise ValueError("voltage is not finite")
        if sync not in (0, 1):
            raise ValueError("sync is not digital")
        return StreamSample(timestamp_us, raw, volts, sync)

    def _record_sample(self, sample: StreamSample, *, drained: bool) -> None:
        diagnostics = self._stream_diagnostics
        header = self._stream_header
        if diagnostics is None or header is None:
            raise StreamStateError("Stream diagnostics were not initialized.")

        previous = self._last_sample_timestamp_us
        if previous is None:
            diagnostics.first_timestamp_us = sample.timestamp_us
        else:
            modular_delta = (sample.timestamp_us - previous) & _UINT32_MASK
            expected_interval_us = 1_000_000.0 / header.sample_rate_hz
            if modular_delta == 0:
                diagnostics.duplicate_timestamps += 1
            elif modular_delta > (_UINT32_MASK // 2):
                diagnostics.reordered_timestamps += 1
            else:
                diagnostics._elapsed_device_us += modular_delta
                estimated_intervals = max(
                    1, int(round(modular_delta / expected_interval_us))
                )
                missing = max(0, estimated_intervals - 1)
                # Allow ordinary sampling jitter without calling it a gap.
                if modular_delta > expected_interval_us * 1.5:
                    diagnostics.timestamp_gap_count += 1
                    diagnostics.estimated_missing_samples += max(1, missing)
                else:
                    diagnostics._valid_intervals_us.append(modular_delta)
        self._last_sample_timestamp_us = sample.timestamp_us
        diagnostics.last_timestamp_us = sample.timestamp_us
        diagnostics.received_samples += 1
        if drained:
            diagnostics.drained_samples += 1

    def _consume_stream_line(
        self, line: str, *, drained: bool
    ) -> tuple[StreamSample | None, bool]:
        """Consume one line, returning ``(sample, end_marker_seen)``."""

        diagnostics = self._stream_diagnostics
        if diagnostics is None:
            raise StreamStateError("No stream diagnostics are active.")
        if line.startswith("D,"):
            try:
                sample = self._parse_stream_sample(line)
            except (TypeError, ValueError, OverflowError):
                diagnostics.torn_lines += 1
                return None, False
            self._record_sample(sample, drained=drained)
            return sample, False
        if line.startswith("STREAM,END"):
            fields = line.split(",")
            # v1.7 adds the ADC-overrun count. Accept the original three-field
            # marker defensively for future protocol diagnostics, although the
            # firmware-version handshake prevents old production captures.
            if len(fields) not in (3, 4):
                diagnostics.torn_lines += 1
                return None, True
            try:
                firmware_count = int(fields[2])
                if firmware_count < 0:
                    raise ValueError
                firmware_overruns = int(fields[3]) if len(fields) == 4 else 0
                if firmware_overruns < 0:
                    raise ValueError
            except ValueError:
                diagnostics.torn_lines += 1
            else:
                diagnostics.firmware_samples_sent = firmware_count
                diagnostics.firmware_adc_overruns = firmware_overruns
            diagnostics.stop_marker_seen = True
            return None, True
        if line.startswith("ERR,"):
            raise Esp32ProtocolError(f"Firmware stream error: {line}")
        if line:
            diagnostics.ignored_lines += 1
        return None, False

    def _finish_stream(self) -> None:
        diagnostics = self._stream_diagnostics
        if diagnostics is not None and diagnostics.stopped_monotonic is None:
            diagnostics.stopped_monotonic = self._monotonic()
        self._stream_active = False

    def read_stream(
        self,
        max_samples: int | None = None,
        *,
        timeout_s: float = 1.0,
    ) -> list[StreamSample]:
        """Read up to ``max_samples`` records from the active stream.

        With ``max_samples=None``, records are collected until ``timeout_s``
        elapses.  An empty list is a normal timeout result, allowing the GUI to
        remain responsive while it polls from a worker thread.
        """

        if not self._stream_active:
            raise StreamStateError("No waveform stream is active.")
        if max_samples is not None:
            max_samples = int(max_samples)
            if max_samples < 0:
                raise ValueError("max_samples cannot be negative.")
            if max_samples == 0:
                return []

        timeout_s = max(0.01, float(timeout_s))
        deadline = self._monotonic() + timeout_s
        max_empty_reads = max(2, int(math.ceil(timeout_s / self.read_timeout_s)) + 2)
        empty_reads = 0
        samples: list[StreamSample] = []
        while self._monotonic() < deadline and empty_reads < max_empty_reads:
            if max_samples is not None and len(samples) >= max_samples:
                break
            line = self._readline()
            if not line:
                empty_reads += 1
                continue
            empty_reads = 0
            sample, ended = self._consume_stream_line(line, drained=False)
            if sample is not None:
                samples.append(sample)
            if ended:
                self._finish_stream()
                break
        return samples

    def stop_stream(
        self,
        *,
        timeout_s: float = 2.0,
        raise_on_timeout: bool = True,
    ) -> StreamDiagnostics:
        """Stop streaming, drain queued D records, and reconcile END count."""

        diagnostics = self._stream_diagnostics
        if diagnostics is None:
            raise StreamStateError("No waveform stream has been started.")
        if not self._stream_active:
            return diagnostics

        self._send("STREAM,STOP")
        timeout_s = max(0.01, float(timeout_s))
        deadline = self._monotonic() + timeout_s
        max_empty_reads = max(2, int(math.ceil(timeout_s / self.read_timeout_s)) + 2)
        empty_reads = 0
        while self._monotonic() < deadline and empty_reads < max_empty_reads:
            line = self._readline()
            if not line:
                empty_reads += 1
                continue
            empty_reads = 0
            sample, ended = self._consume_stream_line(line, drained=True)
            if sample is not None:
                self._drained_samples.append(sample)
            if ended:
                break
        self._finish_stream()
        if not diagnostics.stop_marker_seen and raise_on_timeout:
            raise StreamTimeoutError(
                "Timed out draining the ESP32 stream before STREAM,END; "
                f"received {diagnostics.received_samples} records."
            )
        return diagnostics


# A descriptive alias for GUI code; Esp32Rig remains compatible with the
# command-line wrapper's established name.
Esp32EmitterRig = Esp32Rig


def probe_esp32_status(port: str | None = None) -> tuple[bool, str]:
    """Probe one rig for the GUI's hardware-status banner.

    Expected hardware and dependency failures are returned as text rather than
    raised, matching the v4 ``probe_labjack_status`` contract.  The temporary
    connection is always closed with PWM disabled.
    """

    rig = Esp32Rig(port)
    try:
        rig.connect()
        identity = rig.identity
        identity_text = "Eltec ESP32"
        if identity is not None:
            identity_text = identity.text
        port_text = rig.port_name or port or "auto-detected port"
        return True, f"{identity_text} connected on {port_text}."
    except Esp32BackendError as exc:
        return False, str(exc)
    except Exception as exc:  # Serial drivers may expose platform-specific errors.
        return False, f"ESP32 probe failed: {exc}"
    finally:
        rig.close()


__all__: Sequence[str] = (
    "BAUD_RATE",
    "SAMPLE_RATE_HZ",
    "PWM_FREQUENCY_HZ",
    "PWM_DUTY_CYCLE_PERCENT",
    "PWM_GPIO",
    "MINIMUM_FIRMWARE_VERSION",
    "EXPECTED_FIRMWARE_PREFIX",
    "KNOWN_USB_IDS",
    "Esp32BackendError",
    "PySerialUnavailableError",
    "Esp32NotFoundError",
    "Esp32ConnectionError",
    "Esp32ProtocolError",
    "UnsupportedFirmwareError",
    "StreamStateError",
    "StreamTimeoutError",
    "PortCandidate",
    "FirmwareIdentity",
    "StreamHeader",
    "StreamSample",
    "StreamDiagnostics",
    "discover_candidate_ports",
    "find_port",
    "probe_esp32_status",
    "Esp32Rig",
    "Esp32EmitterRig",
)
