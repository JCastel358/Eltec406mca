"""Operator workflows through real Tk controls and the no-hardware simulator.

All results, raw captures and event logs go to an explicit temporary root.
Worker threads and Tk's callback queue run normally so a deadlock, callback
exception or stale button state fails the tests.
"""

from __future__ import annotations

import csv
import gc
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

MODEL_DIR = Path(__file__).resolve().parents[1]
for entry in (str(MODEL_DIR), str(MODEL_DIR.parent)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import array_analysis as aa
import daq_backend as daq
import eltec_40623_array_tester as tester
import tray_history


class OperatorGuiTests(unittest.TestCase):
    def setUp(self):
        # Tk objects must be reclaimed on their creating thread, never by a
        # later simulator worker triggering Python's cyclic collector.
        gc.collect()
        try:
            probe = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        probe.destroy()
        self.temp = tempfile.TemporaryDirectory(prefix="eltec-array-gui-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {tester.RESULTS_ROOT_ENV: str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        _grid, app_class = tester.build_gui_classes()
        self.window = app_class(simulate=True)
        self.window.withdraw()
        self.callback_errors = []
        self.window.report_callback_exception = lambda kind, value, tb: self.callback_errors.append(value)
        self.addCleanup(self._close_window)

    def _close_window(self):
        self.window._cancel.set()
        worker = self.window._worker
        if worker is not None:
            worker.join(timeout=5)
        with patch("tkinter.messagebox.askyesno", return_value=True):
            if self.window.winfo_exists():
                self.window.on_close()
        self.window = None
        gc.collect()

    def _pump_until(self, condition, *, timeout=25):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.window.update()
            if self.callback_errors:
                self.fail(f"Tk callback failed: {self.callback_errors!r}")
            if condition():
                return
            time.sleep(.01)
        self.fail(f"Timed out: {self.window.status_var.get()}")

    def _wait_idle(self):
        self._pump_until(lambda: not self.window._busy)

    def _measure(self):
        self.window.offset_button.invoke()
        self._wait_idle()

    def _load(self):
        self.window.tester_var.set("Test Technician")
        self.window.lot_var.set("GUI-DEMO")
        self._measure()
        self.assertIsNotNone(self.window.controller)
        self.assertTrue(self.window.controller.offset_checked, self.window.status_var.get())

    def _ready(self):
        self._load()
        for position in self.window.controller.offset_bad_positions():
            self.window.on_tile_click(position)
        self._measure()
        self.assertEqual(self.window.controller.offset_bad_positions(), ())
        # Twenty seconds is enough for the actual noise algorithm; the
        # production/default capture plan is separately asserted as 60 s.
        self.window.controller.plan = tester.CapturePlan(
            capture_seconds=20, stabilisation_s=300, quiet_min_s=1, quiet_max_s=4,
        )
        self.window.vacuum_check.invoke()
        self.assertEqual(str(self.window.noise_button.cget("state")), "normal")

    def _noise(self):
        self.window.noise_button.invoke()
        self._wait_idle()

    def _detail(self, position):
        grid = self.window.grid
        return grid.itemcget(grid._items[position]["detail"], "text")

    def _colour(self, position):
        grid = self.window.grid
        return grid.itemcget(grid._items[position]["rect"], "fill")

    def test_name_and_batch_are_required_before_connecting(self):
        with patch.object(self.window.device, "connect", wraps=self.window.device.connect) as connect:
            for name, batch in (("", ""), ("Test Technician", ""), ("", "GUI-DEMO"), (" ", " ")):
                with self.subTest(name=name, batch=batch):
                    self.window.tester_var.set(name)
                    self.window.lot_var.set(batch)
                    self.window.offset_button.invoke()
                    self.assertIn("Enter technician name and batch", self.window.status_var.get())
                    self.assertIsNone(self.window.controller)
                    self.assertFalse(self.window._busy)
            connect.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_round_socket_text_fits_minimum_window(self):
        self.window.deiconify()
        self.window.state("normal")
        self.window.geometry("1000x720")
        self.window.grid.set_tile("1-10", state=aa.TileState.OFFSET_FAIL, headline="1.234 V", detail="ZERO")
        self.window.show_more_button.invoke()
        self.window.update()
        items = self.window.grid._items["1-10"]
        left, _, right, _ = self.window.grid.coords(items["rect"])
        for name in ("label", "headline", "detail"):
            box = self.window.grid.bbox(items[name])
            self.assertGreaterEqual(box[0], left-2)
            self.assertLessEqual(box[2], right+2)
        self.assertLessEqual(self.window.offset_button.winfo_rootx()+self.window.offset_button.winfo_width(),
                             self.window.winfo_rootx()+self.window.winfo_width())

    def test_offset_check_shows_high_low_and_dead_without_fifty_inputs(self):
        self._load()
        controller = self.window.controller
        self.assertEqual(len(self.window.entries), 2)
        self.assertEqual(controller.plan.capture_seconds, 60)
        self.assertEqual(controller.plan.stabilisation_s, 0)
        self.assertEqual(self._detail("1-10"), "AUTO")
        self.assertIs(controller.effective_occupancy("1-10"), aa.Occupancy.EMPTY)
        self.assertNotIn("1-10", controller.offset_bad_positions())
        self.assertEqual(controller.offset_measurement_count, 1)
        self.assertIn("2-4", controller.offset_bad_positions())
        self.assertIn("4-7", controller.offset_bad_positions())
        self.assertEqual(self._detail("2-4"), "HIGH")
        self.assertEqual(self._detail("1-1"), "OK")
        grid = self.window.grid
        offset_text = grid.itemcget(grid._items["1-1"]["headline"], "text")
        self.assertIn(" V", offset_text)
        self.assertIn(" V", grid.itemcget(grid._items["2-4"]["headline"], "text"))
        self.assertEqual(grid.itemcget(grid._items["1-1"]["detail"], "state"), "normal")
        self.window.show_more_button.invoke()
        self.assertIn(" V", grid.itemcget(grid._items["1-1"]["headline"], "text"))
        self.assertEqual(grid.itemcget(grid._items["2-4"]["detail"], "state"), "normal")
        self.assertEqual(self.window.show_more_button.cget("text"), "Show less")
        self.window.show_more_button.invoke()
        self.assertEqual(grid.itemcget(grid._items["1-1"]["headline"], "text"), offset_text)
        self.assertEqual(controller.offset_measurement_count, 1)
        self.assertTrue(any(self._detail(p) in ("LOW", "ZERO") for p in controller.offset_bad_positions()))
        self.assertFalse(controller.csv_path.exists())
        self.assertEqual(controller.results_root, self.root / "simulation")
        self.assertEqual(str(self.window.noise_button.cget("state")), "disabled")
        self.assertFalse(self.window.vacuum_check.winfo_manager())

    def test_detected_map_confirmation_and_empty_details_fit_minimum_window(self):
        self._ready()
        self.window.deiconify()
        self.window.state("normal")
        self.window.geometry("1000x720")
        self.window.show_more_button.invoke()
        self.window.update()
        check = self.window.vacuum_check
        self.assertLessEqual(check.winfo_reqwidth(), check.master.winfo_width())
        items = self.window.grid._items["1-10"]
        left, _, right, _ = self.window.grid.coords(items["rect"])
        for name in ("headline", "detail"):
            box = self.window.grid.bbox(items[name])
            self.assertGreaterEqual(box[0], left-2)
            self.assertLessEqual(box[2], right+2)
        self.window.toggle_empty("1-1")
        self.assertFalse(self.window.vacuum_var.get())
        self.assertEqual(str(self.window.noise_button.cget("state")), "disabled")

    def test_empty_slots_can_be_marked_before_first_measurement(self):
        self.window.on_tile_click("1-10")
        self.assertEqual(self._detail("1-10"), "EMPTY")
        self.window.on_tile_click("1-1")
        self.window.on_tile_click("1-1")
        self.assertEqual(self._detail("1-1"), "WAITING")
        self._load()
        self.assertIs(self.window.controller.effective_occupancy("1-10"), aa.Occupancy.EMPTY)
        self.assertNotIn("1-10", self.window.controller.offset_bad_positions())

    def test_choose_loaded_sockets_handles_sparse_tray_before_connecting(self):
        self.window.choose_loaded_button.invoke()
        dialog = next(child for child in self.window.winfo_children() if isinstance(child, tk.Toplevel))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        controls = list(descendants(dialog))
        next(child for child in controls if isinstance(child, tk.Button) and child.cget("text") == "Select none").invoke()
        next(child for child in controls if isinstance(child, tk.Checkbutton) and child.cget("text") == "5-1").invoke()
        next(child for child in controls if isinstance(child, tk.Button) and child.cget("text") == "Apply").invoke()
        self.assertEqual(self.window._loaded_positions, {"5-1"})
        self._load()
        controller = self.window.controller
        self.assertEqual(controller.offset_good_positions(), ("5-1",))
        self.assertEqual(controller.offset_bad_positions(), ())
        self.assertEqual(controller.occupancy_choice["5-1"], aa.Occupancy.LOADED)
        self.assertEqual(sum(v is aa.Occupancy.EMPTY for v in controller.occupancy_choice.values()), 49)
        self.assertIn("1 detectors detected", self.window.status_var.get())
        self.window.vacuum_check.invoke()
        self.assertTrue(self.window._can_noise())

    def test_changing_loaded_map_requires_fresh_offset_and_vacuum_confirmation(self):
        self._ready()
        self.window._apply_loaded_positions({"1-1", "1-2"})
        controller = self.window.controller
        self.assertFalse(controller.offset_checked)
        self.assertFalse(self.window.vacuum_var.get())
        self.assertEqual(str(self.window.noise_button.cget("state")), "disabled")
        self._measure()
        self.assertEqual(controller.offset_good_positions(), ("1-1", "1-2"))
        self.assertFalse(self.window.vacuum_var.get())
        self.window.vacuum_check.invoke()
        controller.plan = tester.CapturePlan(capture_seconds=20, quiet_min_s=1, quiet_max_s=4)
        self._noise()
        self.assertEqual(set(controller.state.report.by_position), {"1-1", "1-2"})
        self.assertEqual(str(self.window.choose_loaded_button.cget("state")), "disabled")
        with patch("tkinter.messagebox.showinfo") as details:
            self.window.on_tile_click("1-1")
        self.assertIn("Offset:", details.call_args.args[1])
        self.assertIn("peak-to-peak noise:", details.call_args.args[1])

    def test_offset_noise_and_status_fit_minimum_window(self):
        self._ready()
        self._noise()
        self.window.deiconify()
        self.window.state("normal")
        self.window.geometry("1000x720")
        self.window.update()
        grid = self.window.grid
        for position in ("1-1", "3-6", "4-3"):
            items = grid._items[position]
            left, top, right, bottom = grid.coords(items["rect"])
            previous_bottom = None
            for name in ("label", "headline", "noise", "detail"):
                box = grid.bbox(items[name])
                self.assertGreaterEqual(box[0], left-2, (position, name))
                self.assertLessEqual(box[2], right+2, (position, name))
                self.assertGreaterEqual(box[1], top-2, (position, name))
                self.assertLessEqual(box[3], bottom+2, (position, name))
                if previous_bottom is not None:
                    self.assertGreaterEqual(box[1], previous_bottom-1, (position, name))
                previous_bottom = box[3]

    def test_replacement_then_empty_edit_does_not_corrupt_measured_offsets(self):
        self._load()
        self.window.on_tile_click("2-4")
        self.window.toggle_empty("1-9")
        self.assertFalse(self.window.controller.offset_checked)
        self._measure()
        self.assertNotIn("2-4", self.window.controller.offset_bad_positions())
        self.assertEqual(self._detail("1-9"), "EMPTY")

    def test_replacements_require_remeasurement_and_vacuum_before_noise(self):
        self._load()
        original_bad = self.window.controller.offset_bad_positions()
        for position in original_bad:
            self.window.on_tile_click(position)
            self.assertEqual(self._detail(position), "RECHECK")
        self.assertFalse(self.window.controller.offset_checked)
        self.window.vacuum_var.set(True)
        self.window._refresh_controls()
        self.assertEqual(str(self.window.noise_button.cget("state")), "disabled")
        self._measure()
        self.assertEqual(self.window.controller.offset_bad_positions(), ())
        self.assertEqual(self.window.controller.offset_measurement_count, 2)
        self.assertEqual(len(self.window.controller.offset_good_positions()), 48)
        self.assertIn("48 detected positions", self.window.vacuum_check.cget("text"))
        self.assertFalse(self.window.vacuum_var.get())
        self.assertEqual(str(self.window.noise_button.cget("state")), "disabled")
        self.assertTrue(self.window.vacuum_check.winfo_manager())
        self.window.vacuum_check.invoke()
        self.assertEqual(str(self.window.noise_button.cget("state")), "normal")
        self._measure()
        self.assertFalse(self.window.vacuum_var.get())
        self.assertEqual(str(self.window.noise_button.cget("state")), "disabled")

    def test_partial_tray_excludes_removed_parts_and_empty_tray_cannot_run(self):
        self._load()
        controller = self.window.controller
        rejected = controller.offset_bad_positions()
        for position in rejected:
            self.window.toggle_empty(position)
            self.assertEqual(self._detail(position), "EMPTY")
        self.assertTrue(controller.offset_checked)
        self.assertEqual(controller.offset_bad_positions(), ())
        self.window.vacuum_check.invoke()
        self.assertTrue(self.window._can_noise())
        for position in controller.offset_good_positions():
            self.window.toggle_empty(position)
        self.assertEqual(controller.offset_good_positions(), ())
        self.assertEqual(str(self.window.noise_button.cget("state")), "disabled")
        self.assertIn("No detectors detected", self.window.status_var.get())
        self.window.on_tile_click("1-1")
        self.assertFalse(controller.offset_checked)
        self._measure()
        self.assertEqual(controller.offset_good_positions(), ("1-1",))
        controller.plan = tester.CapturePlan(capture_seconds=20, quiet_min_s=1, quiet_max_s=4)
        self.window.vacuum_check.invoke()
        self._noise()
        self.assertTrue(controller.state.saved, self.window.status_var.get())
        with controller.csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position"], "1-1")
        self.assertEqual(self._detail("1-2"), "EMPTY")

    def test_noise_autosaves_demo_red_green_low_then_next_tray_resets(self):
        self._ready()
        controller = self.window.controller
        self._noise()
        self.assertTrue(controller.state.saved, self.window.status_var.get())
        self.assertEqual(self._detail("1-1"), "PASS")
        self.assertEqual(self._detail("3-6"), "NOISY")
        self.assertEqual(self._detail("4-3"), "LOW")
        self.assertEqual(self._colour("1-1"), tester.OPERATOR_TILE_COLOURS[aa.TileState.PASS][0])
        self.assertEqual(self._colour("3-6"), tester.OPERATOR_TILE_COLOURS[aa.TileState.NOISE_FAIL][0])
        self.assertEqual(self._colour("4-3"), self._colour("3-6"))
        grid = self.window.grid
        self.assertIn(" V", grid.itemcget(grid._items["3-6"]["headline"], "text"))
        self.assertIn(" V", grid.itemcget(grid._items["1-1"]["headline"], "text"))
        self.assertIn("µV", grid.itemcget(grid._items["1-1"]["noise"], "text"))
        self.assertEqual(grid.itemcget(grid._items["1-1"]["number"], "state"), "hidden")
        saved_report = controller.state.report
        self.window.show_more_button.invoke()
        self.assertIn("µV", grid.itemcget(grid._items["1-1"]["noise"], "text"))
        self.assertEqual(grid.itemcget(grid._items["1-1"]["number"], "state"), "normal")
        self.window.show_more_button.invoke()
        self.assertIs(controller.state.report, saved_report)
        with controller.csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 48)
        self.assertTrue(all(float(row["stabilisation_wait_s"]) == 0 for row in rows))
        self.assertTrue(all(row["noise_timing_policy"] == tester.NOISE_TIMING_POLICY for row in rows))
        self.assertTrue(all(row["simulated"] == "YES" for row in rows))
        self.assertTrue(list((self.root / "simulation").rglob("*.npz")))
        events = tray_history.read_tray_events(controller.attempts_path)
        self.assertEqual(sum(e.event == "vacuum_confirmed" for e in events), 1)
        self.assertIn("physical tray", next(e.detail for e in events if e.event == "vacuum_confirmed"))
        self.assertEqual(sum(e.event == "noise_settled" for e in events), 1)
        self.assertIn("SIMULATION", self.window.banner.cget("text"))
        self.assertEqual(self.window.offset_button.cget("text"), "Next tray")
        self.window.offset_button.invoke()
        self.assertIsNone(self.window.controller)
        self.assertFalse(self.window.vacuum_var.get())
        self.assertEqual(self.window.tester_var.get(), "Test Technician")
        self.assertEqual(self.window.lot_var.get(), "GUI-DEMO")
        self.assertIn("Tray 2", self.window.tray_var.get())
        self.assertEqual(self._detail("3-6"), "WAITING")
        self.assertEqual(str(self.window.entries[0].cget("state")), "normal")
        self.assertEqual(str(self.window.noise_button.cget("state")), "disabled")
        self._measure()
        self.assertEqual(self.window.controller.state.tray_number, 2)
        self.assertIn("2-4", self.window.controller.offset_bad_positions())

    def test_stop_cancels_without_verdict_then_allows_edit_recheck_and_retry(self):
        self._ready()
        controller = self.window.controller
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original_read = self.window.device.read_stream

        def delayed_read(**kwargs):
            entered.set()
            release.wait(5)
            return original_read(**kwargs)

        with patch.object(self.window.device, "read_stream", side_effect=delayed_read):
            self.window.noise_button.invoke()
            self._pump_until(entered.is_set)
            self.assertEqual(str(self.window.offset_button.cget("state")), "disabled")
            self.assertTrue(self.window.stop_button.winfo_manager())
            self.window.stop_button.invoke()
            self.assertTrue(self.window._cancel.is_set())
            release.set()
            self._wait_idle()
        self.assertIsNone(controller.state.report)
        self.assertFalse(controller.state.saved)
        self.assertFalse(controller.csv_path.exists())
        self.assertFalse(self.window.device.is_streaming)
        numbers_before_retry = dict(controller.state.lock.sensor_numbers)
        self.assertEqual(str(self.window.noise_button.cget("state")), "normal")
        # An unchanged tray keeps its accepted snapshot and assigned numbers.
        self._noise()
        self.assertTrue(controller.state.saved, self.window.status_var.get())
        self.assertEqual(controller.state.lock.sensor_numbers, numbers_before_retry)

    def test_stopped_tray_can_be_edited_and_rechecked(self):
        self._ready()
        controller = self.window.controller
        controller.prepare_noise()
        controller.phase = tester.Phase.CAPTURING
        self.window._stopped()
        self.window.toggle_empty("1-2")
        self.assertIs(controller.effective_occupancy("1-2"), aa.Occupancy.EMPTY)
        self._measure()
        self.window.vacuum_check.invoke()
        self._noise()
        self.assertTrue(controller.state.saved, self.window.status_var.get())
        self.assertNotIn("1-2", controller.state.report.by_position)

    def test_save_failure_keeps_results_and_retry_saves_without_recapture(self):
        self._ready()
        controller = self.window.controller
        with patch.object(controller, "save_tray", side_effect=OSError("test storage failure")):
            self._noise()
        report = controller.state.report
        self.assertIsNotNone(report)
        self.assertFalse(controller.state.saved)
        self.assertEqual(self.window.offset_button.cget("text"), "Retry save")
        self.assertIn("not saved", self.window.footer_var.get())
        self.assertEqual(str(self.window.noise_button.cget("state")), "disabled")
        self.window.next_tray()
        self.assertIs(self.window.controller, controller)
        with patch.object(controller, "run_noise_phase", side_effect=AssertionError("must not recapture")):
            self.window.offset_button.invoke()
            self._wait_idle()
        self.assertIs(controller.state.report, report)
        self.assertTrue(controller.state.saved, self.window.status_var.get())
        self.assertEqual(self.window.offset_button.cget("text"), "Next tray")
        events = tray_history.read_tray_events(controller.attempts_path)
        self.assertEqual(sum(event.event == "capture_started" for event in events), 1)
        self.assertEqual(sum(event.event == "saved" for event in events), 1)

    def test_unset_production_noise_limits_render_no_limit_not_pass(self):
        self._ready()
        self.window.simulate = False
        c = self.window.controller
        c.noise_limits = aa.NoiseLimits()
        c.plan = tester.CapturePlan(capture_seconds=60, stabilisation_s=0, quiet_min_s=1, quiet_max_s=4)
        self._noise()
        self.assertTrue(c.state.saved, self.window.status_var.get())
        self.assertEqual(self._detail("1-1"), "REVIEW")
        result = c.state.report.by_position["1-1"]
        self.assertIsNotNone(result.noise.legacy)
        self.assertFalse(result.passed)
        self.assertIn(" V", self.window.grid.itemcget(self.window.grid._items["1-1"]["headline"], "text"))
        self.assertIn("µV", self.window.grid.itemcget(self.window.grid._items["1-1"]["noise"], "text"))
        self.assertEqual(self._colour("1-1"), tester.OPERATOR_TILE_COLOURS[aa.TileState.NO_LIMIT][0])
        self.assertIn("no calibrated pass/fail", self.window.status_var.get())
        self.assertIn("3 Hz band RMS", self.window.map_hint_var.get())
        with patch("tkinter.messagebox.showinfo") as details:
            self.window.on_tile_click("1-1")
        self.assertIn("CALIBRATION PENDING", details.call_args.args[1])
        self.assertIn("3 Hz band RMS noise:", details.call_args.args[1])
        self.assertIn("detector-referred", details.call_args.args[1])

    def test_short_noise_capture_keeps_metrics_visible_without_acceptance(self):
        self._ready()
        self.window.simulate = False
        controller = self.window.controller
        controller.noise_limits = aa.NoiseLimits()
        self._noise()  # The engineering capture configured by _ready lasts 20 s.
        result = controller.state.report.by_position["1-1"]
        self.assertFalse(result.passed)
        self.assertEqual(self._detail("1-1"), "NOT READ")
        self.assertIn("µV", self.window.grid.itemcget(self.window.grid._items["1-1"]["noise"], "text"))
        self.assertIn("measurement review", self.window.status_var.get())
        with patch("tkinter.messagebox.showinfo") as details:
            self.window.on_tile_click("1-1")
        self.assertIn("60", details.call_args.args[1])

    def test_closing_during_save_failure_preserves_unsaved_measurement(self):
        self._ready()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def fail_save():
            entered.set()
            release.wait(5)
            raise OSError("test save failure while closing")

        with patch.object(self.window.controller, "save_tray", side_effect=fail_save):
            self.window.noise_button.invoke()
            self._pump_until(entered.is_set)
            self.window.on_close()
            release.set()
            self._pump_until(lambda: not self.window._busy and not self.window._closing)
        self.assertTrue(self.window.winfo_exists())
        self.assertTrue(self.window._save_error)
        self.assertIsNotNone(self.window.controller.state.report)
        self.assertEqual(self.window.offset_button.cget("text"), "Retry save")


if __name__ == "__main__":
    unittest.main()
