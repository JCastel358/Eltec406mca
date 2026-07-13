"""
Eltec 406MCA rig - readout for EXTERNAL (signal-generator) emitter drive.

Troubleshooting companion to esp32_rig_readout.py: use this when the emitter
is chopped by a bench signal generator instead of the ESP32's PWM output
(e.g. while debugging the ESP32 PWM path). The ESP32 + ADS1256 still capture
the sensor waveform, but the firmware's sync bit is meaningless with its PWM
off, so this script:

    - recovers the chopping frequency from the waveform itself (FFT peak near
      the --freq hint, sub-bin refined) and slices cycles by that period.
      Per-cycle peak-to-peak does not depend on phase alignment, so the
      sensitivity number is directly comparable to the synced measurement.
    - reports POLARITY as unavailable: without a sync reference there is no
      way to know which half of the cycle the emitter was driven in. (To get
      polarity back, either fix the ESP32 PWM, or loop the generator's
      SYNC/TTL output into the rig - that needs a firmware change.)
    - checks the sample timestamps for gaps, so serial-link data loss (the
      "48 cycles in an 8 s capture" symptom) is reported instead of silent.

Generator settings: square wave, 10 Hz, 50% duty, unipolar pulse sized for
the MOSFET driver module input (e.g. 0-3.3 V or 0-5 V; NOT a bipolar +/-
output - the module gate should never be driven negative).

Power scheme (2026-07-10): one 6 V 4.5 Ah SLA battery powers everything -
sensor buffer, sensors, and the emitter + MOSFET driver module (watched by
the `bat` check). The emitter's chopped amplitude scales strongly with its
supply voltage, so it sags as the battery discharges: check the battery
before trusting reference-baseline comparisons, and recharge before
concluding the emitter has degraded.

Usage:
    python siggen_rig_readout.py capture                  # 8 s DUT capture + analysis
    python siggen_rig_readout.py capture -f 10 -s 8 -o cap.csv
    python siggen_rig_readout.py capture --channel ref    # reference sensor (AIN1)
    python siggen_rig_readout.py capture --channel ref --set-baseline
    python siggen_rig_readout.py test                     # guided sequence; prompts
                                                          # you to toggle the
                                                          # generator on/off
    add --port COM3 (or /dev/ttyUSB0) to skip auto-detection
"""

from __future__ import annotations

import argparse
import math
import sys
import time

import numpy as np

from esp32_rig_readout import (
    EMITTER_WARMUP_S,
    OFFSET_MAX_V,
    OFFSET_MIN_V,
    REF_CAPTURE_S,
    SAMPLE_RATE_HZ,
    SENSOR_OFFSET_MAX_PLAUSIBLE_V,
    SENSOR_OFFSET_MIN_PLAUSIBLE_V,
    Esp32Rig,
    Sample,
    cmd_bat,
    cmd_ports,
    find_port,
    report_ref_health,
    save_csv,
    save_ref_baseline,
)

DEFAULT_FREQ_HZ = 10.0
DEFAULT_CAPTURE_S = 8.0
# A sample-to-sample timestamp step this many times the nominal period counts
# as a gap (dropped serial data), not jitter.
GAP_FACTOR = 2.5


# --------------------------------------------------------------------------- #
# Stream integrity: the ESP32 timestamps every sample with micros(), so any
# chunk lost on the serial link shows up as a jump in t_us. capture() refills
# the sample count from later data, which hides the loss - check here instead.
# --------------------------------------------------------------------------- #
def report_stream_gaps(samples: list[Sample]) -> None:
    if len(samples) < 2:
        return
    t_us = np.array([s.t_us for s in samples], dtype=np.int64)
    dt = np.diff(t_us)
    expected_us = 1e6 / SAMPLE_RATE_HZ
    gaps = dt[dt > GAP_FACTOR * expected_us]
    if gaps.size:
        lost_ms = float(np.sum(gaps - expected_us)) / 1000.0
        print(f"     WARNING: {gaps.size} gap(s) in the sample timestamps, "
              f"~{lost_ms:.0f} ms of data missing - the serial link dropped "
              "chunks; results below are computed on a gappy record")


# --------------------------------------------------------------------------- #
# Analysis without a sync channel
# --------------------------------------------------------------------------- #
def detect_frequency(volts: np.ndarray, fs: float, hint_hz: float) -> tuple[float, bool]:
    """Find the chopping frequency from the waveform's FFT peak near hint_hz.

    Returns (frequency, detected). detected=False means no clear tone stood
    above the spectrum around the hint, so the hint itself is returned - the
    generator is probably off, set to a very different frequency, or the
    signal is buried in noise.
    """
    v = np.asarray(volts, dtype=float)
    v = v - np.mean(v)
    if v.size < int(4 * fs / hint_hz):          # need at least ~4 cycles
        return hint_hz, False
    spectrum = np.abs(np.fft.rfft(v * np.hanning(v.size)))
    freqs = np.fft.rfftfreq(v.size, 1.0 / fs)
    band = np.flatnonzero((freqs >= 0.5 * hint_hz) & (freqs <= 1.5 * hint_hz))
    if band.size < 3:
        return hint_hz, False
    k = int(band[np.argmax(spectrum[band])])
    floor = float(np.median(spectrum[band]))
    if spectrum[k] < 5.0 * max(floor, 1e-12):   # peak must clearly stand out
        return hint_hz, False
    # Parabolic interpolation across the peak bin for sub-bin accuracy.
    delta = 0.0
    if 0 < k < spectrum.size - 1:
        a, b, c = float(spectrum[k - 1]), float(spectrum[k]), float(spectrum[k + 1])
        denom = a - 2.0 * b + c
        if abs(denom) > 1e-12:
            delta = max(-0.5, min(0.5, 0.5 * (a - c) / denom))
    bin_width = freqs[1] - freqs[0]
    return float(freqs[k] + delta * bin_width), True


