"""Two local power-ground stitches; standalone use writes only a review copy.

C12 reservoir return gains local access to both ground planes. C16 retains its
existing direct comparator bypass loop and gains a short plane connection.
No placement or existing copper is changed. Refill and DRC after integration.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import uuid
import route_board as r
import protected_kelvin as k
from add_reference_planes import local_ground_stitch, point, native_drc
from add_bypass_stitches import geometry_without_fill

SITES={"C12":([26.475,-22.0],[26.475,-20.05],1.0),
       "C16":([65.5,37.775],[64.5,38.5],.3)}
NAMESPACE=uuid.UUID("1c9b259f-1058-4748-a3cb-7596f1de44bb")


def identifier(ref,kind):return str(uuid.uuid5(NAMESPACE,ref+".2/"+kind+"/v1"))


def add_stitches(board):
    expected={identifier(ref,kind) for ref in SITES for kind in ("via","stub")}
    if any(t.m_Uuid.AsString() in expected for t in board.GetTracks()):
        raise RuntimeError("Final power-ground stitches already partly/fully present")
    original=geometry_without_fill(board,set())
    before=r.connectivity(board)
    grounds={layer:[z.GetFilledPolysList(layer) for z in board.Zones()
                    if not z.GetIsRuleArea() and z.GetNetname()=="GND" and z.IsOnLayer(layer)]
             for layer in (r.p.In1_Cu,r.p.In2_Cu)}
    region=[[12,-30],[98,-30],[98,115],[12,115]]
    rows=[]
    for ref,(start,end,width) in SITES.items():
        pad=k.pad(board,ref,"2")
        if k.xy(pad.GetPosition()) != [round(x*1e6) for x in start] or pad.GetNetname()!="GND":
            raise RuntimeError("Reviewed ground pad changed: "+ref)
        if not all(any(poly.Contains(point(*end)) for poly in grounds[layer]) for layer in grounds):
            raise RuntimeError("Local stitch is not over filled GND on both inner layers: "+ref)
        row=local_ground_stitch(board,region,pad,ref+".2",net="GND",width=width,
                                skip_existing_region=False,candidates=[end],required=True)
        row.update(via_uuid=identifier(ref,"via"),stub_uuid=identifier(ref,"stub"))
        rows.append(row)
    for row in rows:
        via=r.p.PCB_VIA(board);via.SetUuid(r.p.KIID(row["via_uuid"]))
        via.SetPosition(point(*row["xy_mm"]));via.SetWidth(r.p.FromMM(.6));via.SetDrill(r.p.FromMM(.3))
        via.SetViaType(r.p.VIATYPE_THROUGH);via.SetLayerPair(r.p.F_Cu,r.p.B_Cu);via.SetNet(board.FindNet("GND"))
        via.SetFrontTentingMode(r.p.TENTING_MODE_TENTED);via.SetBackTentingMode(r.p.TENTING_MODE_TENTED)
        via.SetLocked(True);board.Add(via)
        stub=r.p.PCB_TRACK(board);stub.SetUuid(r.p.KIID(row["stub_uuid"]))
        stub.SetStart(point(*row["ground_stub"]["from_mm"]));stub.SetEnd(point(*row["ground_stub"]["to_mm"]))
        stub.SetWidth(r.p.FromMM(row["ground_stub"]["width_mm"]));stub.SetLayer(r.p.F_Cu)
        stub.SetNet(board.FindNet("GND"));stub.SetLocked(True);board.Add(stub)
    if geometry_without_fill(board,expected)!=original:
        raise RuntimeError("Ground stitches changed existing native geometry")
    after=r.connectivity(board)
    if before!=after:raise RuntimeError("Ground stitches unexpectedly changed net completeness")
    spec=json.loads((r.RUNS/"trial01/handoff.json").read_text())["reviewed_locked_seed"]
    return {"status":"TWO_LOCAL_POWER_GROUND_STITCHES_ADDED","added_uuids":sorted(expected),
            "stitches":rows,"existing_native_geometry_preserved":True,"unconnected_before":before,
            "unconnected_after":after,"kelvin":k.verify(board,spec)}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source",type=Path,default=r.BOARD)
    ap.add_argument("--output-dir",type=Path,required=True)
    args=ap.parse_args();source=args.source.resolve();output=args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=False);project=output/"candidate";project.mkdir()
    candidate=project/source.name
    if candidate==source or candidate==r.BOARD.resolve():raise RuntimeError("Output must be a separate review copy")
    source_sha=r.sha(source)
    for pattern in ("*.kicad_sch","*.kicad_pro","*.kicad_dru","*-lib-table"):
        for path in source.parent.glob(pattern):
            dest=project/path.name
            if path.name.endswith("-lib-table"):
                dest.write_text(path.read_text().replace("${KIPRJMOD}",source.parent.as_posix()))
            else:shutil.copyfile(path,dest)
    shutil.copyfile(source,candidate)
    board=r.p.LoadBoard(str(candidate));r.apply_project_netclasses(board,source.with_suffix(".kicad_pro"))
    baseline=geometry_without_fill(board,set());report=add_stitches(board)
    board.BuildConnectivity()
    if not r.p.ZONE_FILLER(board).Fill(board.Zones()):raise RuntimeError("Candidate plane refill failed")
    if geometry_without_fill(board,set(report["added_uuids"]))!=baseline:
        raise RuntimeError("Plane refill changed existing geometry beyond fill polygons")
    if not r.p.SaveBoard(str(candidate),board,True):raise RuntimeError("Candidate save failed")
    drc=native_drc(candidate,output/"native_drc.json")
    loaded=r.p.LoadBoard(str(candidate))
    assert geometry_without_fill(loaded,set(report["added_uuids"]))==baseline
    spec=json.loads((r.RUNS/"trial01/handoff.json").read_text())["reviewed_locked_seed"]
    k.verify(loaded,spec)
    if r.sha(source)!=source_sha:raise RuntimeError("Source changed during copy review")
    report.update(source=str(source),source_sha256=source_sha,candidate=str(candidate),candidate_sha256=r.sha(candidate),
                  native_drc=drc,source_unchanged=True,planes_refilled=True,tool_sha256=r.sha(__file__))
    r.save_json(output/"validation.json",report)
    print(json.dumps(report,indent=2))


if __name__=="__main__":main()
