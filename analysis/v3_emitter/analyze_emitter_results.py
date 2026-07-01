"""
Analyze 406MCA emitter-rig (v3) production data.

Default run:
    python analyze_emitter_results.py

The script reads every 406mca_emitter_lot_*.csv file in the v3 results folder
(Documents\\Eltec_406MCA_Test_Results\\v3_emitter), summarizes pass/fail yield,
offset / sensitivity / SNR distributions, polarity GOOD/BAD counts, and the most
common failure reasons, then writes CSV exports plus a self-contained HTML report.

It intentionally uses only the Python standard library so it can run on the
tester PC without installing plotting or dataframe packages.

Columns are read positionally against the emitter tester's canonical field order,
so older CSV files whose header predates the battery/noise/SNR columns are still
parsed correctly.
"""

from __future__ import annotations

import argparse
import csv
import html
import math
import statistics
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths + spec constants (mirror the v3 emitter tester so this stays stdlib-only)
# --------------------------------------------------------------------------- #
DEFAULT_RESULTS_DIR = (
    Path.home() / "Documents" / "Eltec_406MCA_Test_Results" / "v3_emitter"
)
EMITTER_CSV_PATTERN = "406mca_emitter_lot_*.csv"

REPORT_FILENAME = "406MCA_Emitter_Analysis_Report.html"
NORMALIZED_ROWS_FILENAME = "406MCA_Emitter_Normalized_Rows.csv"
LOT_SUMMARY_FILENAME = "406MCA_Emitter_Lot_Summary.csv"
FAILURE_REASONS_FILENAME = "406MCA_Emitter_Failure_Reason_Summary.csv"
OUTLIERS_FILENAME = "406MCA_Emitter_Outliers.csv"

# Canonical column order written by eltec_406mca_emitter_tester.py (CSV_FIELDS).
CANONICAL_FIELDS = [
    "timestamp",
    "batch_number",
    "sensor_number",
    "sensor_id",
    "tester_name",
    "model",
    "filter_setup",
    "pwm_channel",
    "pwm_hz",
    "pwm_duty",
    "offset_v",
    "sensitivity_mv",
    "polarity",
    "polarity_good_bad",
    "pass_fail",
    "fail_reasons",
    "operator_comments",
    "waveform_snapshot_paths",
    "battery_v",
    "noise_rms_mv",
    "snr_db",
]

# Minimum peak-to-peak sensitivity (mV) per filter/setup, mirroring the tester's
# FILTER_SPECS_MV. Used to flag units that passed the file but sit under spec.
FILTER_SPECS_MV = {
    "-3 filter": 25.0,
    "-27 filter": 25.0,
    "-266 filter": 30.9,
    "-273 filter + blackened tube": 2.3,
    "-284 filter + extra -6 + blackened tube": 4.0,
}
OFFSET_MIN_V = 0.3
OFFSET_MAX_V = 1.2
MIN_SNR_DB = 3.5  # ~1.5 linear SNR gate from the tester


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Row:
    source_file: str
    source_line: int
    values: dict[str, str]

    def get(self, key: str) -> str:
        return (self.values.get(key) or "").strip()

    def get_float(self, key: str) -> float | None:
        raw = self.get(key)
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None


@dataclass
class LotSummary:
    lot: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    polarity_good: int = 0
    polarity_bad: int = 0
    offsets: list[float] = field(default_factory=list)
    sensitivities: list[float] = field(default_factory=list)
    snrs: list[float] = field(default_factory=list)
    testers: set[str] = field(default_factory=set)
    filters: set[str] = field(default_factory=set)
    first_ts: str = ""
    last_ts: str = ""

    @property
    def yield_pct(self) -> float:
        return (100.0 * self.passed / self.total) if self.total else 0.0


# --------------------------------------------------------------------------- #
# Reading / normalizing
# --------------------------------------------------------------------------- #
def extract_lot_from_filename(path: Path) -> str:
    stem = path.stem  # 406mca_emitter_lot_<lot>
    marker = "emitter_lot_"
    idx = stem.find(marker)
    return stem[idx + len(marker):] if idx >= 0 else stem


