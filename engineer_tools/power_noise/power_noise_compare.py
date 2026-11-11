#!/usr/bin/env python3
"""Two-step, emitter-off battery / regulated-supply comparison on AIN0.

Run without arguments for the desktop window. --simulate --output DIR writes
a clearly labelled demonstration without opening a serial port. Uses each
model's existing serial backend; never enables the emitter or changes firmware.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime
import html
import importlib.util
import json
from pathlib import Path
import queue
import sys
import threading
import time
import uuid
import webbrowser

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = Path.home() / "Eltec_Power_Noise_Comparisons"
MODELS = ("405M22", "406MCA")
SOURCES = ("battery", "supply")
ADC_POSITIVE_FULL_SCALE = 8_388_607
# User confirmed the installed AIN7 divider on 2026-10-23. BAT? still embeds
# the old 99.7k/99.6k firmware ratio, so undo that ratio before applying 4:1.
SUPPLY_DIVIDER_TOP_OHMS = 300_000.0
SUPPLY_DIVIDER_BOTTOM_OHMS = 100_000.0
SUPPLY_DIVIDER_RATIO = (SUPPLY_DIVIDER_TOP_OHMS + SUPPLY_DIVIDER_BOTTOM_OHMS) / SUPPLY_DIVIDER_BOTTOM_OHMS
FIRMWARE_DIVIDER_RATIO = (99_700.0 + 99_600.0) / 99_600.0
# The rig uses nominal 5 V AVDD. TI specifies a buffered input ceiling of
# AVDD - 2 V; gain-1 unbuffered BAT? has nominal +5 V positive full scale.
AIN7_BUFFERED_CEILING_V = 3.0
AIN7_UNBUFFERED_CEILING_V = 5.0
CONTEXT = (
    "AIN7 uses the confirmed 300 kΩ / 100 kΩ divider (4:1). Supply voltage = "
    "BAT? reading / firmware divider ratio × 4. Nominal resistor values provide "
    "an estimate, not a meter calibration. The firmware response and inferred "
    "pin voltage are preserved in JSON. These snapshots are not a continuous "
    "supply-ripple measurement or hardware overvoltage protection. "
    "Different detector voltages mean this compares "
    "the two complete operating conditions, not power-source noise alone."
)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def load_backend(model):
    if model not in MODELS:
        raise ValueError("Choose 405M22 or 406MCA.")
    name = "power_noise_backend_" + model.lower()
    if name not in sys.modules:
        path = ROOT / "single_detector_rig" / ("m" + model.lower()) / "esp32_backend.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def configure_dark(rig, model):
    """Explicit OFF acknowledgement; don't rely on a cached PWM state."""
    rig.disable_emitter_pwm()
    version = rig.identity.version
    gain, buffered = (1, 0) if model == "405M22" else (2, 1)
    if version >= (2, 1, 0):
        mode = "V20" if model == "405M22" else "V19"
        ack = rig._command(f"FE,{mode}", "OK,FE,")
        readback = rig._command("FE?", "FE,")
        if any(response_fields(reply).get("gain") != str(gain)
               or response_fields(reply).get("buf") != str(buffered) for reply in (ack, readback)):
            raise RuntimeError("ADC settings did not match the selected detector. " + readback)
        return readback
    if (model == "405M22" and version == (2, 0, 0)) or (
        model == "406MCA" and version == (1, 9, 0)
    ):
        return f"Fixed firmware profile: gain={gain},buf={buffered}"
    raise RuntimeError(
        "Unsupported firmware/profile combination. Use the rig's matching "
        "production configuration. Legacy 406MCA v1.9 rigs are supported as-is."
    )


def response_fields(reply):
    return dict(item.split("=", 1) for item in reply.split(",")[1:] if "=" in item)


