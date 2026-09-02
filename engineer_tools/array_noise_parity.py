#!/usr/bin/env python3
"""Derive the array rig's pin-level noise limits from a paired lot (engineering only).

TP120 judges the model 40623's noise as a DMM reading behind the legacy
amplifier box 9000232 and rectifier-hold circuit 9000272 ("between 10.0 mV
and 37.9 mV"). The array rig measures the pin directly (windowed pk-pk in
the single rig's ~0.85-22 Hz band). The two are related by the legacy
chain's effective factor - which nobody has on paper, exactly as with the
405 M22's 300 mV / 700 (CALIBRATION_RECORD section 2.2). This tool derives
it the way that factor was derived: the SAME parts measured on both
fixtures, paired, ratioed.

Inputs
------
--legacy legacy_readings.csv   typed from the TP120 data sheet: columns
                               sensor_id, legacy_noise_mv [, position, tray]
--array  <lot CSV | tray .npz | directory of tray_*_raw.npz>
                               the array rig's measurements. A lot CSV gives
                               the recorded worst/median pk-pk per sensor;
                               .npz captures are re-analysed here (and can
                               be replayed through other bands).

Pairing is by sensor_id (``<lot>-<number>``); when the legacy sheet has no
ids but has tray + position, pairs are made on (tray, position).

Outputs
-------
For each metric (worst window pk-pk, median window pk-pk): the count, the
median ratio legacy/array, the regression-through-origin slope, the spread,
and the proposed constants ``NOISE_PP_LIMIT_LOW_MV = 10.0 / factor`` and
``NOISE_PP_LIMIT_HIGH_MV = 37.9 / factor``. Also ``parity_<date>.csv`` (the
pairs) and ``parity_<date>.png`` (scatter + fits) under --out (default:
``<array results root>/calibration/``). The tool never edits the app: fill
the constants in ``array_rig/m40623/array_analysis.py`` by hand, bump
``CALIBRATION_ID``, update CALIBRATION_RECORD section 4b - one commit.

Examples (from the repository root):

  python engineer_tools/array_noise_parity.py --legacy legacy_readings.csv --array "%USERPROFILE%/Documents/Eltec_40623_Test_Results/40623_array_daq/40623_array_lot_12.csv"
  python engineer_tools/array_noise_parity.py --legacy legacy_readings.csv --array <dir with tray_*_raw.npz> --replay-band 0.5 5 --replay-band 0.3 10
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "array_rig" / "m40623"))
import array_analysis as aa  # noqa: E402  (the array rig's production analysis)

LEGACY_LOW_MV = aa.NOISE_LEGACY_PP_LIMIT_LOW_MV
LEGACY_HIGH_MV = aa.NOISE_LEGACY_PP_LIMIT_HIGH_MV
METRICS = ("worst", "median")


# ----------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------
def read_legacy(path: Path) -> list[dict]:
    rows = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            value = row.get("legacy_noise_mv") or row.get("legacy_mv") or row.get("noise_mv")
            if not value:
                continue
            rows.append({
                "sensor_id": row.get("sensor_id", ""),
                "position": row.get("position", ""),
                "tray": row.get("tray", "") or row.get("tray_number", ""),
                "legacy_mv": float(value),
            })
    if not rows:
        sys.exit(f"{path}: no legacy readings found (need a legacy_noise_mv column)")
    return rows


def read_array_csv(path: Path) -> dict[tuple, dict]:
    """Latest tray attempt per sensor_id from the tester's lot CSV."""

    records: dict[tuple, dict] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            worst = row.get("noise_worst_pp_mv", "")
            median = row.get("noise_median_pp_mv", "")
            if not worst or not median:
                continue
            key_id = (row.get("sensor_id") or "").strip()
            key_pos = ((row.get("tray_number") or "").strip(), (row.get("position") or "").strip())
            record = {
                "sensor_id": key_id, "tray": key_pos[0], "position": key_pos[1],
                "worst": float(worst), "median": float(median), "source": path.name,
                "attempt": int(row.get("tray_attempt") or 0),
            }
            for key in ((("id", key_id) if key_id else None), ("pos", key_pos)):
                if key is None:
                    continue
                previous = records.get(key)
                if previous is None or record["attempt"] >= previous["attempt"]:
                    records[key] = record
    return records


