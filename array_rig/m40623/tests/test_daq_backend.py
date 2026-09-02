"""Unit tests for the array rig's DAQ backend - no hardware, no DLL.

The real DLL is replaced by ``FakeLib``: an object with one callable per
entry point in ``daq_backend._SIGNATURES`` that scripts the answers and
records the calls. Time is a ``FakeClock`` so connect retries and stream
diagnostics are deterministic.
"""

from __future__ import annotations

import ctypes
import re
import sys
import unittest
from pathlib import Path

import numpy as np

MODEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODEL_DIR))

import daq_backend as backend  # noqa: E402


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeLib:
    """Scripted stand-in for AIOUSB.dll."""

    def __init__(self, *, device_masks=(1,), pid=0x8145, name=b"USB-AIO16-64MA", serial=0x40E68DEE0D501728):
        self.device_masks = list(device_masks)
        self.pid = pid
        self.name = name
        self.serial = serial
        self.calls: list[tuple] = []
        self.config_block = bytes(21)
        self.readback_override: bytes | None = None
        self.status_overrides: dict[str, int] = {}
        self.scan_counts = np.full(128, 32768, dtype=np.uint16)
        self.callback = None
        self.timer_hz_granted: float | None = None
        self.timer_calls: list[float] = []
        self.poll_sequence: list[int] = []
        self.bulk_buffer_fill: bytes = b""
        # The real DLL delivers one last END-flagged, zero-size buffer from
        # inside ADC_BulkContinuousEnd; opt in per test.
        self.end_delivers_end_flag = False

    def _status(self, name: str) -> int:
        return self.status_overrides.get(name, 0)

    def GetDevices(self):
        self.calls.append(("GetDevices",))
        if len(self.device_masks) > 1:
            return self.device_masks.pop(0)
        return self.device_masks[0]

    def AIOUSB_ReloadDeviceLinks(self):
        self.calls.append(("AIOUSB_ReloadDeviceLinks",))
        return 0

    def QueryDeviceInfo(self, index, pid, name_size, name_buf, dio, counters):
        self.calls.append(("QueryDeviceInfo", index))
        pid._obj.value = self.pid
        name_size._obj.value = len(self.name)
        name_buf.raw = self.name.ljust(64, b"\x00")
        dio._obj.value = 2
        counters._obj.value = 1
        return self._status("QueryDeviceInfo")

    def GetDeviceSerialNumber(self, index, serial):
        self.calls.append(("GetDeviceSerialNumber", index))
        serial._obj.value = self.serial
        return self._status("GetDeviceSerialNumber")

    def ADC_QueryCal(self, index):
        self.calls.append(("ADC_QueryCal", index))
        return self._status("ADC_QueryCal")

    def ADC_SetConfig(self, index, buf, size):
        block = ctypes.string_at(buf, size._obj.value)
        self.calls.append(("ADC_SetConfig", index, block))
        self.config_block = block
        return self._status("ADC_SetConfig")

    def ADC_GetConfig(self, index, buf, size):
        self.calls.append(("ADC_GetConfig", index))
        block = self.readback_override if self.readback_override is not None else self.config_block
        ctypes.memmove(buf, block, len(block))
        size._obj.value = len(block)
        return self._status("ADC_GetConfig")

    def ADC_SetCal(self, index, name):
        self.calls.append(("ADC_SetCal", index, name))
        return self._status("ADC_SetCal")

    def _immediate_read_side_effect(self) -> None:
        """What the vendor DLL really does (``ADC_GetScan_Inner``): it forces
        scan mode and clears the timer/external trigger bits of the device's
        configuration block - 0x05 becomes 0x04 - and never restores them.
        Bench-verified 2026-09-02; without the backend's re-assert a later
        stream is paced but converts nothing."""

        block = bytearray(self.config_block)
        if len(block) > 0x13:
            block[0x11] = 0x04 | (block[0x11] & 0xFC)
            block[0x13] = max(1, block[0x13])   # "oversample at least +1" - the same routine
            self.config_block = bytes(block)

    def ADC_GetScan(self, index, buf):
        self.calls.append(("ADC_GetScan", index))
        self._immediate_read_side_effect()
        for i, value in enumerate(self.scan_counts):
            buf[i] = int(value)
        return self._status("ADC_GetScan")

    def ADC_GetScanV(self, index, buf):
        self.calls.append(("ADC_GetScanV", index))
        self._immediate_read_side_effect()
        for i in range(128):
            buf[i] = float(self.scan_counts[i]) / 65536.0 * 5.0
        return self._status("ADC_GetScanV")

    def CTR_StartOutputFreq(self, index, block, hz):
        requested = hz._obj.value
        self.timer_calls.append(requested)
        self.calls.append(("CTR_StartOutputFreq", index, block, requested))
        if requested > 0 and self.timer_hz_granted is not None:
            hz._obj.value = self.timer_hz_granted
        return self._status("CTR_StartOutputFreq")

    def ADC_BulkContinuousCallbackStart(self, index, bufsize, count, context, callback):
        self.calls.append(("ADC_BulkContinuousCallbackStart", index, bufsize, count, context))
        self.callback = callback
        return self._status("ADC_BulkContinuousCallbackStart")

    def ADC_BulkContinuousEnd(self, index, status):
        self.calls.append(("ADC_BulkContinuousEnd", index))
        if self.end_delivers_end_flag and self.callback is not None:
            self.deliver(b"", flags=backend.CALLBACK_FLAG_END_OF_STREAM)
        return self._status("ADC_BulkContinuousEnd")

    def ADC_BulkAcquire(self, index, size, buf):
        self.calls.append(("ADC_BulkAcquire", index, size))
        ctypes.memmove(buf, self.bulk_buffer_fill[:size], min(size, len(self.bulk_buffer_fill)))
        return self._status("ADC_BulkAcquire")

    def ADC_BulkPoll(self, index, left):
        self.calls.append(("ADC_BulkPoll", index))
        left._obj.value = self.poll_sequence.pop(0) if self.poll_sequence else 0
        return self._status("ADC_BulkPoll")

    # convenience for tests
    def deliver(self, data: bytes, flags: int = 0) -> None:
        """Invoke the registered callback the way the DLL would."""

        assert self.callback is not None, "no stream running"
        buffer = (ctypes.c_ushort * max(1, len(data) // 2))()
        ctypes.memmove(buffer, data, len(data))
        self.callback(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort)), len(data), flags, 0)