def read_rows(results_dir: Path) -> tuple[list[Row], list[Path], list[str]]:
    rows: list[Row] = []
    warnings: list[str] = []
    paths = sorted(results_dir.glob(EMITTER_CSV_PATTERN))
    for path in paths:
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.reader(handle)
                try:
                    header = next(reader)
                except StopIteration:
                    warnings.append(f"{path.name}: empty file, skipped.")
                    continue
                # Map every data row positionally onto the canonical field order.
                # This tolerates old files whose header is missing the trailing
                # battery / noise / SNR columns that were added later.
                for line_no, raw in enumerate(reader, start=2):
                    if not any(cell.strip() for cell in raw):
                        continue
                    values = {
                        name: (raw[i] if i < len(raw) else "")
                        for i, name in enumerate(CANONICAL_FIELDS)
                    }
                    rows.append(Row(path.name, line_no, values))
        except OSError as exc:
            warnings.append(f"{path.name}: could not read ({exc}).")
    return rows, paths, warnings


# Individual fail reasons can themselves contain commas and semicolons
# (e.g. "Polarity is UNKNOWN; expected POSITIVE. (...)"), so splitting the field
# is unreliable. Instead, scan the whole fail_reasons string for the known
# failure categories and count each at most once per sensor.
REASON_CATEGORIES = [
    ("sensitivity too low", "Sensitivity too low"),
    ("polarity", "Polarity wrong / unknown"),
    ("signal-to-noise", "SNR too low"),
    ("snr", "SNR too low"),
    ("offset", "Offset out of range"),
    ("battery", "Battery too low"),
]


def categories_in_reasons(raw: str) -> list[str]:
    if not raw.strip():
        return []
    low = raw.lower()
    found: list[str] = []
    for keyword, category in REASON_CATEGORIES:
        if keyword in low and category not in found:
            found.append(category)
    return found or ["Other"]


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def build_lot_summaries(rows: list[Row]) -> dict[str, LotSummary]:
    lots: dict[str, LotSummary] = {}
    for row in rows:
        lot = row.get("batch_number") or extract_lot_from_filename(Path(row.source_file))
        summary = lots.setdefault(lot, LotSummary(lot=lot))
        summary.total += 1

        pass_fail = row.get("pass_fail").upper()
        if pass_fail == "PASS":
            summary.passed += 1
        elif pass_fail == "FAIL":
            summary.failed += 1

        pol = row.get("polarity_good_bad").upper()
        if pol == "GOOD":
            summary.polarity_good += 1
        elif pol == "BAD":
            summary.polarity_bad += 1

        offset = row.get_float("offset_v")
        if offset is not None:
            summary.offsets.append(offset)
        sens = row.get_float("sensitivity_mv")
        if sens is not None:
            summary.sensitivities.append(sens)
        snr = row.get_float("snr_db")
        if snr is not None and math.isfinite(snr):
            summary.snrs.append(snr)

        if row.get("tester_name"):
            summary.testers.add(row.get("tester_name"))
        if row.get("filter_setup"):
            summary.filters.add(row.get("filter_setup"))

        ts = row.get("timestamp")
        if ts:
            if not summary.first_ts or ts < summary.first_ts:
                summary.first_ts = ts
            if not summary.last_ts or ts > summary.last_ts:
                summary.last_ts = ts
    return lots


def find_outliers(rows: list[Row]) -> list[dict[str, str]]:
    outliers: list[dict[str, str]] = []
    for row in rows:
        flags: list[str] = []
        offset = row.get_float("offset_v")
        if offset is not None and not (OFFSET_MIN_V <= offset <= OFFSET_MAX_V):
            flags.append(f"offset {offset:.3f} V outside {OFFSET_MIN_V}-{OFFSET_MAX_V} V")

        sens = row.get_float("sensitivity_mv")
        min_mv = FILTER_SPECS_MV.get(row.get("filter_setup"))
        if sens is not None and min_mv is not None and sens < min_mv:
            flags.append(f"sensitivity {sens:.2f} mV under spec {min_mv:.1f} mV")

        snr = row.get_float("snr_db")
        if snr is not None and math.isfinite(snr) and snr < MIN_SNR_DB:
            flags.append(f"SNR {snr:.1f} dB under {MIN_SNR_DB:.1f} dB")

        # PASS units that nonetheless tripped a spec flag are the interesting ones.
        if flags:
            outliers.append({
                "source_file": row.source_file,
                "line": str(row.source_line),
                "batch_number": row.get("batch_number"),
                "sensor_id": row.get("sensor_id"),
                "pass_fail": row.get("pass_fail"),
                "offset_v": row.get("offset_v"),
                "sensitivity_mv": row.get("sensitivity_mv"),
                "snr_db": row.get("snr_db"),
                "flags": "; ".join(flags),
            })
    return outliers


