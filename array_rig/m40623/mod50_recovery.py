"""Read-only validation of saved DC evidence before retrying a failed noise stage.

This validates files and acquisition provenance, not physical continuity. The
operator must separately confirm that the same detectors, wiring, power and
fixture remain unchanged, and the worker must verify the connected DAQ serial.
The original folder is never modified; a retry writes a new run folder.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import math
from pathlib import Path
import re
import zipfile
import zlib

import numpy as np

import mod50_hardware as hw


_RAW_NAME = re.compile(r"dc(?:_attempt[0-9]{2,})?_raw\.npz\Z")
_RATE_ERROR = re.compile(
    r"scan rate [0-9.]+/s differs from the nominal [0-9.]+/s by [+-][0-9.]+% \(limit [0-9.]+%\)\Z")
_INCOMPLETE = re.compile(r"Incomplete: [0-9]+ of [0-9]+ scans\Z")
_POOL_ERROR = re.compile(
    r"driver buffer pool exhausted [0-9]+ time\(s\) \(extra buffer inserted: the host fell behind the stream\)\Z")
_EARLY_END = "the device ended the stream before it was stopped - the record is incomplete"
_TIMEOUT = "No fresh DAQ samples for two seconds"
_CLEANUP_FIELDS = ("clock_stop_status", "bulk_end_status", "bulk_worker_status")


def _require(condition, message):
    if not condition:
        raise ValueError("Cannot retry noise from this saved run: " + message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _same_evidence(saved, computed):
    """Allow rounding noise in numeric evidence, never bool/int substitution."""
    if _number(saved) and _number(computed):
        return math.isclose(saved, computed, rel_tol=0., abs_tol=1e-12)
    if isinstance(computed, dict):
        return (isinstance(saved, dict) and saved.keys() == computed.keys()
                and all(_same_evidence(saved[key], value) for key, value in computed.items()))
    if isinstance(computed, list):
        return (isinstance(saved, list) and len(saved) == len(computed)
                and all(_same_evidence(a, b) for a, b in zip(saved, computed)))
    return type(saved) is type(computed) and saved == computed


def _json(raw):
    def invalid_constant(value):
        raise ValueError("Non-finite number in saved JSON: " + value)
    value = json.loads(raw, parse_constant=invalid_constant)
    _require(isinstance(value, dict), "saved metadata must be an object.")
    return value


def _status(value, expected):
    """Check both the original byte and decoded fields; booleans must be real."""
    if not isinstance(value, dict) or type(value.get("raw_byte")) is not int:
        return False
    expected_status = hw.decode_status(expected)
    if value.get("raw_byte") != expected:
        return False
    for card in ("UPPER", "LOWER"):
        observed = value.get(card)
        if not isinstance(observed, dict):
            return False
        if any(observed.get(key) is not wanted for key, wanted in expected_status[card].items()):
            return False
    return True


def _clean_shutdown(diagnostics, *, simulate=False):
    return (isinstance(diagnostics, dict)
            and all((type(diagnostics.get(key)) is int and diagnostics[key] == 0)
                    or (simulate and diagnostics.get(key) is None) for key in _CLEANUP_FIELDS)
            and diagnostics.get("cleanup_error") is None)


def _delivery_error(error):
    if not isinstance(error, str):
        return False
    if error == _TIMEOUT or error == "no complete scans were received":
        return True
    if _RATE_ERROR.fullmatch(error) or _INCOMPLETE.fullmatch(error):
        return True
    if error == _EARLY_END or _POOL_ERROR.fullmatch(error):
        return True
    if error.startswith("DAQ delivery interrupted: "):
        parts = error[len("DAQ delivery interrupted: "):].split("; ")
        return bool(parts) and all(part == _EARLY_END or _POOL_ERROR.fullmatch(part) for part in parts)
    return False


def _delivery_failure(record, *, simulate=False):
    errors = record.get("errors")
    if not isinstance(errors, list) or not errors or not all(_delivery_error(error) for error in errors):
        return False
    # Rate/count warnings alone cannot establish a transient delivery fault.
    primary = any(error == _TIMEOUT or error.startswith("DAQ delivery interrupted: ")
                  or error == _EARLY_END or _POOL_ERROR.fullmatch(error) for error in errors)
    if not primary or not _clean_shutdown(record.get("diagnostics"), simulate=simulate):
        return False
    if "retry_eligible" in record and record["retry_eligible"] is not True:
        return False
    if "failure_kind" in record and record["failure_kind"] not in ("delivery_timeout", "delivery_interrupted"):
        return False
    diagnostics = record["diagnostics"]
    if diagnostics.get("callback_error") is not None:
        return False
    # A granted clock mismatch is not a USB interruption, even if another
    # symptom also looks transient. Do not weaken the existing 1% contract.
    return (_number(diagnostics.get("actual_timer_hz"))
            and abs(diagnostics["actual_timer_hz"] / hw.SAMPLE_HZ - 1) <= .01
            and diagnostics.get("nominal_scan_hz") == hw.SAMPLE_HZ)


def _local_file(directory, name):
    path = directory / name
    resolved = path.resolve(strict=True)
    _require(resolved.parent == directory and resolved.is_file(), "an evidence file leaves the run folder.")
    return resolved


def load_noise_recovery(directory, *, simulate: bool):
    """Return ``{record, raw_path, source}`` or raise ValueError.

    Only the last DC and noise records are authoritative. Earlier failed
    attempts remain evidence and must also be recognizable delivery failures.
    Both the pre-retry November 4 format and the current format are accepted.
    No driver is opened and no file is written by this function.
    """
    # Local import keeps this helper usable by the acquisition module itself.
    from mod50_check import DC_SECONDS, NOISE_SECONDS, SCHEMA, SUPPORTED_TIMING_POLICIES, channel_statistics
    from array_analysis import OFFSET_MIN_V, OFFSET_MAX_V

    try:
        _require(type(simulate) is bool, "select Actual detectors or Generated demo explicitly.")
        folder = Path(directory).resolve(strict=True)
        _require(folder.is_dir(), "the run folder is missing.")
        summary_path = _local_file(folder, "summary.json")
        _require(summary_path.stat().st_size <= 64 * 1024 * 1024, "the summary is too large.")
        summary_bytes = summary_path.read_bytes()
        summary = _json(summary_bytes)
        revisions = {"2026-11-04.2", "2026-11-04.3", "2026-11-11.1", "2026-11-12.1", hw.APP_REVISION}
        expected = {"schema": SCHEMA, "profile": hw.PROFILE,
                    "channel_map": hw.CHANNEL_MAP}
        _require(summary.get("timing_policy") in SUPPORTED_TIMING_POLICIES, "the timing policy is unsupported.")
        for key, value in expected.items():
            _require(summary.get(key) == value, key + " does not match the current tester.")
        _require(summary.get("application_revision") in revisions, "the application revision is unsupported.")
        _require(summary.get("simulated") is simulate, "the saved data source differs from the selected source.")
        _require(summary.get("data_complete") is False, "the saved run is already complete or unrecognized.")
        _require(summary.get("restart_required") is False, "the earlier driver shutdown was not confirmed.")
        _require(_status(summary.get("shutdown_status"), 0), "return to DC/unity with both cards ready was not confirmed.")
        for key in ("lot", "operator"):
            _require(isinstance(summary.get(key), str) and bool(summary[key].strip()) and len(summary[key]) <= 120,
                     "the saved " + key + " is invalid.")
        _require(type(summary.get("tray")) is int and summary["tray"] >= 1, "the tray identity is invalid.")
        created = dt.datetime.fromisoformat(summary["created_at"])
        _require(created.tzinfo is not None, "the saved run time has no timezone.")
        population = summary.get("installed_positions")
        _require(isinstance(population, list) and population
                 and list(hw.normalize_positions(population)) == population, "the detector population is invalid.")
        daq = summary.get("daq")
        _require(isinstance(daq, dict) and daq.get("simulated") is simulate, "DAQ source identity is invalid.")
        serial = daq.get("serial_number")
        _require(isinstance(serial, str) and bool(serial.strip()), "the original DAQ serial is missing.")
        if not simulate:
            _require(daq.get("product_id") == 0x8145, "the original DAQ model is not the expected device.")

        records = summary.get("records")
        _require(isinstance(records, list) and records and all(isinstance(r, dict) for r in records),
                 "stage records are missing.")
        _require(all(r.get("mode") in ("dc", "noise") for r in records), "the stage sequence is unrecognized.")
        dc_records = [r for r in records if r["mode"] == "dc"]
        noise_records = [r for r in records if r["mode"] == "noise"]
        _require(dc_records and noise_records and records[-1]["mode"] == "noise", "there is no failed noise stage to retry.")
        dc, noise = dc_records[-1], noise_records[-1]
        _require(records.index(dc) < records.index(noise_records[0]), "DC was not completed before noise.")
        _require(noise.get("valid_data") is False and _delivery_failure(noise, simulate=simulate),
                 "noise did not end with a recoverable delivery interruption.")
        for record in records:
            if record is dc or record is noise or record.get("valid_data") is True:
                continue
            _require(_delivery_failure(record, simulate=simulate), "an earlier stage had an unsafe or unrecognized error.")
        expected_errors = ["NOISE: " + error for error in noise["errors"]]
        _require(summary.get("errors") == expected_errors,
                 "the run includes another fault, cancellation, or save/shutdown error.")
        _require(noise.get("range_code") == hw.NOISE_RANGE
                 and _status(noise.get("status_before"), 27) and _status(noise.get("status_after"), 27),
                 "the noise range or both cards' ready/routing state was not confirmed.")
        _require(noise.get("requested_seconds") == NOISE_SECONDS, "the noise duration does not match.")
        noise_rate = noise.get("actual_rate_hz")
        _require(_number(noise_rate) and abs(noise_rate / hw.SAMPLE_HZ - 1) <= .01,
                 "noise used an unexpected sample clock.")
        noise_target = round(NOISE_SECONDS * noise_rate)
        _require(type(noise.get("samples")) is int and 0 <= noise["samples"] <= noise_target
                 and type(noise.get("target_samples")) is int and noise["target_samples"] == noise_target
                 and _number(noise.get("recorded_seconds"))
                 and abs(noise["recorded_seconds"] - noise["samples"] / noise_rate) < 1e-9,
                 "the interrupted noise duration is inconsistent.")
        _require(isinstance(noise.get("settling"), dict) and noise["settling"].get("complete") is True,
                 "the noise stage did not finish settling.")

        rate = dc.get("actual_rate_hz")
        _require(_number(rate) and abs(rate / hw.SAMPLE_HZ - 1) <= .01, "DC used an unexpected sample clock.")
        target = round(DC_SECONDS * rate)
        _require(dc.get("valid_data") is True and dc.get("errors") == [], "DC is not a valid completed record.")
        _require(type(dc.get("samples")) is int and dc["samples"] == target
                 and type(dc.get("target_samples")) is int and dc["target_samples"] == target,
                 "DC does not contain the complete prescribed recording.")
        _require(dc.get("requested_seconds") == DC_SECONDS
                 and _number(dc.get("recorded_seconds")) and abs(dc["recorded_seconds"] - target / rate) < 1e-9,
                 "DC duration is inconsistent.")
        _require(dc.get("range_code") == hw.DC_RANGE, "DC used a different input range.")
        settling = dc.get("settling")
        _require(isinstance(settling, dict) and settling.get("complete") is True, "DC settling was incomplete.")
        _require(_status(dc.get("status_before"), 0) and _status(dc.get("status_after"), 0),
                 "DC readiness and routing were not confirmed.")
        diagnostics = dc.get("diagnostics")
        _require(_clean_shutdown(diagnostics, simulate=simulate), "DC stream shutdown was not confirmed.")
        _require(diagnostics.get("callback_error") is None and diagnostics.get("pool_too_small_events") == 0
                 and diagnostics.get("ended_early") is False, "DC has a delivery integrity fault.")
        _require(diagnostics.get("actual_timer_hz") == rate and diagnostics.get("nominal_scan_hz") == hw.SAMPLE_HZ,
                 "DC clock diagnostics disagree.")
        start, stop, received = (diagnostics.get(k) for k in ("started_monotonic", "stopped_monotonic", "scans_received"))
        _require(_number(start) and _number(stop) and stop > start and type(received) is int and received >= target,
                 "DC delivery timing is incomplete.")
        # The simulator advances a virtual settling clock, while stream
        # diagnostics may measure host time. Only actual-device evidence can
        # be judged by this wall-clock throughput check.
        if not simulate:
            _require(abs(received / (stop - start) / hw.SAMPLE_HZ - 1) <= .01, "DC delivery rate was outside tolerance.")
        for record, status in ((dc, 0), (noise, 27)):
            samples = record.get("status_samples")
            _require(isinstance(samples, list) and samples
                     and all(isinstance(item, dict) and type(item.get("raw_byte")) is int
                             and item["raw_byte"] == status for item in samples),
                     "a stage recorded changing readiness or control readback.")

        name = dc.get("raw_file")
        _require(isinstance(name, str) and _RAW_NAME.fullmatch(name), "the DC evidence filename is invalid.")
        raw_path = _local_file(folder, name)
        _require(raw_path.stat().st_size <= 16 * 1024 * 1024, "the DC archive is too large.")
        raw_bytes = raw_path.read_bytes()
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        _require(dc.get("raw_sha256") == raw_hash, "DC evidence no longer matches its saved hash.")
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            entries = archive.infolist()
            _require(len(entries) == 2 and {entry.filename for entry in entries} == {"converter_counts.npy", "metadata_json.npy"}
                     and sum(entry.file_size for entry in entries) <= 16 * 1024 * 1024, "the DC archive structure is invalid.")
            # Inspect NPY headers before numpy allocates from a damaged shape
            # field. These are the two standard versions used by numpy here.
            for entry_name in ("converter_counts.npy", "metadata_json.npy"):
                with archive.open(entry_name) as stream:
                    version = np.lib.format.read_magic(stream)
                    _require(version in ((1, 0), (2, 0)), "the DC archive array format is unsupported.")
                    reader = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
                    shape, _, dtype = reader(stream)
                    if entry_name == "converter_counts.npy":
                        _require(shape == (target, 57, 4) and dtype == np.dtype(np.uint16),
                                 "DC raw shape or count type changed.")
                    else:
                        _require(shape == () and dtype.kind in ("U", "S") and dtype.itemsize <= 2 * 1024 * 1024,
                                 "DC metadata array is invalid.")
        with np.load(io.BytesIO(raw_bytes), allow_pickle=False) as archive:
            raw = archive["converter_counts"]
            _require(raw.dtype == np.dtype(np.uint16) and raw.shape == (target, 57, 4), "DC raw shape or count type changed.")
            metadata_array = archive["metadata_json"]
            _require(metadata_array.ndim == 0 and metadata_array.dtype.kind in ("U", "S"), "DC metadata is invalid.")
            metadata = _json(metadata_array.item())
        for key, value in {**expected, "lot": summary["lot"], "operator": summary["operator"], "tray": summary["tray"],
                           "map_sha256": hw.MAP_SHA256, "daq_serial": serial, "installed_positions": population,
                           "physical_channels": list(range(57)), "logical_to_physical": list(hw.MAPPING),
                           "nominal_rate_hz": hw.SAMPLE_HZ, "oversample_extra": 3, "drop_first": 1,
                           "voltage_reference": "DAQ_OUTPUT", "range_limit_guard_counts": hw.RAIL_GUARD_COUNTS}.items():
            _require(metadata.get(key) == value, "DC raw metadata disagrees on " + key + ".")
        _require(metadata.get("simulated") is simulate and metadata.get("application_revision") in revisions,
                 "DC raw source or software revision is unsupported.")
        _require(metadata.get("timing_policy") in SUPPORTED_TIMING_POLICIES, "DC timing policy is unsupported.")
        _require(metadata.get("valid_data") is True, "DC raw metadata does not confirm a valid acquisition.")
        if "attempt" in metadata or "attempt" in dc:
            _require(metadata.get("attempt") == dc.get("attempt"), "DC attempt identity disagrees with raw metadata.")
        for key in ("mode", "requested_seconds", "range_code", "settling", "valid_data", "errors", "started_at",
                    "status_before", "status_after", "status_samples", "diagnostics", "samples", "target_samples",
                    "actual_rate_hz", "recorded_seconds", "raw_file"):
            _require(metadata.get(key) == dc.get(key), "DC record disagrees with raw metadata on " + key + ".")
        _require(metadata.get("stability_observation") == dc.get("stability_observation"), "DC stability evidence disagrees with raw metadata.")
        rows = dc.get("rows")
        _require(isinstance(rows, list) and len(rows) == 50
                 and all(isinstance(row, dict) and all(row.get(k) == v for k, v in identity.items())
                         for row, identity in zip(rows, hw.CHANNEL_MAP)), "DC detector mapping is incomplete.")
        _require(all(all(_number(row.get(key)) for key in ("mean_v", "min_v", "max_v"))
                     and row["min_v"] <= row["mean_v"] <= row["max_v"] for row in rows),
                 "DC detector statistics are incomplete.")
        # The raw hash protects counts, not independently editable summary
        # statistics. Recompute every row from the validated counts before
        # trusting saved offset or presence decisions. Rebuilt rows also
        # supply traces for the new result instead of copying editable charts.
        computed_rows = channel_statistics(raw, hw.DC_RANGE, rate, "dc", valid_data=True)
        core_fields = ("mean_v", "min_v", "max_v", "near_range_limit", "flags", "presence", "presence_evidence")
        for saved, computed in zip(rows, computed_rows):
            _require(all(key in saved and _same_evidence(saved[key], computed[key]) for key in core_fields),
                     "DC detector statistics disagree with the raw counts at " + computed["position"] + ".")
            # Even a sub-picovolt tolerance must not permit crossing a limit.
            saved_inside = OFFSET_MIN_V <= saved["mean_v"] <= OFFSET_MAX_V
            computed_inside = OFFSET_MIN_V <= computed["mean_v"] <= OFFSET_MAX_V
            _require(saved_inside == computed_inside, "DC offset classification disagrees with raw counts.")
        recovered_record = copy.deepcopy(dc)
        recovered_record["rows"] = computed_rows
        source = {"directory": str(folder), "original_folder": str(folder),
                  "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(), "raw_sha256": raw_hash,
                  "lot": summary["lot"], "operator": summary["operator"], "tray": summary["tray"],
                  "daq_serial": serial, "created_at": summary["created_at"], "original_created_at": summary["created_at"],
                  "application_revision": summary["application_revision"], "schema": SCHEMA,
                  "profile": hw.PROFILE, "timing_policy": metadata["timing_policy"], "map_sha256": hw.MAP_SHA256,
                  "run_id": summary.get("run_id"), "family_id": summary.get("family_id"),
                  "part_mapping": {r["position"]: r["part_id"] for r in summary.get("rows", []) if r.get("installed") and r.get("part_id")},
                  "installed_positions": list(population), "simulated": simulate,
                  "dc_recorded_seconds": dc["recorded_seconds"], "recorded_noise_seconds": noise["recorded_seconds"]}
        return {"record": recovered_record, "raw_path": raw_path, "source": source}
    except (OSError, KeyError, TypeError, zipfile.BadZipFile, zlib.error, EOFError, OverflowError, RuntimeError) as exc:
        raise ValueError("Cannot retry noise from this saved run: unreadable or incomplete evidence.") from exc
