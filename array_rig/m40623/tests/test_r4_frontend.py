"""R4 logic, replay, calibrated transfer and shutdown fault injection; no hardware."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daq_backend as daq
from daq_rig_readout import Capture, LiveStream, ArrayRig
from r4_frontend import (PROFILE_ID, R4Rig, R4SimulatedDaq, analyze_capture,
                         save_capture, load_capture, validate_calibration)
from test_daq_backend import FakeClock, FakeLib, make_device


def calibration():
    transfer = {"frequency_hz": [1., 3., 10., 30., 50., 100.],
                "gain_v_per_v": [900.] * 6, "phase_deg": [0.] * 6,
                "relative_uncertainty": .005}
    return {"schema_version": 1, "frontend_profile": PROFILE_ID, "calibration_id": "test-only",
            "board_serial": "TEST", "daq_serial": "SIM", "provenance": "Synthetic unit test only",
            "acquisition": {mode: {"range_code": code, "sample_rate_hz": 2000.,
                                    "oversample_extra": 3, "drop_first": 1}
                            for mode, code in (("dc", 2), ("noise", 7))},
            "modes": {"noise": {"0": transfer}, "dc": {"0": {
                **transfer, "gain_v_per_v": [1.] * 6, "offset_v": .01, "dc_gain": 1.02}}}}


def sine_capture(freq=3., seconds=10., amplitude=0.009, rate=2000.):
    raw = amplitude * np.sin(2 * np.pi * freq * np.arange(round(rate*seconds))/rate)
    cap = Capture(raw[None], rate, rate, (0,), ("1-1",), 7, 3, 1, None,
                  "test", "SIM", "simulator", True, quiet=True)
    metadata = {"schema_version": 1, "frontend_profile": PROFILE_ID, "board_revision": "R4",
                "board_serial": "TEST", "daq_serial": "SIM", "mode": "noise", "channels": [0],
                "range_code": 7, "oversample_extra": 3, "drop_first": 1,
                "actual_sample_rate_hz": rate, "nominal_sample_rate_hz": rate,
                "simulated": True, "integrity_final": True, "integrity_reasons": [],
                "diagnostics": None, "calibration": calibration(), "recorded_at": "test"}
    metadata["calibration"]["acquisition"]["noise"]["sample_rate_hz"] = rate
    return cap, metadata


class R4Modes(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.device = R4SimulatedDaq()
        self.rig = R4Rig(board_serial="TEST", dedicated_ports=True, device=self.device, quiet=True,
                         monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.rig.connect()

    def tearDown(self):
        self.device.power_ready = True
        self.rig.close()

    def test_only_two_modes_and_safe_sequences(self):
        self.device.control_history.clear()
        self.rig.set_frontend_mode("noise")
        self.assertEqual(self.device.control_history, [(False, False), (True, False), (True, True)])
        self.assertEqual(self.clock.sleeps[-1], .2)
        self.assertEqual(self.rig.range_code, 7)
        self.device.control_history.clear()
        self.rig.set_frontend_mode("dc")
        self.assertEqual(self.device.control_history, [(True, False), (False, False)])
        self.assertEqual(self.clock.sleeps[-1], .05)
        self.assertEqual(self.rig.range_code, 2)
        with self.assertRaises(ValueError):
            self.rig.set_frontend_mode("10x")

    def test_settling_and_connection_gate(self):
        with self.assertRaises(daq.StreamStateError):
            self.rig.capture_r4(.1)
        self.clock.now += 91.
        self.rig.capture_r4(.1)
        self.rig.detector_connection_changed()
        self.assertEqual(self.rig.settling_remaining_s, 90.)

    def test_unsettled_override_remains_invalid(self):
        cap, metadata = self.rig.capture_r4(.1, diagnostic_reason="test startup")
        self.assertTrue(any("UNSETTLED" in reason for reason in metadata["integrity_reasons"]))
        rows = analyze_capture(cap, metadata)["channels"]
        self.assertTrue(all(row["status"] == "INVALID" for row in rows))

    def test_missing_power_is_rejected_in_dc(self):
        self.device.power_ready = False
        with self.assertRaises(daq.StreamIntegrityError):
            self.rig.capture_r4(.1, diagnostic_reason="cannot bypass power")

    def test_power_loss_during_capture_retains_signed_partial_data(self):
        self.rig.set_frontend_mode("noise")
        self.clock.now += 91.
        original = self.device.read_stream
        def fail_after_chunk(**kwargs):
            chunk = original(**kwargs)
            self.device.power_ready = False
            return chunk
        self.device.read_stream = fail_after_chunk
        cap, metadata = self.rig.capture_r4(.3)
        self.assertLess(cap.samples, 600)
        self.assertTrue(np.any(cap.volts < 0))
        self.assertTrue(metadata["integrity_reasons"])
        self.assertFalse(self.device.is_streaming)

    def test_mode_change_drains_ends_old_stream(self):
        self.device.start_stream(scan_hz=2000)
        self.device.read_stream()
        generation = self.rig.generation
        self.rig.set_frontend_mode("noise")
        self.assertFalse(self.device.is_streaming)
        self.assertEqual(self.rig.generation, generation + 1)
        self.assertIsNotNone(self.rig.last_transition_diagnostics)

    def test_nominal_gain_never_used_as_calibration(self):
        self.rig.set_frontend_mode("noise")
        self.clock.now += 91.
        cap, metadata = self.rig.capture_r4(10.)
        rows = analyze_capture(cap, metadata)["channels"]
        self.assertTrue(all(row["status"] == "UNCALIBRATED" for row in rows))
        self.assertTrue(all("input_band_rms_uv" not in row for row in rows))


class R4Analysis(unittest.TestCase):
    def test_measured_gain_rms_psd_mean_abs_and_hold(self):
        cap, metadata = sine_capture(seconds=100.)
        row = analyze_capture(cap, metadata)["channels"][0]
        self.assertAlmostEqual(row["input_band_rms_uv"], 10 / np.sqrt(2), places=9)
        self.assertAlmostEqual(row["input_mean_abs_uv"], 20/np.pi, places=4)
        self.assertAlmostEqual(sum(row["input_psd_uv2_per_hz"])*.01, 50., places=8)
        self.assertTrue(row["hold_complete"])
        self.assertTrue(row["smooth_settled"])
        self.assertAlmostEqual(row["input_smoothed_abs_final_uv"], 20/np.pi, delta=.03)
        self.assertEqual(row["acceptance"], "NO_LIMIT")

    def test_band_edges_and_3hz_q3(self):
        for freq, included in ((1, True), (100, True), (101, False)):
            cap, metadata = sine_capture(freq=freq)
            value = analyze_capture(cap, metadata)["channels"][0]["input_band_rms_uv"]
            self.assertAlmostEqual(value, 10/np.sqrt(2) if included else 0, places=8)
        cap, metadata = sine_capture()
        self.assertAlmostEqual(analyze_capture(cap, metadata, profile="3hz_q3")["channels"][0]["input_band_rms_uv"], 10/np.sqrt(2), places=8)

    def test_replay_equivalence_and_raw_samples(self):
        cap, metadata = sine_capture(rate=1999.)
        with tempfile.TemporaryDirectory() as tmp:
            path = save_capture(Path(tmp)/"record.npz", cap, metadata)
            replay, meta = load_capture(path)
        np.testing.assert_array_equal(replay.volts, cap.volts)
        self.assertEqual(analyze_capture(cap, metadata), analyze_capture(replay, meta))

    def test_clipping_stationarity_and_integrity_cannot_pass(self):
        cap, metadata = sine_capture(amplitude=1.)
        row = analyze_capture(cap, metadata)["channels"][0]
        self.assertEqual(row["status"], "INVALID")
        cap, metadata = sine_capture()
        cap.volts[:, :cap.samples//2] *= 10
        row = analyze_capture(cap, metadata)["channels"][0]
        self.assertIn("NONSTATIONARY", " ".join(row["reasons"]))
        metadata["integrity_reasons"] = ["USB failure"]
        self.assertEqual(analyze_capture(cap, metadata)["channels"][0]["status"], "INVALID")

    def test_reject_legacy_and_invalid_calibration(self):
        cal = calibration()
        cal["modes"]["10x"] = {}
        with self.assertRaises(ValueError):
            validate_calibration(cal)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"legacy.npz"
            np.savez(path, waveform_v=np.zeros((1, 100)), sample_rate_hz=50)
            with self.assertRaisesRegex(ValueError, "Historical"):
                load_capture(path)

    def test_feedback_clamp_headroom_uses_raw_signed_voltage(self):
        for amplitude, expected in ((0.79, False), (0.81, True)):
            cap, metadata = sine_capture(amplitude=amplitude)
            row = analyze_capture(cap, metadata)["channels"][0]
            self.assertEqual(any("NOISE_HEADROOM" in s for s in row["reasons"]), expected)
            self.assertEqual(row["clipped_samples"], 0)
        cap, metadata = sine_capture(amplitude=0.01)
        cap.volts += 0.82
        self.assertIn("NOISE_HEADROOM", " ".join(analyze_capture(cap, metadata)["channels"][0]["reasons"]))
        metadata["mode"] = "dc"
        cap.range_code = 2
        row = analyze_capture(cap, metadata)["channels"][0]
        self.assertNotIn("NOISE_HEADROOM", " ".join(row["reasons"]))

    def test_nonfinite_and_range_mismatch_remain_serializable_invalid(self):
        cap, metadata = sine_capture()
        cap.volts[0, 0] = np.nan
        result = analyze_capture(cap, metadata)
        self.assertEqual(result["channels"][0]["status"], "INVALID")
        json.dumps(result, allow_nan=False)
        cap, metadata = sine_capture()
        cap.range_code = 5
        result = analyze_capture(cap, metadata)["channels"][0]
        self.assertEqual(result["status"], "INVALID")
        self.assertNotIn("input_band_rms_uv", result)

    def test_short_hold_and_repeated_analysis_reset(self):
        cap, metadata = sine_capture(seconds=10.)
        first = analyze_capture(cap, metadata)
        self.assertFalse(first["channels"][0]["hold_complete"])
        self.assertFalse(first["channels"][0]["smooth_settled"])
        self.assertEqual(first, analyze_capture(cap, metadata))


class ShutdownIntegrity(unittest.TestCase):
    def test_dio_byte_directions_and_conditioned_readback(self):
        device, lib, clock = make_device()
        records = []
        def configure(index, tristate, mask, data):
            records.append((int(mask[0]), int(data[0]), int(data[1])))
            return 0
        def write(index, data):
            records.append(int(data[0]))
            return 0
        def read(index, data):
            data[1] = 0b111
            return 0
        lib.DIO_Configure, lib.DIO_WriteAll, lib.DIO_ReadAll = configure, write, read
        device.connect()
        with self.assertRaises(ValueError):
            device.configure_r4_dio()
        device.configure_r4_dio(dedicated_ports=True)
        device.write_r4_controls(True, True)
        self.assertEqual(records, [(1, 0, 0), 3])
        self.assertEqual(device.read_r4_controls(), (True, True, True))
        with self.assertRaises(ValueError):
            device.write_r4_controls(False, True)

    def test_clock_stop_and_worker_status_preserved(self):
        device, lib, clock = make_device()
        device.connect()
        device.configure(daq.AdcConfig())
        device.start_stream(scan_hz=1000)
        lib.status_overrides["CTR_StartOutputFreq"] = 5
        def end(index, status):
            status._obj.value = 123
            return 0
        lib.ADC_BulkContinuousEnd = end
        diagnostics = device.stop_stream()
        self.assertEqual(diagnostics.clock_stop_status, 5)
        self.assertEqual(diagnostics.bulk_worker_status, 123)
        self.assertIn("123", ";".join(diagnostics.problems()))
        self.assertFalse(device._consumer.is_alive())

    def test_end_failure_still_cleans_consumer_and_keeps_callback(self):
        device, lib, clock = make_device()
        device.connect()
        device.configure(daq.AdcConfig())
        device.start_stream(scan_hz=1000)
        lib.status_overrides["ADC_BulkContinuousEnd"] = 6
        diagnostics = device.stop_stream()
        self.assertEqual(diagnostics.bulk_end_status, 6)
        self.assertFalse(device._consumer.is_alive())
        self.assertIsNotNone(device._callback)

    def test_live_final_failure_is_error_and_is_saved(self):
        class Fault(daq.SimulatedDaq):
            def stop_stream(self, **kwargs):
                result = super().stop_stream(**kwargs)
                result.bulk_worker_status = 19
                return result
        rig = ArrayRig(device=Fault(real_time=True), quiet=True)
        rig.connect()
        live = rig.live_stream()
        live.start()
        self.assertTrue(live.wait_ready())
        live.stop()
        self.assertIn("19", live.error)
        rig.close()

    def test_existing_launcher_routes_explicit_r4_profile_only(self):
        import daq_r4
        import eltec_40623_array_tester as tester
        with patch.object(daq_r4, "main", return_value=0) as launch:
            self.assertEqual(tester.main(["--frontend", "r4", "--simulate"]), 0)
        self.assertEqual(launch.call_args.args[0], ["--gui", "--simulate"])


if __name__ == "__main__":
    unittest.main()