def stat_line(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0, "mean": 0.0, "stdev": 0.0, "min": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }


# --------------------------------------------------------------------------- #
# CSV writers
# --------------------------------------------------------------------------- #
def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def write_lot_summary_csv(path: Path, lots: list[LotSummary]) -> None:
    fieldnames = [
        "lot", "total", "passed", "failed", "yield_pct",
        "polarity_good", "polarity_bad",
        "offset_mean_v", "sensitivity_mean_mv", "snr_mean_db",
        "testers", "filters", "first_ts", "last_ts",
    ]
    rows: list[dict[str, str]] = []
    for lot in lots:
        o = stat_line(lot.offsets)
        s = stat_line(lot.sensitivities)
        n = stat_line(lot.snrs)
        rows.append({
            "lot": lot.lot,
            "total": str(lot.total),
            "passed": str(lot.passed),
            "failed": str(lot.failed),
            "yield_pct": f"{lot.yield_pct:.1f}",
            "polarity_good": str(lot.polarity_good),
            "polarity_bad": str(lot.polarity_bad),
            "offset_mean_v": f"{o['mean']:.4f}" if o["n"] else "",
            "sensitivity_mean_mv": f"{s['mean']:.3f}" if s["n"] else "",
            "snr_mean_db": f"{n['mean']:.2f}" if n["n"] else "",
            "testers": ", ".join(sorted(lot.testers)),
            "filters": ", ".join(sorted(lot.filters)),
            "first_ts": lot.first_ts,
            "last_ts": lot.last_ts,
        })
    write_csv(path, rows, fieldnames)


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #
def yield_bar_svg(lots: list[LotSummary]) -> str:
    if not lots:
        return '<div class="chart-empty">No lots to chart.</div>'
    bar_h, gap, left, top = 22, 10, 140, 20
    width = 760
    height = top + len(lots) * (bar_h + gap) + 20
    track = width - left - 60
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img">']
    parts.append(f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height - 20}" />')
    for i, lot in enumerate(lots):
        y = top + i * (bar_h + gap)
        w = track * (lot.yield_pct / 100.0)
        color = "#2e7d32" if lot.yield_pct >= 90 else ("#f59e0b" if lot.yield_pct >= 70 else "#dc2626")
        label = html.escape(f"Lot {lot.lot}")
        parts.append(f'<text class="tick-label" x="8" y="{y + bar_h - 6}">{label}</text>')
        parts.append(f'<rect x="{left}" y="{y}" width="{w:.1f}" height="{bar_h}" fill="{color}" rx="3" />')
        parts.append(
            f'<text class="bar-value" x="{left + w + 6:.1f}" y="{y + bar_h - 6}">'
            f'{lot.yield_pct:.0f}% ({lot.passed}/{lot.total})</text>'
        )
    parts.append("</svg>")
    return f'<div class="chart">{"".join(parts)}</div>'


