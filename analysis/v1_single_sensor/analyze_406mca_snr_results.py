"""
Analyze 406MCA voltage/distance sweep CSV results.

Default run:
    python analyze_406mca_snr_results.py

Or pass a CSV path:
    python analyze_406mca_snr_results.py C:\\Users\\vma\\Documents\\Eltec_406MCA_Test_Results\\406mca_results.csv

The script prints a console summary, writes a group summary CSV, and writes a
Word-compatible .docx report using only the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import html
import math
import statistics
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_RESULTS_CSV = (
    Path.home() / "Documents" / "Eltec_406MCA_Test_Results" / "v1_single_sensor" / "406mca_results.csv"
)
DEFAULT_TIE_DB = 1.0
DEFAULT_MIN_RUNS = 3


@dataclass(frozen=True)
class Measurement:
    timestamp: str
    sensor_id: str
    model: str
    filter_setup: str
    distance_cm: float
    input_voltage_v: float
    offset_v: float | None
    sensitivity_mv: float | None
    noise_rms_mv: float | None
    snr_db: float
    polarity: str
    pass_fail: str
    fail_reasons: str


@dataclass(frozen=True)
class GroupSummary:
    distance_cm: float
    input_voltage_v: float
    runs: int
    pass_runs: int
    fail_runs: int
    mean_snr_db: float
    std_snr_db: float | None
    min_snr_db: float
    max_snr_db: float
    mean_sensitivity_mv: float | None
    std_sensitivity_mv: float | None
    mean_noise_rms_mv: float | None
    std_noise_rms_mv: float | None
    mean_offset_v: float | None
    fail_reasons: str

    @property
    def pass_rate(self) -> float:
        return self.pass_runs / self.runs if self.runs else 0.0

    @property
    def ci95_snr_half_width(self) -> float | None:
        if self.std_snr_db is None or self.runs < 2:
            return None
        return 1.96 * self.std_snr_db / math.sqrt(self.runs)


def parse_float(row: dict[str, str], field_name: str) -> float | None:
    text = (row.get(field_name) or "").strip()
    if not text:
        return None
    if text.lower() == "inf":
        return math.inf
    if text.lower() == "-inf":
        return -math.inf
    return float(text)


def read_measurements(csv_path: Path) -> tuple[list[Measurement], list[str]]:
    measurements: list[Measurement] = []
    skipped: list[str] = []

    with csv_path.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        for line_number, row in enumerate(reader, start=2):
            if not row or row.get("timestamp") == "timestamp":
                continue

            try:
                distance_cm = parse_float(row, "distance_cm")
                input_voltage_v = parse_float(row, "input_voltage_v")
                snr_db = parse_float(row, "snr_db")
            except ValueError as exc:
                skipped.append(f"line {line_number}: could not parse numeric metadata ({exc})")
                continue

            if distance_cm is None or input_voltage_v is None or snr_db is None:
                skipped.append(
                    f"line {line_number}: missing distance_cm, input_voltage_v, or snr_db; "
                    "likely an older CSV row"
                )
                continue

            if not math.isfinite(snr_db):
                skipped.append(f"line {line_number}: snr_db is not finite")
                continue

            measurement = Measurement(
                timestamp=(row.get("timestamp") or "").strip(),
                sensor_id=(row.get("sensor_id") or "").strip(),
                model=(row.get("model") or "").strip(),
                filter_setup=(row.get("filter_setup") or "").strip(),
                distance_cm=distance_cm,
                input_voltage_v=input_voltage_v,
                offset_v=parse_float(row, "offset_v"),
                sensitivity_mv=parse_float(row, "sensitivity_mv"),
                noise_rms_mv=parse_float(row, "noise_rms_mv"),
                snr_db=snr_db,
                polarity=(row.get("polarity") or "").strip(),
                pass_fail=(row.get("pass_fail") or "").strip().upper(),
                fail_reasons=(row.get("fail_reasons") or "").strip(),
            )
            measurements.append(measurement)

    return measurements, skipped


def mean_or_none(values: list[float | None]) -> float | None:
    finite_values = [value for value in values if value is not None and math.isfinite(value)]
    if not finite_values:
        return None
    return statistics.mean(finite_values)


def stdev_or_none(values: list[float | None]) -> float | None:
    finite_values = [value for value in values if value is not None and math.isfinite(value)]
    if len(finite_values) < 2:
        return None
    return statistics.stdev(finite_values)


def summarize_groups(measurements: list[Measurement]) -> list[GroupSummary]:
    grouped: dict[tuple[float, float], list[Measurement]] = defaultdict(list)
    for measurement in measurements:
        grouped[(measurement.distance_cm, measurement.input_voltage_v)].append(measurement)

    summaries: list[GroupSummary] = []
    for (distance_cm, input_voltage_v), group in grouped.items():
        snr_values = [measurement.snr_db for measurement in group]
        fail_reasons = sorted(
            {
                measurement.fail_reasons
                for measurement in group
                if measurement.fail_reasons
            }
        )
        summaries.append(
            GroupSummary(
                distance_cm=distance_cm,
                input_voltage_v=input_voltage_v,
                runs=len(group),
                pass_runs=sum(1 for measurement in group if measurement.pass_fail == "PASS"),
                fail_runs=sum(1 for measurement in group if measurement.pass_fail != "PASS"),
                mean_snr_db=statistics.mean(snr_values),
                std_snr_db=statistics.stdev(snr_values) if len(snr_values) >= 2 else None,
                min_snr_db=min(snr_values),
                max_snr_db=max(snr_values),
                mean_sensitivity_mv=mean_or_none([measurement.sensitivity_mv for measurement in group]),
                std_sensitivity_mv=stdev_or_none([measurement.sensitivity_mv for measurement in group]),
                mean_noise_rms_mv=mean_or_none([measurement.noise_rms_mv for measurement in group]),
                std_noise_rms_mv=stdev_or_none([measurement.noise_rms_mv for measurement in group]),
                mean_offset_v=mean_or_none([measurement.offset_v for measurement in group]),
                fail_reasons="; ".join(fail_reasons),
            )
        )

    return sorted(summaries, key=lambda item: (item.distance_cm, item.input_voltage_v))


def eligible_summaries(summaries: list[GroupSummary]) -> list[GroupSummary]:
    return [
        summary
        for summary in summaries
        if summary.pass_runs > 0
        and summary.pass_rate == 1.0
        and math.isfinite(summary.mean_snr_db)
    ]


def choose_strict_best(summaries: list[GroupSummary]) -> GroupSummary | None:
    eligible = eligible_summaries(summaries)
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item.mean_snr_db, item.pass_rate, item.runs))


def choose_practical_best(summaries: list[GroupSummary], tie_db: float) -> GroupSummary | None:
    strict_best = choose_strict_best(summaries)
    if strict_best is None:
        return None

    near_ties = [
        summary
        for summary in eligible_summaries(summaries)
        if strict_best.mean_snr_db - summary.mean_snr_db <= tie_db
    ]
    if not near_ties:
        return strict_best

    return min(
        near_ties,
        key=lambda item: (
            item.input_voltage_v,
            item.mean_noise_rms_mv if item.mean_noise_rms_mv is not None else math.inf,
            -item.mean_snr_db,
            item.distance_cm,
        ),
    )


def best_by_distance(summaries: list[GroupSummary]) -> list[GroupSummary]:
    best: list[GroupSummary] = []
    distances = sorted({summary.distance_cm for summary in summaries})
    for distance_cm in distances:
        group = [summary for summary in summaries if summary.distance_cm == distance_cm and summary.pass_rate == 1.0]
        if group:
            best.append(max(group, key=lambda item: item.mean_snr_db))
    return best


def distance_summary_rows(summaries: list[GroupSummary]) -> list[dict[str, float | int | None]]:
    rows: list[dict[str, float | int | None]] = []
    distances = sorted({summary.distance_cm for summary in summaries})
    for distance_cm in distances:
        group = [summary for summary in summaries if summary.distance_cm == distance_cm]
        if not group:
            continue
        best = max(group, key=lambda item: item.mean_snr_db)
        total_runs = sum(item.runs for item in group)
        weighted_snr = sum(item.mean_snr_db * item.runs for item in group) / total_runs
        weighted_noise = sum(
            (item.mean_noise_rms_mv or 0.0) * item.runs
            for item in group
            if item.mean_noise_rms_mv is not None
        )
        noise_count = sum(item.runs for item in group if item.mean_noise_rms_mv is not None)
        weighted_sensitivity = sum(
            (item.mean_sensitivity_mv or 0.0) * item.runs
            for item in group
            if item.mean_sensitivity_mv is not None
        )
        sensitivity_count = sum(item.runs for item in group if item.mean_sensitivity_mv is not None)
        rows.append(
            {
                "distance_cm": distance_cm,
                "runs": total_runs,
                "mean_snr_db": weighted_snr,
                "best_voltage_v": best.input_voltage_v,
                "best_snr_db": best.mean_snr_db,
                "mean_noise_rms_mv": None if noise_count == 0 else weighted_noise / noise_count,
                "mean_sensitivity_mv": None if sensitivity_count == 0 else weighted_sensitivity / sensitivity_count,
            }
        )
    return rows


def format_float(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return f"{value:.{digits}f}{suffix}"


def candidate_text(candidate: GroupSummary | None) -> str:
    if candidate is None:
        return "No eligible passing candidate was found."
    return (
        f"{candidate.distance_cm:g} cm at {candidate.input_voltage_v:g} V "
        f"with mean SNR {candidate.mean_snr_db:.2f} dB, "
        f"mean sensitivity {format_float(candidate.mean_sensitivity_mv, 2, ' mV')}, "
        f"and mean noise {format_float(candidate.mean_noise_rms_mv, 3, ' mV')}."
    )


def generate_warnings(
    measurements: list[Measurement],
    summaries: list[GroupSummary],
    strict_best: GroupSummary | None,
    min_runs: int,
) -> list[str]:
    warnings: list[str] = []
    if not measurements:
        return ["No valid measurement rows were found."]

    low_run_groups = [summary for summary in summaries if summary.runs < min_runs]
    if low_run_groups:
        warnings.append(
            f"{len(low_run_groups)} of {len(summaries)} distance/voltage groups have fewer than "
            f"{min_runs} runs. Repeat the leading candidates before making a final setting."
        )

    failed_rows = [measurement for measurement in measurements if measurement.pass_fail != "PASS"]
    if failed_rows:
        warnings.append(f"{len(failed_rows)} rows failed. Failed rows are excluded from candidate selection.")

    distances = sorted({measurement.distance_cm for measurement in measurements})
    if distances and max(distances) < 10:
        warnings.append(
            "All logged distances are under 10 cm. If these were intended to be 45, 56, or 65 cm, "
            "enter the full centimeter value in the tester."
        )

    for distance_cm in sorted({summary.distance_cm for summary in summaries}):
        group = sorted(
            [summary for summary in summaries if summary.distance_cm == distance_cm],
            key=lambda item: item.input_voltage_v,
        )
        if len(group) < 2:
            continue
        best = max(group, key=lambda item: item.mean_snr_db)
        max_voltage = max(item.input_voltage_v for item in group)
        if math.isclose(best.input_voltage_v, max_voltage):
            warnings.append(
                f"At {distance_cm:g} cm, the best SNR was at the highest tested voltage "
                f"({max_voltage:g} V). If safe, test slightly above this voltage or repeat the top range."
            )

    if strict_best is not None and strict_best.runs < min_runs:
        warnings.append(
            "The strict best candidate is based on too few repeats for a production decision."
        )

    return warnings


def write_summary_csv(summaries: list[GroupSummary], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "distance_cm",
        "input_voltage_v",
        "runs",
        "pass_runs",
        "fail_runs",
        "pass_rate",
        "mean_snr_db",
        "std_snr_db",
        "ci95_snr_half_width",
        "min_snr_db",
        "max_snr_db",
        "mean_sensitivity_mv",
        "std_sensitivity_mv",
        "mean_noise_rms_mv",
        "std_noise_rms_mv",
        "mean_offset_v",
        "fail_reasons",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for summary in summaries:
            writer.writerow(
                {
                    "distance_cm": f"{summary.distance_cm:.6f}",
                    "input_voltage_v": f"{summary.input_voltage_v:.6f}",
                    "runs": summary.runs,
                    "pass_runs": summary.pass_runs,
                    "fail_runs": summary.fail_runs,
                    "pass_rate": f"{summary.pass_rate:.6f}",
                    "mean_snr_db": f"{summary.mean_snr_db:.6f}",
                    "std_snr_db": "" if summary.std_snr_db is None else f"{summary.std_snr_db:.6f}",
                    "ci95_snr_half_width": ""
                    if summary.ci95_snr_half_width is None
                    else f"{summary.ci95_snr_half_width:.6f}",
                    "min_snr_db": f"{summary.min_snr_db:.6f}",
                    "max_snr_db": f"{summary.max_snr_db:.6f}",
                    "mean_sensitivity_mv": ""
                    if summary.mean_sensitivity_mv is None
                    else f"{summary.mean_sensitivity_mv:.6f}",
                    "std_sensitivity_mv": ""
                    if summary.std_sensitivity_mv is None
                    else f"{summary.std_sensitivity_mv:.6f}",
                    "mean_noise_rms_mv": ""
                    if summary.mean_noise_rms_mv is None
                    else f"{summary.mean_noise_rms_mv:.6f}",
                    "std_noise_rms_mv": ""
                    if summary.std_noise_rms_mv is None
                    else f"{summary.std_noise_rms_mv:.6f}",
                    "mean_offset_v": "" if summary.mean_offset_v is None else f"{summary.mean_offset_v:.6f}",
                    "fail_reasons": summary.fail_reasons,
                }
            )


def xml_text(text: str) -> str:
    return html.escape(text, quote=False)


def docx_paragraph(text: str = "", bold: bool = False, size: int = 22) -> str:
    bold_xml = "<w:b/>" if bold else ""
    escaped = xml_text(text)
    return (
        "<w:p>"
        "<w:r>"
        f"<w:rPr>{bold_xml}<w:sz w:val=\"{size}\"/><w:szCs w:val=\"{size}\"/></w:rPr>"
        f"<w:t xml:space=\"preserve\">{escaped}</w:t>"
        "</w:r>"
        "</w:p>"
    )


def docx_table(headers: list[str], rows: list[list[str]]) -> str:
    border = (
        "<w:tblPr>"
        "<w:tblW w:w=\"0\" w:type=\"auto\"/>"
        "<w:tblBorders>"
        "<w:top w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"999999\"/>"
        "<w:left w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"999999\"/>"
        "<w:bottom w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"999999\"/>"
        "<w:right w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"999999\"/>"
        "<w:insideH w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"999999\"/>"
        "<w:insideV w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"999999\"/>"
        "</w:tblBorders>"
        "</w:tblPr>"
    )

    def cell(text: str, bold: bool = False) -> str:
        return f"<w:tc><w:tcPr><w:tcW w:w=\"0\" w:type=\"auto\"/></w:tcPr>{docx_paragraph(text, bold=bold, size=20)}</w:tc>"

    xml_rows = []
    xml_rows.append("<w:tr>" + "".join(cell(header, bold=True) for header in headers) + "</w:tr>")
    for row in rows:
        xml_rows.append("<w:tr>" + "".join(cell(value) for value in row) + "</w:tr>")
    return "<w:tbl>" + border + "".join(xml_rows) + "</w:tbl>"


def write_docx_report(
    output_path: Path,
    csv_path: Path,
    measurements: list[Measurement],
    skipped: list[str],
    summaries: list[GroupSummary],
    strict_best: GroupSummary | None,
    practical_best: GroupSummary | None,
    warnings: list[str],
    tie_db: float,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    parts.append(docx_paragraph("406MCA SNR Sweep Analysis", bold=True, size=32))
    parts.append(docx_paragraph(f"CSV analyzed: {csv_path}", size=20))
    parts.append(docx_paragraph(f"Report generated: {datetime.now().isoformat(timespec='seconds')}", size=20))

    parts.append(docx_paragraph("Main Result", bold=True, size=26))
    parts.append(docx_paragraph(f"Strict highest-SNR candidate: {candidate_text(strict_best)}"))
    parts.append(
        docx_paragraph(
            f"Practical lower-voltage candidate within {tie_db:g} dB of strict best: "
            f"{candidate_text(practical_best)}"
        )
    )

    parts.append(docx_paragraph("Top Candidates", bold=True, size=26))
    top_rows = sorted(summaries, key=lambda item: item.mean_snr_db, reverse=True)[:10]
    parts.append(
        docx_table(
            ["Distance cm", "Voltage V", "Runs", "Mean SNR dB", "Sensitivity mV", "Noise RMS mV", "Pass rate"],
            [
                [
                    f"{item.distance_cm:g}",
                    f"{item.input_voltage_v:g}",
                    str(item.runs),
                    f"{item.mean_snr_db:.2f}",
                    format_float(item.mean_sensitivity_mv, 2),
                    format_float(item.mean_noise_rms_mv, 3),
                    f"{item.pass_rate * 100:.0f}%",
                ]
                for item in top_rows
            ],
        )
    )

    parts.append(docx_paragraph("Best Setting By Distance", bold=True, size=26))
    parts.append(
        docx_table(
            ["Distance cm", "Best voltage V", "Best SNR dB", "Sensitivity mV", "Noise RMS mV"],
            [
                [
                    f"{item.distance_cm:g}",
                    f"{item.input_voltage_v:g}",
                    f"{item.mean_snr_db:.2f}",
                    format_float(item.mean_sensitivity_mv, 2),
                    format_float(item.mean_noise_rms_mv, 3),
                ]
                for item in best_by_distance(summaries)
            ],
        )
    )

    parts.append(docx_paragraph("Warnings And Follow-Up", bold=True, size=26))
    if warnings:
        for warning in warnings:
            parts.append(docx_paragraph(f"- {warning}"))
    else:
        parts.append(docx_paragraph("No analysis warnings."))
    if skipped:
        parts.append(docx_paragraph(f"Skipped rows: {len(skipped)}", bold=True))
        for skipped_row in skipped[:8]:
            parts.append(docx_paragraph(f"- {skipped_row}", size=20))

    parts.append(docx_paragraph("What Signal-To-Noise Ratio Means", bold=True, size=26))
    parts.append(
        docx_paragraph(
            "SNR compares the useful waveform signal to the random or repeatability noise. "
            "Here it is calculated as signal RMS divided by noise RMS, then converted to dB "
            "with 20 * log10(signal RMS / noise RMS). Higher dB is better."
        )
    )
    parts.append(
        docx_paragraph(
            "A 3 dB improvement is about 1.41x more signal-to-noise ratio. "
            "A 6 dB improvement is about 2x. A 10 dB improvement is about 3.16x."
        )
    )

    parts.append(docx_paragraph("What Was Done In This Tester", bold=True, size=26))
    explanation_lines = [
        "The tester records AIN0 waveform samples and AIN2 blade sync samples.",
        "The waveform is split into complete blade-sync cycles after the initial settling cycles are ignored.",
        "Stable cycles are selected using the existing stability check.",
        "Sensitivity is measured as the median peak-to-peak signal from the stable cycles, corrected for external gain.",
        "Noise is measured by aligning stable cycles by phase, subtracting the average cycle shape, and calculating the residual RMS.",
        "SNR uses the RMS of the average cycle shape as signal RMS and the residual RMS as noise RMS.",
        "Distance and input voltage are logged with every run so settings can be grouped and compared.",
    ]
    for line in explanation_lines:
        parts.append(docx_paragraph(f"- {line}"))

    parts.append(docx_paragraph("Other Useful Ways To Choose Distance And Voltage", bold=True, size=26))
    method_lines = [
        "Repeat the best candidates 3 to 5 times each and compare means plus variation, not just single runs.",
        "Randomize or reverse the run order to catch drift from warm-up, sensor heating, or alignment changes.",
        "Use a response-surface sweep: coarse voltage/distance grid first, then smaller steps near the best region.",
        "Use a practical tie rule: if settings are within 1 to 2 dB, prefer lower voltage, lower current, lower heat, and easier fixture distance.",
        "Track current, emitter temperature, and any clipping warnings so the best SNR setting is also safe and stable.",
        "Run a dark/no-emitter or blocked-path noise capture to separate electronics noise from optical/signal noise.",
        "Validate the chosen setting on multiple sensors, not only one sensor, before using it as a production setting.",
        "If production tolerance matters, use confidence intervals or an ANOVA-style comparison to decide whether differences are real.",
    ]
    for line in method_lines:
        parts.append(docx_paragraph(f"- {line}"))

    document_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        "<w:body>"
        + "".join(parts)
        + "<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/><w:pgMar w:top=\"720\" w:right=\"720\" w:bottom=\"720\" w:left=\"720\"/></w:sectPr>"
        "</w:body></w:document>"
    )

    content_types = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/word/document.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
        "</Types>"
    )
    rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"word/document.xml\"/>"
        "</Relationships>"
    )

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as docx_file:
        docx_file.writestr("[Content_Types].xml", content_types)
        docx_file.writestr("_rels/.rels", rels)
        docx_file.writestr("word/document.xml", document_xml)


def print_console_report(
    csv_path: Path,
    measurements: list[Measurement],
    skipped: list[str],
    summaries: list[GroupSummary],
    strict_best: GroupSummary | None,
    practical_best: GroupSummary | None,
    warnings: list[str],
    tie_db: float,
) -> None:
    print("406MCA SNR sweep analysis")
    print(f"CSV: {csv_path}")
    print(f"Valid rows: {len(measurements)}")
    print(f"Skipped rows: {len(skipped)}")
    print(f"Distance/voltage groups: {len(summaries)}")
    print()

    print(f"Strict best: {candidate_text(strict_best)}")
    print(f"Practical candidate within {tie_db:g} dB: {candidate_text(practical_best)}")
    print()

    print("Best by distance")
    print("distance_cm  best_voltage_v  mean_snr_db  sens_mV  noise_mV  runs")
    for summary in best_by_distance(summaries):
        print(
            f"{summary.distance_cm:11g}  {summary.input_voltage_v:14g}  "
            f"{summary.mean_snr_db:11.2f}  "
            f"{format_float(summary.mean_sensitivity_mv, 2):>7}  "
            f"{format_float(summary.mean_noise_rms_mv, 3):>8}  "
            f"{summary.runs:4d}"
        )
    print()

    print("Top 10 groups by mean SNR")
    print("rank  distance_cm  voltage_v  runs  mean_snr_db  std_snr  sens_mV  noise_mV")
    for rank, summary in enumerate(sorted(summaries, key=lambda item: item.mean_snr_db, reverse=True)[:10], start=1):
        print(
            f"{rank:4d}  {summary.distance_cm:11g}  {summary.input_voltage_v:9g}  "
            f"{summary.runs:4d}  {summary.mean_snr_db:11.2f}  "
            f"{format_float(summary.std_snr_db, 2):>7}  "
            f"{format_float(summary.mean_sensitivity_mv, 2):>7}  "
            f"{format_float(summary.mean_noise_rms_mv, 3):>8}"
        )
    print()

    if warnings:
        print("Warnings / next steps")
        for warning in warnings:
            print(f"- {warning}")
        print()

    print("Useful methods you may have missed")
    print("- Repeat top settings 3 to 5 times and compare variation, not just the highest single SNR.")
    print("- Randomize or reverse run order to catch warm-up, heating, drift, and alignment changes.")
    print("- Use a coarse grid first, then a finer voltage/distance sweep near the best region.")
    print("- Treat settings within 1 to 2 dB as practically tied unless safety or production constraints differ.")
    print("- Add current, temperature, and clipping/instability checks to avoid choosing an unsafe setting.")
    print("- Run a dark or blocked-path noise test to isolate electronics noise from optical/signal noise.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze 406MCA SNR distance/voltage sweep CSV results.")
    parser.add_argument(
        "csv_path",
        nargs="?",
        default=str(DEFAULT_RESULTS_CSV),
        help=f"CSV path. Default: {DEFAULT_RESULTS_CSV}",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for generated summary CSV and Word report. Default: current directory.",
    )
    parser.add_argument(
        "--tie-db",
        type=float,
        default=DEFAULT_TIE_DB,
        help=f"Near-tie threshold for practical lower-voltage candidate. Default: {DEFAULT_TIE_DB} dB.",
    )
    parser.add_argument(
        "--min-runs",
        type=int,
        default=DEFAULT_MIN_RUNS,
        help=f"Minimum recommended repeats per distance/voltage group. Default: {DEFAULT_MIN_RUNS}.",
    )
    parser.add_argument("--no-docx", action="store_true", help="Do not write the Word .docx report.")
    parser.add_argument("--no-summary-csv", action="store_true", help="Do not write the group summary CSV.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not csv_path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")

    measurements, skipped = read_measurements(csv_path)
    summaries = summarize_groups(measurements)
    strict_best = choose_strict_best(summaries)
    practical_best = choose_practical_best(summaries, tie_db=args.tie_db)
    warnings = generate_warnings(measurements, summaries, strict_best, min_runs=args.min_runs)

    print_console_report(
        csv_path,
        measurements,
        skipped,
        summaries,
        strict_best,
        practical_best,
        warnings,
        tie_db=args.tie_db,
    )

    if not args.no_summary_csv:
        summary_path = output_dir / "406MCA_SNR_Group_Summary.csv"
        write_summary_csv(summaries, summary_path)
        print(f"Wrote summary CSV: {summary_path}")

    if not args.no_docx:
        report_path = output_dir / "406MCA_SNR_Analysis_Report.docx"
        write_docx_report(
            report_path,
            csv_path,
            measurements,
            skipped,
            summaries,
            strict_best,
            practical_best,
            warnings,
            tie_db=args.tie_db,
        )
        print(f"Wrote Word report: {report_path}")


if __name__ == "__main__":
    main()
