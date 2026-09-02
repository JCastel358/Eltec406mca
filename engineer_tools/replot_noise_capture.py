#!/usr/bin/env python3
"""Replot saved 405 M22 noise captures under selectable frequency bands.

Every noise capture the tester saves (``*_noise_raw.npz`` / ``.csv`` under
``~/Documents/Eltec_405M22_Test_Results/405m22_esp32/noise_captures/``) holds
the full RAW 1000 SPS pin waveform, so any band-limiting choice can be
replayed offline — no new hardware filter and no re-capture needed. This tool
loads those captures and, for each one, shows side by side:

  * the raw wideband trace (what the ADC saw, 0-500 Hz),
  * the production analysis trace (the app's exact 20:1 boxcar + per-window
    detrend pipeline, imported from ``stability_analysis.py``), and
  * any number of custom bands (``--band LO HI`` / ``--lowpass FC`` /
    ``--boxcar N``) judged by the same windowed pk-pk rule,

plus the amplitude spectral density of the raw capture so you can see WHERE
the noise lives before choosing a band. A summary table compares worst /
median window pk-pk and the would-be verdict under every band.

Background: the legacy fixture's amplifier is AC-coupled and band-limited
around the 1 Hz signal, so its 300 mV limit was defined over a narrow band.
This rig captures wideband instead and band-limits in software — which is
the correct architecture: sampling slower (e.g. 10 SPS) would NOT remove
out-of-band noise, it would alias it into the band. Custom band-pass filters
here are zero-phase Butterworth-magnitude responses applied via FFT
(numpy-only; reflection padding suppresses edge transients).

Examples (run from the repo root):

  python engineer_tools/replot_noise_capture.py                # all captures, default bands
  python engineer_tools/replot_noise_capture.py --band 0.5 2 --band 0.3 5
  python engineer_tools/replot_noise_capture.py <dir-or-file> --show
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "single_detector_rig" / "m405m22"))
import stability_analysis as sa  # noqa: E402  (the production analysis math)

DEFAULT_CAPTURE_ROOT = (
    Path.home()
    / "Documents"
    / "Eltec_405M22_Test_Results"
    / "405m22_esp32"
    / "noise_captures"
)
# Production constants (mirrors the app; overridable per file by its metadata)
DEFAULT_LIMIT_MV = 300.0 / 700.0  # legacy 300 mV behind the ~700x chain
DEFAULT_ALLOW_FRACTION = 0.15
DEFAULT_DECIMATION = 20
WINDOW_S = 1.0
CLIP_LIMIT_V = 4.9


def load_capture(path: Path) -> dict:
    """Return {'waveform_v', 'rate_hz', 'meta'} from an .npz or .csv capture."""
    if path.suffix.lower() == ".npz":
        data = np.load(path)
        meta = {
            key: str(data[key])
            for key in data.files
            if key != "waveform_v" and data[key].ndim == 0
        }
        return {
            "waveform_v": np.asarray(data["waveform_v"], dtype=float),
            "rate_hz": float(data["sample_rate_hz"]),
            "meta": meta,
            # 2026-08-31+ captures archive the FIR's edge context so a
            # replay seats the filter exactly like the live verdict did.
            "left_context_v": (
                np.asarray(data["left_context_v"], dtype=float)
                if "left_context_v" in data.files
                else None
            ),
            "right_context_v": (
                np.asarray(data["right_context_v"], dtype=float)
                if "right_context_v" in data.files
                else None
            ),
        }
    # CSV: "# key=value; key=value; ..." header, then sample,t_s,volts rows.
    with path.open("r", encoding="utf-8") as handle:
        first = handle.readline().strip()
    meta: dict[str, str] = {}
    if first.startswith("#"):
        for pair in first.lstrip("#").split(";"):
            if "=" in pair:
                key, _, value = pair.strip().partition("=")
                meta[key] = value
    volts = np.loadtxt(path, delimiter=",", skiprows=2 if meta else 1, usecols=2)
    return {
        "waveform_v": np.asarray(volts, dtype=float),
        "rate_hz": float(meta.get("sample_rate_hz", 1000.0)),
        "meta": meta,
        "left_context_v": None,
        "right_context_v": None,
    }


def fft_band_filter(
    waveform_v: np.ndarray,
    rate_hz: float,
    low_hz: float | None,
    high_hz: float | None,
    order: int = 4,
) -> np.ndarray:
    """Zero-phase Butterworth-magnitude band filter via FFT (numpy only).

    Reflection padding (two low-frequency periods each side) keeps the FFT's
    implicit periodicity from injecting wrap-around transients at the edges.
    """
    x = np.asarray(waveform_v, dtype=float)
    mean = x.mean()
    pad = len(x)
    if low_hz:
        pad = min(len(x), int(round(2.0 * rate_hz / low_hz)))
    padded = np.concatenate([x[pad - 1 :: -1], x, x[: -pad - 1 : -1]]) - mean
    freq = np.fft.rfftfreq(len(padded), 1.0 / rate_hz)
    response = np.ones_like(freq)
    if high_hz:
        response /= np.sqrt(1.0 + (freq / high_hz) ** (2 * order))
    if low_hz:
        safe = np.maximum(freq, 1e-12)
        response /= np.sqrt(1.0 + (low_hz / safe) ** (2 * order))
        response[0] = 0.0
    filtered = np.fft.irfft(np.fft.rfft(padded) * response, len(padded))
    out = filtered[pad : pad + len(x)]
    return out if low_hz else out + mean


def judge_trace(
    judged_v: np.ndarray,
    raw_v: np.ndarray,
    judged_rate_hz: float,
    limit_mv: float,
    allow_fraction: float,
    detrend: bool,
) -> tuple[sa.NoiseAnalysis, np.ndarray]:
    """Run the production windowed pk-pk rule on an arbitrary filtered trace.

    Mirrors analyze_noise_capture_band_limited: optional per-window detrend,
    pk-pk vs the limit per 1 s window, clipping checked on the RAW samples of
    each window. Returns the analysis and the exact trace that was judged.
    """
    window_samples = max(1, int(round(WINDOW_S * judged_rate_hz)))
    segments = sa.fixed_window_segments(len(judged_v), window_samples)
    if not segments:
        raise ValueError("capture holds no complete analysis window")
    if detrend:
        judged = np.asarray(sa.detrend_window_segments(judged_v, segments))
    else:
        judged = np.asarray(judged_v[: segments[-1][1]], dtype=float)
    base = sa.analyze_noise_capture(
        judged,
        judged_rate_hz,
        window_s=WINDOW_S,
        threshold_mv=limit_mv,
        max_over_fraction=allow_fraction,
        clip_limit_v=1e30,
    )
    ratio = len(raw_v) / len(judged_v) if len(judged_v) else 1.0
    windows_over = 0
    clipped = 0
    for (start, end), pp_mv in zip(segments, base.window_pp_mv):
        raw_win = raw_v[int(start * ratio) : int(end * ratio)]
        is_clipped = bool(np.any(np.abs(raw_win) >= CLIP_LIMIT_V))
        clipped += is_clipped
        windows_over += is_clipped or pp_mv > limit_mv + 1e-12
    allowed = int(len(segments) * allow_fraction + 1e-9)
    analysis = sa.NoiseAnalysis(
        windows_total=len(segments),
        windows_over=windows_over,
        over_fraction=windows_over / len(segments),
        worst_pp_mv=base.worst_pp_mv,
        median_pp_mv=base.median_pp_mv,
        window_pp_mv=base.window_pp_mv,
        clipped_windows=clipped,
        passed=windows_over <= allowed,
    )
    return analysis, judged


def spectral_density(waveform_v: np.ndarray, rate_hz: float):
    """Averaged-periodogram ASD in uV/rtHz (Hann windows, 50% overlap)."""
    x = np.asarray(waveform_v, dtype=float) - np.mean(waveform_v)
    seg = min(len(x), int(rate_hz * 4))
    step = max(1, seg // 2)
    window = np.hanning(seg)
    scale = 1.0 / (rate_hz * np.sum(window**2))
    spectra = [
        scale * np.abs(np.fft.rfft(x[start : start + seg] * window)) ** 2
        for start in range(0, len(x) - seg + 1, step)
    ]
    psd = np.mean(spectra, axis=0)
    psd[1:-1] *= 2.0
    freq = np.fft.rfftfreq(seg, 1.0 / rate_hz)
    return freq, np.sqrt(psd) * 1e6


def analyze_file(path: Path, variants, limit_override, allow_fraction, detrend):
    capture = load_capture(path)
    raw = capture["waveform_v"]
    rate = capture["rate_hz"]
    meta = capture["meta"]
    limit_mv = limit_override or float(
        meta.get("noise_pp_limit_mv", DEFAULT_LIMIT_MV)
    )
    results = []
    for kind, params in variants:
        if kind == "app":
            factor = int(meta.get("noise_decimation_factor", DEFAULT_DECIMATION))
            analysis, judged, judged_rate = sa.analyze_noise_capture_band_limited(
                raw,
                rate,
                decimation_factor=factor,
                window_s=WINDOW_S,
                threshold_mv=limit_mv,
                max_over_fraction=allow_fraction,
                clip_limit_v=CLIP_LIMIT_V,
                detrend_windows=detrend,
                left_context_v=capture.get("left_context_v"),
                right_context_v=capture.get("right_context_v"),
            )
            judged = np.asarray(judged)
            label = (
                f"app pipeline (AA-FIR {factor}:1 -> {rate/factor:g} SPS + detrend)"
            )
        elif kind == "boxcar":
            factor = params
            judged_rate = rate / factor
            filtered = np.asarray(sa.decimate_boxcar(raw, factor))
            analysis, judged = judge_trace(
                filtered, raw, judged_rate, limit_mv, allow_fraction, detrend
            )
            label = f"boxcar {factor}:1 (~{0.443 * judged_rate:.1f} Hz)"
        else:  # band
            low_hz, high_hz = params
            judged_rate = rate
            filtered = fft_band_filter(raw, rate, low_hz, high_hz)
            analysis, judged = judge_trace(
                filtered, raw, judged_rate, limit_mv, allow_fraction, detrend
            )
            if low_hz:
                label = f"band-pass {low_hz:g}-{high_hz:g} Hz"
            else:
                label = f"low-pass {high_hz:g} Hz"
        results.append(
            {
                "label": label,
                "analysis": analysis,
                "judged": judged,
                "judged_rate": judged_rate,
            }
        )
    return {
        "path": path,
        "raw": raw,
        "rate": rate,
        "meta": meta,
        "limit_mv": limit_mv,
        "results": results,
    }


def plot_file(entry, out_dir: Path, show: bool):
    import matplotlib

    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    raw = entry["raw"]
    rate = entry["rate"]
    limit_uv = entry["limit_mv"] * 1000.0
    results = entry["results"]
    name = entry["path"].stem

    rows = 2 + len(results)
    fig, axes = plt.subplots(rows, 1, figsize=(12, 2.4 * rows), sharex=False)
    fig.suptitle(
        f"{name} — recorded {entry['meta'].get('recorded_at', '?')}, "
        f"stored verdict {entry['meta'].get('noise_outcome', '?')}, "
        f"limit {limit_uv:.0f} µV pk-pk/window",
        fontsize=11,
    )

    t_raw = np.arange(len(raw)) / rate
    axes[0].plot(t_raw, (raw - raw.mean()) * 1e6, lw=0.4, color="0.4")
    axes[0].set_ylabel("raw µV")
    axes[0].set_title(
        f"raw {rate:g} SPS (mean-removed) — wideband, includes noise the "
        "legacy amp never passed",
        fontsize=9,
        loc="left",
    )

    for axis, result in zip(axes[1:-1], results):
        judged = result["judged"] * 1e6
        t = np.arange(len(judged)) / result["judged_rate"]
        analysis = result["analysis"]
        axis.plot(t, judged, lw=0.6, color="tab:blue")
        axis.axhline(limit_uv / 2, color="red", lw=1)
        axis.axhline(-limit_uv / 2, color="red", lw=1)
        window_samples = int(round(WINDOW_S * result["judged_rate"]))
        for index, pp_mv in enumerate(analysis.window_pp_mv):
            if pp_mv > entry["limit_mv"]:
                axis.axvspan(
                    index * WINDOW_S, (index + 1) * WINDOW_S,
                    color="red", alpha=0.12,
                )
        verdict = "PASS" if analysis.passed else "FAIL"
        axis.set_title(
            f"{result['label']} — {verdict}: worst "
            f"{analysis.worst_pp_mv*1000:.0f} µV, median "
            f"{analysis.median_pp_mv*1000:.0f} µV, "
            f"{analysis.windows_over}/{analysis.windows_total} windows over",
            fontsize=9,
            loc="left",
            color="darkgreen" if analysis.passed else "darkred",
        )
        axis.set_ylabel("µV")
        span = max(1.2 * np.max(np.abs(judged)), limit_uv)
        axis.set_ylim(-span, span)

    freq, asd = spectral_density(raw, rate)
    axes[-1].loglog(freq[1:], asd[1:], lw=0.8, color="0.3")
    axes[-1].axvspan(0.5, 5.0, color="tab:green", alpha=0.15,
                     label="part noise band (0.5–5 Hz)")
    axes[-1].axvline(1.0, color="tab:green", lw=1, ls="--", label="1 Hz signal")
    axes[-1].set_xlabel("Hz")
    axes[-1].set_ylabel("µV/√Hz")
    axes[-1].set_title("raw amplitude spectral density — where the noise lives",
                       fontsize=9, loc="left")
    axes[-1].legend(fontsize=8, loc="upper right")
    for axis in axes[:-1]:
        axis.set_xlabel("s")

    fig.tight_layout(rect=(0, 0, 1, 0.96))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}_replot.png"
    fig.savefig(out_path, dpi=110)
    if show:
        plt.show()
    plt.close(fig)
    return out_path


def collect_paths(arguments: list[str]) -> list[Path]:
    roots = [Path(a) for a in arguments] if arguments else [DEFAULT_CAPTURE_ROOT]
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            found = sorted(root.rglob("*_noise_raw*.npz"))
            # fall back to CSVs that have no NPZ twin
            npz_stems = {p.with_suffix("").name for p in found}
            found += [
                p
                for p in sorted(root.rglob("*_noise_raw*.csv"))
                if p.with_suffix("").name not in npz_stems
            ]
            files += found
        elif root.exists():
            files.append(root)
        else:
            sys.exit(f"path not found: {root}")
    if not files:
        sys.exit("no *_noise_raw* captures found")
    return files


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("paths", nargs="*",
                        help="capture files or directories "
                             f"(default: {DEFAULT_CAPTURE_ROOT})")
    parser.add_argument("--band", nargs=2, type=float, action="append",
                        metavar=("LO_HZ", "HI_HZ"),
                        help="add a zero-phase band-pass variant (repeatable)")
    parser.add_argument("--lowpass", type=float, action="append", metavar="HZ",
                        help="add a zero-phase low-pass variant (repeatable)")
    parser.add_argument("--boxcar", type=int, action="append", metavar="N",
                        help="add a boxcar N:1 decimation variant (repeatable)")
    parser.add_argument("--no-app", action="store_true",
                        help="skip the production-pipeline variant")
    parser.add_argument("--limit-mv", type=float, default=None,
                        help="override the pk-pk limit (default: each file's "
                             "stored noise_pp_limit_mv)")
    parser.add_argument("--allow", type=float, default=DEFAULT_ALLOW_FRACTION,
                        help="allowed over-limit window fraction "
                             f"(default {DEFAULT_ALLOW_FRACTION})")
    parser.add_argument("--no-detrend", action="store_true",
                        help="skip the per-window least-squares detrend")
    parser.add_argument("--out", type=Path, default=None,
                        help="PNG output directory (default: <capture dir>/replots)")
    parser.add_argument("--show", action="store_true",
                        help="open interactive windows as well as saving PNGs")
    args = parser.parse_args()

    variants: list[tuple[str, object]] = []
    if not args.no_app:
        variants.append(("app", None))
    for factor in args.boxcar or []:
        variants.append(("boxcar", factor))
    for cutoff in args.lowpass or []:
        variants.append(("band", (None, cutoff)))
    for low, high in args.band or []:
        variants.append(("band", (low, high)))
    if len(variants) == (0 if args.no_app else 1):
        # No custom variants requested: default to a 1 Hz-focused band pair.
        variants.append(("band", (0.5, 5.0)))
        variants.append(("band", (0.3, 10.0)))

    files = collect_paths(args.paths)
    header = f"{'capture':28s} {'variant':42s} {'worstuV':>8s} {'meduV':>7s} {'over':>6s} verdict"
    print(header)
    print("-" * len(header))
    for path in files:
        entry = analyze_file(
            path, variants, args.limit_mv, args.allow, not args.no_detrend
        )
        stored = entry["meta"].get("noise_outcome", "")
        for index, result in enumerate(entry["results"]):
            analysis = result["analysis"]
            verdict = "PASS" if analysis.passed else "FAIL"
            note = ""
            if index == 0 and not args.no_app and stored:
                note = ("  (= stored)" if verdict == stored
                        else f"  (STORED WAS {stored}!)")
            print(
                f"{path.stem:28s} {result['label']:42s} "
                f"{analysis.worst_pp_mv*1000:8.0f} "
                f"{analysis.median_pp_mv*1000:7.0f} "
                f"{analysis.windows_over:3d}/{analysis.windows_total:<2d} "
                f"{verdict}{note}"
            )
        out_dir = args.out or (path.parent / "replots")
        out_path = plot_file(entry, out_dir, args.show)
        print(f"{'':28s} -> {out_path}")
    print(
        "\nnote: custom-band verdicts reuse the stored per-window limit, which "
        "was calibrated for the app's 20:1 pipeline; treat them as relative "
        "comparisons until the limit is re-derived for that band."
    )


if __name__ == "__main__":
    main()