def read_adc_scale(rig, model, front_end):
    """Use the firmware's ADC reference, never the detector's supply voltage.

    The stream's decimal volts have only six decimal places. The raw code
    preserves sub-microvolt changes that this rounding can hide or exaggerate.
    The denominator matches countsToVolts() in all supported rig firmware.
    """
    status = response_fields(rig._command("STATUS?", "STATUS,"))
    try:
        vref = float(status["vref"])
    except (KeyError, ValueError) as exc:
        raise RuntimeError("Firmware did not report a valid ADC reference voltage.") from exc
    if not np.isfinite(vref) or vref <= 0 or status.get("pwm") != "0" or status.get("streaming") != "0":
        raise RuntimeError("Invalid ADC reference or rig is not idle with emitter OFF.")
    gain = 1 if model == "405M22" else 2
    full_scale = 2 * vref / gain
    if rig.identity.version >= (2, 1, 0):
        try:
            reported = float(response_fields(front_end)["fs"])
        except (KeyError, ValueError) as exc:
            raise RuntimeError("Firmware did not report the ADC full-scale voltage.") from exc
        if not np.isfinite(reported) or not np.isclose(reported, full_scale, rtol=0, atol=0.00051):
            raise RuntimeError("ADC reference and front-end full scale disagree.")
    return {"adc_reference_v": vref, "adc_gain": gain, "adc_full_scale_v": full_scale,
            "volts_per_count": full_scale / ADC_POSITIVE_FULL_SCALE,
            "noise_voltage_source": "raw_code scaled with firmware ADC reference and verified gain"}


def supply_snapshot(rig, model="406MCA"):
    """Undo the old firmware divider and apply the confirmed physical divider."""
    if rig.is_streaming:
        raise RuntimeError("AIN7 must be read outside the noise recording.")
    result = {"time": datetime.now().astimezone().isoformat(), "volts": None,
              "method": "BAT? / firmware divider ratio * physical divider ratio",
              "firmware_divider_ratio": load_backend(model).BATTERY_DIVIDER_RATIO,
              "physical_divider_ratio": SUPPLY_DIVIDER_RATIO,
              "divider_top_ohms": SUPPLY_DIVIDER_TOP_OHMS,
              "divider_bottom_ohms": SUPPLY_DIVIDER_BOTTOM_OHMS}
    try:
        reported = float(rig.read_battery_voltage())
        if not np.isfinite(reported) or reported <= 0:
            raise ValueError("AIN7 did not return a finite positive supply voltage.")
        pin = reported / result["firmware_divider_ratio"]
        estimate = pin * SUPPLY_DIVIDER_RATIO
        buffered = model == "406MCA"
        ceiling = AIN7_BUFFERED_CEILING_V if buffered else AIN7_UNBUFFERED_CEILING_V
        result.update(firmware_reported_v=reported, adc_pin_indication_v=pin,
                      corrected_estimate_v=estimate, input_ceiling_v=ceiling,
                      adc_buffered=buffered)
        if pin >= ceiling:
            result.update(status="out_of_range", warning="AIN7 is outside its measurement range; supply estimate is unreliable.")
        else:
            result.update(volts=estimate, status="estimated" if buffered else "indicative",
                          warning="Nominal divider scaling; verify against a meter." if buffered else
                          "Unbuffered ADC loads the resistor divider; this value may read low.")
    except Exception as exc:
        # DC supply voltage provides context; it is not needed for AIN0 RMS.
        # A failed voltage query must not erase an otherwise valid noise capture.
        result.update(status="unavailable", error=str(exc))
    return result


def supply_text(data):
    snapshots = [data.get("supply_voltage_" + point, {}) for point in ("before", "after")]
    values = [s.get("corrected_estimate_v") if s.get("status") == "out_of_range" else s.get("volts") for s in snapshots]
    readings = " → ".join("unavailable" if value is None else f"{value:.3f} V" for value in values)
    statuses = {s.get("status") for s in snapshots}
    label = ("out of range" if "out_of_range" in statuses else
             "indicative" if "indicative" in statuses else
             "old scale; unverified" if statuses & {"app_reading", "unverified"} else
             "4:1 estimate" if "estimated" in statuses else
             "simulated" if "simulated" in statuses else "")
    return readings + (f" ({label})" if label else "")


