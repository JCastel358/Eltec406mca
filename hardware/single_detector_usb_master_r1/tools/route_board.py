"""Guarded local DSN/SES workflow. No routing happens without the `run` command.

Use KiCad's bundled Python. Export is restricted to this project's four-layer,
placement-only board. The two internal copper layers remain reserved for planes.
Import preserves all non-routing native content or refuses the candidate; it
never silently regenerates a board from DSN. Native DRC and plane work follow.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import pcbnew as p
import protected_kelvin as kelvin

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "single_detector_usb_master"
BOARD = PROJECT / "single_detector_usb_master.kicad_pcb"
RUNS = ROOT / "reports/routing"
RUNTIME = Path("C:/Users/JoseCastelblanco/Documents/Eltec_50Piece_board/hardware/ir_array_rev_b/tools")
JAVA = RUNTIME / "java/jdk-25.0.4.1+1-jre/bin/java.exe"
JAR = RUNTIME / "freerouting-2.2.4.jar"
JAVA_SHA = "5808527e3dfc4eb285c7664ba356bc267055ef906e83ed4660d9f7976da7349b"
JAR_SHA = "f5ed374182900ccc78e473518bbb9f6b869f4a07159495f663a76f52bb10523b"
REVISION = "20f1a72e546b9b23c7ba5127086885cfacbdd4be"
LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def utc():
    return datetime.now(timezone.utc).isoformat()


def tokenize(source):
    """Read native/Specctra expressions, including DSN's bare quote declaration.

    This is a structural tokenizer, not an unrestricted expression evaluator.
    Keeping original token spelling also detects changes to native quoted text.
    """
    declaration = re.compile(r"\(string_quote\s+\"\s*\)")
    source = declaration.sub('(string_quote __DSN_QUOTE__)', source)
    return re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', source)


def parse(source):
    stack, roots = [], []
    for token in tokenize(source):
        if token == "(":
            node = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("Unbalanced closing parenthesis")
            stack.pop()
        elif stack:
            stack[-1].append(token)
        else:
            raise ValueError("Unexpected top-level token")
    if stack or len(roots) != 1:
        raise ValueError("Expected one complete expression")
    return roots[0]


def emit(tree, depth=0):
    """Stable serializer; quote declaration restored for Specctra compatibility."""
    if not isinstance(tree, list):
        return '"' if tree == "__DSN_QUOTE__" else tree
    text = "("
    for index, child in enumerate(tree):
        text += ("\n" + "  " * (depth + 1) if isinstance(child, list)
                 else (" " if index else "")) + emit(child, depth + 1)
    return text + ")"


def children(tree, name):
    return [node for node in tree if isinstance(node, list) and node and node[0] == name]


def constrain_dsn(source, reviewed_seed=None):
    tree = parse(source)
    structures = children(tree, "structure")
    if tree[0] != "pcb" or len(structures) != 1:
        raise ValueError("Not a native board DSN")
    structure = structures[0]
    layer_nodes = children(structure, "layer")
    names = [node[1].strip('"') for node in layer_nodes]
    if names != list(LAYERS):
        raise ValueError(f"Expected four physical layers {LAYERS}, got {names}")
    for node in layer_nodes:
        name = node[1].strip('"')
        types = children(node, "type")
        if len(types) != 1:
            raise ValueError("Ambiguous DSN layer type")
        types[0][1] = "signal" if name in ("F.Cu", "B.Cu") else "power"
    for old in children(structure, "autoroute_settings"):
        structure.remove(old)
    settings = ["autoroute_settings", ["autoroute", "on"],
                ["postroute", "on"], ["vias", "on"]]
    for name in LAYERS:
        settings.append(["layer_rule", name,
                         ["active", "on" if name in ("F.Cu", "B.Cu") else "off"],
                         ["preferred_direction", "horizontal" if name == "F.Cu" else "vertical"]])
    # Pinned freerouting Structure.read_scope only consumes this block before
    # a keepout/plane initializes layer_structure. Appending after KiCad's
    # antenna keepout silently loads an empty board (upstream revision above,
    # Structure.java lines 866-870). Keep all layer definitions before it.
    last_layer = max(structure.index(node) for node in layer_nodes)
    if any(isinstance(node, list) and node and node[0] in
           {"keepout", "via_keepout", "place_keepout", "plane"}
           for node in structure[:last_layer]):
        raise RuntimeError("DSN geometry precedes layer definitions; reviewed parser ordering unavailable")
    structure.insert(last_layer + 1, settings)
    if reviewed_seed is not None:
        kelvin.guard_dsn(tree, reviewed_seed)
    result = emit(tree) + "\n"
    if parse(result) != tree:
        raise AssertionError("DSN structural round-trip failed")
    return result


def input_hashes():
    paths = [BOARD, Path(__file__).resolve(), Path(kelvin.__file__).resolve(),
             *PROJECT.glob("*.kicad_sch"), *PROJECT.glob("*.kicad_pro"),
             *PROJECT.glob("*.kicad_dru"), *PROJECT.glob("*-lib-table"),
             *ROOT.glob("libraries/*.kicad_sym"), *ROOT.glob("libraries/master.pretty/*.kicad_mod")]
    for name in ("interface_netlist.xml", "design_manifest.json", "placement_manifest.json"):
        path = ROOT / "reports" / name
        if path.exists():
            paths.append(path)
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(set(paths))}


def assert_source_unchanged(record):
    now = input_hashes()
    if now != record["source_hashes"]:
        changed = sorted(key for key in set(now) | set(record["source_hashes"])
                         if now.get(key) != record["source_hashes"].get(key))
        raise RuntimeError(f"Source changed since DSN export; re-export after review: {changed}")


def assert_unrouted_four_layers(board):
    if board.GetCopperLayerCount() != 4:
        raise RuntimeError("Expected the reviewed four-layer placement board")
    if list(board.GetTracks()):
        raise RuntimeError("Initial-route wrapper refuses existing tracks/vias; use a separately reviewed incremental-routing workflow")
    if any(not zone.GetIsRuleArea() for zone in board.Zones()):
        raise RuntimeError("Initial-route wrapper refuses copper zones; rule areas are retained")
    for layer, kind in ((p.F_Cu, p.LT_SIGNAL), (p.B_Cu, p.LT_SIGNAL),
                        (p.In1_Cu, p.LT_POWER), (p.In2_Cu, p.LT_POWER)):
        if board.GetLayerType(layer) != kind:
            raise RuntimeError(f"Unexpected layer purpose for {board.GetLayerName(layer)}")


def apply_project_netclasses(board, project_path=None):
    """Mirror explicit project PCB class rules through the tested SWIG API.

    Standalone pcbnew's SETTINGS_MANAGER.LoadProject returned false in the
    installed runtime fixture. Do not silently fall back to built-in widths.
    The JSON remains untouched. Native custom .kicad_dru rules still require DRC.
    """
    project_path = Path(project_path or BOARD.with_suffix(".kicad_pro"))
    data = json.loads(project_path.read_text(encoding="utf-8")).get("net_settings", {})
    classes = data.get("classes", [])
    if not classes or sum(row.get("name") == "Default" for row in classes) != 1:
        raise RuntimeError("Define explicit reviewed PCB netclasses, including Default, in .kicad_pro before DSN export")
    if data.get("netclass_assignments"):
        raise RuntimeError("Legacy/label netclass assignments need a reviewed loader; convert to explicit project netclass_patterns first")
    settings = board.GetDesignSettings().m_NetSettings
    settings.ClearNetclasses()
    settings.ClearNetclassPatternAssignments()
    fields = {"clearance": "SetClearance", "track_width": "SetTrackWidth",
              "via_diameter": "SetViaDiameter", "via_drill": "SetViaDrill",
              "microvia_diameter": "SetuViaDiameter", "microvia_drill": "SetuViaDrill",
              "diff_pair_width": "SetDiffPairWidth", "diff_pair_gap": "SetDiffPairGap",
              "diff_pair_via_gap": "SetDiffPairViaGap"}
    for row in classes:
        nc = p.NETCLASS(row["name"])
        for field, method in fields.items():
            if row.get(field) is not None:
                getattr(nc, method)(p.FromMM(row[field]))
        if row.get("priority") is not None:
            nc.SetPriority(int(row["priority"]))
        if row["name"] == "Default":
            settings.SetDefaultNetclass(nc)
        else:
            settings.SetNetclass(row["name"], nc)
    names = {row["name"] for row in classes}
    for item in data.get("netclass_patterns", []):
        if item["netclass"] not in names:
            raise RuntimeError(f"Unknown netclass in project pattern: {item}")
        settings.SetNetclassPatternAssignment(item["pattern"], item["netclass"])
    board.SynchronizeNetsAndNetClasses(False)


def connectivity(board):
    board.BuildConnectivity()
    return int(board.GetConnectivity().GetUnconnectedCount(False))


def identity(board):
    """Compare all native non-routing expressions, including paths and geometry."""
    with tempfile.TemporaryDirectory(prefix="eltec_native_identity_") as tmp:
        native = Path(tmp) / "identity.kicad_pcb"
        if not p.SaveBoard(str(native), board, True):
            raise RuntimeError("Native identity serialization failed")
        tree = parse(native.read_text(encoding="utf-8"))
    # Copper additions are the sole allowed semantic change. Nets remain the
    # existing native nets; footprint UUID/path, all pads, rule areas, outline,
    # stack/settings/text stay part of this strict structural comparison.
    return [node for node in tree if not (isinstance(node, list) and node and
                                         node[0] in {"segment", "via", "arc"})]


def native_netclasses(board):
    methods = ("GetClearance", "GetTrackWidth", "GetViaDiameter", "GetViaDrill",
               "GetuViaDiameter", "GetuViaDrill", "GetDiffPairWidth", "GetDiffPairGap")
    classes = {str(key): {method: getattr(value, method)() for method in methods}
               for key, value in board.GetAllNetClasses().items()}
    effective = {f"{fp.GetReference()}.{pad.GetNumber()}": str(pad.GetNetClassName())
                 for fp in board.GetFootprints() for pad in fp.Pads()}
    return {"classes": classes, "effective_pad_classes": effective}


def outer_tracks_only(board):
    counts = {name: 0 for name in LAYERS}
    vias = 0
    for track in board.GetTracks():
        if isinstance(track, p.PCB_VIA):
            vias += 1
            if track.TopLayer() != p.F_Cu or track.BottomLayer() != p.B_Cu:
                raise RuntimeError("Blind/buried via found; this wrapper allows only through vias")
        else:
            name = board.GetLayerName(track.GetLayer())
            if name not in ("F.Cu", "B.Cu"):
                raise RuntimeError(f"Forbidden internal-layer signal track: {name}")
            counts[name] += 1
    return {"track_segments_by_layer": counts, "through_vias": vias}


def run_path(run):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run):
        raise ValueError("Run name must be a short plain identifier")
    return RUNS / run


def export_board(args):
    folder = run_path(args.name)
    folder.mkdir(parents=True, exist_ok=False)
    source = input_hashes()
    board = p.LoadBoard(str(BOARD))
    assert_unrouted_four_layers(board)
    apply_project_netclasses(board)
    reviewed_seed = None
    if getattr(args, "reviewed_kelvin_seed", False):
        rules = json.loads(BOARD.with_suffix(".kicad_pro").read_text(encoding="utf-8"))["board"]["design_settings"]["rules"]
        if p.FromMM(float(rules["min_track_width"])) > kelvin.WIDTH_NM:
            raise RuntimeError("Reviewed Kelvin seed is below the project's minimum track width")
        reviewed_seed = kelvin.manifest(board, sha(BOARD))
        kelvin.seed_board(board, reviewed_seed)
        preview = folder / "reviewed_seed_preview.kicad_pcb"
        if not p.SaveBoard(str(preview), board, True):
            raise RuntimeError("Native seed preview save failed")
        kelvin.verify(p.LoadBoard(str(preview)), reviewed_seed)
    raw = folder / "native.dsn"
    if not p.ExportSpecctraDSN(board, str(raw)):
        raise RuntimeError("KiCad DSN export failed")
    constrained = folder / "outer_layers.dsn"
    constrained.write_text(constrain_dsn(raw.read_text(encoding="utf-8"), reviewed_seed), encoding="utf-8")
    record = {"status": "EXPORTED_NOT_ROUTED", "created_utc": utc(),
              "source_board": str(BOARD), "source_hashes": source,
              "kicad_version": p.GetBuildVersion(),
              "native_dsn_sha256": sha(raw), "routing_dsn_sha256": sha(constrained),
              "unrouted_before": connectivity(board), "netclasses": native_netclasses(board),
              "layer_policy": {"signals": ["F.Cu", "B.Cu"], "reserved_planes": ["In1.Cu", "In2.Cu"]},
              "initial_route_only": True, "plane_fill_performed": False,
              "reviewed_locked_seed": reviewed_seed,
              "drill_place_origin_nm": kelvin.xy(board.GetDesignSettings().GetAuxOrigin())}
    assert_source_unchanged(record)
    save_json(folder / "handoff.json", record)
    print(json.dumps(record, indent=2))


def runtime_evidence():
    result = {"java": {"path": str(JAVA), "sha256": sha(JAVA)},
              "router": {"path": str(JAR), "sha256": sha(JAR), "version": "2.2.4",
                         "build_revision": REVISION}, "other_project_modified": False}
    if result["java"]["sha256"] != JAVA_SHA or result["router"]["sha256"] != JAR_SHA:
        raise RuntimeError("Pinned local routing runtime changed; inspect it before proceeding")
    version = subprocess.run([str(JAVA), "-version"], capture_output=True, text=True,
                             timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if version.returncode:
        raise RuntimeError("Java version probe failed")
    result["java"]["version"] = (version.stdout + version.stderr).strip()
    return result


def session_evidence(path, expected_connections):
    """A zero-exit router can still produce an empty/invalid session."""
    path = Path(path)
    if not path.exists() or not path.stat().st_size:
        raise RuntimeError("Router produced no nonempty SES")
    tree = parse(path.read_text(encoding="utf-8"))
    routes = children(tree, "routes")
    if tree[0] != "session" or len(routes) != 1:
        raise RuntimeError("Router output is not a complete SES routes expression")
    networks = children(routes[0], "network_out")
    if len(networks) != 1:
        raise RuntimeError("Router SES has no unique network_out")
    wires = [wire for net in children(networks[0], "net") for wire in children(net, "wire")]
    paths = [path for wire in wires for path in children(wire, "path")
             if len(path) >= 7]  # layer, width and at least two x/y points
    if expected_connections > 0 and not paths:
        raise RuntimeError("Router SES contains no wiring despite expected connections")
    return {"session_bytes": path.stat().st_size, "routed_wire_paths": len(paths)}


def apply_recorded_seed(board, record):
    spec = record.get("reviewed_locked_seed")
    if spec is None:
        return None
    if spec != kelvin.manifest(board, sha(BOARD)):
        raise RuntimeError("Recorded seed differs from the reviewed profile/source board")
    kelvin.seed_board(board, spec)
    return kelvin.verify(board, spec)


def route(args):
    folder = run_path(args.name)
    record = json.loads((folder / "handoff.json").read_text())
    assert_source_unchanged(record)
    src, ses = folder / "outer_layers.dsn", folder / "routed.ses"
    if sha(src) != record["routing_dsn_sha256"]:
        raise RuntimeError("Exported DSN changed")
    if ses.exists() or (folder / "router_run.json").exists():
        raise RuntimeError("Run already attempted; use a new export/run name")
    runtime = runtime_evidence()
    profile = folder / "router_profile"
    profile.mkdir()
    command = [str(JAVA), "-jar", str(JAR), "-de", str(src), "-do", str(ses),
               "--gui.enabled=false", "--api_server.enabled=false",
               f"--user_data_path={profile}", "-da", "-mp", str(args.passes), "-mt", "1",
               "--logging.console.level=INFO", "--logging.file.level=DEBUG"]
    result = {"status": "RUNNING", "started_utc": utc(), "command": command,
              "runtime": runtime, "input_sha256": sha(src), "timeout_seconds": args.timeout,
              "handoff_sha256": sha(folder / "handoff.json"),
              "gui_enabled": False, "api_server_enabled": False, "analytics_disabled": True,
              "hardware_upload": False, "pcb_modified": False}
    report = folder / "router_run.json"
    save_json(report, result)
    start = time.monotonic()
    with (folder / "router.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, cwd=folder, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            returncode = proc.wait(timeout=args.timeout)
            result.update(returncode=returncode, status="FAILED")
            if returncode == 0:
                try:
                    result.update(session_evidence(ses, record["unrouted_before"]))
                    result["status"] = "COMPLETED"
                except (RuntimeError, ValueError, UnicodeError) as exc:
                    result["session_rejection"] = str(exc)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=20)
            result.update(status="TIMED_OUT", returncode=proc.returncode)
    result.update(finished_utc=utc(), elapsed_seconds=round(time.monotonic() - start, 2))
    if ses.exists():
        result["ses_sha256"] = sha(ses)
    result["source_unchanged"] = input_hashes() == record["source_hashes"]
    save_json(report, result)
    print(json.dumps(result, indent=2))
    if result["status"] != "COMPLETED" or not result["source_unchanged"]:
        raise RuntimeError("Router did not finish against unchanged input; SES import refused")


def import_session(args):
    folder = run_path(args.name)
    record = json.loads((folder / "handoff.json").read_text())
    routed = json.loads((folder / "router_run.json").read_text())
    assert_source_unchanged(record)
    ses = folder / "routed.ses"
    if routed["status"] != "COMPLETED" or sha(ses) != routed.get("ses_sha256"):
        raise RuntimeError("No successful, hash-matched router session")
    if sha(folder / "handoff.json") != routed.get("handoff_sha256"):
        raise RuntimeError("Routing handoff changed after run")
    session_evidence(ses, record["unrouted_before"])
    if sha(folder / "outer_layers.dsn") != record["routing_dsn_sha256"]:
        raise RuntimeError("DSN was changed after export")
    if (folder / "import.json").exists():
        raise RuntimeError("Session already imported")
    # Byte-identical backup precedes even the in-memory import operation.
    backup = folder / ("before_import_" + sha(BOARD)[:16] + ".kicad_pcb")
    if backup.exists():
        if sha(backup) != sha(BOARD):
            raise RuntimeError("Pre-existing backup hash mismatch")
    else:
        shutil.copyfile(BOARD, backup)
    if sha(backup) != sha(BOARD):
        raise RuntimeError("Backup verification failed")
    board = p.LoadBoard(str(BOARD))
    assert_unrouted_four_layers(board)
    apply_project_netclasses(board)
    seed_check = apply_recorded_seed(board, record)
    original_identity, original_classes = identity(board), native_netclasses(board)
    before = connectivity(board)
    if not p.ImportSpecctraSES(board, str(ses)):
        raise RuntimeError("Native SES import failed; source PCB remains untouched")
    layers = outer_tracks_only(board)
    if seed_check is not None:
        seed_check = kelvin.verify(board, record["reviewed_locked_seed"])
    if identity(board) != original_identity or native_netclasses(board) != original_classes:
        raise RuntimeError("Import changed native non-routing content or netclasses; source PCB remains untouched")
    after = connectivity(board)
    if after > before or (before > 0 and after == before):
        raise RuntimeError("Import failed to reduce the expected unrouted count; source PCB remains untouched")
    candidate = folder / "import_candidate.kicad_pcb"
    if not p.SaveBoard(str(candidate), board, True):
        raise RuntimeError("Candidate native save failed")
    reloaded = p.LoadBoard(str(candidate))
    apply_project_netclasses(reloaded)
    if seed_check is not None:
        kelvin.verify(reloaded, record["reviewed_locked_seed"])
    if identity(reloaded) != original_identity or outer_tracks_only(reloaded) != layers:
        raise RuntimeError("Candidate save/reload changed native content")
    # Project-specific netclasses live in the unchanged .kicad_pro beside the
    # source board. Candidate path has no project: compare classes in-memory,
    # and guard the project's bytes rather than claiming the candidate loads it.
    assert_source_unchanged(record)
    # Atomic replacement from a same-filesystem staging file, after preserving
    # a verified backup. No arbitrary target path is accepted from the CLI.
    staging = BOARD.with_suffix(".routing-stage.kicad_pcb")
    if staging.exists():
        raise RuntimeError("Staging file already exists; inspect it first")
    shutil.copyfile(candidate, staging)
    assert_source_unchanged(record)
    os.replace(staging, BOARD)
    result = {"status": "IMPORTED_NOT_DRC_QUALIFIED", "completed_utc": utc(),
              "backup": str(backup), "backup_sha256": sha(backup),
              "source_after_sha256": sha(BOARD), "ses_sha256": sha(ses),
              "unrouted_before": before, "unrouted_after": after, **layers,
              "native_nonrouting_content_preserved": True,
              "footprint_uuid_paths_pad_geometry_nets_preserved": True,
              "netclasses_preserved_in_memory_and_project_hash_guarded": True,
              "reviewed_locked_seed_verification": seed_check,
              "plane_fill_performed": False, "drc_run": False,
              "required_followup": "Inspect routing, add separate reference planes, refill, and run native KiCad DRC/parity."}
    save_json(folder / "import.json", result)
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("export", export_board), ("run", route), ("import", import_session)):
        item = sub.add_parser(name)
        item.add_argument("name", help="New run identifier under reports/routing")
        item.set_defaults(func=func)
        if name == "run":
            item.add_argument("--passes", type=int, default=8)
            item.add_argument("--timeout", type=int, default=600, help="Hard router process limit in seconds")
        elif name == "export":
            item.add_argument("--reviewed-kelvin-seed", action="store_true",
                              help="Explicitly seed only reviewed U10.12 to C11.1 locked 0.20mm F.Cu trace; source PCB unchanged")
    args = parser.parse_args()
    if args.command == "run" and not (1 <= args.passes <= 100 and 1 <= args.timeout <= 7200):
        parser.error("passes must be 1..100 and timeout 1..7200 seconds")
    args.func(args)


if __name__ == "__main__":
    main()
