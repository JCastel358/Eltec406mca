"""DAQ backend for the Eltec 50-position array rig - model 40623 (TP120) build.

This module is the small hardware boundary of the array tester, the
counterpart of ``esp32_backend.py`` in the single-detector rig. It
intentionally contains no Tkinter dependency; NumPy is used only for the
sample buffers. Importing it is safe on a machine without the ACCES driver -
an actionable ``DaqLibraryUnavailableError`` is raised only when real
hardware is requested.

Hardware
--------
ACCES I/O USB-AIO16-64MA (sold enclosed as the DPK-AIO16-64MA "DAQ-PACK"):
one 16-bit SAR ADC behind two multiplexer stages, 64 single-ended inputs,
500 kS/s aggregate, driven through the vendor's ``AIOUSB.dll`` (cdecl, every
call returns a Win32 status, 0 = success). The 50 detector positions of the
array PCB (5 rows x 10 columns, one unity-gain buffer per position) feed
single-ended inputs CH0-CH49 in row order: row 1 = CH0-CH9 ... row 5 =
CH40-CH49. TP120 labels a position ``row-col`` (``1-3`` = row 1, part 3), so
that is the label used everywhere in this rig.

Facts that shape the code (datasheet + DAQ-PACK guide in
``docs/daq_usb_aio16_64ma/``, verified on the bench laptop 2026-09-02):

* The input range is set per GROUP OF FOUR channels (config bytes 0x00-0x0F),
  the scan list is a single contiguous ``start..end`` range, and the 21-byte
  configuration block is written whole with ``ADC_SetConfig``.
* Hardware "oversample" = extra conversions per channel per scan, taken
  back-to-back before the multiplexer moves on. In bulk (streamed) mode they
  arrive RAW over USB - the host averages them. The first conversion after a
  multiplexer hop is the one most likely to be unsettled, so the
  de-interleaver can drop the first ``drop_first`` conversions of every
  channel before averaging (``DAQ_DROP_CONVERSIONS_AFTER_MUX`` in the tester;
  proven or disproven by ``daq_bench_probe.py slots``).
* There is NO analog anti-alias filter in front of the ADC; the rig captures
  wideband at 1000 scans/s per channel and band-limits in software, exactly
  as the single-detector rig does.
* Volts are ALWAYS derived here from raw counts with our own range table,
  never taken from ``ADC_GetScanV``: a ``-HG`` high-gain factory variant
  would silently mis-scale the driver's volts, and our own scale is checked
  against a known voltage by the bench probe.
* The device loads its firmware from the host at every plug-in and
  re-enumerates, so ``connect`` retries for a few seconds.
* The DLL's immediate-read entry points (``ADC_GetScan`` / ``ADC_GetScanV``)
  REWRITE the device's configuration block: they force scan mode, clear
  the timer/external trigger bits (0x05 -> 0x04) and force oversample to
  at least 1, and never restore any of it
  (vendor source ``ADC_GetScan_Inner``; reproduced on the bench 2026-09-02:
  after three immediate reads a stream started without re-writing the block
  delivered 0 scans in 12 s). The backend therefore re-asserts its own
  block after every immediate read and again before every stream start, so
  the device is always in the configuration ``self.config`` says it is.
"""

from __future__ import annotations

import ctypes
import math
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, Sequence

import numpy as np

# ----------------------------------------------------------------------
# Fixture geometry (the array PCB wiring)
# ----------------------------------------------------------------------
ROWS = 5
COLS = 10
CHANNEL_COUNT = ROWS * COLS


def position_label(row: int, col: int) -> str:
    """TP120-style label for a 1-based ``row`` (1-5) and ``col`` (1-10)."""

    if not 1 <= row <= ROWS or not 1 <= col <= COLS:
        raise ValueError(f"Position row {row} col {col} is outside the {ROWS}x{COLS} array.")
    return f"{row}-{col}"


POSITIONS: tuple[str, ...] = tuple(
    position_label(row, col) for row in range(1, ROWS + 1) for col in range(1, COLS + 1)
)


def channel_for_position(label: str) -> int:
    """DAQ single-ended channel wired to a ``row-col`` position (row order)."""

    try:
        row_text, col_text = label.strip().split("-")
        row, col = int(row_text), int(col_text)
    except ValueError as exc:
        raise ValueError(f"Position label {label!r} is not of the form row-col.") from exc
    position_label(row, col)  # validates the ranges
    return (row - 1) * COLS + (col - 1)


def position_for_channel(channel: int) -> str:
    if not 0 <= channel < CHANNEL_COUNT:
        raise ValueError(f"Channel {channel} is not wired to a position (0-{CHANNEL_COUNT - 1}).")
    return POSITIONS[channel]


# ----------------------------------------------------------------------
# Device identity
# ----------------------------------------------------------------------
DLL_NAME = "AIOUSB.dll"
USB_VID = 0x1605
# Product ids of the 64-channel multiplexed family (DAQ-PACK M Series guide,
# table 3-1). Only 0x8145 has been seen on this bench; the others are the
# input-only and 250 kS/s siblings that share the same protocol.
KNOWN_PIDS: dict[int, str] = {
    0x8145: "USB-AIO16-64MA",
    0x8045: "USB-AI16-64MA",
    0x8146: "USB-AIO16-64ME",
    0x8046: "USB-AI16-64ME",
}
# DeviceIndex pseudo-values understood by the DLL (AIOUSB.cs).
DI_NONE = 0xFFFFFFFF
DI_FIRST = 0xFFFFFFFE
DI_ONLY = 0xFFFFFFFD
MAX_DEVICE_INDEX = 32
MAX_DEVICE_CHANNELS = 128  # ADC_GetScan/ADC_GetScanV buffers are sized for the largest product

# ----------------------------------------------------------------------
# Input ranges (ADC_SetConfig range codes, datasheet + software reference)
# ----------------------------------------------------------------------
ADC_COUNTS = 65536
DIFFERENTIAL_FLAG = 0x08
GAIN_CODE_MASK = 0x07


@dataclass(frozen=True)
class RangeSpec:
    code: int
    low_v: float
    high_v: float
    name: str

    @property
    def span_v(self) -> float:
        return self.high_v - self.low_v

    @property
    def lsb_v(self) -> float:
        return self.span_v / ADC_COUNTS


RANGE_CODES: dict[int, RangeSpec] = {
    0: RangeSpec(0, 0.0, 10.0, "0-10 V"),
    1: RangeSpec(1, -10.0, 10.0, "+/-10 V"),
    2: RangeSpec(2, 0.0, 5.0, "0-5 V"),
    3: RangeSpec(3, -5.0, 5.0, "+/-5 V"),
    4: RangeSpec(4, 0.0, 2.0, "0-2 V"),
    5: RangeSpec(5, -2.0, 2.0, "+/-2 V"),
    6: RangeSpec(6, 0.0, 1.0, "0-1 V"),
    7: RangeSpec(7, -1.0, 1.0, "+/-1 V"),
}


def range_spec(code: int) -> RangeSpec:
    """Range for a config byte; the differential flag (bit 3) is stripped."""

    if not 0 <= code <= 0x0F:
        raise ValueError(f"Range code {code} is outside 0-15.")
    return RANGE_CODES[code & GAIN_CODE_MASK]


def lsb_volts(code: int) -> float:
    return range_spec(code).lsb_v


