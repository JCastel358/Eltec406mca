"""
Focused disagreement analysis for 406MCA scope-verification data.

Default run:
    python Engineer_testing\\disagreementAnalysis.py

This is a simpler companion to dataAnalysis.py. It reads the same lot CSVs,
uses operator/manual disposition as the official result, and focuses on the
units where the program and operator disagreed.
"""

from __future__ import annotations

import argparse
import csv
import html
import math
import statistics
import webbrowser
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Iterable

from dataAnalysis import (
    DEFAULT_RESULTS_DIR,
    FAIL,
    PASS,
    NormalizedRow,
    choose_latest_rows,
    fmt_float,
    parse_date_arg,
    percent,
    read_rows,
    split_program_reasons,
)


REPORT_FILENAME = "406MCA_Disagreement_Report.html"
DETAILS_FILENAME = "406MCA_Disagreement_Details.csv"
STATS_FILENAME = "406MCA_Disagreement_Stats.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze program/operator disagreements in 406MCA scope data.")
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help=f"Folder containing 406mca_scope_verification_lot_*.csv files. Default: {DEFAULT_RESULTS_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for disagreement report and CSV exports. Default: RESULTS_DIR\\analysis\\disagreements.",
    )
    parser.add_argument("--lot", action="append", default=[], help="Lot number to include. Repeat for multiple lots.")
    parser.add_argument("--start-date", default="", help="Include rows on or after YYYY-MM-DD.")
    parser.add_argument("--end-date", default="", help="Include rows on or before YYYY-MM-DD.")
    parser.add_argument("--open", action="store_true", help="Open the generated HTML report after writing it.")
    return parser.parse_args()


def disagreement_rows(rows: list[NormalizedRow]) -> list[NormalizedRow]:
    return [
        row
        for row in rows
        if row.agreement in ("operator_pass_program_fail", "operator_fail_program_pass")
    ]


def disagreement_type(row: NormalizedRow) -> str:
    if row.agreement == "operator_fail_program_pass":
        return "Program false pass"
    if row.agreement == "operator_pass_program_fail":
        return "Program false fail"
    return "No disagreement"


def risk_label(row: NormalizedRow) -> str:
    if row.agreement == "operator_fail_program_pass":
        return "Higher risk: tester passed a unit the operator failed"
    if row.agreement == "operator_pass_program_fail":
        return "Lower risk: tester failed a unit the operator passed"
    return ""


def operator_reason(row: NormalizedRow) -> str:
    if row.scope_tag or row.scope_reason:
        if row.scope_tag and row.scope_reason:
            return f"{row.scope_tag} - {row.scope_reason}"
        return row.scope_tag or row.scope_reason
    if row.operator_result == PASS:
        return "Operator/manual result PASS"
    if row.operator_result == FAIL:
        return "Operator/manual result FAIL, no reason recorded"
    return "No operator/manual result"


def program_reason(row: NormalizedRow) -> str:
    reasons = split_program_reasons(row.program_fail_reasons)
    if reasons:
        return "; ".join(reasons)
    if row.program_result == PASS:
        return "Program result PASS"
    if row.program_result == FAIL:
        return "Program result FAIL, no reason recorded"
    return "No program result"


def compact_reading_delta(label: str, program_value: float | None, manual_value: float | None, delta: float | None, units: str) -> str:
    if program_value is None and manual_value is None:
        return f"{label}: not measured"
    if program_value is None:
        return f"{label}: program blank, manual {manual_value:.3f} {units}"
    if manual_value is None:
        return f"{label}: program {program_value:.3f} {units}, manual blank"
    return f"{label}: program {program_value:.3f}, manual {manual_value:.3f}, delta {delta or 0.0:+.3f} {units}"


