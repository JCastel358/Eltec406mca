"""Reproduce installed KiCad capability checks without modifying a design.

Run with KiCad's bundled Python, not an unrelated Python installation:
  & 'C:\\Program Files\\KiCad\\10.0\\bin\\python.exe' tools/probe_kicad.py

Writes only reports/toolchain_probe.json. Native functional checks use disposable
temporary boards. Source files are loaded and hashed, never saved. No software
is installed, no external router runs, and no routed result is claimed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CLI = Path(sys.executable).with_name("kicad-cli.exe")
SOURCE = ROOT / "source_reference/ESP32_test_board/ESP32_test_board.kicad_pcb"
FOOTPRINT_ROOT = Path(sys.executable).parents[1] / "share/kicad/footprints"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def java_utf8_constants(data: bytes) -> list[str]:
    """Read class constant strings without executing third-party code."""
    assert data[:4] == b"\xca\xfe\xba\xbe"
    count = struct.unpack_from(">H", data, 8)[0]
    offset, index, strings = 10, 1, []
    widths = {3: 4, 4: 4, 5: 8, 6: 8, 7: 2, 8: 2, 9: 4, 10: 4,
              11: 4, 12: 4, 15: 3, 16: 2, 17: 4, 18: 4, 19: 2, 20: 2}
    while index < count:
        tag = data[offset]
        offset += 1
        if tag == 1:
            length = struct.unpack_from(">H", data, offset)[0]
            offset += 2
            strings.append(data[offset:offset + length].decode("utf-8", errors="replace"))
            offset += length
        else:
            offset += widths[tag]
            if tag in (5, 6):
                index += 1
        index += 1
    return strings


def command(args: list[str], timeout: int = 30) -> dict:
    try:
        p = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
        return {"argv": args, "returncode": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": args, "error": repr(exc)}


def check(name: str, function) -> None:
    """Flush each observation so a later native crash does not erase evidence."""
    try:
        result = {"check": name, "status": "passed", "evidence": function()}
    except Exception as exc:
        result = {"check": name, "status": "failed", "error": repr(exc)}
    print(json.dumps(result), flush=True)


def native_checks() -> None:
    import pcbnew as p

    def inspect_api():
        selected = {
            "pcbnew": ["LoadBoard", "SaveBoard", "FootprintLoad", "KIID_PATH",
                       "ExportSpecctraDSN", "ImportSpecctraSES", "ZONE_FILLER"],
            "BOARD": ["BuildConnectivity", "GetConnectivity", "FindFootprintByPath",
                      "GetNetClasses", "GetAllNetClasses", "SynchronizeNetsAndNetClasses"],
            "FOOTPRINT": ["SetPath", "GetPath"],
            "CONNECTIVITY_DATA": ["GetUnconnectedCount", "GetConnectedPads",
                                  "GetConnectedTracks", "RecalculateRatsnest"],
            "ZONE_FILLER": ["Fill"],
            "NET_SETTINGS": ["SetNetclass", "SetNetclassPatternAssignment",
                             "GetEffectiveNetClass", "GetDefaultNetclass"],
        }
        out = {}
        for classname, names in selected.items():
            obj = p if classname == "pcbnew" else getattr(p, classname)
            out[classname] = {
                name: {"available": hasattr(obj, name),
                       "doc": getattr(getattr(obj, name, None), "__doc__", None)}
                for name in names}
        out["schematic_python_editor"] = {
            "eeschema_module_importable": __import__("importlib.util").util.find_spec("eeschema") is not None,
            "note": "pcbnew is the board API. Schematic generation must preserve native file semantics; CLI is a checker/exporter."}
        return out

    check("native_api_presence", inspect_api)

    def read_source():
        before = sha256(SOURCE)
        board = p.LoadBoard(str(SOURCE))
        selected = {}
        for footprint in board.GetFootprints():
            if footprint.GetReference() in ("U1", "U2", "J1", "J2"):
                selected[footprint.GetReference()] = {
                    "uuid": footprint.m_Uuid.AsString(),
                    "schematic_path": footprint.GetPath().AsString(),
                    "library_id": footprint.GetFPID().GetUniStringLibId()}
        after = sha256(SOURCE)
        assert before == after
        return {"source": str(SOURCE), "sha256_before": before, "sha256_after": after,
                "footprint_count": len(list(board.GetFootprints())),
                "copper_layer_count": board.GetCopperLayerCount(),
                "selected_footprints": selected}

    check("load_original_without_mutation", read_source)

    def make_board():
        board = p.BOARD()
        net = p.NETINFO_ITEM(board, "PROBE_NET")
        board.Add(net)
        edge = p.PCB_SHAPE()
        edge.SetShape(p.SHAPE_T_RECT)
        edge.SetStart(p.VECTOR2I(p.FromMM(10), p.FromMM(10)))
        edge.SetEnd(p.VECTOR2I(p.FromMM(45), p.FromMM(30)))
        edge.SetLayer(p.Edge_Cuts)
        edge.SetWidth(p.FromMM(0.05))
        board.Add(edge)
        for reference, x in (("R1", 20), ("R2", 35)):
            fp = p.FootprintLoad(str(FOOTPRINT_ROOT / "Resistor_SMD.pretty"),
                                 "R_0805_2012Metric")
            assert fp is not None
            fp.SetReference(reference)
            fp.SetPosition(p.VECTOR2I(p.FromMM(x), p.FromMM(20)))
            fp.SetPath(p.KIID_PATH("/11111111-1111-4111-8111-111111111111/"
                                  + ("22222222-2222-4222-8222-222222222222" if reference == "R1"
                                     else "33333333-3333-4333-8333-333333333333")))
            board.Add(fp)
            for pad in fp.Pads():
                if pad.GetNumber() == "1":
                    pad.SetNet(net)
        return board, net

    def library_load_and_uuid_roundtrip():
        board, _ = make_board()
        initial = {f.GetReference(): {"uuid": f.m_Uuid.AsString(),
                                     "path": f.GetPath().AsString()}
                   for f in board.GetFootprints()}
        with tempfile.TemporaryDirectory(prefix="eltec_kicad_api_") as scratch:
            path = Path(scratch) / "probe.kicad_pcb"
            saved = p.SaveBoard(str(path), board)
            assert saved and path.exists()
            reloaded = p.LoadBoard(str(path))
            actual = {f.GetReference(): {"uuid": f.m_Uuid.AsString(),
                                        "path": f.GetPath().AsString()}
                      for f in reloaded.GetFootprints()}
            assert initial == actual
            found = reloaded.FindFootprintByPath(p.KIID_PATH(initial["R1"]["path"]))
            assert found is not None and found.GetReference() == "R1"
            return {"save_returned": saved, "footprints": actual,
                    "find_by_schematic_path": found.GetReference(),
                    "format_header": path.read_text(encoding="utf-8").splitlines()[:5],
                    "scope": "Native path and UUID persistence; not full schematic parity."}

    check("footprint_load_save_reopen_and_schematic_path", library_load_and_uuid_roundtrip)

    def netclasses():
        board, _ = make_board()
        settings = board.GetDesignSettings().m_NetSettings
        nc = p.NETCLASS("ProbePower")
        nc.SetTrackWidth(p.FromMM(0.75))
        nc.SetClearance(p.FromMM(0.25))
        settings.SetNetclass("ProbePower", nc)
        settings.SetNetclassPatternAssignment("PROBE_NET", "ProbePower")
        board.SynchronizeNetsAndNetClasses(False)
        effective = settings.GetEffectiveNetClass("PROBE_NET")
        assert abs(p.ToMM(effective.GetTrackWidth()) - 0.75) < 1e-9
        return {"effective_name": effective.GetName(),
                "track_width_mm": p.ToMM(effective.GetTrackWidth()),
                "clearance_mm": p.ToMM(effective.GetClearance()),
                "classes": sorted(str(name) for name in board.GetAllNetClasses()),
                "persistence": "In-memory assignment proven. Persist classes/patterns in .kicad_pro and revalidate with CLI; board SaveBoard alone is not proof of project settings."}

    check("board_netclass_creation_and_assignment", netclasses)

    def connectivity():
        board, net = make_board()
        assert board.BuildConnectivity()
        count = board.GetConnectivity().GetUnconnectedCount(False)
        assert count == 1
        pads = [pad for fp in board.GetFootprints() for pad in fp.Pads()
                if pad.GetNetname() == "PROBE_NET"]
        segment = p.PCB_TRACK(board)
        segment.SetStart(pads[0].GetPosition())
        segment.SetEnd(pads[1].GetPosition())
        segment.SetWidth(p.FromMM(0.3))
        segment.SetLayer(p.F_Cu)
        segment.SetNet(net)
        board.Add(segment)
        board.BuildConnectivity()
        after = board.GetConnectivity().GetUnconnectedCount(False)
        assert after == 0
        return {"before_test_segment": count, "after_test_segment": after,
                "scope": "Disposable two-pad fixture only; actual project not routed or certified."}

    check("connectivity_counts_detect_fixture_connection", connectivity)

    def zonefill():
        board, net = make_board()
        zone = p.ZONE(board)
        zone.SetLayer(p.F_Cu)
        zone.SetNet(net)
        zone.SetLocalClearance(p.FromMM(0.25))
        zone.SetMinThickness(p.FromMM(0.25))
        zone.SetThermalReliefGap(p.FromMM(0.3))
        zone.SetThermalReliefSpokeWidth(p.FromMM(0.3))
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in ((11, 11), (44, 11), (44, 29), (11, 29)):
            outline.Append(p.FromMM(x), p.FromMM(y))
        board.Add(zone)
        board.BuildConnectivity()
        result = p.ZONE_FILLER(board).Fill(board.Zones())
        assert result and zone.GetFilledArea() > 0
        return {"fill_returned": result, "zone_count": len(list(board.Zones())),
                "filled_area_internal_units_squared": zone.GetFilledArea(),
                "scope": "API exercised on disposable fixture; production validation must refill before DRC."}

    check("zone_fill_disposable_fixture", zonefill)

    def dsn_export():
        board, _ = make_board()
        with tempfile.TemporaryDirectory(prefix="eltec_kicad_dsn_") as scratch:
            board_path = Path(scratch) / "probe.kicad_pcb"
            dsn_path = Path(scratch) / "probe.dsn"
            p.SaveBoard(str(board_path), board)
            result = p.ExportSpecctraDSN(board, str(dsn_path))
            assert result and dsn_path.exists() and dsn_path.stat().st_size > 0
            data = dsn_path.read_text(encoding="utf-8")
            assert "PROBE_NET" in data
            return {"export_returned": result, "bytes": dsn_path.stat().st_size,
                    "sha256": sha256(dsn_path), "header": data.splitlines()[:9],
                    "ses_import": {"available": hasattr(p, "ImportSpecctraSES"),
                                   "executed": False,
                                   "reason": "No authentic router session available; no routing requested in capability probe."}}

    check("specctra_dsn_export", dsn_export)


def router_inventory() -> dict:
    user = Path.home()
    roots = [Path("C:/Program Files"), Path("C:/Program Files (x86)"),
             user / "Downloads", user / "Documents/KiCad",
             user / "AppData/Roaming/kicad", user / ".kicad_plugins",
             user / "AppData/Local/Programs"]
    candidates = []
    for root in roots:
        if not root.exists():
            continue
        # Bounded scan: Java/router names in immediate children, and KiCad plugins.
        for item in root.iterdir():
            if any(term in item.name.lower() for term in
                   ("java", "jdk", "freerout", "temurin", "adoptium", "zulu")):
                candidates.append(str(item))
        if "kicad" in root.name.lower():
            for item in root.rglob("*"):
                if item.is_file() and ("freerout" in item.name.lower()
                                       or item.suffix.lower() == ".jar"):
                    candidates.append(str(item))
    executables = {name: shutil.which(name) for name in
                   ("java", "javaw", "freerouting", "freerouting.exe")}
    result = {"path_executables": executables,
              "java_home": os.environ.get("JAVA_HOME"),
              "bounded_search_roots": [str(root) for root in roots],
              "candidates": sorted(set(candidates)),
              "scope": "Absence is bounded to PATH and listed locations, not a full-disk claim. No installation performed."}
    if executables["java"]:
        result["java_version"] = command([executables["java"], "-version"])
    # User-owned related project was identified by the parent agent. Read only:
    # verify runtime and JAR metadata without starting a router or writing there.
    related = user / "Documents/Eltec_50Piece_board/hardware/ir_array_rev_b/tools"
    local_java = related / "java/jdk-25.0.4.1+1-jre/bin/java.exe"
    result["related_project_tools"] = {"path": str(related), "exists": related.exists()}
    if local_java.exists():
        result["related_project_tools"]["java"] = {
            "path": str(local_java), "sha256": sha256(local_java),
            "version_command": command([str(local_java), "-version"])}
    jars = []
    if related.exists():
        for jar in sorted(related.glob("freerouting-*.jar")):
            with zipfile.ZipFile(jar) as archive:
                manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
                version_class = "app/freerouting/settings/GlobalSettings.class"
                constants = java_utf8_constants(archive.read(version_class))
                versions = sorted(set(s for s in constants
                                      if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].*)?", s)))
            jars.append({"path": str(jar), "bytes": jar.stat().st_size,
                         "sha256": sha256(jar), "manifest": manifest,
                         "version_constants": {"class": version_class, "values": versions},
                         "executed": False})
    result["related_project_tools"]["router_jars"] = jars
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-child", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/toolchain_probe.json")
    args = parser.parse_args()
    if args.native_child:
        native_checks()
        return 0
    helps = [[], ["pcb"], ["pcb", "drc"], ["pcb", "export"], ["pcb", "import"],
             ["pcb", "export", "gerbers"], ["pcb", "export", "drill"],
             ["pcb", "export", "pos"], ["pcb", "export", "pdf"],
             ["pcb", "export", "svg"], ["sch"], ["sch", "erc"],
             ["sch", "export"], ["sch", "export", "netlist"],
             ["sch", "export", "bom"], ["sch", "export", "pdf"]]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        cli_help = list(pool.map(lambda a: command([str(CLI), *a, "--help"]), helps))
    native = command([sys.executable, str(Path(__file__).resolve()), "--native-child"], timeout=90)
    observations = []
    for line in native.get("stdout", "").splitlines():
        try:
            observations.append(json.loads(line))
        except json.JSONDecodeError:
            observations.append({"unparsed_native_stdout": line})
    report = {"schema": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "python": {"executable": sys.executable, "version": sys.version},
              "platform": platform.platform(), "kicad_cli": command([str(CLI), "version"]),
              "footprint_library_root": str(FOOTPRINT_ROOT),
              "footprint_library_root_exists": FOOTPRINT_ROOT.exists(),
              "cli_help": cli_help, "native_process": native,
              "native_checks": observations, "external_router_inventory": router_inventory(),
              "limits": ["No design ERC, DRC, or complete schematic parity is established by this capability report.",
                         "No external autorouter ran; only a disposable connectivity fixture was used.",
                         "SES import API presence is observed; a routed-session import is not yet validated."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    for item in observations:
        if "check" in item:
            print(item["status"], item["check"], item.get("error", ""))
    failures = [item for item in observations if item.get("status") == "failed"]
    return 0 if native.get("returncode") == 0 and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
