"""
Analyze 406MCA scope-verification production data.

Default run:
    python Engineer_testing\\dataAnalysis.py

The script reads every 406mca_scope_verification_lot_*.csv file in the
operator results folder, normalizes old and new CSV schemas, and writes an HTML
report plus CSV exports. It intentionally uses only the Python standard library
so it can run on the tester PC without installing plotting packages.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import statistics
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable


# The scope-verification (v2) tester writes into this per-version subfolder.
DEFAULT_RESULTS_DIR = (
    Path.home() / "Documents" / "Eltec_406MCA_Test_Results" / "v2_scope_verification"
)
SCOPE_CSV_PATTERN = "406mca_scope_verification_lot_*.csv"
REPORT_FILENAME = "406MCA_Data_Analysis_Report.html"
NORMALIZED_ROWS_FILENAME = "406MCA_Normalized_Rows.csv"
LOT_SUMMARY_FILENAME = "406MCA_Lot_Summary.csv"
DISAGREEMENTS_FILENAME = "406MCA_Disagreements.csv"
OUTLIERS_FILENAME = "406MCA_Outliers.csv"
FAILURE_REASONS_FILENAME = "406MCA_Failure_Reason_Summary.csv"
AUTOSAVE_SUMMARY_FILENAME = "406MCA_Autosave_Summary.csv"

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

NUMERIC_METRICS = [
    ("program_offset_v", "Program offset", "V"),
    ("manual_offset_v", "Manual offset", "V"),
    ("offset_delta_v", "Offset delta", "V"),
    ("program_sensitivity_mv", "Program sensitivity", "mV"),
    ("manual_sensitivity_mv", "Manual sensitivity", "mV"),
    ("sensitivity_delta_mv", "Sensitivity delta", "mV"),
]


@dataclass(frozen=True)
class NormalizedRow:
    source_file: str
    source_line: int
    timestamp: str
    timestamp_dt: datetime | None
    lot_number: str
    sensor_number: str
    sensor_id: str
    filter_setup: str
    am502_gain: float | None
    labjack_range: str
    program_result: str
    operator_result: str
    scope_tag: str
    scope_reason: str
    polarity: str
    operator_polarity: str
    operator_offset_match: str
    operator_offset_sensor_status: str
    operator_sensitivity_match: str
    operator_sensitivity_sensor_status: str
    program_offset_v: float | None
    manual_offset_v: float | None
    offset_delta_v: float | None
    program_sensitivity_mv: float | None
    manual_sensitivity_mv: float | None
    sensitivity_delta_mv: float | None
    program_fail_reasons: str
    operator_comments: str
    tester_sensor_disagreement_raw: str

    @property
    def key(self) -> tuple[str, str]:
        if self.lot_number or self.sensor_id:
            return (self.lot_number, self.sensor_id or f"line-{self.source_line}")
        return (self.source_file, f"line-{self.source_line}")

    @property
    def agreement(self) -> str:
        if self.operator_result not in (PASS, FAIL) or self.program_result not in (PASS, FAIL):
            return UNKNOWN
        if self.operator_result == PASS and self.program_result == PASS:
            return "both_pass"
        if self.operator_result == FAIL and self.program_result == FAIL:
            return "both_fail"
        if self.operator_result == PASS and self.program_result == FAIL:
            return "operator_pass_program_fail"
        return "operator_fail_program_pass"

    @property
    def tester_sensor_disagreement(self) -> str:
        raw = self.tester_sensor_disagreement_raw.strip().upper()
        if raw in ("YES", "TRUE", "Y", "1"):
            return "YES"
        if raw in ("NO", "FALSE", "N", "0"):
            return "NO"
        if self.agreement in ("operator_pass_program_fail", "operator_fail_program_pass"):
            return "YES"
        if self.agreement in ("both_pass", "both_fail"):
            return "NO"
        return ""


@dataclass(frozen=True)
class Outlier:
    source_file: str
    source_line: int
    timestamp: str
    lot_number: str
    sensor_id: str
    metric: str
    value: float
    lower_bound: float
    upper_bound: float
    direction: str


def parse_float(text: str | None) -> float | None:
    if text is None:
        return None
    cleaned = str(text).strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


def parse_datetime(text: str | None) -> datetime | None:
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def parse_date_arg(text: str | None, label: str) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"{label} must use YYYY-MM-DD format.") from exc


def normalize_result(text: str | None) -> str:
    cleaned = (text or "").strip().upper()
    if cleaned == PASS:
        return PASS
    if cleaned == FAIL:
        return FAIL
    return UNKNOWN


def operator_result_from_row(row: dict[str, str]) -> str:
    for field_name in ("sensor_pass_fail", "scope_pass_fail"):
        result = normalize_result(row.get(field_name))
        if result != UNKNOWN:
            return result

    scope_tag = (row.get("scope_tag") or "").strip()
    if not scope_tag:
        return UNKNOWN
    if scope_tag.upper() == "GOOD":
        return PASS
    return FAIL


def extract_lot_from_filename(path: Path) -> str:
    match = re.match(r"406mca_scope_verification_lot_(.+)\.csv$", path.name, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def compute_delta(manual_value: float | None, program_value: float | None) -> float | None:
    if manual_value is None or program_value is None:
        return None
    return manual_value - program_value


def normalize_csv_row(path: Path, source_line: int, row: dict[str, str]) -> NormalizedRow:
    lot_number = (row.get("lot_number") or "").strip() or extract_lot_from_filename(path)
    sensor_id = (row.get("sensor_id") or "").strip()
    sensor_number = (row.get("sensor_number") or "").strip()
    if not sensor_id and lot_number and sensor_number:
        sensor_id = f"{lot_number}-{sensor_number}"

    program_offset_v = parse_float(row.get("offset_v"))
    manual_offset_v = parse_float(row.get("actual_offset_v"))
    program_sensitivity_mv = parse_float(row.get("sensitivity_mv"))
    manual_sensitivity_mv = parse_float(row.get("actual_sensitivity_mv"))

    return NormalizedRow(
        source_file=path.name,
        source_line=source_line,
        timestamp=(row.get("timestamp") or "").strip(),
        timestamp_dt=parse_datetime(row.get("timestamp")),
        lot_number=lot_number,
        sensor_number=sensor_number,
        sensor_id=sensor_id,
        filter_setup=(row.get("filter_setup") or "").strip(),
        am502_gain=parse_float(row.get("am502_gain")),
        labjack_range=(row.get("labjack_ain0_ain1_range") or "").strip(),
        program_result=normalize_result(row.get("pass_fail")),
        operator_result=operator_result_from_row(row),
        scope_tag=(row.get("scope_tag") or "").strip(),
        scope_reason=(row.get("scope_reason") or "").strip(),
        polarity=(row.get("polarity") or "").strip(),
        operator_polarity=(row.get("operator_polarity") or "").strip(),
        operator_offset_match=(row.get("operator_offset_match") or "").strip(),
        operator_offset_sensor_status=(row.get("operator_offset_sensor_status") or "").strip(),
        operator_sensitivity_match=(row.get("operator_sensitivity_match") or "").strip(),
        operator_sensitivity_sensor_status=(row.get("operator_sensitivity_sensor_status") or "").strip(),
        program_offset_v=program_offset_v,
        manual_offset_v=manual_offset_v,
        offset_delta_v=compute_delta(manual_offset_v, program_offset_v),
        program_sensitivity_mv=program_sensitivity_mv,
        manual_sensitivity_mv=manual_sensitivity_mv,
        sensitivity_delta_mv=compute_delta(manual_sensitivity_mv, program_sensitivity_mv),
        program_fail_reasons=(row.get("fail_reasons") or "").strip(),
        operator_comments=(row.get("operator_comments") or "").strip(),
        tester_sensor_disagreement_raw=(row.get("tester_sensor_disagreement") or "").strip(),
    )


def row_matches_filters(
    row: NormalizedRow,
    lots: set[str],
    start_date: date | None,
    end_date: date | None,
) -> bool:
    if lots and row.lot_number not in lots:
        return False
    if start_date is None and end_date is None:
        return True
    if row.timestamp_dt is None:
        return False
    row_date = row.timestamp_dt.date()
    if start_date is not None and row_date < start_date:
        return False
    if end_date is not None and row_date > end_date:
        return False
    return True


def read_rows(
    results_dir: Path,
    lots: set[str],
    start_date: date | None,
    end_date: date | None,
) -> tuple[list[NormalizedRow], list[Path], list[str]]:
    warnings: list[str] = []
    paths = sorted(results_dir.glob(SCOPE_CSV_PATTERN))
    rows: list[NormalizedRow] = []

    for path in paths:
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                for source_line, row in enumerate(reader, start=2):
                    normalized = normalize_csv_row(path, source_line, row)
                    if row_matches_filters(normalized, lots, start_date, end_date):
                        rows.append(normalized)
        except OSError as exc:
            warnings.append(f"Could not read {path}: {exc}")

    return rows, paths, warnings


def choose_latest_rows(rows: list[NormalizedRow]) -> tuple[list[NormalizedRow], list[str]]:
    grouped: dict[tuple[str, str], list[NormalizedRow]] = defaultdict(list)
    for row in rows:
        grouped[row.key].append(row)

    chosen: list[NormalizedRow] = []
    warnings: list[str] = []
    for key, group in grouped.items():
        if len(group) == 1:
            chosen.append(group[0])
            continue
        group_sorted = sorted(
            group,
            key=lambda item: (
                item.timestamp_dt or datetime.min,
                item.source_file,
                item.source_line,
            ),
        )
        latest = group_sorted[-1]
        chosen.append(latest)
        lot_number, sensor_id = key
        warnings.append(
            f"Duplicate rows for lot {lot_number or '?'} sensor {sensor_id or '?'}: "
            f"kept {latest.source_file} line {latest.source_line}."
        )

    return sorted(chosen, key=lambda item: (item.lot_number, numeric_sort_key(item.sensor_number), item.sensor_id)), warnings


def numeric_sort_key(text: str) -> tuple[int, str]:
    try:
        return (0, f"{int(text):08d}")
    except (TypeError, ValueError):
        return (1, text or "")


def percent(part: int | float, whole: int | float) -> float:
    if whole == 0:
        return 0.0
    return float(part) / float(whole) * 100.0


def fmt_count_pct(count: int, total: int) -> str:
    return f"{count} ({percent(count, total):.1f}%)"


def fmt_float(value: float | None, decimals: int = 3) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.{decimals}f}"


def fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def result_counts(rows: Iterable[NormalizedRow], field_name: str) -> Counter[str]:
    counter: Counter[str] = Counter()
    for row in rows:
        counter[getattr(row, field_name)] += 1
    return counter


def summarize_lots(rows: list[NormalizedRow]) -> list[dict[str, str]]:
    grouped: dict[str, list[NormalizedRow]] = defaultdict(list)
    for row in rows:
        grouped[row.lot_number or "(blank)"].append(row)

    summary_rows: list[dict[str, str]] = []
    for lot_number, group in sorted(grouped.items(), key=lambda item: numeric_sort_key(item[0])):
        total = len(group)
        operator_pass = sum(1 for row in group if row.operator_result == PASS)
        operator_fail = sum(1 for row in group if row.operator_result == FAIL)
        program_pass = sum(1 for row in group if row.program_result == PASS)
        program_fail = sum(1 for row in group if row.program_result == FAIL)
        op_pass_prog_fail = sum(1 for row in group if row.agreement == "operator_pass_program_fail")
        op_fail_prog_pass = sum(1 for row in group if row.agreement == "operator_fail_program_pass")
        summary_rows.append(
            {
                "lot_number": lot_number,
                "total_units": str(total),
                "operator_pass": str(operator_pass),
                "operator_fail": str(operator_fail),
                "operator_yield_pct": f"{percent(operator_pass, operator_pass + operator_fail):.2f}",
                "program_pass": str(program_pass),
                "program_fail": str(program_fail),
                "program_yield_pct": f"{percent(program_pass, program_pass + program_fail):.2f}",
                "operator_pass_program_fail": str(op_pass_prog_fail),
                "operator_fail_program_pass": str(op_fail_prog_pass),
            }
        )
    return summary_rows


def split_program_reasons(text: str) -> list[str]:
    reasons = [piece.strip() for piece in text.split(";")]
    return [reason for reason in reasons if reason]


def summarize_failure_reasons(rows: list[NormalizedRow]) -> list[dict[str, str]]:
    total = len(rows)
    manual_counter: Counter[str] = Counter()
    program_counter: Counter[str] = Counter()

    for row in rows:
        if row.operator_result == FAIL:
            if row.scope_tag or row.scope_reason:
                label = row.scope_tag
                if row.scope_reason:
                    label = f"{label} - {row.scope_reason}" if label else row.scope_reason
                manual_counter[label] += 1
            else:
                manual_counter["Operator fail, no reason recorded"] += 1

        if row.program_result == FAIL:
            reasons = split_program_reasons(row.program_fail_reasons)
            if not reasons:
                program_counter["Program fail, no reason recorded"] += 1
            for reason in reasons:
                program_counter[reason] += 1

    rows_out: list[dict[str, str]] = []
    for category, counter in (("operator_manual", manual_counter), ("program_tester", program_counter)):
        for reason, count in counter.most_common():
            rows_out.append(
                {
                    "category": category,
                    "reason": reason,
                    "count": str(count),
                    "percent_of_units": f"{percent(count, total):.2f}",
                }
            )
    return rows_out


def summarize_operator_marks(rows: list[NormalizedRow]) -> list[dict[str, str]]:
    checks = [
        ("Offset reading marked wrong", lambda row: row.operator_offset_match.upper() == "WRONG"),
        ("Offset sensor marked good", lambda row: row.operator_offset_sensor_status.upper() == "GOOD"),
        ("Offset sensor marked bad", lambda row: row.operator_offset_sensor_status.upper() == "BAD"),
        ("Sensitivity reading marked wrong", lambda row: row.operator_sensitivity_match.upper() == "WRONG"),
        ("Sensitivity sensor marked good", lambda row: row.operator_sensitivity_sensor_status.upper() == "GOOD"),
        ("Sensitivity sensor marked bad", lambda row: row.operator_sensitivity_sensor_status.upper() == "BAD"),
        ("Polarity marked bad", lambda row: row.operator_polarity.upper() == "BAD"),
    ]
    total = len(rows)
    return [
        {
            "operator_mark": label,
            "count": str(sum(1 for row in rows if predicate(row))),
            "percent_of_units": f"{percent(sum(1 for row in rows if predicate(row)), total):.2f}",
        }
        for label, predicate in checks
    ]


def values_for_metric(rows: list[NormalizedRow], metric_name: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = getattr(row, metric_name)
        if value is not None and math.isfinite(value):
            values.append(value)
    return values


def mean_or_none(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def stdev_or_none(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def measurement_agreement(rows: list[NormalizedRow]) -> list[dict[str, str]]:
    specs = [
        (
            "offset",
            "V",
            [row for row in rows if row.program_offset_v is not None and row.manual_offset_v is not None],
            "program_offset_v",
            "manual_offset_v",
            "offset_delta_v",
        ),
        (
            "sensitivity",
            "mV",
            [row for row in rows if row.program_sensitivity_mv is not None and row.manual_sensitivity_mv is not None],
            "program_sensitivity_mv",
            "manual_sensitivity_mv",
            "sensitivity_delta_mv",
        ),
    ]
    summary: list[dict[str, str]] = []
    for metric, units, group, program_field, manual_field, delta_field in specs:
        program_values = [getattr(row, program_field) for row in group]
        manual_values = [getattr(row, manual_field) for row in group]
        deltas = [getattr(row, delta_field) for row in group]
        abs_deltas = [abs(value) for value in deltas if value is not None]
        summary.append(
            {
                "metric": metric,
                "units": units,
                "paired_readings": str(len(group)),
                "program_mean": fmt_float(mean_or_none(program_values), 4),
                "manual_mean": fmt_float(mean_or_none(manual_values), 4),
                "mean_manual_minus_program": fmt_float(mean_or_none(deltas), 4),
                "median_manual_minus_program": fmt_float(median_or_none(deltas), 4),
                "stdev_delta": fmt_float(stdev_or_none(deltas), 4),
                "mean_absolute_delta": fmt_float(mean_or_none(abs_deltas), 4),
                "max_absolute_delta": fmt_float(max(abs_deltas) if abs_deltas else None, 4),
                "correlation": fmt_float(pearson(program_values, manual_values), 4),
            }
        )
    return summary


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    x_diffs = [x - mean_x for x in xs]
    y_diffs = [y - mean_y for y in ys]
    denom_x = math.sqrt(sum(value * value for value in x_diffs))
    denom_y = math.sqrt(sum(value * value for value in y_diffs))
    if denom_x == 0.0 or denom_y == 0.0:
        return None
    return sum(x * y for x, y in zip(x_diffs, y_diffs)) / (denom_x * denom_y)


def quartile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("quartile requires at least one value")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * q
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    lower_weight = upper_index - position
    upper_weight = position - lower_index
    return sorted_values[lower_index] * lower_weight + sorted_values[upper_index] * upper_weight


def detect_outliers(rows: list[NormalizedRow]) -> tuple[list[Outlier], list[dict[str, str]]]:
    outliers: list[Outlier] = []
    summary: list[dict[str, str]] = []

    for metric_name, display_name, units in NUMERIC_METRICS:
        metric_values = sorted(values_for_metric(rows, metric_name))
        if len(metric_values) < 4:
            summary.append(
                {
                    "metric": display_name,
                    "units": units,
                    "values": str(len(metric_values)),
                    "outliers": "0",
                    "outlier_pct": "0.00",
                    "lower_bound": "",
                    "upper_bound": "",
                }
            )
            continue

        q1 = quartile(metric_values, 0.25)
        q3 = quartile(metric_values, 0.75)
        iqr = q3 - q1
        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr
        metric_outliers = 0
        for row in rows:
            value = getattr(row, metric_name)
            if value is None or not math.isfinite(value):
                continue
            if value < lower_bound or value > upper_bound:
                metric_outliers += 1
                outliers.append(
                    Outlier(
                        source_file=row.source_file,
                        source_line=row.source_line,
                        timestamp=row.timestamp,
                        lot_number=row.lot_number,
                        sensor_id=row.sensor_id,
                        metric=display_name,
                        value=value,
                        lower_bound=lower_bound,
                        upper_bound=upper_bound,
                        direction="low" if value < lower_bound else "high",
                    )
                )
        summary.append(
            {
                "metric": display_name,
                "units": units,
                "values": str(len(metric_values)),
                "outliers": str(metric_outliers),
                "outlier_pct": f"{percent(metric_outliers, len(metric_values)):.2f}",
                "lower_bound": fmt_float(lower_bound, 6),
                "upper_bound": fmt_float(upper_bound, 6),
            }
        )

    return outliers, summary


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def normalized_row_to_csv(row: NormalizedRow) -> dict[str, str]:
    return {
        "source_file": row.source_file,
        "source_line": str(row.source_line),
        "timestamp": row.timestamp,
        "lot_number": row.lot_number,
        "sensor_number": row.sensor_number,
        "sensor_id": row.sensor_id,
        "filter_setup": row.filter_setup,
        "am502_gain": fmt_float(row.am502_gain, 6),
        "labjack_range": row.labjack_range,
        "program_result": row.program_result,
        "operator_result": row.operator_result,
        "agreement": row.agreement,
        "tester_sensor_disagreement": row.tester_sensor_disagreement,
        "scope_tag": row.scope_tag,
        "scope_reason": row.scope_reason,
        "program_offset_v": fmt_float(row.program_offset_v, 6),
        "manual_offset_v": fmt_float(row.manual_offset_v, 6),
        "offset_delta_v": fmt_float(row.offset_delta_v, 6),
        "program_sensitivity_mv": fmt_float(row.program_sensitivity_mv, 6),
        "manual_sensitivity_mv": fmt_float(row.manual_sensitivity_mv, 6),
        "sensitivity_delta_mv": fmt_float(row.sensitivity_delta_mv, 6),
        "polarity": row.polarity,
        "operator_polarity": row.operator_polarity,
        "operator_offset_match": row.operator_offset_match,
        "operator_offset_sensor_status": row.operator_offset_sensor_status,
        "operator_sensitivity_match": row.operator_sensitivity_match,
        "operator_sensitivity_sensor_status": row.operator_sensitivity_sensor_status,
        "program_fail_reasons": row.program_fail_reasons,
        "operator_comments": row.operator_comments,
    }


def disagreement_rows(rows: list[NormalizedRow]) -> list[dict[str, str]]:
    return [
        normalized_row_to_csv(row)
        for row in rows
        if row.agreement in ("operator_pass_program_fail", "operator_fail_program_pass")
    ]


def outlier_to_csv(outlier: Outlier) -> dict[str, str]:
    return {
        "source_file": outlier.source_file,
        "source_line": str(outlier.source_line),
        "timestamp": outlier.timestamp,
        "lot_number": outlier.lot_number,
        "sensor_id": outlier.sensor_id,
        "metric": outlier.metric,
        "value": fmt_float(outlier.value, 6),
        "lower_bound": fmt_float(outlier.lower_bound, 6),
        "upper_bound": fmt_float(outlier.upper_bound, 6),
        "direction": outlier.direction,
    }


def read_autosaves(results_dir: Path) -> list[dict[str, str]]:
    autosave_dir = results_dir / "autosave"
    rows: list[dict[str, str]] = []
    if not autosave_dir.exists():
        return rows
    for path in sorted(autosave_dir.glob("lot_*_current_sensor.json")):
        try:
            with path.open("r", encoding="utf-8") as autosave_file:
                payload = json.load(autosave_file)
        except (OSError, json.JSONDecodeError):
            rows.append({"file": path.name, "stage": "Could not read autosave"})
            continue
        rows.append(
            {
                "file": path.name,
                "timestamp": str(payload.get("timestamp", "")),
                "stage": str(payload.get("stage", "")),
                "lot_number": str(payload.get("lot_number", "")),
                "sensor_number": str(payload.get("sensor_number", "")),
                "sensor_id": str(payload.get("sensor_id", "")),
                "sensor_pass_fail": str(payload.get("sensor_pass_fail", "")),
                "tester_sensor_disagreement": str(payload.get("tester_sensor_disagreement", "")),
                "operator_offset_match": str(payload.get("operator_offset_match", "")),
                "operator_sensitivity_match": str(payload.get("operator_sensitivity_match", "")),
            }
        )
    return rows


def table_html(headers: list[str], rows: list[Iterable[str]], css_class: str = "") -> str:
    class_attr = f' class="{html.escape(css_class)}"' if css_class else ""
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        body_rows.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>")
    return f"<table{class_attr}><thead><tr>{header_html}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def card_html(label: str, value: str, helper: str = "") -> str:
    return (
        '<div class="card">'
        f'<div class="card-label">{html.escape(label)}</div>'
        f'<div class="card-value">{html.escape(value)}</div>'
        f'<div class="card-helper">{html.escape(helper)}</div>'
        "</div>"
    )


def normal_pdf(x: float, mean: float, stdev: float) -> float:
    if stdev <= 0:
        return 0.0
    exponent = -0.5 * ((x - mean) / stdev) ** 2
    return math.exp(exponent) / (stdev * math.sqrt(2.0 * math.pi))


def nice_range(series_values: list[list[float]]) -> tuple[float, float]:
    values = [value for series in series_values for value in series if math.isfinite(value)]
    if not values:
        return (0.0, 1.0)
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        margin = abs(minimum) * 0.1 or 1.0
        return minimum - margin, maximum + margin
    margin = (maximum - minimum) * 0.08
    return minimum - margin, maximum + margin


def bell_curve_svg(title: str, series: list[tuple[str, list[float], str]], x_label: str) -> str:
    clean_series = [(label, [value for value in values if math.isfinite(value)], color) for label, values, color in series]
    clean_series = [(label, values, color) for label, values, color in clean_series if values]
    if not clean_series:
        return f'<div class="chart-empty">No numeric data for {html.escape(title)}.</div>'

    width = 860
    height = 300
    left = 62
    right = 24
    top = 32
    bottom = 56
    plot_width = width - left - right
    plot_height = height - top - bottom
    x_min, x_max = nice_range([values for _, values, _ in clean_series])

    pdf_series: list[tuple[str, list[tuple[float, float]], str, float, float, int]] = []
    y_max = 0.0
    steps = 160
    for label, values, color in clean_series:
        mean = statistics.mean(values)
        stdev = statistics.stdev(values) if len(values) >= 2 else 0.0
        if stdev <= 0:
            curve = [(mean, 1.0)]
            y_max = max(y_max, 1.0)
        else:
            curve = []
            for index in range(steps + 1):
                x = x_min + (x_max - x_min) * index / steps
                y = normal_pdf(x, mean, stdev)
                y_max = max(y_max, y)
                curve.append((x, y))
        pdf_series.append((label, curve, color, mean, stdev, len(values)))

    if y_max <= 0:
        y_max = 1.0

    def x_pos(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * plot_width

    def y_pos(value: float) -> float:
        return top + plot_height - (value / y_max) * plot_height

    axis = [
        f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" class="axis" />',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" class="axis" />',
    ]
    tick_parts: list[str] = []
    for index in range(6):
        value = x_min + (x_max - x_min) * index / 5
        x = x_pos(value)
        tick_parts.append(f'<line x1="{x:.1f}" y1="{top + plot_height}" x2="{x:.1f}" y2="{top + plot_height + 5}" class="tick" />')
        tick_parts.append(f'<text x="{x:.1f}" y="{top + plot_height + 22}" class="tick-label" text-anchor="middle">{value:.2f}</text>')

    curve_parts: list[str] = []
    legend_parts: list[str] = []
    for legend_index, (label, curve, color, mean, stdev, count) in enumerate(pdf_series):
        if len(curve) == 1:
            x = x_pos(curve[0][0])
            curve_parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}" stroke="{color}" stroke-width="3" />')
        else:
            points = " ".join(f"{x_pos(x):.1f},{y_pos(y):.1f}" for x, y in curve)
            curve_parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" />')
        legend_y = 18 + legend_index * 18
        legend = f"{label}: n={count}, mean={mean:.3f}, sd={stdev:.3f}" if stdev > 0 else f"{label}: n={count}, mean={mean:.3f}, sd=0"
        legend_parts.append(f'<rect x="{left + 10}" y="{legend_y - 10}" width="10" height="10" fill="{color}" />')
        legend_parts.append(f'<text x="{left + 28}" y="{legend_y}" class="legend">{html.escape(legend)}</text>')

    return (
        f'<div class="chart"><h3>{html.escape(title)}</h3>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">'
        + "".join(axis)
        + "".join(tick_parts)
        + "".join(curve_parts)
        + "".join(legend_parts)
        + f'<text x="{left + plot_width / 2:.1f}" y="{height - 12}" class="axis-label" text-anchor="middle">{html.escape(x_label)}</text>'
        "</svg></div>"
    )


def bar_svg(title: str, data: list[tuple[str, int]], color: str = "#2563eb") -> str:
    data = [(label, count) for label, count in data if count > 0]
    if not data:
        return f'<div class="chart-empty">No data for {html.escape(title)}.</div>'
    width = 860
    bar_height = 26
    gap = 10
    left = 230
    right = 40
    top = 34
    height = top + len(data) * (bar_height + gap) + 28
    max_count = max(count for _, count in data)
    parts = [f'<div class="chart"><h3>{html.escape(title)}</h3><svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">']
    for index, (label, count) in enumerate(data):
        y = top + index * (bar_height + gap)
        bar_width = (width - left - right) * count / max_count
        parts.append(f'<text x="{left - 12}" y="{y + 18}" class="bar-label" text-anchor="end">{html.escape(label[:36])}</text>')
        parts.append(f'<rect x="{left}" y="{y}" width="{bar_width:.1f}" height="{bar_height}" rx="3" fill="{color}" />')
        parts.append(f'<text x="{left + bar_width + 8:.1f}" y="{y + 18}" class="bar-value">{count}</text>')
    parts.append("</svg></div>")
    return "".join(parts)


def generate_html_report(
    report_path: Path,
    results_dir: Path,
    source_paths: list[Path],
    raw_row_count: int,
    rows: list[NormalizedRow],
    lot_summary: list[dict[str, str]],
    failure_reasons: list[dict[str, str]],
    operator_marks: list[dict[str, str]],
    agreement_stats: list[dict[str, str]],
    outlier_summary: list[dict[str, str]],
    outliers: list[Outlier],
    autosaves: list[dict[str, str]],
    warnings: list[str],
    export_paths: list[Path],
) -> None:
    total_units = len(rows)
    operator_counts = result_counts(rows, "operator_result")
    program_counts = result_counts(rows, "program_result")
    agreement_counts = Counter(row.agreement for row in rows)
    disagreement_count = agreement_counts["operator_pass_program_fail"] + agreement_counts["operator_fail_program_pass"]
    rows_with_outliers = {(outlier.source_file, outlier.source_line) for outlier in outliers}

    cards = [
        card_html("Units Analyzed", str(total_units), f"{raw_row_count} rows loaded before duplicate handling"),
        card_html("Official Operator Yield", fmt_pct(percent(operator_counts[PASS], operator_counts[PASS] + operator_counts[FAIL])), fmt_count_pct(operator_counts[PASS], operator_counts[PASS] + operator_counts[FAIL])),
        card_html("Official Fail Rate", fmt_pct(percent(operator_counts[FAIL], operator_counts[PASS] + operator_counts[FAIL])), fmt_count_pct(operator_counts[FAIL], operator_counts[PASS] + operator_counts[FAIL])),
        card_html("Program Yield", fmt_pct(percent(program_counts[PASS], program_counts[PASS] + program_counts[FAIL])), fmt_count_pct(program_counts[PASS], program_counts[PASS] + program_counts[FAIL])),
        card_html("Disagreements", fmt_count_pct(disagreement_count, total_units), "operator/manual result vs tester result"),
        card_html("Rows With Outliers", fmt_count_pct(len(rows_with_outliers), total_units), "IQR rule across numeric metrics"),
    ]

    agreement_rows = [
        ("Both pass", agreement_counts["both_pass"]),
        ("Both fail", agreement_counts["both_fail"]),
        ("Operator pass / program fail", agreement_counts["operator_pass_program_fail"]),
        ("Operator fail / program pass", agreement_counts["operator_fail_program_pass"]),
        ("Unknown", agreement_counts[UNKNOWN]),
    ]

    manual_failure_data = [
        (row["reason"], int(row["count"]))
        for row in failure_reasons
        if row["category"] == "operator_manual"
    ][:12]
    program_failure_data = [
        (row["reason"], int(row["count"]))
        for row in failure_reasons
        if row["category"] == "program_tester"
    ][:12]

    charts = [
        bell_curve_svg(
            "Offset Distribution: Program vs Manual",
            [
                ("Program offset", values_for_metric(rows, "program_offset_v"), "#1d4ed8"),
                ("Manual offset", values_for_metric(rows, "manual_offset_v"), "#16a34a"),
            ],
            "Offset (V)",
        ),
        bell_curve_svg(
            "Sensitivity Distribution: Program vs Manual",
            [
                ("Program sensitivity", values_for_metric(rows, "program_sensitivity_mv"), "#7c3aed"),
                ("Manual sensitivity", values_for_metric(rows, "manual_sensitivity_mv"), "#ea580c"),
            ],
            "Sensitivity (mV)",
        ),
        bell_curve_svg(
            "Offset Delta Distribution",
            [("Manual minus program", values_for_metric(rows, "offset_delta_v"), "#0891b2")],
            "Offset delta (V)",
        ),
        bell_curve_svg(
            "Sensitivity Delta Distribution",
            [("Manual minus program", values_for_metric(rows, "sensitivity_delta_mv"), "#be123c")],
            "Sensitivity delta (mV)",
        ),
        bar_svg("Top Manual Failure Reasons", manual_failure_data, "#dc2626"),
        bar_svg("Top Program Failure Reasons", program_failure_data, "#9333ea"),
    ]

    source_list = "".join(f"<li>{html.escape(str(path))}</li>" for path in source_paths)
    export_list = "".join(f"<li>{html.escape(str(path))}</li>" for path in export_paths)
    warning_html = "<p>No data-quality warnings.</p>"
    if warnings:
        warning_html = "<ul>" + "".join(f"<li>{html.escape(warning)}</li>" for warning in warnings) + "</ul>"

    autosave_html = ""
    if autosaves:
        autosave_html = (
            "<section><h2>Autosave Summary</h2>"
            "<p>Autosaves are listed for visibility only and are not counted in yield.</p>"
            + table_html(
                ["File", "Timestamp", "Stage", "Lot", "Sensor", "Result", "Disagreement"],
                [
                    (
                        row.get("file", ""),
                        row.get("timestamp", ""),
                        row.get("stage", ""),
                        row.get("lot_number", ""),
                        row.get("sensor_id", ""),
                        row.get("sensor_pass_fail", ""),
                        row.get("tester_sensor_disagreement", ""),
                    )
                    for row in autosaves
                ],
            )
            + "</section>"
        )

    report_html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>406MCA Data Analysis</title>
<style>
body {{
    margin: 0;
    font-family: Arial, Helvetica, sans-serif;
    color: #172033;
    background: #f5f7fb;
}}
header {{
    padding: 28px 34px 22px;
    background: #102033;
    color: white;
}}
header h1 {{
    margin: 0 0 8px;
    font-size: 30px;
    letter-spacing: 0;
}}
header p {{
    margin: 4px 0;
    color: #dbe7f5;
}}
main {{
    max-width: 1160px;
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
    margin: 0 0 16px;
    font-size: 22px;
    letter-spacing: 0;
}}
h3 {{
    margin: 0 0 12px;
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
.card-label {{
    font-size: 12px;
    text-transform: uppercase;
    color: #5b677a;
}}
.card-value {{
    margin-top: 8px;
    font-size: 26px;
    font-weight: 700;
}}
.card-helper {{
    margin-top: 6px;
    color: #607087;
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
    color: #26364d;
}}
.chart {{
    margin: 18px 0 4px;
    overflow-x: auto;
}}
.chart-empty {{
    padding: 14px;
    color: #6b7280;
    border: 1px dashed #cbd5e1;
    border-radius: 8px;
}}
svg {{
    width: 100%;
    min-width: 720px;
    height: auto;
}}
.axis, .tick {{
    stroke: #64748b;
    stroke-width: 1;
}}
.tick-label, .axis-label, .legend, .bar-label, .bar-value {{
    fill: #334155;
    font-size: 12px;
}}
.legend {{
    font-size: 13px;
}}
ul {{
    margin-top: 8px;
}}
</style>
</head>
<body>
<header>
<h1>406MCA Scope Verification Data Analysis</h1>
<p>Generated {html.escape(datetime.now().isoformat(timespec="seconds"))}</p>
<p>Results directory: {html.escape(str(results_dir))}</p>
</header>
<main>
<section>
<h2>Summary</h2>
<div class="cards">{''.join(cards)}</div>
</section>
<section>
<h2>Program vs Operator Agreement</h2>
{table_html(["Category", "Count", "Percent of units"], [(label, str(count), fmt_pct(percent(count, total_units))) for label, count in agreement_rows])}
</section>
<section>
<h2>Lot Summary</h2>
{table_html(["Lot", "Total", "Operator Pass", "Operator Fail", "Operator Yield %", "Program Pass", "Program Fail", "Program Yield %", "Operator Pass / Program Fail", "Operator Fail / Program Pass"], [(row["lot_number"], row["total_units"], row["operator_pass"], row["operator_fail"], row["operator_yield_pct"], row["program_pass"], row["program_fail"], row["program_yield_pct"], row["operator_pass_program_fail"], row["operator_fail_program_pass"]) for row in lot_summary])}
</section>
<section>
<h2>Bell-Curve Distributions</h2>
{''.join(charts[:4])}
</section>
<section>
<h2>Failure Reasons</h2>
{''.join(charts[4:])}
{table_html(["Category", "Reason", "Count", "Percent of units"], [(row["category"], row["reason"], row["count"], row["percent_of_units"]) for row in failure_reasons])}
</section>
<section>
<h2>Operator-Marked Reading Problems</h2>
{table_html(["Operator mark", "Count", "Percent of units"], [(row["operator_mark"], row["count"], row["percent_of_units"]) for row in operator_marks])}
</section>
<section>
<h2>Measurement Agreement</h2>
{table_html(["Metric", "Units", "Pairs", "Program Mean", "Manual Mean", "Mean Delta", "Median Delta", "Delta Std Dev", "Mean Abs Delta", "Max Abs Delta", "Correlation"], [(row["metric"], row["units"], row["paired_readings"], row["program_mean"], row["manual_mean"], row["mean_manual_minus_program"], row["median_manual_minus_program"], row["stdev_delta"], row["mean_absolute_delta"], row["max_absolute_delta"], row["correlation"]) for row in agreement_stats])}
</section>
<section>
<h2>IQR Statistical Outliers</h2>
{table_html(["Metric", "Units", "Values", "Outliers", "Outlier %", "Lower Bound", "Upper Bound"], [(row["metric"], row["units"], row["values"], row["outliers"], row["outlier_pct"], row["lower_bound"], row["upper_bound"]) for row in outlier_summary])}
</section>
{autosave_html}
<section>
<h2>Data Quality And Outputs</h2>
<h3>Warnings</h3>
{warning_html}
<h3>Source CSVs</h3>
<ul>{source_list}</ul>
<h3>Exported Files</h3>
<ul>{export_list}</ul>
</section>
</main>
</body>
</html>
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_html, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze 406MCA scope-verification lot CSV data.")
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help=f"Folder containing 406mca_scope_verification_lot_*.csv files. Default: {DEFAULT_RESULTS_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for generated HTML and CSV exports. Default: RESULTS_DIR\\analysis.",
    )
    parser.add_argument("--lot", action="append", default=[], help="Lot number to include. Repeat for multiple lots.")
    parser.add_argument("--start-date", default="", help="Include rows on or after YYYY-MM-DD.")
    parser.add_argument("--end-date", default="", help="Include rows on or before YYYY-MM-DD.")
    parser.add_argument(
        "--include-autosave-summary",
        action="store_true",
        help="List in-progress autosave JSON files without counting them in yield.",
    )
    parser.add_argument("--open", action="store_true", help="Open the generated HTML report after writing it.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else results_dir / "analysis"
    lots = {lot.strip() for lot in args.lot if lot.strip()}
    start_date = parse_date_arg(args.start_date, "--start-date")
    end_date = parse_date_arg(args.end_date, "--end-date")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise SystemExit("--start-date cannot be after --end-date.")

    if not results_dir.exists():
        raise SystemExit(f"Results directory not found: {results_dir}")

    raw_rows, source_paths, warnings = read_rows(results_dir, lots, start_date, end_date)
    rows, duplicate_warnings = choose_latest_rows(raw_rows)
    warnings.extend(duplicate_warnings)
    if not source_paths:
        warnings.append(f"No source CSVs matched {SCOPE_CSV_PATTERN}.")
    if not rows:
        raise SystemExit("No rows matched the requested filters.")

    lot_summary = summarize_lots(rows)
    failure_reasons = summarize_failure_reasons(rows)
    operator_marks = summarize_operator_marks(rows)
    agreement_stats = measurement_agreement(rows)
    outliers, outlier_summary = detect_outliers(rows)
    autosaves = read_autosaves(results_dir) if args.include_autosave_summary else []

    normalized_csv_rows = [normalized_row_to_csv(row) for row in rows]
    lot_summary_path = output_dir / LOT_SUMMARY_FILENAME
    normalized_rows_path = output_dir / NORMALIZED_ROWS_FILENAME
    disagreements_path = output_dir / DISAGREEMENTS_FILENAME
    outliers_path = output_dir / OUTLIERS_FILENAME
    failure_reasons_path = output_dir / FAILURE_REASONS_FILENAME
    report_path = output_dir / REPORT_FILENAME
    export_paths = [
        report_path,
        normalized_rows_path,
        lot_summary_path,
        disagreements_path,
        outliers_path,
        failure_reasons_path,
    ]

    write_csv(normalized_rows_path, normalized_csv_rows, list(normalized_csv_rows[0].keys()))
    write_csv(lot_summary_path, lot_summary, list(lot_summary[0].keys()))
    disagreement_data = disagreement_rows(rows)
    write_csv(
        disagreements_path,
        disagreement_data,
        list(normalized_csv_rows[0].keys()),
    )
    write_csv(
        outliers_path,
        [outlier_to_csv(outlier) for outlier in outliers],
        ["source_file", "source_line", "timestamp", "lot_number", "sensor_id", "metric", "value", "lower_bound", "upper_bound", "direction"],
    )
    write_csv(
        failure_reasons_path,
        failure_reasons,
        ["category", "reason", "count", "percent_of_units"],
    )
    if args.include_autosave_summary:
        autosave_path = output_dir / AUTOSAVE_SUMMARY_FILENAME
        export_paths.append(autosave_path)
        autosave_fields = [
            "file",
            "timestamp",
            "stage",
            "lot_number",
            "sensor_number",
            "sensor_id",
            "sensor_pass_fail",
            "tester_sensor_disagreement",
            "operator_offset_match",
            "operator_sensitivity_match",
        ]
        write_csv(autosave_path, autosaves, autosave_fields)

    generate_html_report(
        report_path=report_path,
        results_dir=results_dir,
        source_paths=source_paths,
        raw_row_count=len(raw_rows),
        rows=rows,
        lot_summary=lot_summary,
        failure_reasons=failure_reasons,
        operator_marks=operator_marks,
        agreement_stats=agreement_stats,
        outlier_summary=outlier_summary,
        outliers=outliers,
        autosaves=autosaves,
        warnings=warnings,
        export_paths=export_paths,
    )

    operator_counts = result_counts(rows, "operator_result")
    program_counts = result_counts(rows, "program_result")
    disagreements = len(disagreement_rows(rows))
    print("406MCA data analysis complete")
    print(f"Source CSV files found: {len(source_paths)}")
    print(f"Rows analyzed: {len(rows)} ({len(raw_rows)} loaded before duplicate handling)")
    print(
        "Official operator yield: "
        f"{fmt_pct(percent(operator_counts[PASS], operator_counts[PASS] + operator_counts[FAIL]))} "
        f"({operator_counts[PASS]} pass / {operator_counts[FAIL]} fail)"
    )
    print(
        "Program yield: "
        f"{fmt_pct(percent(program_counts[PASS], program_counts[PASS] + program_counts[FAIL]))} "
        f"({program_counts[PASS]} pass / {program_counts[FAIL]} fail)"
    )
    print(f"Program/operator disagreements: {disagreements}")
    print(f"IQR outlier entries: {len(outliers)}")
    print(f"Wrote HTML report: {report_path}")
    print(f"Wrote CSV exports to: {output_dir}")

    if args.open:
        webbrowser.open(report_path.resolve().as_uri())


if __name__ == "__main__":
    main()
