#!/usr/bin/env python3
"""Characterise the 405 M22 noise pipeline's EFFECTIVE passband.

The production noise verdict is not taken on the raw 1000 SPS trace: it runs
on a 20:1 boxcar decimation (a low-pass) whose 1 s windows are each
least-squares detrended (a high-pass). Neither step was ever specified as a
filter, so this tool measures what they actually do:

  1. the boxcar's frequency response, its -3 dB corner, nulls and sidelobes,
     and how much out-of-band energy ALIASES into the analysis band when the
     trace is decimated to 50 SPS;
  2. the per-window detrend's effective high-pass response (measured by
     Monte-Carlo over signal phase, because block detrending is not an LTI
     filter and has no closed-form transfer function);
  3. the two combined - the real passband every PASS/FAIL is judged over;
  4. an estimate, from a real capture's measured spectrum, of what legacy
     amplifier passband would explain the ~700x "effective chain factor"
     when the amplifier's nameplate gain is 4000x.

Item 4 is the useful one for the open calibration question: if the legacy
amp is a band-pass centred near 1 Hz, then "700x" is not a gain at all, it
is 4000x times the amp's average attenuation over the part's noise band -
which would mean the correct fix is to replicate the amp's response
digitally, not to keep tuning a single scalar.

Usage:
    python engineer_tools/filter_response_analysis.py [CAPTURE.npz]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

FS_RAW = 1000.0          # ADS1256 stream rate
N_BOX = 20               # NOISE_DECIMATION_FACTOR
FS_DEC = FS_RAW / N_BOX  # 50 SPS analysis rate
WINDOW_S = 1.0
WIN_N = int(round(FS_DEC * WINDOW_S))   # 50 samples per judged window
NOMINAL_LEGACY_GAIN = 4000.0
EFFECTIVE_LEGACY_FACTOR = 700.0

DEFAULT_CAPTURE = (
    Path.home() / "Documents" / "Eltec_405M22_Test_Results" / "405m22_esp32"
    / "noise_captures" / "lot_500" / "500-44_noise_raw.npz"
)


# ----------------------------------------------------------------- boxcar ---
def boxcar_response(freq_hz, fs=FS_RAW, n=N_BOX):
    """|H(f)| of an N-sample moving average (the Dirichlet/aliased-sinc kernel).

    H(f) = sin(pi f N / fs) / (N sin(pi f / fs)) - exactly 1 at DC, zero at
    every multiple of fs/N, and only ~-13 dB at its first sidelobe.
    """
    f = np.atleast_1d(np.asarray(freq_hz, dtype=float))
    num = np.sin(np.pi * f * n / fs)
    den = n * np.sin(np.pi * f / fs)
    out = np.ones_like(f)
    nonzero = np.abs(den) > 1e-15
    out[nonzero] = num[nonzero] / den[nonzero]
    return np.abs(out)


def boxcar_corner_hz(fs=FS_RAW, n=N_BOX):
    """-3 dB corner of the boxcar, found by bisection (~0.443 * fs/n)."""
    lo, hi = 1e-6, fs / n
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if boxcar_response(mid, fs, n)[0] > 1 / np.sqrt(2):
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------- detrend ---
def detrend_response(freq_hz, fs=FS_DEC, win=WIN_N, n_phase=512):
    """RMS retention of a sinusoid under per-window least-squares detrending.

    Block detrending is NOT shift-invariant: how much of a tone survives
    depends on where the window boundaries fall relative to its phase. The
    honest characterisation is therefore an ensemble ratio over uniformly
    distributed phase, which is what this returns.
    """
    freqs = np.atleast_1d(np.asarray(freq_hz, dtype=float))
    index = np.arange(win)
    t = index / fs
    mean_x = index.mean()
    centred_x = index - mean_x
    denom = np.sum(centred_x**2)
    phases = np.linspace(0.0, 2 * np.pi, n_phase, endpoint=False)
    out = np.empty_like(freqs)
    for position, f in enumerate(freqs):
        sig = np.sin(2 * np.pi * f * t[None, :] + phases[:, None])
        mean_y = sig.mean(axis=1, keepdims=True)
        slope = ((centred_x * (sig - mean_y)).sum(axis=1, keepdims=True)
                 / denom)
        residual = sig - (mean_y + slope * centred_x)
        out[position] = np.sqrt(
            np.mean(residual**2) / max(np.mean(sig**2), 1e-30)
        )
    return out


def response_corner_hz(response_fn, target=1 / np.sqrt(2), lo=1e-3, hi=25.0):
    """Bisect for the frequency where a monotonic-rising response hits target."""
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if response_fn(mid)[0] < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------- aliasing ---
def brickwall_lowpass(waveform, fs, cutoff_hz):
    """Ideal (zero-phase, FFT) low-pass - the reference an ideal decimator has."""
    spectrum = np.fft.rfft(waveform - waveform.mean())
    freq = np.fft.rfftfreq(len(waveform), 1.0 / fs)
    spectrum[freq > cutoff_hz] = 0.0
    return np.fft.irfft(spectrum, len(waveform))


def decimate_boxcar_np(waveform, n):
    blocks = len(waveform) // n
    return waveform[: blocks * n].reshape(blocks, n).mean(axis=1)


def band_rms(waveform, fs, low_hz, high_hz):
    """RMS of the content between low_hz and high_hz (Parseval, one-sided)."""
    x = waveform - waveform.mean()
    spectrum = np.fft.rfft(x)
    freq = np.fft.rfftfreq(len(x), 1.0 / fs)
    mask = (freq >= low_hz) & (freq <= high_hz)
    power = np.sum(np.abs(spectrum[mask]) ** 2) * 2.0 / len(x) ** 2
    return np.sqrt(power)


# ----------------------------------------------------- legacy amp modelling ---
def butter_bandpass_mag(freq_hz, low_hz, high_hz, order=1):
    """Magnitude of a cascaded 1-pole high-pass * 1-pole low-pass band-pass."""
    f = np.atleast_1d(np.asarray(freq_hz, dtype=float))
    mag = np.ones_like(f)
    if low_hz:
        safe = np.maximum(f, 1e-12)
        mag = mag / np.sqrt(1.0 + (low_hz / safe) ** (2 * order))
    if high_hz:
        mag = mag / np.sqrt(1.0 + (f / high_hz) ** (2 * order))
    return mag


def pipeline_response(freq_hz):
    """|H(f)| of the production pipeline: boxcar low-pass * detrend high-pass."""
    return boxcar_response(freq_hz) * detrend_response(freq_hz, n_phase=128)


def effective_factor(psd_freq, psd, low_hz, high_hz, pipeline_mag):
    """Ratio (legacy-amp-weighted RMS) / (app-pipeline-weighted RMS).

    Both sides weight the SAME measured pin spectrum by the response the
    respective instrument actually applies, so the ratio is exactly what the
    700/4000 = 0.175 "effective chain factor" claims to be. Assumes both
    traces are near-Gaussian, so pk-pk tracks RMS.
    """
    amp = butter_bandpass_mag(psd_freq, low_hz, high_hz)
    legacy_power = np.trapezoid((amp * psd) ** 2, psd_freq)
    app_power = np.trapezoid((pipeline_mag * psd) ** 2, psd_freq)
    return np.sqrt(legacy_power / max(app_power, 1e-30))


def welch_asd(waveform, fs, seg_s=4.0):
    """Amplitude spectral density (V/rtHz) by Hann-windowed averaging."""
    x = np.asarray(waveform, dtype=float) - np.mean(waveform)
    seg = min(len(x), int(fs * seg_s))
    step = max(1, seg // 2)
    window = np.hanning(seg)
    scale = 1.0 / (fs * np.sum(window**2))
    spectra = [
        scale * np.abs(np.fft.rfft(x[start:start + seg] * window)) ** 2
        for start in range(0, len(x) - seg + 1, step)
    ]
    psd = np.mean(spectra, axis=0)
    psd[1:-1] *= 2.0
    return np.fft.rfftfreq(seg, 1.0 / fs), np.sqrt(psd)


# ------------------------------------------------------------------- main ---
def main() -> None:
    capture_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CAPTURE
    data = np.load(capture_path)
    raw = np.asarray(data["waveform_v"], dtype=float)
    fs = float(data["sample_rate_hz"])

    print("=" * 72)
    print("1. BOXCAR (20:1 moving average, 1000 -> 50 SPS)")
    print("=" * 72)
    corner = boxcar_corner_hz()
    print(f"  -3 dB corner              : {corner:.2f} Hz  (= 0.443 * 50)")
    print(f"  first null                : {FS_RAW / N_BOX:.0f} Hz "
          f"(then every {FS_RAW / N_BOX:.0f} Hz)")
    print(f"  new Nyquist after decim.  : {FS_DEC / 2:.0f} Hz "
          "-> anything above folds back")
    for f in (10, 22, 25, 40, 50, 60, 100, 120, 150, 180, 250, 350, 450):
        gain = boxcar_response(f)[0]
        db = 20 * np.log10(max(gain, 1e-12))
        alias = abs(((f + FS_DEC / 2) % FS_DEC) - FS_DEC / 2)
        note = "" if f <= FS_DEC / 2 else f"  ALIASES TO {alias:5.1f} Hz"
        print(f"    {f:4d} Hz : {gain:7.4f} ({db:6.1f} dB){note}")

    print()
    print("=" * 72)
    print("2. PER-WINDOW DETREND (least-squares line removed per 1 s window)")
    print("=" * 72)
    d_corner = response_corner_hz(detrend_response)
    print(f"  -3 dB corner              : {d_corner:.2f} Hz  "
          "(acts as a high-pass)")
    for f in (0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0):
        keep = detrend_response(f)[0]
        db = 20 * np.log10(max(keep, 1e-12))
        print(f"    {f:5.2f} Hz : keeps {keep:6.3f} ({db:6.1f} dB)")

    print()
    print("=" * 72)
    print("3. COMBINED EFFECTIVE PASSBAND OF THE PRODUCTION VERDICT")
    print("=" * 72)
    print(f"  high-pass corner (detrend): {d_corner:.2f} Hz")
    print(f"  low-pass corner  (boxcar) : {corner:.2f} Hz")
    print(f"  => verdicts are judged over roughly "
          f"{d_corner:.2f} - {corner:.1f} Hz")

    print()
    print("=" * 72)
    print(f"4. ALIASING CHECK on {capture_path.name}")
    print("=" * 72)
    hf_rms = band_rms(raw, fs, FS_DEC / 2, fs / 2)
    inband_rms = band_rms(raw, fs, 0.5, 22.0)
    print(f"  raw RMS, 0.5-22 Hz (in band) : {inband_rms * 1e6:8.1f} uV")
    print(f"  raw RMS, 25-500 Hz (foldable): {hf_rms * 1e6:8.1f} uV")
    boxcar_dec = decimate_boxcar_np(raw, N_BOX)
    ideal_dec = decimate_boxcar_np(
        brickwall_lowpass(raw, fs, FS_DEC / 2) + raw.mean(), N_BOX
    )
    contamination = boxcar_dec - ideal_dec
    print(f"  decimated RMS (boxcar)       : "
          f"{np.std(boxcar_dec) * 1e6:8.1f} uV")
    print(f"  decimated RMS (ideal AA)     : "
          f"{np.std(ideal_dec) * 1e6:8.1f} uV")
    print(f"  ALIASED-IN residual          : "
          f"{np.std(contamination) * 1e6:8.1f} uV "
          f"({100 * np.std(contamination) / max(np.std(ideal_dec), 1e-30):.1f}%"
          " of the honest signal)")

    print()
    print("=" * 72)
    print("5. WHAT LEGACY PASSBAND WOULD EXPLAIN 700x FROM A 4000x AMP?")
    print("=" * 72)
    target = EFFECTIVE_LEGACY_FACTOR / NOMINAL_LEGACY_GAIN
    print(f"  needed average attenuation: {target:.3f} "
          f"({20 * np.log10(target):.1f} dB) across the part's noise band")
    freq, asd = welch_asd(raw, fs)
    keep = (freq > 0.02) & (freq <= 200.0)
    freq, asd = freq[keep], asd[keep]
    pipeline_mag = pipeline_response(freq)
    print()
    print("  Every model is AC-coupled (a real amp always blocks DC). Each")
    print("  ratio weights the SAME measured spectrum by the amp's response")
    print("  vs the app pipeline's response.")
    print()
    print(f"    {'hp_hz':>7s} {'lp_hz':>7s} {'ratio':>8s} {'implied x':>10s}")
    for low_hz, high_hz in [
        (0.1, 1.0), (0.3, 1.0), (0.5, 1.0), (0.5, 2.0), (0.5, 3.0),
        (0.5, 5.0), (0.5, 22.0), (1.0, 3.0), (1.0, 5.0), (2.0, 5.0),
        (3.0, 10.0), (5.0, 20.0), (0.8, 1.2),
    ]:
        factor = effective_factor(freq, asd, low_hz, high_hz, pipeline_mag)
        flag = "  <== would explain 700x" if abs(factor - target) < 0.02 else ""
        print(f"    {low_hz:7.1f} {high_hz:7.1f} "
              f"{factor:8.3f} {factor * NOMINAL_LEGACY_GAIN:10.0f}{flag}")

    best = None
    for low_hz in np.arange(0.1, 12.01, 0.1):
        for high_hz in np.arange(low_hz + 0.1, 30.01, 0.1):
            factor = effective_factor(freq, asd, low_hz, high_hz, pipeline_mag)
            if best is None or abs(factor - target) < abs(best[2] - target):
                best = (low_hz, high_hz, factor)
    print()
    print(f"  closest band-pass over a 0.1-12 / 0.1-30 Hz grid: "
          f"hp={best[0]:.1f} Hz, lp={best[1]:.1f} Hz -> ratio {best[2]:.3f}")
    if abs(best[2] - target) > 0.02:
        print("  ** NO physically sensible band-pass reaches 0.175. The 700x")
        print("     is therefore NOT explained by the amp's passband alone --")
        print("     look for a real gain difference (10x probe, range switch).")

    print()
    print("  NOTE: this assumes the amp's nameplate 4000x is its midband gain")
    print("  and that both traces are near-Gaussian (pk-pk tracks RMS). It is")
    print("  a hypothesis generator for the amp builder, not a calibration.")

    make_figure(corner, d_corner, raw, fs, capture_path)


def make_figure(box_corner, det_corner, raw, fs, capture_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(11, 10))

    freq = np.logspace(-2, np.log10(500), 3000)
    box = boxcar_response(freq)
    # The detrend runs on the ALREADY-decimated 50 SPS trace, so it is only
    # defined below that Nyquist; simulating it above 25 Hz just aliases.
    sub_nyquist = freq <= FS_DEC / 2
    det = detrend_response(freq[sub_nyquist])
    combined = box[sub_nyquist] * det

    ax = axes[0]
    ax.semilogx(freq, 20 * np.log10(np.maximum(box, 1e-6)),
                label=f"boxcar 20:1 (low-pass, -3 dB @ {box_corner:.1f} Hz)")
    ax.semilogx(freq[sub_nyquist], 20 * np.log10(np.maximum(det, 1e-6)),
                label=f"per-window detrend (high-pass, -3 dB @ {det_corner:.2f} Hz)")
    ax.semilogx(freq[sub_nyquist], 20 * np.log10(np.maximum(combined, 1e-6)),
                lw=2.2, color="k", label="combined = what the verdict sees")
    ax.axhline(-3, color="0.6", ls=":", lw=1)
    ax.axvspan(0.5, 5.0, color="tab:green", alpha=0.12,
               label="part's real noise band 0.5-5 Hz")
    ax.axvline(25, color="tab:red", ls="--", lw=1,
               label="25 Hz: Nyquist after decimation (fold point)")
    ax.set_ylim(-60, 6)
    ax.set_xlabel("Hz")
    ax.set_ylabel("dB")
    ax.set_title("Effective passband of the production noise verdict",
                 fontsize=10, loc="left")
    ax.legend(fontsize=8, loc="lower center")
    ax.grid(alpha=0.3, which="both")

    ax = axes[1]
    lin_f = np.linspace(0.1, 300, 6000)
    ax.plot(lin_f, boxcar_response(lin_f), lw=1, color="tab:blue")
    ax.axhline(0, color="0.7", lw=0.5)
    for null in range(50, 301, 50):
        ax.axvline(null, color="0.85", lw=0.8)
    ax.axvspan(0, 25, color="tab:green", alpha=0.12)
    for centre in (50, 100, 150, 200, 250, 300):
        ax.axvspan(centre - 25, centre + 25, color="tab:red", alpha=0.05)
    ax.annotate("everything in a red band folds into the green band",
                xy=(150, 0.5), fontsize=8, ha="center")
    ax.set_xlabel("Hz")
    ax.set_ylabel("|H|")
    ax.set_title("Boxcar sidelobes are the aliasing leak "
                 "(nulls at 50 Hz multiples, sidelobes only ~-13 dB)",
                 fontsize=10, loc="left")
    ax.grid(alpha=0.3)

    ax = axes[2]
    f_asd, asd = welch_asd(raw, fs)
    keep = f_asd > 0.05
    ax.loglog(f_asd[keep], asd[keep] * 1e6, lw=0.8, color="0.35",
              label=f"{capture_path.stem} measured pin noise")
    for low_hz, high_hz, style in [
        (None, 2.0, "--"), (0.5, 3.0, "-."), (0.3, 1.5, ":"),
    ]:
        model = butter_bandpass_mag(f_asd[keep], low_hz, high_hz)
        label = (f"amp model lp={high_hz} Hz"
                 + (f", hp={low_hz} Hz" if low_hz else ""))
        ax.loglog(f_asd[keep], asd[keep] * 1e6 * model, style, lw=1,
                  label=label)
    ax.axvspan(0.5, 5.0, color="tab:green", alpha=0.12)
    ax.set_xlabel("Hz")
    ax.set_ylabel("uV/sqrt(Hz)")
    ax.set_title("Measured pin spectrum vs candidate legacy-amp passbands",
                 fontsize=10, loc="left")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, which="both")

    fig.tight_layout()
    out = capture_path.parent / "pipeline_filter_response.png"
    fig.savefig(out, dpi=110)
    print(f"\nfigure -> {out}")


if __name__ == "__main__":
    main()
