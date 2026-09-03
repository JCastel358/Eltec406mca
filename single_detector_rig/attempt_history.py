"""
Per-batch attempt history for the unified Eltec test rig.

Every model tester writes ONE verdict row per TEST into its batch CSV. That
row alone cannot explain why a part was read more than once, so each batch
also keeps a sibling ``*_attempts.csv`` with one row per EVENT:

    measured        a measurement finished (verdict + key readings)
    measure_error   an attempt recorded nothing (rig / stream fault)
    stream_retry    a capture inside an attempt was restarted by the app
                    itself (serial glitch or stall, 2026-09-03); the reason
                    column carries the stall attribution tag. Evidence, not
                    a read of its own - it never moves the attempt count
    rig_note        something the app noticed and put right on the rig
                    before reading (2026-09-03: the 406 MCA front end had
                    reverted, i.e. the ESP32 restarted). Evidence only
    stopped         the technician pressed Stop during a capture
    saved           the verdict row was written to the batch CSV

2026-09-02 (rule change): the skip queue is gone. A sensor number is only
spent when a part PASSES, so a failed or unreadable part simply leaves its
number open and the next part loaded into the rig is tested under the same
number - which is what the bench already did physically. There is no set-
aside pile to walk any more, so ``skipped``/``resumed``/``remeasure`` events
and the queue helpers that ordered them were removed with the Skip part,
Re-measure and Measure skipped buttons.

Everything here is pure stdlib so it stays importable from each model
directory (they add this package's directory to ``sys.path``).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

EVENT_MEASURED = "measured"
EVENT_MEASURE_ERROR = "measure_error"
EVENT_STREAM_RETRY = "stream_retry"
EVENT_RIG_NOTE = "rig_note"
EVENT_STOPPED = "stopped"
EVENT_SAVED = "saved"

ATTEMPT_EVENTS = (
    EVENT_MEASURED,
    EVENT_MEASURE_ERROR,
    EVENT_STREAM_RETRY,
    EVENT_RIG_NOTE,
    EVENT_STOPPED,
    EVENT_SAVED,
)

ATTEMPT_FIELDS = [
    "timestamp",
    "batch_number",
    "sensor_number",
    "sensor_id",
    "event",
    "attempt",          # 1-based measurement attempt this row belongs to
    "outcome",          # verdict shown for measured/saved rows
    "reason",           # the rig error for measure_error rows
    "note",             # free text (operator comment)
    "tester_name",
    "offset_v",
    "sensitivity_mv",
    "polarity",
    "noise_worst_pp_mv",
    "fail_reasons",
]


@dataclass
class AttemptEvent:
    timestamp: str
    batch_number: str
    sensor_number: int
    sensor_id: str
    event: str
    attempt: int
    outcome: str = ""
    reason: str = ""
    note: str = ""
    tester_name: str = ""
    offset_v: str = ""
    sensitivity_mv: str = ""
    polarity: str = ""
    noise_worst_pp_mv: str = ""
    fail_reasons: str = ""
    extra: dict = field(default_factory=dict)


def attempts_path_for(results_csv_path: Path) -> Path:
    """``<lot>.csv`` -> ``<lot>_attempts.csv`` next to it."""
    return results_csv_path.with_name(results_csv_path.stem + "_attempts.csv")


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def append_attempt(
    attempts_path: Path,
    *,
    batch_number: str,
    sensor_number: int,
    sensor_id: str,
    event: str,
    attempt: int,
    outcome: str = "",
    reason: str = "",
    note: str = "",
    tester_name: str = "",
    offset_v: float | None = None,
    sensitivity_mv: float | None = None,
    polarity: str = "",
    noise_worst_pp_mv: float | None = None,
    fail_reasons: list[str] | tuple[str, ...] | None = None,
) -> None:
    if event not in ATTEMPT_EVENTS:
        raise ValueError(f"Unknown attempt event {event!r}")
    attempts_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not attempts_path.exists() or attempts_path.stat().st_size == 0
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "batch_number": batch_number,
        "sensor_number": str(sensor_number),
        "sensor_id": sensor_id,
        "event": event,
        "attempt": str(attempt),
        "outcome": outcome or "",
        "reason": " ".join((reason or "").split()),
        "note": " ".join((note or "").split()),
        "tester_name": tester_name,
        "offset_v": _fmt(offset_v),
        "sensitivity_mv": _fmt(sensitivity_mv),
        "polarity": polarity or "",
        "noise_worst_pp_mv": _fmt(noise_worst_pp_mv),
        "fail_reasons": "; ".join(fail_reasons or ()),
    }
    with attempts_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ATTEMPT_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def read_attempts(attempts_path: Path) -> list[AttemptEvent]:
    if not attempts_path.exists():
        return []
    events: list[AttemptEvent] = []
    try:
        with attempts_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    number = int((row.get("sensor_number") or "0").strip())
                except ValueError:
                    number = 0
                try:
                    attempt = int((row.get("attempt") or "0").strip())
                except ValueError:
                    attempt = 0
                known = {name: (row.get(name) or "") for name in ATTEMPT_FIELDS}
                events.append(
                    AttemptEvent(
                        timestamp=known["timestamp"],
                        batch_number=known["batch_number"],
                        sensor_number=number,
                        sensor_id=known["sensor_id"],
                        event=known["event"],
                        attempt=attempt,
                        outcome=known["outcome"],
                        reason=known["reason"],
                        note=known["note"],
                        tester_name=known["tester_name"],
                        offset_v=known["offset_v"],
                        sensitivity_mv=known["sensitivity_mv"],
                        polarity=known["polarity"],
                        noise_worst_pp_mv=known["noise_worst_pp_mv"],
                        fail_reasons=known["fail_reasons"],
                    )
                )
    except Exception:
        return events
    return events


def saved_sensor_ids(results_csv_path: Path) -> set[str]:
    """Sensor ids that already have at least one verdict row in the batch CSV.

    A reused number writes several rows under one id, so this is a set of
    ids that have been WRITTEN, not a count of parts.
    """
    ids: set[str] = set()
    if not results_csv_path.exists():
        return ids
    try:
        with results_csv_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                sensor_id = (row.get("sensor_id") or "").strip()
                if sensor_id:
                    ids.add(sensor_id)
    except Exception:
        pass
    return ids


def format_sensor_ids(sensor_ids, limit: int = 8) -> str:
    """A short "a, b, c, ... (+n)" list of sensor ids for a summary line."""
    ids = list(sensor_ids)
    if len(ids) <= limit:
        return ", ".join(ids)
    return ", ".join(ids[:limit]) + f", … (+{len(ids) - limit})"


def measure_attempt_count(attempts_path: Path, sensor_id: str) -> int:
    """Reads logged against one sensor id: measured + errored + stopped.

    Across a whole batch this spans every part that was tested under a
    reused number; each tester counts the part in front of it from zero.
    """
    counted = 0
    for event in read_attempts(attempts_path):
        if event.sensor_id != sensor_id:
            continue
        if event.event in (EVENT_MEASURED, EVENT_MEASURE_ERROR, EVENT_STOPPED):
            counted += 1
    return counted
