"""
Live signal-change monitor for the Eltec 406MCA setup.

This script watches the incoming LabJack signal continuously instead of taking
one pass/fail snapshot. It records an initial baseline, then reports whether the
current AIN0 waveform has changed enough to show that the setup is responding.

Default wiring matches eltec_406mca_tester.py:
    AIN0: sensor or conditioned waveform
    AIN2: blade sync signal

Run:
    python eltec_406mca_signal_monitor.py

Use simulator mode without hardware:
    python eltec_406mca_signal_monitor.py --simulator
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from eltec_406mca_tester import (
    DEFAULT_AM502_GAIN,
    DEFAULT_EMITTER_PWM_CHANNEL,
    DEFAULT_EMITTER_PWM_DUTY_CYCLE,
    DEFAULT_EMITTER_PWM_FREQUENCY_HZ,
    DEFAULT_SAMPLE_RATE_HZ,
    DEFAULT_WAVEFORM_INPUT_RANGE_LABEL,
    EXPECTED_FREQUENCY_HZ,
    FILTER_SPECS_MV,
    LABJACK_AIN0_RANGE_OPTIONS,
    LABJACK_T7_PWM_CHANNELS,
    SIM_CASES,
    SYNC_CHANNEL,
    WAVEFORM_CHANNEL,
    WAVEFORM_SETTLING_READS,
    LabJackT7,
    calculate_pwm_roll_and_config,
    cycle_peak_to_peak_values,
    cycle_segments_from_edges,
    find_sync_edges,
    normalize_pwm_channel,
    labjack_ain0_range_from_label,
    labjack_range_offset_warning,
    ljm,
)


DEFAULT_WINDOW_SECONDS = 1.0
DEFAULT_BASELINE_SECONDS = 3.0
DEFAULT_PP_CHANGE_THRESHOLD_MV = 5.0
DEFAULT_MEAN_CHANGE_THRESHOLD_MV = 10.0
DEFAULT_RMS_CHANGE_THRESHOLD_MV = 3.0
DEFAULT_REFRESH_SECONDS = 0.5
CSV_FIELDS = [
    "timestamp",
    "elapsed_s",
    "changed",
    "waveform_pp_mv",
    "delta_pp_mv",
    "mean_v",
    "delta_mean_mv",
    "rms_ac_mv",
    "delta_rms_mv",
    "frequency_hz",
]


@dataclass
class SignalStats:
    mean_v: float
    pp_mv: float
    rms_ac_mv: float
    min_v: float
    max_v: float
    frequency_hz: float | None
    warnings: list[str]


@dataclass
class ChangeResult:
    changed: bool
    delta_pp_mv: float
    delta_mean_mv: float
    delta_rms_mv: float
    reasons: list[str]


class LiveLabJackStream:
    def __init__(
        self,
        sample_rate_hz: float,
        waveform_range_v: float,
        emitter_pwm_enabled: bool = False,
        emitter_pwm_channel: str = DEFAULT_EMITTER_PWM_CHANNEL,
        emitter_pwm_frequency_hz: float = DEFAULT_EMITTER_PWM_FREQUENCY_HZ,
        emitter_pwm_duty_cycle: float = DEFAULT_EMITTER_PWM_DUTY_CYCLE,
    ) -> None:
        self.sample_rate_hz = float(sample_rate_hz)
        self.waveform_range_v = float(waveform_range_v)
        self.emitter_pwm_enabled = bool(emitter_pwm_enabled)
        self.emitter_pwm_channel = normalize_pwm_channel(emitter_pwm_channel)
        self.emitter_pwm_frequency_hz = float(emitter_pwm_frequency_hz)
        self.emitter_pwm_duty_cycle = float(emitter_pwm_duty_cycle)
        self.device = LabJackT7()
        self.actual_scan_rate_hz = self.sample_rate_hz
        self.scan_names = [SYNC_CHANNEL] + [WAVEFORM_CHANNEL] * WAVEFORM_SETTLING_READS
        self.scan_list: list[int] | None = None
        self.started = False

    def __enter__(self) -> "LiveLabJackStream":
        if ljm is None:
            raise RuntimeError("The labjack.ljm Python package is not available.")

        self.device.connect()
        self.device.configure_analog_inputs(waveform_range_v=self.waveform_range_v)
        if self.emitter_pwm_enabled:
            self.device.configure_emitter_pwm(
                channel=self.emitter_pwm_channel,
                frequency_hz=self.emitter_pwm_frequency_hz,
                duty_cycle_percent=self.emitter_pwm_duty_cycle,
            )
        self.scan_list = ljm.namesToAddresses(len(self.scan_names), self.scan_names)[0]
        scans_per_read = max(20, int(self.sample_rate_hz / 10.0))
        self.actual_scan_rate_hz = float(
            ljm.eStreamStart(
                self.device.handle,
                scans_per_read,
                len(self.scan_names),
                self.scan_list,
                self.sample_rate_hz,
            )
        )
        self.started = True
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.started:
            try:
                ljm.eStreamStop(self.device.handle)
            except Exception:
                pass
            self.started = False
        if self.emitter_pwm_enabled:
            self.device.disable_emitter_pwm(self.emitter_pwm_channel)
        self.device.close()

    def read_window(self, duration_s: float) -> tuple[np.ndarray, np.ndarray, float]:
        if not self.started:
            raise RuntimeError("LabJack stream is not started.")

        target_scans = max(1, int(round(duration_s * self.actual_scan_rate_hz)))
        waveform: list[float] = []
        sync: list[float] = []

        while len(waveform) < target_scans:
            data, _device_backlog, _ljm_backlog = ljm.eStreamRead(self.device.handle)
            arr = np.asarray(data, dtype=float).reshape((-1, len(self.scan_names)))
            valid = np.all(arr > -9998.0, axis=1)
            arr = arr[valid]
            if arr.size == 0:
                continue
            sync.extend(arr[:, 0].tolist())
            waveform.extend(arr[:, -1].tolist())

        return (
            np.asarray(waveform[:target_scans], dtype=float),
            np.asarray(sync[:target_scans], dtype=float),
            self.actual_scan_rate_hz,
        )


class SimulatorStream:
    def __init__(
        self,
        filter_setup: str,
        sim_case: str,
        sample_rate_hz: float,
        change_after_s: float,
        gain: float,
    ) -> None:
        self.filter_setup = filter_setup
        self.sim_case = sim_case
        self.sample_rate_hz = sample_rate_hz
        self.actual_scan_rate_hz = sample_rate_hz
        self.change_after_s = change_after_s
        self.gain = gain
        self.started_at = time.monotonic()
        self.sample_index = 0
        self.rng = np.random.default_rng(1)
        self.random_good_mv = FILTER_SPECS_MV[self.filter_setup] * 1.45

    def __enter__(self) -> "SimulatorStream":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def case_parameters(self, case_name: str) -> tuple[float, float, float]:
        min_mv = FILTER_SPECS_MV[self.filter_setup]
        if case_name == "Low sensitivity":
            sensitivity_mv = min_mv * 0.62
        elif case_name == "Random good sensor":
            sensitivity_mv = self.random_good_mv
        else:
            sensitivity_mv = min_mv * 1.35

        if case_name == "Low offset":
            offset_v = 0.18
        elif case_name == "High offset":
            offset_v = 1.38
        else:
            offset_v = 0.75

        polarity = -1.0 if case_name == "Wrong polarity" else 1.0
        return sensitivity_mv, offset_v, polarity

    def read_window(self, duration_s: float) -> tuple[np.ndarray, np.ndarray, float]:
        elapsed_s = time.monotonic() - self.started_at
        case_name = self.sim_case
        if elapsed_s >= self.change_after_s and case_name == "Known good":
            case_name = "Low sensitivity"

        sample_count = max(1, int(round(duration_s * self.sample_rate_hz)))
        sample_numbers = self.sample_index + np.arange(sample_count, dtype=float)
        self.sample_index += sample_count
        t = sample_numbers / self.sample_rate_hz
        phase = (t * EXPECTED_FREQUENCY_HZ) % 1.0

        sensitivity_mv, offset_v, polarity = self.case_parameters(case_name)
        amplified_pp_v = (sensitivity_mv / 1000.0) * self.gain
        triangle = np.where(phase < 0.5, -1.0 + 4.0 * phase, 3.0 - 4.0 * phase)
        waveform = offset_v + polarity * triangle * (amplified_pp_v / 2.0)
        waveform += self.rng.normal(0.0, 0.003, size=sample_count)

        sync = np.where(phase < 0.5, 5.0, 0.0)
        sync += self.rng.normal(0.0, 0.005, size=sample_count)
        time.sleep(duration_s)
        return waveform, sync, self.sample_rate_hz


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously monitor AIN0 and report changes from an initial baseline.",
    )
    parser.add_argument("--simulator", action="store_true", help="Run without LabJack hardware.")
    parser.add_argument(
        "--filter-setup",
        choices=sorted(FILTER_SPECS_MV),
        default="-3 filter",
        help="Filter/setup to use for simulator signal size.",
    )
    parser.add_argument(
        "--sim-case",
        choices=SIM_CASES,
        default="Known good",
        help="Simulator case. The default steps to Low sensitivity after --sim-change-after seconds.",
    )
    parser.add_argument(
        "--sim-change-after",
        type=float,
        default=8.0,
        help="Seconds before the simulator changes signal when --sim-case is Known good.",
    )
    parser.add_argument("--sample-rate", type=float, default=DEFAULT_SAMPLE_RATE_HZ, help="Stream scan rate in Hz.")
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=DEFAULT_WINDOW_SECONDS,
        help="Seconds of samples to summarize for each live reading.",
    )
    parser.add_argument(
        "--baseline-seconds",
        type=float,
        default=DEFAULT_BASELINE_SECONDS,
        help="Seconds used to establish the initial no-change baseline.",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=float,
        default=DEFAULT_REFRESH_SECONDS,
        help="Pause between live readings. Set to 0 for back-to-back windows.",
    )
    parser.add_argument(
        "--gain",
        type=float,
        default=DEFAULT_AM502_GAIN,
        help="External gain before AIN0. Amplitude metrics are divided by this value.",
    )
    parser.add_argument(
        "--ain0-range",
        choices=list(LABJACK_AIN0_RANGE_OPTIONS),
        default=DEFAULT_WAVEFORM_INPUT_RANGE_LABEL,
        help="LabJack AIN0 input range.",
    )
    parser.add_argument(
        "--emitter-pwm",
        action="store_true",
        help="Enable the emitter MOSFET PWM output while monitoring.",
    )
    parser.add_argument(
        "--emitter-pwm-channel",
        choices=LABJACK_T7_PWM_CHANNELS,
        default=DEFAULT_EMITTER_PWM_CHANNEL,
        help="T7-Pro DIO line used for emitter PWM output.",
    )
    parser.add_argument(
        "--emitter-pwm-hz",
        type=float,
        default=DEFAULT_EMITTER_PWM_FREQUENCY_HZ,
        help="Emitter PWM frequency in Hz.",
    )
    parser.add_argument(
        "--emitter-pwm-duty",
        type=float,
        default=DEFAULT_EMITTER_PWM_DUTY_CYCLE,
        help="Emitter PWM duty cycle in percent.",
    )
    parser.add_argument(
        "--pp-threshold-mv",
        type=float,
        default=DEFAULT_PP_CHANGE_THRESHOLD_MV,
        help="Peak-to-peak change threshold after external gain correction.",
    )
    parser.add_argument(
        "--mean-threshold-mv",
        type=float,
        default=DEFAULT_MEAN_CHANGE_THRESHOLD_MV,
        help="Mean/offset change threshold at AIN0.",
    )
    parser.add_argument(
        "--rms-threshold-mv",
        type=float,
        default=DEFAULT_RMS_CHANGE_THRESHOLD_MV,
        help="AC RMS change threshold after external gain correction.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Optional CSV path for logging live readings.",
    )
    parser.add_argument(
        "--max-windows",
        type=int,
        default=0,
        help="Stop after this many live readings. The default is 0, which runs until Ctrl+C.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.sample_rate <= 0:
        raise ValueError("--sample-rate must be positive.")
    if args.window_seconds <= 0:
        raise ValueError("--window-seconds must be positive.")
    if args.baseline_seconds <= 0:
        raise ValueError("--baseline-seconds must be positive.")
    if args.refresh_seconds < 0:
        raise ValueError("--refresh-seconds cannot be negative.")
    if args.gain <= 0:
        raise ValueError("--gain must be positive.")
    if args.pp_threshold_mv < 0 or args.mean_threshold_mv < 0 or args.rms_threshold_mv < 0:
        raise ValueError("Change thresholds cannot be negative.")
    if args.max_windows < 0:
        raise ValueError("--max-windows cannot be negative.")
    normalize_pwm_channel(args.emitter_pwm_channel)
    calculate_pwm_roll_and_config(args.emitter_pwm_hz, args.emitter_pwm_duty)


def calculate_stats(
    waveform_v: np.ndarray,
    sync_v: np.ndarray,
    sample_rate_hz: float,
    gain: float,
) -> SignalStats:
    waveform_v = np.asarray(waveform_v, dtype=float)
    sync_v = np.asarray(sync_v, dtype=float)
    warnings: list[str] = []

    if waveform_v.size == 0:
        return SignalStats(
            mean_v=float("nan"),
            pp_mv=0.0,
            rms_ac_mv=0.0,
            min_v=float("nan"),
            max_v=float("nan"),
            frequency_hz=None,
            warnings=["No waveform samples were captured."],
        )

    edges, frequency_hz, sync_warnings = find_sync_edges(sync_v, sample_rate_hz)
    warnings.extend(sync_warnings)
    segments = cycle_segments_from_edges(edges)
    cycle_pp_v = cycle_peak_to_peak_values(waveform_v, segments)

    if cycle_pp_v:
        pp_v = float(np.median(cycle_pp_v))
    else:
        pp_v = float(np.max(waveform_v) - np.min(waveform_v))
        warnings.append("Using whole-window peak-to-peak because sync cycles were not reliable.")

    mean_v = float(np.mean(waveform_v))
    ac_v = waveform_v - mean_v
    rms_ac_v = float(np.sqrt(np.mean(ac_v * ac_v)))
    return SignalStats(
        mean_v=mean_v,
        pp_mv=(pp_v / gain) * 1000.0,
        rms_ac_mv=(rms_ac_v / gain) * 1000.0,
        min_v=float(np.min(waveform_v)),
        max_v=float(np.max(waveform_v)),
        frequency_hz=frequency_hz,
        warnings=warnings,
    )


def compare_to_baseline(
    stats: SignalStats,
    baseline: SignalStats,
    pp_threshold_mv: float,
    mean_threshold_mv: float,
    rms_threshold_mv: float,
) -> ChangeResult:
    delta_pp_mv = stats.pp_mv - baseline.pp_mv
    delta_mean_mv = (stats.mean_v - baseline.mean_v) * 1000.0
    delta_rms_mv = stats.rms_ac_mv - baseline.rms_ac_mv
    reasons: list[str] = []

    if abs(delta_pp_mv) >= pp_threshold_mv:
        reasons.append(f"pp {delta_pp_mv:+.2f} mV")
    if abs(delta_mean_mv) >= mean_threshold_mv:
        reasons.append(f"mean {delta_mean_mv:+.2f} mV")
    if abs(delta_rms_mv) >= rms_threshold_mv:
        reasons.append(f"rms {delta_rms_mv:+.2f} mV")

    return ChangeResult(
        changed=bool(reasons),
        delta_pp_mv=delta_pp_mv,
        delta_mean_mv=delta_mean_mv,
        delta_rms_mv=delta_rms_mv,
        reasons=reasons,
    )


def format_frequency(frequency_hz: float | None) -> str:
    if frequency_hz is None:
        return "no sync"
    return f"{frequency_hz:6.3f} Hz"


def print_stats(prefix: str, stats: SignalStats) -> None:
    print(
        f"{prefix} pp={stats.pp_mv:7.2f} mV  "
        f"mean={stats.mean_v:7.4f} V  "
        f"rms={stats.rms_ac_mv:7.2f} mV  "
        f"freq={format_frequency(stats.frequency_hz)}"
    )


def open_csv_writer(csv_path: Path | None) -> tuple[csv.DictWriter | None, object | None]:
    if csv_path is None:
        return None, None
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_file = csv_path.open("a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if csv_file.tell() == 0:
        writer.writeheader()
    return writer, csv_file


def write_csv_row(
    writer: csv.DictWriter | None,
    elapsed_s: float,
    stats: SignalStats,
    change: ChangeResult,
) -> None:
    if writer is None:
        return
    writer.writerow(
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": f"{elapsed_s:.3f}",
            "changed": "YES" if change.changed else "NO",
            "waveform_pp_mv": f"{stats.pp_mv:.6f}",
            "delta_pp_mv": f"{change.delta_pp_mv:.6f}",
            "mean_v": f"{stats.mean_v:.6f}",
            "delta_mean_mv": f"{change.delta_mean_mv:.6f}",
            "rms_ac_mv": f"{stats.rms_ac_mv:.6f}",
            "delta_rms_mv": f"{change.delta_rms_mv:.6f}",
            "frequency_hz": "" if stats.frequency_hz is None else f"{stats.frequency_hz:.6f}",
        }
    )


def make_stream(args: argparse.Namespace, waveform_range_v: float):
    if args.simulator:
        return SimulatorStream(
            filter_setup=args.filter_setup,
            sim_case=args.sim_case,
            sample_rate_hz=args.sample_rate,
            change_after_s=args.sim_change_after,
            gain=args.gain,
        )
    return LiveLabJackStream(
        sample_rate_hz=args.sample_rate,
        waveform_range_v=waveform_range_v,
        emitter_pwm_enabled=getattr(args, "emitter_pwm", False),
        emitter_pwm_channel=getattr(args, "emitter_pwm_channel", DEFAULT_EMITTER_PWM_CHANNEL),
        emitter_pwm_frequency_hz=getattr(args, "emitter_pwm_hz", DEFAULT_EMITTER_PWM_FREQUENCY_HZ),
        emitter_pwm_duty_cycle=getattr(args, "emitter_pwm_duty", DEFAULT_EMITTER_PWM_DUTY_CYCLE),
    )


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        waveform_range_v = labjack_ain0_range_from_label(args.ain0_range)
        range_warning = labjack_range_offset_warning(waveform_range_v, args.ain0_range)

        csv_writer, csv_file = open_csv_writer(args.csv)
        try:
            print("Eltec 406MCA live signal monitor")
            print(f"AIN0={WAVEFORM_CHANNEL}, sync={SYNC_CHANNEL}, range={args.ain0_range}, gain=x{args.gain:g}")
            if args.emitter_pwm:
                print(
                    f"Emitter PWM: {args.emitter_pwm_channel} at "
                    f"{args.emitter_pwm_hz:g} Hz, {args.emitter_pwm_duty:g}% duty"
                )
            print("Press Ctrl+C to stop.")
            if range_warning is not None:
                print(f"Range warning: {range_warning}")
            if args.simulator:
                print(f"Simulator mode: {args.sim_case}")

            with make_stream(args, waveform_range_v) as stream:
                print(f"Actual sample rate: {stream.actual_scan_rate_hz:.2f} Hz")
                print(f"Building {args.baseline_seconds:g} second baseline...")
                waveform, sync, actual_rate = stream.read_window(args.baseline_seconds)
                baseline = calculate_stats(waveform, sync, actual_rate, args.gain)
                print_stats("Baseline:", baseline)
                for warning in baseline.warnings:
                    print(f"Baseline warning: {warning}")

                started_at = time.monotonic()
                windows_seen = 0
                while True:
                    waveform, sync, actual_rate = stream.read_window(args.window_seconds)
                    stats = calculate_stats(waveform, sync, actual_rate, args.gain)
                    change = compare_to_baseline(
                        stats,
                        baseline,
                        pp_threshold_mv=args.pp_threshold_mv,
                        mean_threshold_mv=args.mean_threshold_mv,
                        rms_threshold_mv=args.rms_threshold_mv,
                    )
                    elapsed_s = time.monotonic() - started_at
                    state = "CHANGED" if change.changed else "steady "
                    reason_text = ", ".join(change.reasons) if change.reasons else "within thresholds"
                    print(
                        f"{elapsed_s:8.1f}s  {state}  "
                        f"pp={stats.pp_mv:7.2f} mV ({change.delta_pp_mv:+7.2f})  "
                        f"mean={stats.mean_v:7.4f} V ({change.delta_mean_mv:+7.2f} mV)  "
                        f"rms={stats.rms_ac_mv:7.2f} mV ({change.delta_rms_mv:+7.2f})  "
                        f"freq={format_frequency(stats.frequency_hz)}  {reason_text}"
                    )
                    write_csv_row(csv_writer, elapsed_s, stats, change)
                    if csv_file is not None:
                        csv_file.flush()
                    windows_seen += 1
                    if args.max_windows and windows_seen >= args.max_windows:
                        break
                    if args.refresh_seconds > 0:
                        time.sleep(args.refresh_seconds)
        finally:
            if csv_file is not None:
                csv_file.close()
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
