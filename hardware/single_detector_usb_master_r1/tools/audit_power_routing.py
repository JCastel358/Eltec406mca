"""Read-only native power-route audit with source-linked evidence.

Verifies reviewed power repairs, bypass/thermal stitches, actual plane contact
and the protected sense branch. Does not replace DRC or bench qualification.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import route_board as r
import protected_kelvin as k
from add_reference_planes import uid
from add_final_power_ground_stitches import identifier, SITES


def signature(t):
    if isinstance(t,r.p.PCB_VIA):
        return {"kind":"via","uuid":t.m_Uuid.AsString(),"position_nm":k.xy(t.GetPosition()),
                "width_nm":t.GetWidth(r.p.F_Cu),"drill_nm":t.GetDrillValue(),"net":t.GetNetname(),
                "top":t.TopLayer(),"bottom":t.BottomLayer(),"locked":bool(t.IsLocked())}
    return {"kind":"track",**k.track_signature(t)}


def plane_contacts(board,via):
    return {board.GetLayerName(layer):any(z.GetFilledPolysList(layer).Contains(via.GetPosition())
            for z in board.Zones() if not z.GetIsRuleArea() and z.IsOnLayer(layer)
            and z.GetNetname()==via.GetNetname()) for layer in (r.p.In1_Cu,r.p.In2_Cu)}


def main():
    folder=r.ROOT/"reports/power_review_final";folder.mkdir(parents=True,exist_ok=True)
    source_sha=r.sha(r.BOARD);board=r.p.LoadBoard(str(r.BOARD));r.apply_project_netclasses(board)
    integration_path=r.ROOT/"reports/final_ground_integration/integration.json"
    integration=json.loads(integration_path.read_text(encoding="utf-8"))
    assert integration["board_sha256"]==source_sha,"Final integrated source revision differs"
    assert integration["violations"]==integration["unconnected"]==integration["parity"]==0
    tracks={t.m_Uuid.AsString():t for t in board.GetTracks()}
    combo_path=r.ROOT/"reports/power_bypass_combined/integration_copy.json"
    combo=json.loads(combo_path.read_text(encoding="utf-8"))
    expected_board=r.p.LoadBoard(str(r.ROOT/Path(combo["candidate"])))
    expected_tracks={t.m_Uuid.AsString():t for t in expected_board.GetTracks()}
    checks=[]
    for identity in combo["added_uuids"]:
        assert identity in tracks,"Missing reviewed power/bypass item: "+identity
        assert signature(tracks[identity])==signature(expected_tracks[identity]),"Altered power/bypass item: "+identity
        checks.append({"uuid":identity,"exact_geometry_preserved":True,"net":tracks[identity].GetNetname()})
    plan_path=r.ROOT/"reports/reference_planes_routed/plane_plan.json"
    plan=json.loads(plan_path.read_text(encoding="utf-8"))
    stitches=[]
    for row in plan["local_ground_stitches"]:
        if not row["add"]:continue
        via=tracks[uid("stitch/"+row["reference"]).AsString()]
        stub=tracks[uid("stitch_ground_stub/"+row["reference"]).AsString()]
        assert via.GetNetname()==stub.GetNetname()==row["net"]
        assert k.xy(via.GetPosition())==[round(x*1e6) for x in row["xy_mm"]]
        assert k.xy(stub.GetStart())==[round(x*1e6) for x in row["ground_stub"]["from_mm"]]
        assert k.xy(stub.GetEnd())==[round(x*1e6) for x in row["ground_stub"]["to_mm"]]
        assert stub.GetWidth()==r.p.FromMM(row["ground_stub"]["width_mm"])
        contacts=plane_contacts(board,via);assert all(contacts.values()),row["reference"]+" has no real plane contact"
        stitches.append({"reference":row["reference"],"net":row["net"],"via_mm":row["xy_mm"],
                         "stub_width_mm":row["ground_stub"]["width_mm"],"both_inner_plane_contacts":contacts})
    bridges=[]
    for row in plan["local_ground_bridges"]:
        if not row["add"]:continue
        stub=tracks[uid("ground_bridge/"+row["reference"]).AsString()]
        assert stub.GetNetname()==row["net"] and stub.GetWidth()==r.p.FromMM(row["width_mm"])
        assert k.xy(stub.GetStart())==[round(x*1e6) for x in row["from_mm"]]
        assert k.xy(stub.GetEnd())==[round(x*1e6) for x in row["to_mm"]]
        bridges.append(row["reference"])
    bypass=[]
    for row in combo["bypass_stitches"]["stitches"]:
        via=tracks[row["via_uuid"]];contacts=plane_contacts(board,via);assert all(contacts.values())
        bypass.append({"reference":row["reference"],"via_mm":row["xy_mm"],"stub_length_mm":1.2,
                       "stub_width_mm":row["ground_stub"]["width_mm"],"both_inner_plane_contacts":contacts})
    final_ground=[]
    for ref,(start,end,width) in SITES.items():
        pair=[identifier(ref,"via"),identifier(ref,"stub")]
        present=[x in tracks for x in pair];assert present[0]==present[1],"Partial final stitch: "+ref
        assert all(present),"Required final local stitch is absent: "+ref
        via,stub=[tracks[x] for x in pair]
        assert k.xy(via.GetPosition())==[round(x*1e6) for x in end]
        assert k.xy(stub.GetStart())==[round(x*1e6) for x in start] and k.xy(stub.GetEnd())==[round(x*1e6) for x in end]
        assert stub.GetWidth()==r.p.FromMM(width) and via.GetNetname()==stub.GetNetname()=="GND"
        contacts=plane_contacts(board,via);assert all(contacts.values())
        final_ground.append({"reference":ref,"present":True,"via_mm":end,"stub_length_mm":math.dist(start,end),
                             "stub_width_mm":width,"both_inner_plane_contacts":contacts})
    zones=[z for z in board.Zones() if not z.GetIsRuleArea()]
    assert all(z.IsFilled() for z in zones)
    for layer in (r.p.In1_Cu,r.p.In2_Cu):
        dg=[z.GetFilledPolysList(layer) for z in zones if z.IsOnLayer(layer) and z.GetNetname()=="GND"]
        eg=[z.GetFilledPolysList(layer) for z in zones if z.IsOnLayer(layer) and z.GetNetname()=="EM_GND"]
        assert dg and eg
        assert not any(a.Collide(b) for a in dg for b in eg),"Ground domains overlap in filled copper"
    spec=json.loads((r.RUNS/"trial01/handoff.json").read_text())["reviewed_locked_seed"]
    count=r.connectivity(board);assert count==0
    if r.sha(r.BOARD)!=source_sha:raise RuntimeError("Source changed during read-only audit")
    result={"status":"REVIEWED_POWER_GEOMETRY_AND_CONNECTIVITY_PASS","created_utc":datetime.now(timezone.utc).isoformat(),
            "source_board_sha256":source_sha,"tool_sha256":r.sha(__file__),"read_only":True,
            "reviewed_power_bypass_items":checks,"local_ground_stitches":stitches,"ground_pin_bridges":bridges,
            "additional_bypass_stitches":bypass,"final_C12_C16_stitches":final_ground,
            "kelvin":k.verify(board,spec),"unconnected":count,"outer_routing":r.outer_tracks_only(board),
            "all_copper_zones_filled":True,"detector_emitter_filled_ground_domains_separate":True,
            "source_records":{"power_combination_sha256":r.sha(combo_path),"routed_plane_plan_sha256":r.sha(plan_path),
                              "final_ground_integration_sha256":r.sha(integration_path),"native_drc_sha256":integration["drc_sha256"]},
            "native_final_integration_drc":{"violations":0,"unconnected":0,"schematic_parity_issues":0},
            "concrete_copper_defects_found":[],
            "remaining_layout_actions":[x["reference"]+" local ground candidate awaiting integration" for x in final_ground if not x["present"]],
            "bench_qualification_still_required":[
                "Actual module AVDD operating range and overshoot with instrument uncertainty; static upper corner headroom is about 4.64 mV.",
                "USB loss, battery removal/depletion and power restoration: measure actual AVDD/AIN discharge and isolation timing; module internal L1/DCR/capacitance remain acceptance inputs.",
                "Reservoir effective capacitance/ESR and complete held-rail transient step/load bounds on the assembled board.",
                "LDO startup/inrush, repeated cycling and thermal performance with actual battery range, lead impedance and load.",
                "Measurement noise, offset/repeatability and sensor/emitter operation using the actual modules; DRC does not validate analog performance."],
            "qualification_scope":"Native copper/layout review, not production approval or bench validation."}
    r.save_json(folder/"final_review.json",result)
    print(json.dumps({"status":result["status"],"source_board_sha256":source_sha,
                      "checked_power_items":len(checks),"ground_stitches":len(stitches),"bypass_stitches":len(bypass),
                      "final_C12_C16_stitches":final_ground,"unconnected":count},indent=2))


if __name__=="__main__":main()