def period_segments(sample_count: int, fs: float, freq_hz: float,
                    skip_cycles: int = 1) -> list[tuple[int, int]]:
    """Consecutive one-period windows (float period, so no cumulative drift).

    The windows are not phase-aligned to the drive - they don't need to be:
    any window exactly one period long contains one full high and one full
    low phase, so max-min per window is the true per-cycle peak-to-peak.
    """
    period = fs / freq_hz
    segments: list[tuple[int, int]] = []
    k = skip_cycles
    while True:
        start = int(round(k * period))
        end = int(round((k + 1) * period))
        if end > sample_count:
            break
        if end - start >= 8:
            segments.append((start, end))
        k += 1
    return segments


def analyze_unsynced(samples: list[Sample], freq_hint_hz: float) -> dict:
    """Mirror of esp32_rig_readout.analyze(), minus everything sync-based.

    Sensitivity = median per-cycle peak-to-peak, cycles sliced by the detected
    (or hinted) period - the same math the tester app and the synced readout
    use. Noise/SNR come from folding all cycles onto a mean template, like the
    app's estimate_noise_from_segments().
    """
    volts = np.array([s.volts for s in samples], dtype=float)
    result: dict = {
        "n": len(samples),
        "offset_v": float(np.mean(volts)) if volts.size else float("nan"),
    }
    freq_hz, detected = detect_frequency(volts, SAMPLE_RATE_HZ, freq_hint_hz)
    result["freq_hz"] = freq_hz
    result["freq_detected"] = detected

    segments = period_segments(volts.size, SAMPLE_RATE_HZ, freq_hz)
    result["cycles"] = len(segments)
    if len(segments) < 2:
        result["error"] = ("Fewer than 2 full cycles in the capture - is the "
                           "generator on and the capture long enough?")
        return result

    pp = [float(np.max(volts[a:b]) - np.min(volts[a:b])) for a, b in segments]
    result["sensitivity_mv"] = float(np.median(pp)) * 1000.0
    result["pp_spread_mv"] = (max(pp) - min(pp)) * 1000.0

    # Fold every cycle onto a common phase axis; the mean is the coherent
    # signal template, the residual is cycle-to-cycle noise.
    length = int(round(float(np.median([b - a for a, b in segments]))))
    length = min(max(length, 8), 2000)
    target_phase = np.linspace(0.0, 1.0, length, endpoint=False)
    folded = np.vstack([
        np.interp(target_phase,
                  np.linspace(0.0, 1.0, b - a, endpoint=False),
                  volts[a:b])
        for a, b in segments
    ])
    template = np.mean(folded, axis=0)
    residual = folded - template
    noise_rms_v = float(np.sqrt(np.mean(residual * residual)))
    noise_rms_v *= math.sqrt(folded.shape[0] / (folded.shape[0] - 1))
    signal_rms_v = float(np.sqrt(np.mean((template - np.mean(template)) ** 2)))
    result["noise_rms_mv"] = noise_rms_v * 1000.0
    result["signal_rms_mv"] = signal_rms_v * 1000.0
    if noise_rms_v > 0:
        snr = signal_rms_v / noise_rms_v
        result["snr"] = snr
        result["snr_db"] = 20.0 * math.log10(snr) if snr > 0 else float("-inf")
    return result


def print_analysis(r: dict, freq_hint_hz: float, indent: str = "     ") -> None:
    if "error" in r:
        print(f"{indent}{r['error']}")
        return
    freq_note = (f"detected from waveform (hint {freq_hint_hz:g})"
                 if r["freq_detected"]
                 else "NOT detected - fell back to the --freq hint; check the generator")
    print(f"{indent}Sensitivity     : {r['sensitivity_mv']:.2f} mV pk-pk "
          f"(cycle spread {r['pp_spread_mv']:.2f} mV over {r['cycles']} cycles)")
    if "snr" in r:
        print(f"{indent}Signal / noise  : {r['signal_rms_mv']:.3f} / "
              f"{r['noise_rms_mv']:.3f} mV rms  (SNR {r['snr_db']:.1f} dB)")
    print(f"{indent}Chop frequency  : {r['freq_hz']:.3f} Hz  [{freq_note}]")
    print(f"{indent}Polarity        : n/a with external drive (no sync reference)")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_capture(rig: Esp32Rig, args) -> None:
    where = "reference sensor (AIN1)" if args.channel == "ref" else "DUT (AIN0)"
    print(f"Capturing {args.seconds:g} s from the {where} at {SAMPLE_RATE_HZ:g} Hz")
    print("  (the signal generator should already be driving the emitter)")
    samples = rig.capture(args.seconds, channel=args.channel)
    if args.output:
        save_csv(samples, args.output)
    report_stream_gaps(samples)
    r = analyze_unsynced(samples, args.freq)
    print(f"     Mean level      : {r['offset_v']:.4f} V")
    print_analysis(r, args.freq)
    if args.channel == "ref" and "error" not in r:
        if args.set_baseline:
            save_ref_baseline(r["sensitivity_mv"], r["offset_v"])
        else:
            report_ref_health(r["sensitivity_mv"])


