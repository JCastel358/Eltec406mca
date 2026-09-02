#!/usr/bin/env python3
"""Bench probe for the array rig's DAQ (ACCES USB-AIO16-64MA) - engineering only.

Never issues a verdict. It exists to prove the acquisition path on the real
hardware before the tester trusts it, and to record the fixture numbers
that go into ``docs/CALIBRATION_RECORD.md``:

    python array_rig/m40623/daq_bench_probe.py info
    python array_rig/m40623/daq_bench_probe.py selfcal
    python array_rig/m40623/daq_bench_probe.py config            # apply + read back the production block
    python array_rig/m40623/daq_bench_probe.py scan --reads 10   # own-scale volts per channel (the -HG question needs a metered voltage on CH0)
    python array_rig/m40623/daq_bench_probe.py slots --seconds 2 # per-conversion-slot means: is slot 0 the settling one?
    python array_rig/m40623/daq_bench_probe.py floor --seconds 20   # onboard reference through the same path: the instrument's own noise
    python array_rig/m40623/daq_bench_probe.py stream --seconds 60 [--save tray.npz]   # rate / pool / leftover check
    python array_rig/m40623/daq_bench_probe.py crosstalk --source-channel 0 --seconds 10
    python array_rig/m40623/daq_bench_probe.py capture --seconds 20 --save x.npz   # raw wideband capture to a file

``--simulate`` runs every command against ``SimulatedDaq`` (no hardware).
``--oneshot`` makes ``stream``/``capture`` use the ``ADC_BulkAcquire`` path
instead of the callback stream (the fallback if callbacks misbehave).

The judged-band numbers (``floor``, ``crosstalk``) need ``array_analysis``
from this directory; until it exists the probe reports raw rms only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
from pathlib import Path

import numpy as np

MODEL_DIR = Path(__file__).resolve().parent
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

import daq_backend as daq  # noqa: E402

# The production acquisition constants. They are mirrored (and explained) in
# eltec_40623_array_tester.py; the probe takes them as defaults so a sweep
# with other values is a command-line matter, not an edit.
DEFAULT_RANGE_CODE = 2
DEFAULT_OVERSAMPLE = 3
DEFAULT_DROP_FIRST = 1
DEFAULT_SCAN_HZ = 1000.0
DEFAULT_BUFFER_BYTES = 64_000
DEFAULT_BUFFER_COUNT = 32
DEFAULT_DECIMATION_FACTOR = 20
DEFAULT_WINDOW_S = 1.0


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def open_device(args: argparse.Namespace):
    if args.simulate:
        device = daq.SimulatedDaq(real_time=True)
    else:
        device = daq.AiousbDaq()
    info = device.connect(timeout_s=args.connect_timeout)
    print(f"device: {info.summary()}")
    if info.calibration_supported is False:
        print("  note: ADC_QueryCal says this unit does not support :AUTO: self-calibration")
    return device


def config_from_args(args: argparse.Namespace, *, cal_code: int = daq.CAL_NORMAL) -> daq.AdcConfig:
    return daq.AdcConfig(
        range_code=args.range,
        start_channel=args.start,
        end_channel=args.end,
        oversample=args.oversample,
        trigger=daq.TRIGGER_TIMER | daq.TRIGGER_SCAN,
        cal_code=cal_code,
    )


def describe_config(config: daq.AdcConfig, scan_hz: float) -> str:
    spec = daq.range_spec(config.range_code)
    return (
        f"range code {config.range_code} ({spec.name}, LSB {spec.lsb_v * 1e6:.1f} uV), channels "
        f"{config.start_channel}-{config.end_channel} ({config.channels}), oversample {config.oversample} "
        f"({config.conversions_per_channel} conversions/channel), trigger {config.trigger:#04x}, cal {config.cal_code:#04x}; "
        f"at {scan_hz:.0f} scans/s: {config.conversions_per_second * scan_hz / 1000:.0f} kS/s aggregate, "
        f"{config.scan_bytes * scan_hz / 1000:.0f} KB/s over USB"
    )


def channel_label(config: daq.AdcConfig, index: int) -> str:
    channel = config.start_channel + index
    if channel < daq.CHANNEL_COUNT:
        return f"CH{channel:<3d} {daq.position_for_channel(channel):>5s}"
    return f"CH{channel:<3d}   n/a"


def capture(
    device,
    config: daq.AdcConfig,
    *,
    seconds: float,
    scan_hz: float,
    drop_first: int,
    average: bool,
    buffer_bytes: int,
    buffer_count: int,
    oneshot: bool,
    progress: bool = True,
) -> tuple[np.ndarray, daq.StreamDiagnostics | None, float]:
    """Acquire ``seconds`` of scans. Returns ``(counts [scans, channels(, conv)], diagnostics, timer_hz)``."""

    wanted = int(round(seconds * scan_hz))
    if oneshot:
        if not hasattr(device, "bulk_acquire"):
            raise SystemExit("--oneshot needs the real device (ADC_BulkAcquire)")
        counts, timer_hz = device.bulk_acquire(
            scans=wanted, scan_hz=scan_hz, timeout_s=seconds + 10.0, drop_first=drop_first, average=average
        )
        return counts, None, timer_hz
    header = device.start_stream(
        scan_hz=scan_hz, buffer_bytes=buffer_bytes, buffer_count=buffer_count, drop_first=drop_first, average=average
    )
    chunks: list[np.ndarray] = []
    received = 0
    started = time.monotonic()
    last_report = started
    try:
        while received < wanted:
            chunk = device.read_stream(timeout_s=1.0)
            if chunk is None:
                if time.monotonic() - started > seconds + 5.0:
                    raise daq.StreamTimeoutError(f"only {received} of {wanted} scans after {seconds + 5:.0f} s")
                continue
            chunks.append(chunk)
            received += chunk.shape[0]
            if progress and time.monotonic() - last_report >= 5.0:
                last_report = time.monotonic()
                print(f"  ... {received}/{wanted} scans", flush=True)
    finally:
        diagnostics = device.stop_stream()
        if hasattr(device, "drain_stream"):
            for chunk in device.drain_stream():
                chunks.append(chunk)
                received += chunk.shape[0]
    data = np.concatenate(chunks)[:wanted] if chunks else np.empty((0, config.channels))
    return data, diagnostics, header.actual_timer_hz


def band_limited_pp_mv(volts: np.ndarray, scan_hz: float) -> np.ndarray | None:
    """Per-channel per-window pk-pk (mV) in the judged band, or None until array_analysis exists."""

    try:
        import array_analysis  # noqa: WPS433 (sibling module, may not exist yet)
    except ImportError:
        return None
    return array_analysis.band_limited_window_pp_mv(
        volts, scan_hz, decimation_factor=DEFAULT_DECIMATION_FACTOR, window_s=DEFAULT_WINDOW_S
    )


def print_table(rows: list[list[str]], header: list[str]) -> None:
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    line = "  ".join(str(h).ljust(w) for h, w in zip(header, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


def save_capture(path: Path, volts: np.ndarray, *, scan_hz: float, config: daq.AdcConfig, device, timer_hz: float, extra: dict) -> None:
    info = device.info
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        waveform_v=volts.astype(np.float32),
        sample_rate_hz=float(scan_hz),
        channels=np.arange(config.start_channel, config.end_channel + 1),
        positions=np.array([daq.position_for_channel(c) if c < daq.CHANNEL_COUNT else "" for c in range(config.start_channel, config.end_channel + 1)]),
        recorded_at=np.array(_dt.datetime.now().isoformat(timespec="seconds")),
        daq_serial=np.array(info.serial_number if info else "unknown"),
        daq_model=np.array(info.name if info else "unknown"),
        range_code=np.array(str(config.range_code)),
        range_span_v=np.array(str(daq.range_spec(config.range_code).span_v)),
        oversample=np.array(str(config.oversample)),
        actual_timer_hz=np.array(f"{timer_hz:.6f}"),
        simulated=np.array(str(bool(info.simulated) if info else False)),
        source=np.array("daq_bench_probe"),
        **{key: np.array(str(value)) for key, value in extra.items()},
    )
    print(f"saved {path} ({volts.shape[0]} channels x {volts.shape[1]} samples)")


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def cmd_info(args: argparse.Namespace) -> int:
    device = open_device(args)
    if hasattr(device, "read_config_block"):
        block = device.read_config_block()
        print(f"config block ({len(block)} bytes): {block.hex(' ')}")
        try:
            print(f"decoded: {daq.parse_config_block(block)}")
        except ValueError as exc:
            print(f"decoded: (not one uniform range) {exc}")
    print(f"production config would be: {describe_config(config_from_args(args), args.hz)}")
    device.close()
    return 0


def cmd_selfcal(args: argparse.Namespace) -> int:
    device = open_device(args)
    print("running ADC_SetCal(':AUTO:') ...", flush=True)
    started = time.monotonic()
    device.self_calibrate()
    print(f"self-calibration finished in {time.monotonic() - started:.1f} s")
    if hasattr(device, "read_config_block"):
        print(f"config block after cal: {device.read_config_block().hex(' ')}")
    device.close()
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    device = open_device(args)
    config = config_from_args(args)
    print(f"writing: {daq.build_config_block(config).hex(' ')}")
    applied = device.configure(config)
    print(f"read back OK: {describe_config(applied, args.hz)}")
    device.close()
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    device = open_device(args)
    config = device.configure(config_from_args(args))
    counts = device.read_scan_counts(reads=args.reads)
    median_counts = np.median(counts, axis=0)
    spread = counts.max(axis=0) - counts.min(axis=0)
    own = daq.counts_to_volts(median_counts, config.range_code)
    driver = device.read_scan_driver_volts() if hasattr(device, "read_scan_driver_volts") else np.full_like(own, np.nan)
    rows = []
    for i in range(config.channels):
        rows.append([
            channel_label(config, i), f"{median_counts[i]:8.1f}", f"{spread[i]:5.0f}",
            f"{own[i]:9.5f}", f"{driver[i]:9.5f}", f"{(driver[i] - own[i]) * 1e3:+8.2f}",
        ])
    print_table(rows, ["channel  pos", "counts", "p-p", "own V", "driver V*", "diff mV"])
    print(
        "\n-HG check: put a known, METERED voltage (about 1.5 V) on CH0 and ground CH1. 'own V' must match the meter within "
        "a few mV; ~10x or ~100x off means a -HG (high gain) unit and the range table must be changed."
        "\n* 'driver V' is the DLL's ADC_GetScanV taken in a separate read AFTER the counts above. It uses the same "
        "counts x span / 65536 formula as 'own V' (vendor DLL source), so it is a sanity check of our arithmetic, never a "
        "gain check: a difference in that column is the input moving between reads, not a scaling error."
    )
    device.close()
    return 0


def cmd_slots(args: argparse.Namespace) -> int:
    device = open_device(args)
    config = device.configure(config_from_args(args))
    if config.conversions_per_channel < 2:
        raise SystemExit("slots needs --oversample >= 1 (at least two conversions per channel)")
    print(f"streaming {args.seconds:.0f} s with ALL conversions kept: {describe_config(config, args.hz)}")
    cube, diagnostics, _ = capture(
        device, config, seconds=args.seconds, scan_hz=args.hz, drop_first=0, average=False,
        buffer_bytes=args.buffer_bytes, buffer_count=args.buffer_count, oneshot=args.oneshot,
    )
    if diagnostics is not None:
        print(f"stream: {diagnostics.summary()}")
    means = cube.mean(axis=0)  # [channels, conversions]
    rest = means[:, 1:].mean(axis=1)
    delta0 = means[:, 0] - rest
    lsb = daq.lsb_volts(config.range_code)
    rows = []
    for i in range(config.channels):
        rows.append([channel_label(config, i)] + [f"{means[i, k]:9.2f}" for k in range(config.conversions_per_channel)]
                    + [f"{delta0[i]:+8.2f}", f"{delta0[i] * lsb * 1e6:+8.1f}"])
    print_table(rows, ["channel  pos"] + [f"slot{k}" for k in range(config.conversions_per_channel)] + ["slot0-rest", "uV"])
    worst = int(np.argmax(np.abs(delta0)))
    print(
        f"\nlargest slot-0 deviation: {channel_label(config, worst)} {delta0[worst]:+.2f} counts "
        f"({delta0[worst] * lsb * 1e6:+.1f} uV). A systematic slot-0 offset that follows a big step between neighbouring "
        f"channels confirms that the first conversion after a multiplexer hop is unsettled (keep drop_first = 1)."
    )
    device.close()
    return 0


def _stream_and_report(args: argparse.Namespace, device, config: daq.AdcConfig, *, seconds: float):
    counts, diagnostics, timer_hz = capture(
        device, config, seconds=seconds, scan_hz=args.hz, drop_first=args.drop, average=True,
        buffer_bytes=args.buffer_bytes, buffer_count=args.buffer_count, oneshot=args.oneshot,
    )
    volts = daq.counts_to_volts(counts, config.range_code).T  # [channels, samples]
    if diagnostics is not None:
        print(f"stream: {diagnostics.summary()}")
        problems = diagnostics.problems()
        print("integrity: " + ("OK" if not problems else "; ".join(problems)))
        if diagnostics.consumer_cpu_s is not None and diagnostics.elapsed_s:
            print(f"consumer CPU: {100.0 * diagnostics.consumer_cpu_s / diagnostics.elapsed_s:.1f} % of the stream time")
    else:
        print(f"one-shot bulk acquisition: {counts.shape[0]} scans, timer {timer_hz:.3f} Hz")
    return volts, diagnostics, timer_hz


def cmd_stream(args: argparse.Namespace) -> int:
    device = open_device(args)
    config = device.configure(config_from_args(args))
    print(f"streaming {args.seconds:.0f} s: {describe_config(config, args.hz)}")
    volts, diagnostics, timer_hz = _stream_and_report(args, device, config, seconds=args.seconds)
    means = volts.mean(axis=1)
    rms = volts.std(axis=1)
    print(f"per-channel mean: min {means.min():.4f} V, max {means.max():.4f} V; raw rms: min {rms.min() * 1e6:.1f} uV, "
          f"median {np.median(rms) * 1e6:.1f} uV, max {rms.max() * 1e6:.1f} uV")
    if args.save:
        save_capture(Path(args.save), volts, scan_hz=args.hz, config=config, device=device, timer_hz=timer_hz,
                     extra={"probe_command": "stream", "drop_first": args.drop})
    device.close()
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    if not args.save:
        raise SystemExit("capture needs --save <file.npz>")
    return cmd_stream(args)


def _report_band_limited(volts: np.ndarray, scan_hz: float, config: daq.AdcConfig, *, title: str) -> None:
    rms = volts.std(axis=1)
    pp = band_limited_pp_mv(volts, scan_hz)
    rows = []
    for i in range(config.channels):
        row = [channel_label(config, i), f"{volts[i].mean():9.5f}", f"{rms[i] * 1e6:8.1f}"]
        if pp is not None:
            row += [f"{pp[i].max() * 1e3:8.1f}", f"{np.median(pp[i]) * 1e3:8.1f}"]
        rows.append(row)
    header = ["channel  pos", "mean V", "raw rms uV"] + (["worst pp uV", "median pp uV"] if pp is not None else [])
    print(title)
    print_table(rows, header)
    if pp is None:
        print("(array_analysis not available yet - judged-band pk-pk not computed)")
    else:
        worst = pp.max(axis=1)
        w = int(np.argmax(worst))
        print(f"\njudged band ({DEFAULT_WINDOW_S:.0f} s windows, decimation {DEFAULT_DECIMATION_FACTOR}): worst channel "
              f"{channel_label(config, w)} {worst[w] * 1e3:.1f} uV pk-pk; median over channels of the per-channel median "
              f"{np.median(np.median(pp, axis=1)) * 1e3:.1f} uV")


def cmd_floor(args: argparse.Namespace) -> int:
    device = open_device(args)
    # Bench finding 2026-09-02: on a UNIPOLAR range the onboard ground
    # reference reads exactly code 0 on every channel (it sits at or below the
    # bottom of the range, so the ADC clips and shows no noise at all). The
    # onboard full-scale reference (0.909 V at 0-10 V, so ~0.909 V mid-range
    # on the production 0-5 V range) goes through the same multiplexer and
    # amplifier path and is the usable instrument-floor source; --floor-source
    # ground is kept for bipolar ranges (where 0 V sits mid-scale).
    cal_code = daq.CAL_GROUND if args.floor_source == "ground" else daq.CAL_REF_UNIPOLAR
    config = device.configure(config_from_args(args, cal_code=cal_code))
    print(f"cal mode {'GROUND' if cal_code == daq.CAL_GROUND else 'FULL-SCALE REFERENCE'} (the ADC reads its own "
          f"onboard reference through the same multiplexer/amplifier path): {describe_config(config, args.hz)}")
    try:
        volts, _, _ = _stream_and_report(args, device, config, seconds=args.seconds)
    finally:
        device.configure(config_from_args(args))
    _report_band_limited(volts, args.hz, config, title=f"\ninstrument floor over {args.seconds:.0f} s:")
    if args.save:
        save_capture(Path(args.save), volts, scan_hz=args.hz, config=config, device=device, timer_hz=args.hz,
                     extra={"probe_command": "floor", "drop_first": args.drop})
    device.close()
    return 0


def cmd_crosstalk(args: argparse.Namespace) -> int:
    device = open_device(args)
    config = device.configure(config_from_args(args))
    source = args.source_channel - config.start_channel
    if not 0 <= source < config.channels:
        raise SystemExit("--source-channel must be inside the scanned range")
    print(f"crosstalk: drive channel CH{args.source_channel} with a low-frequency signal (e.g. 1 V pk-pk at 10 Hz), "
          f"ground or load every other input; {describe_config(config, args.hz)}")
    volts, _, _ = _stream_and_report(args, device, config, seconds=args.seconds)
    detrended = volts - volts.mean(axis=1, keepdims=True)
    pp = band_limited_pp_mv(volts, args.hz)
    if pp is not None:
        level = np.median(pp, axis=1)  # mV pk-pk per channel in the judged band
        unit = "median judged-band pp"
    else:
        level = detrended.std(axis=1) * 1e3
        unit = "raw rms"
    reference = level[source]
    rows = []
    for i in range(config.channels):
        ratio_db = 20.0 * np.log10(level[i] / reference) if reference > 0 and level[i] > 0 else float("nan")
        rows.append([channel_label(config, i), f"{level[i]:9.4f}", f"{ratio_db:7.1f}" + ("  <- source" if i == source else "")])
    print_table(rows, ["channel  pos", f"{unit} mV", "dB re source"])
    others = np.delete(level, source)
    if reference > 0 and others.max() > 0:
        print(f"\nworst neighbour: {20.0 * np.log10(others.max() / reference):.1f} dB below the source "
              f"(datasheet: -60 dB at 500 kS/s)")
    device.close()
    return 0


def cmd_simulate_check(args: argparse.Namespace) -> int:
    """Quick self-test of the probe plumbing against the simulator."""

    args.simulate = True
    args.seconds = 2.0
    return cmd_stream(args)


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["info", "selfcal", "config", "scan", "slots", "floor", "stream", "capture", "crosstalk", "simcheck"])
    parser.add_argument("--simulate", action="store_true", help="use SimulatedDaq instead of the hardware")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--range", type=int, default=DEFAULT_RANGE_CODE, help="range code 0-7 (2 = 0-5 V)")
    parser.add_argument("--oversample", type=int, default=DEFAULT_OVERSAMPLE, help="extra conversions per channel")
    parser.add_argument("--drop", type=int, default=DEFAULT_DROP_FIRST, help="conversions dropped after each mux hop")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=daq.CHANNEL_COUNT - 1)
    parser.add_argument("--hz", type=float, default=DEFAULT_SCAN_HZ, help="scan rate per channel")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--reads", type=int, default=10)
    parser.add_argument("--buffer-bytes", type=int, default=DEFAULT_BUFFER_BYTES)
    parser.add_argument("--buffer-count", type=int, default=DEFAULT_BUFFER_COUNT)
    parser.add_argument("--oneshot", action="store_true", help="use ADC_BulkAcquire instead of the callback stream")
    parser.add_argument("--source-channel", type=int, default=0)
    parser.add_argument("--save", type=str, default="", help="write the capture to this .npz")
    parser.add_argument("--floor-source", choices=["reference", "ground"], default="reference",
                        help="floor: onboard full-scale reference (default; ground clips at code 0 on unipolar ranges) or ground")
    return parser


COMMANDS = {
    "info": cmd_info, "selfcal": cmd_selfcal, "config": cmd_config, "scan": cmd_scan, "slots": cmd_slots,
    "floor": cmd_floor, "stream": cmd_stream, "capture": cmd_capture, "crosstalk": cmd_crosstalk,
    "simcheck": cmd_simulate_check,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except daq.DaqError as exc:
        print(f"DAQ error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