def counts_to_volts(counts: Any, range_code: int) -> Any:
    """Own-scale conversion of raw 16-bit counts (scalar or array) to volts.

    ``low + counts * span / 65536`` - the straight-binary transfer function
    of the standard-gain board. Deliberately not ``ADC_GetScanV``: see the
    module docstring (possible -HG variant).
    """

    spec = range_spec(range_code)
    if isinstance(counts, np.ndarray):
        return spec.low_v + counts.astype(np.float64) * (spec.span_v / ADC_COUNTS)
    return spec.low_v + float(counts) * (spec.span_v / ADC_COUNTS)


def volts_to_counts(volts: Any, range_code: int) -> Any:
    """Inverse of ``counts_to_volts`` (clipped to the 16-bit range); simulator use."""

    spec = range_spec(range_code)
    scaled = (np.asarray(volts, dtype=np.float64) - spec.low_v) * (ADC_COUNTS / spec.span_v)
    clipped = np.clip(np.rint(scaled), 0, ADC_COUNTS - 1)
    if np.ndim(volts) == 0:
        return int(clipped)
    return clipped.astype(np.uint16)


# ----------------------------------------------------------------------
# The 21-byte ADC configuration block
# ----------------------------------------------------------------------
CONFIG_BLOCK_SIZE = 21
RANGE_GROUPS = 16
CHANNELS_PER_RANGE_GROUP = 4  # "64M" boards: config byte n sets channels 4n..4n+3
MAX_SCAN_CHANNEL = 63
MAX_OVERSAMPLE = 255

CAL_NORMAL = 0x00           # ADC reads the external pins
CAL_GROUND = 0x01           # onboard unipolar ground reference (expects 0 V)
CAL_REF_UNIPOLAR = 0x03     # onboard unipolar full-scale reference (0.90909 V at 0-10 V)
CAL_BIPOLAR_ZERO = 0x05     # onboard bipolar zero (0 V)
CAL_BIPOLAR_REF = 0x07      # onboard bipolar full-scale reference
CAL_CODES = (CAL_NORMAL, CAL_GROUND, CAL_REF_UNIPOLAR, CAL_BIPOLAR_ZERO, CAL_BIPOLAR_REF)

TRIGGER_TIMER = 0x01        # the onboard 8254 pacing clock triggers acquisition
TRIGGER_EXTERNAL = 0x02     # the external A/D trigger pin triggers acquisition
TRIGGER_SCAN = 0x04         # one trigger = the whole start..end scan (with oversamples)
TRIGGER_FALLING = 0x08      # trigger on the falling edge instead of rising
TRIGGER_CTR0_EXT = 0x10     # counter 0 clocked externally instead of the 10 MHz clock

_OFFSET_CAL = 0x10
_OFFSET_TRIGGER = 0x11
_OFFSET_SCAN_LOW = 0x12
_OFFSET_OVERSAMPLE = 0x13
_OFFSET_SCAN_HIGH = 0x14


@dataclass(frozen=True)
class AdcConfig:
    """One complete ADC configuration (what ``ADC_SetConfig`` writes).

    The production values are set by the tester (``DAQ_RANGE_CODE`` etc.);
    the defaults here are those values so the probe and tests agree.
    """

    range_code: int = 2                                # 0-5 V on every group
    start_channel: int = 0
    end_channel: int = CHANNEL_COUNT - 1               # CH0-CH49
    oversample: int = 3                                # 4 conversions per channel per scan
    trigger: int = TRIGGER_TIMER | TRIGGER_SCAN        # 0x05: timer-paced whole scans
    cal_code: int = CAL_NORMAL

    def __post_init__(self) -> None:
        if not 0 <= self.range_code <= 0x0F:
            raise ValueError(f"range_code {self.range_code} is outside 0-15.")
        if not 0 <= self.start_channel <= MAX_SCAN_CHANNEL:
            raise ValueError(f"start_channel {self.start_channel} is outside 0-{MAX_SCAN_CHANNEL}.")
        if not 0 <= self.end_channel <= MAX_SCAN_CHANNEL:
            raise ValueError(f"end_channel {self.end_channel} is outside 0-{MAX_SCAN_CHANNEL}.")
        if self.end_channel < self.start_channel:
            raise ValueError(
                f"end_channel {self.end_channel} is before start_channel {self.start_channel}."
            )
        if not 0 <= self.oversample <= MAX_OVERSAMPLE:
            raise ValueError(f"oversample {self.oversample} is outside 0-{MAX_OVERSAMPLE}.")
        if not 0 <= self.trigger <= 0xFF:
            raise ValueError(f"trigger byte {self.trigger:#x} is outside 0-255.")
        if self.cal_code not in CAL_CODES:
            raise ValueError(f"cal_code {self.cal_code:#x} is not one of {CAL_CODES}.")

    @property
    def channels(self) -> int:
        return self.end_channel - self.start_channel + 1

    @property
    def conversions_per_channel(self) -> int:
        return self.oversample + 1

    @property
    def scan_bytes(self) -> int:
        """Bytes per scan in bulk mode: every conversion is one uint16."""

        return self.channels * self.conversions_per_channel * 2

    @property
    def conversions_per_second(self) -> float:
        """Aggregate ADC load at 1 scan/s; multiply by the scan rate."""

        return float(self.channels * self.conversions_per_channel)


def build_config_block(config: AdcConfig) -> bytes:
    block = bytearray(CONFIG_BLOCK_SIZE)
    for group in range(RANGE_GROUPS):
        block[group] = config.range_code
    block[_OFFSET_CAL] = config.cal_code
    block[_OFFSET_TRIGGER] = config.trigger
    block[_OFFSET_SCAN_LOW] = ((config.end_channel & 0x0F) << 4) | (config.start_channel & 0x0F)
    block[_OFFSET_OVERSAMPLE] = config.oversample
    block[_OFFSET_SCAN_HIGH] = (config.end_channel & 0xF0) | ((config.start_channel >> 4) & 0x0F)
    return bytes(block)


def parse_config_block(block: bytes) -> AdcConfig:
    if len(block) != CONFIG_BLOCK_SIZE:
        raise ValueError(f"Config block is {len(block)} bytes; expected {CONFIG_BLOCK_SIZE}.")
    codes = set(block[:RANGE_GROUPS])
    if len(codes) != 1:
        raise ValueError(
            "Config block has mixed range codes per group "
            f"({sorted(codes)}); this rig uses one range on every group."
        )
    low, high = block[_OFFSET_SCAN_LOW], block[_OFFSET_SCAN_HIGH]
    start = (low & 0x0F) | ((high & 0x0F) << 4)
    end = (low >> 4) | (high & 0xF0)
    return AdcConfig(
        range_code=block[0],
        start_channel=start,
        end_channel=end,
        oversample=block[_OFFSET_OVERSAMPLE],
        trigger=block[_OFFSET_TRIGGER],
        cal_code=block[_OFFSET_CAL],
    )


# ----------------------------------------------------------------------
# Errors (mirroring the single rig's backend hierarchy)
# ----------------------------------------------------------------------
class DaqError(RuntimeError):
    """Base class for array-rig DAQ failures."""


class DaqLibraryUnavailableError(DaqError):
    """AIOUSB.dll could not be loaded (driver package not installed, or a 32/64-bit mismatch)."""


class DaqNotFoundError(DaqError):
    """No ACCES device answered within the connect timeout."""


class DaqStatusError(DaqError):
    """A DLL call returned a non-zero Win32 status."""

    def __init__(self, function: str, status: int) -> None:
        super().__init__(f"{function} failed with Win32 status {status}.")
        self.function = function
        self.status = status


