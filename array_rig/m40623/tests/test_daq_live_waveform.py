"""Tests for daq_live_waveform.py - the graphical live viewer of the DAQ (window ladder, selection, judged trace, tiles, viewer, CLI).

Everything runs on matplotlib's Agg backend (selected before the viewer is
imported, so no window ever opens) against an injected
``SimulatedDaq(real_time=False)`` - a virtual clock, so the ring buffer
fills in milliseconds - except the CLI cases, which go through
``--simulate`` (the wall-clock simulator the real command line uses) with a
sub-second ``--exit-after``. Output files go to temporary directories; a
guard checks nothing landed under the technician's Documents folder. No test
ever constructs the hardware device.
"""

from __future__ import annotations

import ast
import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
MODEL_DIR = TESTS_DIR.parent
RIG_DIR = MODEL_DIR.parent
for entry in (str(MODEL_DIR), str(RIG_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

try:
    import matplotlib
except ImportError as exc:  # pragma: no cover - the bench laptop has matplotlib
    raise unittest.SkipTest(f"matplotlib is not installed: {exc}")
matplotlib.use("Agg")  # before the viewer imports pyplot: headless, no window
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402

import array_analysis as aa  # noqa: E402
import daq_backend as daq  # noqa: E402
import daq_live_waveform as lv  # noqa: E402
import daq_rig_readout as readout  # noqa: E402
import eltec_40623_array_tester as app  # noqa: E402  (read for its tile colours; the viewer must not import it)

HOME_RESULTS = Path.home() / "Documents" / "Eltec_40623_Test_Results"
# The simulator's offsets settle upward for ~8 s after power-on; at this
# virtual time the profile values are exact.
SETTLED_T = 200.0


def home_signature() -> set[str]:
    if not HOME_RESULTS.exists():
        return set()
    return {str(p) for p in HOME_RESULTS.rglob("*")}


_HOME_BEFORE = home_signature()


class HomeGuardMixin:
    def assert_home_untouched(self) -> None:
        self.assertEqual(home_signature(), _HOME_BEFORE, "the tests wrote into the technician's Documents folder")


def make_rig(profile: daq.SimProfile | None = None, **kwargs):
    sim = daq.SimulatedDaq(profile, real_time=False)
    sim._virtual_t = SETTLED_T
    return readout.ArrayRig(device=sim, quiet=True, **kwargs), sim


def wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = lv.main(argv)
    return code, out.getvalue(), err.getvalue()


def key(name: str) -> SimpleNamespace:
    return SimpleNamespace(key=name)


class FailingStartDaq(daq.SimulatedDaq):
    def start_stream(self, **kwargs):
        raise daq.DaqStatusError("ADC_BulkContinuousCallbackStart", 5)


# ----------------------------------------------------------------------
# Window ladder (the ESP32 viewer's numbers)
# ----------------------------------------------------------------------
class WindowLadderTests(unittest.TestCase):
    def test_presets_are_the_esp32_viewer_rungs(self):
        self.assertEqual(lv.WINDOW_PRESETS_S, (0.25, 0.5, 1.0, 2.0, 4.0, 6.0, 8.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0))
        self.assertEqual(lv.MAX_WINDOW_S, 60.0)
        self.assertEqual(lv.MAX_WINDOW_S, readout.DEFAULT_LIVE_BUFFER_S)
        self.assertEqual(lv.DEFAULT_WINDOW_S, 4.0)

    def test_ladder_clamps_to_the_ceiling_and_adds_the_start(self):
        self.assertEqual(lv.window_ladder(4.0, 60.0), list(lv.WINDOW_PRESETS_S))
        self.assertEqual(lv.window_ladder(3.0, 10.0), [0.25, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0])
        self.assertEqual(lv.window_ladder(7.0, 5.0), [0.25, 0.5, 1.0, 2.0, 4.0, 6.0, 7.0])  # ceiling raised to the start
        self.assertEqual(lv.window_ladder(120.0, 120.0)[-1], 120.0)
        for bad in (0.0, -1.0, float("nan")):
            with self.assertRaises(ValueError):
                lv.window_ladder(bad, 60.0)

    def test_stepping_clamps_at_the_ends_and_the_button_wraps(self):
        ladder = lv.window_ladder(4.0, 60.0)
        self.assertEqual(lv.step_window(4.0, ladder, +1), 6.0)
        self.assertEqual(lv.step_window(4.0, ladder, -1), 2.0)
        self.assertEqual(lv.step_window(60.0, ladder, +1), 60.0)
        self.assertEqual(lv.step_window(0.25, ladder, -1), 0.25)
        self.assertEqual(lv.step_window(5.0, ladder, +1), 6.0)   # off-ladder value: nearest rung in that direction
        self.assertEqual(lv.step_window(5.0, ladder, -1), 4.0)
        self.assertEqual(lv.step_window(4.0, ladder, 0), 4.0)
        self.assertEqual(lv.cycle_window(4.0, ladder), 6.0)
        self.assertEqual(lv.cycle_window(60.0, ladder), 0.25)   # wraps back to the narrowest
        short = lv.window_ladder(4.0, 10.0)
        self.assertEqual(lv.step_window(10.0, short, +1), 10.0)  # clamped at --max-window
        self.assertEqual(lv.cycle_window(10.0, short), 0.25)


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------
class SelectionTests(unittest.TestCase):
    def test_tokens_rows_columns_and_indices(self):
        sel = lv.Selection.from_token("2-4", 0, 49)
        self.assertEqual((sel.channel, sel.row, sel.col, sel.index, sel.label, sel.on_grid), (13, 2, 4, 13, "2-4", True))
        self.assertEqual(lv.Selection.from_token("CH13", 0, 49).channel, 13)
        self.assertEqual(lv.Selection.from_token("13", 0, 49).channel, 13)
        self.assertEqual(lv.Selection.from_token(13, 0, 49).channel, 13)
        self.assertEqual(lv.Selection.from_token("5-10", 0, 49).channel, 49)
        self.assertEqual(lv.Selection.from_token("1-1", 0, 49).channel, 0)
        narrow = lv.Selection.from_token("2-4", 10, 19)
        self.assertEqual(narrow.index, 3)
        with self.assertRaises(ValueError) as ctx:
            lv.Selection.from_token("1-1", 10, 19)
        self.assertIn("outside the scanned range CH10-CH19", str(ctx.exception))
        with self.assertRaises(ValueError):
            lv.Selection.from_token("9-9", 0, 49)
        with self.assertRaises(ValueError):
            lv.Selection(5, 10, 3)
        above = lv.Selection(55, 0, 63)
        self.assertEqual((above.label, above.row, above.col, above.on_grid, above.index), ("CH55", None, None, False, 55))

    def test_arrow_moves_wrap_on_the_grid(self):
        sel = lv.Selection(13)                  # 2-4
        self.assertEqual(sel.move(0, +1), 14)   # 2-5
        self.assertEqual(sel.move(0, -1), 13)
        self.assertEqual(sel.move(-1, 0), 3)    # 1-4
        self.assertEqual(sel.move(-1, 0), 43)   # wraps to 5-4
        self.assertEqual(sel.move(+1, 0), 3)    # back to 1-4
        sel.select(9)                           # 1-10
        self.assertEqual(sel.move(0, +1), 0)    # wraps to 1-1
        self.assertEqual(sel.move(0, -1), 9)
        self.assertEqual(sel.row, 1)
        self.assertEqual(sel.col, 10)

    def test_next_and_prev_wrap_within_the_scan(self):
        sel = lv.Selection(49)
        self.assertEqual(sel.next(), 0)
        self.assertEqual(sel.prev(), 49)
        narrow = lv.Selection(19, 10, 19)
        self.assertEqual(narrow.next(), 10)
        self.assertEqual(narrow.prev(), 19)
        wide = lv.Selection(49, 0, 55)
        self.assertEqual(wide.next(), 50)
        self.assertEqual(wide.label, "CH50")
        self.assertEqual(wide.move(0, +1), 51)  # no grid position: arrows act like n / p
        self.assertEqual(wide.move(-1, 0), 50)

    def test_moves_skip_positions_outside_the_scan(self):
        sel = lv.Selection(13, 10, 19)          # only row 2 scanned
        self.assertEqual(sel.move(+1, 0), 13)   # every other row is outside: stays put
        self.assertEqual(sel.move(0, +1), 14)
        edge = lv.Selection(19, 10, 19)
        self.assertEqual(edge.move(0, +1), 10)  # 2-10 wraps to 2-1
        partial = lv.Selection(5, 5, 14)        # 1-6 .. 2-5 scanned
        self.assertEqual(partial.move(+1, 0), 5)    # 2-6, 3-6, 4-6, 5-6 are outside; wraps to itself
        self.assertEqual(partial.move(0, -1), 9)    # 1-5 .. 1-1 are outside; wraps to 1-10
        with self.assertRaises(ValueError):
            partial.select(4)


# ----------------------------------------------------------------------
# Judged-band trace and grid metric (parity with array_analysis)
# ----------------------------------------------------------------------
class JudgedBandTraceTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        n = 310 + 3000
        t = np.arange(n) / 1000.0
        # 4 mV pk-pk 5 Hz tone (inside the judged band) on a 0.7 V offset with
        # a slow drift (the detrend removes it) and 60 uV rms white noise
        self.v = 0.7 + 0.05 * t + 2e-3 * np.sin(2 * np.pi * 5.0 * t) + rng.normal(0.0, 60e-6, n)

    def test_window_pp_equals_the_analysis_pipeline_on_the_same_samples(self):
        t_rel, mv, pp = lv.judged_band_trace(self.v, 1000.0, context_samples=310)
        expected = aa.band_limited_window_pp_mv(self.v[310:], 1000.0, left_context=self.v[:310])
        self.assertTrue(np.array_equal(pp, expected[0]))
        self.assertEqual(pp.shape, (3,))
        self.assertEqual(mv.shape, (150,))
        self.assertEqual(t_rel.shape, (150,))
        filtered = aa.decimate_antialiased_multi(self.v[310:][None, :], aa.NOISE_DECIMATION_FACTOR,
                                                 left_context=self.v[:310][None, :])
        judged = aa.detrend_window_segments_multi(filtered, 50)[0] * 1000.0
        self.assertTrue(np.array_equal(mv, judged))
        self.assertTrue(np.all(np.abs(pp - 4.0) < 0.6))   # the tone comes through the band

    def test_time_axis_ends_at_now_with_block_centres(self):
        t_rel, _mv, _pp = lv.judged_band_trace(self.v, 1000.0, context_samples=310)
        self.assertAlmostEqual(t_rel[0], (10 - 2999) / 1000.0)
        self.assertAlmostEqual(t_rel[-1], -0.009)
        self.assertTrue(np.allclose(np.diff(t_rel), 0.02))

    def test_newest_whole_seconds_are_judged_when_the_window_is_not_a_multiple(self):
        v = self.v[: 310 + 2456]
        t_rel, mv, pp = lv.judged_band_trace(v, 1000.0, context_samples=310)
        self.assertEqual(pp.shape, (2,))
        self.assertEqual(mv.shape, (100,))
        expected = aa.band_limited_window_pp_mv(v[-2000:], 1000.0, left_context=v[:-2000])
        self.assertTrue(np.array_equal(pp, expected[0]))
        self.assertAlmostEqual(t_rel[-1], -0.009)
        self.assertAlmostEqual(t_rel[0], (10 - 1999) / 1000.0)

    def test_context_seats_the_fir_without_changing_the_judged_windows(self):
        _t, _mv, all_three = lv.judged_band_trace(self.v, 1000.0, context_samples=310)
        _t, _mv, newest_two = lv.judged_band_trace(self.v, 1000.0, context_samples=400)   # 2910 in the window -> 2 windows
        self.assertEqual(newest_two.shape, (2,))
        # the same real neighbours seat the FIR either way, so the newest two windows agree
        self.assertTrue(np.allclose(newest_two, all_three[1:], rtol=0.0, atol=1e-9))

    def test_too_short_for_a_window_is_empty(self):
        for samples, context in ((self.v[:900], 0), (self.v[:1300], 310), (self.v[:0], 0)):
            t_rel, mv, pp = lv.judged_band_trace(samples, 1000.0, context_samples=context)
            self.assertEqual((t_rel.size, mv.size, pp.size), (0, 0, 0))
        with self.assertRaises(ValueError):
            lv.judged_band_trace(self.v, 0.0, context_samples=0)


class GridMetricTests(unittest.TestCase):
    def test_mean_and_noise_metrics(self):
        rng = np.random.default_rng(3)
        block = 0.7 + rng.normal(0.0, 60e-6, (3, 4310))
        block[1] += 1.0
        means = lv.grid_metric_values(block, 1000.0, "mean")
        np.testing.assert_allclose(means, block[:, -1000:].mean(axis=1))
        noise = lv.grid_metric_values(block, 1000.0, "noise")
        expected = aa.band_limited_window_pp_mv(block[:, 310:], 1000.0, left_context=block[:, :310]).max(axis=1)
        self.assertTrue(np.array_equal(noise, expected))
        self.assertEqual(noise.shape, (3,))
        longer = np.concatenate([block, block], axis=1)   # only the newest GRID_NOISE_WINDOWS windows count
        again = lv.grid_metric_values(longer, 1000.0, "noise")
        expected = aa.band_limited_window_pp_mv(longer[:, -4000:], 1000.0, left_context=longer[:, :-4000]).max(axis=1)
        self.assertTrue(np.array_equal(again, expected))
        self.assertEqual(lv.GRID_NOISE_WINDOWS, 4)
        self.assertTrue(np.all(np.isnan(lv.grid_metric_values(block[:, :900], 1000.0, "noise"))))
        self.assertTrue(np.all(np.isnan(lv.grid_metric_values(block[:, :0], 1000.0, "mean"))))
        np.testing.assert_allclose(lv.grid_metric_values(block[:, :10], 1000.0, "mean"), block[:, :10].mean(axis=1))
        with self.assertRaises(ValueError):
            lv.grid_metric_values(block, 1000.0, "bogus")


# ----------------------------------------------------------------------
# Tile colours and texts
# ----------------------------------------------------------------------
class TileColourTests(unittest.TestCase):
    def test_offset_bands_follow_the_analysis_constants(self):
        eps = 1e-3
        cases = (
            (float("nan"), lv.TILE_UNAVAILABLE),
            (0.0, lv.TILE_DEAD),
            (aa.OFFSET_DEAD_V - eps, lv.TILE_DEAD),
            (aa.OFFSET_DEAD_V, lv.TILE_LOW),
            (aa.OFFSET_MIN_V - eps, lv.TILE_LOW),
            (aa.OFFSET_MIN_V, lv.TILE_IN_BAND),
            (0.7, lv.TILE_IN_BAND),
            (aa.OFFSET_MAX_V, lv.TILE_IN_BAND),
            (aa.OFFSET_MAX_V + eps, lv.TILE_HIGH),
            (aa.OFFSET_RAIL_V - eps, lv.TILE_HIGH),
            (aa.OFFSET_RAIL_V, lv.TILE_RAILED),
            (4.995, lv.TILE_RAILED),
        )
        for volts, expected in cases:
            self.assertEqual(lv.offset_tile_colour(volts), expected, volts)

    def test_tile_colours_are_the_testers_by_value(self):
        self.assertEqual(lv.TILE_UNAVAILABLE, app.GRID_COLOURS[aa.TileState.NOT_MEASURED])
        self.assertEqual(lv.TILE_DEAD, app.GRID_COLOURS[aa.TileState.EMPTY])
        self.assertEqual(lv.TILE_LOW, app.GRID_COLOURS[aa.TileState.SETTLING])
        self.assertEqual(lv.TILE_IN_BAND, app.GRID_COLOURS[aa.TileState.LOADED])
        self.assertEqual(lv.TILE_HIGH, app.GRID_COLOURS[aa.TileState.OFFSET_FAIL])
        self.assertEqual(lv.ELTEC_BLUE, app.ELTEC_BLUE)
        self.assertEqual(lv.TEXT_DARK, app.TEXT_DARK)

    def test_noise_scale_and_sequential_colours(self):
        self.assertEqual(lv.nice_ceiling(0.063), 0.07)
        self.assertEqual(lv.nice_ceiling(0.31), 0.5)
        self.assertEqual(lv.nice_ceiling(1.7), 2.0)
        self.assertEqual(lv.nice_ceiling(7.0), 7.0)
        for value in (0.0, -1.0, float("nan"), lv.NOISE_SCALE_FLOOR_MV):
            self.assertEqual(lv.nice_ceiling(value), lv.NOISE_SCALE_FLOOR_MV)
        quiet = lv.noise_tile_colour(0.0, 1.0)
        loud = lv.noise_tile_colour(1.0, 1.0)
        self.assertEqual(lv.noise_tile_colour(float("nan"), 1.0), lv.TILE_UNAVAILABLE)
        self.assertGreater(sum(to_rgb(quiet[0])), sum(to_rgb(loud[0])))   # darker = noisier
        self.assertEqual(loud[1], "#ffffff")
        self.assertEqual(quiet[1], lv.TEXT_DARK)
        self.assertEqual(lv.noise_tile_colour(2.0, 1.0), loud)             # clipped at the scale top
        self.assertEqual(lv.noise_tile_colour(0.5, 0.0), lv.noise_tile_colour(0.5, lv.NOISE_SCALE_FLOOR_MV))
        for bg, _fg in (quiet, loud, lv.noise_tile_colour(0.5, 1.0)):
            self.assertNotIn(bg, (lv.TILE_HIGH[0], lv.TILE_RAILED[0]))     # never a pass/fail colour

    def test_tile_value_text_and_grid_titles(self):
        self.assertEqual(lv.tile_value_text(0.7, "mean"), "0.700 V")
        self.assertEqual(lv.tile_value_text(0.0574, "noise"), "0.057 mV")
        self.assertEqual(lv.tile_value_text(float("nan"), "mean"), "no data")
        self.assertEqual(lv.tile_value_text(float("nan"), "noise"), "< 1 s")
        mean_title = lv.grid_title("mean")
        self.assertIn("never a verdict", mean_title)
        self.assertIn(f"{aa.OFFSET_MIN_V:.1f}-{aa.OFFSET_MAX_V:.1f} V", mean_title)
        self.assertIn(aa.OFFSET_LIMITS_STATUS, mean_title)
        noise_title = lv.grid_title("noise", scale_mv=0.07)
        self.assertIn("NO LIMIT", noise_title)
        self.assertIn("never a verdict", noise_title)
        self.assertIn("scale 0-0.07 mV", noise_title)
        for text in (mean_title, noise_title):
            self.assertNotIn("PASS", text)
            self.assertNotIn("FAIL", text)


# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------
class FormatTests(unittest.TestCase):
    def stats(self, **overrides) -> readout.LiveStats:
        values = dict(total_scans=4000, elapsed_s=4.0, rate_hz=999.9, lag_s=0.02, chunks=123, error=None,
                      diagnostics=None, running=True)
        values.update(overrides)
        return readout.LiveStats(**values)

    def test_format_stats(self):
        text = lv.format_stats(label="2-4", channel=13, mean_v=0.70123, raw_pp_mv=1.234, judged_pp_mv=[0.31, 0.28, 0.30],
                               stats=self.stats(), simulated=True)
        self.assertIn("pos 2-4 CH13", text)
        self.assertIn("mean 0.7012 V", text)
        self.assertIn("raw pk-pk 1.23 mV", text)
        self.assertIn("judged 0.310/0.300 mV (worst/median 1 s window)", text)
        self.assertIn("rate 999.9 scans/s", text)
        self.assertIn("lag 0.02 s", text)
        self.assertIn("chunks 123", text)
        self.assertIn("[SIM]", text)
        self.assertNotIn("HOLD", text)
        self.assertNotIn("ERROR", text)
        plain = lv.format_stats(label="1-1", channel=0, mean_v=0.7, raw_pp_mv=0.4, judged_pp_mv=None,
                                stats=self.stats(error="read_stream failed: boom"), simulated=False, held=True)
        self.assertNotIn("[SIM]", plain)
        self.assertIn("judged n/a (needs >= 1 s)", plain)
        self.assertIn("HOLD", plain)
        self.assertIn("ERROR: read_stream failed: boom", plain)
        self.assertIn("judged n/a", lv.format_stats(label="1-1", channel=0, mean_v=0.7, raw_pp_mv=0.4,
                                                    judged_pp_mv=np.empty(0), stats=self.stats(), simulated=False))

    def test_format_window_pp(self):
        text = lv.format_window_pp([0.0574, 0.052, 0.0601])
        self.assertEqual(text, "1 s windows (oldest -> newest): 0.057 0.052 0.060 mV pk-pk; worst 0.060, median 0.057 (3 windows)")
        self.assertIn("(1 window)", lv.format_window_pp([0.1]))
        self.assertEqual(lv.format_window_pp([]), "")
        long = lv.format_window_pp(np.arange(1, 21) / 100.0)
        self.assertIn("...", long)
        self.assertIn("0.200 mV pk-pk", long)
        self.assertNotIn("0.001 ", long)
        self.assertIn("(20 windows)", long)

    def test_format_closing_line(self):
        diagnostics = daq.StreamDiagnostics(nominal_scan_hz=1000.0, actual_timer_hz=1000.0, started_monotonic=0.0,
                                            stopped_monotonic=4.0, scans_received=4000, buffers_received=40)
        text = lv.format_closing_line(self.stats(diagnostics=diagnostics, running=False))
        self.assertTrue(text.startswith("Stream closed: 4000 scans received, rate 999.9 scans/s, lag 0.02 s, 123 chunks"))
        self.assertIn(diagnostics.summary(), text)
        self.assertTrue(text.endswith("integrity OK"))
        bad = daq.StreamDiagnostics(nominal_scan_hz=1000.0, actual_timer_hz=1000.0, started_monotonic=0.0,
                                    stopped_monotonic=4.0, scans_received=4000, pool_too_small_events=2)
        text = lv.format_closing_line(self.stats(diagnostics=bad, error="read_stream failed: boom"))
        self.assertIn("integrity driver buffer pool exhausted 2 time(s)", text)
        self.assertIn("ERROR: read_stream failed: boom", text)
        self.assertIn("no stream diagnostics", lv.format_closing_line(self.stats()))

    def test_save_path_is_stamped_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            when = __import__("datetime").datetime(2026, 9, 2, 13, 5, 9)
            first = lv.save_path(tmp, when)
            self.assertEqual(first.name, "daq_live_20260902_130509.npz")
            first.write_bytes(b"x")
            self.assertEqual(lv.save_path(tmp, when).name, "daq_live_20260902_130509_2.npz")


# ----------------------------------------------------------------------
# The viewer on the Agg backend
# ----------------------------------------------------------------------
class ViewerTests(HomeGuardMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rig, self.sim = make_rig()
        self.rig.connect()
        self.live = self.rig.live_stream(buffer_s=8.0)
        self.live.start()
        self.assertTrue(self.live.wait_ready(5.0))
        self.assertTrue(wait_until(lambda: self.live.stats().total_scans >= 9000))
        self.viewer = self.build(["-w", "4", "--position", "2-4"])

    def tearDown(self):
        self.viewer.close()
        self.rig.close()
        plt.close("all")
        self.tmp.cleanup()
        self.assert_home_untouched()

    def build(self, extra: list[str]) -> lv.Viewer:
        args = lv.build_parser().parse_args([*extra, "--save-dir", self.tmp.name])
        lv._validate_args(args)
        return lv.build_viewer(self.rig, self.live, args)

    def test_trace_stats_judged_panel_and_grid_after_updates(self):
        v = self.viewer
        for frame in range(6):
            v.tick(frame)
        x, y = v.trace.get_xdata(), v.trace.get_ydata()
        self.assertEqual(x.size, 4000)
        self.assertEqual(x[-1], 0.0)
        self.assertGreaterEqual(x[0], -4.0)
        self.assertAlmostEqual(float(np.mean(y)), 1.62, delta=0.005)     # 2-4 is the HO part of the default profile
        self.assertEqual(v.ax_trace.get_xlim(), (-4.0, 0.0))
        lo, hi = v.ax_trace.get_ylim()
        self.assertLess(lo, y.min())
        self.assertGreater(hi, y.max())
        title = v.ax_trace.get_title()
        self.assertIn("2-4 (CH13)", title)
        self.assertIn("4 s window", title)
        self.assertIn("SIMULATED USB-AIO16-64MA", title)
        stats = v.stats_text.get_text()
        self.assertIn("pos 2-4 CH13", stats)
        self.assertIn("mean 1.62", stats)
        self.assertIn("[SIM]", stats)
        self.assertIn("judged ", stats)
        self.assertNotIn("HOLD", stats)
        # the judged panel: 4 complete windows over the 4 s window
        self.assertEqual(v.judged_line.get_xdata().size, 200)
        self.assertFalse(v.judged_hint.get_visible())
        self.assertIn("(4 windows)", v.judged_text.get_text())
        self.assertEqual(v.ax_judged.get_xlim(), (-4.0, 0.0))
        self.assertIn("judged band 0.85-22 Hz", v.ax_judged.get_title(loc="left"))
        # the grid: every scanned position has a value and the default profile's colours
        self.assertEqual(v.grid_values.shape, (50,))
        self.assertTrue(np.all(np.isfinite(v.grid_values)))
        rgb = v.grid_image.get_array()
        self.assertEqual(rgb.shape, (5, 10, 3))
        np.testing.assert_allclose(rgb[1, 3], to_rgb(lv.TILE_HIGH[0]))      # 2-4: 1.62 V
        np.testing.assert_allclose(rgb[2, 0], to_rgb(lv.TILE_RAILED[0]))    # 3-1: railed
        np.testing.assert_allclose(rgb[3, 6], to_rgb(lv.TILE_LOW[0]))       # 4-7: 0.21 V
        np.testing.assert_allclose(rgb[4, 9], to_rgb(lv.TILE_DEAD[0]))      # 5-10: empty socket
        np.testing.assert_allclose(rgb[0, 0], to_rgb(lv.TILE_IN_BAND[0]))   # 1-1: 0.70 V
        self.assertEqual(v.tile_texts[13].get_text(), "2-4\n1.620 V")
        self.assertEqual(v.selection_outline.get_xy(), (3.5, 1.5))
        self.assertTrue(v.selection_outline.get_visible())
        self.assertIn("never a verdict", v.ax_grid.get_title(loc="left"))
        # fast frames blit; the slow refresh and label changes draw the whole figure
        self.assertGreaterEqual(v.full_draws, 1)
        self.assertGreaterEqual(v.blit_draws, 1)
        self.assertTrue(v.trace.get_animated())
        self.assertTrue(v.stats_text.get_animated())

    def test_selecting_another_position_by_api_keys_and_click(self):
        v = self.viewer
        v.tick(0)
        v.select_channel(36)                                     # 4-7
        v.tick(1)
        self.assertIn("4-7 (CH36)", v.ax_trace.get_title())
        self.assertIn("pos 4-7 CH36", v.stats_text.get_text())
        self.assertAlmostEqual(float(np.mean(v.trace.get_ydata())), 0.21, delta=0.005)
        self.assertEqual(v.selection_outline.get_xy(), (6.5, 3.5))
        self.assertIn("Pos 4-7 CH36", v.position_button.label.get_text())
        v.on_key(key("right"))
        self.assertEqual(v.selection.channel, 37)
        v.on_key(key("down"))
        self.assertEqual(v.selection.channel, 47)
        v.on_key(key("up"))
        self.assertEqual(v.selection.channel, 37)
        v.on_key(key("left"))
        self.assertEqual(v.selection.channel, 36)
        v.on_key(key("n"))
        self.assertEqual(v.selection.channel, 37)
        v.on_key(key("p"))
        self.assertEqual(v.selection.channel, 36)
        v.on_key(key(None))                                      # a bare modifier: ignored
        self.assertEqual(v.selection.channel, 36)
        v.on_click(SimpleNamespace(inaxes=v.ax_grid, button=1, xdata=4.2, ydata=1.8))   # tile 2-4
        self.assertEqual(v.selection.channel, 13)
        v.on_click(SimpleNamespace(inaxes=v.ax_trace, button=1, xdata=1.0, ydata=1.0))  # not the grid
        v.on_click(SimpleNamespace(inaxes=v.ax_grid, button=3, xdata=1.0, ydata=1.0))   # not the left button
        v.on_click(SimpleNamespace(inaxes=v.ax_grid, button=1, xdata=None, ydata=None))
        self.assertEqual(v.selection.channel, 13)
        self.assertEqual(v.next_position(), 14)
        v.tick(2)
        self.assertIn("2-5 (CH14)", v.ax_trace.get_title())

    def test_window_keys_and_button(self):
        v = self.viewer
        v.on_key(key("]"))
        self.assertEqual(v.window_s, 6.0)
        v.tick(0)
        self.assertIn("6 s window", v.ax_trace.get_title())
        self.assertIn("Window: 6 s", v.window_button.label.get_text())
        self.assertEqual(v.ax_trace.get_xlim(), (-6.0, 0.0))
        self.assertEqual(v.trace.get_xdata().size, 6000)
        self.assertEqual(v.judged_line.get_xdata().size, 300)
        for name, expected in (("[", 4.0), ("+", 6.0), ("-", 4.0), ("=", 6.0), ("_", 4.0)):
            v.on_key(key(name))
            self.assertEqual(v.window_s, expected, name)
        self.assertEqual(v.cycle_window(), 6.0)
        self.assertEqual(v.cycle_window(), 8.0)
        v.set_window(60.0)
        self.assertEqual(v.cycle_window(), 0.25)                 # wraps
        v.tick(1)
        self.assertEqual(v.trace.get_xdata().size, 250)
        self.assertTrue(v.judged_hint.get_visible())             # < 1 s: no complete window
        self.assertEqual(v.judged_text.get_text(), "")
        self.assertIn("judged n/a", v.stats_text.get_text())
        v.set_window(1000.0)                                     # clamped to the ladder's top
        self.assertEqual(v.window_s, 60.0)
        v.tick(2)
        self.assertLessEqual(v.trace.get_xdata().size, self.live.maxlen)   # widening never invents history

    def test_hold_freezes_the_display_and_keeps_the_controls(self):
        v = self.viewer
        v.tick(0)
        self.assertEqual(v.hold_button.color, lv.BUTTON_RUNNING)
        v.on_key(key(" "))
        self.assertTrue(v.held)
        self.assertIn("HOLD", v.ax_trace.get_title())
        self.assertEqual(v.hold_button.label.get_text(), "HOLD  [space]")
        self.assertEqual(v.hold_button.color, lv.BUTTON_HELD)
        v.tick(1)
        frozen = v.trace.get_ydata().copy()
        before = self.live.stats().total_scans
        self.assertTrue(wait_until(lambda: self.live.stats().total_scans >= before + 3000))
        v.tick(2)
        self.assertTrue(np.array_equal(v.trace.get_ydata(), frozen))
        self.assertIn("HOLD", v.stats_text.get_text())
        v.step_window(+1)                                        # widen while held: more of the frozen buffer
        v.tick(3)
        self.assertEqual(v.trace.get_xdata().size, 6000)
        self.assertTrue(np.array_equal(v.trace.get_ydata()[-4000:], frozen))
        v.select_channel(0)                                      # re-select while held: another frozen channel
        v.tick(4)
        self.assertAlmostEqual(float(np.mean(v.trace.get_ydata())), 0.70, delta=0.005)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            v.on_key(key("s"))                                   # saves the frozen buffer
        self.assertEqual(len(v.saved_paths), 1)
        self.assertIn("of held buffer", buffer.getvalue())
        with np.load(v.saved_paths[0]) as data:
            self.assertEqual(data["waveform_v"].shape, v._frozen[1].shape)
            self.assertTrue(np.allclose(data["waveform_v"], v._frozen[1].astype(np.float32)))
        v.on_key(key(" "))                                       # Run: live again
        self.assertFalse(v.held)
        self.assertEqual(v.hold_button.label.get_text(), "Run  [space]")
        v.select_channel(13)
        v.set_window(4.0)
        v.tick(5)
        self.assertFalse(np.array_equal(v.trace.get_ydata(), frozen))
        self.assertNotIn("HOLD", v.ax_trace.get_title())

    def test_grid_metric_toggle(self):
        v = self.viewer
        v.tick(0)
        self.assertEqual(v.grid_metric, "mean")
        v.on_key(key("g"))
        self.assertEqual(v.grid_metric, "noise")
        v.tick(1)
        title = v.ax_grid.get_title(loc="left")
        self.assertIn("NO LIMIT", title)
        self.assertIn("never a verdict", title)
        self.assertIn(f"scale 0-{v.noise_scale_mv:g} mV", title)
        self.assertTrue(np.all(np.isfinite(v.grid_values)))
        self.assertTrue(v.tile_texts[13].get_text().endswith(" mV"))
        rgb = v.grid_image.get_array()
        for row in range(5):
            for col in range(10):
                tile = tuple(np.round(rgb[row, col], 6))
                for fail in (lv.TILE_HIGH[0], lv.TILE_RAILED[0]):
                    self.assertNotEqual(tile, tuple(np.round(to_rgb(fail), 6)))
        v.on_key(key("g"))
        self.assertEqual(v.grid_metric, "mean")
        with self.assertRaises(ValueError):
            v.set_grid_metric("bogus")
        other = self.build(["--grid-metric", "noise"])
        self.assertEqual(other.grid_metric, "noise")
        other.request_close()

    def test_save_key_writes_the_live_buffer_in_the_readout_layout(self):
        v = self.viewer
        v.tick(0)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            v.on_key(key("s"))
            v.save_buffer()                                      # a second save in the same second gets its own name
        self.assertEqual(len(v.saved_paths), 2)
        self.assertEqual(buffer.getvalue().count("Saved 10000 scans x 50 channels (10.0 s of live buffer) to "), 2)
        self.assertNotEqual(v.saved_paths[0], v.saved_paths[1])
        for path in v.saved_paths:
            self.assertEqual(path.parent, Path(self.tmp.name))
            self.assertTrue(path.name.startswith("daq_live_"))
            self.assertEqual(path.suffix, ".npz")
        with np.load(v.saved_paths[0]) as data:
            self.assertEqual(set(data.files), set(readout.NPZ_KEYS))
            self.assertEqual(data["waveform_v"].shape[0], 50)
            self.assertGreaterEqual(data["waveform_v"].shape[1], 9000)
            self.assertEqual(data["waveform_v"].dtype, np.float32)
            self.assertEqual(list(data["positions"]), list(daq.POSITIONS))
            self.assertEqual(list(data["channels"]), list(range(50)))
            self.assertEqual(str(data["source"]), readout.NPZ_SOURCE)
            self.assertEqual(str(data["simulated"]), "True")
            self.assertEqual(float(data["sample_rate_hz"]), 1000.0)

    def test_exit_deadline_closes_the_figure(self):
        other = self.build(["--exit-after", "0"])
        self.assertIsNotNone(other.exit_deadline)
        other.tick(0)
        self.assertTrue(other.close_requested)
        self.assertFalse(plt.fignum_exists(other.fig.number))
        other.tick(1)                                            # after close: a no-op
        self.assertIsNone(self.viewer.exit_deadline)

    def test_matplotlib_default_keys_are_released(self):
        for name in lv.RELEASED_KEYMAPS:
            self.assertEqual(plt.rcParams[name], [], name)
        self.assertIn("q", plt.rcParams["keymap.quit"])          # matplotlib's close key is kept


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
class CliParserTests(unittest.TestCase):
    def test_defaults_mirror_the_readout(self):
        args = lv.build_parser().parse_args([])
        self.assertEqual(args.position, "1-1")
        self.assertEqual(args.window, 4.0)
        self.assertEqual(args.max_window, 60.0)
        self.assertEqual(args.grid_metric, "mean")
        self.assertIsNone(args.save_dir)
        self.assertFalse(args.save_on_exit)
        self.assertIsNone(args.exit_after)
        self.assertEqual(args.fps, 20.0)
        self.assertEqual(
            (args.simulate, args.range, args.hz, args.oversample, args.drop, args.start, args.end, args.no_selfcal,
             args.connect_timeout),
            (False, readout.DEFAULT_RANGE_CODE, readout.DEFAULT_SCAN_HZ, readout.DEFAULT_OVERSAMPLE,
             readout.DEFAULT_DROP_FIRST, 0, daq.CHANNEL_COUNT - 1, False, readout.DEFAULT_CONNECT_TIMEOUT_S),
        )
        readout_args = readout.build_parser().parse_args(["info"])
        for name in ("range", "hz", "oversample", "drop", "start", "end", "connect_timeout", "simulate", "no_selfcal"):
            self.assertEqual(getattr(args, name), getattr(readout_args, name), name)

    def test_every_option_parses(self):
        args = lv.build_parser().parse_args([
            "--position", "CH13", "-w", "8", "--max-window", "120", "--grid-metric", "noise", "--save-dir", "x",
            "--save-on-exit", "--exit-after", "3", "--fps", "10", "--simulate", "--range", "3", "--hz", "500",
            "--oversample", "1", "--drop", "0", "--start", "10", "--end", "19", "--no-selfcal", "--connect-timeout", "2",
        ])
        self.assertEqual((args.position, args.window, args.max_window, args.grid_metric, args.save_dir),
                         ("CH13", 8.0, 120.0, "noise", "x"))
        self.assertEqual((args.save_on_exit, args.exit_after, args.fps), (True, 3.0, 10.0))
        self.assertEqual((args.simulate, args.range, args.hz, args.oversample, args.drop), (True, 3, 500.0, 1, 0))
        self.assertEqual((args.start, args.end, args.no_selfcal, args.connect_timeout), (10, 19, True, 2.0))
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                lv.build_parser().parse_args(["--grid-metric", "bogus"])

    def test_max_window_is_raised_to_the_starting_window(self):
        args = lv.build_parser().parse_args(["-w", "90"])
        lv._validate_args(args)
        self.assertEqual(args.max_window, 90.0)
        for bad in (["-w", "0"], ["--max-window", "-1"], ["--fps", "0"], ["--exit-after", "-1"]):
            with self.assertRaises(ValueError):
                lv._validate_args(lv.build_parser().parse_args(bad))


class HeadlessRunTests(HomeGuardMixin, unittest.TestCase):
    def test_exit_after_runs_headless_and_saves_on_exit(self):
        self.assertFalse(lv.backend_is_interactive())
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run_cli(["--simulate", "--exit-after", "0.4", "--fps", "10", "--position", "2-4",
                                      "--save-on-exit", "--save-dir", tmp])
            self.assertEqual(code, 0, err)
            self.assertIn("Connected: SIMULATED USB-AIO16-64MA", out)
            self.assertIn("position 2-4 (CH13), window 4 s (up to 60 s)", out)
            self.assertIn("SPACE = Hold/Run", out)
            self.assertIn("no pin-level noise limit derived yet", out)
            self.assertIn("Saved ", out)
            self.assertIn("Stream closed: ", out)
            self.assertIn("scans received, rate", out)
            self.assertIn("integrity OK", out)
            self.assertNotIn("PASS", out)
            self.assertNotIn("FAIL", out)
            files = sorted(Path(tmp).glob("daq_live_*.npz"))
            self.assertEqual(len(files), 1)
            self.assertIn(str(files[0]), out)
            with np.load(files[0]) as data:
                self.assertEqual(set(data.files), set(readout.NPZ_KEYS))
                self.assertEqual(data["waveform_v"].shape[0], 50)
                self.assertGreaterEqual(data["waveform_v"].shape[1], 200)
        self.assertEqual(err, "")
        self.assert_home_untouched()

    def test_bad_arguments_exit_2_before_any_device_is_opened(self):
        original = lv.open_rig

        def must_not_open(args):
            raise AssertionError("open_rig must not be called for arguments that fail validation")

        lv.open_rig = must_not_open
        try:
            code, _out, err = run_cli(["--simulate", "--position", "9-9", "--exit-after", "0"])
            self.assertEqual(code, 2)
            self.assertIn("row-col", err)
            code, _out, err = run_cli(["--simulate", "--end", "9", "--position", "2-4", "--exit-after", "0"])
            self.assertEqual(code, 2)
            self.assertIn("outside the scanned range CH0-CH9", err)
            code, _out, err = run_cli(["--simulate", "-w", "0", "--exit-after", "0"])
            self.assertEqual(code, 2)
            self.assertIn("must be positive", err)
            code, _out, err = run_cli(["--simulate", "--fps", "0", "--exit-after", "0"])
            self.assertEqual(code, 2)
        finally:
            lv.open_rig = original

    def test_daq_error_exits_2_without_touching_hardware(self):
        original = lv.open_rig

        def failing(args):
            raise daq.DaqNotFoundError("No ACCES device found (test double)")

        lv.open_rig = failing
        try:
            code, _out, err = run_cli(["--exit-after", "0"])
        finally:
            lv.open_rig = original
        self.assertEqual(code, 2)
        self.assertIn("DAQ error: No ACCES device found", err)

    def test_stream_start_failure_exits_2(self):
        original = lv.open_rig

        def opening_a_failing_device(args):
            rig = readout.ArrayRig(device=FailingStartDaq(real_time=False), quiet=True)
            rig.connect()
            return rig

        lv.open_rig = opening_a_failing_device
        try:
            code, _out, err = run_cli(["--exit-after", "0"])
        finally:
            lv.open_rig = original
        self.assertEqual(code, 2)
        self.assertIn("stream did not start", err)
        self.assertIn("ADC_BulkContinuousCallbackStart", err)


# ----------------------------------------------------------------------
# Source hygiene and the Documents guard
# ----------------------------------------------------------------------
class SourceHygieneTests(HomeGuardMixin, unittest.TestCase):
    def test_viewer_imports_nothing_from_the_other_rig_or_the_tester(self):
        source = (MODEL_DIR / "daq_live_waveform.py").read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for forbidden in ("single_detector_rig", "Arduino", "esp32_rig_readout", "esp32_backend", "live_waveform",
                          "eltec_40623_array_tester", "tkinter", "stability_analysis"):
            self.assertFalse(any(name == forbidden or name.startswith(forbidden + ".") for name in imported),
                             f"{forbidden!r} must not be imported by daq_live_waveform.py")
        for required in ("daq_rig_readout", "array_analysis", "daq_backend", "matplotlib.pyplot"):
            self.assertIn(required, imported)
        self.assertNotIn("single_detector_rig", source)
        for needle in ("Path.home(", "Eltec_40623_Test_Results", "import tkinter", "from tkinter"):
            self.assertNotIn(needle, source)
        self.assertIn("from __future__ import annotations", source)
        self.assertIn("__all__", source)
        self.assertIn("same as the tester's", source)
        self.assertIn("never a verdict", source)
        # matplotlib stays out of the readout: the viewer is the only place it is imported
        self.assertNotIn("import matplotlib", (MODEL_DIR / "daq_rig_readout.py").read_text(encoding="utf-8"))

    def test_public_surface(self):
        for name in ("Selection", "Viewer", "build_viewer", "run_viewer", "window_ladder", "step_window",
                     "judged_band_trace", "grid_metric_values", "offset_tile_colour", "format_stats", "main"):
            self.assertIn(name, lv.__all__)
            self.assertTrue(hasattr(lv, name))

    def test_nothing_written_under_documents(self):
        self.assert_home_untouched()


if __name__ == "__main__":
    unittest.main()


class HoldRuleTests(unittest.TestCase):
    def test_flat_trace_holds_and_a_grown_signal_rescales(self):
        import daq_live_waveform as lv

        fig, ax = plt.subplots()
        try:
            flat = np.zeros(100)
            lv.autoscale(ax, flat, lv.TRACE_MIN_PAD_V)
            # minimum zoom already: a flat (code-0) trace must NOT force a rescale every frame
            self.assertFalse(lv.hold_or_rescale(ax, flat, lv.TRACE_MIN_PAD_V))
            self.assertFalse(lv.hold_or_rescale(ax, flat + 5e-5, lv.TRACE_MIN_PAD_V))
            # left the limits: rescale (with the wider drift pad)
            self.assertTrue(lv.hold_or_rescale(ax, np.linspace(0.0, 0.01, 100), lv.TRACE_MIN_PAD_V))
            # shrank to well under 40 % of a wide scale: rescale
            self.assertTrue(lv.hold_or_rescale(ax, np.linspace(0.0, 0.001, 100), lv.TRACE_MIN_PAD_V))
        finally:
            plt.close(fig)
