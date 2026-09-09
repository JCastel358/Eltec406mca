"""Disposable native fixture checks; never routes or imports the project PCB."""
from __future__ import annotations
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import pcbnew as p
import route_board as route


def xy(x, y):
    return p.VECTOR2I(p.FromMM(x), p.FromMM(y))


def fixture(path):
    board = p.BOARD()
    board.SetCopperLayerCount(4)
    for layer, kind in ((p.F_Cu, p.LT_SIGNAL), (p.B_Cu, p.LT_SIGNAL),
                        (p.In1_Cu, p.LT_POWER), (p.In2_Cu, p.LT_POWER)):
        board.SetLayerType(layer, kind)
    net = p.NETINFO_ITEM(board, "FixtureNet")
    board.Add(net)
    for i, x in enumerate((10, 20), 1):
        fp = p.FOOTPRINT(board)
        fp.SetReference(f"J{i}")
        fp.SetFPID(p.LIB_ID("Fixture", "OnePad"))
        route_path = p.KIID_PATH()
        for value in ("11111111-1111-4111-8111-111111111111",
                      f"22222222-2222-4222-8222-{i:012d}"):
            route_path.push_back(p.KIID(value))
        fp.SetPath(route_path)
        fp.SetPosition(xy(x, 10))
        pad = p.PAD(fp)
        pad.SetNumber("1")
        pad.SetPosition(xy(x, 10))
        pad.SetSize(xy(2, 2))
        pad.SetAttribute(p.PAD_ATTRIB_SMD)
        pad.SetShape(p.PAD_SHAPE_RECT)
        pad.SetLayerSet(p.PAD.SMDMask())
        pad.SetNet(net)
        fp.Add(pad)
        board.Add(fp)
    edge = p.PCB_SHAPE()
    edge.SetShape(p.SHAPE_T_RECT)
    edge.SetStart(xy(0, 0))
    edge.SetEnd(xy(30, 20))
    edge.SetLayer(p.Edge_Cuts)
    edge.SetWidth(p.FromMM(.05))
    board.Add(edge)
    zone = p.ZONE(board)
    zone.SetIsRuleArea(True)
    zone.SetLayerSet(p.LSET.AllCuMask(4))
    zone.SetDoNotAllowTracks(True)
    zone.SetDoNotAllowVias(True)
    zone.SetDoNotAllowZoneFills(True)
    zone.Outline().NewOutline()
    for x, y in ((1, 1), (4, 1), (4, 4), (1, 4)):
        zone.Outline().Append(p.FromMM(x), p.FromMM(y))
    board.Add(zone)
    assert p.SaveBoard(str(path), board, True)
    return board