class StreamStateError(DaqError):
    """A stream call was made in the wrong state (not configured / already streaming)."""


class StreamTimeoutError(DaqError):
    """The stream produced no data within the caller's timeout."""


class StreamIntegrityError(DaqError):
    """The stream lost data or ran at the wrong rate; the capture must be retried."""


def check_status(function: str, status: int) -> None:
    if int(status) != 0:
        raise DaqStatusError(function, int(status))


def device_indices_from_mask(mask: int) -> tuple[int, ...]:
    """``GetDevices()`` returns a bit mask: bit n set = device index n present."""

    return tuple(index for index in range(MAX_DEVICE_INDEX) if (mask >> index) & 1)


# ----------------------------------------------------------------------
# Byte stream -> scans
# ----------------------------------------------------------------------
class ScanDeinterleaver:
    """Turn the raw bulk byte stream into ``[scans, channels]`` averaged counts.

    Bulk-mode data is a flat little-endian uint16 stream ordered scan by
    scan, channel by channel, conversion by conversion:
    ``[s0c0v0 s0c0v1 .. s0c0vK s0c1v0 .. s0cNvK s1c0v0 ...]``. Callback
    buffers do not align to scan boundaries, so a partial scan is carried
    over to the next ``feed``. With ``average=False`` the full
    ``[scans, channels, conversions]`` array is returned instead (bench
    probe use: it is how the "drop the first conversion" rule is verified).
    """

    def __init__(
        self,
        channels: int,
        conversions_per_channel: int,
        *,
        drop_first: int = 1,
        average: bool = True,
    ) -> None:
        if channels < 1:
            raise ValueError("channels must be >= 1.")
        if conversions_per_channel < 1:
            raise ValueError("conversions_per_channel must be >= 1.")
        if not 0 <= drop_first < conversions_per_channel:
            raise ValueError(
                f"drop_first {drop_first} must be < conversions_per_channel "
                f"{conversions_per_channel} (at least one conversion must survive)."
            )
        self.channels = channels
        self.conversions_per_channel = conversions_per_channel
        self.drop_first = drop_first
        self.average = average
        self.scan_bytes = channels * conversions_per_channel * 2
        self._pending = bytearray()
        self.scans_emitted = 0

    @property
    def pending_bytes(self) -> int:
        return len(self._pending)

    def feed(self, data: bytes | bytearray | memoryview) -> np.ndarray:
        if self._pending:
            self._pending.extend(data)
            buffer: bytes | bytearray = self._pending
        else:
            buffer = data if isinstance(data, (bytes, bytearray)) else bytes(data)
        scans = len(buffer) // self.scan_bytes
        usable = scans * self.scan_bytes
        block = np.frombuffer(bytes(buffer[:usable]), dtype="<u2")
        block = block.reshape(scans, self.channels, self.conversions_per_channel)
        remainder = bytearray(buffer[usable:])
        self._pending = remainder
        self.scans_emitted += scans
        if not self.average:
            return block.astype(np.float64)
        kept = block[:, :, self.drop_first:]
        return kept.mean(axis=2, dtype=np.float64)


def deinterleave_scans(
    data: bytes,
    channels: int,
    conversions_per_channel: int,
    *,
    drop_first: int = 1,
    average: bool = True,
) -> tuple[np.ndarray, bytes]:
    """One-shot helper: ``(scans array, leftover bytes)``."""

    worker = ScanDeinterleaver(
        channels, conversions_per_channel, drop_first=drop_first, average=average
    )
    scans = worker.feed(data)
    return scans, bytes(worker._pending)


# ----------------------------------------------------------------------
# Records shared by the real and simulated devices
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class DaqInfo:
    name: str
    serial_number: str
    device_index: int
    product_id: int
    dll_version: str
    simulated: bool = False
    calibration_supported: bool | None = None

    def summary(self) -> str:
        kind = "SIMULATED " if self.simulated else ""
        return f"{kind}{self.name} (PID {self.product_id:#06x}, serial {self.serial_number}, DLL {self.dll_version})"


@dataclass(frozen=True)
class StreamHeader:
    scan_hz: float
    actual_timer_hz: float
    channels: int
    config: AdcConfig
    started_monotonic: float


@dataclass
class StreamDiagnostics:
    """Integrity and timing information for one bulk stream.

    The single-detector rig judges a serial stream by timestamp gaps; the DAQ
    stream has no per-sample timestamps, so the rate check compares the
    number of complete scans delivered with the pacing clock's nominal rate
    over the wall-clock stream duration. ``pool_too_small_events`` counts the
    driver's "buffer pool exhausted" flag - each one means data was lost
    while the host stalled (the DAQ analogue of the CP210x RX overflow).
    """

    nominal_scan_hz: float
    actual_timer_hz: float
    started_monotonic: float
    stopped_monotonic: float | None = None
    scans_received: int = 0
    buffers_received: int = 0
    bytes_received: int = 0
    leftover_bytes: int = 0
    pool_too_small_events: int = 0
    #: the driver's end-of-stream flag was seen (it always is after a normal
    #: ``ADC_BulkContinuousEnd``: the DLL delivers one last END-flagged buffer)
    ended_by_device: bool = False
    #: the END flag arrived BEFORE ``stop_stream`` asked for it - the device or
    #: driver ended the stream on its own, so the record is incomplete
    ended_early: bool = False
    callback_error: str | None = None
    consumer_cpu_s: float | None = None

    @property
    def elapsed_s(self) -> float | None:
        if self.stopped_monotonic is None:
            return None
        return self.stopped_monotonic - self.started_monotonic

    @property
    def received_rate_hz(self) -> float | None:
        elapsed = self.elapsed_s
        if elapsed is None or elapsed <= 0:
            return None
        return self.scans_received / elapsed

    @property
    def rate_error_fraction(self) -> float | None:
        received = self.received_rate_hz
        if received is None or self.nominal_scan_hz <= 0:
            return None
        return (received - self.nominal_scan_hz) / self.nominal_scan_hz

    def problems(self, *, max_rate_error: float = 0.01) -> list[str]:
        problems: list[str] = []
        if self.callback_error:
            problems.append(f"callback error: {self.callback_error}")
        if self.pool_too_small_events:
            # The DLL flags a buffer it had to INSERT because no blank pool
            # buffer was free: the host fell behind the stream. Data is only
            # lost if the device FIFO overflowed meanwhile, which cannot be
            # told apart from here - so the capture is retried regardless.
            problems.append(
                f"driver buffer pool exhausted {self.pool_too_small_events} time(s) (extra buffer inserted: "
                "the host fell behind the stream)"
            )
        if self.ended_early:
            problems.append("the device ended the stream before it was stopped - the record is incomplete")
        if self.scans_received == 0:
            problems.append("no complete scans were received")
        error = self.rate_error_fraction
        if error is not None and abs(error) > max_rate_error:
            problems.append(
                f"scan rate {self.received_rate_hz:.1f}/s differs from the nominal "
                f"{self.nominal_scan_hz:.1f}/s by {100.0 * error:+.2f}% (limit {100.0 * max_rate_error:.1f}%)"
            )
        timer_error = (self.actual_timer_hz - self.nominal_scan_hz) / self.nominal_scan_hz if self.nominal_scan_hz else 0.0
        if abs(timer_error) > max_rate_error:
            problems.append(
                f"pacing clock granted {self.actual_timer_hz:.3f} Hz for a nominal {self.nominal_scan_hz:.1f} Hz"
            )
        return problems

    def check(self, *, max_rate_error: float = 0.01) -> None:
        problems = self.problems(max_rate_error=max_rate_error)
        if problems:
            raise StreamIntegrityError("; ".join(problems))

    def summary(self) -> str:
        rate = self.received_rate_hz
        rate_text = "n/a" if rate is None else f"{rate:.2f} scans/s"
        return (
            f"{self.scans_received} scans in {self.elapsed_s or 0.0:.2f} s ({rate_text}, timer {self.actual_timer_hz:.3f} Hz), "
            f"{self.buffers_received} buffers, {self.leftover_bytes} leftover bytes, "
            f"{self.pool_too_small_events} pool events"
            + (", ended by device before stop" if self.ended_early else "")
            + (f", callback error: {self.callback_error}" if self.callback_error else "")
        )


