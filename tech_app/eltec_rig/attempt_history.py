"""
Per-batch attempt history for the unified Eltec test rig (v2.0).

Every model tester writes ONE verdict row per sensor into its batch CSV. That
row alone cannot explain why a part was re-measured or set aside, so each
batch now also keeps a sibling ``*_attempts.csv`` with one row per EVENT:

    measured        a measurement finished (verdict + key readings)
    measure_error   an attempt recorded nothing (rig / stream fault)
    remeasure       the technician discarded the shown verdict and re-ran
    skipped         the part was set aside to be measured later (reason)
    resumed         a skipped part was loaded again, in skip order
    saved           the verdict row was written to the batch CSV

Skipping never consumes a fresh sensor number: the part keeps its id, comes
back in first-skipped-first-measured order via "Measure skipped", and the
next fresh number is derived from BOTH files so a skipped id is never handed
out twice. Everything here is pure stdlib so it stays importable from each
model directory (they add this package's directory to ``sys.path``).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

EVENT_MEASURED = "measured"
EVENT_MEASURE_ERROR = "measure_error"
EVENT_REMEASURE = "remeasure"
EVENT_SKIPPED = "skipped"
EVENT_RESUMED = "resumed"
EVENT_SAVED = "saved"

ATTEMPT_EVENTS = (
    EVENT_MEASURED,
    EVENT_MEASURE_ERROR,
    EVENT_REMEASURE,
    EVENT_SKIPPED,
    EVENT_RESUMED,
    EVENT_SAVED,
)

# Why a part was set aside. Short on purpose - the technician picks one in
# two clicks; the attempt rows around it carry the numbers.
SKIP_REASON_CHOICES = (
    "Bad contact / would not seat",
    "Reading drifting or not settling",
    "Rig or stream fault",
    "Interrupted / no time now",
    "Result looked wrong",
    "Other",
)
DEFAULT_SKIP_REASON = SKIP_REASON_CHOICES[0]

ATTEMPT_FIELDS = [
    "timestamp",
    "batch_number",
    "sensor_number",
    "sensor_id",
    "event",
    "attempt",          # 1-based measurement attempt this row belongs to
    "outcome",          # verdict shown for measured/remeasure/saved rows
    "reason",           # skip reason, or the rig error for measure_error
    "note",             # free text (skip note / operator comment)
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
    """Sensor ids that already have a verdict row in the batch CSV."""
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


def skipped_queue(attempts_path: Path, results_csv_path: Path) -> list[tuple[int, str]]:
    """Skipped parts still without a verdict, first-skipped first.

    A part that was resumed and skipped AGAIN goes to the back (its latest
    skip is what orders it), so the physical pile and the queue agree.
    """
    saved = saved_sensor_ids(results_csv_path)
    last_skip_order: dict[str, tuple[int, int]] = {}
    for index, event in enumerate(read_attempts(attempts_path)):
        if event.event == EVENT_SKIPPED and event.sensor_id:
            last_skip_order[event.sensor_id] = (index, event.sensor_number)
    queue = [
        (number, sensor_id)
        for sensor_id, (index, number) in sorted(
            last_skip_order.items(), key=lambda item: item[1][0]
        )
        if sensor_id not in saved
    ]
    return queue


def highest_sensor_number(attempts_path: Path) -> int:
    highest = 0
    for event in read_attempts(attempts_path):
        highest = max(highest, event.sensor_number)
    return highest


def attempt_counts(attempts_path: Path, sensor_id: str) -> tuple[int, int]:
    """(measurement attempts so far, times skipped) for one sensor id."""
    measured = 0
    skipped = 0
    for event in read_attempts(attempts_path):
        if event.sensor_id != sensor_id:
            continue
        if event.event in (EVENT_MEASURED, EVENT_MEASURE_ERROR):
            measured += 1
        elif event.event == EVENT_SKIPPED:
            skipped += 1
    return measured, skipped


def format_queue(queue: list[tuple[int, str]], limit: int = 8) -> str:
    ids = [sensor_id for _number, sensor_id in queue]
    if len(ids) <= limit:
        return ", ".join(ids)
    return ", ".join(ids[:limit]) + f", … (+{len(ids) - limit})"