def main():
    project_before = route.input_hashes()
    runtime = route.runtime_evidence()
    results = []
    def check(name, fn):
        try:
            detail = fn()
            results.append({"check": name, "status": "PASS", "detail": detail})
        except Exception as exc:
            results.append({"check": name, "status": "FAIL", "detail": repr(exc)})
    def rejects(fn):
        try:
            fn()
        except (ValueError, RuntimeError):
            return "Expected refusal observed"
        raise AssertionError("Expected refusal did not occur")

    with tempfile.TemporaryDirectory(prefix="eltec_routing_fixture_") as tmp:
        root = Path(tmp)
        path = root / "fixture.kicad_pcb"
        board = fixture(path)
        source_sha = route.sha(path)
        check("four_layer_initial_route_validation", lambda: route.assert_unrouted_four_layers(board))
        check("two_pad_unrouted_count", lambda: {"count": route.connectivity(board)})
        raw = root / "fixture.dsn"
        assert p.ExportSpecctraDSN(board, str(raw))
        constrained = route.constrain_dsn(raw.read_text())
        def layer_rules():
            tree = route.parse(constrained)
            structure = route.children(tree, "structure")[0]
            rules = route.children(route.children(structure, "autoroute_settings")[0], "layer_rule")
            actual = {r[1]: route.children(r, "active")[0][1] for r in rules}
            assert actual == {"F.Cu": "on", "In1.Cu": "off", "In2.Cu": "off", "B.Cu": "on"}
            return actual
        check("explicit_outer_only_layer_rules", layer_rules)
        def unchanged_dsn_content():
            before, after = route.parse(raw.read_text()), route.parse(constrained)
            route.children(after, "structure")[0].remove(route.children(route.children(after, "structure")[0], "autoroute_settings")[0])
            # Native export currently marks power layers as power. If a future
            # native exporter changes this, constrain_dsn intentionally fixes it.
            for tree in (before, after):
                for layer in route.children(route.children(tree, "structure")[0], "layer"):
                    route.children(layer, "type")[0][1] = "ignored_for_comparison"
            assert before == after
            return "All non-layer-routing-settings expressions retained"
        check("dsn_nonrouting_structure_preserved", unchanged_dsn_content)
        check("unsupported_stack_refused", lambda: rejects(lambda: route.constrain_dsn(constrained.replace("In2.Cu", "Unexpected.Cu"))))
        baseline = route.identity(board)
        classes = route.native_netclasses(board)
        check("identity_repeat_stable", lambda: baseline == route.identity(board) or (_ for _ in ()).throw(AssertionError("Identity changed")))
        ses = root / "fixture.ses"
        ses.write_text('(session fixture (base_design fixture) (routes (resolution um 10) '
                       '(parser (host_cad "KiCad") (host_version 10.0.6)) (library_out) '
                       '(network_out (net FixtureNet (wire (path F.Cu 2500 100000 -100000 200000 -100000))))))')
        def native_import():
            assert p.ImportSpecctraSES(board, str(ses))
            assert route.identity(board) == baseline
            assert route.native_netclasses(board) == classes
            assert route.connectivity(board) == 0
            assert route.sha(path) == source_sha
            return {"input": "Hand-authored two-pad SES fixture; no external router executed",
                    "native_identity_preserved": True, "unrouted_before": 1, "unrouted_after": 0,
                    "layers": route.outer_tracks_only(board)}
        check("native_ses_import_fixture", native_import)
        check("existing_routes_refused", lambda: rejects(lambda: route.assert_unrouted_four_layers(board)))
        def inner_track_rejected():
            track = next(iter(board.GetTracks()))
            track.SetLayer(p.In1_Cu)
            return rejects(lambda: route.outer_tracks_only(board))
        check("internal_signal_track_refused", inner_track_rejected)
        check("source_pcb_bytes_unchanged", lambda: route.sha(path) == source_sha or (_ for _ in ()).throw(AssertionError("Source changed")))
        # Exercise project attachment against a custom class in disposable JSON.
        pro = path.with_suffix(".kicad_pro")
        pro.write_text(json.dumps({"net_settings": {"meta": {"version": 5}, "classes": [
            {"name": "Default", "clearance": .2, "track_width": .25, "via_diameter": .6, "via_drill": .3},
            {"name": "FixturePower", "clearance": .3, "track_width": .75, "via_diameter": .8, "via_drill": .4}],
            "netclass_patterns": [{"netclass": "FixturePower", "pattern": "FixtureNet"}]}}))
        def attached_project_classes():
            original_pro = route.sha(pro)
            loaded = p.LoadBoard(str(path))
            route.apply_project_netclasses(loaded, pro)
            effective = loaded.GetDesignSettings().m_NetSettings.GetEffectiveNetClass("FixtureNet")
            assert effective.GetName() == "FixturePower", effective.GetName()
            assert effective.GetTrackWidth() == p.FromMM(.75), "Effective width differs"
            assert route.sha(pro) == original_pro, "Project file modified"
            native_dsn = root / "with_netclasses.dsn"
            assert p.ExportSpecctraDSN(loaded, str(native_dsn))
            net_tree = route.children(route.parse(native_dsn.read_text()), "network")[0]
            class_nodes = route.children(net_tree, "class")
            matched = [node for node in class_nodes if node[1].strip('"') == "FixturePower"]
            assert len(matched) == 1, "Project class missing from native DSN"
            width = route.children(route.children(matched[0], "rule")[0], "width")[0][1]
            assert float(width) == 750, width  # DSN unit um; exporter resolution remains explicit.
            return {"effective_class": effective.GetName(), "width_mm": p.ToMM(effective.GetTrackWidth()),
                    "project_json_unchanged": True, "dsn_class_width_um": float(width)}
        check("explicit_project_netclass_loading", attached_project_classes)

        def guarded_import_end_to_end():
            originals = route.ROOT, route.PROJECT, route.BOARD, route.RUNS
            try:
                route.ROOT, route.PROJECT, route.BOARD, route.RUNS = root, root, path, root / "runs"
                with contextlib.redirect_stdout(io.StringIO()):
                    route.export_board(SimpleNamespace(name="fixture"))
                folder = route.run_path("fixture")
                handoff = json.loads((folder / "handoff.json").read_text())
                saved_pro = pro.read_bytes()
                pro.write_bytes(saved_pro + b"\n")
                rejects(lambda: route.assert_source_unchanged(handoff))
                pro.write_bytes(saved_pro)
                route.assert_source_unchanged(handoff)
                # This synthetic success record exercises import guards only;
                # it is confined to the disposable fixture, never a design run.
                (folder / "routed.ses").write_bytes(ses.read_bytes())
                route.save_json(folder / "router_run.json", {
                    "status": "COMPLETED", "ses_sha256": route.sha(folder / "routed.ses"),
                    "fixture_only": True, "external_router_executed": False})
                with contextlib.redirect_stdout(io.StringIO()):
                    route.import_session(SimpleNamespace(name="fixture"))
                imported = json.loads((folder / "import.json").read_text())
                assert imported["backup_sha256"] == source_sha
                assert route.sha(Path(imported["backup"])) == source_sha
                assert imported["unrouted_after"] == 0
                assert len(list(p.LoadBoard(str(path)).GetTracks())) == 1
                return {"backup_verified": True, "changed_source_refused": True,
                        "native_paths_and_nonrouting_content_preserved": True,
                        "atomic_replacement_fixture_only": True, "unrouted_before": 1, "unrouted_after": 0}
            finally:
                route.ROOT, route.PROJECT, route.BOARD, route.RUNS = originals
        check("guarded_import_end_to_end_fixture", guarded_import_end_to_end)

    unchanged = project_before == route.input_hashes()
    results.append({"check": "actual_project_sources_untouched", "status": "PASS" if unchanged else "FAIL"})
    report = {"status": "PASS" if all(x["status"] == "PASS" for x in results) else "FAIL",
              "runtime": runtime, "checks": results, "actual_board_routed": False,
              "external_router_executed": False, "scope": "Disposable native fixture and guarded API checks only"}
    route.save_json(route.ROOT / "reports/routing_tool_probe.json", report)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
