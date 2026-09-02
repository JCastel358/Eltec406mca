#!/usr/bin/env python3
"""Capture and compare detector waveforms: legacy chopper vs the rig emitter.

Purpose (2026-08-31): before trusting the 449 M18 frequency-tracking app at
18 Hz, verify that the rig's miniature blackbody emitter — a resistor that
must physically heat and cool every cycle — produces the SAME waveform shape
in a detector that the legacy fixture's continuously-hot blackbody + 20/80
mechanical chopper blade produces. Amplitude is expected to differ and is
deliberately ignored; the worry is thermal time constant: at 18 Hz the
emitter has only ~11 ms ON / ~44 ms OFF, and if it cannot swing its radiant
output that fast the detector sees a shallow, rounded ripple instead of the
chopped pulse the TP443 analysis assumes.

Two capture modes, both using the ESP32 + ADS1256 rig purely as a recorder
on AIN0 (SENSOR):

  * ``--setup legacy`` — the detector is driven by the LEGACY fixture
    (blackbody + chopper). The rig's emitter PWM is forced OFF; the board
    only streams the detector output. Wire detector signal -> AIN0 and
    detector ground -> rig analog ground; that ground wire IS the required
    common ground (the ADS1256 measures AIN0 against its own AGND, so the
    two systems must share it via exactly that one connection).
  * ``--setup rig`` — the same detector on the rig fixture: the ESP32 drives
    the miniature emitter with the qualified 18 Hz (or 5 Hz) 20 % duty on
    GPIO33, waits a thermal warm-up, then streams. Each streamed record's
    sync bit carries the contemporaneous PWM state, so cycles are folded on
    the true drive edges and the detector's thermal lag is measured too.

``compare`` then folds every capture into per-cycle traces on its own
fundamental (chopper motors drift; each cycle is resampled between its own
boundaries), averages them, normalises the mean cycle to unit peak-to-peak
(shape only, amplitude discarded), aligns the captures by circular
cross-correlation and reports the numbers that answer the question:

  * 10-90 % rise and 90-10 % fall time (ms and % of period),
  * width at 50 % level (the 20/80 blade should give ~20-30 %),
  * harmonic ratios H2/H1 ... H5/H1 (a rounded thermal ripple loses its
    harmonics; a chopped pulse keeps them),
  * shape correlation and RMS shape residual against the first
    (reference) capture,
  * for rig captures: median delay from the PWM ON edge to the in-cycle
    detector peak.

Captures are saved OUTSIDE the repository (default
``~/Documents/Eltec_EmitterComparison``) and never into the
``Eltec_*_Test_Results`` evidence folders.

Requires firmware v3.2 on the board (the backend refuses older firmware at
connect because PWM,DUTY does not exist before v3.2). If the bench board is
still on v3.1, flash first: ``python Arduino/Eltec/flash_firmware.py``.

Examples (run from the repo root):

  # detector driven by the LEGACY fixture, rig only listening
  python engineer_tools/emitter_waveform_comparison.py capture --setup legacy --label det42 --seconds 30

  # same detector on the rig fixture, 18 Hz 20/80, 10 s emitter warm-up
  python engineer_tools/emitter_waveform_comparison.py capture --setup rig --freq 18 --label det42 --seconds 30

  # shape comparison: no arguments = newest legacy capture (reference) vs
  # newest rig capture; or pass explicit .npz paths (PowerShell does not
  # expand globs, so type/tab-complete full paths if overriding)
  python engineer_tools/emitter_waveform_comparison.py compare --show
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "single_detector_rig" / "m449m18"))
import esp32_backend as eb  # noqa: E402  (the production 449 M18 backend)

# NOT one of the Eltec_*_Test_Results evidence folders — engineering
# experiment data lives in its own place and is never mixed with production
# test evidence.
DEFAULT_OUTPUT_ROOT = Path.home() / "Documents" / "Eltec_EmitterComparison"
# ADS1256 front-end limit used elsewhere in the repo for "this window is
# meaningless" clip detection; here it is only a hookup warning.
CLIP_LIMIT_V = 4.9
# Cycle folding resamples every cycle onto this many phase points. 400 points
# at 18 Hz is ~7x the raw 1000 SPS density, so np.interp is the only
# resampling needed and harmonic estimates up to H5 are comfortably clean.
CYCLE_POINTS = 400
MIN_CYCLES = 10
# Fundamental search range for legacy captures (no sync bit): wide enough for
# a drifting chopper motor near 5 or 18 Hz, narrow enough to skip mains hum.
FUNDAMENTAL_SEARCH_HZ = (1.0, 30.0)


# ----------------------------------------------------------------------
# Capture
# ----------------------------------------------------------------------
def _timestamp_seconds(samples: list) -> np.ndarray:
    """Unwrap the firmware's uint32 microsecond timestamps to seconds."""
    ts = np.asarray([s.timestamp_us for s in samples], dtype=np.int64)
    deltas = np.diff(ts) & 0xFFFFFFFF  # modular: survives the ~71 min wrap
    return np.concatenate([[0.0], np.cumsum(deltas)]) / 1e6


