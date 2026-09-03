"""Tests for the per-batch attempt log and the sensor-numbering rule.

Pure-file tests for ``attempt_history`` plus flow tests that drive the real
``EmitterTesterApp`` methods of ALL THREE model testers against a bare
harness (no Tk, no hardware).

2026-09-02: the skip queue is gone. A sensor number is only spent when a
part PASSES, so a failed or unreadable part leaves its number open and the
replacement loaded into the rig is tested under the same number. What is
checked here is that rule, the two-button flow around it (Stop abandons a
capture and records nothing; Next writes the verdict and reads the part
already in the rig), and the audit trail both leave behind.
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

    def test_the_retired_skip_events_are_gone(self):
        # The skip pile no longer exists; writing one of its events must be
        # rejected rather than silently logged into a file nothing reads.
        self.assertEqual(
            ah.ATTEMPT_EVENTS,
            ("measured", "measure_error", "stream_retry", "rig_note", "stopped", "saved"),
        )
        for retired in ("skipped", "resumed", "remeasure"):
            with self.assertRaises(ValueError):
                ah.append_attempt(
                    self.attempts, batch_number="B1", sensor_number=1,
                    sensor_id="B1-1", event=retired, attempt=1,
                )

    def test_stream_retry_is_logged_but_is_not_a_read(self):
        # 2026-09-03: a capture the app restarted by itself (serial glitch
        # or stall) is evidence of what the rig did - it must be accepted,
        # keep its attribution tag, and must not move the attempt count.
        ah.append_attempt(
            self.attempts, batch_number="B1", sensor_number=4, sensor_id="B1-4",
            event=ah.EVENT_STREAM_RETRY, attempt=1,
            reason="noise capture, restart 1/2: ESP32 noise stream stalled [host-stall]",
        )
        ah.append_attempt(
            self.attempts, batch_number="B1", sensor_number=4, sensor_id="B1-4",
            event=ah.EVENT_MEASURED, attempt=1, outcome="PASS",
        )
        ah.append_attempt(
            self.attempts, batch_number="B1", sensor_number=4, sensor_id="B1-4",
            event=ah.EVENT_RIG_NOTE, attempt=1,
            reason="ESP32 front end had reverted to the boot default",
        )
        self.assertEqual(ah.measure_attempt_count(self.attempts, "B1-4"), 1)
        events = ah.read_attempts(self.attempts)
        self.assertEqual(
            [event.event for event in events], ["stream_retry", "measured", "rig_note"]
        )
        self.assertIn("[host-stall]", events[0].reason)

    def test_measure_attempt_count_covers_every_read_of_one_id(self):
        for event in (ah.EVENT_MEASURED, ah.EVENT_STOPPED, ah.EVENT_MEASURE_ERROR):
            ah.append_attempt(
                self.attempts, batch_number="B1", sensor_number=9, sensor_id="B1-9",
                event=event, attempt=1,
            )
        # A saved row is bookkeeping, not a read.
        ah.append_attempt(
            self.attempts, batch_number="B1", sensor_number=9, sensor_id="B1-9",
            event=ah.EVENT_SAVED, attempt=3, outcome="PASS",
        )
        self.assertEqual(ah.measure_attempt_count(self.attempts, "B1-9"), 3)
        self.assertEqual(ah.measure_attempt_count(self.attempts, "B1-2"), 0)
        self.assertEqual(
            ah.measure_attempt_count(Path(self.tmp.name) / "missing.csv", "B1-9"), 0
        )

    def test_saved_sensor_ids_reads_the_batch_csv(self):
        self.results.parent.mkdir(parents=True, exist_ok=True)
        with self.results.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["sensor_id"])
            writer.writeheader()
            # A reused number writes the same id more than once.
            for sensor_id in ("B1-1", "B1-2", "B1-2"):
                writer.writerow({"sensor_id": sensor_id})
        self.assertEqual(ah.saved_sensor_ids(self.results), {"B1-1", "B1-2"})
        self.assertEqual(ah.saved_sensor_ids(Path(self.tmp.name) / "none.csv"), set())


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


class _NumberingFlowMixin:
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
            step="result",
            RESULT_STEP="result",
            SETUP_STEP="setup",
            busy=False,
            measuring=False,
            result_saved=False,
            measure_attempts=0,
            number_attempt=1,
            measure_token=4,
            batch_number="B7",
            tester_name="Tech",
            current_sensor_number=0,
            current_sensor_id="",
            last_result=None,
            last_metrics=None,
            last_measure_error=None,
            last_noise_report=None,
            preview_waveform=None,
            preview_sync=None,
            notes_var=FakeVar(""),
            status_var=FakeVar(""),
            measure_status_var=FakeVar(""),
            shown=[],
            prepared=[],
            reads=[],
        )
        cls = app.EmitterTesterApp
        h._attempts_path = lambda: cls._attempts_path(h)
        h._log_attempt = lambda event, **kw: cls._log_attempt(h, event, **kw)
        h._advance_to_next_sensor = lambda: cls._advance_to_next_sensor(h)
        h.abort_measurement = lambda: cls.abort_measurement(h)
        h.stop = lambda: cls.stop(h)
        h.go_next = lambda: cls.go_next(h)
        h.delete_autosave = lambda: None
        h.render_step = lambda: None
        h._reset_measure_progress = lambda: None
        h.show_step = lambda step: h.shown.append(step)
        h.run_measurement = lambda: h.reads.append(h.current_sensor_id)

        def prepare():
            # The real prepare_current_sensor also clears a dozen tk vars and
            # capture buffers; what matters to the numbering rule is this.
            h.current_sensor_id = f"{h.batch_number}-{h.current_sensor_number}"
            h.result_saved = False
            h.last_result = None
            h.measure_attempts = 0
            h.number_attempt = app.number_attempt_for_batch(
                app.batch_results_path(h.batch_number), h.current_sensor_id
            )
            h.prepared.append(h.current_sensor_id)

        h.prepare_current_sensor = prepare
        return h

    def _verdict(self, passed: bool):
        app = self.app
        return app.FinalResult(
            passed=passed,
            fail_reasons=[] if passed else ["sensitivity too low"],
            warnings=[],
            offset_v=1.4,
            sensitivity_mv=30.0 if passed else 5.0,
            polarity=app.POSITIVE_POLARITY,
        )

    def _write_verdict_row(self, h, passed: bool):
        """Stand-in for save_current_sensor: the real CSV write, no Tk."""
        app = self.app
        result = self._verdict(passed)
        app.append_result_csv(
            app.batch_results_path(h.batch_number),
            batch_number=h.batch_number,
            sensor_number=h.current_sensor_number,
            sensor_id=h.current_sensor_id,
            tester_name=h.tester_name,
            filter_setup=app.DEFAULT_FILTER_SETUP,
            pwm_channel="DAC0",
            pwm_hz=1.0,
            pwm_duty=50.0,
            final_result=result,
            comment="",
            snapshot_paths=[],
            failure_mode="" if passed else "LS - Low sensitivity",
            measure_attempts=h.measure_attempts,
            number_attempt=h.number_attempt,
        )
        h._log_attempt(app.attempt_history.EVENT_SAVED, result=result)
        h.result_saved = True

    def test_a_number_is_only_spent_by_a_pass(self):
        app = self.app
        h = self.harness()
        h.current_sensor_number = app.next_sensor_number_for_batch(
            app.batch_results_path(h.batch_number)
        )
        h.prepare_current_sensor()
        self.assertEqual(h.current_sensor_id, "B7-1")

        # Part 1 passes -> the number is earned and the next one is 2.
        self._write_verdict_row(h, passed=True)
        h._advance_to_next_sensor()
        self.assertEqual(h.current_sensor_id, "B7-2")
        self.assertEqual(h.number_attempt, 1)

        # Two bad parts under number 2: the number does not move, and each
        # replacement is recorded as another attempt at the same id.
        self._write_verdict_row(h, passed=False)
        h._advance_to_next_sensor()
        self.assertEqual(h.current_sensor_id, "B7-2")
        self.assertEqual(h.number_attempt, 2)
        self._write_verdict_row(h, passed=False)
        h._advance_to_next_sensor()
        self.assertEqual(h.current_sensor_id, "B7-2")
        self.assertEqual(h.number_attempt, 3)

        # The part that finally passes takes number 2 with it.
        self._write_verdict_row(h, passed=True)
        h._advance_to_next_sensor()
        self.assertEqual(h.current_sensor_id, "B7-3")

        with app.batch_results_path("B7").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(
            [(row["sensor_id"], row["pass_fail"], row["number_attempt"]) for row in rows],
            [
                ("B7-1", app.OUTCOME_PASS, "1"),
                ("B7-2", app.OUTCOME_FAIL, "1"),
                ("B7-2", app.OUTCOME_FAIL, "2"),
                ("B7-2", app.OUTCOME_PASS, "3"),
            ],
        )

    def test_next_reads_the_replacement_with_no_screen_in_between(self):
        app = self.app
        h = self.harness()
        h.current_sensor_number = 1
        h.prepare_current_sensor()
        h.last_result = self._verdict(passed=False)
        h.save_current_sensor = lambda: (
            self._write_verdict_row(h, passed=False) or True
        )
        h.go_next()
        # The verdict was written, the same number came back, and the rig was
        # read again straight away - no "load the next sensor" step.
        self.assertEqual(h.current_sensor_id, "B7-1")
        self.assertEqual(h.reads, ["B7-1"])
        self.assertEqual(h.shown, ["result"])

    def test_stop_during_a_capture_records_nothing_and_keeps_the_number(self):
        app = self.app
        h = self.harness()
        h.current_sensor_number = 5
        h.prepare_current_sensor()
        h.measuring = True
        h.busy = True
        h.last_result = self._verdict(passed=True)
        h.stop()
        self.assertFalse(h.measuring)
        self.assertFalse(h.busy)
        self.assertEqual(h.measure_token, 5)  # bumped: the capture is orphaned
        self.assertIsNone(h.last_result)
        self.assertEqual(h.current_sensor_id, "B7-5")
        self.assertFalse(app.batch_results_path("B7").exists())
        events = [e.event for e in app.attempt_history.read_attempts(h._attempts_path())]
        self.assertEqual(events, ["stopped"])
        self.assertEqual(h.measure_attempts, 1)

    def test_stop_when_idle_ends_the_batch(self):
        h = self.harness()
        h.current_sensor_number = 2
        h.prepare_current_sensor()
        h.ended = []
        h._end_batch = lambda: h.ended.append("end")
        h.stop()
        self.assertEqual(h.ended, ["end"])

    def test_attempt_log_counts_every_read_of_the_part_in_the_rig(self):
        app = self.app
        h = self.harness()
        h.current_sensor_number = 1
        h.prepare_current_sensor()
        failing = self._verdict(passed=False)
        h._log_attempt(app.attempt_history.EVENT_MEASURED, result=failing)
        h._log_attempt(app.attempt_history.EVENT_MEASURE_ERROR, reason="stream fault")
        self.assertEqual(h.measure_attempts, 2)
        rows = app.attempt_history.read_attempts(h._attempts_path())
        self.assertEqual([r.event for r in rows], ["measured", "measure_error"])
        self.assertEqual(rows[0].outcome, app.OUTCOME_FAIL)
        self.assertEqual(rows[0].fail_reasons, "sensitivity too low")
        self.assertEqual(rows[1].reason, "stream fault")
        # A replacement part under the same number starts its count over.
        self._write_verdict_row(h, passed=False)
        h._advance_to_next_sensor()
        self.assertEqual(h.current_sensor_id, "B7-1")
        self.assertEqual(h.measure_attempts, 0)

    def test_verdict_csv_carries_number_attempt_and_not_skip_count(self):
        app = self.app
        self.assertIn("measure_attempts", app.CSV_FIELDS)
        self.assertIn("number_attempt", app.CSV_FIELDS)
        self.assertNotIn("skip_count", app.CSV_FIELDS)
        path = app.batch_results_path("B9")
        app.append_result_csv(
            path,
            batch_number="B9", sensor_number=2, sensor_id="B9-2", tester_name="Tech",
            filter_setup=app.DEFAULT_FILTER_SETUP, pwm_channel="DAC0", pwm_hz=1.0,
            pwm_duty=50.0, final_result=self._verdict(passed=True), comment="",
            snapshot_paths=[], measure_attempts=3, number_attempt=2,
        )
        with path.open(newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["measure_attempts"], "3")
        self.assertEqual(row["number_attempt"], "2")


class NumberingFlow405M22Tests(_NumberingFlowMixin, unittest.TestCase):
    MODULE_DIR = "m405m22"
    MODULE_NAME = "eltec_405m22_esp32_tester"


class NumberingFlow406MCATests(_NumberingFlowMixin, unittest.TestCase):
    MODULE_DIR = "m406mca"
    MODULE_NAME = "eltec_406mca_esp32_tester"


class NumberingFlow449M18Tests(_NumberingFlowMixin, unittest.TestCase):
    MODULE_DIR = "m449m18"
    MODULE_NAME = "eltec_449m18_esp32_tester"


class FooterButtonTests(unittest.TestCase):
    """The footer palette and the two-button bar exist in every model."""

    def test_palettes_sizes_and_the_two_buttons(self):
        for module_dir, module_name in _MODEL_MODULES:
            with self.subTest(model=module_dir):
                app = _load_app(module_dir, module_name)
                self.assertIn("success", app.RoundButton.PALETTES)
                self.assertIn("warn", app.RoundButton.PALETTES)
                # Stop is red so it is findable mid-capture without reading it.
                self.assertIn("danger", app.RoundButton.PALETTES)
                self.assertIn("xl", app.RoundButton.SIZE_PADS)
                self.assertGreater(app.RoundButton.SIZE_PADS["xl"][0], app.RoundButton.SIZE_PADS["lg"][0])
                # The load step is gone with the skip queue.
                self.assertFalse(hasattr(app.EmitterTesterApp, "LOAD_STEP"))
                self.assertFalse(hasattr(app.EmitterTesterApp, "render_load_step"))
                for retired in (
                    "open_skip_window", "skip_current_part", "measure_skipped",
                    "_load_skipped_part", "go_back", "save_and_end_batch",
                ):
                    self.assertFalse(hasattr(app.EmitterTesterApp, retired), retired)


if __name__ == "__main__":
    unittest.main()
