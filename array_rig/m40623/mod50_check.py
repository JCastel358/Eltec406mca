"""Settled DC/noise tray checks. One worker owns the DAQ from start to close."""
from __future__ import annotations

import contextlib
import base64
import copy
import csv
from dataclasses import asdict
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

import numpy as np

import mod50_hardware as hw
import mod50_legacy_noise as legacy_meter
from mod50_history import History
from mod50_settling import SettlingMonitor, NoiseWarmup, NOISE_WARMUP_SECONDS, MAX_SECONDS as SETTLING_MAX_SECONDS
from array_analysis import OFFSET_MIN_V, OFFSET_MAX_V

DC_SECONDS = 10.0
NOISE_SECONDS = 100.0
LEGACY_TIMING_POLICY = "mod50_connect90_dc10_route90_noise100_v1"
ADAPTIVE_TIMING_POLICY = "mod50_80percent_adaptive5to90_fallback_dc10_noise100_v2"
TIMING_POLICY = "mod50_dc_adaptive5to90_noise_fixed90_capture100_v3"
SUPPORTED_TIMING_POLICIES = (LEGACY_TIMING_POLICY, ADAPTIVE_TIMING_POLICY, TIMING_POLICY)
SCHEMA = "ELTEC_MOD50_TRAY_CHECK_1"
PRESENCE_POLICY = "two_separated_one_second_dc_windows_within_one_adc_code_v1"
NO_SIGNAL_NOTE = "No detector sensed: the slot may be empty, or the detector may be dead or disconnected."
# A failed transport attempt is retained separately, then the whole stage is
# recorded again on the unchanged, still-settled signal path. This never
# joins raw segments or changes the existing clock/rate acceptance checks.
MAX_STAGE_ATTEMPTS = 3
RECOVERY_POLICY = "fresh_stage_after_delivery_failure_max3_unchanged_settled_path_v1"


def results_root(simulate=False):
    override = os.environ.get("ELTEC_ARRAY_RESULTS_ROOT")
    if override:
        root = Path(override) / "mod50_checks"
        return root / "simulation" if simulate else root
    if simulate:
        return Path(tempfile.gettempdir()) / "eltec-array-simulation" / "mod50_checks"
    return Path.home() / "Documents" / "Eltec_40623_Test_Results" / "40623_array_daq" / "mod50_checks"


