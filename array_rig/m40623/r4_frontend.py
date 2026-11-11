"""Opt-in 50SensorNoise R4 acquisition. Never used implicitly for R3 data.

Only this owner sets the global controls; acquired samples remain signed DAQ
volts. Readback is conditioned route, effective gain, and four-rail readiness.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import numpy as np

import daq_backend as daq
from daq_rig_readout import ArrayRig, Capture

PROFILE_ID = "50SensorNoise_R4"
MODES = ("dc", "noise")
NOISE_SETTLE_S = 90.0
DC_SETTLE_S = 1.0
TRANSITION_S = 0.050
ENTER_NOISE_DELAY_S = 0.200  # exceeds hardware recovery interlock delay


class R4SimulatedDaq(daq.SimulatedDaq):
    """Logic/waveform simulator, explicitly not an analog recovery model."""
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.controls = (False, False)
        self.power_ready = True
        self.control_history: list[tuple[bool, bool]] = []
        self._r4_dio_owned = False

    def configure_r4_dio(self, *, dedicated_ports: bool = False) -> None:
        if not dedicated_ports or self.is_streaming:
            raise daq.StreamStateError("R4 requires dedicated ports and a stopped stream")
        self._r4_dio_owned = True
        self.write_r4_controls(False, False)

    def write_r4_controls(self, route_noise: bool, noise_gain: bool) -> None:
        if not self._r4_dio_owned or self.is_streaming or (noise_gain and not route_noise):
            raise daq.StreamStateError("Unsafe R4 control write")
        self.controls = (bool(route_noise), bool(noise_gain))
        self.control_history.append(self.controls)

    def read_r4_controls(self) -> tuple[bool, bool, bool]:
        return (self.controls[0] and self.power_ready,
                self.controls[0] and self.controls[1] and self.power_ready, self.power_ready)

    def _synth(self, scans: int, scan_hz: float) -> np.ndarray:
        if self.controls == (True, True):
            self._advance(scans / scan_hz)
            return self._rng.normal(0.0, 0.0002, (scans, daq.CHANNEL_COUNT))
        return super()._synth(scans, scan_hz)


class R4Rig(ArrayRig):
    """Serialized R4 owner. Connection is explicit permission to reserve DIO.

    A mode/range change ends the old capture. It does not restart acquisition.
    Every reconnect starts DC/unity and repeats the 90 s power/connect gate.
    """
    def __init__(self, *, board_serial: str, dedicated_ports: bool = False,
                 calibration: dict[str, Any] | None = None,
                 monotonic: Any = time.monotonic, sleep: Any = time.sleep,
                 **kwargs: Any) -> None:
        if not board_serial.strip():
            raise ValueError("A board serial/engineering identifier is required")
        if not dedicated_ports:
            raise ValueError("Explicit dedicated_ports=True is required for R4 DIO0..15")
        if kwargs.get("simulate") and kwargs.get("device") is None:
            kwargs["device"] = R4SimulatedDaq(real_time=True)
        kwargs.setdefault("scan_hz", 2000.0)
        kwargs.setdefault("range_code", 2)
        super().__init__(**kwargs)
        if self.scan_hz * self.config.channels * self.config.conversions_per_channel > 500_000:
            raise ValueError("Requested conversion rate exceeds the DAQ 500 kS/s limit")
        self.board_serial = board_serial
        self.calibration = validate_calibration(calibration) if calibration is not None else None
        self._owner = threading.RLock()
        self._clock, self._sleep = monotonic, sleep
        self.mode: str | None = None
        self.commanded = (False, False)
        self.readback: tuple[bool, bool, bool] | None = None
        self.settle_until = float("inf")
        self.generation = 0
        self.last_capture: Capture | None = None
        self.last_metadata: dict[str, Any] | None = None
        self.last_transition_diagnostics: dict[str, Any] | None = None

    def connect(self) -> daq.DaqInfo:
        with self._owner:
            info = super().connect()
            try:
                self.device.configure_r4_dio(dedicated_ports=True)
                self.commanded = (False, False)
                self.mode = None
                self.settle_until = self._clock() + NOISE_SETTLE_S
                self.set_frontend_mode("dc")
                if self.calibration is not None:
                    if (self.calibration["board_serial"] != self.board_serial or
                            self.calibration["daq_serial"] != info.serial_number):
                        raise ValueError("Calibration board/DAQ identity does not match this connection")
            except BaseException:
                super().close()
                self.mode = None
                raise
            return info

    def _stop_and_drain(self) -> None:
        if self.live is not None and self.live.is_alive():
            self.live.stop()
            if self.live.is_alive():
                raise daq.StreamStateError("Acquisition owner did not stop; controls unchanged")
            diagnostics = self.live.diagnostics
        elif self.device.is_streaming:
            diagnostics = self.device.stop_stream()
        else:
            diagnostics = None
        if diagnostics is not None:
            self.last_transition_diagnostics = asdict(diagnostics)
            diagnostics.check()
        drain = getattr(self.device, "drain_stream", None)
        if drain:
            drain()

    def _verify(self, expected: tuple[bool, bool]) -> tuple[bool, bool, bool]:
        try:
            self.readback = tuple(self.device.read_r4_controls())
        except Exception:
            self.settle_until = self._clock() + NOISE_SETTLE_S
            raise
        if self.readback != (*expected, True):
            self.settle_until = self._clock() + NOISE_SETTLE_S
            raise daq.StreamIntegrityError(
                f"R4 conditioned route/gain/power readback {self.readback}; expected {(*expected, True)}")
        return self.readback

    def _write(self, route: bool, gain: bool) -> None:
        self.device.write_r4_controls(route, gain)
        self.commanded = (route, gain)
        self._verify(self.commanded)

    def set_frontend_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("R4 mode must be dc or noise")
        with self._owner:
            self._require_connected()
            self._stop_and_drain()
            self.mode = None  # intermediate states cannot be recorded as modes
            self.generation += 1
            try:
                # Disable gain first, preserving current route even on reconnect.
                self._write(self.commanded[0], False)
                if mode == "noise":
                    self._write(True, False)
                    self._sleep(ENTER_NOISE_DELAY_S)
                    self._write(True, True)
                else:
                    self._sleep(TRANSITION_S)
                    self._write(False, False)
                super().set_range(7 if mode == "noise" else 2)
                self.mode = mode
                self.settle_until = max(self.settle_until, self._clock() +
                                        (NOISE_SETTLE_S if mode == "noise" else DC_SETTLE_S))
            except BaseException:
                self.settle_until = float("inf")
                # Best effort only: hardware gates protect a failed USB write.
                try:
                    self.device.write_r4_controls(False, False)
                    self.commanded = (False, False)
                except Exception:
                    pass
                raise

    def detector_connection_changed(self) -> None:
        with self._owner:
            self._stop_and_drain()
            self.generation += 1
            self.settle_until = self._clock() + NOISE_SETTLE_S

    @property
    def settling_remaining_s(self) -> float:
        return max(0.0, self.settle_until - self._clock())

    def set_range(self, range_code: int) -> daq.RangeSpec:
        if not 0 <= range_code <= 7:
            raise ValueError("R4 uses single-ended range codes 0..7")
        with self._owner:
            self._stop_and_drain()
            self.generation += 1
            result = super().set_range(range_code)
            self.settle_until = max(self.settle_until, self._clock() + DC_SETTLE_S)
            return result

    def capture_r4(self, seconds: float, *, diagnostic_reason: str = "",
                   cancelled: Any = lambda: False, progress: Any = None) -> tuple[Capture, dict[str, Any]]:
        """Capture signed data; preserve partial/faulted data with final diagnostics.

        A diagnostic reason permits starting before settling, never bypasses
        control/readiness faults. Returned status never means detector PASS.
        """
        with self._owner:
            self._require_connected()
            if self.mode not in MODES or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("A stable R4 mode and positive duration are required")
            self._stop_and_drain()
            self._verify(self.commanded)
            remaining = self.settling_remaining_s
            if remaining and not diagnostic_reason.strip():
                raise daq.StreamStateError(f"R4 settling: {remaining:.1f} s remain")
            reasons = [f"UNSETTLED: {remaining:.3f} s remaining"] if remaining else []
            if diagnostic_reason.strip():
                reasons.append("DIAGNOSTIC_OVERRIDE: " + diagnostic_reason.strip())
            chunks: list[np.ndarray] = []
            diagnostics = None
            start = dt.datetime.now().astimezone().isoformat()
            header = self.device.start_stream(scan_hz=self.scan_hz, drop_first=self.drop_first,
                                              buffer_bytes=self.buffer_bytes, buffer_count=self.buffer_count)
            target = max(1, round(seconds * header.actual_timer_hz))
            received = 0
            deadline = time.monotonic() + seconds + 5.0
            try:
                while received < target:
                    if cancelled():
                        raise daq.StreamIntegrityError("Capture cancelled")
                    self._verify(self.commanded)
                    chunk = self.device.read_stream(timeout_s=0.2)
                    if chunk is not None:
                        chunks.append(chunk)
                        received += len(chunk)
                        if progress:
                            progress(min(seconds, received / header.actual_timer_hz))
                    elif time.monotonic() > deadline:
                        raise daq.StreamTimeoutError("R4 stream stalled")
            except Exception as exc:
                reasons.append(f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    diagnostics = self.device.stop_stream()
                    reasons.extend(diagnostics.problems())
                except Exception as exc:
                    reasons.append(f"Stop failed: {exc}")
                try:
                    chunks.extend(self.device.drain_stream())
                    self._verify(self.commanded)
                except Exception as exc:
                    reasons.append(f"Final readback/drain: {exc}")
            counts = np.concatenate(chunks)[:target] if chunks else np.empty((0, self.config.channels))
            if len(counts) < target:
                reasons.append(f"INCOMPLETE: {len(counts)} of {target} scans")
            volts = np.ascontiguousarray(daq.counts_to_volts(counts, self.range_code).T)
            info = self.info
            cap = Capture(volts, self.scan_hz, header.actual_timer_hz, self.channels, self.positions,
                          self.range_code, self.oversample, self.drop_first, diagnostics, start,
                          info.serial_number, info.name, info.simulated, quiet=True)
            metadata = {"schema_version": 1, "frontend_profile": PROFILE_ID, "board_revision": "R4",
                        "board_serial": self.board_serial, "daq_serial": info.serial_number,
                        "mode": self.mode, "generation": self.generation, "channels": list(self.channels),
                        "commanded": list(self.commanded), "readback": self.readback,
                        "range_code": self.range_code, "oversample_extra": self.oversample,
                        "drop_first": self.drop_first, "actual_sample_rate_hz": cap.rate_hz,
                        "nominal_sample_rate_hz": self.scan_hz, "settling_remaining_at_start_s": remaining,
                        "simulated": info.simulated, "diagnostic_reason": diagnostic_reason,
                        "integrity_final": diagnostics is not None, "integrity_reasons": reasons,
                        "diagnostics": asdict(diagnostics) if diagnostics else None,
                        "calibration": self.calibration, "recorded_at": start}
            self.last_capture, self.last_metadata = cap, metadata
            return cap, metadata

    def capture(self, seconds: float, *args: Any, **kwargs: Any) -> Capture:
        raise daq.StreamStateError("Use capture_r4() so control, settling and calibration metadata are retained")

    def live_stream(self, **kwargs: Any) -> Any:
        raise daq.StreamStateError("Use the R4 viewer/capture owner; the legacy viewer does not apply R4 calibration")

    def close(self) -> None:
        with self._owner:
            try:
                self._stop_and_drain()
                if self.connected:
                    self.device.write_r4_controls(False, False)
            finally:
                self.mode = None
                self.settle_until = float("inf")
                super().close()


def validate_calibration(data: dict[str, Any]) -> dict[str, Any]:
    """Strict measured transfer; phase allows reproducible envelope metrics."""
    record = json.loads(json.dumps(data, allow_nan=False))
    if (record.get("schema_version") != 1 or record.get("frontend_profile") != PROFILE_ID or
            set(record.get("modes", {})) != set(MODES)):
        raise ValueError("Calibration must be R4 schema 1 with exactly dc and noise modes")
    for field in ("calibration_id", "board_serial", "daq_serial", "provenance"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"Calibration requires {field}")
    for mode in MODES:
        acquisition = record.get("acquisition", {}).get(mode, {})
        if (set(acquisition) != {"range_code", "sample_rate_hz", "oversample_extra", "drop_first"} or
                not 0 <= acquisition["range_code"] <= 7 or
                not math.isfinite(acquisition["sample_rate_hz"]) or acquisition["sample_rate_hz"] <= 200 or
                not 0 <= acquisition["drop_first"] <= acquisition["oversample_extra"] <= 255):
            raise ValueError("Calibration requires a valid acquisition fingerprint for each mode")
        for channel, entry in record["modes"][mode].items():
            if not channel.isdigit() or not 0 <= int(channel) < 50:
                raise ValueError("Calibration channels must be 0..49")
            f, gain, phase = (np.asarray(entry[key], dtype=float)
                              for key in ("frequency_hz", "gain_v_per_v", "phase_deg"))
            if (f.ndim != 1 or len(f) < 2 or gain.shape != f.shape or phase.shape != f.shape or
                    not np.all(np.isfinite(np.concatenate((f, gain, phase)))) or
                    not np.all(np.diff(f) > 0) or f[0] > 1 or f[0] <= 0 or f[-1] < 100 or
                    np.any(gain <= 0)):
                raise ValueError("Each transfer must cover 1..100 Hz with finite increasing frequency and positive measured gain")
            uncertainty = float(entry["relative_uncertainty"])
            if not math.isfinite(uncertainty) or not 0 < uncertainty < 1:
                raise ValueError("A positive measured relative_uncertainty below 1 is required")
            if mode == "dc":
                if not math.isfinite(float(entry["offset_v"])) or not math.isfinite(float(entry["dc_gain"])) or float(entry["dc_gain"]) <= 0:
                    raise ValueError("DC calibration requires finite offset_v and positive dc_gain")
    return record


def analyze_capture(cap: Capture, metadata: dict[str, Any], *, band: tuple[float, float] = (1., 100.),
                    profile: str = "band", smooth_tau_s: float = 10., hold_s: float = 100.) -> dict[str, Any]:
    """Full-record FFT analysis at the recorded actual rate (live == replay).

    Rectangular periodogram after mean removal; inclusive bins in requested
    band. 3hz_q3 adds the normalized analog Q=3 magnitude/phase within 1..100
    Hz. This is a finite-record R4 metric, not legacy-fixture emulation.
    """
    low, high = map(float, band)
    if not (1 <= low < high <= 100 and cap.rate_hz > 2 * high):
        raise ValueError("Band must lie within 1..100 Hz and below recorded Nyquist")
    if profile not in ("band", "3hz_q3") or smooth_tau_s < 0 or hold_s <= 0:
        raise ValueError("Invalid profile, smoothing or hold interval")
    if metadata.get("frontend_profile") != PROFILE_ID or metadata.get("mode") not in MODES:
        raise ValueError("R4 analysis requires explicitly recorded R4 metadata")
    if cap.samples < 2:
        return {"status": "INVALID", "reasons": ["Insufficient samples"], "channels": []}
    reasons = list(metadata.get("integrity_reasons", []))
    if cap.samples / cap.rate_hz < 10. / low:
        reasons.append("SHORT_RECORD: fewer than ten cycles at the lower analysis edge")
    if not metadata.get("integrity_final"):
        reasons.append("Final stream integrity unavailable")
    cal = metadata.get("calibration")
    if cal is not None:
        cal = validate_calibration(cal)
        if cal["board_serial"] != metadata["board_serial"] or cal["daq_serial"] != metadata["daq_serial"]:
            raise ValueError("Saved calibration identity mismatch")
        expected = {"range_code": cap.range_code, "sample_rate_hz": cap.rate_hz,
                    "oversample_extra": cap.oversample, "drop_first": cap.drop_first}
        if cal["acquisition"][metadata["mode"]] != expected:
            reasons.append("UNCALIBRATED_CONFIGURATION: ADC range/rate/conversions differ from measured calibration")
            cal = None
    spec = daq.range_spec(cap.range_code)
    frequency = np.fft.rfftfreq(cap.samples, 1. / cap.rate_hz)
    selected = (frequency >= low) & (frequency <= high)
    results = []
    for index, channel in enumerate(cap.channels):
        raw = cap.volts[index]
        local = list(reasons)
        clipped = int(np.count_nonzero((raw <= spec.low_v + spec.lsb_v) | (raw >= spec.high_v - 2*spec.lsb_v)))
        if clipped:
            local.append(f"ADC_CLIPPING: {clipped} samples")
        # Feedback protection is characterized only inside this output margin.
        # Check signed raw voltage before filtering or mean removal so an
        # offset or an out-of-band disturbance cannot hide an overload.
        if metadata["mode"] == "noise" and np.any(np.abs(raw) > 0.8):
            local.append("NOISE_HEADROOM: raw output exceeds +/-0.8 V prototype limit")
        if not np.all(np.isfinite(raw)):
            local.append("NONFINITE_SAMPLES")
        entry = cal["modes"][metadata["mode"]].get(str(channel)) if cal else None
        finite = bool(np.all(np.isfinite(raw)))
        result = {"channel": channel, "raw_mean_v": float(np.mean(raw)) if finite else None,
                  "raw_peak_to_peak_v": float(np.ptp(raw)) if finite else None, "clipped_samples": clipped,
                  "reasons": local, "acceptance": "NO_LIMIT"}
        if not entry or not np.all(np.isfinite(raw)):
            result["status"] = "INVALID" if local else "UNCALIBRATED"
            results.append(result)
            continue
        if metadata["mode"] == "dc":
            result["input_dc_v"] = float((np.mean(raw) - entry["offset_v"]) / entry["dc_gain"])
        spectrum = np.fft.rfft(raw - np.mean(raw))
        transfer = np.interp(frequency, entry["frequency_hz"], entry["gain_v_per_v"])
        phase = np.interp(frequency, entry["frequency_hz"], np.unwrap(np.deg2rad(entry["phase_deg"])))
        corrected = np.zeros_like(spectrum)
        corrected[selected] = spectrum[selected] / (transfer[selected] * np.exp(1j*phase[selected]))
        if profile == "3hz_q3":
            s = 1j * frequency[selected] / 3.0
            corrected[selected] *= (s / 3.0) / (s*s + s/3.0 + 1)
        signal_uv = np.fft.irfft(corrected, n=cap.samples) * 1e6
        power = np.abs(corrected) ** 2 / (cap.rate_hz * cap.samples) * 1e12
        power[1:-1 if cap.samples % 2 == 0 else None] *= 2
        rms = float(np.sqrt(np.mean(signal_uv**2)))
        quarter = cap.samples // 4
        blocks = [float(np.sqrt(np.mean(signal_uv[i*quarter:(i+1)*quarter]**2))) for i in range(4)]
        if quarter and max(blocks) > max(1e-12, min(blocks)) * 1.5:
            local.append("NONSTATIONARY: quarter-record RMS ratio exceeds 1.5")
        alpha = -math.expm1(-1/(cap.rate_hz*smooth_tau_s)) if smooth_tau_s else 1.
        smoothed, held = 0., 0.
        # Initialize at zero; hold ends at the requested interval and freezes.
        for i, value in enumerate(np.abs(signal_uv)):
            smoothed += alpha * (float(value) - smoothed)
            if i / cap.rate_hz < hold_s:
                held = max(held, smoothed)
        result.update({"status": "INVALID" if local else "CALIBRATED_NO_LIMIT",
                       "input_band_rms_uv": rms, "input_mean_abs_uv": float(np.mean(np.abs(signal_uv))),
                       "input_smoothed_abs_final_uv": smoothed, "input_held_peak_uv": held,
                       "hold_complete": cap.samples / cap.rate_hz >= hold_s,
                       "smooth_settled": smooth_tau_s == 0 or cap.samples / cap.rate_hz >= 5*smooth_tau_s,
                       "relative_calibration_uncertainty": entry["relative_uncertainty"],
                       "frequency_hz": frequency[selected].tolist(), "input_psd_uv2_per_hz": power[selected].tolist()})
        results.append(result)
    return {"method_id": "r4_finite_record_fft_v1", "profile": profile, "band_hz": [low, high],
            "actual_sample_rate_hz": cap.rate_hz, "smooth_tau_s": smooth_tau_s, "hold_s": hold_s,
            "simulated": cap.simulated, "acceptance": "NO_LIMIT", "channels": results}


def save_capture(path: str | Path, cap: Capture, metadata: dict[str, Any]) -> Path:
    path = Path(path).with_suffix(".npz")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation keeps earlier raw evidence intact, including when
    # an operator accidentally selects the same output twice.
    with path.open("xb") as handle:
        np.savez_compressed(handle, waveform_v=cap.volts, sample_rate_hz=cap.rate_hz,
                            channels=np.asarray(cap.channels), positions=np.asarray(cap.positions),
                            r4_metadata_json=np.array(json.dumps(metadata, allow_nan=False, sort_keys=True)))
    return path


def load_capture(path: str | Path) -> tuple[Capture, dict[str, Any]]:
    with np.load(path, allow_pickle=False) as record:
        if "r4_metadata_json" not in record:
            raise ValueError("Historical capture has no R4 metadata; use its legacy replay tool")
        metadata = json.loads(str(record["r4_metadata_json"].item()))
        if metadata.get("frontend_profile") != PROFILE_ID or metadata.get("mode") not in MODES:
            raise ValueError("Invalid R4 capture profile/mode")
        diag = daq.StreamDiagnostics(**metadata["diagnostics"]) if metadata.get("diagnostics") else None
        cap = Capture(record["waveform_v"].copy(), metadata["nominal_sample_rate_hz"], float(record["sample_rate_hz"]),
                      tuple(record["channels"]), tuple(record["positions"]), metadata["range_code"],
                      metadata["oversample_extra"], metadata["drop_first"], diag, metadata["recorded_at"],
                      metadata["daq_serial"], "USB-AIO16-64MA", metadata["simulated"], quiet=True)
    if list(cap.channels) != metadata["channels"] or cap.rate_hz != metadata["actual_sample_rate_hz"]:
        raise ValueError("Capture metadata/rate/channel mismatch")
    return cap, metadata
