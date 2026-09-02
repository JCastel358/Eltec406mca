"""Tests for the v2.0 per-batch attempt log and the skip / resume queue.

Pure-file tests for ``attempt_history`` plus flow tests that drive the real
``EmitterTesterApp`` methods of BOTH model testers against a bare harness
(no Tk, no hardware): skipping never spends a sensor number, skipped parts
come back first-skipped-first, re-measure and save leave an audit trail,
and the verdict row reports the attempt counts.
"""

from __future__ import annotations

import csv
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

RIG_DIR = Path(__file__).resolve().parents[1]
if str(RIG_DIR) not in sys.path:
    sys.path.insert(0, str(RIG_DIR))

import attempt_history as ah  # noqa: E402


class AttemptLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.results = Path(self.tmp.name) / "lot_B1.csv"
        self.attempts = ah.attempts_path_for(self.results)

    def tearDown(self):
        self.tmp.cleanup()

    def _skip(self, number, reason="Other"):
        ah.append_attempt(
            self.attempts,
            batch_number="B1",
            sensor_number=number,
            sensor_id=f"B1-{number}",
            event=ah.EVENT_SKIPPED,
            attempt=0,
            reason=reason,
        )

    def _save(self, number):
        self.results.parent.mkdir(parents=True, exist_ok=True)
        new = not self.results.exists()
        with self.results.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["sensor_number", "sensor_id"])
            if new:
                writer.writeheader()
            writer.writerow({"sensor_number": number, "sensor_id": f"B1-{number}"})

    def test_attempts_file_sits_next_to_the_batch_csv(self):
        self.assertEqual(self.attempts.name, "lot_B1_attempts.csv")
        self.assertEqual(self.attempts.parent, self.results.parent)

    def test_rows_round_trip_with_readings_and_reasons(self):
        ah.append_attempt(
            self.attempts,
            batch_number="B1",
            sensor_number=4,
            sensor_id="B1-4",
            event=ah.EVENT_MEASURED,
            attempt=1,
            outcome="FAIL",
            offset_v=1.23456789,
            sensitivity_mv=45.5,
            polarity="POSITIVE",
            noise_worst_pp_mv=0.5,
            fail_reasons=["too low", "noisy"],
            note="  two   words \n here ",
        )
        rows = ah.read_attempts(self.attempts)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.sensor_number, row.sensor_id, row.event, row.attempt), (4, "B1-4", "measured", 1))
        self.assertEqual(row.outcome, "FAIL")
        self.assertEqual(row.offset_v, "1.234568")
        self.assertEqual(row.fail_reasons, "too low; noisy")
        self.assertEqual(row.note, "two words here")
        with self.assertRaises(ValueError):
            ah.append_attempt(
                self.attempts, batch_number="B1", sensor_number=1, sensor_id="B1-1",
                event="bogus", attempt=1,
            )

    def test_queue_is_first_skipped_first_and_drops_saved_parts(self):
        self._skip(3)
        self._skip(7)
        self._skip(5)
        self.assertEqual(ah.skipped_queue(self.attempts, self.results), [(3, "B1-3"), (7, "B1-7"), (5, "B1-5")])
        self._save(7)
        self.assertEqual(ah.skipped_queue(self.attempts, self.results), [(3, "B1-3"), (5, "B1-5")])
        # Skipped again after being resumed -> back of the pile.
        self._skip(3, reason="Result looked wrong")
        self.assertEqual(ah.skipped_queue(self.attempts, self.results), [(5, "B1-5"), (3, "B1-3")])

    def test_counts_and_highest_number(self):
        self._skip(9)
        for attempt in (1, 2):
            ah.append_attempt(
                self.attempts, batch_number="B1", sensor_number=9, sensor_id="B1-9",
                event=ah.EVENT_MEASURED, attempt=attempt, outcome="PASS",
            )
        ah.append_attempt(
            self.attempts, batch_number="B1", sensor_number=2, sensor_id="B1-2",
            event=ah.EVENT_MEASURE_ERROR, attempt=1, reason="stream fault",
        )
        self.assertEqual(ah.attempt_counts(self.attempts, "B1-9"), (2, 1))
        self.assertEqual(ah.attempt_counts(self.attempts, "B1-2"), (1, 0))
        self.assertEqual(ah.attempt_counts(self.attempts, "B1-99"), (0, 0))
        self.assertEqual(ah.highest_sensor_number(self.attempts), 9)
        self.assertEqual(ah.highest_sensor_number(Path(self.tmp.name) / "missing.csv"), 0)

    def test_format_queue_is_short(self):
        queue = [(n, f"B1-{n}") for n in range(1, 12)]
        text = ah.format_queue(queue)
        self.assertTrue(text.startswith("B1-1, B1-2"))
        self.assertIn("(+3)", text)
        self.assertEqual(ah.format_queue(queue[:2]), "B1-1, B1-2")


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