class SimulationClock:
    """Advance scheduled waits without shortening real-hardware timing."""
    def __init__(self):
        self.now = time.monotonic()

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def display_trace(y, rate, *, first_sample=0, max_points=600):
    """Time-ordered bucket minima/maxima, retaining endpoints and brief faults.

    This is only display reduction. All statistics use full-resolution data;
    the complete four-conversion data remain in the raw NPZ.
    """
    y = np.asarray(y, dtype=float)
    if len(y) <= max_points:
        indexes = np.arange(len(y))
    else:
        edges = np.linspace(0, len(y), max_points // 2, dtype=int)
        indexes = [0, len(y) - 1]
        for start, stop in zip(edges[:-1], edges[1:]):
            values = y[start:stop]
            indexes.extend((start + int(np.argmin(values)), start + int(np.argmax(values))))
        indexes = np.asarray(sorted(set(indexes)), dtype=int)
    return {"time_s": ((indexes + first_sample) / rate).tolist(), "volts": y[indexes].tolist()}


def dc_detail(y, rate):
    t = np.arange(len(y), dtype=float) / rate
    centered_t = t - t.mean()
    denominator = float(np.dot(centered_t, centered_t))
    slope = float(np.dot(centered_t, y - y.mean()) / denominator) if denominator else 0.
    window = max(1, min(len(y), round(rate)))
    start, end = float(np.mean(y[:window])), float(np.mean(y[-window:]))
    # Extrapolation is descriptive only: it never extends the test, changes a
    # verdict or promises that a transient will keep moving at the same rate.
    estimate = None
    if end < OFFSET_MIN_V and slope > 0:
        estimate = float((OFFSET_MIN_V - end) / slope)
    elif end > OFFSET_MAX_V and slope < 0:
        estimate = float((OFFSET_MAX_V - end) / slope)
    if estimate is not None and (not np.isfinite(estimate) or estimate < 0):
        estimate = None
    trend = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
    return {**display_trace(y, rate, max_points=400), "min_v": float(y.min()), "max_v": float(y.max()),
            "slope_v_per_s": slope, "start_v": start, "end_v": end, "trend": trend,
            "estimated_seconds_to_window": estimate,
            "prediction_note": "Linear projection from this recorded interval only; continued drift and a future pass are not guaranteed.",
            "limit_min_v": OFFSET_MIN_V, "limit_max_v": OFFSET_MAX_V,
            "recorded_seconds": len(y) / rate}


def presence_evidence(y, rate, range_code, *, valid_data, clipped):
    size = max(1, round(rate))
    threshold = float(hw.daq.lsb_volts(range_code))
    evidence = {"policy": PRESENCE_POLICY, "threshold_v": threshold,
                "valid_dc_acquisition": bool(valid_data), "windows": [],
                "note": "Inference only; no electrical presence sensor is fitted."}
    if not valid_data or clipped:
        return "UNKNOWN", evidence
    if len(y) < 4 * size:
        return "UNKNOWN", evidence
    for start in (0, len(y) - size):
        values = y[start:start + size]
        evidence["windows"].append({"start_s": start / rate, "end_s": (start + size) / rate,
            "median_v": float(np.median(values)), "min_v": float(values.min()), "max_v": float(values.max()),
            "within_one_code": bool(np.all(np.abs(values) <= threshold))})
    if all(window["within_one_code"] for window in evidence["windows"]):
        return "NO_SIGNAL", evidence
    return "SIGNAL", evidence


def noise_detail(y, clipping, rate, *, valid_data=True):
    width = min(len(y), max(1, round(4 * rate)))
    offending = np.flatnonzero(clipping | (np.abs(y) > .8))
    if len(offending):
        center = int(offending[0])
        reason = "First range-limit or noise-headroom violation"
    elif not valid_data:
        center = len(y) - 1
        reason = "Last recorded samples of an incomplete acquisition"
    else:
        center = len(y) // 2
        reason = "Representative middle-of-record excerpt; no noise acceptance limit is configured"
    start = max(0, min(len(y) - width, center - width // 2))
    values = y[start:start + width]
    return {**display_trace(values, rate, first_sample=start), "start_s": start / rate,
            "end_s": (start + width - 1) / rate, "selection_reason": reason,
            "min_v": float(values.min()), "max_v": float(values.max()),
            "event_time_s": float(center / rate) if len(offending) else None,
            "recorded_seconds": len(y) / rate}


def channel_statistics(raw, range_code, rate, mode, *, valid_data=True, excluded_positions=()):
    volts, clipping = hw.convert_chunk(raw, range_code)
    rows = []
    legacy_channels = []
    for logical, physical in enumerate(hw.MAPPING):
        y = volts[physical]
        row = {**hw.channel_identity(logical), "mean_v": float(np.mean(y)),
               "min_v": float(np.min(y)), "max_v": float(np.max(y)),
               "peak_to_peak_mv": float(np.ptp(y) * 1000),
               "near_range_limit": bool(np.any(clipping[physical])), "flags": []}
        if row["near_range_limit"]:
            row["flags"].append("At/near DAQ range limit")
        if np.ptp(y) < hw.daq.lsb_volts(range_code):
            row["flags"].append("Flat trace; check detector/connection")
        if mode == "dc":
            row["dc_detail"] = dc_detail(y, rate)
            row["presence"], row["presence_evidence"] = presence_evidence(
                y, rate, range_code, valid_data=valid_data, clipped=row["near_range_limit"])
        if mode == "noise":
            row.update(legacy_meter.unavailable())
            row["noise_detail"] = noise_detail(y, clipping[physical], rate, valid_data=valid_data)
            headroom = bool(np.any(np.abs(y) > .8))
            row["frontend_headroom_exceeded"] = headroom
            if headroom:
                row["flags"].append("Outside +/-0.8 V noise headroom")
            if hw.POSITIONS[logical] in excluded_positions:
                row.update(analysis_excluded=True, daq_1_100_hz_rms_uv=None, daq_3hz_q3_rms_uv=None)
                row.update(legacy_meter.unavailable("EXCLUDED", "Noise analysis excluded for this slot."))
                rows.append(row)
                continue
            # Entire finite record, mean removed, inclusive 1..100 Hz bins.
            # Q=3 is a named RMS metric, not emulation of the legacy DMM hold.
            centered = y - np.mean(y)
            freq = np.fft.rfftfreq(len(y), 1.0 / rate)
            power = np.abs(np.fft.rfft(centered)) ** 2 / len(y) ** 2
            power[1:-1 if len(y) % 2 == 0 else None] *= 2
            selected = (freq >= 1) & (freq <= 100)
            ratio = freq / 3.0
            weight = (ratio / 3.0) ** 2 / ((1 - ratio ** 2) ** 2 + (ratio / 3.0) ** 2)
            invalid = row["near_range_limit"] or headroom
            row["daq_1_100_hz_rms_uv"] = None if invalid else float(np.sqrt(np.sum(power[selected])) * 1e6)
            row["daq_3hz_q3_rms_uv"] = None if invalid else float(np.sqrt(np.sum(power[selected] * weight[selected])) * 1e6)
            if not valid_data:
                row.update(legacy_meter.unavailable("INVALID_ACQUISITION", "Incomplete or invalid noise recording; raw evidence is retained."))
            elif invalid or np.ptp(y) < hw.daq.lsb_volts(range_code):
                row.update(legacy_meter.unavailable("INVALID_SIGNAL", "Clipped, over-range or flat noise signal; check the recorded waveform."))
            else:
                legacy_channels.append((logical, physical))
        rows.append(row)
    if legacy_channels:
        # One full-rate, multichannel pass. Do not use FFT RMS or display
        # excerpts as input to the nonlinear rectifier/smoother/hold model.
        estimates = legacy_meter.analyze(volts[[p for _, p in legacy_channels]], rate)
        for (logical, _), estimate in zip(legacy_channels, estimates):
            rows[logical].update(estimate)
    return rows


def normalize_tests(tests):
    if not isinstance(tests, (list, tuple)) or not tests or any(m not in ("dc", "noise") for m in tests) or len(set(tests)) != len(tests):
        raise ValueError("Choose Offset, Noise, or both tests.")
    return tuple(m for m in ("dc", "noise") if m in tests)


def combine_rows(records, positions, complete, selected_tests=("dc", "noise")):
    by_mode = {record["mode"]: record for record in records}
    rows = []
    for logical in range(50):
        row = {**hw.channel_identity(logical), "installed": hw.POSITIONS[logical] in positions,
               "dc_v": None, "dc_check": "NOT_MEASURED" if "dc" in selected_tests else "NOT_TAKEN", "daq_3hz_q3_rms_uv": None,
               "daq_1_100_hz_rms_uv": None, "result": "INCOMPLETE", "flags": [],
               "dc_detail": None, "noise_detail": None, "presence": "UNKNOWN", "presence_evidence": None,
               "noise_analysis_excluded": False, "acquisition_validity": {"dc": False, "noise": False},
               "acquisition_errors": [],
               "test_status": {m: "NOT_MEASURED" if m in selected_tests else "NOT_TAKEN" for m in ("dc", "noise")}}
        row.update(legacy_meter.unavailable("NOT_MEASURED" if "noise" in selected_tests else "NOT_TAKEN",
                                           "No valid noise recording is available." if "noise" in selected_tests else "Noise was not selected."))
        if not row["installed"]:
            row.update(result="NOT_INSTALLED", dc_check="NOT_INSTALLED",
                       test_status={"dc": "NOT_INSTALLED", "noise": "NOT_INSTALLED"})
            row.update(legacy_meter.unavailable("EXCLUDED", "No detector declared in this slot."))
            rows.append(row)
            continue
        for mode in ("dc", "noise"):
            record = by_mode.get(mode)
            if not record:
                continue
            row["acquisition_validity"][mode] = bool(record["valid_data"])
            row["test_status"][mode] = "MEASURED" if record["valid_data"] else "INCOMPLETE"
            if not record["valid_data"]:
                row["acquisition_errors"].extend(mode.upper() + ": " + reason for reason in
                                                (record.get("errors") or ["Acquisition incomplete"]))
            if len(record["rows"]) != 50:
                continue
            measured = record["rows"][logical]
            detail = copy.deepcopy(measured.get(mode + "_detail"))
            if detail is not None:
                detail["valid_acquisition"] = bool(record["valid_data"])
            row[mode + "_detail"] = detail
            if mode == "dc":
                row["presence_evidence"] = copy.deepcopy(measured.get("presence_evidence"))
            if not record["valid_data"]:
                continue
            row["flags"].extend(mode.upper() + ": " + flag for flag in measured["flags"])
            observation = record.get("stability_observation", {})
            if mode == "dc" and len(observation.get("trace", [])) >= 5 and row["position"] in observation.get("never_settled_positions", []):
                row["flags"].append(mode.upper() + ": Signal never settled during this test; check/retest part or slot")
            if mode == "dc":
                row["presence"] = measured.get("presence", "UNKNOWN")
                row["dc_v"] = measured["mean_v"]
                row["dc_check"] = ("RANGE_LIMIT" if measured["near_range_limit"] else
                                   "IN_WINDOW_PROVISIONAL" if OFFSET_MIN_V <= measured["mean_v"] <= OFFSET_MAX_V else
                                   "OUTSIDE_WINDOW_PROVISIONAL")
                if row["dc_check"] != "IN_WINDOW_PROVISIONAL":
                    row["flags"].append("DC: outside provisional 0.3-1.2 V window")
            else:
                for key in ("daq_3hz_q3_rms_uv", "daq_1_100_hz_rms_uv"):
                    row[key] = measured[key]
                for key in legacy_meter.RESULT_FIELDS:
                    if key in measured:
                        row[key] = copy.deepcopy(measured[key])
        if complete:
            row["result"] = "CHECK" if row["flags"] else "REVIEW_NO_NOISE_LIMIT" if "noise" in selected_tests else "PASS_OFFSET_ONLY"
        if row["presence"] == "NO_SIGNAL":
            row.update(result="NO_SIGNAL", dc_check="NO_SIGNAL", noise_analysis_excluded=True,
                       daq_1_100_hz_rms_uv=None, daq_3hz_q3_rms_uv=None)
            row.update(legacy_meter.unavailable("EXCLUDED", NO_SIGNAL_NOTE))
            row["flags"].append(NO_SIGNAL_NOTE)
        rows.append(row)
    return rows


def interactive_report(result):
    """Self-contained, offline version of the same interactive operator UI."""
    ui = Path(__file__).with_name("mod50_ui")
    html = (ui / "index.html").read_text(encoding="utf-8")
    css = (ui / "styles.css").read_text(encoding="utf-8")
    js = (ui / "app.js").read_text(encoding="utf-8")
    logo = base64.b64encode((ui / "assets" / "eltec-logo-transparent.webp").read_bytes()).decode("ascii")
    # Escaping literal '<' prevents lot/operator text from closing the script
    # element. The renderer uses textContent for all measurement text.
    payload = json.dumps(result, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
    html = html.replace('href="styles.css"', 'data-embedded-styles="true"')
    html = html.replace('src="assets/eltec-logo-transparent.webp"', 'src="data:image/webp;base64,' + logo + '"')
    html = html.replace('</head>', '<style>' + css + '</style></head>')
    html = html.replace('<script src="app.js"></script>', '<script>window.ELTEC_REPORT = ' + payload + ';</script><script>' + js + '</script>')
    return html


def write_report(directory, result):
    """Persist every position plus the replayable trend/excerpt evidence."""
    directory = Path(directory)
    payload = json.dumps(result, indent=2, allow_nan=False)
    fields = ("channel", "position", "card", "card_channel", "physical_daq_channel", "daq_jack",
              "installed", "presence", "noise_analysis_excluded", "dc_v", "dc_check",
              "daq_3hz_q3_rms_uv", "daq_1_100_hz_rms_uv", *legacy_meter.VALUE_FIELDS,
              "legacy_equivalent_method_id", "legacy_equivalent_status", "result", "flags", "acquisition_errors",
              "acquisition_validity", "test_status", "offset_status", "noise_status", "part_id")
    detail_fields = ("dc_min_v", "dc_max_v", "dc_slope_v_per_s", "dc_start_v", "dc_end_v",
                     "dc_estimated_seconds_to_window", "noise_excerpt_start_s", "noise_excerpt_end_s",
                     "noise_excerpt_reason", "legacy_readout_elapsed_s", "legacy_reference_readout_elapsed_s",
                     "legacy_estimate_note", "legacy_noise_method")
    csv_partial = directory / "results.csv.partial"
    with csv_partial.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=("lot", "tray", "operator", "simulated", "run_id", "family_id", "selected_tests", *fields, *detail_fields))
        writer.writeheader()
        for row in result["rows"]:
            values = {field: row.get(field) for field in fields}
            dc, noise = row.get("dc_detail") or {}, row.get("noise_detail") or {}
            values.update({"dc_" + key: dc.get(key) for key in
                           ("min_v", "max_v", "slope_v_per_s", "start_v", "end_v", "estimated_seconds_to_window")})
            values.update(noise_excerpt_start_s=noise.get("start_s"), noise_excerpt_end_s=noise.get("end_s"),
                          noise_excerpt_reason=noise.get("selection_reason"))
            legacy = row.get("legacy_equivalent_detail") or {}
            values.update(legacy_readout_elapsed_s=legacy.get("readout_elapsed_s"),
                          legacy_reference_readout_elapsed_s=legacy.get("reference_readout_elapsed_s"),
                          legacy_estimate_note=legacy.get("reason", "Unvalidated estimate; noise pass/fail disabled."),
                          legacy_noise_method=json.dumps(result.get("legacy_noise_method"), sort_keys=True))
            writer.writerow({"lot": result["lot"], "tray": result["tray"], "operator": result["operator"],
                             "run_id": result.get("run_id"), "family_id": result.get("family_id"),
                             "selected_tests": ";".join(result.get("selected_tests", ["dc", "noise"])),
                             "simulated": result["simulated"], **values, "flags": "; ".join(row["flags"]),
                             "acquisition_errors": "; ".join(row.get("acquisition_errors", [])),
                             "offset_status": row.get("test_status", {}).get("dc", "NOT_MEASURED"),
                             "noise_status": row.get("test_status", {}).get("noise", "NOT_MEASURED"),
                             "test_status": json.dumps(row.get("test_status", {}), sort_keys=True),
                             "acquisition_validity": json.dumps(row.get("acquisition_validity", {}), sort_keys=True)})
    csv_partial.replace(directory / "results.csv")
    (directory / "report.html").write_text(interactive_report(result), encoding="utf-8")
    # The final summary is the completion marker after both exports exist.
    partial = directory / "summary.json.partial"
    partial.write_text(payload, encoding="utf-8")
    partial.replace(directory / "summary.json")


class TrayCheck(threading.Thread):
    def __init__(self, *, lot, operator, tray=1, installed_positions=hw.POSITIONS,
                 simulate=False, output_root=None, rig_factory=hw.Mod50Rig, lease_factory=None, resume_dc=None,
                 selected_tests=("dc", "noise"), part_mapping=None, family_id=None, excluded_suspect_slots=()):
        super().__init__(name="eltec-mod50-owner", daemon=False)
        if not str(lot).strip() or not str(operator).strip():
            raise ValueError("Enter a lot number and technician name.")
        if type(tray) is not int or tray < 1:
            raise ValueError("Tray must be a positive whole number.")
        self.positions = hw.normalize_positions(installed_positions)
        if not self.positions:
            raise ValueError("Select at least one installed detector.")
        self.lot, self.operator, self.tray = str(lot).strip(), str(operator).strip(), tray
        self.simulate = bool(simulate)
        self.selected_tests = normalize_tests(selected_tests)
        self.run_id = uuid.uuid4().hex
        self.family_id = family_id or self.run_id
        self.part_mapping = dict(part_mapping or {})
        if self.part_mapping and (set(self.part_mapping) != set(self.positions) or len(set(self.part_mapping.values())) != len(self.positions)):
            raise ValueError("Each retested part needs one unique destination slot.")
        self.excluded_suspect_slots = list(excluded_suspect_slots)
        self.output_root = Path(output_root) if output_root is not None else results_root(simulate)
        self.rig_factory = rig_factory
        self.lease_factory = lease_factory or (contextlib.nullcontext if simulate else hw.DaqLease)
        self.stop_event, self.finished = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.state = {"phase": "Ready", "progress": 0.0, "message": "", "output": "",
                      "rows": combine_rows([], self.positions, False, self.selected_tests)}
        self.records, self.result, self.directory = [], None, None
        self.resume_dc = copy.deepcopy(resume_dc)
        self._settled_path = None

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def status(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def stop(self):
        self.stop_event.set()

    def check_cancelled(self):
        if self.stop_event.is_set():
            raise hw.daq.StreamIntegrityError("Stopped by operator; the check is incomplete.")

    def wait_settled(self, rig, record, base, span, rate, raw_chunks):
        path = (rig.mode, rig.range_code, rig.commanded, rig.generation)
        positions = list(self.positions)
        if record["mode"] == "noise":
            no_signal = {r["position"] for r in combine_rows(self.records, self.positions, False, self.selected_tests)
                         if r["presence"] == "NO_SIGNAL"}
            positions = [p for p in positions if p not in no_signal]
        noise_warmup = record["mode"] == "noise"
        wait_seconds = NOISE_WARMUP_SECONDS if noise_warmup else SETTLING_MAX_SECONDS
        monitor = (NoiseWarmup(rate, positions, wait_seconds) if noise_warmup else
                   SettlingMonitor(rate, hw.daq.lsb_volts(record["range_code"]), positions, "dc"))
        if self._settled_path == path and rig.settling_remaining_s == 0:
            record["settling"] = dict(monitor.evidence(), complete=True, waited_s=0., reused_unchanged_path=True)
            return monitor
        indexes = [hw.MAPPING[hw.POSITIONS.index(p)] for p in positions]
        last_data, started, scans = time.monotonic(), time.monotonic(), 0
        record["settling"] = monitor.evidence()
        while scans < round(wait_seconds * rate):
            self.check_cancelled()
            rig._verify(rig.commanded)
            self.update(phase="Warming up NOISE" if noise_warmup else "Settling DC",
                        message=(f"Noise warm-up · {scans/rate:.1f} / {wait_seconds:g} seconds" if noise_warmup else
                                 f"Watching DC stability · {scans/rate:.1f} s · {record['settling'].get('stable_count', 0)}/{len(positions)} stable · starts at 80%"),
                        progress=base + span * min(1, scans / (wait_seconds * rate)))
            self.check_cancelled()
            try:
                raw = rig.device.read_stream(timeout_s=.1)
            except hw.daq.StreamTimeoutError:
                record["failure_kind"] = "delivery_timeout"
                raise
            if raw is not None:
                hw.convert_chunk(raw, record["range_code"])
                raw = np.array(raw[:round(wait_seconds * rate) - scans], dtype=np.uint16, copy=True)
                volts, _ = hw.convert_chunk(raw, record["range_code"])
                raw_chunks.append(raw)
                scans += len(raw)
                last_data = time.monotonic()
                stable = monitor.feed(volts[indexes])
                record["settling"] = monitor.evidence()
                record["settling"]["observed_samples"] = scans
                record["status_samples"].append({"samples": 0, "settling_samples": scans,
                                                "raw_byte": rig._verify(rig.commanded)["raw_byte"]})
                diag = getattr(rig.device, "_diagnostics", None)
                if diag is not None and (diag.pool_too_small_events or diag.callback_error or diag.ended_early):
                    record["failure_kind"] = "delivery_interrupted"
                    raise hw.daq.StreamIntegrityError("DAQ delivery interrupted during settling")
                if stable:
                    self.check_cancelled()
                    if path != (rig.mode, rig.range_code, rig.commanded, rig.generation):
                        raise hw.daq.StreamIntegrityError("Signal path changed during settling")
                    rig.settle_until = rig._clock()
                    self._settled_path = path
                    return monitor
            if time.monotonic() - last_data > 2:
                record["failure_kind"] = "delivery_timeout"
                raise hw.daq.StreamTimeoutError("No fresh DAQ samples during settling")
            if time.monotonic() - started > wait_seconds + 5:
                record["failure_kind"] = "delivery_timeout"
                raise hw.daq.StreamTimeoutError("Settling sample delivery exceeded its time limit")
        record["settling"].update(complete=True, deadline_fallback=True,
            warning="Fewer than 80% settled within 90 seconds. All channels recorded together using the fixed-wait fallback.")
        self.check_cancelled()
        rig._verify(rig.commanded)
        rig.settle_until = rig._clock()
        self._settled_path = path
        return monitor

    def capture_stage(self, rig, mode):
        seconds = DC_SECONDS if mode == "dc" else NOISE_SECONDS
        attempt = 1 + sum(previous["mode"] == mode for previous in self.records)
        record = {"mode": mode, "requested_seconds": seconds, "errors": [], "rows": [],
                  "status_samples": [], "range_code": hw.DC_RANGE if mode == "dc" else hw.NOISE_RANGE,
                  "settling": {"complete": False}, "valid_data": False, "attempt": attempt,
                  "failure_kind": None, "retry_eligible": False}
        self.records.append(record)
        raw_chunks, settling_chunks, count, rate = [], [], 0, hw.SAMPLE_HZ
        monitor = None
        expected_state, expected_config, clean_shutdown, verified_after = None, None, False, False
        target = round(seconds * rate)
        modes = ("noise",) if self.resume_dc else self.selected_tests
        base = modes.index(mode) / len(modes)
        wait_span, capture_span = .25 / len(modes), .75 / len(modes)
        capture_base = base + wait_span
        try:
            self.check_cancelled()
            if mode != rig.mode:
                self.update(phase="Switching to " + mode.upper(), message="Changing the global signal path")
                rig.set_frontend_mode(mode)
            if rig.range_code != record["range_code"]:
                raise hw.daq.StreamIntegrityError("Unexpected DAQ range")
            record["status_before"] = rig._verify(rig.commanded)
            expected_state = (rig.mode, rig.range_code, rig.commanded, rig.generation)
            expected_config = copy.deepcopy(rig.device.config)
            record["started_at"] = dt.datetime.now().astimezone().isoformat()
            header = rig.device.start_stream(scan_hz=hw.SAMPLE_HZ, average=False, drop_first=1,
                buffer_bytes=hw._live_callback_buffer_bytes(rig), buffer_count=rig.buffer_count)
            granted = header.actual_timer_hz
            if not np.isfinite(granted) or granted <= 0 or abs(granted / hw.SAMPLE_HZ - 1) > .01:
                raise hw.daq.StreamIntegrityError("DAQ sample clock differs by more than 1%")
            rate = granted
            target = round(seconds * rate)
            settled = self.wait_settled(rig, record, base, wait_span, rate, settling_chunks)
            # Only DC is judged for persistent drift. Noise has a fixed warm-up;
            # normal fluctuations in its mean or RMS are not a detector fault.
            # Every fresh recording independently resets the software meter.
            if mode == "dc":
                monitor = SettlingMonitor(rate, hw.daq.lsb_volts(record["range_code"]), settled.positions, mode)
            record["capture_started_at"] = dt.datetime.now().astimezone().isoformat()
            last_data = time.monotonic()
            while count < target:
                self.check_cancelled()
                rig._verify(rig.commanded)
                try:
                    raw = rig.device.read_stream(timeout_s=.1)
                except hw.daq.StreamTimeoutError:
                    record["failure_kind"] = "delivery_timeout"
                    raise
                if raw is not None:
                    hw.convert_chunk(raw, record["range_code"])
                    retained = np.array(raw[:target - count], dtype=np.uint16, copy=True)
                    raw_chunks.append(retained)
                    values, _ = hw.convert_chunk(retained, record["range_code"])
                    if monitor is not None:
                        monitor.feed(values[[hw.MAPPING[hw.POSITIONS.index(p)] for p in monitor.positions]], stop_when_ready=False)
                    count += len(retained)
                    last_data = time.monotonic()
                status = rig._verify(rig.commanded)
                record["status_samples"].append({"samples": count, "raw_byte": status["raw_byte"]})
                diag = getattr(rig.device, "_diagnostics", None)
                if diag is not None and (diag.pool_too_small_events or diag.callback_error or diag.ended_early):
                    record["failure_kind"] = "delivery_interrupted"
                    raise hw.daq.StreamIntegrityError("DAQ delivery interrupted: " + "; ".join(diag.problems()))
                if time.monotonic() - last_data > 2.:
                    record["failure_kind"] = "delivery_timeout"
                    raise hw.daq.StreamTimeoutError("No fresh DAQ samples for two seconds")
                self.update(phase="Recording " + mode.upper(), message=f"{count/rate:.1f} / {seconds:g} seconds",
                            progress=capture_base + capture_span * min(1., count / target))
            self.check_cancelled()
        except Exception as exc:
            record["errors"].append(str(exc))
        finally:
            record["transfer_at_stop"] = hw.transfer_snapshot(rig.device)
            try:
                if rig.device.is_streaming:
                    diag = rig.device.stop_stream()
                    record["diagnostics"] = asdict(diag)
                    record["errors"].extend(diag.problems())
                    rig.device.drain_stream()
                    statuses = (diag.clock_stop_status, diag.bulk_end_status, diag.bulk_worker_status)
                    clean_shutdown = (all(value in (None, 0) for value in statuses) if self.simulate else
                                      all(value == 0 for value in statuses))
                    clean_shutdown = bool(clean_shutdown and not diag.cleanup_error and
                        not getattr(rig.device, "_shutdown_failed", False) and not rig.device.is_streaming and
                        not (getattr(rig.device, "_consumer", None) and rig.device._consumer.is_alive()) and
                        np.isfinite(diag.actual_timer_hz) and abs(diag.actual_timer_hz / hw.SAMPLE_HZ - 1) <= .01)
                expected_controls = expected_state[2] if expected_state is not None else rig.commanded
                record["status_after"] = rig._verify(expected_controls)
                verified_after = (expected_state == (rig.mode, rig.range_code, rig.commanded, rig.generation) and
                                  expected_config == rig.device.config and
                                  (not record["settling"]["complete"] or rig.settling_remaining_s == 0))
                if expected_state is not None and not verified_after:
                    record["errors"].append("Signal path/range or settling state changed during acquisition")
            except Exception as exc:
                record["errors"].append("Final readback/shutdown: " + str(exc))
        record["retry_eligible"] = bool(record["failure_kind"] in ("delivery_timeout", "delivery_interrupted") and
            clean_shutdown and verified_after and not self.stop_event.is_set())
        raw = np.concatenate(raw_chunks) if raw_chunks else np.empty((0, 57, 4), dtype=np.uint16)
        raw_file = mode + ("_raw.npz" if attempt == 1 else f"_attempt{attempt:02d}_raw.npz")
        record.update(samples=len(raw), target_samples=target, actual_rate_hz=rate,
                      recorded_seconds=len(raw)/rate, raw_file=raw_file)
        if mode == "noise":
            record["legacy_noise_method"] = legacy_meter.method_metadata(rate)
        if len(raw) != target:
            record["errors"].append(f"Incomplete: {len(raw)} of {target} scans")
        record["valid_data"] = not record["errors"] and record["settling"]["complete"]
        if monitor is not None:
            record["stability_observation"] = monitor.evidence()
            record["stability_observation"]["never_settled_positions"] = [p for p in monitor.positions if p not in monitor.ever_stable]
        if settling_chunks:
            settling_file = mode + f"_attempt{attempt:02d}_settling_raw.npz"
            with (self.directory / settling_file).open("xb") as stream:
                np.savez_compressed(stream, converter_counts=np.concatenate(settling_chunks),
                    metadata_json=np.array(json.dumps({"mode": mode, "range_code": record["range_code"],
                        "actual_rate_hz": rate, "settling": record["settling"], "map_sha256": hw.MAP_SHA256}, allow_nan=False)))
            record["settling"]["raw_file"] = settling_file
        metadata = {key: value for key, value in record.items() if key != "rows"}
        metadata.update(schema=SCHEMA, profile=hw.PROFILE, application_revision=hw.APP_REVISION,
                        recovery_policy=RECOVERY_POLICY,
                        timing_policy=TIMING_POLICY, lot=self.lot, tray=self.tray, operator=self.operator,
                        simulated=self.simulate, daq_serial=rig.info.serial_number, calibration=None,
                        physical_channels=list(range(57)), logical_to_physical=list(hw.MAPPING),
                        channel_map=hw.CHANNEL_MAP, map_sha256=hw.MAP_SHA256, installed_positions=list(self.positions),
                        control_events=list(rig.control_events),
                        nominal_rate_hz=hw.SAMPLE_HZ, oversample_extra=3, drop_first=1,
                        sample_layout="scans,57 physical channels,4 conversions; discard zero, mean 1:4",
                        range_limit_guard_counts=hw.RAIL_GUARD_COUNTS, voltage_reference="DAQ_OUTPUT",
                        battery_supervision="manual", noise_acceptance_limit=None,
                        selected_tests=list(self.selected_tests), run_id=self.run_id, family_id=self.family_id,
                        part_mapping=self.part_mapping,
                        measurement_scope="selected offset/noise tests; no sensitivity/polarity")
        destination = self.directory / record["raw_file"]
        partial = destination.with_suffix(".npz.partial")
        with partial.open("xb") as stream:
            np.savez_compressed(stream, converter_counts=raw,
                                metadata_json=np.array(json.dumps(metadata, sort_keys=True, allow_nan=False)))
            stream.flush()
            os.fsync(stream.fileno())
        partial.replace(destination)
        record["raw_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        # Raw counts are durable before numerical processing/report generation.
        if len(raw):
            excluded = set(hw.POSITIONS) - set(self.positions)
            if mode == "noise":
                excluded.update(row["position"] for row in combine_rows(self.records, self.positions, False, self.selected_tests)
                                if row["presence"] == "NO_SIGNAL")
            record["rows"] = channel_statistics(raw, record["range_code"], rate, mode,
                                                 valid_data=record["valid_data"], excluded_positions=excluded)
        self.update(rows=combine_rows(self.records, self.positions, False, self.selected_tests))
        return record

    def restore_dc(self):
        """Copy validated DC evidence into this new attempt, never alter its source.

        The recovery loader owns full metadata/eligibility validation. Recheck
        the hash on the actual bytes used here so a changed source cannot slip
        between UI approval and acquisition. New hardware still reconnects and
        completes the normal noise-route settling gate.
        """
        resume = self.resume_dc
        record, source = copy.deepcopy(resume["record"]), resume["source"]
        if record.get("mode") != "dc" or not record.get("valid_data") or record.get("errors") or not record.get("settling", {}).get("complete"):
            raise ValueError("Saved DC acquisition is not valid for noise recovery")
        for key, expected in (("profile", hw.PROFILE), ("map_sha256", hw.MAP_SHA256),
                              ("schema", SCHEMA)):
            if source.get(key) != expected:
                raise ValueError("Saved DC recovery " + key + " does not match this tester")
        if source.get("timing_policy") not in SUPPORTED_TIMING_POLICIES:
            raise ValueError("Saved DC recovery timing policy is unsupported")
        if not self.simulate and not source.get("daq_serial"):
            raise ValueError("Saved DC recovery has no DAQ identity")
        raw_path = Path(resume["raw_path"])
        if not raw_path.is_absolute():
            raise ValueError("Saved DC recovery path must be absolute")
        payload = raw_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != record.get("raw_sha256"):
            raise ValueError("Saved DC data changed after validation; recovery refused")
        # Keep the original basename so the unchanged NPZ metadata and this
        # record continue to agree if a later noise recovery is needed.
        if not re.fullmatch(r"dc(?:_attempt\d{2,})?_raw\.npz", record.get("raw_file", "")):
            raise ValueError("Saved DC raw filename is not a supported stage artifact")
        destination = self.directory / record["raw_file"]
        with destination.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        record["reused_from"] = copy.deepcopy(source)
        self.records.append(record)
        self.update(rows=combine_rows(self.records, self.positions, False, self.selected_tests))

    def run(self):
        rig, info, errors = None, None, []
        try:
            self.check_cancelled()
            stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
            lot_name = re.sub(r"[^\w.-]", "_", self.lot)[:60]
            name = f"{'SIMULATED' if self.simulate else 'MEASURED'}_{lot_name}_tray{self.tray}_{stamp}_{uuid.uuid4().hex[:8]}"
            self.directory = self.output_root / name
            self.directory.mkdir(parents=True, exist_ok=False)
            if self.resume_dc is not None:
                self.restore_dc()
            self.update(output=str(self.directory), phase="Connecting", message="Checking both M25 cards")
            with self.lease_factory():
                try:
                    self.check_cancelled()
                    kwargs = {"simulate": self.simulate}
                    if self.simulate:
                        clock = SimulationClock()
                        kwargs.update(monotonic=clock.monotonic, sleep=clock.sleep)
                    rig = self.rig_factory(**kwargs)
                    info = rig.connect()
                    if info.simulated != self.simulate:
                        raise hw.daq.StreamStateError("Selected source and connected device disagree")
                    if self.resume_dc is not None and self.resume_dc["source"].get("daq_serial") != info.serial_number:
                        raise hw.daq.StreamStateError("Connected DAQ identity differs from the saved DC acquisition; noise recovery refused")
                    for mode in (("noise",) if self.resume_dc is not None else self.selected_tests):
                        for attempt in range(1, MAX_STAGE_ATTEMPTS + 1):
                            self.check_cancelled()
                            record = self.capture_stage(rig, mode)
                            if record["valid_data"] or not record["retry_eligible"] or attempt == MAX_STAGE_ATTEMPTS:
                                break
                            self.update(phase="Retrying " + mode.upper(),
                                message=f"DAQ delivery interrupted. Retrying {mode.upper()} ({attempt + 1}/{MAX_STAGE_ATTEMPTS}); " +
                                        ("the unchanged signal path stays settled." if record["settling"]["complete"] else "checking settling again."))
                            # Brief, cancellable retry backoff; no control writes,
                            # reconnect, or artificial reset of the settling gate.
                            if self.simulate:
                                rig._sleep(.25)
                            else:
                                self.stop_event.wait(.25)
                        if not record["valid_data"]:
                            errors.extend(mode.upper() + ": " + e for e in record["errors"])
                            break
                finally:
                    if rig is not None:
                        self.update(phase="Returning to DC", message="Stopping acquisition and releasing the DAQ")
                        try:
                            rig.close()
                        except Exception as exc:
                            errors.append("DAQ shutdown: " + str(exc))
        except Exception as exc:
            errors.append(str(exc))
        finally:
            if self.stop_event.is_set() and not errors:
                errors.append("Stopped by operator")
            latest = {record["mode"]: record for record in self.records}
            complete = set(latest) == set(self.selected_tests) and all(r["valid_data"] for r in latest.values()) and not errors
            rejected = [{"mode": record["mode"], "attempt": record.get("attempt", 1),
                         "raw_file": record.get("raw_file"), "errors": list(record["errors"])}
                        for record in self.records if not record["valid_data"]]
            self.result = {"schema": SCHEMA, "profile": hw.PROFILE, "application_revision": hw.APP_REVISION,
                "created_at": dt.datetime.now().astimezone().isoformat(), "lot": self.lot,
                "tray": self.tray, "operator": self.operator, "simulated": self.simulate,
                "data_complete": bool(complete), "timing_policy": TIMING_POLICY,
                "installed_positions": list(self.positions), "channel_map": hw.CHANNEL_MAP,
                "daq": asdict(info) if info else None, "records": self.records, "errors": errors,
                "shutdown_status": getattr(rig, "shutdown_status", None),
                "control_events": getattr(rig, "control_events", []),
                "restart_required": bool(getattr(getattr(rig, "device", None), "_shutdown_failed", False)),
                "noise_acceptance_limit": None, "calibration_status": "PENDING",
                "legacy_noise_method": legacy_meter.method_metadata() if "noise" in self.selected_tests else None,
                "selected_tests": list(self.selected_tests), "run_id": self.run_id, "family_id": self.family_id,
                "part_mapping": self.part_mapping, "excluded_suspect_slots": self.excluded_suspect_slots,
                "rows": combine_rows(self.records, self.positions, complete, self.selected_tests)}
            for row in self.result["rows"]:
                row["part_id"] = (self.part_mapping.get(row["position"], self.run_id + ":" + row["position"])
                                  if row["installed"] else None)
            self.result["recovery"] = {"policy": RECOVERY_POLICY, "max_attempts_per_stage": MAX_STAGE_ATTEMPTS,
                "attempts_by_stage": {mode: sum(record["mode"] == mode for record in self.records) for mode in ("dc", "noise")},
                "recovered_modes": [mode for mode, record in latest.items() if record["valid_data"] and
                                    any(failed["mode"] == mode for failed in rejected)],
                "rejected_attempts": rejected}
            if self.resume_dc is not None:
                self.result["resumed_from"] = copy.deepcopy(self.resume_dc["source"])
            self.result["presence_policy"] = PRESENCE_POLICY
            self.result["no_signal_positions"] = [row["position"] for row in self.result["rows"]
                                                   if row["presence"] == "NO_SIGNAL"]
            self.result["noise_analysis_positions"] = [row["position"] for row in self.result["rows"]
                                                       if row["installed"] and row["presence"] != "NO_SIGNAL"]
            try:
                if self.directory is not None:
                    write_report(self.directory, self.result)
                    history = History(self.output_root, self.simulate)
                    history.record(self.result, self.directory)
                    self.result["batch_summary"] = history.summary(self.lot)
                    self.result["tray_summary"] = history.summary(self.lot, self.family_id)
                    self.result["current_parts"] = history.current_parts(self.lot, self.family_id)
                    self.result["slot_warnings"] = history.warnings()
                    write_report(self.directory, self.result)
            except Exception as exc:
                errors.append("Saving report: " + str(exc))
                self.result["data_complete"] = False
            self.update(phase="Complete" if self.result["data_complete"] else "Incomplete",
                        progress=1. if self.result["data_complete"] else self.status()["progress"],
                        rows=self.result["rows"],
                        message=("Saved selected tests. Noise results require review; no noise pass/fail limit." if "noise" in self.selected_tests
                                 else "Saved offset results. Noise was not taken.") if not errors else "; ".join(errors))
            self.finished.set()
