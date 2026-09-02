"""The engineer tools that read the array rig's captures: parity derivation and the replot tool.

Both tools live in ``engineer_tools/`` (shared across rigs); they are exercised
here on synthetic tray captures in a temporary directory so the array suite
proves they still understand the tester's ``.npz`` / CSV formats.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
MODEL_DIR = TESTS_DIR.parent
REPO_ROOT = MODEL_DIR.parents[1]
TOOLS_DIR = REPO_ROOT / "engineer_tools"
for entry in (str(MODEL_DIR), str(MODEL_DIR.parent)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import array_analysis as aa  # noqa: E402
import daq_backend as daq  # noqa: E402
import eltec_40623_array_tester as app  # noqa: E402


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_tray_npz(path: Path, *, lot: str, tray: int, seconds: float = 20.0, seed: int = 1) -> dict[str, float]:
    """Write a tray capture the way the tester does; return the per-position noise rms used (uV)."""

    rng = np.random.default_rng(seed)
    n = int(seconds * 1000)
    rms_uv = {p: float(rng.uniform(30.0, 120.0)) for p in daq.POSITIONS}
    waveform = np.empty((50, n), dtype=np.float32)
    for c, position in enumerate(daq.POSITIONS):
        waveform[c] = 0.7 + rng.normal(0.0, rms_uv[position] * 1e-6, n)
    occupancy = [aa.Occupancy.LOADED] * 50
    occupancy[9] = aa.Occupancy.EMPTY
    numbers = app.assign_sensor_numbers(occupancy, 1)
    lock = app.LockSnapshot(occupancy=occupancy, sensor_numbers=numbers, offset_initial_v=np.full(50, 0.7),
                            ho_positions=(), start_number=1, locked_at="t")
    capture = app.TrayCapture(
        waveform_v=waveform, sample_rate_hz=1000.0, actual_timer_hz=1000.0,
        left_context_v=np.full((50, 310), 0.7, dtype=np.float32), right_context_v=np.full((50, 310), 0.7, dtype=np.float32),
        diagnostics=None, quiet_wait_s=3.0, quiet_settled=True, stabilisation_wait_s=0.0, started_at="t", attempts_used=1,
    )
    info = daq.DaqInfo(name="sim", serial_number="S", device_index=0, product_id=0x8145, dll_version="x", simulated=True)
    app.save_tray_raw_capture(path, capture, lock, lot=lot, tray_number=tray, tray_attempt=1, daq_info=info, plan=app.CapturePlan())
    return rms_uv


class ParityToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tool = load_tool("array_noise_parity")

    def make_lot(self, factor: float = 250.0) -> tuple[Path, Path, dict]:
        captures = self.root / "noise_captures" / "lot_12"
        captures.mkdir(parents=True)
        synthetic_tray_npz(captures / "tray_1_raw.npz", lot="12", tray=1)
        tray = self.tool.load_tray_npz(captures / "tray_1_raw.npz")
        records = self.tool.analyze_tray(tray)
        # legacy readings = factor x the array's worst window pk-pk (mV), plus 3 % scatter
        rng = np.random.default_rng(5)
        legacy = self.root / "legacy_readings.csv"
        with legacy.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sensor_id", "position", "legacy_noise_mv"])
            for record in records:
                writer.writerow([record["sensor_id"], record["position"], f"{factor * record['worst'] * rng.normal(1.0, 0.03):.3f}"])
            writer.writerow(["12-999", "9-9", "20.0"])  # no array partner -> ignored
        return legacy, captures, {r["sensor_id"]: r for r in records}

    def test_npz_pairs_and_proposes_limits(self):
        legacy, captures, records = self.make_lot(factor=250.0)
        self.assertEqual(len(records), 49)  # position 1-10 was empty
        out = self.root / "cal"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = self.tool.main(["--legacy", str(legacy), "--array", str(captures), "--out", str(out), "--no-plot"])
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("49 paired parts", text)
        self.assertIn("NOISE_PP_LIMIT_LOW_MV", text)
        pairs_csv = next(out.glob("parity_*.csv"))
        with pairs_csv.open(newline="", encoding="utf-8") as handle:
            rows = [r for r in csv.reader(handle) if r]
        self.assertEqual(rows[0][0], "sensor_id")
        self.assertEqual(len([r for r in rows[1:] if len(r) > 3]), 49)
        # the fitted factor on the worst metric is the one we planted
        pairs = self.tool.pair(self.tool.read_legacy(legacy), self.tool.collect_array(captures))
        fit = self.tool.fit(pairs, "worst")
        self.assertAlmostEqual(fit["median_ratio"], 250.0, delta=250.0 * 0.05)
        self.assertAlmostEqual(fit["slope"], 250.0, delta=250.0 * 0.05)
        low, high = self.tool.proposed_limits(fit["median_ratio"])
        self.assertAlmostEqual(low, 10.0 / fit["median_ratio"])
        self.assertAlmostEqual(high, 37.9 / fit["median_ratio"])

    def test_lot_csv_input_and_position_fallback(self):
        legacy, captures, records = self.make_lot()
        lot_csv = self.root / "40623_array_lot_12.csv"
        with lot_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["sensor_id", "tray_number", "position", "tray_attempt", "noise_worst_pp_mv", "noise_median_pp_mv"])
            writer.writeheader()
            for record in records.values():
                writer.writerow({"sensor_id": record["sensor_id"], "tray_number": "1", "position": record["position"], "tray_attempt": "1",
                                 "noise_worst_pp_mv": f"{record['worst']:.6f}", "noise_median_pp_mv": f"{record['median']:.6f}"})
        array = self.tool.collect_array(lot_csv)
        pairs = self.tool.pair(self.tool.read_legacy(legacy), array)
        self.assertEqual(len(pairs), 49)
        # legacy sheet without ids: pair on tray + position
        no_ids = self.root / "legacy_positions.csv"
        with no_ids.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["tray", "position", "legacy_noise_mv"])
            writer.writerow(["1", "1-1", "25.0"])
            writer.writerow(["1", "1-10", "25.0"])  # empty position -> no partner
        pairs = self.tool.pair(self.tool.read_legacy(no_ids), array)
        self.assertEqual([p["array_position"] for p in pairs], ["1-1"])

    def test_replay_band_uses_npz(self):
        legacy, captures, _ = self.make_lot()
        array = self.tool.collect_array(captures, band=(0.5, 5.0))
        self.assertEqual(len([k for k in array if k[0] == "id"]), 49)
        with self.assertRaises(SystemExit):
            self.tool.collect_array(self.root / "nothing.csv", band=(0.5, 5.0))

    def test_decisions_agree_counts(self):
        pairs = [{"legacy_mv": 20.0, "worst": 0.08, "median": 0.06}, {"legacy_mv": 50.0, "worst": 0.2, "median": 0.15}, {"legacy_mv": 5.0, "worst": 0.02, "median": 0.01}]
        self.assertEqual(self.tool.decisions_agree(pairs, "worst", 250.0), (3, 3))


class ReplotTrayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tool = load_tool("replot_noise_capture")

    def test_tray_capture_is_split_per_position_and_replays_the_tester_numbers(self):
        path = self.root / "tray_3_raw.npz"
        synthetic_tray_npz(path, lot="12", tray=3, seconds=5.0)
        self.assertTrue(self.tool.is_tray_capture(path))
        entries = self.tool.load_tray_channels(path)
        self.assertEqual(len(entries), 49)  # 1-10 empty
        self.assertEqual(entries[0]["name"], "tray_3_1-1")
        self.assertEqual(entries[0]["channel"], 0)
        self.assertEqual(entries[0]["sensor_number"], 1)
        self.assertEqual(entries[0]["left_context_v"].shape, (310,))
        only = self.tool.load_tray_channels(path, positions={"2-4"})
        self.assertEqual([e["position"] for e in only], ["2-4"])
        by_channel = self.tool.load_tray_channels(path, channels={49})
        self.assertEqual([e["position"] for e in by_channel], ["5-10"])
        entry = self.tool.analyze_file(path, [("app", None)], None, 0.15, True, capture=only[0])
        self.assertTrue(entry["no_limit"])
        replay = entry["results"][0]["analysis"]
        # the tester's numpy port and this replay (pure-Python 405 functions) agree
        tray = np.load(path)
        raw = np.asarray(tray["waveform_v"], dtype=float)
        results, _, _ = aa.analyze_tray_noise(raw, 1000.0, positions=list(daq.POSITIONS),
                                              left_context=np.asarray(tray["left_context_v"], dtype=float),
                                              right_context=np.asarray(tray["right_context_v"], dtype=float))
        channel = daq.channel_for_position("2-4")
        self.assertAlmostEqual(replay.worst_pp_mv, results[channel].worst_pp_mv, places=9)
        self.assertAlmostEqual(replay.median_pp_mv, results[channel].median_pp_mv, places=9)

    def test_collect_paths_finds_tray_captures(self):
        captures = self.root / "lot_1"
        captures.mkdir()
        synthetic_tray_npz(captures / "tray_1_raw.npz", lot="1", tray=1, seconds=2.0)
        files = self.tool.collect_paths([str(self.root)])
        self.assertEqual([f.name for f in files], ["tray_1_raw.npz"])
        self.assertIn("40623", self.tool.CAPTURE_ROOTS)


if __name__ == "__main__":
    unittest.main()