def cmd_test(rig: Esp32Rig, args) -> None:
    """Same order as esp32_rig_readout's test, but YOU toggle the generator."""
    print("External-drive mode: the script cannot switch the emitter, so it")
    print("prompts you to toggle the signal generator at each step.")

    print("1/6  Battery check")
    cmd_bat(rig, args)

    print("2/6  DC offset - generator OFF")
    input("     Turn the signal generator output OFF, then press Enter... ")
    time.sleep(0.2)
    offset_v = rig.read_offset_voltage()
    if not (SENSOR_OFFSET_MIN_PLAUSIBLE_V <= offset_v <= SENSOR_OFFSET_MAX_PLAUSIBLE_V):
        state = "no sensor detected (floating/railed input)"
    elif OFFSET_MIN_V <= offset_v <= OFFSET_MAX_V:
        state = "OK"
    else:
        state = f"outside the healthy {OFFSET_MIN_V:g}-{OFFSET_MAX_V:g} V band"
    print(f"     offset = {offset_v:.4f} V  [{state}]")

    print(f"3/6  Emitter on - set the generator to {args.freq:g} Hz square, "
          "50% duty, unipolar")
    print("     (emitter power comes from the 6 V battery - a sagging battery")
    print("      lowers every pk-pk number, so keep it charged)")
    input("     Turn the generator output ON, then press Enter... ")
    print(f"     warming up {EMITTER_WARMUP_S:g} s (thermal ramp)")
    time.sleep(EMITTER_WARMUP_S)

    print(f"4/6  Reference sensor check - emitter health ({REF_CAPTURE_S:g} s on AIN1)")
    ref_samples = rig.capture(REF_CAPTURE_S, channel="ref")
    report_stream_gaps(ref_samples)
    ref_r = analyze_unsynced(ref_samples, args.freq)
    if "error" in ref_r:
        print(f"     {ref_r['error']}")
    else:
        print(f"     reference = {ref_r['sensitivity_mv']:.2f} mV pk-pk")
        report_ref_health(ref_r["sensitivity_mv"])

    print(f"5/6  Capturing {args.seconds:g} s of DUT waveform (AIN0)")
    samples = rig.capture(args.seconds)
    report_stream_gaps(samples)
    if args.output:
        save_csv(samples, args.output)

    print("6/6  Results")
    r = analyze_unsynced(samples, args.freq)
    print(f"     DC offset       : {offset_v:.4f} V")
    print_analysis(r, args.freq)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Eltec rig readout with a SIGNAL GENERATOR driving the emitter")
    ap.add_argument("--port",
                    help="serial port (COM3, /dev/ttyUSB0); auto-detect if omitted")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("ports", help="list serial ports")
    p = sub.add_parser("capture", help="capture one channel and analyze it")
    p.add_argument("-s", "--seconds", type=float, default=DEFAULT_CAPTURE_S)
    p.add_argument("-f", "--freq", type=float, default=DEFAULT_FREQ_HZ,
                   help="generator frequency hint in Hz (default 10)")
    p.add_argument("-o", "--output", help="save samples to this CSV file")
    p.add_argument("--channel", choices=["sensor", "ref"], default="sensor",
                   help="sensor = DUT on AIN0 (default), ref = AIN1")
    p.add_argument("--set-baseline", action="store_true",
                   help="with --channel ref: record this as the emitter baseline")
    p = sub.add_parser("test", help="guided battery -> offset -> ref -> capture")
    p.add_argument("-s", "--seconds", type=float, default=DEFAULT_CAPTURE_S)
    p.add_argument("-f", "--freq", type=float, default=DEFAULT_FREQ_HZ,
                   help="generator frequency in Hz (default 10)")
    p.add_argument("-o", "--output", help="save DUT samples to this CSV file")

    args = ap.parse_args()
    if args.command == "ports":
        cmd_ports(args)
        return

    rig = Esp32Rig(args.port or find_port())
    rig.connect()
    # Bigger OS receive buffer (Windows/pyserial) so a brief host stall can't
    # overflow the default ~4 KB driver buffer and silently drop stream data.
    try:
        rig.ser.set_buffer_size(rx_size=1 << 20)
    except Exception:
        pass
    try:
        {"capture": cmd_capture, "test": cmd_test}[args.command](rig, args)
    finally:
        rig.close()      # sends PWM,OFF - harmless, the ESP32 PWM is unused here


if __name__ == "__main__":
    main()