class DaqDevice(Protocol):
    """The duck-typed device surface the tester, harness and probe rely on."""

    info: DaqInfo | None
    config: AdcConfig | None

    @property
    def is_streaming(self) -> bool: ...

    def connect(self, *, timeout_s: float = 10.0) -> DaqInfo: ...

    def close(self) -> None: ...

    def configure(self, config: AdcConfig) -> AdcConfig: ...

    def self_calibrate(self) -> None: ...

    def set_cal_mode(self, cal_code: int) -> None: ...

    def read_scan_counts(self, *, reads: int = 1) -> np.ndarray: ...

    def read_scan_volts_median(self, *, reads: int = 3) -> np.ndarray: ...

    def start_stream(
        self,
        *,
        scan_hz: float,
        buffer_bytes: int = 64_000,
        buffer_count: int = 32,
        drop_first: int = 1,
        average: bool = True,
    ) -> StreamHeader: ...

    def read_stream(self, *, timeout_s: float = 1.0) -> np.ndarray | None: ...

    def stop_stream(self, *, timeout_s: float = 2.0) -> StreamDiagnostics: ...


# ----------------------------------------------------------------------
# The real device: AIOUSB.dll through ctypes
# ----------------------------------------------------------------------
_UL = ctypes.c_ulong  # "unsigned long" is 32-bit on Win64 - matches the DLL
ADCallback = ctypes.CFUNCTYPE(
    None, ctypes.POINTER(ctypes.c_ushort), ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong
)
CALLBACK_FLAG_END_OF_STREAM = 0x02
CALLBACK_FLAG_POOL_TOO_SMALL = 0x04
STREAM_BLOCK_GRANULE_BYTES = 512  # BufSize must be a multiple of 512 (software reference)
CONNECT_RETRY_S = 0.5

# Prototypes of every DLL entry point this backend calls (from the shipped
# AIOUSB.cs and the USB Software Reference Manual; all cdecl).
_SIGNATURES: dict[str, tuple[list[Any], Any]] = {
    "GetDevices": ([], _UL),
    "QueryDeviceInfo": (
        [_UL, ctypes.POINTER(_UL), ctypes.POINTER(_UL), ctypes.c_char_p, ctypes.POINTER(_UL), ctypes.POINTER(_UL)],
        _UL,
    ),
    "GetDeviceSerialNumber": ([_UL, ctypes.POINTER(ctypes.c_uint64)], _UL),
    "AIOUSB_ReloadDeviceLinks": ([], _UL),
    "ADC_SetConfig": ([_UL, ctypes.c_void_p, ctypes.POINTER(_UL)], _UL),
    "ADC_GetConfig": ([_UL, ctypes.c_void_p, ctypes.POINTER(_UL)], _UL),
    "ADC_SetCal": ([_UL, ctypes.c_char_p], _UL),
    "ADC_QueryCal": ([_UL], _UL),
    "ADC_GetScan": ([_UL, ctypes.POINTER(ctypes.c_ushort)], _UL),
    "ADC_GetScanV": ([_UL, ctypes.POINTER(ctypes.c_double)], _UL),
    "CTR_StartOutputFreq": ([_UL, _UL, ctypes.POINTER(ctypes.c_double)], _UL),
    "ADC_BulkContinuousCallbackStart": ([_UL, _UL, _UL, _UL, ADCallback], _UL),
    "ADC_BulkContinuousEnd": ([_UL, ctypes.POINTER(_UL)], _UL),
    "ADC_BulkAcquire": ([_UL, _UL, ctypes.c_void_p], _UL),
    "ADC_BulkPoll": ([_UL, ctypes.POINTER(_UL)], _UL),
}


def load_library(name: str = DLL_NAME) -> Any:
    """Load AIOUSB.dll (cdecl) and declare every prototype in ``_SIGNATURES``."""

    try:
        library = ctypes.CDLL(name)
    except OSError as exc:
        raise DaqLibraryUnavailableError(
            f"Could not load {name}: {exc}. Install the ACCES 'USB-AIO16-64MA Install' package "
            "and use a 64-bit Python (the 64-bit DLL lives in System32)."
        ) from exc
    bind_signatures(library)
    return library


def bind_signatures(library: Any) -> None:
    for name, (argtypes, restype) in _SIGNATURES.items():
        try:
            function = getattr(library, name)
        except AttributeError as exc:
            raise DaqLibraryUnavailableError(f"{DLL_NAME} has no entry point {name}.") from exc
        function.argtypes = argtypes
        function.restype = restype


