from pathlib import Path
import sys

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from eltec_406mca_tester import (  # noqa: E402
    DEFAULT_AM502_GAIN,
    DEFAULT_MAX_CAPTURE_CYCLES,
    DEFAULT_SAMPLE_RATE_HZ,
    FILTER_SPECS_MV,
    POSITIVE_POLARITY,
    EXPECTED_FREQUENCY_HZ,
    analyze_waveform,
    evaluate_result,
    simulate_waveform_samples,
)


def variable_amplitude_waveform(
    amplitudes_mv: list[float],
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    frequency_hz: float = EXPECTED_FREQUENCY_HZ,
    am502_gain: float = DEFAULT_AM502_GAIN,
) -> tuple[np.ndarray, np.ndarray, float]:
    duration_s = len(amplitudes_mv) / frequency_hz
    sample_count = int(round(duration_s * sample_rate_hz))
    t = np.arange(sample_count, dtype=float) / sample_rate_hz
    phase = (t * frequency_hz) % 1.0
    cycle_index = np.minimum((t * frequency_hz).astype(int), len(amplitudes_mv) - 1)
    amplitudes_v = np.asarray(amplitudes_mv, dtype=float)[cycle_index] / 1000.0 * am502_gain

    triangle = np.where(phase < 0.5, -1.0 + 4.0 * phase, 3.0 - 4.0 * phase)
    waveform = triangle * (amplitudes_v / 2.0)
    sync = np.where(phase < 0.5, 5.0, 0.0)
    return waveform, sync, sample_rate_hz


def run_sim_case(filter_setup: str, case_name: str, offset_v: float, noise_rms_v: float = 0.0):
    np.random.seed(1)
    waveform, sync, sample_rate = simulate_waveform_samples(
        filter_setup=filter_setup,
        case_name=case_name,
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
        cycles=DEFAULT_MAX_CAPTURE_CYCLES,
        am502_gain=DEFAULT_AM502_GAIN,
        noise_rms_v=noise_rms_v,
    )
    metrics = analyze_waveform(
        waveform,
        sync,
        sample_rate,
        am502_gain=DEFAULT_AM502_GAIN,
    )
    final = evaluate_result(offset_v, metrics, filter_setup)
    return metrics, final


def test_known_good_passes():
    metrics, final = run_sim_case("-3 filter", "Known good", 0.75)
    assert final.passed, final.fail_reasons
    assert metrics.stabilized
    assert metrics.polarity == POSITIVE_POLARITY
    assert metrics.sensitivity_mv > FILTER_SPECS_MV["-3 filter"]
    assert abs(metrics.measured_frequency_hz - 10.0) < 0.01


def test_low_sensitivity_fails():
    _metrics, final = run_sim_case("-3 filter", "Low sensitivity", 0.75)
    assert not final.passed
    assert any("Sensitivity too low" in reason for reason in final.fail_reasons)


def test_wrong_polarity_fails():
    _metrics, final = run_sim_case("-3 filter", "Wrong polarity", 0.75)
    assert not final.passed
    assert any("Polarity" in reason for reason in final.fail_reasons)


def test_low_and_high_offset_fail():
    _metrics, low = run_sim_case("-3 filter", "Known good", 0.10)
    _metrics, high = run_sim_case("-3 filter", "Known good", 1.35)
    assert not low.passed
    assert not high.passed
    assert any("Offset out of range" in reason for reason in low.fail_reasons)
    assert any("Offset out of range" in reason for reason in high.fail_reasons)


def test_am502_gain_is_divided_out():
    filter_setup = "-3 filter"
    waveform, sync, sample_rate = simulate_waveform_samples(
        filter_setup=filter_setup,
        case_name="Known good",
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
        cycles=DEFAULT_MAX_CAPTURE_CYCLES,
        am502_gain=DEFAULT_AM502_GAIN,
        noise_rms_v=0.0,
    )
    metrics = analyze_waveform(
        waveform,
        sync,
        sample_rate,
        am502_gain=DEFAULT_AM502_GAIN,
    )
    expected_mv = FILTER_SPECS_MV[filter_setup] * 1.35
    assert abs(metrics.sensitivity_mv - expected_mv) < 0.5
    assert metrics.sensitivity_amplified_mv > metrics.sensitivity_mv * 90.0


def test_slow_settling_waveform_uses_stable_cycles():
    filter_setup = "-3 filter"
    settled_mv = FILTER_SPECS_MV[filter_setup] * 1.35
    waveform, sync, sample_rate = variable_amplitude_waveform(
        [
            6.0,
            12.0,
            18.0,
            26.0,
            31.0,
            33.0,
            settled_mv,
            settled_mv,
            settled_mv,
            settled_mv,
            settled_mv,
            settled_mv,
        ]
    )
    metrics = analyze_waveform(waveform, sync, sample_rate, am502_gain=DEFAULT_AM502_GAIN)
    final = evaluate_result(0.75, metrics, filter_setup)
    assert final.passed, final.fail_reasons
    assert metrics.stabilized
    assert metrics.stabilization_cycle is not None and metrics.stabilization_cycle >= 5
    assert abs(metrics.sensitivity_mv - settled_mv) < 0.75


def test_unstable_waveform_fails():
    filter_setup = "-3 filter"
    waveform, sync, sample_rate = variable_amplitude_waveform(
        [12.0, 17.0, 22.0, 27.0, 32.0, 37.0, 42.0, 47.0, 52.0, 57.0, 62.0, 67.0]
    )
    metrics = analyze_waveform(waveform, sync, sample_rate, am502_gain=DEFAULT_AM502_GAIN)
    final = evaluate_result(0.75, metrics, filter_setup)
    assert not metrics.stabilized
    assert not final.passed
    assert any("did not stabilize" in reason for reason in final.fail_reasons)


def main():
    tests = [
        test_known_good_passes,
        test_low_sensitivity_fails,
        test_wrong_polarity_fails,
        test_low_and_high_offset_fail,
        test_am502_gain_is_divided_out,
        test_slow_settling_waveform_uses_stable_cycles,
        test_unstable_waveform_fails,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("All analysis tests passed.")


if __name__ == "__main__":
    main()