def make_device(lib: FakeLib | None = None, clock: FakeClock | None = None):
    lib = lib or FakeLib()
    clock = clock or FakeClock()
    device = backend.AiousbDaq(library=lib, sleep=clock.sleep, monotonic=clock.monotonic)
    return device, lib, clock


def scan_bytes_for(scans: int, channels: int, conversions: int, base: int = 0) -> tuple[bytes, np.ndarray]:
    """Deterministic uint16 stream plus the expected drop-first averaged array."""

    values = (base + np.arange(scans * channels * conversions)) % 65536
    data = values.astype("<u2").tobytes()
    cube = values.reshape(scans, channels, conversions).astype(np.float64)
    return data, cube[:, :, 1:].mean(axis=2)


# ----------------------------------------------------------------------
# Geometry and scaling
# ----------------------------------------------------------------------
class GeometryTests(unittest.TestCase):
    def test_positions_are_row_major_tp120_labels(self):
        self.assertEqual(len(backend.POSITIONS), 50)
        self.assertEqual(backend.POSITIONS[0], "1-1")
        self.assertEqual(backend.POSITIONS[9], "1-10")
        self.assertEqual(backend.POSITIONS[10], "2-1")
        self.assertEqual(backend.POSITIONS[-1], "5-10")

    def test_channel_for_position_and_back(self):
        self.assertEqual(backend.channel_for_position("1-1"), 0)
        self.assertEqual(backend.channel_for_position("1-3"), 2)
        self.assertEqual(backend.channel_for_position("2-1"), 10)
        self.assertEqual(backend.channel_for_position("5-10"), 49)
        for channel in range(50):
            self.assertEqual(backend.channel_for_position(backend.position_for_channel(channel)), channel)

    def test_invalid_labels_and_channels_raise(self):
        for label in ("0-1", "6-1", "1-11", "A1", "1", "1-x"):
            with self.assertRaises(ValueError):
                backend.channel_for_position(label)
        with self.assertRaises(ValueError):
            backend.position_for_channel(50)

    def test_counts_to_volts_every_range_code(self):
        for code, spec in backend.RANGE_CODES.items():
            self.assertAlmostEqual(backend.counts_to_volts(0, code), spec.low_v)
            self.assertAlmostEqual(backend.counts_to_volts(65535, code), spec.high_v - spec.lsb_v)
            self.assertAlmostEqual(backend.counts_to_volts(32768, code), (spec.low_v + spec.high_v) / 2)
        # the differential flag selects the same span
        self.assertAlmostEqual(backend.counts_to_volts(32768, 2 | backend.DIFFERENTIAL_FLAG), 2.5)
        with self.assertRaises(ValueError):
            backend.range_spec(16)

    def test_lsb_of_the_production_range_is_76_3_uV(self):
        self.assertAlmostEqual(backend.lsb_volts(2) * 1e6, 76.29, places=1)

    def test_counts_volts_round_trip_on_arrays(self):
        volts = np.array([0.0, 0.3, 1.2, 4.9, 6.0])
        counts = backend.volts_to_counts(volts, 2)
        self.assertEqual(counts.dtype, np.uint16)
        self.assertEqual(int(counts[-1]), 65535)  # clipped at the rail
        back = backend.counts_to_volts(counts, 2)
        np.testing.assert_allclose(back[:4], volts[:4], atol=backend.lsb_volts(2))

    def test_device_indices_from_mask(self):
        self.assertEqual(backend.device_indices_from_mask(1), (0,))
        self.assertEqual(backend.device_indices_from_mask(0b100), (2,))
        self.assertEqual(backend.device_indices_from_mask(0b101), (0, 2))
        self.assertEqual(backend.device_indices_from_mask(0), ())