# Both model apps import helper modules under the SAME names
# (stability_analysis, esp32_backend, the vendored eltec_406mca_tester), so
# loading the second model must drop the first model's copies first.
_SHARED_MODULE_NAMES = ("stability_analysis", "esp32_backend", "eltec_406mca_tester")
_MODEL_DIRS = ("m405m22", "m406mca", "m449m18")
_MODEL_MODULES = (
    ("m405m22", "eltec_405m22_esp32_tester"),
    ("m406mca", "eltec_406mca_esp32_tester"),
    ("m449m18", "eltec_449m18_esp32_tester"),
)


def _load_app(module_dir: str, module_name: str):
    for other in _MODEL_DIRS:
        other_dir = str(RIG_DIR / other)
        while other_dir in sys.path:
            sys.path.remove(other_dir)
    for name in _SHARED_MODULE_NAMES + tuple(module for _dir, module in _MODEL_MODULES):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(RIG_DIR / module_dir))
    return importlib.import_module(module_name)


class _SkipFlowMixin:
    """Drives the real app methods on a SimpleNamespace stand-in."""

    MODULE_DIR = ""
    MODULE_NAME = ""

    @classmethod
    def setUpClass(cls):
        cls.app = _load_app(cls.MODULE_DIR, cls.MODULE_NAME)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patcher = mock.patch.object(self.app, "results_root_dir", lambda: self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def harness(self):
        app = self.app
        h = SimpleNamespace(
            step="load",
            LOAD_STEP="load",
            RESULT_STEP="result",
            SETUP_STEP="setup",
            busy=False,
            measuring=False,
            result_saved=False,
            resuming_skipped=False,
            measure_attempts=0,
            skip_count=0,
            batch_number="B7",
            tester_name="Tech",
            current_sensor_number=0,
            current_sensor_id="",
            last_result=None,
            last_noise_report=None,
            notes_var=FakeVar(""),
            status_var=FakeVar(""),
            shown=[],
            prepared=[],
        )
        cls = app.EmitterTesterApp
        h._attempts_path = lambda: cls._attempts_path(h)
        h.skipped_parts_queue = lambda: cls.skipped_parts_queue(h)
        h._next_fresh_sensor_number = lambda: cls._next_fresh_sensor_number(h)
        h.can_skip_part = lambda: cls.can_skip_part(h)
        h._log_attempt = lambda event, **kw: cls._log_attempt(h, event, **kw)
        h.skip_current_part = lambda reason, note="": cls.skip_current_part(h, reason, note)
        h._advance_to_next_sensor = lambda: cls._advance_to_next_sensor(h)
        h._load_skipped_part = lambda n, s: cls._load_skipped_part(h, n, s)
        h.delete_autosave = lambda: None
        h.show_step = lambda step: h.shown.append(step)

        def prepare():
            h.current_sensor_id = f"{h.batch_number}-{h.current_sensor_number}"
            h.result_saved = False
            h.last_result = None
            h.measure_attempts, h.skip_count = app.attempt_history.attempt_counts(
                h._attempts_path(), h.current_sensor_id
            )
            h.prepared.append(h.current_sensor_id)

        h.prepare_current_sensor = prepare
        return h

    def _write_verdict_row(self, h):
        """Stand-in for save_current_sensor's CSV write + saved event."""
        path = self.app.batch_results_path(h.batch_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["sensor_number", "sensor_id", "measure_attempts", "skip_count"])
            if new:
                writer.writeheader()
            writer.writerow({
                "sensor_number": h.current_sensor_number,
                "sensor_id": h.current_sensor_id,
                "measure_attempts": h.measure_attempts,
                "skip_count": h.skip_count,
            })
        h._log_attempt(self.app.attempt_history.EVENT_SAVED)
        h.result_saved = True

    def test_skip_keeps_the_number_and_resume_walks_the_pile_in_order(self):
        h = self.harness()
        h.current_sensor_number = h._next_fresh_sensor_number()
        h.prepare_current_sensor()
        self.assertEqual(h.current_sensor_id, "B7-1")

        self.assertTrue(h.skip_current_part("Bad contact / would not seat", "loose pin"))
        self.assertEqual(h.current_sensor_id, "B7-2")       # fresh number, 1 stays open
        self._write_verdict_row(h)
        h._advance_to_next_sensor()
        self.assertEqual(h.current_sensor_id, "B7-3")
        self.assertTrue(h.skip_current_part("Interrupted / no time now"))
        self.assertEqual(h.current_sensor_id, "B7-4")
        self.assertEqual(h.skipped_parts_queue(), [(1, "B7-1"), (3, "B7-3")])
        self.assertIn("2 skipped", h.status_var.get())
        # B7-4 is on the bench with nothing logged yet, so it is still the
        # next fresh number; the skipped ids are never re-issued.
        self.assertEqual(h._next_fresh_sensor_number(), 4)
        self.assertNotIn(h._next_fresh_sensor_number(), [n for n, _ in h.skipped_parts_queue()])

        # Technician reaches the pile: first skipped comes back first.
        h._load_skipped_part(*h.skipped_parts_queue()[0])
        self.assertTrue(h.resuming_skipped)
        self.assertEqual(h.current_sensor_id, "B7-1")
        self.assertEqual(h.skip_count, 1)
        self._write_verdict_row(h)
        h._advance_to_next_sensor()
        self.assertEqual(h.current_sensor_id, "B7-3")       # next in the pile
        self.assertTrue(h.resuming_skipped)
        self._write_verdict_row(h)
        h._advance_to_next_sensor()
        self.assertFalse(h.resuming_skipped)                # pile empty -> fresh
        # B7-4 was on the bench untouched when the pile was started, so its
        # number was never spent and is handed out again.
        self.assertEqual(h.current_sensor_id, "B7-4")
        self.assertEqual(h.skipped_parts_queue(), [])

        events = [(e.sensor_id, e.event) for e in self.app.attempt_history.read_attempts(h._attempts_path())]
        self.assertEqual(
            events,
            [
                ("B7-1", "skipped"), ("B7-2", "saved"), ("B7-3", "skipped"),
                ("B7-1", "resumed"), ("B7-1", "saved"), ("B7-3", "resumed"), ("B7-3", "saved"),
            ],
        )
        skip_rows = [e for e in self.app.attempt_history.read_attempts(h._attempts_path()) if e.event == "skipped"]
        self.assertEqual(skip_rows[0].reason, "Bad contact / would not seat")
        self.assertEqual(skip_rows[0].note, "loose pin")

    def test_skip_is_only_offered_for_an_unsaved_part_when_idle(self):
        h = self.harness()
        h.current_sensor_number = 1
        h.prepare_current_sensor()
        self.assertTrue(h.can_skip_part())
        h.measuring = True
        self.assertFalse(h.can_skip_part())
        h.measuring = False
        h.result_saved = True
        self.assertFalse(h.can_skip_part())
        self.assertFalse(h.skip_current_part("Other"))
        h.result_saved = False
        h.step = "setup"
        self.assertFalse(h.can_skip_part())

    def test_measured_and_remeasure_events_keep_the_discarded_verdict(self):
        app = self.app
        h = self.harness()
        h.current_sensor_number = 1
        h.prepare_current_sensor()
        failing = app.FinalResult(
            passed=False, fail_reasons=["sensitivity too low"], warnings=[], offset_v=1.5,
            sensitivity_mv=10.0, polarity="POSITIVE",
        )
        h._log_attempt(app.attempt_history.EVENT_MEASURED, result=failing)
        self.assertEqual(h.measure_attempts, 1)
        h.last_result = failing
        # Re-measure discards the shown verdict -> audit row, then a new attempt.
        h._log_attempt(app.attempt_history.EVENT_REMEASURE, result=failing)
        passing = app.FinalResult(passed=True, fail_reasons=[], warnings=[], offset_v=1.4, sensitivity_mv=30.0, polarity="POSITIVE")
        h._log_attempt(app.attempt_history.EVENT_MEASURED, result=passing)
        self.assertEqual(h.measure_attempts, 2)
        rows = app.attempt_history.read_attempts(h._attempts_path())
        self.assertEqual([r.event for r in rows], ["measured", "remeasure", "measured"])
        self.assertEqual(rows[0].outcome, app.OUTCOME_FAIL)
        self.assertEqual(rows[0].fail_reasons, "sensitivity too low")
        self.assertEqual(rows[1].attempt, 1)
        self.assertEqual(rows[2].attempt, 2)
        self.assertEqual(rows[2].outcome, app.OUTCOME_PASS)

    def test_verdict_csv_carries_attempt_and_skip_counts(self):
        app = self.app
        path = app.batch_results_path("B9")
        result = app.FinalResult(passed=True, fail_reasons=[], warnings=[], offset_v=1.0, sensitivity_mv=25.0, polarity="POSITIVE")
        kwargs = dict(
            batch_number="B9", sensor_number=2, sensor_id="B9-2", tester_name="Tech",
            filter_setup=app.DEFAULT_FILTER_SETUP, pwm_channel="DAC0", pwm_hz=1.0, pwm_duty=50.0,
            final_result=result, comment="", snapshot_paths=[], measure_attempts=3, skip_count=1,
        )
        app.append_result_csv(path, **kwargs)
        with path.open(newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["measure_attempts"], "3")
        self.assertEqual(row["skip_count"], "1")
        self.assertIn("measure_attempts", app.CSV_FIELDS)
        self.assertIn("skip_count", app.CSV_FIELDS)
        # A skipped id above the saved ones still blocks the next fresh number.
        attempts = app.attempt_history.attempts_path_for(path)
        app.attempt_history.append_attempt(
            attempts, batch_number="B9", sensor_number=5, sensor_id="B9-5",
            event=app.attempt_history.EVENT_SKIPPED, attempt=0, reason="Other",
        )
        h = self.harness()
        h.batch_number = "B9"
        self.assertEqual(h._next_fresh_sensor_number(), 6)


class SkipFlow405M22Tests(_SkipFlowMixin, unittest.TestCase):
    MODULE_DIR = "m405m22"
    MODULE_NAME = "eltec_405m22_esp32_tester"


class SkipFlow406MCATests(_SkipFlowMixin, unittest.TestCase):
    MODULE_DIR = "m406mca"
    MODULE_NAME = "eltec_406mca_esp32_tester"


class SkipFlow449M18Tests(_SkipFlowMixin, unittest.TestCase):
    MODULE_DIR = "m449m18"
    MODULE_NAME = "eltec_449m18_esp32_tester"


class FooterButtonTests(unittest.TestCase):
    """The v2.0 footer palette exists in every model."""

    def test_success_and_warn_palettes_and_xl_size(self):
        for module_dir, module_name in _MODEL_MODULES:
            with self.subTest(model=module_dir):
                app = _load_app(module_dir, module_name)
                self.assertIn("success", app.RoundButton.PALETTES)
                self.assertIn("warn", app.RoundButton.PALETTES)
                self.assertIn("xl", app.RoundButton.SIZE_PADS)
                self.assertGreater(app.RoundButton.SIZE_PADS["xl"][0], app.RoundButton.SIZE_PADS["lg"][0])


if __name__ == "__main__":
    unittest.main()
