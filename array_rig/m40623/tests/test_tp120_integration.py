"""Production analysis, calibration identity and archived evidence end to end."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from test_adaptive_settling import ScriptedDaq, lock_for
import array_analysis as aa
import daq_backend as daq
import eltec_40623_array_tester as app
import legacy_noise as ln


def signal(indices):
    data = np.full((len(indices), 50), 0.7)
    data[:, 40:] += (0.002 * np.sin(2*np.pi*3*indices/1000))[:, None]
    return data


class ProductionNoiseIntegrationTests(unittest.TestCase):
    def capture(self, calibration=None, *, old_limits=aa.NoiseLimits()):
        device = ScriptedDaq(signal, chunks=(37, 1601, 403))
        device.info = replace(device.info, simulated=False)
        self.addCleanup(device.close)
        plan = app.CapturePlan(capture_seconds=60, quiet_min_s=3, quiet_max_s=3)
        report = app.run_tray_capture(device, plan, lock_for(*range(40, 50)),
                                      noise_limits=old_limits, noise_calibration=calibration)
        return device, plan, report

    def test_real_path_uses_3hz_and_never_uses_old_peak_limits(self):
        device, plan, report = self.capture(old_limits=aa.NoiseLimits(low_mv=0, high_mv=1e-12))
        self.assertIsNone(report.rig_fault)
        self.assertEqual(len(report.results), 10)
        for result in report.results:
            self.assertIs(result.verdict, aa.PositionVerdict.NO_LIMIT)
            self.assertFalse(result.passed)
            self.assertAlmostEqual(result.noise.legacy.band_rms_mv, np.sqrt(2), delta=.04)
            self.assertEqual(result.noise.legacy.method_id, ln.METHOD_ID)
        with tempfile.TemporaryDirectory() as directory:
            path = app.save_tray_raw_capture(Path(directory)/'raw.npz', report.capture, lock_for(*range(40, 50)),
                                            lot='test', tray_number=1, tray_attempt=1, daq_info=device.info, plan=plan)
            with np.load(path, allow_pickle=False) as saved:
                evidence = json.loads(str(saved['noise_analysis_json']))
                self.assertEqual(evidence[40]['position'], '5-1')
                self.assertEqual(evidence[40]['verdict'], 'NO_LIMIT')
                self.assertEqual(evidence[40]['acquisition']['drop_first'], plan.drop_first)
                self.assertEqual(evidence[40]['acquisition']['input_gain'], 1)
            row = app.position_row(report.results[0], app.RowContext(
                lot='test', tray_number=1, tray_attempt=1, tester_name='Test',
                daq_serial=device.info.serial_number, simulated=False, plan=plan))
            self.assertEqual(row['pass_fail'], 'CALIBRATION PENDING')
            self.assertGreater(float(row['noise_3hz_rms_uv']), 1000)
            self.assertEqual(json.loads(row['noise_analysis_json'])['method_id'], ln.METHOD_ID)

    def test_qualified_metric_provenance_survives_overall_result(self):
        device, plan, report = self.capture()
        signature = json.loads(report.results[0].noise.legacy.acquisition_json)
        calibration = ln.LegacyNoiseCalibration.from_dict({
            'schema_version': 1, 'method_id': ln.METHOD_ID,
            'calibration_id': 'SYNTHETIC-REGRESSION-ONLY', 'provenance': 'Synthetic test evidence, not hardware qualification',
            'metric': 'band_mean_abs_mv', 'low_mv': .8, 'high_mv': 1.5,
            'background_metric_mv': .01, 'minimum_resolvable_metric_mv': .05,
            'acquisition': signature,
        })
        _, _, report = self.capture(calibration)
        result = report.results[0]
        self.assertTrue(result.passed)
        self.assertEqual(result.calibration_status, 'NOISE_QUALIFIED')
        self.assertEqual(result.calibration_id, calibration.calibration_id)
        self.assertEqual(result.noise.legacy.as_dict()['calibration'], calibration.as_dict())
        self.assertTrue(result.provisional)  # Offset qualification remains separate.

    def test_header_upgrade_failure_keeps_original_file(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'old.csv'
            original = 'position,custom_column\r\n5-1,retain\r\n'
            path.write_bytes(original.encode())
            with patch.object(app.os, 'replace', side_effect=OSError('locked')):
                with self.assertRaises(OSError):
                    app.append_position_rows(path, [{'position': '5-2', 'noise_3hz_rms_uv': '1.2'}])
            self.assertEqual(path.read_bytes(), original.encode())
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            app.append_position_rows(path, [{'position': '5-2', 'noise_3hz_rms_uv': '1.2'}])
            with path.open(newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]['custom_column'], 'retain')
            self.assertEqual(rows[1]['noise_3hz_rms_uv'], '1.2')


if __name__ == '__main__':
    unittest.main()
