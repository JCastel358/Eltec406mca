"""One audited trial01 import amendment; original routing guards stay unchanged.

KiCad's DSN export quantizes R52's native coordinate by +33nm on each axis.
Restore only that verified source placement inside the native-import callback.
No general tolerance, placement relaxation or route modification is allowed.
"""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import route_board as r

EXPECTED_SOURCE_SHA = "d521b5ccd8612cdf9124a9c9deba861d1cd8b8a23226c328b7ebdb7d1327707e"
ORIGINAL_POSITION_NM = [64666667, 97666667]
IMPORTED_POSITION_NM = [64666700, 97666700]
REF = "R52"


def lookup(board):
    found = [fp for fp in board.GetFootprints() if fp.GetReference() == REF]
    if len(found) != 1:
        raise RuntimeError("The reviewed R52 footprint is not unique")
    return found[0]


def signature(fp):
    pos=fp.GetPosition()
    return {"reference":str(fp.GetReference()),"uuid":fp.m_Uuid.AsString(),
            "path":fp.GetPath().AsString(),"layer":fp.GetLayer(),
            "rotation_deg":fp.GetOrientationDegrees(),"position_nm":[pos.x,pos.y]}


def routing_expressions(board):
    with tempfile.TemporaryDirectory(prefix="eltec_roundoff_route_") as tmp:
        path=Path(tmp)/"native.kicad_pcb"
        if not r.p.SaveBoard(str(path),board,True):
            raise RuntimeError("Cannot serialize routing for amendment verification")
        tree=r.parse(path.read_text(encoding="utf-8"))
    return [x for x in tree if isinstance(x,list) and x and x[0] in {"segment","via","arc"}]


def expected_quantized_identity(original):
    result=copy.deepcopy(original)
    footprints=[x for x in result if isinstance(x,list) and x and x[0]=="footprint"
                and any(isinstance(y,list) and y[:3]==["property",'"Reference"','"R52"'] for y in x)]
    if len(footprints)!=1:
        raise RuntimeError("Cannot locate exact R52 native identity expression")
    positions=r.children(footprints[0],"at")
    if len(positions)!=1 or positions[0][1:3] != ["64.666667","97.666667"]:
        raise RuntimeError("R52 source placement differs from the reviewed amendment")
    positions[0][1:3]=["64.6667","97.6667"]
    return result


def main():
    folder=r.run_path("trial01")
    handoff=folder/"handoff.json";session=folder/"routed.ses"
    report=folder/"import_roundoff_amendment.json"
    if report.exists() or (folder/"import.json").exists():
        raise RuntimeError("Amendment/import already attempted; inspect existing evidence before any retry")
    h=json.loads(handoff.read_text(encoding="utf-8"))
    r.assert_source_unchanged(h)
    if r.sha(r.BOARD)!=EXPECTED_SOURCE_SHA:
        raise RuntimeError("This adapter is authorized only for the exact trial01 source")
    original_native_import=r.p.ImportSpecctraSES
    evidence={"status":"PREPARED_NOT_IMPORTED","created_utc":datetime.now(timezone.utc).isoformat(),
              "adapter":"tools/import_trial01_roundoff.py","adapter_sha256":r.sha(__file__),
              "unchanged_original_importer_sha256":r.sha(r.__file__),
              "source_board_sha256":r.sha(r.BOARD),"handoff_sha256":r.sha(handoff),
              "session_sha256":r.sha(session),"native_dsn_sha256":r.sha(folder/"native.dsn"),
              "permitted_reference":REF,"original_position_nm":ORIGINAL_POSITION_NM,
              "quantized_position_nm":IMPORTED_POSITION_NM,
              "permitted_delta_nm":[33,33],"native_import_callback_count":0,
              "original_handoff_router_run_and_importer_files_modified":False}
    r.save_json(report,evidence)

    def corrected_native_import(board,filename):
        evidence["native_import_callback_count"]+=1
        if evidence["native_import_callback_count"]!=1 or Path(filename).resolve()!=session.resolve():
            raise RuntimeError("Unexpected native import invocation")
        before=signature(lookup(board))
        if before["position_nm"]!=ORIGINAL_POSITION_NM:
            raise RuntimeError("R52 in-memory baseline does not match the original placement")
        identity_before=r.identity(board)
        classes_before=r.native_netclasses(board)
        expected_after=expected_quantized_identity(identity_before)
        if not original_native_import(board,filename):
            raise RuntimeError("Native SES import failed before the amendment")
        fp=lookup(board);imported=signature(fp)
        expected_signature={**before,"position_nm":IMPORTED_POSITION_NM}
        if imported!=expected_signature:
            raise RuntimeError("Native R52 identity/path/layer/rotation/placement differs from the exact reviewed quantization")
        if r.identity(board)!=expected_after or r.native_netclasses(board)!=classes_before:
            raise RuntimeError("SES changed more than the two exact R52 position tokens; amendment refused")
        routes_before=routing_expressions(board)
        count_before=r.connectivity(board)
        fp.SetPosition(r.p.VECTOR2I(*ORIGINAL_POSITION_NM))
        if signature(fp)!=before or r.identity(board)!=identity_before:
            raise RuntimeError("Original strict native identity was not completely restored")
        if routing_expressions(board)!=routes_before:
            raise RuntimeError("Placement restoration changed routing expressions")
        count_after=r.connectivity(board)
        if count_after!=count_before:
            raise RuntimeError("Placement restoration changed route connectivity")
        evidence.update(status="EXACT_ROUNDOFF_RESTORED_IN_MEMORY",footprint_before=before,
                        footprint_quantized=imported,footprint_restored=signature(fp),
                        strict_nonrouting_identity_restored=True,netclasses_unchanged=True,
                        routing_expressions_unchanged=True,unconnected_before_restoration=count_before,
                        unconnected_after_restoration=count_after)
        r.save_json(report,evidence)
        return True

    try:
        r.p.ImportSpecctraSES=corrected_native_import
        # Original importer independently repeats strict identity, class,
        # Kelvin, layer, source-hash, connectivity, save/reload and backup
        # checks, then performs its existing atomic board replacement.
        r.import_session(SimpleNamespace(name="trial01"))
    except BaseException as exc:
        evidence.update(status="FAILED",error=str(exc),source_board_current_sha256=r.sha(r.BOARD))
        r.save_json(report,evidence)
        raise
    finally:
        r.p.ImportSpecctraSES=original_native_import
    imported=json.loads((folder/"import.json").read_text(encoding="utf-8"))
    evidence.update(status="IMPORTED_WITH_ORIGINAL_GUARDS_PASSED",
                    completed_utc=datetime.now(timezone.utc).isoformat(),
                    source_after_sha256=r.sha(r.BOARD),unconnected_after_import=imported["unrouted_after"],
                    fabrication_or_electrical_qualification=False)
    r.save_json(report,evidence)
    imported["position_roundoff_amendment"]={
        "report":"reports/routing/trial01/import_roundoff_amendment.json","report_sha256":r.sha(report),
        "tool":"tools/import_trial01_roundoff.py","tool_sha256":r.sha(__file__)}
    r.save_json(folder/"import.json",imported)
    print(json.dumps({"amendment":str(report),"status":evidence["status"],
                      "source_after_sha256":evidence["source_after_sha256"],
                      "unconnected_after_import":imported["unrouted_after"]},indent=2))


if __name__=="__main__":main()