def what_happened(row: NormalizedRow) -> str:
    offset_text = compact_reading_delta("offset", row.program_offset_v, row.manual_offset_v, row.offset_delta_v, "V")
    sensitivity_text = compact_reading_delta(
        "sensitivity",
        row.program_sensitivity_mv,
        row.manual_sensitivity_mv,
        row.sensitivity_delta_mv,
        "mV",
    )
    operator_marks = []
    if row.operator_polarity:
        operator_marks.append(f"operator polarity={row.operator_polarity}")
    if row.operator_offset_match:
        operator_marks.append(f"offset reading={row.operator_offset_match}")
    if row.operator_offset_sensor_status:
        operator_marks.append(f"offset sensor={row.operator_offset_sensor_status}")
    if row.operator_sensitivity_match:
        operator_marks.append(f"sensitivity reading={row.operator_sensitivity_match}")
    if row.operator_sensitivity_sensor_status:
        operator_marks.append(f"sensitivity sensor={row.operator_sensitivity_sensor_status}")
    marks_text = "; ".join(operator_marks) if operator_marks else "no newer operator reading flags recorded"

    if row.agreement == "operator_fail_program_pass":
        return (
            f"Program passed the unit, but the operator failed it for {operator_reason(row)}. "
            f"{offset_text}; {sensitivity_text}. Operator checks: {marks_text}."
        )
    if row.agreement == "operator_pass_program_fail":
        return (
            f"Operator passed the unit, but the program failed it for {program_reason(row)}. "
            f"{offset_text}; {sensitivity_text}. Operator checks: {marks_text}."
        )
    return "Program and operator agreed."


def mean_or_blank(values: list[float]) -> str:
    return fmt_float(statistics.mean(values), 4) if values else ""


def median_or_blank(values: list[float]) -> str:
    return fmt_float(statistics.median(values), 4) if values else ""


def stdev_or_blank(values: list[float]) -> str:
    return fmt_float(statistics.stdev(values), 4) if len(values) >= 2 else ""


