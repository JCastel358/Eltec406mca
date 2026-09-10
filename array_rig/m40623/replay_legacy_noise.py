"""Re-analyse a saved array-tester NPZ without opening the DAQ or changing it."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

import legacy_noise as ln
from daq_backend import range_spec


def _scalar(capture: Any, key: str, *, default: Any = None) -> Any:
    if key not in capture.files:
        if default is not None:
            return default
        raise ValueError(f"Capture metadata is missing {key!r}; acquisition settings cannot be inferred safely")
    value = np.asarray(capture[key])
    if value.size != 1:
        raise ValueError(f"Capture metadata {key!r} must contain a single value")
    return value.reshape(-1)[0].item()


def _integer(capture: Any, key: str) -> int:
    value = str(_scalar(capture, key))
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Capture metadata {key!r} must be an integer") from exc


def _labels(capture: Any, key: str, count: int, *, default: Any = None) -> list[Any]:
    if key not in capture.files and default is not None:
        return [default] * count
    if key not in capture.files:
        raise ValueError(f"Capture is missing {key!r}")
    values = np.asarray(capture[key])
    if values.ndim != 1 or values.size != count:
        raise ValueError(f"Capture {key!r} must have one item per waveform channel")
    return values.tolist()


def replay_capture(path: str | Path, *, calibration: ln.LegacyNoiseCalibration | None = None) -> dict[str, Any]:
    """Read the saved acquisition identity and return JSON-ready noise evidence.

    NPZ loading explicitly disables pickle. Legacy files without acquisition
    settings are rejected rather than silently assuming the current setup.
    A missing actual timer frequency permits exploratory replay at the saved
    nominal rate, but cannot qualify a capture for calibrated decisions.
    """
    source = Path(path).resolve(strict=True)
    warnings: list[str] = []
    with np.load(source, allow_pickle=False) as capture:
        if "waveform_v" not in capture.files:
            raise ValueError("Capture is missing 'waveform_v'")
        raw = np.asarray(capture["waveform_v"], dtype=np.float64)
        if raw.ndim != 2:
            raise ValueError("Saved waveform_v must be [channels, samples]")
        count = raw.shape[0]
        positions = [str(value) for value in _labels(capture, "positions", count)]
        channels_raw = _labels(capture, "channels", count)
        channels = [int(value) for value in channels_raw]
        if any(float(original) != parsed for original, parsed in zip(channels_raw, channels)) or len(set(channels)) != count:
            raise ValueError("Saved channels must be distinct integer channel numbers")
        occupancy = [str(value) for value in _labels(capture, "occupancy", count, default="UNKNOWN")]
        sensor_numbers = _labels(capture, "sensor_numbers", count, default=0)
        nominal_rate = float(_scalar(capture, "sample_rate_hz"))
        actual_known = "actual_timer_hz" in capture.files
        rate = float(_scalar(capture, "actual_timer_hz", default=nominal_rate))
        if not actual_known:
            warnings.append("Actual timer frequency was not saved; nominal sample_rate_hz was used for exploratory replay.")
            if calibration is not None:
                raise ValueError("Calibrated replay requires saved actual_timer_hz, which this capture does not contain")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("Saved actual sample rate must be positive and finite")
        range_code = _integer(capture, "range_code")
        span = range_spec(range_code)
        oversample = _integer(capture, "oversample")
        # Tester archives use drop_conversions; the working waveform/readout
        # tools save the same setting under drop_first.
        drop = _integer(capture, "drop_conversions" if "drop_conversions" in capture.files else "drop_first")
        serial = str(_scalar(capture, "daq_serial"))
        simulated = str(_scalar(capture, "simulated", default="False")).strip().lower() in ("true", "1")
        if simulated and calibration is not None:
            raise ValueError("A simulated capture cannot receive calibrated detector acceptance")
        if simulated:
            warnings.append("This capture is simulated; no physical detector qualification is implied.")
        config = ln.LegacyNoiseConfig(
            input_gain=1.0, adc_min_v=span.low_v, adc_max_v=span.high_v,
            adc_bits=16, range_code=range_code, oversample_extra=oversample,
            drop_first=drop, setup_id=f"{serial}:buffer-1x:drop-{drop}",
        )
        results, _, _ = ln.analyze_legacy_noise(raw, rate, positions=positions, channels=channels,
                                                config=config, calibration=calibration)
    rows = []
    for index, result in enumerate(results):
        row = result.as_dict()
        row["occupancy"] = occupancy[index]
        row["sensor_number"] = sensor_numbers[index]
        row["detector_decision_applicable"] = occupancy[index] == "LOADED" and not simulated
        # Empty/unknown sockets may have measurable voltage noise, but must
        # never receive a detector pass/fail in a replay report.
        row["noise_metric_verdict"] = row["verdict"]
        if occupancy[index] != "LOADED":
            row["verdict"] = "EMPTY" if occupancy[index] == "EMPTY" else "OCCUPANCY_UNKNOWN"
        rows.append(row)
    return {
        "schema_version": 1, "method_id": ln.METHOD_ID, "source_capture": str(source),
        "scope": "Noise-only replay; saved occupancy retained; offset and overall detector verdicts are not recomputed.",
        "sample_rate_hz": rate, "nominal_sample_rate_hz": nominal_rate,
        "actual_timer_recorded": actual_known, "simulated": simulated,
        "calibration_id": "PENDING" if calibration is None else calibration.calibration_id,
        "acquisition": config.acquisition_signature(rate), "warnings": warnings, "results": rows,
    }


def write_replay_report(report: dict[str, Any], output: str | Path) -> Path:
    """Write an explicit new JSON or CSV path. Existing files are never replaced."""
    target = Path(output).resolve()
    if target == Path(report["source_capture"]).resolve():
        raise ValueError("Replay output must not replace the source capture")
    if target.suffix.lower() not in (".json", ".csv"):
        raise ValueError("Replay --output must end in .json or .csv")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() == ".json":
        payload = json.dumps(report, indent=2, allow_nan=False) + "\n"
        with target.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        rows = []
        for row in report["results"]:
            values = {"source_capture": report["source_capture"], "scope": report["scope"],
                      "simulated": report["simulated"], "actual_timer_recorded": report["actual_timer_recorded"],
                      "replay_warnings": json.dumps(report["warnings"]), **row}
            rows.append({key: json.dumps(value, sort_keys=True, allow_nan=False)
                         if isinstance(value, (dict, list, tuple)) else value for key, value in values.items()})
        with target.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="Saved array-tester .npz capture")
    parser.add_argument("--output", required=True, type=Path, help="New report path ending in .json or .csv (never overwritten)")
    parser.add_argument("--calibration", type=Path, help="Qualified paired noise calibration JSON; otherwise acceptance stays pending")
    args = parser.parse_args(argv)
    try:
        if args.calibration is not None and args.output.resolve() == args.calibration.resolve():
            raise ValueError("Replay output must not replace the calibration record")
        calibration = None if args.calibration is None else ln.load_noise_calibration(args.calibration)
        report = replay_capture(args.capture, calibration=calibration)
        path = write_replay_report(report, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Replay failed: {exc}\n")
    print(f"Saved {len(report['results'])} channel noise measurements to {path}")
    print(f"Calibration: {report['calibration_id']}; this report does not recompute overall detector acceptance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