# ----------------------------------------------------------------------
# Configuration block
# ----------------------------------------------------------------------
class ConfigBlockTests(unittest.TestCase):
    def test_production_config_bytes(self):
        block = backend.build_config_block(backend.AdcConfig())
        self.assertEqual(len(block), 21)
        self.assertEqual(block[:16], b"\x02" * 16)
        self.assertEqual(block[0x10], 0x00)  # normal (external pins)
        self.assertEqual(block[0x11], 0x05)  # timer trigger + scan
        self.assertEqual(block[0x12], 0x10)  # end 49 = 0x31 -> low nybble 1 << 4 | start 0
        self.assertEqual(block[0x13], 3)
        self.assertEqual(block[0x14], 0x30)  # end high nybble 0x30 | start high nybble 0

    def test_high_channel_nybbles(self):
        block = backend.build_config_block(backend.AdcConfig(start_channel=4, end_channel=63))
        self.assertEqual(block[0x12], 0xF4)
        self.assertEqual(block[0x14], 0x30)
        # the manual's worked example: start 7, end 107 is beyond this 64-channel board
        block = backend.build_config_block(backend.AdcConfig(start_channel=0, end_channel=63))
        self.assertEqual((block[0x12], block[0x14]), (0xF0, 0x30))

    def test_parse_round_trip(self):
        for config in (
            backend.AdcConfig(),
            backend.AdcConfig(range_code=7, start_channel=16, end_channel=63, oversample=255, trigger=0x1F, cal_code=backend.CAL_GROUND),
            backend.AdcConfig(range_code=2 | backend.DIFFERENTIAL_FLAG, start_channel=0, end_channel=31),
        ):
            self.assertEqual(backend.parse_config_block(backend.build_config_block(config)), config)

    def test_rejections(self):
        with self.assertRaises(ValueError):
            backend.AdcConfig(start_channel=10, end_channel=9)
        with self.assertRaises(ValueError):
            backend.AdcConfig(oversample=256)
        with self.assertRaises(ValueError):
            backend.AdcConfig(range_code=16)
        with self.assertRaises(ValueError):
            backend.AdcConfig(end_channel=64)
        with self.assertRaises(ValueError):
            backend.AdcConfig(cal_code=2)
        with self.assertRaises(ValueError):
            backend.parse_config_block(bytes(20))
        mixed = bytearray(backend.build_config_block(backend.AdcConfig()))
        mixed[3] = 7
        with self.assertRaises(ValueError):
            backend.parse_config_block(bytes(mixed))

    def test_derived_sizes(self):
        config = backend.AdcConfig()
        self.assertEqual(config.channels, 50)
        self.assertEqual(config.conversions_per_channel, 4)
        self.assertEqual(config.scan_bytes, 400)
        self.assertEqual(config.conversions_per_second, 200.0)
        self.assertEqual(64_000 % config.scan_bytes, 0)
        self.assertEqual(64_000 % 512, 0)


# ----------------------------------------------------------------------
# De-interleaving
# ----------------------------------------------------------------------
class DeinterleaveTests(unittest.TestCase):
    def test_drops_first_conversion_and_averages_the_rest(self):
        data, expected = scan_bytes_for(2, 3, 4)
        scans, leftover = backend.deinterleave_scans(data, 3, 4)
        self.assertEqual(scans.shape, (2, 3))
        np.testing.assert_allclose(scans, expected)
        self.assertEqual(leftover, b"")
        # hand check of scan 0 channel 0: conversions 0,1,2,3 -> mean(1,2,3) = 2
        self.assertEqual(scans[0, 0], 2.0)

    def test_carry_over_is_identical_for_any_split(self):
        data, expected = scan_bytes_for(7, 50, 4, base=100)
        one_shot, _ = backend.deinterleave_scans(data, 50, 4)
        np.testing.assert_allclose(one_shot, expected)
        for splits in ([1, 7, 13], [399, 401], [400], [2799, 2800, 2801], [64_000 - 3]):
            worker = backend.ScanDeinterleaver(50, 4)
            chunks = []
            previous = 0
            for cut in splits + [len(data)]:
                cut = min(cut, len(data))
                chunk = worker.feed(data[previous:cut])
                if chunk.shape[0]:
                    chunks.append(chunk)
                previous = cut
                self.assertEqual(worker.pending_bytes, cut % 400)
            np.testing.assert_allclose(np.concatenate(chunks), expected)
            self.assertEqual(worker.scans_emitted, 7)

    def test_partial_scan_alone_yields_nothing_and_is_kept(self):
        worker = backend.ScanDeinterleaver(50, 4)
        out = worker.feed(b"\x00" * 399)
        self.assertEqual(out.shape, (0, 50))
        self.assertEqual(worker.pending_bytes, 399)

    def test_drop_zero_keeps_all_conversions(self):
        data, _ = scan_bytes_for(1, 2, 4)
        scans, _ = backend.deinterleave_scans(data, 2, 4, drop_first=0)
        self.assertEqual(scans[0, 0], 1.5)  # mean(0,1,2,3)

    def test_drop_must_leave_one_conversion(self):
        with self.assertRaises(ValueError):
            backend.ScanDeinterleaver(2, 4, drop_first=4)
        with self.assertRaises(ValueError):
            backend.ScanDeinterleaver(2, 1, drop_first=1)
        backend.ScanDeinterleaver(2, 1, drop_first=0)  # single conversion, nothing dropped

    def test_average_false_returns_the_full_cube(self):
        data, _ = scan_bytes_for(3, 5, 4)
        cube, _ = backend.deinterleave_scans(data, 5, 4, average=False)
        self.assertEqual(cube.shape, (3, 5, 4))
        self.assertEqual(cube[0, 0, 0], 0.0)
        self.assertEqual(cube[0, 1, 0], 4.0)


