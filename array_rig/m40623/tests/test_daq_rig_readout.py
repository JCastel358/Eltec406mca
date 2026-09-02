"""Tests for daq_rig_readout.py - the DAQ bench readout (ArrayRig, Capture, LiveStream, CLI).

Everything runs against an injected ``SimulatedDaq(real_time=False)`` (a
virtual clock, so a 2 s capture takes milliseconds) except the CLI cases,
which go through ``--simulate`` - the wall-clock-paced simulator the real
command line uses - with sub-second durations. Output files go to temporary
directories; a guard checks nothing landed under the technician's Documents
folder. No test ever constructs the hardware device.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
MODEL_DIR = TESTS_DIR.parent
RIG_DIR = MODEL_DIR.parent
REPO_ROOT = RIG_DIR.parent
for entry in (str(MODEL_DIR), str(RIG_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import array_analysis as aa  # noqa: E402
import daq_backend as daq  # noqa: E402
import daq_bench_probe as probe  # noqa: E402
import daq_rig_readout as readout  # noqa: E402
import eltec_40623_array_tester as app  # noqa: E402

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


def make_rig(profile: daq.SimProfile | None = None, *, quiet: bool = True, settled: bool = True, **kwargs):
    sim = daq.SimulatedDaq(profile, real_time=False)
    if settled:
        sim._virtual_t = SETTLED_T
    return readout.ArrayRig(device=sim, quiet=quiet, **kwargs), sim


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = readout.main(argv)
    return code, out.getvalue(), err.getvalue()


# ----------------------------------------------------------------------
# Test doubles (all built on the simulator; none touches a DLL)
# ----------------------------------------------------------------------
class ScriptedScanDaq(daq.SimulatedDaq):
    """``read_scan_counts`` answers with scripted rows so the median arithmetic is checkable."""

    def __init__(self, rows: np.ndarray) -> None:
        super().__init__(real_time=False)
        self.rows = np.asarray(rows, dtype=np.float64)
        self.scan_calls: list[int] = []

    def read_scan_counts(self, *, reads: int = 1) -> np.ndarray:
        self._require_config()
        self.scan_calls.append(reads)
        return self.rows[:reads]


class RampDaq(daq.SimulatedDaq):
    """``read_stream`` returns counts equal to the scan index on every channel - ring order is checkable."""

    def read_stream(self, *, timeout_s: float = 1.0):
        config = self._require_config()
        if not self._streaming or self._diagnostics is None:
            raise daq.StreamStateError("No stream is running.")
        n = self.chunk_scans
        start = self._stream_scans
        counts = np.repeat(np.arange(start, start + n, dtype=np.float64)[:, None], config.channels, axis=1)
        self._stream_scans += n
        self._diagnostics.scans_received += n
        self._diagnostics.buffers_received += 1
        return counts


class FailingStartDaq(daq.SimulatedDaq):
    def start_stream(self, **kwargs):
        raise daq.DaqStatusError("ADC_BulkContinuousCallbackStart", 5)


class FailingReadDaq(daq.SimulatedDaq):
    def read_stream(self, *, timeout_s: float = 1.0):
        raise daq.StreamIntegrityError("callback error: boom")


def wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ----------------------------------------------------------------------
# Tokens and context words
# ----------------------------------------------------------------------
class TokenTests(unittest.TestCase):
    def test_every_spelling_of_a_position(self):
        for token in ("2-4", " 2-4 ", "CH13", "ch13", "CH 13", "13", 13, np.int64(13)):
            self.assertEqual(readout.parse_channel_token(token), 13, token)
        self.assertEqual(readout.parse_channel_token("1-1"), 0)
        self.assertEqual(readout.parse_channel_token("5-10"), 49)
        self.assertEqual(readout.parse_channel_token("CH63"), 63)

    def test_bad_tokens_raise_with_the_accepted_forms(self):
        for token in ("9-9", "0-1", "bogus", "64", "CH64", "-1", -1, "", "1-", True, "CHx"):
            with self.assertRaises(ValueError, msg=repr(token)) as ctx:
                readout.parse_channel_token(token)
            self.assertIn("row-col", str(ctx.exception))

    def test_channel_label(self):
        self.assertEqual(readout.channel_label(13), "2-4")
        self.assertEqual(readout.channel_label(49), "5-10")
        self.assertEqual(readout.channel_label(55), "CH55")

    def test_offset_context_and_flag_boundaries(self):
        cases = (
            (4.9, ">= 4.9 V: railed", "R"),
            (4.995, ">= 4.9 V: railed", "R"),
            (0.0, "~0 V: empty socket or dead part", "o"),
            (0.049, "~0 V: empty socket or dead part", "o"),
            (0.2, "under 0.3 V", "<"),
            (1.5, "over 1.2 V", ">"),
            (0.3, "in the PROVISIONAL 0.3-1.2 V band (TP120)", " "),
            (0.7, "in the PROVISIONAL 0.3-1.2 V band (TP120)", " "),
            (1.2, "in the PROVISIONAL 0.3-1.2 V band (TP120)", " "),
        )
        for volts, words, flag in cases:
            self.assertEqual(readout.offset_context(volts), words, volts)
            self.assertEqual(readout.offset_flag(volts), flag, volts)

    def test_grid_lines_layout(self):
        lines = readout.grid_lines({0: "a", 49: "z"})
        self.assertEqual(len(lines), 1 + daq.ROWS)
        self.assertIn("col 10", lines[0])
        self.assertTrue(lines[1].startswith("row 1"))
        self.assertIn("a", lines[1])
        self.assertTrue(lines[5].rstrip().endswith("z"))
        self.assertEqual(lines[3].count("-"), daq.COLS)


# ----------------------------------------------------------------------
# ArrayRig lifecycle and configuration
# ----------------------------------------------------------------------
class RigLifecycleTests(unittest.TestCase):
    def test_connect_configures_and_self_calibrates_exactly_once(self):
        rig, sim = make_rig()
        info = rig.connect()
        self.assertTrue(info.simulated)
        self.assertIs(rig.info, info)
        self.assertTrue(rig.connected)
        self.assertEqual(
            sim.config,
            daq.AdcConfig(range_code=2, start_channel=0, end_channel=49, oversample=3,
                          trigger=daq.TRIGGER_TIMER | daq.TRIGGER_SCAN, cal_code=daq.CAL_NORMAL),
        )
        self.assertEqual(sim.calibrations, 1)
        self.assertTrue(rig.self_calibrated)
        rig.close()

    def test_connect_without_self_calibration(self):
        rig, sim = make_rig(self_calibrate=False)
        rig.connect()
        self.assertEqual(sim.calibrations, 0)
        self.assertFalse(rig.self_calibrated)
        rig.close()

    def test_connect_prints_unless_quiet(self):
        rig, _ = make_rig(quiet=False)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rig.connect()
        self.assertIn("Connected: SIMULATED USB-AIO16-64MA", buffer.getvalue())
        rig.close()
        rig, _ = make_rig(quiet=True)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rig.connect()
        self.assertEqual(buffer.getvalue(), "")
        rig.close()

    def test_custom_acquisition_settings_reach_the_device(self):
        rig, sim = make_rig(range_code=3, oversample=1, drop_first=0, start_channel=10, end_channel=19, scan_hz=500.0)
        rig.connect()
        self.assertEqual(sim.config.range_code, 3)
        self.assertEqual(sim.config.oversample, 1)
        self.assertEqual((sim.config.start_channel, sim.config.end_channel), (10, 19))
        self.assertEqual(rig.channels, tuple(range(10, 20)))
        self.assertEqual(rig.positions, tuple(daq.POSITIONS[10:20]))
        self.assertEqual(rig.scan_hz, 500.0)
        self.assertEqual(rig.range.name, "+/-5 V")
        self.assertEqual(rig.drop_first, 0)
        rig.close()

    def test_channels_above_the_array_are_unwired(self):
        rig, _ = make_rig(end_channel=55)
        self.assertEqual(rig.positions[49], "5-10")
        self.assertEqual(rig.positions[55], "")
        self.assertEqual(rig.labels[55], "CH55")

    def test_constructor_validation(self):
        sim = daq.SimulatedDaq(real_time=False)
        with self.assertRaises(ValueError):
            readout.ArrayRig(device=sim, oversample=1, drop_first=2)
        with self.assertRaises(ValueError):
            readout.ArrayRig(device=sim, scan_hz=0)
        with self.assertRaises(ValueError):
            readout.ArrayRig(device=sim, range_code=99)

    def test_close_is_idempotent_and_stops_a_running_stream(self):
        rig, sim = make_rig()
        rig.connect()
        sim.start_stream(scan_hz=1000.0)
        self.assertTrue(rig.is_streaming)
        rig.close()
        self.assertFalse(sim.is_streaming)
        self.assertIsNone(sim.info)
        self.assertFalse(rig.connected)
        self.assertIsNone(rig.info)
        rig.close()  # second close: nothing to do, no error
        readout.ArrayRig(device=daq.SimulatedDaq(real_time=False)).close()  # never connected

    def test_context_manager(self):
        rig, sim = make_rig()
        with rig as opened:
            self.assertIs(opened, rig)
            self.assertTrue(rig.connected)
        self.assertFalse(rig.connected)
        self.assertIsNone(sim.info)

    def test_reads_require_a_connection(self):
        rig, _ = make_rig()
        with self.assertRaises(daq.StreamStateError):
            rig.read_scan_volts()
        with self.assertRaises(daq.StreamStateError):
            rig.capture(1.0)
        with self.assertRaises(daq.StreamStateError):
            rig.live_stream()

    def test_resolve_channel_forms_and_scan_range(self):
        rig, _ = make_rig()
        for token in ("2-4", "CH13", "13", 13):
            self.assertEqual(rig.resolve_channel(token), 13)
        self.assertEqual(rig.index_of("2-4"), 13)
        self.assertEqual(rig.label_of("CH13"), "2-4")
        with self.assertRaises(ValueError):
            rig.resolve_channel("CH50")
        narrow, _ = make_rig(start_channel=10, end_channel=19)
        self.assertEqual(narrow.resolve_channel("2-4"), 13)
        self.assertEqual(narrow.index_of("2-4"), 3)
        with self.assertRaises(ValueError) as ctx:
            narrow.resolve_channel("1-1")
        self.assertIn("outside the scanned range CH10-CH19", str(ctx.exception))

    def test_set_range_reconfigures_and_refuses_while_streaming(self):
        rig, sim = make_rig()
        rig.connect()
        spec = rig.set_range(3)
        self.assertEqual(spec.code, 3)
        self.assertEqual(sim.config.range_code, 3)
        self.assertEqual(rig.range_code, 3)
        self.assertEqual(sim.config.oversample, 3)  # everything else kept
        with self.assertRaises(ValueError):
            rig.set_range(99)
        sim.start_stream(scan_hz=1000.0)
        with self.assertRaises(daq.StreamStateError):
            rig.set_range(2)
        sim.stop_stream()
        self.assertEqual(rig.set_range(2).name, "0-5 V")
        rig.close()


# ----------------------------------------------------------------------
# Immediate reads
# ----------------------------------------------------------------------
class ImmediateReadTests(unittest.TestCase):
    def test_read_offset_voltage_is_the_median_of_reads_for_that_channel(self):
        rows = np.full((24, 50), 32768.0)
        rows[:, 13] = np.arange(24) * 10.0 + 100.0     # CH13 = 2-4: 100, 110, ... 330
        rows[:5, 13] = [100.0, 300.0, 200.0, 900.0, 50.0]
        sim = ScriptedScanDaq(rows)
        rig = readout.ArrayRig(device=sim, quiet=True)
        rig.connect()
        self.assertAlmostEqual(rig.read_offset_voltage("2-4", reads=5), daq.counts_to_volts(200.0, 2), places=12)
        self.assertEqual(sim.scan_calls, [5])
        expected = daq.counts_to_volts(float(np.median(rows[:, 13])), 2)
        self.assertAlmostEqual(rig.read_offset_voltage("CH13"), expected, places=12)
        self.assertEqual(sim.scan_calls[-1], readout.DEFAULT_OFFSET_READS)
        self.assertEqual(readout.DEFAULT_OFFSET_READS, 24)
        with self.assertRaises(ValueError):
            rig.read_scan_volts(reads=0)
        rig.close()

    def test_read_offsets_all_positions_in_one_read_set(self):
        rows = np.full((3, 50), 32768.0)
        rows[:, 13] = [100.0, 300.0, 200.0]
        sim = ScriptedScanDaq(rows)
        rig = readout.ArrayRig(device=sim, quiet=True)
        rig.connect()
        offsets = rig.read_offsets(reads=3)
        self.assertEqual(tuple(offsets), daq.POSITIONS)
        self.assertEqual(sim.scan_calls, [3])
        self.assertAlmostEqual(offsets["2-4"], daq.counts_to_volts(200.0, 2), places=12)
        self.assertAlmostEqual(offsets["1-1"], daq.counts_to_volts(32768.0, 2), places=12)
        chosen = rig.read_offsets(["CH13", "4-7", 0], reads=2)
        self.assertEqual(list(chosen), ["2-4", "4-7", "1-1"])
        self.assertEqual(sim.scan_calls, [3, 2])
        rig.close()

    def test_profile_offsets_come_through(self):
        rig, _ = make_rig(daq.SimProfile(offsets_v={"2-4": 1.62, "4-7": 0.21}))
        rig.connect()
        self.assertAlmostEqual(rig.read_offset_voltage("2-4"), 1.62, delta=0.005)
        self.assertAlmostEqual(rig.read_offset_voltage("4-7"), 0.21, delta=0.005)
        self.assertAlmostEqual(rig.read_offset_voltage("1-1"), 0.70, delta=0.005)
        rig.close()

    def test_unwired_channel_is_keyed_chn(self):
        rig, _ = make_rig(end_channel=55)
        rig.connect()
        offsets = rig.read_offsets(["CH55"])
        self.assertEqual(list(offsets), ["CH55"])
        self.assertEqual(offsets["CH55"], 0.0)
        self.assertNotIn("CH55", rig.read_offsets())  # the default set is the wired positions only
        rig.close()


# ----------------------------------------------------------------------
# Captures
# ----------------------------------------------------------------------
class CaptureTests(HomeGuardMixin, unittest.TestCase):
    def test_capture_shape_time_axis_and_diagnostics(self):
        rig, _ = make_rig()
        rig.connect()
        cap = rig.capture(2.0, progress=False)
        self.assertEqual(cap.volts.shape, (50, 2000))
        self.assertEqual(cap.volts.dtype, np.float64)
        self.assertEqual(cap.samples, 2000)
        self.assertEqual(cap.channels, tuple(range(50)))
        self.assertEqual(cap.positions, daq.POSITIONS)
        self.assertEqual(cap.labels, daq.POSITIONS)
        self.assertEqual(cap.t_us.shape, (2000,))
        self.assertEqual(cap.t_us.dtype, np.int64)
        self.assertTrue(np.all(np.diff(cap.t_us) == int(round(1e6 / cap.actual_timer_hz))))
        self.assertEqual(cap.t_s[0], 0.0)
        self.assertAlmostEqual(cap.t_s[-1], 1.999)
        self.assertEqual(cap.actual_timer_hz, 1000.0)
        self.assertEqual(cap.scan_hz, 1000.0)
        self.assertIsNotNone(cap.diagnostics)
        self.assertEqual(cap.diagnostics.scans_received, 2000)
        self.assertEqual((cap.range_code, cap.oversample, cap.drop_first), (2, 3, 1))
        self.assertTrue(cap.simulated)
        self.assertEqual(cap.daq_serial, "SIMULATED")
        self.assertEqual(cap.daq_model, "USB-AIO16-64MA")
        self.assertEqual(len(cap.started_at), 19)
        self.assertEqual(cap.started_at[10], "T")
        self.assertTrue(cap.quiet)
        self.assertFalse(rig.is_streaming)
        rig.close()

    def test_capture_positions_subset_keeps_the_given_order(self):
        rig, _ = make_rig()
        rig.connect()
        cap = rig.capture(1.0, ["4-7", "CH13", 0], progress=False)
        self.assertEqual(cap.positions, ("4-7", "2-4", "1-1"))
        self.assertEqual(cap.channels, (36, 13, 0))
        self.assertEqual(cap.volts.shape, (3, 1000))
        self.assertAlmostEqual(cap.means()[1], 1.62, delta=0.005)
        self.assertAlmostEqual(cap.means()[0], 0.21, delta=0.005)
        rig.close()

    def test_bad_position_fails_before_the_stream_starts(self):
        rig, sim = make_rig()
        rig.connect()
        with self.assertRaises(ValueError):
            rig.capture(1.0, ["9-9"], progress=False)
        self.assertEqual(sim.stream_attempts, 0)
        with self.assertRaises(ValueError):
            rig.capture(0.0, progress=False)
        with self.assertRaises(ValueError):
            rig.capture(1.0, quiet_wait_s=-1.0, progress=False)
        rig.close()

    def test_integrity_problems_raise_and_leave_the_device_stopped(self):
        profile = daq.SimProfile(gap_on_attempts=frozenset({1}), pool_events_on_attempts=frozenset({2}))
        rig, sim = make_rig(profile)
        rig.connect()
        with self.assertRaises(daq.StreamIntegrityError) as ctx:
            rig.capture(1.0, progress=False)
        self.assertIn("scan rate", str(ctx.exception))
        self.assertFalse(sim.is_streaming)
        with self.assertRaises(daq.StreamIntegrityError) as ctx:
            rig.capture(1.0, progress=False)
        self.assertIn("pool exhausted", str(ctx.exception))
        cap = rig.capture(1.0, progress=False)  # attempt 3 is clean
        self.assertEqual(cap.samples, 1000)
        self.assertEqual(sim.stream_attempts, 3)
        rig.close()

    def test_quiet_wait_discards_leading_data(self):
        rig, _ = make_rig()
        rig.connect()
        cap = rig.capture(1.0, quiet_wait_s=0.5, progress=False)
        self.assertEqual(cap.samples, 1000)
        self.assertGreaterEqual(cap.diagnostics.scans_received, 1500)
        rig.close()

    def test_progress_lines(self):
        rig, _ = make_rig(quiet=False)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rig.connect()
            rig.capture(1.5)
        text = buffer.getvalue()
        self.assertIn("1000/1500 scans", text)
        self.assertIn("captured 1500 scans x 50 channels", text)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rig.capture(1.0, progress=False)
        self.assertEqual(buffer.getvalue(), "")
        rig.close()

    def test_capture_helpers_on_a_synthetic_capture(self):
        volts = np.vstack([np.linspace(0.0, 0.1, 100), np.full(100, 1.5), np.full(100, 0.2)])
        cap = readout.Capture(
            volts=volts, scan_hz=1000.0, actual_timer_hz=999.5, channels=(0, 13, 36), positions=("1-1", "2-4", "4-7"),
            range_code=2, oversample=3, drop_first=1, diagnostics=None, started_at="2026-09-02T10:00:00",
            daq_serial="S", daq_model="M", simulated=True,
        )
        self.assertEqual(cap.index_of("2-4"), 1)
        self.assertEqual(cap.index_of(13), 1)
        self.assertEqual(cap.index_of("CH36"), 2)
        self.assertTrue(np.array_equal(cap.channel("2-4"), np.full(100, 1.5)))
        with self.assertRaises(ValueError) as ctx:
            cap.index_of("3-1")
        self.assertIn("not in this capture", str(ctx.exception))
        np.testing.assert_allclose(cap.means(), [0.05, 1.5, 0.2])
        np.testing.assert_allclose(cap.peak_to_peak_mv(), [100.0, 0.0, 0.0])
        self.assertAlmostEqual(cap.t_s[-1], 99 / 999.5)
        self.assertEqual(cap.t_us[1], round(1e6 / 999.5))
        part = cap.subset(["4-7", "1-1"])
        self.assertEqual(part.positions, ("4-7", "1-1"))
        self.assertEqual(part.channels, (36, 0))
        self.assertTrue(np.array_equal(part.volts[1], volts[0]))
        self.assertEqual(part.started_at, cap.started_at)
        unwired = readout.Capture(
            volts=volts[:1], scan_hz=1000.0, actual_timer_hz=1000.0, channels=(55,), positions=("",), range_code=2,
            oversample=3, drop_first=1, diagnostics=None, started_at="t", daq_serial="S", daq_model="M", simulated=True,
        )
        self.assertEqual(unwired.labels, ("CH55",))
        with self.assertRaises(ValueError):
            readout.Capture(volts=volts, scan_hz=1000.0, actual_timer_hz=1000.0, channels=(0, 1), positions=("1-1", "1-2"),
                            range_code=2, oversample=3, drop_first=1, diagnostics=None, started_at="t", daq_serial="S",
                            daq_model="M", simulated=True)
        with self.assertRaises(ValueError):
            readout.Capture(volts=volts[0], scan_hz=1000.0, actual_timer_hz=1000.0, channels=(0,), positions=("1-1",),
                            range_code=2, oversample=3, drop_first=1, diagnostics=None, started_at="t", daq_serial="S",
                            daq_model="M", simulated=True)

    def test_band_limited_pp_is_the_analysis_module_pipeline(self):
        rig, _ = make_rig()
        rig.connect()
        cap = rig.capture(2.0, progress=False)
        pp = cap.band_limited_pp_mv()
        self.assertEqual(pp.shape, (50, 2))
        expected = aa.band_limited_window_pp_mv(cap.volts, cap.scan_hz, decimation_factor=aa.NOISE_DECIMATION_FACTOR,
                                                window_s=aa.NOISE_WINDOW_S)
        self.assertTrue(np.array_equal(pp, expected))
        self.assertEqual(cap.band_limited_pp_mv(window_s=0.5).shape, (50, 4))
        self.assertEqual(rig.capture(0.3, progress=False).band_limited_pp_mv().shape, (50, 0))
        rig.close()

    def test_to_csv_header_rows_and_round_trip(self):
        rig, _ = make_rig(quiet=False)
        with redirect_stdout(io.StringIO()):
            rig.connect()
            cap = rig.capture(0.5, ["2-4", "5-2"], progress=False)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "cap.csv"
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                written = cap.to_csv(target)
            self.assertEqual(written, target)
            self.assertIn(f"Saved 500 samples x 2 channels to {target}", buffer.getvalue())
            with target.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0], ["t_us", "2-4", "5-2"])
            self.assertEqual(len(rows), 501)
            self.assertEqual([r[0] for r in rows[1:4]], ["0", "1000", "2000"])
            for k in (0, 250, 499):
                self.assertAlmostEqual(float(rows[k + 1][1]), cap.volts[0, k], places=6)
                self.assertAlmostEqual(float(rows[k + 1][2]), cap.volts[1, k], places=6)
                self.assertEqual(len(rows[k + 1][1].split(".")[1]), 6)
        rig.close()
        self.assert_home_untouched()

    def test_to_npz_uppercase_suffix_matches_numpy(self):
        rig, _sim = make_rig()
        rig.connect()
        cap = rig.capture(0.2, progress=False)
        rig.close()
        with tempfile.TemporaryDirectory() as tmp:
            path = cap.to_npz(Path(tmp) / "cap.NPZ")
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "cap.NPZ.npz")

    def test_to_npz_uses_the_probe_layout(self):
        rig, sim = make_rig()
        rig.connect()
        cap = rig.capture(0.4, ["2-4", "CH9"], progress=False)
        with tempfile.TemporaryDirectory() as tmp:
            written = cap.to_npz(Path(tmp) / "cap")   # suffix added
            self.assertEqual(written.name, "cap.npz")
            with np.load(written) as data:   # closed before the temp dir goes (Windows holds open files)
                self.assertEqual(set(data.files), set(readout.NPZ_KEYS))
                self.assertEqual(data["waveform_v"].shape, (2, 400))
                self.assertEqual(data["waveform_v"].dtype, np.float32)
                self.assertEqual(float(data["sample_rate_hz"]), 1000.0)
                self.assertEqual(list(data["positions"]), ["2-4", "1-10"])
                self.assertEqual(list(data["channels"]), [13, 9])
                self.assertEqual(str(data["source"]), "daq_rig_readout")
                self.assertEqual(str(data["range_code"]), "2")
                self.assertEqual(str(data["range_span_v"]), "5.0")
                self.assertEqual(str(data["drop_first"]), "1")
                self.assertEqual(str(data["simulated"]), "True")
                self.assertEqual(str(data["actual_timer_hz"]), "1000.000000")
                self.assertEqual(str(data["recorded_at"]), cap.started_at)
            # the same key set the bench probe writes, minus its probe_command
            with redirect_stdout(io.StringIO()):
                probe.save_capture(Path(tmp) / "probe.npz", cap.volts, scan_hz=1000.0, config=sim.config, device=sim,
                                   timer_hz=1000.0, extra={"probe_command": "stream", "drop_first": 1})
            with np.load(Path(tmp) / "probe.npz") as probe_data:
                probe_keys = set(probe_data.files)
            self.assertEqual(set(readout.NPZ_KEYS), probe_keys - {"probe_command"})
        rig.close()
        self.assert_home_untouched()

    def test_npz_replays_in_the_replot_tool(self):
        spec = importlib.util.spec_from_file_location("replot_noise_capture", REPO_ROOT / "engineer_tools" / "replot_noise_capture.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        rig, _ = make_rig()
        rig.connect()
        cap = rig.capture(2.0, ["2-4", "3-6"], progress=False)
        rig.close()
        with tempfile.TemporaryDirectory() as tmp:
            path = cap.to_npz(Path(tmp) / "bench.npz")
            self.assertTrue(tool.is_tray_capture(path))
            entries = tool.load_tray_channels(path)
            self.assertEqual([e["position"] for e in entries], ["2-4", "3-6"])
            self.assertEqual(entries[1]["name"], "tray_bench_3-6")
            entry = tool.analyze_file(path, [("app", None)], None, 0.15, True, capture=entries[1])
            self.assertTrue(entry["no_limit"])
            replay = entry["results"][0]["analysis"]
            self.assertEqual(replay.windows_total, 2)
            self.assertAlmostEqual(replay.worst_pp_mv, cap.band_limited_pp_mv()[1].max(), places=9)
        self.assert_home_untouched()


# ----------------------------------------------------------------------
# LiveStream
# ----------------------------------------------------------------------
class LiveStreamTests(unittest.TestCase):
    def test_fills_snapshots_and_stops(self):
        rig, sim = make_rig()
        rig.connect()
        live = rig.live_stream(buffer_s=2.0)
        self.assertIs(rig.live, live)
        self.assertEqual(live.maxlen, 2500)
        self.assertFalse(live.running)
        t_empty, v_empty = live.snapshot()
        self.assertEqual((t_empty.shape, v_empty.shape), ((0,), (50, 0)))
        self.assertTrue(np.all(np.isnan(live.latest())))
        live.start()
        self.assertTrue(live.wait_ready(5.0))
        self.assertTrue(live.running)
        self.assertTrue(sim.is_streaming)
        self.assertTrue(wait_until(lambda: live.stats().total_scans >= 3000))
        t_s, volts = live.snapshot()
        self.assertEqual(volts.shape, (50, 2500))
        self.assertEqual(t_s.shape, (2500,))
        self.assertEqual(t_s[-1], 0.0)
        self.assertTrue(np.all(np.diff(t_s) > 0))
        self.assertAlmostEqual(t_s[0], -2499 / 1000.0)
        t_s, volts = live.snapshot(channels=[13, 36], seconds=0.5)
        self.assertEqual(volts.shape, (2, 500))
        self.assertEqual(t_s.shape, (500,))
        self.assertAlmostEqual(volts[0].mean(), 1.62, delta=0.005)
        self.assertAlmostEqual(volts[1].mean(), 0.21, delta=0.005)
        _t, by_token = live.snapshot(["2-4"], seconds=0.5)
        self.assertEqual(by_token.shape, (1, 500))
        with self.assertRaises(IndexError):
            live.snapshot(channels=[99])
        latest = live.latest()
        self.assertEqual(latest.shape, (50,))
        self.assertAlmostEqual(latest[13], 1.62, delta=0.005)
        self.assertEqual(live.latest(channels=[0]).shape, (1,))
        stats = live.stats()
        self.assertTrue(stats.running)
        self.assertGreater(stats.chunks, 0)
        self.assertGreaterEqual(stats.lag_s, 0.0)
        self.assertIsNone(stats.error)
        self.assertIsNone(stats.diagnostics)
        live.stop()
        self.assertFalse(live.is_alive())
        stats = live.stats()
        self.assertFalse(stats.running)
        self.assertIsNotNone(stats.diagnostics)
        self.assertEqual(stats.diagnostics.scans_received, stats.total_scans)
        self.assertFalse(sim.is_streaming)
        cap = rig.capture(0.2, progress=False)  # the device is free again
        self.assertEqual(cap.samples, 200)
        rig.close()

    def test_ring_buffer_keeps_order_across_the_wrap(self):
        sim = RampDaq(real_time=False)
        rig = readout.ArrayRig(device=sim, quiet=True)
        rig.connect()
        live = rig.live_stream(buffer_s=1.0)   # maxlen 1250, chunks of 100: the write pointer wraps mid-chunk
        live.start()
        self.assertTrue(live.wait_ready(5.0))
        self.assertTrue(wait_until(lambda: live.stats().total_scans >= 4000))
        live.stop()
        total = live.stats().total_scans
        _t, volts = live.snapshot()
        self.assertEqual(volts.shape, (50, 1250))
        lsb = daq.lsb_volts(2)
        self.assertTrue(np.allclose(np.diff(volts[0]), lsb))
        self.assertTrue(np.allclose(volts[:, -1], daq.counts_to_volts(float(total - 1), 2)))
        self.assertTrue(np.allclose(volts[7], volts[0]))
        rig.close()

    def test_start_failure_is_recorded_not_raised(self):
        sim = FailingStartDaq(real_time=False)
        rig = readout.ArrayRig(device=sim, quiet=True)
        rig.connect()
        live = rig.live_stream()
        live.start()
        self.assertFalse(live.wait_ready(2.0))
        self.assertTrue(live.finished.is_set())
        self.assertFalse(live.ready.is_set())
        self.assertIn("start_stream failed", live.error)
        self.assertIn("ADC_BulkContinuousCallbackStart", live.error)
        stats = live.stats()
        self.assertFalse(stats.running)
        self.assertIsNone(stats.diagnostics)
        self.assertEqual(stats.total_scans, 0)
        live.stop()
        rig.close()

    def test_read_failure_stops_the_stream_and_is_recorded(self):
        sim = FailingReadDaq(real_time=False)
        rig = readout.ArrayRig(device=sim, quiet=True)
        rig.connect()
        live = rig.live_stream()
        live.start()
        self.assertTrue(live.wait_ready(2.0))
        self.assertTrue(live.finished.wait(2.0))
        self.assertIn("read_stream failed", live.error)
        self.assertIsNotNone(live.diagnostics)
        self.assertFalse(sim.is_streaming)
        self.assertFalse(live.running)
        rig.close()

    def test_rig_refuses_conflicting_work_while_live(self):
        rig, sim = make_rig()
        rig.connect()
        live = rig.live_stream()
        live.start()
        self.assertTrue(live.wait_ready(2.0))
        with self.assertRaises(daq.StreamStateError):
            rig.capture(0.1, progress=False)
        with self.assertRaises(daq.StreamStateError):
            rig.set_range(3)
        with self.assertRaises(daq.StreamStateError):
            rig.live_stream()
        with self.assertRaises(daq.StreamStateError):
            rig.read_scan_volts()
        live.stop()
        second = rig.live_stream()
        self.assertIsNot(second, live)
        rig.close()

    def test_close_stops_a_running_live_stream(self):
        rig, sim = make_rig()
        rig.connect()
        live = rig.live_stream()
        live.start()
        self.assertTrue(live.wait_ready(2.0))
        rig.close()
        self.assertTrue(live.finished.is_set())
        self.assertFalse(live.is_alive())
        self.assertFalse(sim.is_streaming)
        self.assertIsNone(rig.live)

    def test_rate_is_the_delivery_rate_between_chunks(self):
        # Two chunks of 160 scans 0.16 s apart deliver 1000 scans/s even
        # though the first one arrived 0.5 s after the stream started.
        rig, _sim = make_rig()
        rig.connect()
        live = rig.live_stream(buffer_s=2.0)
        live._started_wall = 100.0
        with live.lock:
            live.total_scans, live.chunks = 320, 2
            live._first_push_wall, live._first_push_scans, live._last_push_wall = 100.5, 160, 100.66
        stats = live.stats()
        self.assertAlmostEqual(stats.rate_hz, 160 / 0.16, places=6)
        # a single chunk falls back to total / elapsed
        with live.lock:
            live.chunks = 1
        live._stopped_wall = 101.0
        self.assertAlmostEqual(live.stats().rate_hz, 320 / 1.0, places=6)
        rig.close()

    def test_range_set_after_construction_is_used_by_the_stream(self):
        rig, _sim = make_rig()
        rig.connect()
        live = rig.live_stream(buffer_s=2.0)
        rig.set_range(0)                       # 0-10 V: the simulator's 0.70 V is 4587 counts, not 9175
        live.start()
        self.assertTrue(live.wait_ready(5.0))
        while live.stats().total_scans < 500:
            time.sleep(0.005)
        live.stop()
        self.assertAlmostEqual(float(live.latest(["1-1"], seconds=0.2)[0]), 0.70, places=2)
        rig.close()

    def test_rejected_chunk_is_recorded_as_an_error(self):
        class WrongShapeDaq(daq.SimulatedDaq):
            def read_stream(self, *, timeout_s: float = 1.0):
                chunk = super().read_stream(timeout_s=timeout_s)
                return None if chunk is None else chunk[:, :10]

        sim = WrongShapeDaq(real_time=False)
        rig = readout.ArrayRig(device=sim, quiet=True)
        rig.connect()
        live = rig.live_stream(buffer_s=2.0)
        live.start()
        self.assertTrue(live.wait_ready(5.0))
        self.assertTrue(live.finished.wait(5.0))
        self.assertIn("chunk", live.error or "")
        self.assertFalse(sim.is_streaming)
        rig.close()

    def test_buffer_validation(self):
        rig, _ = make_rig()
        rig.connect()
        with self.assertRaises(ValueError):
            rig.live_stream(buffer_s=0)
        rig.close()


# ----------------------------------------------------------------------
# CLI (wall-clock simulator, sub-second durations)
# ----------------------------------------------------------------------
class CliParserTests(unittest.TestCase):
    def test_every_subcommand_parses(self):
        parser = readout.build_parser()
        args = parser.parse_args(["info"])
        self.assertEqual(args.command, "info")
        self.assertEqual((args.range, args.hz, args.oversample, args.drop, args.reads), (2, 1000.0, 3, 1, 24))
        self.assertEqual((args.start, args.end, args.simulate, args.no_selfcal, args.connect_timeout), (0, 49, False, False, 10.0))
        args = parser.parse_args(["offset", "2-4", "CH13", "13", "--all"])
        self.assertEqual(args.positions, ["2-4", "CH13", "13"])
        self.assertTrue(args.all)
        args = parser.parse_args(["stream", "-s", "0.3", "-p", "2-4", "3-6", "-p", "CH9", "-o", "x.csv", "--npz", "x.npz"])
        self.assertEqual(args.seconds, 0.3)
        self.assertEqual(args.positions, ["2-4", "3-6", "CH9"])
        self.assertEqual((args.output, args.npz), ("x.csv", "x.npz"))
        args = parser.parse_args(["stream"])
        self.assertEqual(args.seconds, readout.DEFAULT_CAPTURE_S)
        self.assertIsNone(args.positions)
        args = parser.parse_args(["noise", "-s", "0.3", "--quiet-wait", "0.1"])
        self.assertEqual((args.seconds, args.quiet_wait), (0.3, 0.1))
        self.assertEqual(parser.parse_args(["noise"]).seconds, readout.DEFAULT_NOISE_S)
        args = parser.parse_args(["watch", "-s", "0.5", "--interval", "0.2", "-p", "1-1"])
        self.assertEqual((args.seconds, args.interval, args.positions), (0.5, 0.2, ["1-1"]))
        self.assertIsNone(parser.parse_args(["watch"]).seconds)
        args = parser.parse_args(["test", "-s", "0.3"])
        self.assertEqual(args.seconds, 0.3)
        self.assertEqual(parser.parse_args(["test"]).seconds, readout.DEFAULT_NOISE_S)

    def test_global_options_before_or_after_the_command(self):
        parser = readout.build_parser()
        before = parser.parse_args(["--simulate", "--range", "3", "--no-selfcal", "offset"])
        self.assertTrue(before.simulate)
        self.assertEqual(before.range, 3)
        self.assertTrue(before.no_selfcal)
        after = parser.parse_args(["offset", "--simulate", "--start", "10", "--end", "19", "--reads", "5"])
        self.assertTrue(after.simulate)
        self.assertEqual((after.start, after.end, after.reads), (10, 19, 5))
        self.assertEqual(after.range, 2)  # untouched defaults still stand
        both = parser.parse_args(["--hz", "500", "watch", "--hz", "250"])
        self.assertEqual(both.hz, 250.0)


class CliRunTests(HomeGuardMixin, unittest.TestCase):
    def test_info(self):
        code, out, _ = run_cli(["info", "--simulate"])
        self.assertEqual(code, 0)
        self.assertIn("SIMULATED USB-AIO16-64MA", out)
        self.assertIn("ran ADC_SetCal(':AUTO:') at connect", out)
        self.assertIn("range code 2 (0-5 V", out)
        self.assertIn("stream buffers 32 x 64000 B", out)
        self.assertIn("CH = (row-1)*10 + (col-1)", out)
        code, out, _ = run_cli(["--simulate", "--no-selfcal", "info"])
        self.assertEqual(code, 0)
        self.assertIn("skipped (--no-selfcal)", out)

    def test_offset_table(self):
        code, out, _ = run_cli(["offset", "--all", "--simulate"])
        self.assertEqual(code, 0)
        self.assertIn("median of 24 immediate scans", out)
        for row in range(1, 6):
            self.assertIn(f"row {row} ", out)
        self.assertIn("col 10", out)
        self.assertIn("4.995R", out)        # 3-1 railed in the default profile
        self.assertIn("0.000o", out)        # 1-10 / 5-10 empty
        self.assertIn("50 positions read:", out)
        self.assertIn("PROVISIONAL", out)
        self.assertIn("context only, never a verdict", out)
        code, out, _ = run_cli(["offset", "--simulate"])
        self.assertEqual(code, 0)
        self.assertIn("row 5 ", out)

    def test_offset_positions(self):
        code, out, _ = run_cli(["offset", "2-4", "CH13", "13", "3-1", "1-10", "--simulate", "--reads", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(out.count("2-4 (CH13)"), 3)
        self.assertIn("[over 1.2 V]", out)
        self.assertIn("3-1 (CH20): 4.995 V  [>= 4.9 V: railed]", out)
        self.assertIn("[~0 V: empty socket or dead part]", out)
        self.assertIn("median of 3 immediate scans", out)
        self.assertIn("PROVISIONAL", out)
        self.assertNotIn("row 1 ", out)

    def test_bad_position_exits_2(self):
        code, _out, err = run_cli(["offset", "9-9", "--simulate"])
        self.assertEqual(code, 2)
        self.assertIn("row-col", err)
        code, _out, err = run_cli(["--simulate", "--end", "9", "offset", "2-4"])
        self.assertEqual(code, 2)
        self.assertIn("outside the scanned range CH0-CH9", err)

    def test_stream_saves_csv_and_npz(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path, npz_path = Path(tmp) / "cap.csv", Path(tmp) / "cap.npz"
            code, out, _ = run_cli(["stream", "-s", "0.3", "-p", "2-4", "5-2", "-o", str(csv_path), "--npz", str(npz_path), "--simulate"])
            self.assertEqual(code, 0)
            self.assertIn("Capturing 0.3 s of waveform, 2 selected position(s)", out)
            self.assertIn("integrity: OK", out)
            self.assertIn("2-4  CH13", out)
            self.assertIn("5-2  CH41", out)
            self.assertIn("no complete 1 s window", out)
            self.assertIn(f"Saved 300 samples x 2 channels to {csv_path}", out)
            self.assertIn(str(npz_path), out)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0], ["t_us", "2-4", "5-2"])
            self.assertEqual(len(rows), 301)
            with np.load(npz_path) as data:
                self.assertEqual(set(data.files), set(readout.NPZ_KEYS))
        self.assert_home_untouched()

    def test_noise_prints_the_calibration_status(self):
        code, out, _ = run_cli(["noise", "-s", "0.3", "--simulate"])
        self.assertEqual(code, 0)
        self.assertIn("Capturing 0.3 s of emitter-off noise", out)
        self.assertIn("5-10  CH49", out)
        self.assertIn("calibration status: PENDING", out)
        self.assertIn("no pin-level noise limit derived yet", out)
        self.assertIn("section 4b", out)
        self.assertNotIn("PASS", out)
        self.assertNotIn("FAIL", out)

    def test_duplicate_positions_are_reported_once(self):
        code, out, _err = run_cli(["--simulate", "stream", "-s", "0.3", "-p", "2-4", "CH13", "-p", "13"])
        self.assertEqual(code, 0)
        self.assertEqual(sum(1 for line in out.splitlines() if line.strip().startswith("2-4 ")), 1)

    def test_watch_grid_then_selected(self):
        code, out, _ = run_cli(["watch", "-s", "0.5", "--interval", "0.2", "--simulate"])
        self.assertEqual(code, 0)
        self.assertIn("live readout every 0.2 s for 0.5 s", out)
        self.assertIn("scans/s, lag", out)
        self.assertIn("mean V over the last", out)
        self.assertIn("raw pk-pk mV over the last", out)
        self.assertIn("row 5 ", out)
        self.assertIn("stream: ", out)
        self.assertIn("integrity: OK", out)
        code, out, _ = run_cli(["watch", "-p", "2-4", "CH36", "-s", "0.4", "--interval", "0.2", "--simulate"])
        self.assertEqual(code, 0)
        self.assertIn("2-4 (CH13)", out)
        self.assertIn("4-7 (CH36)", out)
        self.assertNotIn("row 1 ", out)

    def test_watch_exits_cleanly_on_keyboard_interrupt(self):
        real_time = readout.time

        class InterruptingTime:
            calls = 0

            @classmethod
            def sleep(cls, seconds: float) -> None:
                cls.calls += 1
                if cls.calls >= 2:
                    raise KeyboardInterrupt
                real_time.sleep(seconds)

            monotonic = staticmethod(real_time.monotonic)

        readout.time = InterruptingTime  # cmd_watch's sleep only; the simulator keeps its own clock
        try:
            code, out, _ = run_cli(["watch", "--interval", "0.15", "--simulate"])
        finally:
            readout.time = real_time
        self.assertEqual(code, 0)
        self.assertIn("stopping the stream", out)
        self.assertIn("stream: ", out)   # stop_stream ran and its diagnostics were printed

    def test_guided_test_sequence(self):
        code, out, _ = run_cli(["test", "-s", "0.3", "--simulate"])
        self.assertEqual(code, 0)
        self.assertIn("1/3  Identity and self-calibration", out)
        self.assertIn("2/3  DC offsets of every position", out)
        self.assertIn("row 5 ", out)
        self.assertIn("3/3  Emitter-off noise capture (0.3 s)", out)
        self.assertIn("no pin-level noise limit derived yet", out)
        self.assertIn("offset band PROVISIONAL", out)
        self.assertIn("issues no verdict", out)

    def test_daq_error_exits_2_without_touching_hardware(self):
        original = readout.open_rig

        def failing(args):
            raise daq.DaqNotFoundError("No ACCES device found (test double)")

        readout.open_rig = failing
        try:
            code, _out, err = run_cli(["info"])
        finally:
            readout.open_rig = original
        self.assertEqual(code, 2)
        self.assertIn("DAQ error: No ACCES device found", err)


# ----------------------------------------------------------------------
# Mirrored constants and the engineering-only rule
# ----------------------------------------------------------------------
class ConstantsAndGuardTests(HomeGuardMixin, unittest.TestCase):
    def test_defaults_mirror_the_tester_and_the_probe(self):
        self.assertEqual(readout.DEFAULT_RANGE_CODE, app.DAQ_RANGE_CODE)
        self.assertEqual(readout.DEFAULT_SCAN_HZ, app.DAQ_SCAN_HZ)
        self.assertEqual(readout.DEFAULT_OVERSAMPLE, app.DAQ_OVERSAMPLE)
        self.assertEqual(readout.DEFAULT_DROP_FIRST, app.DAQ_DROP_CONVERSIONS_AFTER_MUX)
        self.assertEqual(readout.DEFAULT_BUFFER_BYTES, app.STREAM_BUFFER_BYTES)
        self.assertEqual(readout.DEFAULT_BUFFER_COUNT, app.STREAM_BUFFER_COUNT)
        self.assertEqual(readout.DEFAULT_RANGE_CODE, probe.DEFAULT_RANGE_CODE)
        self.assertEqual(readout.DEFAULT_SCAN_HZ, probe.DEFAULT_SCAN_HZ)
        self.assertEqual(readout.DEFAULT_OVERSAMPLE, probe.DEFAULT_OVERSAMPLE)
        self.assertEqual(readout.DEFAULT_DROP_FIRST, probe.DEFAULT_DROP_FIRST)
        self.assertEqual(readout.DEFAULT_BUFFER_BYTES, probe.DEFAULT_BUFFER_BYTES)
        self.assertEqual(readout.DEFAULT_BUFFER_COUNT, probe.DEFAULT_BUFFER_COUNT)
        self.assertEqual(readout.DEFAULT_NOISE_S, app.NOISE_CAPTURE_SECONDS_ENGINEERING)
        self.assertEqual(readout.DEFAULT_OFFSET_READS, 24)

    def test_readout_stays_engineering_only(self):
        source = (MODEL_DIR / "daq_rig_readout.py").read_text(encoding="utf-8")
        # no GUI, no verdict engine, no cross-rig import, no results path of its own
        for forbidden in ("import matplotlib", "import tkinter", "from tkinter", "single_detector_rig",
                          "import eltec_40623_array_tester", "Path.home(", "Eltec_40623_Test_Results"):
            self.assertFalse(forbidden in source, f"{forbidden!r} must not appear in daq_rig_readout.py")
        self.assertIn("never issues a", source)
        self.assertIn("__all__", source)

    def test_calibration_status_line_matches_the_analysis_module(self):
        self.assertIsNone(aa.NOISE_PP_LIMIT_LOW_MV)
        self.assertIsNone(aa.NOISE_PP_LIMIT_HIGH_MV)
        self.assertIn("NO_LIMIT", readout.CALIBRATION_STATUS_LINE)
        self.assertIn("section 4b", readout.CALIBRATION_STATUS_LINE)
        self.assertEqual(aa.CALIBRATION_STATUS, "PENDING")
        self.assertEqual(aa.OFFSET_LIMITS_STATUS, "PROVISIONAL")

    def test_nothing_written_under_documents(self):
        self.assert_home_untouched()


if __name__ == "__main__":
    unittest.main()