def dll_version_text(name: str = DLL_NAME) -> str:
    """File version of the loaded DLL (Windows only; 'unknown' elsewhere)."""

    try:
        import ctypes.wintypes  # noqa: F401  (Windows only)

        version_dll = ctypes.windll.version  # type: ignore[attr-defined]
    except (AttributeError, ImportError):  # pragma: no cover - non-Windows
        return "unknown"
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        handle = kernel32.GetModuleHandleW(name)
        if not handle:
            return "unknown"
        buffer = ctypes.create_unicode_buffer(1024)
        kernel32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        if not kernel32.GetModuleFileNameW(handle, buffer, 1024):
            return "unknown"
        path = buffer.value
        size = version_dll.GetFileVersionInfoSizeW(path, None)
        if not size:
            return "unknown"
        data = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(path, 0, size, data):
            return "unknown"
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not version_dll.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return "unknown"
        fixed = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        ms, ls = fixed[2], fixed[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # pragma: no cover - best effort only
        return "unknown"


class AiousbDaq:
    """``DaqDevice`` implementation over the vendor DLL.

    Threading: the DLL invokes ``_on_buffer`` on its own thread. That
    callback only copies the buffer and enqueues it (never touches the DLL,
    never raises - ctypes would swallow the exception silently, so it is
    recorded instead). A daemon consumer thread de-interleaves the raw bytes
    into scan arrays for ``read_stream``. Keeping ``self._callback``
    referenced for the stream lifetime is mandatory: a garbage-collected
    CFUNCTYPE object is a crash in the DLL's thread.
    """

    def __init__(
        self,
        *,
        dll_name: str = DLL_NAME,
        device_index: int | None = None,
        library: Any | None = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self._dll_name = dll_name
        self._requested_index = device_index
        self._lib = library
        if library is not None:
            bind_signatures_if_possible(library)
        self._sleep = sleep
        self._monotonic = monotonic
        self.info: DaqInfo | None = None
        self.config: AdcConfig | None = None
        self._index: int | None = None
        self._streaming = False
        self._callback: Any = None
        self._raw_queue: queue.Queue[tuple[bytes, int, bool]] | None = None
        self._out_queue: queue.Queue[np.ndarray] | None = None
        self._consumer: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._diagnostics: StreamDiagnostics | None = None
        self._deinterleaver: ScanDeinterleaver | None = None
        self._callback_error: str | None = None
        self._stop_requested = False
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------
    @property
    def is_streaming(self) -> bool:
        return self._streaming

    @property
    def device_index(self) -> int:
        if self._index is None:
            raise DaqNotFoundError("Not connected - call connect() first.")
        return self._index

    def connect(self, *, timeout_s: float = 10.0) -> DaqInfo:
        if self._lib is None:
            self._lib = load_library(self._dll_name)
        deadline = self._monotonic() + timeout_s
        attempts = 0
        while True:
            attempts += 1
            mask = int(self._lib.GetDevices())
            indices = device_indices_from_mask(mask)
            if self._requested_index is not None:
                indices = tuple(index for index in indices if index == self._requested_index)
            if indices:
                self._index = indices[0]
                break
            if self._monotonic() >= deadline:
                raise DaqNotFoundError(
                    f"No ACCES device found after {attempts} attempt(s) over {timeout_s:.0f} s "
                    "(the device loads its firmware from the host and re-enumerates after plug-in; "
                    "check the USB cable and Device Manager for 'ACCES USB-AIO16-64MA')."
                )
            # The DLL caches the device links it saw at load; re-scan while the
            # freshly plugged unit finishes its firmware download.
            self._lib.AIOUSB_ReloadDeviceLinks()
            self._sleep(CONNECT_RETRY_S)
        pid = _UL(0)
        name_size = _UL(63)
        name_buffer = ctypes.create_string_buffer(64)
        dio_bytes = _UL(0)
        counters = _UL(0)
        check_status(
            "QueryDeviceInfo",
            self._lib.QueryDeviceInfo(
                self._index, ctypes.byref(pid), ctypes.byref(name_size), name_buffer,
                ctypes.byref(dio_bytes), ctypes.byref(counters),
            ),
        )
        name = name_buffer.raw[: name_size.value].decode("ascii", "replace").strip("\x00 ")
        serial = ctypes.c_uint64(0)
        serial_text = "unknown"
        if int(self._lib.GetDeviceSerialNumber(self._index, ctypes.byref(serial))) == 0:
            serial_text = f"{serial.value:016X}"
        calibration_supported = int(self._lib.ADC_QueryCal(self._index)) == 0
        self.info = DaqInfo(
            name=name or KNOWN_PIDS.get(pid.value, "ACCES USB device"),
            serial_number=serial_text,
            device_index=self._index,
            product_id=pid.value,
            dll_version=dll_version_text(self._dll_name),
            simulated=False,
            calibration_supported=calibration_supported,
        )
        return self.info

    def close(self) -> None:
        if self._streaming:
            try:
                self.stop_stream()
            except DaqError:
                pass
        self._index = None
        self.info = None

    # -- configuration -----------------------------------------------------
    def configure(self, config: AdcConfig) -> AdcConfig:
        with self._lock:
            if self._streaming:
                raise StreamStateError("Stop the stream before reconfiguring the ADC.")
            self._write_config_block(config)
            self.config = config
            return config

    def _write_config_block(self, config: AdcConfig) -> None:
        """``ADC_SetConfig`` + byte-exact read-back of ``config``."""

        block = build_config_block(config)
        size = _UL(len(block))
        buffer = ctypes.create_string_buffer(block, len(block))
        check_status("ADC_SetConfig", self._lib.ADC_SetConfig(self.device_index, buffer, ctypes.byref(size)))
        readback = self.read_config_block()
        if readback != block:
            raise DaqError(
                "ADC_GetConfig read back a different block than was written: "
                f"wrote {block.hex(' ')}, read {readback.hex(' ')}."
            )

    def _reassert_config(self) -> None:
        """Put ``self.config`` back on the device.

        ``ADC_GetScan``/``ADC_GetScanV`` rewrite the trigger byte (module
        docstring), so the block is re-written after every immediate read and
        before every stream start. Cheap (two control transfers) next to the
        immediate read itself, and the only way to keep ``self.config``
        truthful.
        """

        self._write_config_block(self._require_config())

    def _reassert_config_quietly(self) -> None:
        """``_reassert_config`` on a failure path: a second error must not mask the first."""

        try:
            self._reassert_config()
        except DaqError:
            pass

    def read_config_block(self) -> bytes:
        buffer = ctypes.create_string_buffer(64)
        size = _UL(64)
        check_status("ADC_GetConfig", self._lib.ADC_GetConfig(self.device_index, buffer, ctypes.byref(size)))
        return buffer.raw[: size.value]

    def self_calibrate(self) -> None:
        with self._lock:
            check_status("ADC_SetCal", self._lib.ADC_SetCal(self.device_index, b":AUTO:"))

    def set_cal_mode(self, cal_code: int) -> None:
        base = self.config or AdcConfig()
        self.configure(replace(base, cal_code=cal_code))

    # -- immediate reads ---------------------------------------------------
    def read_scan_counts(self, *, reads: int = 1) -> np.ndarray:
        if reads < 1:
            raise ValueError("reads must be >= 1.")
        config = self._require_config()
        with self._lock:
            if self._streaming:
                raise StreamStateError("Stop the stream before an immediate scan.")
            rows = np.empty((reads, config.channels), dtype=np.float64)
            buffer = (ctypes.c_ushort * MAX_DEVICE_CHANNELS)()
            try:
                for row in range(reads):
                    check_status("ADC_GetScan", self._lib.ADC_GetScan(self.device_index, buffer))
                    rows[row, :] = np.frombuffer(buffer, dtype=np.uint16)[
                        config.start_channel : config.end_channel + 1
                    ]
            except DaqError:
                self._reassert_config_quietly()
                raise
            self._reassert_config()  # ADC_GetScan cleared the timer-trigger bit
        return rows

    def read_scan_volts_median(self, *, reads: int = 3) -> np.ndarray:
        config = self._require_config()
        counts = self.read_scan_counts(reads=reads)
        return counts_to_volts(np.median(counts, axis=0), config.range_code)

    def read_scan_driver_volts(self) -> np.ndarray:
        """The driver's own ``ADC_GetScanV`` volts - bench probe only.

        An arithmetic cross-check of the own-scale volts, never a gain (-HG)
        check: the DLL uses the same counts x span / 65536 formula (module
        docstring), so only a known, metered voltage on an input can tell a
        high-gain unit.
        """

        config = self._require_config()
        buffer = (ctypes.c_double * MAX_DEVICE_CHANNELS)()
        with self._lock:
            if self._streaming:
                raise StreamStateError("Stop the stream before an immediate scan.")
            try:
                check_status("ADC_GetScanV", self._lib.ADC_GetScanV(self.device_index, buffer))
            except DaqError:
                self._reassert_config_quietly()
                raise
            self._reassert_config()  # same side effect as ADC_GetScan
        return np.frombuffer(buffer, dtype=np.float64)[config.start_channel : config.end_channel + 1].copy()

    # -- bulk streaming ----------------------------------------------------
    def start_stream(
        self,
        *,
        scan_hz: float,
        buffer_bytes: int = 64_000,
        buffer_count: int = 32,
        drop_first: int = 1,
        average: bool = True,
    ) -> StreamHeader:
        config = self._require_config()
        if scan_hz <= 0:
            raise ValueError("scan_hz must be positive.")
        if buffer_bytes <= 0 or buffer_bytes % STREAM_BLOCK_GRANULE_BYTES:
            raise ValueError(f"buffer_bytes must be a positive multiple of {STREAM_BLOCK_GRANULE_BYTES}.")
        if buffer_count < 2:
            raise ValueError("buffer_count must be >= 2 (the DLL needs at least two pool buffers).")
        with self._lock:
            if self._streaming:
                raise StreamStateError("A stream is already running.")
            # The block on the device may not be ours any more (an immediate
            # read clears the timer-trigger bit): re-write it, or the pacing
            # clock ticks and nothing is converted.
            self._reassert_config()
            self._deinterleaver = ScanDeinterleaver(
                config.channels, config.conversions_per_channel, drop_first=drop_first, average=average
            )
            self._raw_queue = queue.Queue()
            self._out_queue = queue.Queue()
            self._stop_event.clear()
            self._callback_error = None
            self._stop_requested = False
            self._callback = ADCallback(self._on_buffer)
            timer_hz = ctypes.c_double(float(scan_hz))
            started = self._monotonic()
            self._diagnostics = StreamDiagnostics(
                nominal_scan_hz=float(scan_hz), actual_timer_hz=float(scan_hz), started_monotonic=started
            )
            self._consumer = threading.Thread(
                target=self._consume, name="daq-stream-consumer", daemon=True
            )
            self._consumer.start()
            try:
                check_status(
                    "ADC_BulkContinuousCallbackStart",
                    self._lib.ADC_BulkContinuousCallbackStart(
                        self.device_index, buffer_bytes, buffer_count, 0, self._callback
                    ),
                )
            except DaqStatusError:
                self._stop_event.set()
                self._callback = None
                raise
            self._streaming = True
            try:
                check_status(
                    "CTR_StartOutputFreq",
                    self._lib.CTR_StartOutputFreq(self.device_index, 0, ctypes.byref(timer_hz)),
                )
            except DaqStatusError:
                self._end_bulk()
                self._streaming = False
                raise
            self._diagnostics.started_monotonic = self._monotonic()
            self._diagnostics.actual_timer_hz = float(timer_hz.value)
            return StreamHeader(
                scan_hz=float(scan_hz),
                actual_timer_hz=float(timer_hz.value),
                channels=config.channels,
                config=config,
                started_monotonic=self._diagnostics.started_monotonic,
            )

    def _on_buffer(self, pbuf: Any, bufsize: int, flags: int, context: int) -> None:
        try:
            data = ctypes.string_at(pbuf, int(bufsize)) if int(bufsize) > 0 else b""
            raw_queue = self._raw_queue
            if raw_queue is not None:
                # "Early" is decided HERE, at delivery on the DLL's thread: the
                # consumer may dequeue an END buffer only after stop_stream()
                # has set the flag, and would then miss that the device ended
                # the stream on its own.
                raw_queue.put((data, int(flags), not self._stop_requested))
        except Exception as exc:  # never let an exception escape into the DLL
            self._callback_error = repr(exc)

    def _consume(self) -> None:
        raw_queue = self._raw_queue
        out_queue = self._out_queue
        worker = self._deinterleaver
        diagnostics = self._diagnostics
        if raw_queue is None or out_queue is None or worker is None or diagnostics is None:
            return
        cpu_start = time.process_time()
        while True:
            try:
                data, flags, early = raw_queue.get(timeout=0.05)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue
            diagnostics.buffers_received += 1
            diagnostics.bytes_received += len(data)
            if flags & CALLBACK_FLAG_POOL_TOO_SMALL:
                diagnostics.pool_too_small_events += 1
            try:
                scans = worker.feed(data)
            except Exception as exc:
                self._callback_error = f"de-interleave failed: {exc!r}"
                diagnostics.callback_error = self._callback_error
                break
            if scans.shape[0]:
                diagnostics.scans_received += int(scans.shape[0])
                out_queue.put(scans)
            if flags & CALLBACK_FLAG_END_OF_STREAM:
                diagnostics.ended_by_device = True
                diagnostics.ended_early = early
                break
        diagnostics.leftover_bytes = worker.pending_bytes
        diagnostics.consumer_cpu_s = time.process_time() - cpu_start

    def read_stream(self, *, timeout_s: float = 1.0) -> np.ndarray | None:
        if not self._streaming or self._out_queue is None:
            raise StreamStateError("No stream is running.")
        if self._callback_error:
            raise StreamIntegrityError(self._callback_error)
        try:
            return self._out_queue.get(timeout=timeout_s)
        except queue.Empty:
            if self._callback_error:
                raise StreamIntegrityError(self._callback_error)
            return None

    def _end_bulk(self) -> None:
        stop_hz = ctypes.c_double(0.0)  # 0 Hz stops the pacing clock
        self._lib.CTR_StartOutputFreq(self.device_index, 0, ctypes.byref(stop_hz))
        status = _UL(0)
        check_status("ADC_BulkContinuousEnd", self._lib.ADC_BulkContinuousEnd(self.device_index, ctypes.byref(status)))

    def stop_stream(self, *, timeout_s: float = 2.0) -> StreamDiagnostics:
        with self._lock:
            if not self._streaming or self._diagnostics is None:
                raise StreamStateError("No stream is running.")
            diagnostics = self._diagnostics
            self._stop_requested = True
            try:
                self._end_bulk()
            finally:
                diagnostics.stopped_monotonic = self._monotonic()
                self._streaming = False
            # ADC_BulkContinuousEnd joins the DLL's own threads before it
            # returns, so the last (zero-size, END-flagged) buffer has already
            # been handed to the callback by now; the short sleep only lets
            # the consumer thread drain the raw queue before it is told to stop.
            self._sleep(0.1)
            self._stop_event.set()
        if self._consumer is not None:
            self._consumer.join(timeout=timeout_s)
        if self._deinterleaver is not None:
            diagnostics.leftover_bytes = self._deinterleaver.pending_bytes
        if self._callback_error and not diagnostics.callback_error:
            diagnostics.callback_error = self._callback_error
        self._callback = None  # safe only after ADC_BulkContinuousEnd returned
        return diagnostics

    def drain_stream(self) -> list[np.ndarray]:
        """Whatever scan chunks are still queued after ``stop_stream``."""

        chunks: list[np.ndarray] = []
        if self._out_queue is None:
            return chunks
        while True:
            try:
                chunks.append(self._out_queue.get_nowait())
            except queue.Empty:
                return chunks

    # -- one-shot bulk acquisition (fallback path, bench probe) ------------
    def bulk_acquire(
        self,
        *,
        scans: int,
        scan_hz: float,
        timeout_s: float,
        drop_first: int = 1,
        average: bool = True,
    ) -> tuple[np.ndarray, float]:
        """Acquire exactly ``scans`` scans with ``ADC_BulkAcquire``/``ADC_BulkPoll``.

        Returns ``(scans array, actual timer Hz)``. Used when callbacks
        misbehave on a given host; the tester's interface does not change.
        """

        config = self._require_config()
        if scans < 1:
            raise ValueError("scans must be >= 1.")
        total_bytes = scans * config.scan_bytes
        buffer = (ctypes.c_ubyte * total_bytes)()
        with self._lock:
            if self._streaming:
                raise StreamStateError("Stop the stream before a bulk acquisition.")
            self._reassert_config()  # see start_stream
            check_status("ADC_BulkAcquire", self._lib.ADC_BulkAcquire(self.device_index, total_bytes, buffer))
            timer_hz = ctypes.c_double(float(scan_hz))
            check_status("CTR_StartOutputFreq", self._lib.CTR_StartOutputFreq(self.device_index, 0, ctypes.byref(timer_hz)))
            deadline = self._monotonic() + timeout_s
            bytes_left = _UL(total_bytes)
            try:
                while True:
                    check_status("ADC_BulkPoll", self._lib.ADC_BulkPoll(self.device_index, ctypes.byref(bytes_left)))
                    if bytes_left.value == 0:
                        break
                    if self._monotonic() >= deadline:
                        raise StreamTimeoutError(
                            f"Bulk acquisition of {scans} scans did not finish within {timeout_s:.1f} s "
                            f"({bytes_left.value} bytes still outstanding)."
                        )
                    self._sleep(0.01)
            finally:
                stop_hz = ctypes.c_double(0.0)
                self._lib.CTR_StartOutputFreq(self.device_index, 0, ctypes.byref(stop_hz))
        data, leftover = deinterleave_scans(
            bytes(buffer), config.channels, config.conversions_per_channel, drop_first=drop_first, average=average
        )
        if leftover:
            raise StreamIntegrityError(f"Bulk acquisition returned {len(leftover)} stray bytes.")
        return data, float(timer_hz.value)

    def _require_config(self) -> AdcConfig:
        if self.config is None:
            raise StreamStateError("Configure the ADC (configure()) before reading it.")
        return self.config


def bind_signatures_if_possible(library: Any) -> None:
    """Bind prototypes when the injected object is a real ctypes library; test doubles are left alone."""

    if isinstance(library, ctypes.CDLL):
        bind_signatures(library)


# ----------------------------------------------------------------------
# Simulator
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class SimProfile:
    """What the simulated tray looks like, keyed by ``row-col`` position."""

    default_offset_v: float = 0.70
    offsets_v: dict[str, float] = field(default_factory=dict)
    empty_positions: frozenset[str] = frozenset()
    railed_positions: frozenset[str] = frozenset()
    dead_positions: frozenset[str] = frozenset()
    #: white-noise rms at the pin, per position (default applies elsewhere)
    default_noise_rms_uv: float = 60.0
    noise_rms_uv: dict[str, float] = field(default_factory=dict)
    #: positions with random 3 mV bursts (roughly one every two seconds)
    burst_positions: dict[str, float] = field(default_factory=dict)
    #: offsets settle UPWARD after power-on (405 lot-500 observation): each
    #: loaded position starts ``settle_drop_v`` low and recovers with this tau
    settle_drop_v: float = 0.15
    settling_tau_s: float = 8.0
    empty_v: float = 0.0
    railed_v: float = 4.995
    dead_v: float = 0.02
    #: stream attempts (1-based, counted per start_stream) that report a rate
    #: error / a pool event in their diagnostics - exercises the retry policy
    gap_on_attempts: frozenset[int] = frozenset()
    pool_events_on_attempts: frozenset[int] = frozenset()


def default_sim_profile() -> SimProfile:
    """A mixed tray: HO at 2-4, railed 3-1, LO 4-7, dead 5-2, empty 1-10 and 5-10, bursty 3-6."""

    return SimProfile(
        offsets_v={"2-4": 1.62, "4-7": 0.21},
        empty_positions=frozenset({"1-10", "5-10"}),
        railed_positions=frozenset({"3-1"}),
        dead_positions=frozenset({"5-2"}),
        burst_positions={"3-6": 0.003},
    )


class SimulatedDaq:
    """``DaqDevice`` implementation that needs no hardware.

    ``real_time=False`` (tests) advances a virtual clock by exactly the data
    produced, so a 60 s capture takes milliseconds and the diagnostics report
    a perfect rate; ``real_time=True`` (``--simulate`` in the GUI) paces the
    stream against the wall clock.
    """

    def __init__(
        self,
        profile: SimProfile | None = None,
        *,
        seed: int = 1,
        real_time: bool = False,
        chunk_scans: int = 100,
    ) -> None:
        self.profile = profile or default_sim_profile()
        self._rng = np.random.default_rng(seed)
        self.real_time = real_time
        self.chunk_scans = chunk_scans
        self.info: DaqInfo | None = None
        self.config: AdcConfig | None = None
        self._streaming = False
        self._virtual_t = 0.0
        self._power_on_monotonic = time.monotonic()
        self._stream_attempts = 0
        self._stream_scans = 0
        self._stream_hz = 0.0
        self._stream_started = 0.0
        self._stream_drop_first = 1
        self._stream_average = True
        self._diagnostics: StreamDiagnostics | None = None
        self.calibrations = 0

    # -- helpers -----------------------------------------------------------
    def _now(self) -> float:
        if self.real_time:
            return time.monotonic() - self._power_on_monotonic
        return self._virtual_t

    def _advance(self, seconds: float) -> None:
        if not self.real_time:
            self._virtual_t += seconds

    def _position_final_offsets(self) -> np.ndarray:
        profile = self.profile
        offsets = np.full(CHANNEL_COUNT, profile.default_offset_v, dtype=np.float64)
        for label, value in profile.offsets_v.items():
            offsets[channel_for_position(label)] = value
        for label in profile.dead_positions:
            offsets[channel_for_position(label)] = profile.dead_v
        for label in profile.railed_positions:
            offsets[channel_for_position(label)] = profile.railed_v
        for label in profile.empty_positions:
            offsets[channel_for_position(label)] = profile.empty_v
        return offsets

    def _loaded_mask(self) -> np.ndarray:
        mask = np.ones(CHANNEL_COUNT, dtype=bool)
        for label in self.profile.empty_positions | self.profile.dead_positions | self.profile.railed_positions:
            mask[channel_for_position(label)] = False
        return mask

    def _offsets_at(self, t: float) -> np.ndarray:
        final = self._position_final_offsets()
        settling = self.profile.settle_drop_v * math.exp(-t / self.profile.settling_tau_s)
        return final - settling * self._loaded_mask()

    def _noise_rms_v(self) -> np.ndarray:
        rms = np.full(CHANNEL_COUNT, self.profile.default_noise_rms_uv * 1e-6, dtype=np.float64)
        for label, value in self.profile.noise_rms_uv.items():
            rms[channel_for_position(label)] = value * 1e-6
        for label in self.profile.empty_positions:
            rms[channel_for_position(label)] = 2e-6
        for label in self.profile.dead_positions:
            rms[channel_for_position(label)] = 3e-6
        return rms

    def _synth(self, scans: int, scan_hz: float) -> np.ndarray:
        """``[scans, channels]`` volts for the next ``scans`` scans."""

        t0 = self._now()
        t = t0 + np.arange(scans) / scan_hz
        offsets = np.stack([self._offsets_at(ti) for ti in t[:: max(1, scans // 4)]] or [self._offsets_at(t0)])
        # a piecewise-constant settling curve is plenty for a simulator
        idx = np.minimum(np.arange(scans) // max(1, scans // 4), offsets.shape[0] - 1)
        volts = offsets[idx]
        volts = volts + self._rng.normal(0.0, 1.0, size=volts.shape) * self._noise_rms_v()
        for label, amplitude in self.profile.burst_positions.items():
            channel = channel_for_position(label)
            burst_count = self._rng.poisson(scans / (2.0 * scan_hz))
            for _ in range(burst_count):
                start = int(self._rng.integers(0, scans))
                width = int(min(scans - start, max(1, scan_hz // 10)))
                volts[start : start + width, channel] += amplitude * self._rng.uniform(0.5, 1.0)
        self._advance(scans / scan_hz)
        return volts

    # -- DaqDevice surface -------------------------------------------------
    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def connect(self, *, timeout_s: float = 10.0) -> DaqInfo:
        del timeout_s
        self.info = DaqInfo(
            name="USB-AIO16-64MA",
            serial_number="SIMULATED",
            device_index=0,
            product_id=0x8145,
            dll_version="simulated",
            simulated=True,
            calibration_supported=True,
        )
        return self.info

    def close(self) -> None:
        self._streaming = False
        self.info = None

    def configure(self, config: AdcConfig) -> AdcConfig:
        if self._streaming:
            raise StreamStateError("Stop the stream before reconfiguring the ADC.")
        # the simulator models the 50 wired positions; wider scans are zero-padded
        self.config = config
        return config

    def self_calibrate(self) -> None:
        self.calibrations += 1

    def set_cal_mode(self, cal_code: int) -> None:
        base = self.config or AdcConfig()
        self.configure(replace(base, cal_code=cal_code))

    def _volts_for_config(self, volts_50: np.ndarray) -> np.ndarray:
        config = self._require_config()
        if config.cal_code == CAL_GROUND or config.cal_code == CAL_BIPOLAR_ZERO:
            volts_50 = np.zeros_like(volts_50) + self._rng.normal(0.0, 20e-6, size=volts_50.shape)
        if config.channels == CHANNEL_COUNT and config.start_channel == 0:
            return volts_50
        padded = np.zeros(volts_50.shape[:-1] + (MAX_SCAN_CHANNEL + 1,), dtype=np.float64)
        padded[..., :CHANNEL_COUNT] = volts_50
        return padded[..., config.start_channel : config.end_channel + 1]

    def read_scan_counts(self, *, reads: int = 1) -> np.ndarray:
        config = self._require_config()
        if self._streaming:
            raise StreamStateError("Stop the stream before an immediate scan.")
        rows = []
        for _ in range(reads):
            volts = self._synth(1, 1000.0)[0]
            rows.append(volts_to_counts(self._volts_for_config(volts), config.range_code).astype(np.float64))
            self._advance(0.002)
        return np.stack(rows)

    def read_scan_volts_median(self, *, reads: int = 3) -> np.ndarray:
        config = self._require_config()
        counts = self.read_scan_counts(reads=reads)
        return counts_to_volts(np.median(counts, axis=0), config.range_code)

    def read_scan_driver_volts(self) -> np.ndarray:
        return self.read_scan_volts_median(reads=1)

    def start_stream(
        self,
        *,
        scan_hz: float,
        buffer_bytes: int = 64_000,
        buffer_count: int = 32,
        drop_first: int = 1,
        average: bool = True,
    ) -> StreamHeader:
        config = self._require_config()
        if self._streaming:
            raise StreamStateError("A stream is already running.")
        if buffer_bytes <= 0 or buffer_bytes % STREAM_BLOCK_GRANULE_BYTES:
            raise ValueError(f"buffer_bytes must be a positive multiple of {STREAM_BLOCK_GRANULE_BYTES}.")
        if buffer_count < 2:
            raise ValueError("buffer_count must be >= 2.")
        if not 0 <= drop_first < config.conversions_per_channel:
            raise ValueError("drop_first must leave at least one conversion per channel.")
        self._stream_attempts += 1
        self._stream_scans = 0
        self._stream_hz = float(scan_hz)
        self._stream_started = self._now()
        self._stream_drop_first = drop_first
        self._stream_average = average
        self._streaming = True
        self._diagnostics = StreamDiagnostics(
            nominal_scan_hz=float(scan_hz), actual_timer_hz=float(scan_hz), started_monotonic=self._stream_started
        )
        if self._stream_attempts in self.profile.pool_events_on_attempts:
            self._diagnostics.pool_too_small_events = 1
        return StreamHeader(
            scan_hz=float(scan_hz),
            actual_timer_hz=float(scan_hz),
            channels=config.channels,
            config=config,
            started_monotonic=self._stream_started,
        )

    def read_stream(self, *, timeout_s: float = 1.0) -> np.ndarray | None:
        config = self._require_config()
        if not self._streaming or self._diagnostics is None:
            raise StreamStateError("No stream is running.")
        scans = self.chunk_scans
        if self.real_time:
            due = self._stream_started + (self._stream_scans + scans) / self._stream_hz
            wait = due - self._now()
            if wait > 0:
                time.sleep(min(wait, timeout_s))
                if wait > timeout_s:
                    return None
        volts = self._volts_for_config(self._synth(scans, self._stream_hz))
        counts = volts_to_counts(volts, config.range_code).astype(np.float64)
        self._stream_scans += scans
        self._diagnostics.scans_received += scans
        self._diagnostics.buffers_received += 1
        self._diagnostics.bytes_received += scans * config.scan_bytes
        if not self._stream_average:
            conv = config.conversions_per_channel
            return np.repeat(counts[:, :, None], conv, axis=2)
        return counts

    def stop_stream(self, *, timeout_s: float = 2.0) -> StreamDiagnostics:
        del timeout_s
        if not self._streaming or self._diagnostics is None:
            raise StreamStateError("No stream is running.")
        diagnostics = self._diagnostics
        self._streaming = False
        elapsed = self._stream_scans / self._stream_hz if self._stream_hz else 0.0
        if self._stream_attempts in self.profile.gap_on_attempts:
            elapsed *= 1.05  # 5 % too slow -> fails the 1 % rate check
        diagnostics.stopped_monotonic = diagnostics.started_monotonic + elapsed
        return diagnostics

    def drain_stream(self) -> list[np.ndarray]:
        return []

    @property
    def stream_attempts(self) -> int:
        return self._stream_attempts

    def _require_config(self) -> AdcConfig:
        if self.config is None:
            raise StreamStateError("Configure the ADC (configure()) before reading it.")
        return self.config


__all__ = [
    "ADC_COUNTS", "ADCallback", "AdcConfig", "AiousbDaq", "CAL_BIPOLAR_REF", "CAL_BIPOLAR_ZERO",
    "CAL_CODES", "CAL_GROUND", "CAL_NORMAL", "CAL_REF_UNIPOLAR", "CHANNEL_COUNT", "COLS",
    "CONFIG_BLOCK_SIZE", "DIFFERENTIAL_FLAG", "DLL_NAME", "DaqDevice", "DaqError", "DaqInfo",
    "DaqLibraryUnavailableError", "DaqNotFoundError", "DaqStatusError", "KNOWN_PIDS", "POSITIONS",
    "RANGE_CODES", "ROWS", "RangeSpec", "ScanDeinterleaver", "SimProfile", "SimulatedDaq",
    "StreamDiagnostics", "StreamHeader", "StreamIntegrityError", "StreamStateError",
    "StreamTimeoutError", "TRIGGER_CTR0_EXT", "TRIGGER_EXTERNAL", "TRIGGER_FALLING", "TRIGGER_SCAN",
    "TRIGGER_TIMER", "USB_VID", "build_config_block", "channel_for_position", "check_status",
    "counts_to_volts", "default_sim_profile", "deinterleave_scans", "device_indices_from_mask",
    "lsb_volts", "load_library", "parse_config_block", "position_for_channel", "position_label",
    "range_spec", "volts_to_counts",
]
