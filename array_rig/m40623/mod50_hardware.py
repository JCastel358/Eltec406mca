"""M25/HUB controls for the array tester, ported from the working bench viewer.

DIRECT_DB37_R2: UPPER to J2, LOWER to J1. All 57 physical rows are acquired;
only the explicit map below selects the 50 detector positions. This module
has no dependency on the separate 50AmpBoard workspace and no ESP32 access.
"""
from __future__ import annotations

import ctypes
from dataclasses import asdict
import hashlib
import json
import sys
import time

import numpy as np

import daq_backend as daq
import r4_frontend as r4
from daq_rig_readout import _live_callback_buffer_bytes

PROFILE = "M25_HUB_DIRECT_DB37_R2_ARRAY_CHECK"
APP_REVISION = "2026-11-11.1"
SAMPLE_HZ = 1000.0
BUFFER_COUNT = 64
POSITIONS = tuple(f"{i // 10 + 1}-{i % 10 + 1}" for i in range(50))
MAPPING = tuple(range(16)) + tuple(range(32, 41)) + tuple(range(16, 32)) + tuple(range(48, 57))
# Preserve the measured bench's signed ranges and converter endpoint guard.
# Calibrated saturation can be code 63, not exactly zero. This is a data
# quality guard, never a detector acceptance limit.
DC_RANGE = 3  # +/-5 V
NOISE_RANGE = 7  # +/-1 V
RAIL_GUARD_COUNTS = 328


def channel_identity(logical):
    if not 0 <= logical < 50:
        raise ValueError("Logical channel must be 0..49")
    local = logical % 25
    return {"channel": logical + 1, "position": POSITIONS[logical],
            "physical_daq_channel": MAPPING[logical],
            "card": "UPPER" if logical < 25 else "LOWER", "card_channel": local + 1,
            "daq_jack": "J2" if logical < 25 else "J1"}


CHANNEL_MAP = [channel_identity(i) for i in range(50)]
MAP_SHA256 = hashlib.sha256(json.dumps(CHANNEL_MAP, sort_keys=True).encode()).hexdigest()


def normalize_positions(positions):
    if isinstance(positions, str):
        raise ValueError("Pass a collection of position labels")
    values = tuple(positions)
    if any(p not in POSITIONS for p in values) or len(set(values)) != len(values):
        raise ValueError("Use unique positions 1-1 through 5-10")
    return tuple(p for p in POSITIONS if p in values)


def decode_status(value):
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("Expected a status byte")
    result = {"raw_byte": value}
    for card, route, gain, ready, present in (("UPPER", 0, 1, 2, 6), ("LOWER", 3, 4, 5, 7)):
        result[card] = {"route": bool(value & (1 << route)), "gain": bool(value & (1 << gain)),
                        "ready": not bool(value & (1 << ready)),
                        "present": not bool(value & (1 << present))}
    return result


class TransferWatch:
    def __init__(self):
        self.started = time.monotonic()
        self.last_payload = self.last_delivery = self.started
        self.callbacks = self.bytes = self.scans = 0
        self.max_payload_gap_s = self.max_delivery_gap_s = 0.0

    def payload(self, size):
        self.callbacks += 1
        self.bytes += size
        if size:
            now = time.monotonic()
            self.max_payload_gap_s = max(self.max_payload_gap_s, now - self.last_payload)
            self.last_payload = now

    def delivery(self, count):
        if count:
            now = time.monotonic()
            self.max_delivery_gap_s = max(self.max_delivery_gap_s, now - self.last_delivery)
            self.last_delivery = now
            self.scans += count

    def snapshot(self):
        now = time.monotonic()
        return {"callbacks": self.callbacks, "bytes": self.bytes, "delivered_scans": self.scans,
                "last_payload_age_s": now - self.last_payload,
                "last_delivery_age_s": now - self.last_delivery,
                "max_payload_gap_s": self.max_payload_gap_s,
                "max_delivery_gap_s": self.max_delivery_gap_s}


def transfer_snapshot(device):
    watch = getattr(device, "transfer_watch", None)
    result = watch.snapshot() if watch else {}
    for attr, key in (("_raw_queue", "raw_blocks_waiting"), ("_out_queue", "decoded_blocks_waiting")):
        queue = getattr(device, attr, None)
        result[key] = queue.qsize() if queue is not None else None
    diag = getattr(device, "_diagnostics", None)
    result["diagnostics"] = asdict(diag) if diag is not None else None
    return result


class CheckedDaq(daq.AiousbDaq):
    def connect(self, **kwargs):
        info = super().connect(**kwargs)
        if info.product_id != 0x8145 or len(daq.device_indices_from_mask(int(self._lib.GetDevices()))) != 1:
            self.close()
            raise daq.DaqError("Leave only the M25/HUB USB-AIO16-64MA connected to this computer.")
        return info

    def start_stream(self, **kwargs):
        self.transfer_watch = TransferWatch()
        return super().start_stream(**kwargs)

    def _on_buffer(self, pbuf, bufsize, flags, context):
        try:
            self.transfer_watch.payload(int(bufsize))
        except Exception:
            pass  # Optional telemetry must not disrupt a driver callback.
        return super()._on_buffer(pbuf, bufsize, flags, context)

    def read_stream(self, **kwargs):
        raw = super().read_stream(**kwargs)
        self.transfer_watch.delivery(0 if raw is None else len(raw))
        return raw