def load_tray_npz(path: Path) -> dict:
    data = np.load(path)
    meta = {k: str(data[k]) for k in data.files if data[k].ndim == 0 and k not in ("sample_rate_hz",)}
    return {
        "path": path,
        "waveform_v": np.asarray(data["waveform_v"], dtype=np.float64),
        "rate_hz": float(data["sample_rate_hz"]),
        "left": np.asarray(data["left_context_v"], dtype=np.float64) if "left_context_v" in data.files else None,
        "right": np.asarray(data["right_context_v"], dtype=np.float64) if "right_context_v" in data.files else None,
        "positions": [str(p) for p in data["positions"]],
        "sensor_numbers": [int(n) for n in data["sensor_numbers"]] if "sensor_numbers" in data.files else [0] * 50,
        "occupancy": [str(o) for o in data["occupancy"]] if "occupancy" in data.files else ["LOADED"] * 50,
        "lot": meta.get("lot_number", ""),
        "tray": meta.get("tray_number", ""),
        "meta": meta,
    }


def analyze_tray(tray: dict, *, band: tuple[float, float] | None = None) -> list[dict]:
    """Per-position worst/median pk-pk (mV): production band, or a replay band."""

    raw = tray["waveform_v"]
    rate = tray["rate_hz"]
    if band is None:
        results, _, _ = aa.analyze_tray_noise(
            raw, rate, positions=tray["positions"], limits=aa.NoiseLimits(),
            left_context=tray["left"], right_context=tray["right"],
        )
        worst = [r.worst_pp_mv for r in results]
        median = [r.median_pp_mv for r in results]
    else:
        filtered = np.stack([fft_band_filter(raw[c], rate, band[0], band[1]) for c in range(raw.shape[0])])
        window = int(round(aa.NOISE_WINDOW_S * rate))
        judged = aa.detrend_window_segments_multi(filtered, window)
        pp = aa.window_peak_to_peak_mv_multi(judged, window)
        worst = [float(v) for v in pp.max(axis=1)]
        median = [float(v) for v in np.median(pp, axis=1)]
    out = []
    for c, position in enumerate(tray["positions"]):
        number = tray["sensor_numbers"][c]
        if tray["occupancy"][c] != "LOADED" or number <= 0:
            continue
        out.append({
            "sensor_id": f"{tray['lot']}-{number}", "tray": tray["tray"], "position": position,
            "worst": worst[c], "median": median[c], "source": tray["path"].name, "attempt": 0,
        })
    return out


