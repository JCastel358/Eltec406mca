"""Durable part-attempt accounting and advisory slot history for one fixture.

Parts are operator-linked original tray positions, not serial identities.
Every attempt stays immutable; only valid selected stages replace the current
assessment. SQLite transactions make importing an already saved run idempotent.
"""
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import sqlite3

import mod50_hardware as hw


def stage_failure(row, mode):
    if not row.get("acquisition_validity", {}).get(mode) or row.get("presence") == "NO_SIGNAL":
        return False
    return ((mode == "dc" and row.get("dc_check") in ("OUTSIDE_WINDOW_PROVISIONAL", "RANGE_LIMIT"))
            or any(flag.startswith(mode.upper() + ":") for flag in row.get("flags", [])))


class History:
    def __init__(self, root, simulate=False):
        self.path = Path(root) / ("simulation_history.sqlite3" if simulate else "fixture_history.sqlite3")

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY, family_id TEXT NOT NULL, lot TEXT NOT NULL,
                        directory TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS parts (
                        id TEXT PRIMARY KEY, family_id TEXT NOT NULL, lot TEXT NOT NULL,
                        origin_position TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS attempts (
                        run_id TEXT NOT NULL, part_id TEXT NOT NULL, position TEXT NOT NULL,
                        tests TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(run_id, part_id));
                    CREATE TABLE IF NOT EXISTS slot_events (
                        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, position TEXT NOT NULL,
                        failed INTEGER NOT NULL, UNIQUE(run_id, position));
                    CREATE TABLE IF NOT EXISTS repairs (
                        id INTEGER PRIMARY KEY, position TEXT NOT NULL, after_event INTEGER NOT NULL,
                        operator TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS parts_lot_family ON parts(lot,family_id);
                    CREATE INDEX IF NOT EXISTS attempts_part ON attempts(part_id);
                    CREATE INDEX IF NOT EXISTS slot_events_position ON slot_events(position,id);
                """)
                yield connection
        except sqlite3.Error as exc:
            raise ValueError("The saved fixture history could not be read or updated; keep the history file and resolve this before another run.") from exc
        finally:
            connection.close()

    def record(self, result, directory):
        with self.connection() as db:
            run_id = result["run_id"]
            family = result.get("family_id", run_id)
            if db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                return
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (run_id, family, result["lot"],
                       str(Path(directory).resolve()), result["created_at"]))
            mapping = result.get("part_mapping") or {}
            tests = result.get("selected_tests", ["dc", "noise"])
            for row in result["rows"]:
                if not row["installed"]:
                    continue
                position = row["position"]
                part_id = mapping.get(position, run_id + ":" + position)
                part = db.execute("SELECT * FROM parts WHERE id=?", (part_id,)).fetchone()
                if mapping and (part is None or part["lot"] != result["lot"] or part["family_id"] != family):
                    raise ValueError("Retest refers to a part outside this original tray")
                if not part:
                    db.execute("INSERT INTO parts VALUES (?,?,?,?)", (part_id, family, result["lot"], position))
                db.execute("INSERT INTO attempts VALUES (?,?,?,?,?)", (run_id, part_id, position,
                           json.dumps(tests), json.dumps(row, allow_nan=False)))
                # Delivery failures, no-signal inference and unused sockets
                # are not evidence of a defective slot. Reused DC in noise
                # recovery must not count as a second slot test.
                observed = [m for m in tests if not (result.get("resumed_from") and m == "dc")]
                valid = [m for m in observed if row.get("acquisition_validity", {}).get(m)]
                if row.get("presence") != "NO_SIGNAL" and valid:
                    failed = any(stage_failure(row, m) for m in valid)
                    if failed or len(valid) == len(observed):
                        db.execute("INSERT INTO slot_events(run_id,position,failed) VALUES (?,?,?)",
                                   (run_id, position, int(failed)))

    def current_parts(self, lot, family=None):
        if not self.path.exists():
            return []
        with self.connection() as db:
            query, params = "SELECT * FROM parts WHERE lot=?", [lot]
            if family is not None:
                query += " AND family_id=?"
                params.append(family)
            parts = []
            for part in db.execute(query + " ORDER BY rowid", params).fetchall():
                item = dict(part)
                stages, requested, latest, count = {}, set(), None, 0
                for attempt in db.execute("SELECT * FROM attempts WHERE part_id=? ORDER BY rowid", (part["id"],)):
                    row = json.loads(attempt["data"])
                    count += 1
                    latest = attempt
                    tests = json.loads(attempt["tests"])
                    requested.update(tests)
                    for mode in tests:
                        if row.get("acquisition_validity", {}).get(mode):
                            stages[mode] = row
                dc, noise = stages.get("dc", {}), stages.get("noise", {})
                no_signal = bool(stages) and all(r.get("presence") == "NO_SIGNAL" for r in stages.values())
                failure = any(stage_failure(row, mode) for mode, row in stages.items())
                complete = requested.issubset(stages)
                outcome = ("NO_SIGNAL" if no_signal else "CHECK" if failure else "INCOMPLETE" if not complete
                           else "REVIEW_NO_NOISE_LIMIT" if "noise" in requested else "PASS_OFFSET_ONLY")
                item.update(part_id=item.pop("id"), position=latest["position"], latest_run_id=latest["run_id"],
                            attempts=count, result=outcome, dc_check=dc.get("dc_check", "NOT_TAKEN"),
                            dc_v=dc.get("dc_v"), selected_tests=sorted(requested),
                            acquisition_validity={m: m in stages for m in ("dc", "noise")})
                parts.append(item)
            return parts

    def summary(self, lot, family=None):
        parts = self.current_parts(lot, family)
        counts = {key: sum(p["result"] == value for p in parts) for key, value in
                  (("offset_pass", "PASS_OFFSET_ONLY"), ("review", "REVIEW_NO_NOISE_LIMIT"),
                   ("failed", "CHECK"), ("incomplete", "INCOMPLETE"), ("no_signal", "NO_SIGNAL"))}
        assessed = counts["offset_pass"] + counts["review"] + counts["failed"]
        return {"lot": lot, "tracked_parts": len(parts), "assessed_parts": assessed, **counts,
                "screening_yield_percent": 100 * (assessed - counts["failed"]) / assessed if assessed else None,
                "attempts": sum(p["attempts"] for p in parts),
                "note": "Screening yield means no flagged issue among assessed parts. Noise Review is not an acceptance pass. Parts are linked by original tray position, not serial number."}

    def warnings(self):
        if not self.path.exists():
            return []
        with self.connection() as db:
            warnings = []
            for position in hw.POSITIONS:
                cleared = db.execute("SELECT COALESCE(MAX(after_event),0) FROM repairs WHERE position=?", (position,)).fetchone()[0]
                events = db.execute("SELECT * FROM slot_events WHERE position=? AND id>? ORDER BY id", (position, cleared)).fetchall()
                streak, triggered, trigger_id = 0, False, None
                for event in events:
                    streak = streak + 1 if event["failed"] else 0
                    if streak >= 3:
                        triggered, trigger_id = True, event["id"]
                # Once warned, only an explicit repair verification clears it.
                if triggered:
                    warnings.append({"position": position, "trigger_event": trigger_id,
                                     "message": "Three consecutive valid checks flagged this slot. It may be faulty; avoid it until checked and repaired."})
            return warnings

    def verify_repairs(self, positions, operator):
        positions = hw.normalize_positions(positions)
        if not positions:
            return
        with self.connection() as db:
            for position in positions:
                last = db.execute("SELECT COALESCE(MAX(id),0) FROM slot_events WHERE position=?", (position,)).fetchone()[0]
                db.execute("INSERT INTO repairs(position,after_event,operator,created_at) VALUES (?,?,?,?)",
                           (position, last, operator, dt.datetime.now().astimezone().isoformat()))