class DemoDaq(r4.R4SimulatedDaq):
    """Synthetic data and card logic; this is not an analog settling model."""
    status_override = None

    def _volts_for_config(self, volts):
        config = self._require_config()
        return volts[..., config.start_channel:config.end_channel + 1]

    def read_mod50_status(self):
        if self.status_override is not None:
            return self.status_override
        if not self.power_ready:
            return 255
        route, gain = self.controls
        return int(route) * 0x09 | int(route and gain) * 0x12

    def _synth(self, scans, scan_hz):
        self._advance(scans / scan_hz)
        out = self._rng.normal(0.0, 0.003 if self.controls == (True, True) else .0002, (scans, 64))
        if self.controls != (True, True):
            out[:, np.asarray(MAPPING)] += .6 + np.arange(50) / 200.0
        out[:, 41:48] = 0
        return out


class Mod50Rig(r4.R4Rig):
    def __init__(self, *, simulate=False, device=None, **kwargs):
        if device is None:
            device = DemoDaq(real_time=False) if simulate else CheckedDaq()
        super().__init__(board_serial="UNIDENTIFIED-M25-HUB", dedicated_ports=True,
                         device=device, simulate=simulate, scan_hz=SAMPLE_HZ,
                         start_channel=0, end_channel=56, oversample=3, drop_first=1,
                         buffer_count=BUFFER_COUNT, quiet=True, **kwargs)
        self.control_events = []
        self.shutdown_status = None

    def connect(self):
        try:
            info = super().connect()
            self.set_range(DC_RANGE)
            return info
        except BaseException:
            self.device.close()
            self._connected = False
            self._info = None
            raise

    def _verify(self, expected):
        try:
            self.readback = decode_status(self.device.read_mod50_status())
            issues = []
            for slot in ("UPPER", "LOWER"):
                card = self.readback[slot]
                if not card["ready"] or not card["present"]:
                    issues.append(slot + " not ready/present")
                if (card["route"], card["gain"]) != expected:
                    issues.append(slot + " switching mismatch")
            if issues:
                raise daq.StreamIntegrityError("; ".join(issues) + ". Check physical RUN, power and control cable.")
        except Exception:
            self.settle_until = self._clock() + r4.NOISE_SETTLE_S
            raise
        return self.readback

    def set_frontend_mode(self, mode):
        super().set_frontend_mode(mode)
        if mode == "dc":
            self.set_range(DC_RANGE)

    def _write(self, route, gain):
        super()._write(route, gain)
        self.control_events.append({"monotonic_s": self._clock(), "route_noise": route,
                                    "gain": gain, "readback": self.readback})

    def capture_r4(self, *args, **kwargs):
        raise daq.StreamStateError("Use the M25/HUB tray check to preserve its map, timing and raw conversions.")

    def close(self):
        """End the stream before gain-off, 50 ms hold, and return to DC."""
        with self._owner:
            errors = []
            try:
                self._stop_and_drain()
            except Exception as exc:
                errors.append(str(exc))
            failed_shutdown = getattr(self.device, "_shutdown_failed", False)
            if failed_shutdown:
                errors.append("Driver shutdown was not confirmed. Use physical STOP and restart the app before another check.")
            if not failed_shutdown and not self.device.is_streaming and getattr(self.device, "_r4_dio_owned", False):
                try:
                    self.device.write_r4_controls(self.commanded[0], False)
                    self._sleep(r4.TRANSITION_S)
                except Exception as exc:
                    errors.append("Gain-off: " + str(exc))
                try:
                    self.device.write_r4_controls(False, False)
                    self.commanded = (False, False)
                    self.shutdown_status = self._verify(self.commanded)
                except Exception as exc:
                    errors.append("Return to DC: " + str(exc))
            try:
                self.device.close()
            finally:
                self.mode, self._info = None, None
                self._connected = False
                self.settle_until = float("inf")
            if errors:
                raise daq.StreamIntegrityError("; ".join(errors))


class DaqLease:
    """Use the working viewer's lock so either app excludes the other."""
    def __enter__(self):
        if sys.platform != "win32":
            raise RuntimeError("Real acquisition requires Windows and the ACCES driver.")
        self.dll = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dll.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        self.dll.CreateMutexW.restype = ctypes.c_void_p
        self.dll.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.dll.WaitForSingleObject.restype = ctypes.c_uint32
        self.dll.ReleaseMutex.argtypes = self.dll.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.dll.CreateMutexW(None, False, "Local\\MOD50_LIVE_BENCH_DAQ")
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if self.dll.WaitForSingleObject(self.handle, 0) not in (0, 128):
            self.dll.CloseHandle(self.handle)
            raise RuntimeError("Another MOD50 window is using the DAQ. Stop its acquisition first.")
        return self

    def __exit__(self, *args):
        self.dll.ReleaseMutex(self.handle)
        self.dll.CloseHandle(self.handle)


def convert_chunk(raw, range_code):
    raw = np.asarray(raw)
    if (raw.ndim != 3 or raw.shape[1:] != (57, 4) or not len(raw)
            or not np.all(np.isfinite(raw)) or np.any(raw < 0) or np.any(raw > 65535)
            or np.any(raw != np.floor(raw))):
        raise daq.StreamIntegrityError("Expected complete 57-channel, four-conversion scans.")
    retained = raw[:, :, 1:]
    volts = daq.counts_to_volts(retained.mean(axis=2), range_code).T
    clipped = np.any((retained <= RAIL_GUARD_COUNTS) | (retained >= 65535 - RAIL_GUARD_COUNTS), axis=2).T
    return volts, clipped