# ----------------------------------------------------------------------
# Diagnostics
# ----------------------------------------------------------------------
class DiagnosticsTests(unittest.TestCase):
    def make(self, scans, elapsed, **kw):
        diag = backend.StreamDiagnostics(nominal_scan_hz=1000.0, actual_timer_hz=1000.0, started_monotonic=0.0, **kw)
        diag.scans_received = scans
        diag.stopped_monotonic = elapsed
        return diag

    def test_rate_within_one_percent_passes(self):
        self.make(59_500, 60.0).check()
        self.make(60_500, 60.0).check()

    def test_rate_beyond_one_percent_raises(self):
        with self.assertRaises(backend.StreamIntegrityError):
            self.make(58_000, 60.0).check()
        with self.assertRaises(backend.StreamIntegrityError):
            self.make(61_000, 60.0).check()

    def test_pool_event_and_callback_error_and_empty_stream_fail(self):
        with self.assertRaises(backend.StreamIntegrityError):
            self.make(60_000, 60.0, pool_too_small_events=1).check()
        with self.assertRaises(backend.StreamIntegrityError):
            self.make(60_000, 60.0, callback_error="boom").check()
        with self.assertRaises(backend.StreamIntegrityError):
            self.make(0, 60.0).check()

    def test_timer_grant_far_from_nominal_fails(self):
        diag = self.make(60_000, 60.0)
        diag.actual_timer_hz = 980.0
        with self.assertRaises(backend.StreamIntegrityError):
            diag.check()

    def test_summary_mentions_counts(self):
        text = self.make(1234, 1.234).summary()
        self.assertIn("1234 scans", text)
        self.assertIn("1000.00 scans/s", text)


