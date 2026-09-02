"""
Per-lot tray history for the Eltec array test rig.

The array rig measures a TRAY of up to fifty parts at once, so the unit of
work is the tray, not the part. Every model tester writes one verdict row
per position into its lot CSV; this module keeps the sibling
``*_attempts.csv`` with one row per tray EVENT (the mirror of the single
rig's ``attempt_history.py``):

    locked                   occupancy frozen, sensor numbers assigned, HO parts recorded
    stabilisation_shortened  the technician cut the TP120 5-minute wait short (actual wait recorded)
    capture_started          the noise stream started (attempt n)
    capture_retry            a stream integrity failure -> the capture restarts
    capture_error            every attempt failed; positions recorded NOT MEASURED
    judged                   verdicts computed and shown
    saved                    rows written to the lot CSV (+ raw capture, grid snapshot)
    remeasure                the technician discarded the shown verdicts and re-ran the tray

Sensor numbers are assigned at lock time, row-major over the LOADED
positions, continuing from the highest number already saved in the lot
(CSV first, attempts as the fallback), so a re-measured tray keeps its
numbers and a new tray never reuses one. Pure stdlib so every model
directory can import it via ``sys.path``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

EVENT_LOCKED = "locked"
EVENT_STABILISATION_SHORTENED = "stabilisation_shortened"
EVENT_CAPTURE_STARTED = "capture_started"
EVENT_CAPTURE_RETRY = "capture_retry"
EVENT_CAPTURE_ERROR = "capture_error"
EVENT_JUDGED = "judged"
EVENT_SAVED = "saved"
EVENT_REMEASURE = "remeasure"

TRAY_EVENTS = (
    EVENT_LOCKED,
    EVENT_STABILISATION_SHORTENED,
    EVENT_CAPTURE_STARTED,
    EVENT_CAPTURE_RETRY,
    EVENT_CAPTURE_ERROR,
    EVENT_JUDGED,
    EVENT_SAVED,
    EVENT_REMEASURE,
)

TRAY_FIELDS = [
    "timestamp",
    "lot_number",
    "tray_number",
    "tray_attempt",          # 1-based measurement attempt of this tray
    "event",
    "phase",
    "detail",                # free text: stream diagnostics, error, note
    "tester_name",
    "loaded_count",
    "ho_positions",          # positions failed fast at lock time, "1-3 2-7"
    "first_sensor_number",
    "last_sensor_number",
    "stabilisation_wait_s",
    "quiet_wait_s",
    "capture_seconds",
    "error",
]


@dataclass
class TrayEvent:
    timestamp: str
    lot_number: str
    tray_number: int
    tray_attempt: int
    event: str
    phase: str = ""
    detail: str = ""
    tester_name: str = ""
    loaded_count: int = 0
    ho_positions: str = ""
    first_sensor_number: int = 0
    last_sensor_number: int = 0
    stabilisation_wait_s: str = ""
    quiet_wait_s: str = ""
    capture_seconds: str = ""
    error: str = ""


def attempts_path_for(results_csv_path: Path) -> Path:
    """``<lot>.csv`` -> ``<lot>_attempts.csv`` next to it."""

    return results_csv_path.with_name(results_csv_path.stem + "_attempts.csv")


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _int(text: str | None) -> int:
    try:
        return int((text or "0").strip())
    except ValueError:
        return 0


def append_tray_event(
    attempts_path: Path,
    *,
    lot_number: str,
    tray_number: int,
    tray_attempt: int,
    event: str,
    phase: str = "",
    detail: str = "",
    tester_name: str = "",
    loaded_count: int = 0,
    ho_positions: str = "",
    first_sensor_number: int = 0,
    last_sensor_number: int = 0,
    stabilisation_wait_s: float | None = None,
    quiet_wait_s: float | None = None,
    capture_seconds: float | None = None,
    error: str = "",
) -> None:
    if event not in TRAY_EVENTS:
        raise ValueError(f"Unknown tray event {event!r}")
    attempts_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not attempts_path.exists() or attempts_path.stat().st_size == 0
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "lot_number": lot_number,
        "tray_number": str(tray_number),
        "tray_attempt": str(tray_attempt),
        "event": event,
        "phase": phase,
        "detail": " ".join((detail or "").split()),
        "tester_name": tester_name,
        "loaded_count": str(loaded_count),
        "ho_positions": ho_positions,
        "first_sensor_number": str(first_sensor_number),
        "last_sensor_number": str(last_sensor_number),
        "stabilisation_wait_s": _fmt(stabilisation_wait_s),
        "quiet_wait_s": _fmt(quiet_wait_s),
        "capture_seconds": _fmt(capture_seconds),
        "error": " ".join((error or "").split()),
    }
    with attempts_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRAY_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def read_tray_events(attempts_path: Path) -> list[TrayEvent]:
    if not attempts_path.exists():
        return []
    events: list[TrayEvent] = []
    try:
        with attempts_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                known = {name: (row.get(name) or "") for name in TRAY_FIELDS}
                events.append(
                    TrayEvent(
                        timestamp=known["timestamp"],
                        lot_number=known["lot_number"],
                        tray_number=_int(known["tray_number"]),
                        tray_attempt=_int(known["tray_attempt"]),
                        event=known["event"],
                        phase=known["phase"],
                        detail=known["detail"],
                        tester_name=known["tester_name"],
                        loaded_count=_int(known["loaded_count"]),
                        ho_positions=known["ho_positions"],
                        first_sensor_number=_int(known["first_sensor_number"]),
                        last_sensor_number=_int(known["last_sensor_number"]),
                        stabilisation_wait_s=known["stabilisation_wait_s"],
                        quiet_wait_s=known["quiet_wait_s"],
                        capture_seconds=known["capture_seconds"],
                        error=known["error"],
                    )
                )
    except Exception:
        return events
    return events


def highest_tray_number(attempts_path: Path) -> int:
    highest = 0
    for event in read_tray_events(attempts_path):
        highest = max(highest, event.tray_number)
    return highest


def tray_attempts(attempts_path: Path, tray_number: int) -> int:
    """Highest attempt number recorded for one tray (0 if never measured)."""

    highest = 0
    for event in read_tray_events(attempts_path):
        if event.tray_number == tray_number:
            highest = max(highest, event.tray_attempt)
    return highest


def highest_sensor_number(results_csv_path: Path, attempts_path: Path | None = None) -> int:
    """Highest sensor number in the lot CSV (any row, including HO fail-fast rows),
    falling back to the attempts log so a locked-but-unsaved tray is not renumbered."""

    highest = 0
    if results_csv_path.exists():
        try:
            with results_csv_path.open("r", newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    highest = max(highest, _int(row.get("sensor_number")))
        except Exception:
            pass
    if attempts_path is not None:
        for event in read_tray_events(attempts_path):
            highest = max(highest, event.last_sensor_number)
    return highest


def format_positions(positions: list[str] | tuple[str, ...]) -> str:
    return " ".join(positions)