def capture(args: argparse.Namespace) -> Path:
    rig = eb.Esp32Rig(args.port)
    rig.connect()
    identity = rig.identity.text if rig.identity else "unknown"
    print(f"connected: {identity} on {rig.port_name}")

    drive_hz: float | None = None
    drive_duty: float | None = None
    try:
        if args.setup == "rig":
            drive_hz = float(args.freq)
            drive_duty = eb.PWM_DUTY_CYCLE_PERCENT
            print(
                f"emitter ON: {drive_hz:g} Hz / {drive_duty:g}% on "
                f"GPIO{eb.PWM_GPIO}; warming up {args.warmup:g} s ..."
            )
            rig.configure_emitter_pwm("DIO0", drive_hz, drive_duty)
            time.sleep(args.warmup)
        else:
            # Belt and braces: the board boots with PWM off, but make sure the
            # rig emitter cannot pollute a capture of the legacy fixture.
            rig.disable_emitter_pwm()
            print(
                "rig emitter forced OFF - drive the detector with the legacy "
                "fixture now (capture starts immediately)."
            )

        header = rig.start_stream("sensor")
        print(f"streaming {header.channel} at {header.sample_rate_hz:g} SPS "
              f"for {args.seconds:g} s ...")
        samples: list = []
        deadline = time.monotonic() + args.seconds
        next_report = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            samples.extend(rig.read_stream(timeout_s=0.5))
            if time.monotonic() >= next_report:
                print(f"  {len(samples)} records ...")
                next_report += 5.0
        diagnostics = rig.stop_stream()
        samples.extend(rig.drained_samples)
    finally:
        rig.close()  # close() also turns the PWM off

    print(f"stream integrity: {diagnostics.summary()}")
    if not diagnostics.healthy:
        print("WARNING: stream diagnostics are unhealthy - treat this "
              "capture with suspicion and consider re-capturing.")
    if len(samples) < 2:
        sys.exit("no samples received - check the wiring and try again")

    volts = np.asarray([s.volts for s in samples], dtype=float)
    sync = np.asarray([s.sync for s in samples], dtype=np.uint8)
    t_s = _timestamp_seconds(samples)
    print(f"detector level: mean {volts.mean():+.3f} V, "
          f"pk-pk {np.ptp(volts) * 1000:.1f} mV")
    if np.any(np.abs(volts) >= CLIP_LIMIT_V):
        print(f"WARNING: samples at/above {CLIP_LIMIT_V} V - the ADS1256 "
              "front end is clipping; the shape cannot be trusted.")

    out_dir = Path(args.out) if args.out else DEFAULT_OUTPUT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    parts = [args.label] if args.label else []
    parts.append(args.setup)
    if drive_hz is not None:
        parts.append(f"{drive_hz:g}hz")
    parts.append(stamp)
    out_path = out_dir / ("_".join(parts) + ".npz")
    np.savez_compressed(
        out_path,
        waveform_v=volts,
        t_s=t_s,
        sync=sync,
        sample_rate_hz=header.sample_rate_hz,
        setup=args.setup,
        label=args.label or "",
        drive_frequency_hz=-1.0 if drive_hz is None else drive_hz,
        drive_duty_percent=-1.0 if drive_duty is None else drive_duty,
        recorded_at=_dt.datetime.now().isoformat(timespec="seconds"),
        firmware=identity,
        diagnostics=diagnostics.summary(),
        stream_healthy=diagnostics.healthy,
    )
    print(f"saved {len(volts)} samples -> {out_path}")
    return out_path


# ----------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------
def load_capture(path: Path) -> dict:
    data = np.load(path)
    drive_hz = float(data["drive_frequency_hz"])
    return {
        "path": path,
        "waveform_v": np.asarray(data["waveform_v"], dtype=float),
        "t_s": np.asarray(data["t_s"], dtype=float),
        "sync": np.asarray(data["sync"], dtype=np.uint8),
        "rate_hz": float(data["sample_rate_hz"]),
        "setup": str(data["setup"]),
        "label": str(data["label"]),
        "drive_hz": None if drive_hz < 0 else drive_hz,
        "recorded_at": str(data["recorded_at"]),
    }


