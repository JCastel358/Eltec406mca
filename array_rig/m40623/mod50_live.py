"""Continuous M25/HUB noise preview using the tray check's settling policy.

This worker owns the DAQ until stopped. It keeps only a rolling waveform in
memory; it does not create detector results, yield history, or reports.
"""
from __future__ import annotations

import contextlib
import copy
from dataclasses import asdict
import threading
import time

import numpy as np

import mod50_hardware as hw
from mod50_check import NOISE_SECONDS as PREVIEW_RECORD_SECONDS
from mod50_positions import detector_position, normalize_box_side
from mod50_settling import NoiseSettlingMonitor, MAX_SECONDS

WINDOW_SECONDS = 10.0
TRACE_POINTS = 200


def waveform_envelope(values, rate, first_sample, max_points=TRACE_POINTS):
    """Bound display size while retaining each bin's min and max in time order."""
    values = np.asarray(values, dtype=float)
    if not values.size:
        return {"time_s": [], "voltage_v": []}
    bins = min(len(values), max(1, max_points // 2))
    edges = np.linspace(0, len(values), bins + 1, dtype=int)
    indexes = []
    for start, end in zip(edges[:-1], edges[1:]):
        segment = values[start:end]
        indexes.extend(sorted({start + int(np.argmin(segment)), start + int(np.argmax(segment))}))
    points = np.asarray(indexes, dtype=int)
    return {"time_s": ((first_sample + points) / rate).tolist(), "voltage_v": values[points].tolist()}


class LiveNoiseViewer(threading.Thread):
    """Start explicitly, inspect status, and stop before another DAQ owner starts."""
    def __init__(self, *, simulate=False, installed_positions=hw.POSITIONS,
                 box_side="top", rig_factory=hw.Mod50Rig, lease_factory=None,
                 pace_demo=True):
        super().__init__(name="eltec-mod50-live-owner", daemon=False)
        self.positions = hw.normalize_positions(installed_positions)
        if not self.positions:
            raise ValueError("Select at least one loaded detector for the live viewer.")
        if type(simulate) is not bool:
            raise ValueError("Select Actual detectors or Generated demo.")
        self.simulate = simulate
        self.box_side = normalize_box_side(box_side)
        self.rig_factory = rig_factory
        self.lease_factory = lease_factory or (contextlib.nullcontext if simulate else hw.DaqLease)
        self.pace_demo = pace_demo
        self.stop_event, self.finished = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.result, self.directory = None, None
        self.state = {"phase": "Ready", "message": "Start the live viewer to watch noise settling.",
                      "progress": 0., "elapsed_s": 0., "ready": False, "first_ready_s": None,
                      "would_record": False, "recording_start_s": None, "recording_remaining_s": None,
                      "deadline_fallback": False, "preview_complete": False,
                      "settling": {}, "rows": [], "errors": [], "restart_required": False,
                      "box_side": self.box_side, "simulate": self.simulate,
                      "saved": False, "diagnostic_only": True}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def status(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def stop(self):
        self.stop_event.set()

    def _publish(self, monitor, window, rate, samples, first_ready, record_start, fallback):
        evidence = monitor.evidence()
        channels = {item["position"]: item for item in evidence.get("channels", [])}
        rows = []
        for logical, position in enumerate(hw.POSITIONS):
            installed = position in self.positions
            channel = channels.get(position, {})
            y = window[logical]
            row = {**hw.channel_identity(logical), **channel,
                   "detector_position": detector_position(position, self.box_side),
                   "installed": installed, "ready": bool(channel.get("stable", False)),
                   "state": channel.get("state", "collecting") if installed else "excluded",
                   "mean_v": channel.get("mean_v", float(np.mean(y)) if len(y) else None),
                   "slope_v_per_s": channel.get("slope_v_per_s"),
                   "clipped": bool(channel.get("clipped", False)),
                   "trace": waveform_envelope(y, rate, samples - window.shape[1])}
            rows.append(row)
        elapsed = samples / rate
        remaining = max(0., PREVIEW_RECORD_SECONDS - (elapsed - record_start)) if record_start is not None else None
        complete = remaining == 0. if remaining is not None else False
        ready = bool(evidence.get("complete"))
        count, required = evidence.get("stable_count", 0), evidence.get("required_count", 0)
        if record_start is None:
            message = f"Watching noise baseline · {count}/{len(self.positions)} ready · {required} needed to start"
        elif complete:
            message = "The 60-second recording would be complete. The live viewer continues until Stop."
        elif fallback:
            message = f"90-second fallback reached · example recording has {remaining:.1f} seconds left"
        else:
            message = f"Ready at {record_start:.1f} seconds · example recording has {remaining:.1f} seconds left"
        self.update(phase="Live noise", message=message, elapsed_s=elapsed,
                    progress=min(1., elapsed / MAX_SECONDS) if record_start is None else 1. - remaining / PREVIEW_RECORD_SECONDS,
                    ready=ready, first_ready_s=first_ready, would_record=record_start is not None,
                    recording_start_s=record_start, recording_remaining_s=remaining,
                    deadline_fallback=fallback, preview_complete=complete, settling=evidence, rows=rows)

    def run(self):
        rig, errors, diagnostics = None, [], None
        restart_required = False
        try:
            if self.stop_event.is_set():
                return
            self.update(phase="Connecting", message="Checking both M25 cards for the live viewer")
            with self.lease_factory():
                try:
                    if self.stop_event.is_set():
                        return
                    rig = self.rig_factory(simulate=self.simulate)
                    info = rig.connect()
                    if info.simulated != self.simulate:
                        raise hw.daq.StreamStateError("Selected source and connected device disagree")
                    if self.stop_event.is_set():
                        return
                    rig.set_frontend_mode("noise")
                    if rig.range_code != hw.NOISE_RANGE:
                        raise hw.daq.StreamIntegrityError("Unexpected live-viewer DAQ range")
                    rig._verify(rig.commanded)
                    expected = (rig.mode, rig.range_code, rig.commanded, rig.generation)
                    config = copy.deepcopy(rig.device.config)
                    if self.stop_event.is_set():
                        return
                    header = rig.device.start_stream(scan_hz=hw.SAMPLE_HZ, average=False, drop_first=1,
                        buffer_bytes=hw._live_callback_buffer_bytes(rig), buffer_count=rig.buffer_count)
                    rate = header.actual_timer_hz
                    if not np.isfinite(rate) or rate <= 0 or abs(rate / hw.SAMPLE_HZ - 1) > .01:
                        raise hw.daq.StreamIntegrityError("DAQ sample clock differs by more than 1%")
                    monitor = NoiseSettlingMonitor(rate, hw.daq.lsb_volts(hw.NOISE_RANGE), self.positions, trace_limit=90)
                    indexes = [hw.MAPPING[hw.POSITIONS.index(p)] for p in self.positions]
                    window = np.empty((50, 0), dtype=float)
                    samples, first_ready, record_start, fallback = 0, None, None, False
                    started = last_data = time.monotonic()
                    publish_after = 0
                    while not self.stop_event.is_set():
                        rig._verify(rig.commanded)
                        if expected != (rig.mode, rig.range_code, rig.commanded, rig.generation) or config != rig.device.config:
                            raise hw.daq.StreamIntegrityError("Signal path or DAQ configuration changed during live viewing")
                        raw = rig.device.read_stream(timeout_s=.1)
                        if raw is not None:
                            volts, clipping = hw.convert_chunk(raw, hw.NOISE_RANGE)
                            samples += len(raw)
                            last_data = time.monotonic()
                            if self.simulate and self.pace_demo:
                                self.stop_event.wait(max(0., started + samples / rate - time.monotonic()))
                            monitor.feed(volts[indexes], clipping[indexes], stop_when_ready=False)
                            evidence = monitor.evidence()
                            if first_ready is None:
                                first_ready = evidence.get("first_ready_s")
                            if record_start is None:
                                if first_ready is not None and first_ready <= MAX_SECONDS:
                                    record_start = first_ready
                                elif samples / rate >= MAX_SECONDS:
                                    record_start, fallback = MAX_SECONDS, True
                            window = np.concatenate((window, volts[list(hw.MAPPING)]), axis=1)[:, -round(WINDOW_SECONDS * rate):]
                            if samples >= publish_after or self.stop_event.is_set():
                                self._publish(monitor, window, rate, samples, first_ready, record_start, fallback)
                                publish_after = samples + round(rate * .25)
                        diag = getattr(rig.device, "_diagnostics", None)
                        if diag is not None and (diag.pool_too_small_events or diag.callback_error or diag.ended_early):
                            raise hw.daq.StreamIntegrityError("DAQ delivery interrupted during live viewing")
                        if time.monotonic() - last_data > 2.:
                            raise hw.daq.StreamTimeoutError("No fresh DAQ samples for two seconds")
                finally:
                    if rig is not None:
                        self.update(phase="Returning to DC", message="Stopping the live viewer and releasing the DAQ")
                        try:
                            if rig.device.is_streaming:
                                diag = rig.device.stop_stream()
                                diagnostics = asdict(diag)
                                errors.extend(diag.problems())
                                # A nonzero shutdown status is enough to block a
                                # new owner, even if the driver did not set its
                                # narrower bulk-end failure flag.
                                statuses = (diag.clock_stop_status, diag.bulk_end_status, diag.bulk_worker_status)
                                restart_required |= bool(any(value not in (None, 0) for value in statuses)
                                    or diag.cleanup_error or getattr(rig.device, "_shutdown_failed", False)
                                    or (getattr(rig.device, "_consumer", None) and rig.device._consumer.is_alive()))
                                rig.device.drain_stream()
                            if rig.connected:
                                rig._verify(rig.commanded)
                        except Exception as exc:
                            errors.append("Final stream check: " + str(exc))
                        try:
                            rig.close()
                        except Exception as exc:
                            errors.append("DAQ shutdown: " + str(exc))
                            restart_required = True
                        restart_required |= bool(getattr(rig.device, "_shutdown_failed", False))
        except Exception as exc:
            errors.append(str(exc))
        finally:
            self.update(phase="Live viewer stopped" if not errors else "Live viewer error",
                        message="Live viewer stopped. No detector results were saved." if not errors else "; ".join(errors),
                        ready=False, errors=errors, restart_required=restart_required,
                        power_readiness_failed=bool(getattr(rig, "power_readiness_failed", False)),
                        diagnostics=diagnostics, stopped=True)
            self.result = self.status()
            self.finished.set()
