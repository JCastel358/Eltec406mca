#!/usr/bin/env python3
"""Read-only summary and integrity checks for failure-calibration studies.

This utility intentionally uses only the Python standard library and never
creates, edits, or deletes study data.  ``summarize`` reads the hardware batch
CSVs; ``verify`` checks the complete batch-row → manifest → review chain and
every evidence artifact named by each ``run_manifest.json``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping, Sequence


DEFAULT_RESULTS_ROOT = (
    Path.home()
    / "Documents"
    / "Eltec_406MCA_Test_Results"
    / "v6_1_failure_calibration"
)
BATCH_CSV_GLOB = "406mca_failure_calibration_lot_*.csv"
AUDIT_BATCH_FIELDS = frozenset(
    {
        "offset_v",
        "sensitivity_mv",
        "polarity",
        "noise_rms_mv",
        "snr_db",
        "battery_v",
        "reference_calibrated_at",
        "reference_calibration_mv",
        "reference_lower_mv",
        "reference_upper_mv",
        "reference_check_mv",
        "reference_drift_pct",
        "stabilized",
        "stability_timeout",
        "stability_threshold_mv",
        "stability_required_deltas",
        "stabilization_cycle",
        "stabilization_seconds",
        "stability_window_max_delta_mv",
        "last_peak_delta_mv",
        "capture_cycles",
        "measurement_cycles",
        "stability_phase",
        "measurement_attempt",
        "measurement_failures",
        "active_stability_required_deltas",
        "active_measurement_cycles_required",
        "pwm_on_seconds",
        "data_source",
        "policy_offset_min_v",
        "policy_offset_max_v",
        "policy_sensitivity_min_mv",
        "policy_min_snr_ratio",
        "policy_stability_timeout_s",
        "policy_max_measurement_attempts",
        "offset_plausibility_override",
    }
)
REQUIRED_BATCH_FIELDS = frozenset(
    {
        "specimen_id",
        "is_synthetic",
        "app_verdict",
        "app_suggested_failure_mode_tag",
        "ground_truth_verdict",
        "ground_truth_failure_mode_tag",
        "review_classification",
    }
)
VERIFY_BATCH_FIELDS = REQUIRED_BATCH_FIELDS | AUDIT_BATCH_FIELDS | frozenset(
    {
        "calibration_schema_version",
        "calibration_app_version",
        "calibration_ruleset_id",
        "session_id",
        "run_id",
        "measurement_run_number",
        "batch_number",
        "sensor_id",
        "filter_setup",
        "fail_reasons",
        "app_suggested_failure_mode_reason",
        "ground_truth_failure_mode_reason",
        "verdict_assessment",
        "failure_reason_assessment",
        "ground_truth_basis",
        "review_confidence",
        "known_physical_root_cause",
        "review_notes",
        "reviewed_at_utc",
        "reviewer",
        "failure_mode_tag",
        "failure_mode_reason",
        "operator_comments",
        "run_manifest_path",
        "run_manifest_sha256",
        "review_record_path",
        "review_record_sha256",
        "related_run_ids",
    }
)
TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
NO_FAILURE_MODE = "NO_FAILURE_MODE"
UNSPECIFIED = "UNSPECIFIED"


class DatasetError(RuntimeError):
    """The requested dataset cannot be read without risking a false report."""


def _json_print(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _normal(value: object) -> str:
    return "" if value is None else str(value).strip()


def _is_synthetic(row: Mapping[str, object]) -> bool:
    explicit = _normal(row.get("is_synthetic")).casefold()
    source = _normal(row.get("data_source")).casefold()
    return explicit in TRUE_VALUES or source == "simulator" or source.startswith(
        "simulator_"
    )


def _under_simulator_dir(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return any(part.casefold() == "simulator" for part in relative.parts)


def _discover_batch_csvs(path: Path) -> tuple[list[Path], int]:
    if not path.exists():
        raise DatasetError(f"Study path does not exist: {path}")
    if path.is_file():
        if path.suffix.casefold() != ".csv":
            raise DatasetError(f"Summarize expects a CSV file or directory: {path}")
        if any(part.casefold() == "simulator" for part in path.parts):
            return [], 1
        return [path], 0
    if not path.is_dir():
        raise DatasetError(f"Study path is not a regular file or directory: {path}")

    included: list[Path] = []
    excluded = 0
    for candidate in sorted(path.rglob(BATCH_CSV_GLOB)):
        if _under_simulator_dir(candidate, path):
            excluded += 1
        elif candidate.is_file():
            included.append(candidate)
    return included, excluded


def _nested_counts(
    counts: Mapping[tuple[str, str], int]
) -> dict[str, dict[str, int]]:
    nested: dict[str, dict[str, int]] = defaultdict(dict)
    for (left, right), count in sorted(counts.items()):
        nested[left][right] = int(count)
    return dict(nested)


def summarize(path: Path) -> dict[str, object]:
    """Return sparse confusion counts for hardware calibration batch rows."""
    csv_paths, excluded_simulator_files = _discover_batch_csvs(path)
    rows = 0
    excluded_synthetic_rows = 0
    missing_specimen_rows = 0
    specimens: set[str] = set()
    verdict_confusion: Counter[tuple[str, str]] = Counter()
    review_classifications: Counter[str] = Counter()
    mode_confusion: Counter[tuple[str, str]] = Counter()

    for csv_path in csv_paths:
        try:
            handle = csv_path.open("r", newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise DatasetError(f"Could not open batch CSV {csv_path}: {exc}") from exc
        with handle:
            try:
                reader = csv.DictReader(handle)
                fields = set(reader.fieldnames or ())
                missing = sorted(REQUIRED_BATCH_FIELDS - fields)
                if missing:
                    raise DatasetError(
                        f"{csv_path} is not a failure-calibration batch CSV; "
                        "missing field(s): " + ", ".join(missing)
                    )
                for row in reader:
                    if _is_synthetic(row):
                        excluded_synthetic_rows += 1
                        continue

                    rows += 1
                    specimen = _normal(row.get("specimen_id"))
                    if specimen:
                        specimens.add(specimen.casefold())
                    else:
                        missing_specimen_rows += 1

                    app_verdict = _normal(row.get("app_verdict")).upper() or UNSPECIFIED
                    truth_verdict = (
                        _normal(row.get("ground_truth_verdict")).upper()
                        or UNSPECIFIED
                    )
                    verdict_confusion[(app_verdict, truth_verdict)] += 1

                    classification = (
                        _normal(row.get("review_classification")).upper()
                        or UNSPECIFIED
                    )
                    review_classifications[classification] += 1

                    app_mode = (
                        _normal(row.get("app_suggested_failure_mode_tag")).upper()
                        or NO_FAILURE_MODE
                    )
                    truth_mode = (
                        _normal(row.get("ground_truth_failure_mode_tag")).upper()
                        or UNSPECIFIED
                    )
                    mode_confusion[(app_mode, truth_mode)] += 1
            except (csv.Error, UnicodeError, OSError) as exc:
                raise DatasetError(f"Could not parse batch CSV {csv_path}: {exc}") from exc

    return {
        "path": str(path),
        "csv_files": len(csv_paths),
        "rows": rows,
        "unique_specimens": len(specimens),
        "rows_missing_specimen_id": missing_specimen_rows,
        "excluded_simulator_files": excluded_simulator_files,
        "excluded_synthetic_rows": excluded_synthetic_rows,
        "verdict_confusion": _nested_counts(verdict_confusion),
        "review_classifications": dict(sorted(review_classifications.items())),
        "app_mode_to_truth_mode": _nested_counts(mode_confusion),
    }


def _discover_manifests(path: Path) -> list[Path]:
    if not path.exists():
        raise DatasetError(f"Study path does not exist: {path}")
    if path.is_file():
        if path.name != "run_manifest.json":
            raise DatasetError(
                f"Verify expects a run_manifest.json file or study directory: {path}"
            )
        return [path]
    if not path.is_dir():
        raise DatasetError(f"Study path is not a regular file or directory: {path}")
    return sorted(candidate for candidate in path.rglob("run_manifest.json") if candidate.is_file())


def _expected_size(artifact: Mapping[str, object]) -> int | None:
    raw = artifact.get("bytes", artifact.get("size_bytes", artifact.get("size")))
    if isinstance(raw, bool):
        return None
    try:
        size = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _artifact_location(
    manifest_path: Path, artifact: Mapping[str, object]
) -> tuple[Path, str]:
    """Resolve an artifact within its run directory, including moved studies.

    Prefer the manifest's portable ``relative_path``.  Older schema-v1
    manifests may contain only an absolute ``path``; those artifacts are
    siblings of ``run_manifest.json`` and are safely rebased by basename.
    """
    relative_text = _normal(artifact.get("relative_path"))
    listed_path = _normal(artifact.get("path"))
    if relative_text:
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Artifact relative_path must remain inside its run directory")
        return manifest_path.parent / relative, listed_path or relative_text
    if not listed_path:
        raise ValueError("Artifact entry has neither relative_path nor path")
    listed = Path(listed_path)
    if listed.is_absolute():
        return manifest_path.parent / listed.name, listed_path
    return manifest_path.parent / listed, listed_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _problem(
    *, manifest: Path, listed_path: str = "", resolved_path: Path | None = None, error: str
) -> dict[str, str]:
    payload = {"manifest": str(manifest), "error": error}
    if listed_path:
        payload["listed_path"] = listed_path
    if resolved_path is not None:
        payload["resolved_path"] = str(resolved_path)
    return payload


def _mode(tag: object, reason: object) -> str:
    tag_text = _normal(tag)
    reason_text = _normal(reason)
    if not tag_text and not reason_text:
        return ""
    return f"{tag_text} - {reason_text}"


def _id_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [_normal(item) for item in value if _normal(item)]
    return [item.strip() for item in _normal(value).split(";") if item.strip()]


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return _normal(value).casefold() in TRUE_VALUES


def _normalized_number(value: object, decimals: int) -> str:
    """Match a JSON number to the precision used when its CSV cell was saved."""
    text = _normal(value)
    if not text:
        return ""
    if isinstance(value, bool):
        return f"<invalid number: {text}>"
    try:
        number = float(text)
    except (TypeError, ValueError):
        return f"<invalid number: {text}>"
    if number != number or number in (float("inf"), float("-inf")):
        return f"<invalid number: {text}>"
    return f"{number:.{decimals}f}"


def _normalized_integer(value: object) -> str:
    text = _normal(value)
    if not text:
        return ""
    if isinstance(value, bool):
        return f"<invalid integer: {text}>"
    try:
        number = float(text)
    except (TypeError, ValueError):
        return f"<invalid integer: {text}>"
    if number != number or number in (float("inf"), float("-inf")):
        return f"<invalid integer: {text}>"
    if not number.is_integer():
        return f"<invalid integer: {text}>"
    return str(int(number))


def _normalized_bool(value: object) -> bool | str:
    if isinstance(value, bool):
        return value
    text = _normal(value).casefold()
    if not text:
        return ""
    if text in TRUE_VALUES:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return f"<invalid boolean: {_normal(value)}>"


def _valid_sha256(value: object) -> str | None:
    text = _normal(value).casefold()
    if len(text) != 64:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


def _compare_sources(
    mismatch: list[dict[str, object]],
    *,
    manifest_path: Path,
    run_id: str,
    field: str,
    values: Mapping[str, object],
) -> None:
    compared = list(values.values())
    if compared and any(value != compared[0] for value in compared[1:]):
        mismatch.append(
            {
                "kind": "field_mismatch",
                "manifest": str(manifest_path),
                "run_id": run_id,
                "field": field,
                "values": dict(values),
            }
        )


def _compare_manifest_audit_fields(
    mismatch: list[dict[str, object]],
    unreadable: list[dict[str, object]],
    *,
    manifest_path: Path,
    run_id: str,
    row: Mapping[str, object],
    manifest: Mapping[str, object],
    prediction: Mapping[str, object],
) -> None:
    """Cross-check verdict inputs and recorded measurement-policy telemetry."""
    sections: dict[str, Mapping[str, object]] = {"app_prediction": prediction}
    for section_name in ("metrics", "reference", "stability_report", "policy"):
        value = manifest.get(section_name)
        if not isinstance(value, dict):
            unreadable.append(
                {
                    "kind": "manifest_audit_section",
                    "manifest": str(manifest_path),
                    "run_id": run_id,
                    "field": section_name,
                    "error": f"Manifest {section_name} is not an object",
                }
            )
            sections[section_name] = {}
        else:
            sections[section_name] = value

    numeric_fields = (
        ("offset_v", "app_prediction", "offset_v", 6),
        ("sensitivity_mv", "app_prediction", "sensitivity_mv", 6),
        ("noise_rms_mv", "metrics", "noise_rms_mv", 4),
        ("snr_db", "metrics", "signal_to_noise_db", 2),
        ("battery_v", "root", "battery_v", 3),
        ("reference_calibration_mv", "reference", "baseline_mv", 4),
        ("reference_lower_mv", "reference", "lower_mv", 4),
        ("reference_upper_mv", "reference", "upper_mv", 4),
        ("reference_check_mv", "reference", "check_mv", 4),
        ("reference_drift_pct", "reference", "drift_percent", 3),
        ("stability_threshold_mv", "stability_report", "threshold_mv", 6),
        ("stabilization_seconds", "stability_report", "stabilization_seconds", 3),
        (
            "stability_window_max_delta_mv",
            "stability_report",
            "confirming_max_delta_mv",
            6,
        ),
        ("last_peak_delta_mv", "stability_report", "last_peak_delta_mv", 6),
        ("pwm_on_seconds", "stability_report", "pwm_on_seconds", 3),
        ("policy_offset_min_v", "policy", "offset_min_v", 6),
        ("policy_offset_max_v", "policy", "offset_max_v", 6),
        ("policy_sensitivity_min_mv", "policy", "sensitivity_min_mv", 6),
        (
            "policy_min_snr_ratio",
            "policy",
            "min_signal_to_noise_ratio",
            6,
        ),
        ("policy_stability_timeout_s", "policy", "stability_timeout_s", 3),
    )
    for csv_field, section_name, manifest_field, decimals in numeric_fields:
        source = manifest if section_name == "root" else sections[section_name]
        _compare_sources(
            mismatch,
            manifest_path=manifest_path,
            run_id=run_id,
            field=csv_field,
            values={
                "csv": _normalized_number(row.get(csv_field), decimals),
                "manifest": _normalized_number(source.get(manifest_field), decimals),
            },
        )

    integer_fields = (
        ("stability_required_deltas", "stability_report", "required_deltas"),
        ("stabilization_cycle", "stability_report", "stabilization_cycle"),
        ("capture_cycles", "stability_report", "capture_cycles"),
        ("measurement_cycles", "stability_report", "measurement_cycles"),
        ("measurement_attempt", "stability_report", "measurement_attempt"),
        ("measurement_failures", "stability_report", "measurement_failures"),
        (
            "active_stability_required_deltas",
            "stability_report",
            "active_required_deltas",
        ),
        (
            "active_measurement_cycles_required",
            "stability_report",
            "measurement_cycles_required",
        ),
        (
            "policy_max_measurement_attempts",
            "policy",
            "max_measurement_attempts",
        ),
    )
    for csv_field, section_name, manifest_field in integer_fields:
        source = sections[section_name]
        _compare_sources(
            mismatch,
            manifest_path=manifest_path,
            run_id=run_id,
            field=csv_field,
            values={
                "csv": _normalized_integer(row.get(csv_field)),
                "manifest": _normalized_integer(source.get(manifest_field)),
            },
        )

    boolean_fields = (
        ("stabilized", "stability_report", "stabilized"),
        ("stability_timeout", "stability_report", "timed_out"),
        ("offset_plausibility_override", "root", "offset_plausibility_override"),
    )
    for csv_field, section_name, manifest_field in boolean_fields:
        source = manifest if section_name == "root" else sections[section_name]
        _compare_sources(
            mismatch,
            manifest_path=manifest_path,
            run_id=run_id,
            field=csv_field,
            values={
                "csv": _normalized_bool(row.get(csv_field)),
                "manifest": _normalized_bool(source.get(manifest_field)),
            },
        )

    text_fields = (
        ("polarity", "app_prediction", "polarity"),
        ("reference_calibrated_at", "reference", "calibrated_at"),
        ("stability_phase", "stability_report", "phase"),
        ("data_source", "stability_report", "data_source"),
    )
    for csv_field, section_name, manifest_field in text_fields:
        source = sections[section_name]
        _compare_sources(
            mismatch,
            manifest_path=manifest_path,
            run_id=run_id,
            field=csv_field,
            values={
                "csv": _normal(row.get(csv_field)),
                "manifest": _normal(source.get(manifest_field)),
            },
        )


def _portable_csv_location(
    *,
    study_root: Path,
    csv_path: Path,
    listed_path: str,
    run_dir: Path,
    expected_name: str,
) -> Path:
    """Resolve a CSV link after the complete study folder has been copied."""
    if not listed_path:
        raise ValueError("path is blank")
    listed = Path(listed_path)
    root_resolved = study_root.resolve()
    if not listed.is_absolute():
        if ".." in listed.parts:
            raise ValueError("relative path escapes the study directory")
        candidate = study_root / listed
    else:
        folded = [part.casefold() for part in listed.parts]
        marker = "v6_1_failure_calibration"
        if marker in folded:
            marker_index = len(folded) - 1 - folded[::-1].index(marker)
            relative_parts = listed.parts[marker_index + 1 :]
            candidate = study_root.joinpath(*relative_parts)
        else:
            # Legacy absolute-only links can still be checked because both
            # canonical JSON records are siblings in the selected run folder.
            candidate = run_dir / expected_name
    candidate_resolved = candidate.resolve(strict=False)
    if not candidate_resolved.is_relative_to(root_resolved):
        raise ValueError(
            f"resolved path escapes the study directory containing {csv_path.name}"
        )
    return candidate


def _read_hardware_batch_rows(
    csv_paths: Sequence[Path],
    unreadable: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int]:
    records: list[dict[str, object]] = []
    excluded_synthetic_rows = 0
    for csv_path in csv_paths:
        try:
            handle = csv_path.open("r", newline="", encoding="utf-8-sig")
        except OSError as exc:
            unreadable.append(
                {"batch_csv": str(csv_path), "error": f"Could not open CSV: {exc}"}
            )
            continue
        with handle:
            try:
                reader = csv.DictReader(handle)
                fields = set(reader.fieldnames or ())
                missing_fields = sorted(VERIFY_BATCH_FIELDS - fields)
                if missing_fields:
                    unreadable.append(
                        {
                            "batch_csv": str(csv_path),
                            "error": (
                                "Calibration batch schema is missing field(s): "
                                + ", ".join(missing_fields)
                            ),
                        }
                    )
                    continue
                for row in reader:
                    if _is_synthetic(row):
                        excluded_synthetic_rows += 1
                        continue
                    records.append(
                        {
                            "batch_csv": csv_path,
                            "line": reader.line_num,
                            "row": dict(row),
                        }
                    )
            except (csv.Error, UnicodeError, OSError) as exc:
                unreadable.append(
                    {
                        "batch_csv": str(csv_path),
                        "error": f"Could not parse CSV: {exc}",
                    }
                )
    return records, excluded_synthetic_rows


def _verify_recorded_hash(
    *,
    file_path: Path,
    expected: object,
    record_kind: str,
    manifest_path: Path,
    run_id: str,
    mismatch: list[dict[str, object]],
    unreadable: list[dict[str, object]],
) -> bool:
    expected_sha = _valid_sha256(expected)
    if expected_sha is None:
        unreadable.append(
            {
                "kind": f"{record_kind}_hash",
                "manifest": str(manifest_path),
                "run_id": run_id,
                "path": str(file_path),
                "error": f"CSV {record_kind} SHA-256 is blank or invalid",
            }
        )
        return False
    try:
        actual_sha = _sha256(file_path)
    except OSError as exc:
        unreadable.append(
            {
                "kind": f"{record_kind}_hash",
                "manifest": str(manifest_path),
                "run_id": run_id,
                "path": str(file_path),
                "error": f"Could not hash {record_kind}: {exc}",
            }
        )
        return False
    if actual_sha != expected_sha:
        mismatch.append(
            {
                "kind": f"{record_kind}_hash",
                "manifest": str(manifest_path),
                "run_id": run_id,
                "path": str(file_path),
                "expected_sha256": expected_sha,
                "actual_sha256": actual_sha,
            }
        )
        return False
    return True


def _verify_manifest_artifacts(
    *,
    manifest_path: Path,
    payload: Mapping[str, object],
    missing: list[dict[str, object]],
    mismatch: list[dict[str, object]],
    unreadable: list[dict[str, object]],
) -> tuple[int, int]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        unreadable.append(
            _problem(manifest=manifest_path, error="Manifest artifacts is not a list")
        )
        return 0, 0
    listed_count = 0
    ok_count = 0
    for index, artifact in enumerate(artifacts):
        listed_count += 1
        if not isinstance(artifact, dict):
            unreadable.append(
                _problem(
                    manifest=manifest_path,
                    error=f"Artifact entry {index} is not a JSON object",
                )
            )
            continue
        listed_path = _normal(artifact.get("path")) or _normal(
            artifact.get("relative_path")
        )
        expected_size = _expected_size(artifact)
        expected_sha = _valid_sha256(artifact.get("sha256"))
        if not listed_path or expected_size is None or expected_sha is None:
            unreadable.append(
                _problem(
                    manifest=manifest_path,
                    listed_path=listed_path,
                    error=(
                        f"Artifact entry {index} lacks a valid path, byte size, "
                        "or SHA-256 digest"
                    ),
                )
            )
            continue
        try:
            artifact_path, listed_path = _artifact_location(manifest_path, artifact)
        except ValueError as exc:
            unreadable.append(
                _problem(
                    manifest=manifest_path,
                    listed_path=listed_path,
                    error=f"Artifact entry {index} has an invalid location: {exc}",
                )
            )
            continue
        if not artifact_path.exists():
            missing.append(
                _problem(
                    manifest=manifest_path,
                    listed_path=listed_path,
                    resolved_path=artifact_path,
                    error="Artifact is missing",
                )
            )
            continue
        if not artifact_path.is_file():
            unreadable.append(
                _problem(
                    manifest=manifest_path,
                    listed_path=listed_path,
                    resolved_path=artifact_path,
                    error="Artifact is not a regular file",
                )
            )
            continue
        try:
            actual_size = artifact_path.stat().st_size
            actual_sha = _sha256(artifact_path)
        except OSError as exc:
            unreadable.append(
                _problem(
                    manifest=manifest_path,
                    listed_path=listed_path,
                    resolved_path=artifact_path,
                    error=f"Could not read artifact: {exc}",
                )
            )
            continue
        differences: dict[str, object] = {}
        if actual_size != expected_size:
            differences["expected_bytes"] = expected_size
            differences["actual_bytes"] = actual_size
        if actual_sha != expected_sha:
            differences["expected_sha256"] = expected_sha
            differences["actual_sha256"] = actual_sha
        if differences:
            mismatch.append(
                {
                    "kind": "evidence_artifact",
                    "manifest": str(manifest_path),
                    "listed_path": listed_path,
                    "resolved_path": str(artifact_path),
                    **differences,
                }
            )
        else:
            ok_count += 1
    return listed_count, ok_count


def _read_review_json(
    review_path: Path,
    *,
    manifest_path: Path,
    run_id: str,
    unreadable: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]] | None:
    try:
        payload = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        unreadable.append(
            {
                "kind": "review_json",
                "manifest": str(manifest_path),
                "run_id": run_id,
                "path": str(review_path),
                "error": f"Could not read review JSON: {exc}",
            }
        )
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("review"), dict):
        unreadable.append(
            {
                "kind": "review_json",
                "manifest": str(manifest_path),
                "run_id": run_id,
                "path": str(review_path),
                "error": "Review JSON root/review entry is not an object",
            }
        )
        return None
    review = payload["review"]
    assert isinstance(review, dict)
    required = (
        "specimen_id",
        "app_verdict",
        "ground_truth_verdict",
        "ground_truth_failure_mode",
        "verdict_assessment",
        "failure_reason_assessment",
        "review_classification",
        "ground_truth_basis",
        "review_confidence",
        "review_notes",
        "reviewed_at_utc",
        "reviewer",
    )
    blank = [field for field in required if not _normal(review.get(field))]
    if blank:
        unreadable.append(
            {
                "kind": "review_json",
                "manifest": str(manifest_path),
                "run_id": run_id,
                "path": str(review_path),
                "error": "Review JSON has blank required field(s): " + ", ".join(blank),
            }
        )
        return None
    return payload, review


def verify(path: Path) -> dict[str, object]:
    """Verify hardware evidence plus finalized ground-truth record linkage."""
    discovered_manifests = _discover_manifests(path)
    if path.is_dir():
        simulator_manifests = [
            item for item in discovered_manifests if _under_simulator_dir(item, path)
        ]
        manifest_paths = [
            item for item in discovered_manifests if not _under_simulator_dir(item, path)
        ]
    else:
        simulator_manifests = []
        manifest_paths = discovered_manifests
    missing: list[dict[str, object]] = []
    mismatch: list[dict[str, object]] = []
    unreadable: list[dict[str, object]] = []
    artifacts_listed = 0
    artifacts_ok = 0
    manifests_read = 0
    synthetic_manifests = 0
    manifest_records: list[dict[str, object]] = []
    chain_checked = path.is_dir()
    batch_csv_paths: list[Path] = []
    batch_records: list[dict[str, object]] = []
    excluded_simulator_csvs = 0
    excluded_synthetic_rows = 0

    if not manifest_paths:
        unreadable.append(
            {
                "manifest": "",
                "error": f"No run_manifest.json files found under {path}",
            }
        )

    if chain_checked:
        try:
            batch_csv_paths, excluded_simulator_csvs = _discover_batch_csvs(path)
        except DatasetError as exc:
            unreadable.append({"batch_csv": "", "error": str(exc)})
        batch_records, excluded_synthetic_rows = _read_hardware_batch_rows(
            batch_csv_paths, unreadable
        )
        if not batch_csv_paths:
            unreadable.append(
                {
                    "batch_csv": "",
                    "error": f"No hardware {BATCH_CSV_GLOB} files found under {path}",
                }
            )

    for manifest_path in manifest_paths:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            unreadable.append(
                _problem(manifest=manifest_path, error=f"Could not read manifest: {exc}")
            )
            continue
        if not isinstance(payload, dict):
            unreadable.append(
                _problem(manifest=manifest_path, error="Manifest root is not a JSON object")
            )
            continue
        if _bool_value(payload.get("is_synthetic")):
            synthetic_manifests += 1
            continue
        run_id = _normal(payload.get("run_id"))
        specimen_id = _normal(payload.get("specimen_id"))
        prediction = payload.get("app_prediction")
        if not run_id or not specimen_id or not isinstance(prediction, dict):
            unreadable.append(
                _problem(
                    manifest=manifest_path,
                    error="Manifest lacks a run ID, specimen ID, or app prediction object",
                )
            )
            continue

        manifests_read += 1
        manifest_records.append(
            {
                "path": manifest_path,
                "payload": payload,
                "run_id": run_id,
            }
        )
        listed, ok = _verify_manifest_artifacts(
            manifest_path=manifest_path,
            payload=payload,
            missing=missing,
            mismatch=mismatch,
            unreadable=unreadable,
        )
        artifacts_listed += listed
        artifacts_ok += ok

    if chain_checked and not manifest_records and manifest_paths:
        unreadable.append(
            {
                "manifest": "",
                "error": f"No non-synthetic run manifests found under {path}",
            }
        )

    linked_records_ok = 0
    reviews_read = 0
    if chain_checked:
        rows_by_run: dict[str, list[dict[str, object]]] = defaultdict(list)
        for record in batch_records:
            row = record["row"]
            assert isinstance(row, dict)
            run_id = _normal(row.get("run_id"))
            required_values = (
                "calibration_schema_version",
                "calibration_app_version",
                "calibration_ruleset_id",
                "session_id",
                "run_id",
                "measurement_run_number",
                "batch_number",
                "sensor_id",
                "specimen_id",
                "app_verdict",
                "ground_truth_verdict",
                "ground_truth_failure_mode_tag",
                "ground_truth_failure_mode_reason",
                "verdict_assessment",
                "failure_reason_assessment",
                "review_classification",
                "ground_truth_basis",
                "review_confidence",
                "review_notes",
                "reviewed_at_utc",
                "reviewer",
                "run_manifest_path",
                "run_manifest_sha256",
                "review_record_path",
                "review_record_sha256",
            )
            blanks = [field for field in required_values if not _normal(row.get(field))]
            if blanks:
                unreadable.append(
                    {
                        "kind": "batch_row",
                        "batch_csv": str(record["batch_csv"]),
                        "line": record["line"],
                        "run_id": run_id,
                        "error": "Batch row has blank required field(s): " + ", ".join(blanks),
                    }
                )
            if run_id:
                rows_by_run[run_id].append(record)

        manifests_by_run: dict[str, list[dict[str, object]]] = defaultdict(list)
        for record in manifest_records:
            manifests_by_run[str(record["run_id"])].append(record)

        for run_id, records in sorted(rows_by_run.items()):
            if len(records) > 1:
                mismatch.append(
                    {
                        "kind": "duplicate_batch_run_id",
                        "run_id": run_id,
                        "rows": [
                            {
                                "batch_csv": str(item["batch_csv"]),
                                "line": item["line"],
                            }
                            for item in records
                        ],
                    }
                )
        for run_id, records in sorted(manifests_by_run.items()):
            if len(records) > 1:
                mismatch.append(
                    {
                        "kind": "duplicate_manifest_run_id",
                        "run_id": run_id,
                        "manifests": [str(item["path"]) for item in records],
                    }
                )

        for run_id, records in sorted(manifests_by_run.items()):
            for manifest_record in records:
                manifest_path = manifest_record["path"]
                manifest = manifest_record["payload"]
                assert isinstance(manifest_path, Path)
                assert isinstance(manifest, dict)
                problem_count = len(missing) + len(mismatch) + len(unreadable)
                matching_rows = rows_by_run.get(run_id, [])
                if not matching_rows:
                    missing.append(
                        {
                            "kind": "batch_row",
                            "manifest": str(manifest_path),
                            "run_id": run_id,
                            "error": "No non-synthetic batch row has this manifest run ID",
                        }
                    )
                    row_record = None
                elif len(matching_rows) == 1:
                    row_record = matching_rows[0]
                else:
                    row_record = None

                review_path = manifest_path.parent / "review.json"
                review_data = None
                if not review_path.exists():
                    missing.append(
                        {
                            "kind": "review_json",
                            "manifest": str(manifest_path),
                            "run_id": run_id,
                            "path": str(review_path),
                            "error": "Sibling review.json is missing",
                        }
                    )
                elif not review_path.is_file():
                    unreadable.append(
                        {
                            "kind": "review_json",
                            "manifest": str(manifest_path),
                            "run_id": run_id,
                            "path": str(review_path),
                            "error": "Sibling review.json is not a regular file",
                        }
                    )
                else:
                    review_data = _read_review_json(
                        review_path,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        unreadable=unreadable,
                    )
                    if review_data is not None:
                        reviews_read += 1

                if row_record is None:
                    continue
                row = row_record["row"]
                csv_path = row_record["batch_csv"]
                assert isinstance(row, dict)
                assert isinstance(csv_path, Path)

                for field, expected_name, canonical in (
                    ("run_manifest_path", "run_manifest.json", manifest_path),
                    ("review_record_path", "review.json", review_path),
                ):
                    listed_path = _normal(row.get(field))
                    try:
                        linked_path = _portable_csv_location(
                            study_root=path,
                            csv_path=csv_path,
                            listed_path=listed_path,
                            run_dir=manifest_path.parent,
                            expected_name=expected_name,
                        )
                    except ValueError as exc:
                        unreadable.append(
                            {
                                "kind": field,
                                "manifest": str(manifest_path),
                                "run_id": run_id,
                                "batch_csv": str(csv_path),
                                "error": f"CSV link is invalid: {exc}",
                            }
                        )
                        continue
                    if not linked_path.exists():
                        missing.append(
                            {
                                "kind": field,
                                "manifest": str(manifest_path),
                                "run_id": run_id,
                                "batch_csv": str(csv_path),
                                "listed_path": listed_path,
                                "resolved_path": str(linked_path),
                                "error": "CSV-linked canonical JSON file is missing",
                            }
                        )
                    if linked_path.resolve(strict=False) != canonical.resolve(strict=False):
                        mismatch.append(
                            {
                                "kind": field,
                                "manifest": str(manifest_path),
                                "run_id": run_id,
                                "batch_csv": str(csv_path),
                                "listed_path": listed_path,
                                "resolved_path": str(linked_path),
                                "expected_path": str(canonical),
                            }
                        )

                _verify_recorded_hash(
                    file_path=manifest_path,
                    expected=row.get("run_manifest_sha256"),
                    record_kind="run_manifest",
                    manifest_path=manifest_path,
                    run_id=run_id,
                    mismatch=mismatch,
                    unreadable=unreadable,
                )
                if review_path.is_file():
                    _verify_recorded_hash(
                        file_path=review_path,
                        expected=row.get("review_record_sha256"),
                        record_kind="review_record",
                        manifest_path=manifest_path,
                        run_id=run_id,
                        mismatch=mismatch,
                        unreadable=unreadable,
                    )

                prediction = manifest.get("app_prediction")
                assert isinstance(prediction, dict)
                _compare_sources(
                    mismatch,
                    manifest_path=manifest_path,
                    run_id=run_id,
                    field="calibration_schema_version",
                    values={
                        "csv": _normal(row.get("calibration_schema_version")),
                        "manifest": _normal(manifest.get("calibration_schema_version")),
                    },
                )
                _compare_sources(
                    mismatch,
                    manifest_path=manifest_path,
                    run_id=run_id,
                    field="calibration_app_version",
                    values={
                        "csv": _normal(row.get("calibration_app_version")),
                        "manifest": _normal(manifest.get("calibration_app_version")),
                    },
                )
                _compare_sources(
                    mismatch,
                    manifest_path=manifest_path,
                    run_id=run_id,
                    field="ruleset_id",
                    values={
                        "csv": _normal(row.get("calibration_ruleset_id")),
                        "manifest": _normal(manifest.get("ruleset_id")),
                    },
                )
                manifest_csv_fields = {
                    "session_id": manifest.get("session_id"),
                    "run_id": manifest.get("run_id"),
                    "measurement_run_number": manifest.get("measurement_run_number"),
                    "batch_number": manifest.get("batch_number"),
                    "sensor_id": manifest.get("sensor_id"),
                    "specimen_id": manifest.get("specimen_id"),
                    "filter_setup": manifest.get("filter_setup"),
                }
                for field, manifest_value in manifest_csv_fields.items():
                    _compare_sources(
                        mismatch,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        field=field,
                        values={
                            "csv": _normal(row.get(field)),
                            "manifest": _normal(manifest_value),
                        },
                    )
                _compare_sources(
                    mismatch,
                    manifest_path=manifest_path,
                    run_id=run_id,
                    field="is_synthetic",
                    values={
                        "csv": _bool_value(row.get("is_synthetic")),
                        "manifest": _bool_value(manifest.get("is_synthetic")),
                    },
                )
                csv_app_mode = _mode(
                    row.get("app_suggested_failure_mode_tag"),
                    row.get("app_suggested_failure_mode_reason"),
                )
                _compare_sources(
                    mismatch,
                    manifest_path=manifest_path,
                    run_id=run_id,
                    field="app_verdict",
                    values={
                        "csv": _normal(row.get("app_verdict")),
                        "manifest": _normal(prediction.get("verdict")),
                    },
                )
                _compare_sources(
                    mismatch,
                    manifest_path=manifest_path,
                    run_id=run_id,
                    field="app_suggested_failure_mode",
                    values={
                        "csv": csv_app_mode,
                        "manifest": _normal(prediction.get("suggested_failure_mode")),
                    },
                )
                manifest_fail_reasons = prediction.get("fail_reasons")
                if not isinstance(manifest_fail_reasons, list):
                    unreadable.append(
                        {
                            "kind": "app_prediction",
                            "manifest": str(manifest_path),
                            "run_id": run_id,
                            "error": "Manifest app_prediction.fail_reasons is not a list",
                        }
                    )
                else:
                    _compare_sources(
                        mismatch,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        field="app_fail_reasons",
                        values={
                            "csv": _normal(row.get("fail_reasons")),
                            "manifest": "; ".join(
                                _normal(item) for item in manifest_fail_reasons
                            ),
                        },
                    )

                _compare_manifest_audit_fields(
                    mismatch,
                    unreadable,
                    manifest_path=manifest_path,
                    run_id=run_id,
                    row=row,
                    manifest=manifest,
                    prediction=prediction,
                )

                if review_data is not None:
                    review_payload, review = review_data
                    _compare_sources(
                        mismatch,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        field="review_selected_run_id",
                        values={
                            "csv": _normal(row.get("run_id")),
                            "manifest": _normal(manifest.get("run_id")),
                            "review": _normal(review_payload.get("selected_run_id")),
                        },
                    )
                    for field, review_key in (
                        ("batch_number", "batch_number"),
                        ("sensor_id", "sensor_id"),
                    ):
                        _compare_sources(
                            mismatch,
                            manifest_path=manifest_path,
                            run_id=run_id,
                            field=f"review_{field}",
                            values={
                                "csv": _normal(row.get(field)),
                                "manifest": _normal(manifest.get(field)),
                                "review": _normal(review_payload.get(review_key)),
                            },
                        )
                    _compare_sources(
                        mismatch,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        field="review_specimen_id",
                        values={
                            "csv": _normal(row.get("specimen_id")),
                            "manifest": _normal(manifest.get("specimen_id")),
                            "review": _normal(review.get("specimen_id")),
                        },
                    )
                    _compare_sources(
                        mismatch,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        field="review_app_verdict",
                        values={
                            "csv": _normal(row.get("app_verdict")),
                            "manifest": _normal(prediction.get("verdict")),
                            "review": _normal(review.get("app_verdict")),
                        },
                    )
                    _compare_sources(
                        mismatch,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        field="review_app_suggested_failure_mode",
                        values={
                            "csv": csv_app_mode,
                            "manifest": _normal(prediction.get("suggested_failure_mode")),
                            "review": _normal(review.get("app_suggested_failure_mode")),
                        },
                    )
                    csv_truth_mode = _mode(
                        row.get("ground_truth_failure_mode_tag"),
                        row.get("ground_truth_failure_mode_reason"),
                    )
                    review_fields = {
                        "ground_truth_verdict": review.get("ground_truth_verdict"),
                        "ground_truth_failure_mode": review.get("ground_truth_failure_mode"),
                        "verdict_assessment": review.get("verdict_assessment"),
                        "failure_reason_assessment": review.get(
                            "failure_reason_assessment"
                        ),
                        "review_classification": review.get("review_classification"),
                        "ground_truth_basis": review.get("ground_truth_basis"),
                        "review_confidence": review.get("review_confidence"),
                        "known_physical_root_cause": review.get(
                            "known_physical_root_cause"
                        ),
                        "review_notes": review.get("review_notes"),
                        "reviewed_at_utc": review.get("reviewed_at_utc"),
                        "reviewer": review.get("reviewer"),
                    }
                    for field, review_value in review_fields.items():
                        csv_value = (
                            csv_truth_mode
                            if field == "ground_truth_failure_mode"
                            else _normal(row.get(field))
                        )
                        _compare_sources(
                            mismatch,
                            manifest_path=manifest_path,
                            run_id=run_id,
                            field=field,
                            values={
                                "csv": csv_value,
                                "review": _normal(review_value),
                            },
                        )
                    _compare_sources(
                        mismatch,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        field="related_run_ids",
                        values={
                            "csv": _id_list(row.get("related_run_ids")),
                            "review": _id_list(review_payload.get("related_run_ids")),
                        },
                    )
                    _compare_sources(
                        mismatch,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        field="production_failure_mode",
                        values={
                            "csv": _mode(
                                row.get("failure_mode_tag"),
                                row.get("failure_mode_reason"),
                            ),
                            "review": _normal(
                                review_payload.get("production_failure_mode")
                            ),
                        },
                    )
                    _compare_sources(
                        mismatch,
                        manifest_path=manifest_path,
                        run_id=run_id,
                        field="operator_comment",
                        values={
                            "csv": _normal(row.get("operator_comments")),
                            "review": _normal(review_payload.get("operator_comment")),
                        },
                    )
                    for csv_field, review_key in (
                        ("calibration_schema_version", "calibration_schema_version"),
                        ("calibration_app_version", "calibration_app_version"),
                        ("calibration_ruleset_id", "ruleset_id"),
                    ):
                        _compare_sources(
                            mismatch,
                            manifest_path=manifest_path,
                            run_id=run_id,
                            field=f"review_{review_key}",
                            values={
                                "csv": _normal(row.get(csv_field)),
                                "manifest": _normal(
                                    manifest.get(
                                        "ruleset_id"
                                        if review_key == "ruleset_id"
                                        else review_key
                                    )
                                ),
                                "review": _normal(review_payload.get(review_key)),
                            },
                        )

                if len(missing) + len(mismatch) + len(unreadable) == problem_count:
                    linked_records_ok += 1

        for run_id, records in sorted(rows_by_run.items()):
            if run_id not in manifests_by_run:
                for record in records:
                    missing.append(
                        {
                            "kind": "run_manifest",
                            "run_id": run_id,
                            "batch_csv": str(record["batch_csv"]),
                            "line": record["line"],
                            "error": "Batch row has no non-synthetic run manifest",
                        }
                    )

    valid = not missing and not mismatch and not unreadable
    return {
        "path": str(path),
        "valid": valid,
        "manifests": len(manifest_paths),
        "manifests_read": manifests_read,
        "hardware_manifests": len(manifest_records),
        "excluded_simulator_manifests": len(simulator_manifests),
        "excluded_synthetic_manifests": synthetic_manifests,
        "artifacts_listed": artifacts_listed,
        "artifacts_ok": artifacts_ok,
        "finalization_chain_checked": chain_checked,
        "batch_csv_files": len(batch_csv_paths),
        "hardware_batch_rows": len(batch_records),
        "excluded_simulator_csv_files": excluded_simulator_csvs,
        "excluded_synthetic_batch_rows": excluded_synthetic_rows,
        "reviews_read": reviews_read,
        "linked_records_ok": linked_records_ok,
        "missing": missing,
        "mismatch": mismatch,
        "unreadable": unreadable,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize or verify an Eltec 406MCA failure-calibration study "
            "without modifying it."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    summarize_parser = subparsers.add_parser(
        "summarize", help="print hardware verdict/review confusion counts as JSON"
    )
    summarize_parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_RESULTS_ROOT,
        help=f"batch CSV or study directory (default: {DEFAULT_RESULTS_ROOT})",
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify finalized batch/manifest/review links and evidence hashes",
    )
    verify_parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_RESULTS_ROOT,
        help=(
            "run_manifest.json (artifacts only) or complete study directory "
            f"(default: {DEFAULT_RESULTS_ROOT})"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "summarize":
            _json_print(summarize(args.path))
            return 0
        report = verify(args.path)
        _json_print(report)
        return 0 if report["valid"] else 1
    except DatasetError as exc:
        if args.command == "verify":
            _json_print(
                {
                    "path": str(args.path),
                    "valid": False,
                    "manifests": 0,
                    "manifests_read": 0,
                    "artifacts_listed": 0,
                    "artifacts_ok": 0,
                    "missing": [],
                    "mismatch": [],
                    "unreadable": [{"manifest": "", "error": str(exc)}],
                }
            )
            return 1
        _json_print({"path": str(args.path), "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