def fft_band_filter(waveform_v: np.ndarray, rate_hz: float, low_hz: float | None, high_hz: float | None, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth-magnitude band filter via FFT (as engineer_tools/replot_noise_capture.py)."""

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


def collect_array(path: Path, *, band: tuple[float, float] | None = None) -> dict[tuple, dict]:
    if path.is_file() and path.suffix.lower() == ".csv":
        if band is not None:
            sys.exit("--replay-band needs .npz captures, not the lot CSV")
        return read_array_csv(path)
    files = [path] if path.is_file() else sorted(path.rglob("tray_*_raw*.npz"))
    if not files:
        sys.exit(f"{path}: no tray_*_raw.npz captures found")
    records: dict[tuple, dict] = {}
    for file in files:
        for record in analyze_tray(load_tray_npz(file), band=band):
            if record["sensor_id"]:
                records[("id", record["sensor_id"])] = record
            records[("pos", (record["tray"], record["position"]))] = record
    return records


# ----------------------------------------------------------------------
# pairing and statistics
# ----------------------------------------------------------------------
def pair(legacy: list[dict], array: dict[tuple, dict]) -> list[dict]:
    pairs = []
    for row in legacy:
        record = None
        if row["sensor_id"]:
            record = array.get(("id", row["sensor_id"]))
        if record is None and row["position"]:
            record = array.get(("pos", (row["tray"], row["position"])))
        if record is None:
            continue
        pairs.append({**row, "worst": record["worst"], "median": record["median"], "array_position": record["position"], "source": record["source"]})
    return pairs


def fit(pairs: list[dict], metric: str) -> dict:
    legacy = np.array([p["legacy_mv"] for p in pairs], dtype=float)
    array = np.array([p[metric] for p in pairs], dtype=float)
    keep = (array > 0) & (legacy > 0)
    legacy, array = legacy[keep], array[keep]
    if legacy.size == 0:
        return {"n": 0}
    ratios = legacy / array
    slope = float(np.sum(legacy * array) / np.sum(array * array))
    predicted = slope * array
    ss_res = float(np.sum((legacy - predicted) ** 2))
    ss_tot = float(np.sum((legacy - legacy.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {
        "n": int(legacy.size), "median_ratio": float(np.median(ratios)), "mean_ratio": float(ratios.mean()),
        "sd_ratio": float(ratios.std(ddof=1)) if ratios.size > 1 else 0.0,
        "cv_percent": float(100.0 * ratios.std(ddof=1) / ratios.mean()) if ratios.size > 1 and ratios.mean() else 0.0,
        "slope": slope, "r2": r2, "ratios": ratios, "legacy": legacy, "array": array,
    }


def proposed_limits(factor: float) -> tuple[float, float]:
    return LEGACY_LOW_MV / factor, LEGACY_HIGH_MV / factor


def decisions_agree(pairs: list[dict], metric: str, factor: float) -> tuple[int, int]:
    """How many parts the proposed pin-level window classifies like the legacy window."""

    low, high = proposed_limits(factor)
    agree = 0
    for p in pairs:
        legacy_ok = LEGACY_LOW_MV <= p["legacy_mv"] <= LEGACY_HIGH_MV
        array_ok = low <= p[metric] <= high
        agree += legacy_ok == array_ok
    return agree, len(pairs)


# ----------------------------------------------------------------------
# outputs
# ----------------------------------------------------------------------
def default_out_dir() -> Path:
    override = os.environ.get("ELTEC_ARRAY_RESULTS_ROOT", "").strip()
    root = Path(override).expanduser() if override else Path.home() / "Documents" / "Eltec_40623_Test_Results" / "40623_array_daq"
    return root / "calibration"


def write_pairs_csv(path: Path, pairs: list[dict], fits: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sensor_id", "tray", "position", "legacy_mv", "array_worst_pp_mv", "array_median_pp_mv", "ratio_worst", "ratio_median", "source"])
        for p in pairs:
            writer.writerow([
                p["sensor_id"], p["tray"], p["array_position"], f"{p['legacy_mv']:.4f}", f"{p['worst']:.6f}", f"{p['median']:.6f}",
                f"{p['legacy_mv'] / p['worst']:.3f}" if p["worst"] else "", f"{p['legacy_mv'] / p['median']:.3f}" if p["median"] else "", p["source"],
            ])
        writer.writerow([])
        for metric, result in fits.items():
            if result.get("n"):
                writer.writerow([f"{metric}: n={result['n']} median_ratio={result['median_ratio']:.3f} slope={result['slope']:.3f} cv%={result['cv_percent']:.1f} r2={result['r2']:.3f}"])


def plot(path: Path, pairs: list[dict], fits: dict[str, dict], title: str) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for axis, metric in zip(axes, METRICS):
        result = fits[metric]
        if not result.get("n"):
            axis.set_title(f"{metric}: no pairs")
            continue
        axis.scatter(result["array"] * 1000.0, result["legacy"], s=18, color="tab:blue")
        xs = np.linspace(0, result["array"].max() * 1.05, 50)
        axis.plot(xs * 1000.0, result["slope"] * xs, color="tab:red", lw=1, label=f"slope {result['slope']:.1f}")
        axis.plot(xs * 1000.0, result["median_ratio"] * xs, color="tab:green", lw=1, ls="--", label=f"median ratio {result['median_ratio']:.1f}")
        axis.axhspan(LEGACY_LOW_MV, LEGACY_HIGH_MV, color="tab:green", alpha=0.08, label="TP120 window 10.0-37.9 mV")
        axis.set_xlabel(f"array {metric} window pk-pk (uV, judged band)")
        axis.set_ylabel("legacy DMM reading (mV)")
        axis.set_title(f"{metric}: n={result['n']}, cv {result['cv_percent']:.1f} %, r2 {result['r2']:.2f}")
        axis.legend(fontsize=8)
    fig.suptitle(title)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def report(pairs: list[dict], fits: dict[str, dict], *, label: str) -> None:
    print(f"\n=== {label}: {len(pairs)} paired parts ===")
    header = f"{'sensor_id':12s} {'pos':6s} {'legacy mV':>10s} {'worst uV':>10s} {'median uV':>10s} {'ratio w':>9s} {'ratio m':>9s}"
    print(header)
    print("-" * len(header))
    for p in pairs:
        rw = p["legacy_mv"] / p["worst"] if p["worst"] else float("nan")
        rm = p["legacy_mv"] / p["median"] if p["median"] else float("nan")
        print(f"{p['sensor_id']:12s} {p['array_position']:6s} {p['legacy_mv']:10.2f} {p['worst'] * 1000:10.1f} {p['median'] * 1000:10.1f} {rw:9.1f} {rm:9.1f}")
    for metric in METRICS:
        result = fits[metric]
        if not result.get("n"):
            print(f"\n{metric}: no usable pairs")
            continue
        low_m, high_m = proposed_limits(result["median_ratio"])
        low_s, high_s = proposed_limits(result["slope"])
        agree_m = decisions_agree(pairs, metric, result["median_ratio"])
        agree_s = decisions_agree(pairs, metric, result["slope"])
        print(
            f"\n{metric} window pk-pk: n={result['n']}  median ratio {result['median_ratio']:.2f}  "
            f"mean {result['mean_ratio']:.2f} +/- {result['sd_ratio']:.2f} (cv {result['cv_percent']:.1f} %)  "
            f"slope through origin {result['slope']:.2f}  r2 {result['r2']:.3f}"
        )
        print(f"  factor = median ratio {result['median_ratio']:.2f}: NOISE_PP_LIMIT_LOW_MV = {low_m:.5f}, NOISE_PP_LIMIT_HIGH_MV = {high_m:.5f}  "
              f"(decisions agree {agree_m[0]}/{agree_m[1]})")
        print(f"  factor = slope        {result['slope']:.2f}: NOISE_PP_LIMIT_LOW_MV = {low_s:.5f}, NOISE_PP_LIMIT_HIGH_MV = {high_s:.5f}  "
              f"(decisions agree {agree_s[0]}/{agree_s[1]})")
    print(
        "\nCALIBRATION_RECORD 4b sentence: 'Chain factor <F> derived <date> from <n> parts measured on fixture 9000233 per TP120 "
        "and on the array rig (median ratio / slope agree within <x> %, cv <y> %); NOISE_PP_LIMIT_LOW/HIGH_MV = 10.0/37.9 / <F> "
        "on the <metric> window metric; decisions identical on <k>/<n> parts.' Then set the constants, bump CALIBRATION_ID."
    )


# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legacy", required=True, type=Path, help="legacy data sheet CSV (sensor_id, legacy_noise_mv[, position, tray])")
    parser.add_argument("--array", required=True, type=Path, help="lot CSV, one tray .npz, or a directory of tray_*_raw.npz")
    parser.add_argument("--replay-band", nargs=2, type=float, action="append", metavar=("LO_HZ", "HI_HZ"),
                        help="also re-judge the .npz captures through this band (repeatable)")
    parser.add_argument("--out", type=Path, default=None, help=f"output directory (default {default_out_dir()})")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args(argv)

    legacy = read_legacy(args.legacy)
    out_dir = args.out or default_out_dir()
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    variants: list[tuple[str, tuple[float, float] | None]] = [("production band (~0.85-22 Hz)", None)]
    for low, high in args.replay_band or []:
        variants.append((f"replay band {low:g}-{high:g} Hz", (low, high)))
    exit_code = 0
    for index, (label, band) in enumerate(variants):
        array = collect_array(args.array, band=band)
        pairs = pair(legacy, array)
        if not pairs:
            print(f"{label}: no pairs - check sensor_id / tray+position columns")
            exit_code = 1
            continue
        fits = {metric: fit(pairs, metric) for metric in METRICS}
        report(pairs, fits, label=label)
        suffix = "" if index == 0 else f"_band{band[0]:g}-{band[1]:g}"
        csv_path = out_dir / f"parity_{stamp}{suffix}.csv"
        write_pairs_csv(csv_path, pairs, fits)
        print(f"pairs written to {csv_path}")
        if not args.no_plot:
            png = plot(out_dir / f"parity_{stamp}{suffix}.png", pairs, fits, f"40623 noise parity - {label}")
            if png:
                print(f"plot written to {png}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