def render_html(
    results_dir: Path,
    rows: list[Row],
    lots: list[LotSummary],
    reason_counts: Counter,
    outliers: list[dict[str, str]],
    warnings: list[str],
) -> str:
    total = len(rows)
    passed = sum(l.passed for l in lots)
    failed = sum(l.failed for l in lots)
    overall_yield = (100.0 * passed / total) if total else 0.0
    offsets = [v for l in lots for v in l.offsets]
    sens = [v for l in lots for v in l.sensitivities]
    snrs = [v for l in lots for v in l.snrs]
    o, s, n = stat_line(offsets), stat_line(sens), stat_line(snrs)

    def card(label: str, value: str, helper: str = "") -> str:
        helper_html = f'<div class="card-helper">{html.escape(helper)}</div>' if helper else ""
        return (
            f'<div class="card"><div class="card-label">{html.escape(label)}</div>'
            f'<div class="card-value">{html.escape(value)}</div>{helper_html}</div>'
        )

    cards = "".join([
        card("Sensors tested", str(total), f"{len(lots)} lot(s)"),
        card("Overall yield", f"{overall_yield:.1f}%", f"{passed} pass / {failed} fail"),
        card("Mean offset", f"{o['mean']:.3f} V" if o["n"] else "-", f"spec {OFFSET_MIN_V}-{OFFSET_MAX_V} V"),
        card("Mean sensitivity", f"{s['mean']:.2f} mV" if s["n"] else "-",
             f"min {s['min']:.2f} / max {s['max']:.2f} mV" if s["n"] else ""),
        card("Mean SNR", f"{n['mean']:.1f} dB" if n["n"] else "n/a", f"{n['n']} with SNR"),
    ])

    lot_rows = "".join(
        f"<tr><td>{html.escape(l.lot)}</td><td>{l.total}</td><td>{l.passed}</td>"
        f"<td>{l.failed}</td><td>{l.yield_pct:.1f}%</td>"
        f"<td>{l.polarity_good}/{l.polarity_bad}</td>"
        f"<td>{stat_line(l.offsets)['mean']:.3f}</td>"
        f"<td>{stat_line(l.sensitivities)['mean']:.2f}</td>"
        f"<td>{html.escape(', '.join(sorted(l.testers)))}</td></tr>"
        for l in lots
    )

    reason_rows = "".join(
        f"<tr><td>{html.escape(reason)}</td><td>{count}</td></tr>"
        for reason, count in reason_counts.most_common()
    ) or '<tr><td colspan="2">No failures recorded.</td></tr>'

    outlier_rows = "".join(
        f"<tr><td>{html.escape(o_['batch_number'])}</td><td>{html.escape(o_['sensor_id'])}</td>"
        f"<td>{html.escape(o_['pass_fail'])}</td><td>{html.escape(o_['flags'])}</td></tr>"
        for o_ in outliers
    ) or '<tr><td colspan="4">No spec outliers flagged.</td></tr>'

    warn_html = ""
    if warnings:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
        warn_html = f'<section><h2>Warnings</h2><ul>{items}</ul></section>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>406MCA Emitter Analysis</title>