# ----------------------------------------------------------------------
# The real device against the fake DLL
# ----------------------------------------------------------------------
class ConnectTests(unittest.TestCase):
    def test_connect_reads_identity(self):
        device, lib, _ = make_device()
        info = device.connect()
        self.assertEqual(info.name, "USB-AIO16-64MA")
        self.assertEqual(info.product_id, 0x8145)
        self.assertEqual(info.serial_number, "40E68DEE0D501728")
        self.assertEqual(info.device_index, 0)
        self.assertTrue(info.calibration_supported)
        self.assertFalse(info.simulated)
        self.assertIn("USB-AIO16-64MA", info.summary())

    def test_connect_retries_while_the_device_re_enumerates(self):
        device, lib, clock = make_device(FakeLib(device_masks=[0, 0, 1]))
        info = device.connect(timeout_s=10.0)
        self.assertEqual(info.device_index, 0)
        self.assertEqual(sum(1 for c in lib.calls if c[0] == "AIOUSB_ReloadDeviceLinks"), 2)
        self.assertEqual(clock.sleeps, [backend.CONNECT_RETRY_S, backend.CONNECT_RETRY_S])

    def test_connect_times_out(self):
        device, lib, clock = make_device(FakeLib(device_masks=[0]))
        with self.assertRaises(backend.DaqNotFoundError) as ctx:
            device.connect(timeout_s=2.0)
        self.assertIn("re-enumerates", str(ctx.exception))
        self.assertGreaterEqual(len(clock.sleeps), 4)

    def test_requested_index_is_honoured(self):
        lib = FakeLib(device_masks=[0b110])
        clock = FakeClock()
        device = backend.AiousbDaq(library=lib, device_index=2, sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(device.connect().device_index, 2)
        device = backend.AiousbDaq(library=FakeLib(device_masks=[0b110]), device_index=0, sleep=clock.sleep, monotonic=clock.monotonic)
        with self.assertRaises(backend.DaqNotFoundError):
            device.connect(timeout_s=1.0)

    def test_non_zero_status_names_the_function(self):
        lib = FakeLib()
        lib.status_overrides["QueryDeviceInfo"] = 87
        device, _, _ = make_device(lib)
        with self.assertRaises(backend.DaqStatusError) as ctx:
            device.connect()
        self.assertEqual(ctx.exception.function, "QueryDeviceInfo")
        self.assertEqual(ctx.exception.status, 87)

    def test_reads_before_configure_are_refused(self):
        device, _, _ = make_device()
        device.connect()
        with self.assertRaises(backend.StreamStateError):
            device.read_scan_counts()
        with self.assertRaises(backend.StreamStateError):
            device.start_stream(scan_hz=1000.0)


class ConfigureAndScanTests(unittest.TestCase):
    def test_configure_writes_and_reads_back(self):
        device, lib, _ = make_device()
        device.connect()
        config = device.configure(backend.AdcConfig())
        written = [c for c in lib.calls if c[0] == "ADC_SetConfig"][0][2]
        self.assertEqual(written, backend.build_config_block(config))
        self.assertEqual(device.config, config)

    def test_configure_readback_mismatch_raises(self):
        lib = FakeLib()
        lib.readback_override = bytes(21)
        device, _, _ = make_device(lib)
        device.connect()
        with self.assertRaises(backend.DaqError) as ctx:
            device.configure(backend.AdcConfig())
        self.assertIn("read back", str(ctx.exception))
        self.assertIsNone(device.config)

    def test_self_calibrate_sends_auto(self):
        device, lib, _ = make_device()
        device.connect()
        device.self_calibrate()
        self.assertIn(("ADC_SetCal", 0, b":AUTO:"), lib.calls)

    def test_set_cal_mode_rewrites_the_block(self):
        device, lib, _ = make_device()
        device.connect()
        device.configure(backend.AdcConfig())
        device.set_cal_mode(backend.CAL_GROUND)
        self.assertEqual(device.config.cal_code, backend.CAL_GROUND)
        self.assertEqual(lib.config_block[0x10], backend.CAL_GROUND)

    def test_read_scan_counts_and_median_volts(self):
        lib = FakeLib()
        lib.scan_counts[:] = 0
        lib.scan_counts[:50] = np.arange(50) * 100 + 9175  # channel 0 -> 0.7 V on 0-5 V
        device, _, _ = make_device(lib)
        device.connect()
        device.configure(backend.AdcConfig())
        counts = device.read_scan_counts(reads=3)
        self.assertEqual(counts.shape, (3, 50))
        volts = device.read_scan_volts_median(reads=3)
        self.assertEqual(volts.shape, (50,))
        self.assertAlmostEqual(volts[0], 0.7, places=3)
        self.assertEqual(sum(1 for c in lib.calls if c[0] == "ADC_GetScan"), 6)

    def test_immediate_reads_restore_the_configuration_block(self):
        # The DLL's ADC_GetScan/ADC_GetScanV rewrite byte 0x11 (trigger) and
        # leave it; the backend must write its own block back every time so
        # the device is always in the configuration ``config`` says it is.
        device, lib, _ = make_device()
        device.connect()
        device.configure(backend.AdcConfig())
        intended = backend.build_config_block(backend.AdcConfig())
        self.assertEqual(intended[0x11], 0x05)
        lib._immediate_read_side_effect()  # the fake really models it
        self.assertEqual(lib.config_block[0x11], 0x04)
        device.configure(backend.AdcConfig())
        device.read_scan_counts(reads=2)
        self.assertEqual(lib.config_block, intended)
        names = [c[0] for c in lib.calls]
        last_scan = max(i for i, name in enumerate(names) if name == "ADC_GetScan")
        last_set = max(i for i, name in enumerate(names) if name == "ADC_SetConfig")
        self.assertGreater(last_set, last_scan)
        device.read_scan_driver_volts()
        self.assertEqual(lib.config_block, intended)

    def test_immediate_read_failure_is_not_masked_by_the_reassert(self):
        device, lib, _ = make_device()
        device.connect()
        device.configure(backend.AdcConfig())
        lib.status_overrides["ADC_GetScan"] = 5
        lib.status_overrides["ADC_SetConfig"] = 7   # the re-assert fails too: the read's error must win
        with self.assertRaises(backend.DaqStatusError) as raised:
            device.read_scan_counts()
        self.assertEqual(raised.exception.function, "ADC_GetScan")

    def test_driver_volts_are_separate_from_own_scale(self):
        device, lib, _ = make_device()
        device.connect()
        device.configure(backend.AdcConfig())
        driver = device.read_scan_driver_volts()
        self.assertEqual(driver.shape, (50,))
        self.assertAlmostEqual(driver[0], 2.5, places=3)


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.lib = FakeLib()
        self.lib.timer_hz_granted = 1000.0
        self.device, _, self.clock = make_device(self.lib)
        self.device.connect()
        self.device.configure(backend.AdcConfig())

    def test_start_stream_registers_callback_then_starts_the_timer(self):
        header = self.device.start_stream(scan_hz=1000.0, buffer_bytes=64_000, buffer_count=32)
        names = [c[0] for c in self.lib.calls]
        self.assertLess(names.index("ADC_BulkContinuousCallbackStart"), names.index("CTR_StartOutputFreq"))
        start_call = [c for c in self.lib.calls if c[0] == "ADC_BulkContinuousCallbackStart"][0]
        self.assertEqual(start_call[2:], (64_000, 32, 0))
        self.assertEqual(header.actual_timer_hz, 1000.0)
        self.assertTrue(self.device.is_streaming)
        self.device.stop_stream()

    def test_unaligned_buffers_become_scans_and_flags_are_counted(self):
        self.device.start_stream(scan_hz=1000.0)
        data, expected = scan_bytes_for(5, 50, 4)
        self.lib.deliver(data[:700])
        self.lib.deliver(data[700:1_500], flags=backend.CALLBACK_FLAG_POOL_TOO_SMALL)
        self.lib.deliver(data[1_500:])
        self.clock.now += 0.005
        chunks = []
        for _ in range(10):
            chunk = self.device.read_stream(timeout_s=0.2)
            if chunk is None:
                if sum(c.shape[0] for c in chunks) >= 5:
                    break
                continue
            chunks.append(chunk)
        received = np.concatenate(chunks)
        np.testing.assert_allclose(received, expected)
        diagnostics = self.device.stop_stream()
        self.assertEqual(diagnostics.scans_received, 5)
        self.assertEqual(diagnostics.pool_too_small_events, 1)
        self.assertEqual(diagnostics.buffers_received, 3)
        self.assertEqual(diagnostics.leftover_bytes, 0)
        self.assertEqual(sum(1 for c in self.lib.calls if c[0] == "ADC_BulkContinuousEnd"), 1)
        # the timer was started at 1000 Hz and stopped with 0 Hz
        self.assertEqual(self.lib.timer_calls, [1000.0, 0.0])
        self.assertFalse(self.device.is_streaming)

    def test_end_of_stream_flag_marks_the_diagnostics(self):
        self.device.start_stream(scan_hz=1000.0)
        data, _ = scan_bytes_for(2, 50, 4)
        self.lib.deliver(data)
        self.lib.deliver(b"", flags=backend.CALLBACK_FLAG_END_OF_STREAM)
        self.device.read_stream(timeout_s=0.5)
        diagnostics = self.device.stop_stream()
        self.assertTrue(diagnostics.ended_by_device)
        self.assertEqual(diagnostics.scans_received, 2)

    def test_callback_exception_is_recorded_not_lost(self):
        import queue

        class BrokenQueue:
            def put(self, item):
                raise RuntimeError("enqueue failed")

        self.device.start_stream(scan_hz=1000.0)
        real_queue = self.device._raw_queue
        self.device._raw_queue = BrokenQueue()  # an internal failure inside the DLL's callback thread
        data, _ = scan_bytes_for(1, 50, 4)
        self.lib.deliver(data)  # must not raise through ctypes; must be recorded instead
        self.assertIn("enqueue failed", self.device._callback_error)
        with self.assertRaises(backend.StreamIntegrityError):
            self.device.read_stream(timeout_s=0.01)
        self.device._raw_queue = real_queue if real_queue is not None else queue.Queue()
        diagnostics = self.device.stop_stream()
        self.assertIn("enqueue failed", diagnostics.callback_error)

    def test_rate_check_uses_the_fake_clock(self):
        self.device.start_stream(scan_hz=1000.0)
        data, _ = scan_bytes_for(1000, 50, 4)
        self.lib.deliver(data)
        self.device.read_stream(timeout_s=0.5)
        self.clock.now += 1.0  # exactly one second for 1000 scans
        diagnostics = self.device.stop_stream()
        # stop_stream itself sleeps 0.1 s on the fake clock AFTER stamping the stop time
        self.assertAlmostEqual(diagnostics.elapsed_s, 1.0, places=6)
        diagnostics.check()

    def test_second_start_and_reads_without_stream_are_refused(self):
        self.device.start_stream(scan_hz=1000.0)
        with self.assertRaises(backend.StreamStateError):
            self.device.start_stream(scan_hz=1000.0)
        with self.assertRaises(backend.StreamStateError):
            self.device.read_scan_counts()
        with self.assertRaises(backend.StreamStateError):
            self.device.configure(backend.AdcConfig())
        self.device.stop_stream()
        with self.assertRaises(backend.StreamStateError):
            self.device.read_stream()
        with self.assertRaises(backend.StreamStateError):
            self.device.stop_stream()

    def test_start_failure_leaves_no_stream(self):
        self.lib.status_overrides["ADC_BulkContinuousCallbackStart"] = 31
        with self.assertRaises(backend.DaqStatusError):
            self.device.start_stream(scan_hz=1000.0)
        self.assertFalse(self.device.is_streaming)
        self.assertIsNone(self.device._callback)

    def test_buffer_arguments_are_validated(self):
        with self.assertRaises(ValueError):
            self.device.start_stream(scan_hz=1000.0, buffer_bytes=1000)
        with self.assertRaises(ValueError):
            self.device.start_stream(scan_hz=1000.0, buffer_count=1)
        with self.assertRaises(ValueError):
            self.device.start_stream(scan_hz=0.0)

    def _corrupt_trigger_byte(self) -> bytes:
        """Mimic the DLL side effect having happened behind the backend's back."""

        block = bytearray(self.lib.config_block)
        block[0x11] = 0x04
        self.lib.config_block = bytes(block)
        return backend.build_config_block(backend.AdcConfig())

    def test_start_stream_reasserts_the_block_after_an_immediate_read(self):
        self.device.read_scan_counts(reads=3)  # the tester's offset polling
        intended = self._corrupt_trigger_byte()
        before = len(self.lib.calls)
        self.device.start_stream(scan_hz=1000.0)
        since = self.lib.calls[before:]
        names = [c[0] for c in since]
        start = names.index("ADC_BulkContinuousCallbackStart")
        sets = [c for i, c in enumerate(since) if c[0] == "ADC_SetConfig" and i < start]
        self.assertTrue(sets, "no ADC_SetConfig before the stream start")
        self.assertEqual(sets[-1][2], intended)
        self.assertEqual(self.lib.config_block[0x11], 0x05)
        self.device.stop_stream()

    def test_bulk_acquire_reasserts_the_block(self):
        self.device.read_scan_counts(reads=1)
        intended = self._corrupt_trigger_byte()
        data, _ = scan_bytes_for(10, 50, 4)
        self.lib.bulk_buffer_fill = data
        self.lib.poll_sequence = [0]
        before = len(self.lib.calls)
        self.device.bulk_acquire(scans=10, scan_hz=1000.0, timeout_s=5.0)
        since = self.lib.calls[before:]
        names = [c[0] for c in since]
        acquire = names.index("ADC_BulkAcquire")
        sets = [c for i, c in enumerate(since) if c[0] == "ADC_SetConfig" and i < acquire]
        self.assertTrue(sets)
        self.assertEqual(sets[-1][2], intended)

    def test_end_flag_before_stop_is_a_problem(self):
        self.device.start_stream(scan_hz=1000.0)
        data, _ = scan_bytes_for(2, 50, 4)
        self.lib.deliver(data)
        self.lib.deliver(b"", flags=backend.CALLBACK_FLAG_END_OF_STREAM)
        self.device.read_stream(timeout_s=0.5)
        diagnostics = self.device.stop_stream()
        self.assertTrue(diagnostics.ended_by_device)
        self.assertTrue(diagnostics.ended_early)
        self.assertIn("before it was stopped", "; ".join(diagnostics.problems()))
        self.assertIn("ended by device before stop", diagnostics.summary())

    def test_end_flag_from_a_normal_stop_is_not_a_problem(self):
        self.lib.end_delivers_end_flag = True
        self.device.start_stream(scan_hz=1000.0)
        data, _ = scan_bytes_for(1000, 50, 4)
        self.lib.deliver(data)
        self.device.read_stream(timeout_s=0.5)
        self.clock.now += 1.0
        diagnostics = self.device.stop_stream()
        self.assertTrue(diagnostics.ended_by_device)
        self.assertFalse(diagnostics.ended_early)
        self.assertNotIn("ended by device", diagnostics.summary())
        diagnostics.check()

    def test_bulk_acquire_one_shot(self):
        data, expected = scan_bytes_for(10, 50, 4)
        self.lib.bulk_buffer_fill = data
        self.lib.poll_sequence = [4000, 2000, 0]
        scans, hz = self.device.bulk_acquire(scans=10, scan_hz=1000.0, timeout_s=5.0)
        np.testing.assert_allclose(scans, expected)
        self.assertEqual(hz, 1000.0)
        self.assertEqual(self.lib.timer_calls, [1000.0, 0.0])

    def test_bulk_acquire_timeout(self):
        self.lib.bulk_buffer_fill = bytes(4000)
        self.lib.poll_sequence = [4000] * 1000
        with self.assertRaises(backend.StreamTimeoutError):
            self.device.bulk_acquire(scans=10, scan_hz=1000.0, timeout_s=0.5)
        self.assertEqual(self.lib.timer_calls[-1], 0.0)


# ----------------------------------------------------------------------
# Simulator
# ----------------------------------------------------------------------
class SimulatedDaqTests(unittest.TestCase):
    def test_default_profile_positions(self):
        sim = backend.SimulatedDaq(real_time=False)
        info = sim.connect()
        self.assertTrue(info.simulated)
        sim.configure(backend.AdcConfig())
        # let the upward settling finish so the reads reflect the profile
        sim._virtual_t = 200.0
        volts = sim.read_scan_volts_median(reads=5)
        self.assertEqual(volts.shape, (50,))
        by = lambda label: volts[backend.channel_for_position(label)]
        self.assertAlmostEqual(by("1-1"), 0.70, delta=0.01)
        self.assertAlmostEqual(by("2-4"), 1.62, delta=0.01)
        self.assertGreater(by("3-1"), 4.9)
        self.assertAlmostEqual(by("4-7"), 0.21, delta=0.01)
        self.assertLess(by("5-2"), 0.05)
        self.assertLess(by("1-10"), 0.01)
        self.assertLess(by("5-10"), 0.01)

    def test_offsets_settle_upward_after_power_on(self):
        sim = backend.SimulatedDaq(real_time=False)
        sim.connect()
        sim.configure(backend.AdcConfig())
        early = sim.read_scan_volts_median(reads=1)[0]
        sim._virtual_t = 100.0
        late = sim.read_scan_volts_median(reads=1)[0]
        self.assertLess(early, late)
        self.assertAlmostEqual(late - early, 0.15, delta=0.02)

    def test_stream_shapes_and_perfect_diagnostics(self):
        sim = backend.SimulatedDaq(real_time=False)
        sim.connect()
        sim.configure(backend.AdcConfig())
        sim.start_stream(scan_hz=1000.0)
        total = 0
        while total < 2000:
            chunk = sim.read_stream()
            self.assertEqual(chunk.shape[1], 50)
            total += chunk.shape[0]
        diagnostics = sim.stop_stream()
        self.assertEqual(diagnostics.scans_received, total)
        self.assertAlmostEqual(diagnostics.elapsed_s, total / 1000.0)
        diagnostics.check()
        self.assertEqual(sim.stream_attempts, 1)

    def test_injected_gap_and_pool_events_fail_the_check_on_the_named_attempt(self):
        profile = backend.SimProfile(gap_on_attempts=frozenset({1}), pool_events_on_attempts=frozenset({2}))
        sim = backend.SimulatedDaq(profile, real_time=False)
        sim.connect()
        sim.configure(backend.AdcConfig())
        for attempt in (1, 2, 3):
            sim.start_stream(scan_hz=1000.0)
            sim.read_stream()
            diagnostics = sim.stop_stream()
            if attempt in (1, 2):
                with self.assertRaises(backend.StreamIntegrityError):
                    diagnostics.check()
            else:
                diagnostics.check()

    def test_average_false_returns_cube(self):
        sim = backend.SimulatedDaq(real_time=False)
        sim.connect()
        sim.configure(backend.AdcConfig())
        sim.start_stream(scan_hz=1000.0, average=False)
        cube = sim.read_stream()
        self.assertEqual(cube.shape, (100, 50, 4))
        sim.stop_stream()

    def test_ground_cal_mode_reads_near_zero(self):
        sim = backend.SimulatedDaq(real_time=False)
        sim.connect()
        sim.configure(backend.AdcConfig())
        sim.set_cal_mode(backend.CAL_GROUND)
        volts = sim.read_scan_volts_median(reads=3)
        self.assertLess(np.abs(volts).max(), 0.001)

    def test_state_guards(self):
        sim = backend.SimulatedDaq(real_time=False)
        sim.connect()
        with self.assertRaises(backend.StreamStateError):
            sim.read_scan_counts()
        sim.configure(backend.AdcConfig())
        with self.assertRaises(backend.StreamStateError):
            sim.stop_stream()
        sim.start_stream(scan_hz=1000.0)
        with self.assertRaises(backend.StreamStateError):
            sim.start_stream(scan_hz=1000.0)
        with self.assertRaises(backend.StreamStateError):
            sim.configure(backend.AdcConfig())
        sim.stop_stream()


# ----------------------------------------------------------------------
# Source hygiene
# ----------------------------------------------------------------------
class SourceHygieneTests(unittest.TestCase):
    SOURCE = (MODEL_DIR / "daq_backend.py").read_text(encoding="utf-8")

    def test_backend_has_no_tk_import(self):
        self.assertNotIn("tkinter", self.SOURCE)

    def test_every_dll_call_has_a_declared_prototype(self):
        used = set(re.findall(r"self\._lib\.([A-Za-z_0-9]+)\(", self.SOURCE))
        missing = used - set(backend._SIGNATURES)
        self.assertEqual(missing, set(), f"DLL calls without a prototype: {sorted(missing)}")
        self.assertIn("ADC_BulkContinuousCallbackStart", used)

    def test_no_cross_tree_import(self):
        self.assertNotIn("single_detector_rig", self.SOURCE)


if __name__ == "__main__":
    unittest.main()
