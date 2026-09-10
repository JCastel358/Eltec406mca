"""Operator offsets use the waveform stream; no DLL or physical DAQ is opened."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

MODEL_DIR = Path(__file__).resolve().parents[1]
for entry in (str(MODEL_DIR), str(MODEL_DIR.parent)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import array_analysis as aa
import daq_backend as daq
import eltec_40623_array_tester as app
import tray_history
from test_daq_backend import FakeLib


class StreamLib(FakeLib):
    """Immediate reads are unusable, paced bulk contains the detector voltages.

    Model the unsettled first conversion after every channel change separately
    so the test passes through the real ctypes callback and de-interleaver.
    """

    def __init__(self, levels):
        super().__init__()
        self.levels = np.asarray(levels)
        self.scan_counts[:] = 0

    def CTR_StartOutputFreq(self, index, counter, hz):
        status = super().CTR_StartOutputFreq(index, counter, hz)
        if hz._obj.value:
            config = daq.parse_config_block(self.config_block)
            stable = daq.volts_to_counts(self.levels, config.range_code)
            values = np.broadcast_to(stable[None, :, None], (1280, 50, 4)).copy()
            values[:200, :, :] = 60000  # startup samples must also be discarded
            values[:, :, 0] = 62000  # corrupt first conversion at every MUX hop
            raw = values.astype("<u2").tobytes()
            # Driver callbacks need not land on channel or scan boundaries.
            # Cut through a four-conversion channel, then complete it in the
            # next callback; a fresh stream must also reset that carry state.
            self.deliver(raw[:514])
            self.deliver(raw[514:844])
            for start in range(844, len(raw), 64000):
                self.deliver(raw[start:start + 64000])
        return status


class OffsetStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eltec-offset-stream-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def simulated(self, profile=None):
        device = daq.SimulatedDaq(profile or daq.SimProfile(settle_drop_v=0.0), real_time=False)
        device.connect()
        device.configure(daq.AdcConfig())
        self.addCleanup(device.close)
        return device

    def test_operator_uses_paced_stream_and_preserves_all_channel_positions(self):
        # Actual reported arrangement: row 5 only. Different DC values expose
        # a channel swap, interleave mistake, or offset by the oversample count.
        levels = np.full(50, 0.006)
        levels[40:] = np.linspace(0.31, 1.19, 10)
        lib = StreamLib(levels)
        device = daq.AiousbDaq(library=lib, sleep=lambda _: None)
        controller = app.TrayController(device, lot="bulk", tray_number=1, tester_name="Test",
                                       results_root=self.root)
        controller.start()
        self.addCleanup(controller.close)
        for _ in range(3):
            volts = controller.measure_offsets()
            np.testing.assert_allclose(volts, levels, atol=daq.lsb_volts(2))
            self.assertEqual(controller.offset_good_positions(), daq.POSITIONS[40:])
        self.assertFalse(any(call[0] == "ADC_GetScan" for call in lib.calls))
        self.assertFalse(device.is_streaming)
        event = tray_history.read_tray_events(controller.attempts_path)[0]
        detail = json.loads(event.detail)
        self.assertEqual(detail["offset_acquisition_policy"], app.OFFSET_ACQUISITION_POLICY)
        self.assertEqual(detail["offset_acquisitions"][0]["samples_per_channel"], 1000)
        self.assertEqual(detail["offset_acquisitions"][0]["drop_first"], 1)

    def test_millivolt_channels_remain_visible_and_require_occupancy_review(self):
        # Pattern recorded in lot ets, 2026-09-09 14:48:53: healthy 5-10,
        # millivolt channels, high 2-6, low 2-7. Physical occupancy was unknown.
        levels = np.full(50, 0.006)
        levels[15], levels[16], levels[49] = 2.5022, 0.2049, 0.6664
        device = daq.AiousbDaq(library=StreamLib(levels), sleep=lambda _: None)
        controller = app.TrayController(device, lot="logged-pattern", tray_number=1, tester_name="Test",
                                       results_root=self.root)
        controller.start()
        self.addCleanup(controller.close)
        measured = controller.measure_offsets()
        self.assertEqual(controller.offset_good_positions(), ("5-10",))
        self.assertEqual(len(controller.offset_bad_positions()), 49)
        self.assertIs(controller.effective_occupancy("1-1"), aa.Occupancy.LOADED)
        self.assertAlmostEqual(measured[49], 0.6664, delta=daq.lsb_volts(2))
        self.assertGreater(measured[0], 0.005)

    def test_invalid_stream_never_accepts_a_snapshot_and_releases_device(self):
        for invalid in (np.zeros((10, 49)), np.full((10, 50), np.nan)):
            with self.subTest(shape=invalid.shape):
                device = self.simulated()
                with patch.object(device, "read_stream", return_value=invalid):
                    with self.assertRaisesRegex(ValueError, "fifty finite"):
                        app.read_offset_snapshot(device, app.CapturePlan())
                self.assertFalse(device.is_streaming)

    def test_silent_stream_times_out_instead_of_becoming_an_empty_tray(self):
        device = self.simulated()
        elapsed = [0.0]

        def no_data(**kwargs):
            elapsed[0] += 0.25
            return None

        with patch.object(device, "read_stream", side_effect=no_data), \
                patch.object(app.time, "monotonic", side_effect=lambda: elapsed[0]):
            with self.assertRaises(daq.StreamTimeoutError):
                app.read_offset_snapshot(device, replace(app.CapturePlan(), no_data_timeout_s=0.5))
        self.assertFalse(device.is_streaming)

    def test_slow_trickle_is_bounded_even_when_silence_timer_keeps_resetting(self):
        device = self.simulated()
        elapsed = [0.0]

        def trickle(**kwargs):
            elapsed[0] += 0.1
            return np.ones((1, 50))

        with patch.object(device, "read_stream", side_effect=trickle), \
                patch.object(app.time, "monotonic", side_effect=lambda: elapsed[0]):
            with self.assertRaisesRegex(daq.StreamTimeoutError, "complete measurement"):
                app.read_offset_snapshot(device, replace(app.CapturePlan(), no_data_timeout_s=0.5))
        self.assertLess(elapsed[0], 2.0)
        self.assertFalse(device.is_streaming)

    def test_buffer_loss_rejects_offsets_instead_of_classifying_detectors(self):
        device = self.simulated(daq.SimProfile(settle_drop_v=0.0, pool_events_on_attempts=(1,)))
        with self.assertRaisesRegex(daq.StreamIntegrityError, "buffer pool exhausted"):
            app.read_offset_snapshot(device, app.CapturePlan())
        self.assertFalse(device.is_streaming)

    def test_cancel_releases_stream_and_does_not_accept_snapshot(self):
        device = self.simulated()
        checks = [0]

        def cancel():
            checks[0] += 1
            return checks[0] >= 3

        with self.assertRaises(app.CaptureCancelled):
            app.read_offset_snapshot(device, app.CapturePlan(), cancelled=cancel)
        self.assertFalse(device.is_streaming)


if __name__ == "__main__":
    unittest.main()