<style>
body {{ font-family: "Segoe UI", Arial, sans-serif; margin: 0; background: #f4f6f8; color: #1f2a3a; }}
header {{ background: #1d4aa8; color: #fff; padding: 20px 28px; }}
header h1 {{ margin: 0 0 6px; font-size: 22px; }}
header p {{ margin: 2px 0; opacity: 0.9; font-size: 13px; }}
main {{ padding: 22px 28px 40px; max-width: 1100px; }}
section {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px 20px; margin-bottom: 20px; }}
h2 {{ margin-top: 0; font-size: 17px; color: #26364d; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; }}
.card {{ padding: 15px; border: 1px solid #d9e1ec; border-radius: 8px; background: #fbfcff; }}
.card-label {{ font-size: 12px; text-transform: uppercase; color: #5b677a; }}
.card-value {{ margin-top: 8px; font-size: 26px; font-weight: 700; }}
.card-helper {{ margin-top: 6px; color: #607087; font-size: 13px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ border-bottom: 1px solid #e2e8f0; padding: 8px 9px; text-align: left; vertical-align: top; }}
th {{ background: #eef3f8; color: #26364d; }}
.chart {{ margin: 10px 0 4px; overflow-x: auto; }}
.chart-empty {{ padding: 14px; color: #6b7280; border: 1px dashed #cbd5e1; border-radius: 8px; }}
svg {{ width: 100%; min-width: 720px; height: auto; }}
.tick-label, .bar-value {{ fill: #334155; font-size: 12px; }}
.axis {{ stroke: #64748b; stroke-width: 1; }}
</style>
</head>
<body>
<header>
<h1>406MCA Emitter Rig (v3) Data Analysis</h1>
<p>Generated {html.escape(datetime.now().isoformat(timespec="seconds"))}</p>
<p>Results directory: {html.escape(str(results_dir))}</p>
</header>
<main>
<section><h2>Summary</h2><div class="cards">{cards}</div></section>
<section><h2>Yield by lot</h2>{yield_bar_svg(lots)}</section>
<section><h2>Per-lot detail</h2>
<table><thead><tr><th>Lot</th><th>Total</th><th>Pass</th><th>Fail</th><th>Yield</th>
<th>Polarity good/bad</th><th>Mean offset (V)</th><th>Mean sens (mV)</th><th>Testers</th></tr></thead>
<tbody>{lot_rows}</tbody></table></section>
<section><h2>Failure reasons</h2>
<table><thead><tr><th>Reason</th><th>Count</th></tr></thead><tbody>{reason_rows}</tbody></table></section>
<section><h2>Spec outliers</h2>
<table><thead><tr><th>Lot</th><th>Sensor</th><th>Result</th><th>Flags</th></tr></thead>
<tbody>{outlier_rows}</tbody></table></section>
{warn_html}
</main>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze 406MCA emitter-rig lot CSV data.")
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help=f"Folder containing {EMITTER_CSV_PATTERN} files. Default: {DEFAULT_RESULTS_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for generated HTML and CSV exports. Default: RESULTS_DIR\\analysis.",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open the HTML report in a browser.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir else results_dir / "analysis"
    )

    if not results_dir.exists():
        raise SystemExit(f"Results directory not found: {results_dir}")

    rows, source_paths, warnings = read_rows(results_dir)
    if not rows:
        raise SystemExit(
            f"No {EMITTER_CSV_PATTERN} rows found in {results_dir}. "
            "Run the emitter tester first."
        )

    lot_map = build_lot_summaries(rows)
    lots = sorted(lot_map.values(), key=lambda l: l.last_ts or l.lot)

    reason_counts: Counter = Counter()
    for row in rows:
        if row.get("pass_fail").upper() == "FAIL":
            for category in categories_in_reasons(row.get("fail_reasons")):
                reason_counts[category] += 1

    outliers = find_outliers(rows)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Normalized rows export (canonical column order).
    write_csv(
        output_dir / NORMALIZED_ROWS_FILENAME,
        [{**r.values, "source_file": r.source_file, "line": str(r.source_line)} for r in rows],
        CANONICAL_FIELDS + ["source_file", "line"],
    )
    write_lot_summary_csv(output_dir / LOT_SUMMARY_FILENAME, lots)
    write_csv(
        output_dir / FAILURE_REASONS_FILENAME,
        [{"reason": r, "count": str(c)} for r, c in reason_counts.most_common()],
        ["reason", "count"],
    )
    write_csv(
        output_dir / OUTLIERS_FILENAME,
        outliers,
        ["source_file", "line", "batch_number", "sensor_id", "pass_fail",
         "offset_v", "sensitivity_mv", "snr_db", "flags"],
    )

    report_path = output_dir / REPORT_FILENAME
    report_path.write_text(
        render_html(results_dir, rows, lots, reason_counts, outliers, warnings),
        encoding="utf-8",
    )

    total = len(rows)
    passed = sum(l.passed for l in lots)
    print("406MCA emitter analysis complete")
    print(f"  Source files: {len(source_paths)} | sensors: {total} | lots: {len(lots)}")
    print(f"  Overall yield: {100.0 * passed / total:.1f}% ({passed}/{total})")
    if reason_counts:
        top = ", ".join(f"{r} x{c}" for r, c in reason_counts.most_common(3))
        print(f"  Top failure reasons: {top}")
    if outliers:
        print(f"  Spec outliers flagged: {len(outliers)}")
    for warning in warnings:
        print(f"  WARNING: {warning}")
    print(f"  Wrote report: {report_path}")
    print(f"  Wrote CSV exports to: {output_dir}")

    if not args.no_open:
        try:
            webbrowser.open(report_path.as_uri())
        except OSError:
            pass


if __name__ == "__main__":
    main()