def delta_values(rows: Iterable[NormalizedRow], attr: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = getattr(row, attr)
        if value is not None and math.isfinite(value):
            values.append(value)
    return values


def delta_stats(rows: list[NormalizedRow]) -> list[dict[str, str]]:
    stats: list[dict[str, str]] = []
    for label, attr, units in (
        ("Offset delta", "offset_delta_v", "V"),
        ("Sensitivity delta", "sensitivity_delta_mv", "mV"),
    ):
        values = delta_values(rows, attr)
        abs_values = [abs(value) for value in values]
        stats.append(
            {
                "metric": label,
                "units": units,
                "paired_disagreements": str(len(values)),
                "mean_delta": mean_or_blank(values),
                "median_delta": median_or_blank(values),
                "stdev_delta": stdev_or_blank(values),
                "mean_absolute_delta": mean_or_blank(abs_values),
                "max_absolute_delta": fmt_float(max(abs_values), 4) if abs_values else "",
            }
        )
    return stats


def detail_csv_row(row: NormalizedRow) -> dict[str, str]:
    return {
        "timestamp": row.timestamp,
        "lot_number": row.lot_number,
        "sensor_number": row.sensor_number,
        "sensor_id": row.sensor_id,
        "disagreement_type": disagreement_type(row),
        "risk_label": risk_label(row),
        "operator_result": row.operator_result,
        "program_result": row.program_result,
        "operator_reason": operator_reason(row),
        "program_reason": program_reason(row),
        "program_offset_v": fmt_float(row.program_offset_v, 6),
        "manual_offset_v": fmt_float(row.manual_offset_v, 6),
        "offset_delta_v": fmt_float(row.offset_delta_v, 6),
        "program_sensitivity_mv": fmt_float(row.program_sensitivity_mv, 6),
        "manual_sensitivity_mv": fmt_float(row.manual_sensitivity_mv, 6),
        "sensitivity_delta_mv": fmt_float(row.sensitivity_delta_mv, 6),
        "program_polarity": row.polarity,
        "operator_polarity": row.operator_polarity,
        "operator_offset_match": row.operator_offset_match,
        "operator_offset_sensor_status": row.operator_offset_sensor_status,
        "operator_sensitivity_match": row.operator_sensitivity_match,
        "operator_sensitivity_sensor_status": row.operator_sensitivity_sensor_status,
        "operator_comments": row.operator_comments,
        "source_file": row.source_file,
        "source_line": str(row.source_line),
        "what_happened": what_happened(row),
    }


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def counter_table_rows(counter: Counter[str], total: int) -> list[tuple[str, str, str]]:
    return [(label, str(count), f"{percent(count, total):.2f}") for label, count in counter.most_common()]


def by_lot_rows(disagreements: list[NormalizedRow], all_rows: list[NormalizedRow]) -> list[dict[str, str]]:
    all_by_lot: Counter[str] = Counter(row.lot_number or "(blank)" for row in all_rows)
    disagreement_by_lot: dict[str, list[NormalizedRow]] = defaultdict(list)
    for row in disagreements:
        disagreement_by_lot[row.lot_number or "(blank)"].append(row)

    rows: list[dict[str, str]] = []
    for lot_number, group in sorted(disagreement_by_lot.items()):
        false_pass = sum(1 for row in group if row.agreement == "operator_fail_program_pass")
        false_fail = sum(1 for row in group if row.agreement == "operator_pass_program_fail")
        lot_total = all_by_lot[lot_number]
        rows.append(
            {
                "lot_number": lot_number,
                "lot_units": str(lot_total),
                "disagreements": str(len(group)),
                "disagreement_rate_pct": f"{percent(len(group), lot_total):.2f}",
                "program_false_pass": str(false_pass),
                "program_false_fail": str(false_fail),
            }
        )
    return rows


def build_stats_rows(all_rows: list[NormalizedRow], disagreements: list[NormalizedRow]) -> list[dict[str, str]]:
    total = len(all_rows)
    disagreement_total = len(disagreements)
    type_counter = Counter(disagreement_type(row) for row in disagreements)
    lot_counter = Counter(row.lot_number or "(blank)" for row in disagreements)
    reason_counter = Counter(operator_reason(row) for row in disagreements)
    program_reason_counter = Counter(program_reason(row) for row in disagreements if row.agreement == "operator_pass_program_fail")
    stats: list[dict[str, str]] = [
        {"category": "overall", "item": "units_analyzed", "count": str(total), "percent_of_all_units": "100.00"},
        {
            "category": "overall",
            "item": "disagreements",
            "count": str(disagreement_total),
            "percent_of_all_units": f"{percent(disagreement_total, total):.2f}",
        },
    ]
    for label, count in type_counter.most_common():
        stats.append(
            {
                "category": "disagreement_type",
                "item": label,
                "count": str(count),
                "percent_of_all_units": f"{percent(count, total):.2f}",
            }
        )
    for lot, count in lot_counter.most_common():
        stats.append(
            {
                "category": "lot",
                "item": lot,
                "count": str(count),
                "percent_of_all_units": f"{percent(count, total):.2f}",
            }
        )
    for reason, count in reason_counter.most_common():
        stats.append(
            {
                "category": "operator_reason",
                "item": reason,
                "count": str(count),
                "percent_of_all_units": f"{percent(count, total):.2f}",
            }
        )
    for reason, count in program_reason_counter.most_common():
        stats.append(
            {
                "category": "program_reason_on_false_fail",
                "item": reason,
                "count": str(count),
                "percent_of_all_units": f"{percent(count, total):.2f}",
            }
        )
    return stats


def html_table(headers: list[str], rows: Iterable[Iterable[str]]) -> str:
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    row_html = []
    for row in rows:
        row_html.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>")
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{''.join(row_html)}</tbody></table>"


def card(label: str, value: str, helper: str = "") -> str:
    return (
        '<div class="card">'
        f'<div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>'
        f'<div class="helper">{html.escape(helper)}</div>'
        "</div>"
    )


def simple_bar_table(title: str, rows: list[tuple[str, str, str]]) -> str:
    if not rows:
        return f"<h3>{html.escape(title)}</h3><p>No rows.</p>"
    max_count = max(int(row[1]) for row in rows)
    body = []
    for label, count_text, pct_text in rows:
        width = 0 if max_count == 0 else int(int(count_text) / max_count * 100)
        body.append(
            "<tr>"
            f"<td>{html.escape(label)}</td>"
            f"<td>{html.escape(count_text)}</td>"
            f"<td>{html.escape(pct_text)}%</td>"
            f'<td><div class="bar"><span style="width:{width}%"></span></div></td>'
            "</tr>"
        )
    return (
        f"<h3>{html.escape(title)}</h3>"
        '<table><thead><tr><th>Item</th><th>Count</th><th>% of All Units</th><th></th></tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def write_report(
    report_path: Path,
    results_dir: Path,
    all_rows: list[NormalizedRow],
    disagreements: list[NormalizedRow],
    detail_rows: list[dict[str, str]],
    stats_rows: list[dict[str, str]],
    lot_rows: list[dict[str, str]],
    delta_summary: list[dict[str, str]],
    warnings: list[str],
    details_path: Path,
    stats_path: Path,
) -> None:
    total = len(all_rows)
    disagreement_total = len(disagreements)
    type_counter = Counter(disagreement_type(row) for row in disagreements)
    operator_reason_counter = Counter(operator_reason(row) for row in disagreements)
    program_false_pass = type_counter["Program false pass"]
    program_false_fail = type_counter["Program false fail"]

    cards = [
        card("Units Analyzed", str(total), "deduplicated saved CSV rows"),
        card("Disagreements", str(disagreement_total), f"{percent(disagreement_total, total):.2f}% of all units"),
        card("Program False Pass", str(program_false_pass), "operator failed, program passed"),
        card("Program False Fail", str(program_false_fail), "operator passed, program failed"),
    ]
    warning_html = "<p>No data-quality warnings.</p>"
    if warnings:
        warning_html = "<ul>" + "".join(f"<li>{html.escape(warning)}</li>" for warning in warnings) + "</ul>"

    detail_table_rows = [
        (
            row["timestamp"],
            row["sensor_id"],
            row["disagreement_type"],
            row["operator_reason"],
            row["program_reason"],
            row["offset_delta_v"],
            row["sensitivity_delta_mv"],
            row["what_happened"],
        )
        for row in detail_rows
    ]

    report = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>406MCA Disagreement Analysis</title>
<style>
body {{
    margin: 0;
    font-family: Arial, Helvetica, sans-serif;
    color: #152033;
    background: #f6f8fb;
}}
header {{
    padding: 26px 34px 22px;
    background: #16243a;
    color: white;
}}
header h1 {{
    margin: 0 0 8px;
    font-size: 30px;
    letter-spacing: 0;
}}
header p {{
    margin: 4px 0;
    color: #d7e2f0;
}}
main {{
    max-width: 1180px;
    margin: 0 auto;
    padding: 24px;
}}
section {{
    margin: 0 0 22px;
    padding: 22px;
    background: white;
    border: 1px solid #d9e1ec;
    border-radius: 8px;
}}
h2 {{
    margin: 0 0 15px;
    font-size: 22px;
    letter-spacing: 0;
}}
h3 {{
    margin: 18px 0 10px;
    font-size: 17px;
    letter-spacing: 0;
}}
.cards {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 14px;
}}
.card {{
    padding: 15px;
    border: 1px solid #d9e1ec;
    border-radius: 8px;
    background: #fbfcff;
}}
.label {{
    color: #5c697c;
    font-size: 12px;
    text-transform: uppercase;
}}
.value {{
    margin-top: 8px;
    font-size: 28px;
    font-weight: 700;
}}
.helper {{
    margin-top: 6px;
    color: #64748b;
    font-size: 13px;
}}
table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
}}
th, td {{
    border-bottom: 1px solid #e2e8f0;
    padding: 8px 9px;
    text-align: left;
    vertical-align: top;
}}
th {{
    background: #eef3f8;
}}
.bar {{
    height: 12px;
    background: #e2e8f0;
    border-radius: 3px;
    overflow: hidden;
    min-width: 120px;
}}
.bar span {{
    display: block;
    height: 100%;
    background: #2563eb;
}}
.note {{
    color: #475569;
    line-height: 1.45;
}}
</style>
</head>
<body>
<header>
<h1>406MCA Disagreement Analysis</h1>
<p>Results directory: {html.escape(str(results_dir))}</p>
<p>Operator/manual result is treated as truth. Program false pass means the tester passed a unit the operator failed.</p>
</header>
<main>
<section>
<h2>Plain Summary</h2>
<div class="cards">{''.join(cards)}</div>
<p class="note">The higher-risk disagreement is <b>Program false pass</b>, because the automated tester passed a unit that the operator failed. A <b>Program false fail</b> is usually less risky for shipped product, but it can cost time or reject a good unit.</p>
</section>
<section>
<h2>Disagreement Statistics</h2>
{simple_bar_table("By Type", counter_table_rows(Counter(disagreement_type(row) for row in disagreements), total))}
{simple_bar_table("By Operator Reason", counter_table_rows(operator_reason_counter, total))}
</section>
<section>
<h2>Lot Breakdown</h2>
{html_table(["Lot", "Lot Units", "Disagreements", "Disagreement Rate %", "Program False Pass", "Program False Fail"], [(row["lot_number"], row["lot_units"], row["disagreements"], row["disagreement_rate_pct"], row["program_false_pass"], row["program_false_fail"]) for row in lot_rows])}
</section>
<section>
<h2>Measurement Deltas Inside Disagreements</h2>
{html_table(["Metric", "Units", "Paired Disagreements", "Mean Delta", "Median Delta", "Std Dev", "Mean Abs Delta", "Max Abs Delta"], [(row["metric"], row["units"], row["paired_disagreements"], row["mean_delta"], row["median_delta"], row["stdev_delta"], row["mean_absolute_delta"], row["max_absolute_delta"]) for row in delta_summary])}
</section>
<section>
<h2>Every Disagreement</h2>
{html_table(["Timestamp", "Sensor", "Type", "Operator Reason", "Program Reason", "Offset Delta V", "Sensitivity Delta mV", "What Happened"], detail_table_rows)}
</section>
<section>
<h2>Data Quality And Outputs</h2>
<h3>Warnings</h3>
{warning_html}
<h3>Exports</h3>
<ul>
<li>{html.escape(str(details_path))}</li>
<li>{html.escape(str(stats_path))}</li>
</ul>
</section>
</main>
</body>
</html>
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else results_dir / "analysis" / "disagreements"
    lots = {lot.strip() for lot in args.lot if lot.strip()}
    start_date: date | None = parse_date_arg(args.start_date, "--start-date")
    end_date: date | None = parse_date_arg(args.end_date, "--end-date")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise SystemExit("--start-date cannot be after --end-date.")
    if not results_dir.exists():
        raise SystemExit(f"Results directory not found: {results_dir}")

    raw_rows, _source_paths, warnings = read_rows(results_dir, lots, start_date, end_date)
    all_rows, duplicate_warnings = choose_latest_rows(raw_rows)
    warnings.extend(duplicate_warnings)
    if not all_rows:
        raise SystemExit("No rows matched the requested filters.")

    disagreements = disagreement_rows(all_rows)
    detail_rows = [detail_csv_row(row) for row in disagreements]
    stats_rows = build_stats_rows(all_rows, disagreements)
    lot_rows = by_lot_rows(disagreements, all_rows)
    delta_summary = delta_stats(disagreements)

    details_path = output_dir / DETAILS_FILENAME
    stats_path = output_dir / STATS_FILENAME
    report_path = output_dir / REPORT_FILENAME

    detail_fields = list(detail_csv_row(disagreements[0]).keys()) if disagreements else list(detail_csv_row(all_rows[0]).keys())
    write_csv(details_path, detail_rows, detail_fields)
    write_csv(stats_path, stats_rows, ["category", "item", "count", "percent_of_all_units"])
    write_report(
        report_path=report_path,
        results_dir=results_dir,
        all_rows=all_rows,
        disagreements=disagreements,
        detail_rows=detail_rows,
        stats_rows=stats_rows,
        lot_rows=lot_rows,
        delta_summary=delta_summary,
        warnings=warnings,
        details_path=details_path,
        stats_path=stats_path,
    )

    type_counter = Counter(disagreement_type(row) for row in disagreements)
    print("406MCA disagreement analysis complete")
    print(f"Rows analyzed: {len(all_rows)}")
    print(f"Disagreements: {len(disagreements)} ({percent(len(disagreements), len(all_rows)):.2f}% of units)")
    print(f"Program false pass: {type_counter['Program false pass']}")
    print(f"Program false fail: {type_counter['Program false fail']}")
    print(f"Wrote HTML report: {report_path}")
    print(f"Wrote details CSV: {details_path}")
    print(f"Wrote stats CSV: {stats_path}")

    if args.open:
        webbrowser.open(report_path.resolve().as_uri())


if __name__ == "__main__":
    main()