def highpass_detrend(volts: np.ndarray, rate_hz: float, corner_hz: float) -> np.ndarray:
    """Zero-phase FFT high-pass (Butterworth magnitude) to strip DC drift.

    Same numpy-only technique as replot_noise_capture.fft_band_filter, with
    reflection padding so the FFT's implicit periodicity does not inject
    edge transients into the first/last cycles.
    """
    x = np.asarray(volts, dtype=float)
    pad = min(len(x), int(round(2.0 * rate_hz / corner_hz)))
    padded = np.concatenate([x[pad - 1 :: -1], x, x[: -pad - 1 : -1]]) - x.mean()
    freq = np.fft.rfftfreq(len(padded), 1.0 / rate_hz)
    safe = np.maximum(freq, 1e-12)
    response = 1.0 / np.sqrt(1.0 + (corner_hz / safe) ** 8)
    response[0] = 0.0
    filtered = np.fft.irfft(np.fft.rfft(padded) * response, len(padded))
    return filtered[pad : pad + len(x)]


def estimate_fundamental(volts: np.ndarray, rate_hz: float) -> float:
    """FFT-peak fundamental in the chopper band, refined by quadratic fit."""
    x = volts - volts.mean()
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freq = np.fft.rfftfreq(len(x), 1.0 / rate_hz)
    lo, hi = FUNDAMENTAL_SEARCH_HZ
    band = (freq >= lo) & (freq <= hi)
    if not np.any(band):
        raise ValueError("capture too short to resolve the chopper band")
    peak = np.flatnonzero(band)[np.argmax(spectrum[band])]
    if 0 < peak < len(spectrum) - 1:  # quadratic interpolation on the peak
        alpha, beta, gamma = np.log(spectrum[peak - 1 : peak + 2] + 1e-30)
        offset = 0.5 * (alpha - gamma) / (alpha - 2 * beta + gamma)
        return float((peak + offset) * rate_hz / len(x))
    return float(freq[peak])


def cycle_boundaries_from_sync(t_s: np.ndarray, sync: np.ndarray) -> np.ndarray:
    """PWM ON (0->1) edge times: exact drive-cycle boundaries."""
    edges = np.flatnonzero((sync[1:] == 1) & (sync[:-1] == 0)) + 1
    return t_s[edges]


def cycle_boundaries_from_signal(
    t_s: np.ndarray, volts: np.ndarray, rate_hz: float, f0: float
) -> np.ndarray:
    """Rising zero-crossings of the fundamental component.

    A chopper motor drifts, so fixed-period folding smears the mean cycle;
    band-passing +/-40 % around f0 and cutting on each rising zero-crossing
    tracks the drift cycle by cycle.
    """
    x = volts - volts.mean()
    freq = np.fft.rfftfreq(len(x), 1.0 / rate_hz)
    response = np.exp(-0.5 * ((freq - f0) / (0.4 * f0)) ** 4)  # flat-ish band
    fundamental = np.fft.irfft(np.fft.rfft(x) * response, len(x))
    rising = np.flatnonzero((fundamental[:-1] < 0) & (fundamental[1:] >= 0))
    crossings = []
    for index in rising:
        span = fundamental[index + 1] - fundamental[index]
        frac = -fundamental[index] / span if span else 0.0
        crossings.append(t_s[index] + frac * (t_s[index + 1] - t_s[index]))
    return np.asarray(crossings)


