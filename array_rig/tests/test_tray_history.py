"""Tests for the array rig's per-lot tray history."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

RIG_DIR = Path(__file__).resolve().parents[1]
if str(RIG_DIR) not in sys.path:
    sys.path.insert(0, str(RIG_DIR))

import tray_history as th  # noqa: E402


class TrayHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name) / "40623_array_lot_7.csv"
        self.attempts = th.attempts_path_for(self.results)

    def test_attempts_path_sits_next_to_the_lot_csv(self):
        self.assertEqual(self.attempts.name, "40623_array_lot_7_attempts.csv")
        self.assertEqual(self.attempts.parent, self.results.parent)

    def test_append_and_read_round_trip(self):
        th.append_tray_event(
            self.attempts, lot_number="7", tray_number=1, tray_attempt=1, event=th.EVENT_LOCKED,
            phase="LOCKED", detail="  two\nlines  ", tester_name="JC", loaded_count=48,
            ho_positions="1-3 2-7", first_sensor_number=1, last_sensor_number=48,
        )
        th.append_tray_event(
            self.attempts, lot_number="7", tray_number=1, tray_attempt=1, event=th.EVENT_SAVED,
            stabilisation_wait_s=300.0, quiet_wait_s=3.25, capture_seconds=60.0,
        )
        events = th.read_tray_events(self.attempts)
        self.assertEqual([e.event for e in events], ["locked", "saved"])
        self.assertEqual(events[0].detail, "two lines")
        self.assertEqual(events[0].loaded_count, 48)
        self.assertEqual(events[0].ho_positions, "1-3 2-7")
        self.assertEqual(events[0].last_sensor_number, 48)
        self.assertEqual(events[1].quiet_wait_s, "3.250")
        self.assertEqual(events[1].capture_seconds, "60.000")
        with self.attempts.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], th.TRAY_FIELDS)
        self.assertEqual(len(rows), 3)  # header written once

    def test_unknown_event_rejected(self):
        with self.assertRaises(ValueError):
            th.append_tray_event(self.attempts, lot_number="7", tray_number=1, tray_attempt=1, event="teleported")
        self.assertFalse(self.attempts.exists())

    def test_missing_file_reads_empty(self):
        self.assertEqual(th.read_tray_events(self.attempts), [])
        self.assertEqual(th.highest_tray_number(self.attempts), 0)
        self.assertEqual(th.highest_sensor_number(self.results, self.attempts), 0)

    def test_highest_tray_and_attempts(self):
        for tray, attempt in ((1, 1), (2, 1), (2, 2), (1, 2)):
            th.append_tray_event(self.attempts, lot_number="7", tray_number=tray, tray_attempt=attempt, event=th.EVENT_JUDGED)
        self.assertEqual(th.highest_tray_number(self.attempts), 2)
        self.assertEqual(th.tray_attempts(self.attempts, 1), 2)
        self.assertEqual(th.tray_attempts(self.attempts, 2), 2)
        self.assertEqual(th.tray_attempts(self.attempts, 3), 0)

    def test_highest_sensor_number_from_csv_with_gaps_and_fallback(self):
        with self.results.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["timestamp", "sensor_number", "pass_fail"])
            writer.writeheader()
            writer.writerow({"timestamp": "t", "sensor_number": "3", "pass_fail": "PASS"})
            writer.writerow({"timestamp": "t", "sensor_number": "12", "pass_fail": "FAIL"})
            writer.writerow({"timestamp": "t", "sensor_number": "", "pass_fail": ""})
            writer.writerow({"timestamp": "t", "sensor_number": "x", "pass_fail": ""})
        self.assertEqual(th.highest_sensor_number(self.results), 12)
        th.append_tray_event(
            self.attempts, lot_number="7", tray_number=2, tray_attempt=1, event=th.EVENT_LOCKED,
            first_sensor_number=13, last_sensor_number=40,
        )
        self.assertEqual(th.highest_sensor_number(self.results, self.attempts), 40)
        self.assertEqual(th.highest_sensor_number(self.results), 12)

    def test_corrupt_file_never_raises(self):
        self.attempts.write_text("timestamp,lot_number\n\"unterminated", encoding="utf-8")
        events = th.read_tray_events(self.attempts)
        self.assertIsInstance(events, list)

    def test_format_positions(self):
        self.assertEqual(th.format_positions(["1-3", "2-7"]), "1-3 2-7")
        self.assertEqual(th.format_positions([]), "")


if __name__ == "__main__":
    unittest.main()