def require_supply_in_range(snapshot):
    if snapshot.get("status") == "out_of_range":
        raise RuntimeError("AIN7 is outside its measurement range. Disconnect the supply and check the regulator. "
                           "No noise comparison accepted; software cannot disconnect power.")


def metrics(volts, rate_hz=1000):
    values = np.asarray(volts, dtype=float)
    if values.ndim != 1 or len(values) < 2 or not np.all(np.isfinite(values)):
        raise ValueError("A finite waveform with at least two samples is required.")
    if not np.isfinite(rate_hz) or rate_hz < 1:
        raise ValueError("Sample rate must be finite and at least 1 sample/s.")
    mean = float(np.mean(values))
    rms = float(np.sqrt(np.mean((values - mean) ** 2)))
    if np.ptp(values) == 0:
        raise ValueError("Flat signal: check the detector, connection, and ADC. No noise verdict.")
    edge = min(int(rate_hz * 5), len(values) // 2)
    return {
        "samples": len(values), "duration_s": len(values) / rate_hz,
        "sample_rate_hz": rate_hz, "mean_v": mean,
        "rms_mv": rms * 1000, "peak_to_peak_mv": float(np.ptp(values)) * 1000,
        "start_end_shift_mv": float(np.mean(values[-edge:]) - np.mean(values[:edge])) * 1000,
    }


def comparison(battery, supply):
    result = {}
    for key in ("rms_mv", "peak_to_peak_mv"):
        a, b = battery[key], supply[key]
        result[key] = {
            "supply_change_percent": 100 * (b / a - 1) if a > 0 else None,
            "lower": "battery" if a < b else "supply" if b < a else "equal",
        }
    lower = result["rms_mv"]["lower"]
    result["headline"] = (
        "RMS noise was equal in these recordings." if lower == "equal" else
        f"{'Battery' if lower == 'battery' else 'Regulated supply'} measured lower RMS noise."
    )
    if result["peak_to_peak_mv"]["lower"] != lower:
        result["headline"] += " Peak-to-peak gives a different ranking; see both measurements."
    return result


def new_session(output, model, detector_id, duration, settle, simulated=False):
    if model not in MODELS or not 10 <= duration <= 600 or not 0 <= settle <= 600:
        raise ValueError("Use 10–600 seconds of recording and 0–600 seconds settling.")
    output = Path(output).expanduser().resolve()
    if any(part.lower().startswith("eltec_") and "test_results" in part.lower()
           for part in output.parts):
        raise ValueError("Choose a separate folder, outside production test results.")
    stamp = datetime.now().astimezone().isoformat()
    name = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + model + "_" + uuid.uuid4().hex[:8]
    folder = output / (("DEMO_" if simulated else "") + name)
    folder.mkdir(parents=True, exist_ok=False)
    state = {
        "model": model, "detector_id": detector_id.strip() or "not provided",
        "created": stamp, "duration_s": duration, "settle_s": settle,
        "simulated": simulated, "input": "AIN0 versus AINCOM", "context": CONTEXT,
        "method": "DC-removed RMS and full-record max-minus-min from full ADC counts; no software filtering or detrending",
        "format_version": 4,
        "supply_divider_ratio": SUPPLY_DIVIDER_RATIO,
        "captures": {}, "attempts": [],
    }
    write_json(folder / "session.json", state)
    return folder, state


def diagnostic_summary(diagnostics):
    if diagnostics is None:
        return {}
    fields = asdict(diagnostics)
    return {key: value for key, value in fields.items() if not key.startswith("_")}


def check_diagnostics(diag):
    if (diag is None or not diag.healthy or not diag.stop_marker_seen
            or diag.count_matches_firmware is not True or diag.driver_rx_overflow_events
            or diag.ignored_lines or diag.measured_rate_hz is None):
        raise RuntimeError("Recording rejected: incomplete or interrupted sample stream. "
                           + (diag.summary() if diag else "No stream diagnostics."))


def record(folder, state, source, port, cancel, progress, rig_factory=None):
    """Stream raw samples to disk; rejected and stopped attempts stay as evidence."""
    if source not in SOURCES:
        raise ValueError("Unknown source")
    attempt = f"{source}_{uuid.uuid4().hex[:8]}"
    detail = {"source": source, "raw_csv": attempt + ".csv", "status": "incomplete",
              "started": datetime.now().astimezone().isoformat()}
    rig = None
    values = []
    error = None
    diag = None
    try:
        backend = load_backend(state["model"])
        rig = (rig_factory or backend.Esp32Rig)(port=port or None)
        progress("Connecting…")
        rig.connect()
        detail["firmware"] = rig.identity.text
        detail["port"] = rig.port_name
        detail["front_end"] = configure_dark(rig, state["model"])
        detail.update(read_adc_scale(rig, state["model"], detail["front_end"]))
        progress("Reading AIN7 through the 4:1 divider…")
        detail["supply_voltage_before"] = supply_snapshot(rig, state["model"])
        require_supply_in_range(detail["supply_voltage_before"])
        if cancel.is_set():
            raise RuntimeError("Stopped. This incomplete recording is not compared.")
        header = rig.start_stream("sensor")
        rate = header.sample_rate_hz
        if rate != 1000 or header.channel != "SENSOR":
            raise RuntimeError("Expected the rig's 1000 samples/s SENSOR (AIN0) stream.")
        settling_count = int(round(state["settle_s"] * rate))
        needed = int(round(state["duration_s"] * rate))
        total = settling_count + needed
        received = 0
        last_data = time.monotonic()
        last_update = 0
        bad_sync = False
        clipped = False
        scale_mismatch = False
        with (folder / detail["raw_csv"]).open("x", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("timestamp_us", "raw_code", "volts", "sync", "phase", "firmware_volts"))
            try:
                while received < total:
                    if cancel.is_set():
                        raise RuntimeError("Stopped. This incomplete recording is not compared.")
                    chunk = rig.read_stream(max_samples=min(250, total - received), timeout_s=0.25)
                    now = time.monotonic()
                    if not chunk and (not rig.is_streaming or now - last_data > 2):
                        raise RuntimeError("Sample stream stopped or stalled. Reconnect and retry.")
                    if chunk:
                        last_data = now
                    for sample in chunk:
                        phase = "settling" if received < settling_count else "measurement"
                        volts = sample.raw * detail["volts_per_count"]
                        writer.writerow((sample.timestamp_us, sample.raw, volts, sample.sync, phase, sample.volts))
                        bad_sync |= sample.sync != 0
                        clipped |= abs(sample.raw) >= int(0.98 * ADC_POSITIVE_FULL_SCALE)
                        # Allow the firmware's float arithmetic and 1 uV text
                        # rounding, but catch a changed gain/reference/reset.
                        scale_mismatch |= abs(volts - sample.volts) > 3e-6
                        if phase == "measurement":
                            values.append(volts)
                        received += 1
                    handle.flush()
                    if now - last_update >= 0.2:
                        remaining = (settling_count - received) / rate
                        progress(f"Settling: {max(0, remaining):.0f} s remaining" if remaining > 0
                                 else f"Recording: {len(values) / rate:.0f} / {state['duration_s']:g} s")
                        last_update = now
            finally:
                if rig.is_streaming:
                    diag = rig.stop_stream()
                else:
                    diag = rig.stream_diagnostics
                for sample in rig.drained_samples:
                    writer.writerow((sample.timestamp_us, sample.raw, sample.raw * detail["volts_per_count"],
                                     sample.sync, "drain", sample.volts))
                    bad_sync |= sample.sync != 0
        check_diagnostics(diag)
        if cancel.is_set():
            raise RuntimeError("Stopped. This recording is not compared.")
        if bad_sync:
            raise RuntimeError("Emitter gate was active during recording. No noise verdict.")
        if clipped:
            raise RuntimeError("ADC near full scale. Check wiring/headroom. No noise verdict.")
        if scale_mismatch:
            raise RuntimeError("Raw ADC scale disagrees with firmware voltage. Check gain/reference; no noise verdict.")
        if len(values) != needed:
            raise RuntimeError("Wrong recording length. No noise verdict.")
        detail.update(metrics(values, rate))
        progress("Reading final AIN7 supply voltage…")
        detail["supply_voltage_after"] = supply_snapshot(rig, state["model"])
        require_supply_in_range(detail["supply_voltage_after"])
        if cancel.is_set():
            raise RuntimeError("Stopped. This recording is not compared.")
        detail["status"] = "accepted"
    except Exception as exc:
        error = exc
        detail["status"] = "rejected"
        detail["error"] = str(exc)
    finally:
        if rig is not None:
            if rig.is_streaming:
                try:
                    diag = rig.stop_stream()
                except Exception:
                    pass
            if diag is None:
                diag = rig.stream_diagnostics
            rig.close()
        detail["diagnostics"] = diagnostic_summary(diag)
        write_json(folder / (attempt + ".json"), detail)
        state["attempts"].append(detail)
        if error is None:
            state["captures"][source] = detail
        write_json(folder / "session.json", state)
    if error:
        raise error
    return detail


def save_report(folder, state):
    a, b = (state["captures"][source] for source in SOURCES)
    # Both recordings must describe the same acquisition path, sample count,
    # and duration. A changed board/profile is not a power-source comparison.
    if any(data.get("status") != "accepted" for data in (a, b)):
        raise ValueError("Only accepted recordings can be compared.")
    for key in ("front_end", "firmware", "port", "samples", "sample_rate_hz", "duration_s",
                "adc_reference_v", "adc_gain", "adc_full_scale_v", "noise_voltage_source"):
        if a.get(key) != b.get(key):
            raise ValueError(f"The recordings used different {key}. Start a new comparison.")
    result = comparison(a, b)
    state["comparison"] = result
    write_json(folder / "session.json", state)
    rows = []
    with (folder / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("source", "detector_model", "detector_id", "rms_mv", "peak_to_peak_mv",
                         "mean_v", "start_end_shift_mv", "duration_s", "simulated",
                         "supply_voltage_before_v", "supply_voltage_after_v", "adc_full_scale_v",
                         "supply_voltage_before_status", "supply_voltage_after_status", "physical_divider_ratio",
                         "ain7_firmware_before_v", "ain7_firmware_after_v"))
        for source, data in zip(SOURCES, (a, b)):
            writer.writerow((source, state["model"], state["detector_id"], data["rms_mv"],
                             data["peak_to_peak_mv"], data["mean_v"], data["start_end_shift_mv"],
                             data["duration_s"], state["simulated"],
                             data.get("supply_voltage_before", {}).get("volts"),
                             data.get("supply_voltage_after", {}).get("volts"), data.get("adc_full_scale_v"),
                             data.get("supply_voltage_before", {}).get("status"),
                             data.get("supply_voltage_after", {}).get("status"), state.get("supply_divider_ratio"),
                             data.get("supply_voltage_before", {}).get("firmware_reported_v"),
                             data.get("supply_voltage_after", {}).get("firmware_reported_v")))
    for label, key, unit in (("RMS noise", "rms_mv", "mV"),
                             ("Peak-to-peak noise", "peak_to_peak_mv", "mV")):
        percent = result[key]["supply_change_percent"]
        change = "unavailable" if percent is None else f"{abs(percent):.1f}% {'lower' if percent < 0 else 'higher' if percent > 0 else 'change'}"
        rows.append(f"<tr><th>{label}</th><td>{a[key]:.6f} {unit}</td><td>{b[key]:.6f} {unit}</td><td>{change}</td></tr>")
    rows.append(f"<tr><th>Supply voltage estimate<br>(before → after)</th><td>{supply_text(a)}</td>"
                f"<td>{supply_text(b)}</td><td>AIN7 · 4:1 divider</td></tr>")
    demo = "DEMONSTRATION — simulated data. " if state["simulated"] else ""
    report = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>Detector power noise comparison</title>
<style>body{{font:17px system-ui;color:#172b3a;background:#f3f6f8;margin:0;padding:40px}}
main{{max-width:980px;margin:auto;background:white;padding:32px;border-radius:12px}}
h1{{font-size:28px}}.result{{background:#e9f4f1;padding:20px;font-size:23px;font-weight:650}}
table{{width:100%;border-collapse:collapse;margin:24px 0}}th,td{{padding:14px;text-align:left;border-bottom:1px solid #dbe2e8}}
.note{{color:#4b5c68;font-size:15px}}@media print{{body{{padding:0;background:white}}main{{padding:0}}}}</style>
<main><p>{html.escape(demo + state['model'] + ' · Detector: ' + state['detector_id'])}</p>
<h1>Battery vs. regulated supply</h1><div class="result">{html.escape(result['headline'])}</div>
<table><tr><th>Measurement</th><th>Battery</th><th>Regulated supply</th><th>Supply vs. battery</th></tr>{''.join(rows)}</table>
<p>Lower RMS means lower average noise in this recording. Lower peak-to-peak means a smaller largest excursion.</p>
<p>{state['duration_s']:g} seconds per source, after {state['settle_s']:g} seconds settling. AIN0, 1,000 samples/s.
No software filter; the fixture and ADC set the measured bandwidth. Both recordings use the same settings.</p>
<p>RMS = √mean((voltage − mean voltage)²). Peak-to-peak = maximum − minimum.
DC offset is excluded from RMS; drift and spikes remain included. These are measured input volts, with no production-test or legacy gain multiplier.</p>
<p class="note">Noise uses full ADC counts scaled by the firmware's ADC reference and detector gain,
avoiding the 1 µV rounding of the transmitted decimal voltage. AIN7 supply voltage does not scale the noise results.</p>
<p class="note">{html.escape(CONTEXT)}</p>
<p class="note">A faulty regulator or incorrect detector supply invalidates a comparison of the intended operating conditions.
This program cannot protect hardware from overvoltage. In 405M22 mode the unbuffered ADC loads the divider,
so voltage is indicative. An out-of-range reading rejects the capture; a low reading does not prove safe power.</p>
<p class="note">One recording per source: an observed comparison, not proof of a repeatable difference.
Noise includes the detector, fixture, and ADC. Keep the detector shielded from changing light, heat, and airflow.
For a close result, repeat the comparison. Fixed settling time does not guarantee thermal stability.</p>
<p class="note">DC mean: battery {a['mean_v']:.6f} V; supply {b['mean_v']:.6f} V.
End-minus-start mean (first/last 5 s): battery {a['start_end_shift_mv']:.6f} mV;
supply {b['start_end_shift_mv']:.6f} mV. This helps reveal drift.</p>
<p class="note">{html.escape(a.get('firmware', 'SIMULATION'))} · {html.escape(a.get('front_end', 'SIMULATION'))}<br>
{html.escape(state['created'])}<br>Raw recordings and summary.csv are saved beside this report.</p></main></html>"""
    (folder / "report.html").write_text(report, encoding="utf-8")
    return result


def simulate(output, model, duration=60, settle=30):
    folder, state = new_session(output, model, "DEMO", duration, settle, True)
    rng = np.random.default_rng(422)
    for source, sigma in (("battery", 0.00020), ("supply", 0.00012)):
        full_scale = 5.0 if model == "405M22" else 2.5
        scale = full_scale / ADC_POSITIVE_FULL_SCALE
        raw = np.rint((1.2 + rng.normal(0, sigma, int(duration * 1000))) / scale).astype(int)
        values = raw * scale
        data = metrics(values)
        voltage = 6.3 if source == "battery" else 5.05
        data.update(status="accepted", firmware="SIMULATED", front_end="SIMULATED", port="none",
                    adc_reference_v=2.5, adc_gain=1 if model == "405M22" else 2,
                    adc_full_scale_v=full_scale, noise_voltage_source="SIMULATED ADC counts",
                    supply_voltage_before={"volts": voltage, "status": "simulated"},
                    supply_voltage_after={"volts": voltage - 0.001, "status": "simulated"})
        state["captures"][source] = data
        with (folder / (source + "_SIMULATED.csv")).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("timestamp_us", "raw_code", "volts", "phase"))
            writer.writerows((i * 1000, int(raw[i]), float(v), "measurement") for i, v in enumerate(values))
    save_report(folder, state)
    return folder


def launch_gui(args):
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Detector power noise comparison")
    root.geometry("820x700")
    root.minsize(780, 680)
    style = ttk.Style(root)
    style.configure("TLabel", font=("Segoe UI", 11))
    style.configure("TButton", font=("Segoe UI", 11), padding=8)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Battery vs. regulated supply", font=("Segoe UI", 21, "bold")).pack(anchor="w")
    ttk.Label(frame, text="1. Record battery     2. Switch detector power     3. Record supply").pack(anchor="w", pady=(8, 16))
    form = ttk.Frame(frame)
    form.pack(fill="x")
    model = tk.StringVar(value=args.model)
    detector = tk.StringVar()
    port = tk.StringVar(value=args.port or "")
    fields = []
    for row, (label, variable) in enumerate((("Detector model", model), ("Detector ID (optional)", detector), ("Port (blank = auto)", port))):
        ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=5)
        widget = (ttk.Combobox(form, textvariable=variable, values=MODELS, state="readonly") if row == 0
                  else ttk.Entry(form, textvariable=variable))
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        fields.append(widget)
    form.columnconfigure(1, weight=1)
    ttk.Label(frame, text=f"Each source: {args.settle:g} s settling + {args.seconds:g} s recording. Emitter stays OFF.").pack(anchor="w", pady=(14, 8))
    confirmed = tk.BooleanVar()
    ttk.Checkbutton(frame, variable=confirmed, text="Emitter unplugged; safe supply checked with meter; detector covered.").pack(anchor="w")
    status = tk.StringVar(value="Connect the ~6.3 V detector battery, then press Record battery.")
    ttk.Label(frame, textvariable=status, wraplength=730, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=18)
    progressbar = ttk.Progressbar(frame, mode="indeterminate")
    progressbar.pack(fill="x")
    results = tk.StringVar(value="Results will appear here: RMS and peak-to-peak in mV.")
    ttk.Label(frame, textvariable=results, wraplength=730, justify="left").pack(anchor="w", pady=16)
    actions = ttk.Frame(frame)
    actions.pack(fill="x")
    events = queue.Queue()
    cancel = threading.Event()
    current = {"folder": None, "state": None, "busy": False, "closing": False, "source": "battery", "port": None}

    def open_report():
        webbrowser.open((current["folder"] / "report.html").as_uri())

    def start():
        if not confirmed.get():
            messagebox.showinfo("Before recording", "Check the detector supply with a meter before connecting it. Unplug the emitter and cover the detector, then tick the checkbox. AIN7 is not overvoltage protection.")
            return
        if current["state"] is None:
            try:
                folder, state = new_session(args.output, model.get(), detector.get(), args.seconds, args.settle)
            except Exception as exc:
                messagebox.showerror("Cannot start", str(exc))
                return
            current.update(folder=folder, state=state, port=port.get().strip())
            for widget in fields:
                widget.configure(state="disabled")
        current["busy"] = True
        cancel.clear()
        start_button.configure(state="disabled")
        stop_button.configure(state="normal")
        progressbar.start()

        def worker():
            try:
                record(current["folder"], current["state"], current["source"], current["port"], cancel,
                       lambda text: events.put(("progress", text)))
                if current["source"] == "supply":
                    save_report(current["folder"], current["state"])
                events.put(("done", None))
            except Exception as exc:
                events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=False).start()

    def close():
        if current["busy"]:
            current["closing"] = True
            cancel.set()
            status.set("Stopping and saving the partial recording…")
        else:
            root.destroy()

    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "progress":
                    status.set(value)
                    continue
                current["busy"] = False
                progressbar.stop()
                stop_button.configure(state="disabled")
                if current["closing"]:
                    root.destroy()
                    return
                if kind == "error":
                    status.set(value)
                    start_button.configure(state="normal", text=f"Retry {current['source']}")
                    continue
                state = current["state"]
                if current["source"] == "battery":
                    data = state["captures"]["battery"]
                    results.set(f"Battery: RMS {data['rms_mv']:.6f} mV   |   Peak-to-peak {data['peak_to_peak_mv']:.6f} mV\n"
                                f"Supply voltage (before → after): {supply_text(data)}")
                    current["source"] = "supply"
                    confirmed.set(False)
                    status.set("Battery saved. Disconnect it. Verify the regulator output with a meter before connecting the supply; tick the checkbox again, then Record supply.")
                    start_button.configure(state="normal", text="2. Record supply")
                else:
                    a, b = (state["captures"][s] for s in SOURCES)
                    result = state["comparison"]
                    status.set(result["headline"])
                    change = result["rms_mv"]["supply_change_percent"]
                    results.set(f"Battery: RMS {a['rms_mv']:.6f} mV   |   Peak-to-peak {a['peak_to_peak_mv']:.6f} mV\n"
                                f"Supply:  RMS {b['rms_mv']:.6f} mV   |   Peak-to-peak {b['peak_to_peak_mv']:.6f} mV\n"
                                f"Supply RMS change: {change:+.1f}% (negative = lower noise)\n"
                                f"AIN7 before → after: battery {supply_text(a)}; supply {supply_text(b)}")
                    report_button.configure(state="normal")
                    start_button.configure(text="Comparison complete", state="disabled")
        except queue.Empty:
            pass
        root.after(100, poll)

    start_button = ttk.Button(actions, text="1. Record battery", command=start)
    start_button.pack(side="left")
    stop_button = ttk.Button(actions, text="Stop", command=cancel.set, state="disabled")
    stop_button.pack(side="left", padx=8)
    report_button = ttk.Button(actions, text="Open results", command=open_report, state="disabled")
    report_button.pack(side="left")
    ttk.Label(frame, text="AIN7 uses your 300 kΩ / 100 kΩ divider (4:1). Check supply voltage with a meter.\nDifferent supply voltages compare both source and operating voltage.",
              wraplength=730, foreground="#526474").pack(anchor="w", pady=(16, 4))
    ttk.Label(frame, text=f"Saved to: {args.output}", wraplength=730, foreground="#526474").pack(anchor="w")
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, poll)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, default="406MCA")
    parser.add_argument("--port", default=None)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--settle", type=float, default=30)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--simulate", action="store_true", help="Write a hardware-free example report and exit")
    args = parser.parse_args()
    if not 10 <= args.seconds <= 600 or not 0 <= args.settle <= 600:
        parser.error("Recording must be 10–600 s; settling must be 0–600 s.")
    if args.simulate:
        print(simulate(args.output, args.model, args.seconds, args.settle) / "report.html")
    else:
        launch_gui(args)


if __name__ == "__main__":
    main()