def fold_cycles(
    t_s: np.ndarray, volts: np.ndarray, boundaries: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Resample each cycle (boundary -> next boundary) to CYCLE_POINTS."""
    cycles = []
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        grid = start + (end - start) * np.arange(CYCLE_POINTS) / CYCLE_POINTS
        cycles.append(np.interp(grid, t_s, volts))
    stack = np.asarray(cycles)
    return stack, stack.mean(axis=0)


def analyze_capture(capture_data: dict) -> dict:
    volts = capture_data["waveform_v"]
    t_s = capture_data["t_s"]
    rate = capture_data["rate_hz"]

    f0_estimate = estimate_fundamental(volts, rate)
    detrended = highpass_detrend(volts, rate, corner_hz=f0_estimate / 4.0)

    sync_edges = cycle_boundaries_from_sync(t_s, capture_data["sync"])
    if len(sync_edges) >= MIN_CYCLES + 1:
        boundaries = sync_edges
        boundary_source = "PWM sync edges"
    else:
        boundaries = cycle_boundaries_from_signal(t_s, detrended, rate, f0_estimate)
        boundary_source = "signal zero-crossings"
    if len(boundaries) < MIN_CYCLES + 1:
        raise ValueError(
            f"{capture_data['path'].name}: only {max(0, len(boundaries) - 1)} "
            f"cycles found (need {MIN_CYCLES}); capture longer or check hookup"
        )

    # Whole-span average, not median period: the sync bit is sampled at
    # 1 kHz, so individual 18 Hz periods quantise to 55/56 ms and a median
    # would land on one of them (-0.8 %); the span across all edges does not.
    fundamental_hz = (len(boundaries) - 1) / float(boundaries[-1] - boundaries[0])
    cycles, mean_cycle = fold_cycles(t_s, detrended, boundaries)

    # Shape-only view: unit pk-pk, amplitude intentionally discarded.
    pp_v = float(np.ptp(mean_cycle))
    shape = (mean_cycle - mean_cycle.min()) / pp_v

    # Per-cycle repeatability: how much any one cycle looks like the mean.
    centered = cycles - cycles.mean(axis=1, keepdims=True)
    mean_centered = mean_cycle - mean_cycle.mean()
    norms = np.linalg.norm(centered, axis=1) * np.linalg.norm(mean_centered)
    cycle_correlation = float(
        np.mean((centered @ mean_centered) / np.maximum(norms, 1e-30))
    )

    # Harmonics straight off the folded mean cycle: folding removed the
    # drive drift, so bin k IS harmonic k exactly.
    coefficients = np.abs(np.fft.rfft(mean_cycle - mean_cycle.mean()))
    h1 = max(coefficients[1], 1e-30)
    harmonic_ratios = [float(coefficients[k] / h1) for k in range(2, 6)]

    # Rise/fall/width read off the shape rotated so the minimum leads.
    rotated = np.roll(shape, -int(np.argmin(shape)))
    peak_index = int(np.argmax(rotated))
    period_s = 1.0 / fundamental_hz

    def crossing(segment: np.ndarray, level: float, offset: int) -> float:
        above = np.flatnonzero(segment >= level)
        if len(above) == 0:
            return float("nan")
        index = above[0]
        if index == 0:
            return float(offset)
        span = segment[index] - segment[index - 1]
        frac = (level - segment[index - 1]) / span if span else 0.0
        return offset + index - 1 + frac

    rise_lo = crossing(rotated[: peak_index + 1], 0.1, 0)
    rise_hi = crossing(rotated[: peak_index + 1], 0.9, 0)
    falling = rotated[peak_index:][::-1]  # walk backwards from cycle end
    fall_hi = crossing(falling, 0.9, 0)
    fall_lo = crossing(falling, 0.1, 0)
    rise_fraction = (rise_hi - rise_lo) / CYCLE_POINTS
    fall_fraction = (fall_hi - fall_lo) / CYCLE_POINTS
    width50_fraction = float(np.mean(rotated >= 0.5))

    # Thermal lag: PWM ON edge -> in-cycle detector peak (sync captures only).
    lag_ms = None
    if boundary_source == "PWM sync edges":
        # boundaries ARE the ON edges, so the folded peak index is the lag.
        onset_peak = int(np.argmax(mean_cycle))
        lag_ms = 1000.0 * period_s * onset_peak / CYCLE_POINTS

    return {
        "capture": capture_data,
        "fundamental_hz": fundamental_hz,
        "boundary_source": boundary_source,
        "cycle_count": len(cycles),
        "pp_mv": pp_v * 1000.0,
        "shape": shape,
        "mean_cycle": mean_cycle,
        "cycle_std": cycles.std(axis=0) / pp_v,
        "cycle_correlation": cycle_correlation,
        "harmonic_ratios": harmonic_ratios,
        "rise_fraction": rise_fraction,
        "fall_fraction": fall_fraction,
        "width50_fraction": width50_fraction,
        "period_s": period_s,
        "lag_ms": lag_ms,
    }


def align_to_reference(reference_shape: np.ndarray, shape: np.ndarray) -> tuple[int, np.ndarray]:
    """Best circular shift of ``shape`` onto the reference (phase is free)."""
    a = reference_shape - reference_shape.mean()
    b = shape - shape.mean()
    correlation = np.fft.irfft(np.fft.rfft(a) * np.conj(np.fft.rfft(b)), len(a))
    shift = int(np.argmax(correlation))
    return shift, np.roll(shape, shift)


def shape_similarity(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(Pearson correlation, RMS residual in % of pk-pk) of aligned shapes."""
    x = a - a.mean()
    y = b - b.mean()
    corr = float(np.dot(x, y) / max(np.linalg.norm(x) * np.linalg.norm(y), 1e-30))
    residual = float(np.sqrt(np.mean((a - b) ** 2)) * 100.0)
    return corr, residual


# ----------------------------------------------------------------------
# Compare command
# ----------------------------------------------------------------------
def newest_capture_pair(root: Path) -> list[Path]:
    """Most recent legacy capture (the reference) + most recent rig capture."""
    picks: list[Path] = []
    for kind in ("legacy", "rig"):
        matches = sorted(
            root.glob(f"*_{kind}_*.npz"), key=lambda p: p.stat().st_mtime
        )
        if not matches:
            sys.exit(
                f"no *_{kind}_*.npz capture found in {root} - run "
                f"'capture --setup {kind}' first, or pass file paths explicitly"
            )
        picks.append(matches[-1])
    return picks


def compare(args: argparse.Namespace) -> None:
    paths = [Path(p) for p in args.paths]
    if not paths:
        # No arguments: compare the newest legacy capture against the newest
        # rig capture, so the common bench loop is just "capture, capture,
        # compare" with no timestamped filenames to type.
        paths = newest_capture_pair(DEFAULT_OUTPUT_ROOT)
        print("comparing the newest captures (pass file paths to override):")
        for path in paths:
            print(f"  {path}")
    entries = [analyze_capture(load_capture(p)) for p in paths]
    reference = entries[0]

    aligned = [reference["shape"]]
    cross = [(1.0, 0.0)]
    for entry in entries[1:]:
        _, rolled = align_to_reference(reference["shape"], entry["shape"])
        aligned.append(rolled)
        cross.append(shape_similarity(reference["shape"], rolled))

    header = (
        f"{'capture':34s} {'f0 Hz':>7s} {'cyc':>4s} {'pk-pk mV':>9s} "
        f"{'rise ms':>8s} {'fall ms':>8s} {'w50 %':>6s} {'H2/H1':>6s} "
        f"{'H3/H1':>6s} {'corr':>6s} {'resid %':>8s}"
    )
    print(header)
    print("-" * len(header))
    for entry, (corr, residual) in zip(entries, cross):
        name = entry["capture"]["path"].stem
        rise_ms = entry["rise_fraction"] * entry["period_s"] * 1000.0
        fall_ms = entry["fall_fraction"] * entry["period_s"] * 1000.0
        h2, h3 = entry["harmonic_ratios"][:2]
        print(
            f"{name:34.34s} {entry['fundamental_hz']:7.3f} "
            f"{entry['cycle_count']:4d} {entry['pp_mv']:9.2f} "
            f"{rise_ms:8.2f} {fall_ms:8.2f} "
            f"{entry['width50_fraction'] * 100:6.1f} {h2:6.3f} {h3:6.3f} "
            f"{corr:6.3f} {residual:8.2f}"
        )
        details = [f"cycles cut on {entry['boundary_source']}",
                   f"per-cycle repeatability {entry['cycle_correlation']:.3f}"]
        if entry["lag_ms"] is not None:
            details.append(f"PWM-ON -> detector-peak lag {entry['lag_ms']:.1f} ms")
        print(f"{'':34s} ({'; '.join(details)})")
    print(
        "\nreading the numbers: 'corr'/'resid %' compare each capture's "
        "normalised mean cycle against the FIRST file. If the rig capture's "
        "rise time is much longer, its 50 %-width much wider, and H2/H3 "
        "much weaker than the legacy capture's, the miniature emitter is "
        "not swinging fast enough at this chop frequency."
    )

    out_dir = Path(args.out) if args.out else entries[0]["capture"]["path"].parent
    out_path = out_dir / (
        "compare_" + "_vs_".join(e["capture"]["path"].stem[:24] for e in entries)
        + ".png"
    )
    plot_comparison(entries, aligned, cross, out_path, args.show)
    print(f"plot -> {out_path}")


def plot_comparison(entries, aligned, cross, out_path: Path, show: bool) -> None:
    import matplotlib

    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = len(entries) + 2
    fig, axes = plt.subplots(rows, 1, figsize=(11, 2.3 * rows))
    fig.suptitle("legacy chopper vs rig emitter - detector waveform shape",
                 fontsize=12)

    # One short raw snippet per capture, so hookup problems are visible.
    for axis, entry in zip(axes[: len(entries)], entries):
        capture_data = entry["capture"]
        t = capture_data["t_s"]
        snippet = t <= min(t[-1], 4.0 / entry["fundamental_hz"])
        axis.plot(t[snippet], capture_data["waveform_v"][snippet] * 1000.0,
                  lw=0.8)
        axis.set_ylabel("mV")
        axis.set_xlabel("s")
        axis.set_title(
            f"{capture_data['path'].stem} - raw, {entry['fundamental_hz']:.2f} Hz, "
            f"pk-pk {entry['pp_mv']:.1f} mV ({capture_data['setup']} setup)",
            fontsize=9, loc="left",
        )

    # The panel that answers the question: aligned unit-amplitude mean cycles.
    overlay = axes[len(entries)]
    phase = np.arange(CYCLE_POINTS) / CYCLE_POINTS * 100.0
    for entry, shape, (corr, _residual) in zip(entries, aligned, cross):
        label = f"{entry['capture']['path'].stem[:28]}"
        if entry is not entries[0]:
            label += f" (corr {corr:.3f})"
        line, = overlay.plot(phase, shape, lw=1.4, label=label)
        overlay.fill_between(phase, shape - entry["cycle_std"],
                             shape + entry["cycle_std"],
                             color=line.get_color(), alpha=0.15)
    overlay.set_xlabel("cycle phase (%)")
    overlay.set_ylabel("normalised")
    overlay.set_title("mean cycles, unit pk-pk, aligned (band = +/-1 sigma "
                      "per-cycle spread)", fontsize=9, loc="left")
    overlay.legend(fontsize=8)

    spectrum = axes[len(entries) + 1]
    harmonic_numbers = np.arange(2, 6)
    width = 0.8 / len(entries)
    for index, entry in enumerate(entries):
        spectrum.bar(harmonic_numbers + (index - (len(entries) - 1) / 2) * width,
                     entry["harmonic_ratios"], width=width,
                     label=entry["capture"]["path"].stem[:28])
    spectrum.set_xticks(harmonic_numbers)
    spectrum.set_xlabel("harmonic number")
    spectrum.set_ylabel("|Hk| / |H1|")
    spectrum.set_title("harmonic content of the mean cycle - a thermally "
                       "rounded ripple loses these first", fontsize=9, loc="left")
    spectrum.legend(fontsize=8)

    fig.tight_layout(rect=(0, 0, 1, 0.965))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    if show:
        plt.show()
    plt.close(fig)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    capture_parser = commands.add_parser(
        "capture", help="record the detector output on AIN0 to an .npz file"
    )
    capture_parser.add_argument("--setup", choices=("legacy", "rig"),
                                required=True,
                                help="legacy = rig only listens (PWM off); "
                                     "rig = rig drives the miniature emitter")
    capture_parser.add_argument("--freq", type=float, default=18.0,
                                choices=(5.0, 18.0),
                                help="rig drive frequency (qualified TP443 "
                                     "drives only; duty is fixed at 20%%)")
    capture_parser.add_argument("--seconds", type=float, default=30.0,
                                help="capture length (default 30)")
    capture_parser.add_argument("--warmup", type=float, default=10.0,
                                help="rig setup only: emitter thermal settle "
                                     "before capture (default 10 s)")
    capture_parser.add_argument("--label", default="",
                                help="detector id prefixed to the file name")
    capture_parser.add_argument("--port", default=None,
                                help="serial port (default: auto-discover)")
    capture_parser.add_argument("--out", default=None,
                                help=f"output directory (default "
                                     f"{DEFAULT_OUTPUT_ROOT})")
    capture_parser.set_defaults(func=capture)

    compare_parser = commands.add_parser(
        "compare", help="fold, normalise and compare saved captures"
    )
    compare_parser.add_argument("paths", nargs="*",
                                help="capture .npz files; the FIRST is the "
                                     "reference (use the legacy capture). "
                                     "With no paths: the newest legacy and "
                                     "newest rig capture in "
                                     f"{DEFAULT_OUTPUT_ROOT}")
    compare_parser.add_argument("--out", default=None,
                                help="PNG output directory (default: next to "
                                     "the first capture)")
    compare_parser.add_argument("--show", action="store_true",
                                help="open an interactive window too")
    compare_parser.set_defaults(func=compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
