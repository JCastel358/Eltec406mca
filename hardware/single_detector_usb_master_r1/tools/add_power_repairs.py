"""Two additive reviewed emitter-master repairs; copy-only CLI.

Call add_repairs(board) for guarded integration. Uses only outer-layer tracks
and four 0.60/0.30mm through vias, outside component pads. No existing copper,
schematic, placement or ground-plane changes are made.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import uuid
import route_board as r
import protected_kelvin as k

PADS={"C27.1":[87000000,-20725000],"Q20.1":[89062500,-22950000],
      "F2.2":[90400000,-16000000],"Q19.3":[83937500,-21000000]}


def point(a):return r.p.VECTOR2I(*(round(x*1e6) for x in a))
def uid(name):return r.p.KIID(str(uuid.uuid5(uuid.NAMESPACE_URL,"eltec-single-master-r1-emitter-repair-v1-"+name)))


def add_repairs(board):
    for name,expected in PADS.items():
        ref,num=name.split(".");a=k.pad(board,ref,num)
        net="EM_MASTER_GATE" if name in ("C27.1","Q20.1") else "EM_BAT_FUSED"
        if k.xy(a.GetPosition())!=expected or a.GetNetname()!=net or not a.IsOnLayer(r.p.F_Cu):
            raise RuntimeError("Reviewed emitter repair endpoint changed: "+name)
    # The short C27-side gate via stub joins a verified existing gate junction.
    join=point((89.3061,-18.1311))
    if not any(not isinstance(t,r.p.PCB_VIA) and t.GetNetname()=="EM_MASTER_GATE"
               and t.GetLayer()==r.p.F_Cu and (t.GetStart()==join or t.GetEnd()==join)
               for t in board.GetTracks()):
        raise RuntimeError("Existing C27-to-R24 gate junction changed")
    original=r.identity(board);before=r.connectivity(board)
    originals={t.m_Uuid.AsString() for t in board.GetTracks()};added=[]
    def track(name,net,start,end,width,layer):
        identity=uid(name)
        if identity.AsString() in originals:raise RuntimeError("Repair item already exists")
        t=r.p.PCB_TRACK(board);t.SetUuid(identity);t.SetStart(point(start));t.SetEnd(point(end))
        t.SetWidth(r.p.FromMM(width));t.SetLayer(layer);t.SetNetCode(board.FindNet(net).GetNetCode());t.SetLocked(True)
        board.Add(t);added.append({"kind":"track","uuid":identity.AsString(),"net":net,"start_mm":start,"end_mm":end,
                                  "width_mm":width,"layer":board.GetLayerName(layer)})
    def via(name,net,at):
        identity=uid(name)
        if identity.AsString() in originals:raise RuntimeError("Repair item already exists")
        t=r.p.PCB_VIA(board);t.SetUuid(identity);t.SetPosition(point(at));t.SetWidth(r.p.FromMM(.6));t.SetDrill(r.p.FromMM(.3))
        t.SetLayerPair(r.p.F_Cu,r.p.B_Cu);t.SetNetCode(board.FindNet(net).GetNetCode());t.SetLocked(True)
        board.Add(t);added.append({"kind":"via","uuid":identity.AsString(),"net":net,"at_mm":at,"diameter_mm":.6,"drill_mm":.3})
    net="EM_MASTER_GATE"
    via("gate-c27-via",net,(89.2,-17.5));via("gate-q20-via",net,(89.0625,-24.1))
    track("gate-c27-stub",net,(89.3061,-18.1311),(89.2,-17.5),.25,r.p.F_Cu)
    track("gate-q20-stub",net,(89.0625,-22.95),(89.0625,-24.1),.25,r.p.F_Cu)
    track("gate-back",net,(89.2,-17.5),(89.0625,-24.1),.25,r.p.B_Cu)
    net="EM_BAT_FUSED"
    via("fused-f2-via",net,(90.4,-14.6));via("fused-q19-via",net,(83.9375,-22.2))
    track("fused-f2-stub",net,(90.4,-16),(90.4,-14.6),1.0,r.p.F_Cu)
    track("fused-q19-neck",net,(83.9375,-21),(83.9375,-22.2),.4,r.p.F_Cu)
    # The existing 1mm EM_BAT_SW trunk at x91.4632 leaves only0.2632mm
    # clearance to the new0.6mm via. Keep its first0.6mm trace neck narrow;
    # a1mm rounded endpoint here would violate clearance to that trunk.
    track("fused-f2-back-neck",net,(90.4,-14.6),(89.8,-14.6),.4,r.p.B_Cu)
    for i,(a,z) in enumerate(zip([(89.8,-14.6),(88.8,-14.6),(83.9375,-19.4625)],
                                 [(88.8,-14.6),(83.9375,-19.4625),(83.9375,-22.2)])):
        track("fused-back-"+str(i),net,a,z,1.0,r.p.B_Cu)
    if r.identity(board)!=original:raise RuntimeError("Emitter repair changed native nonrouting identity")
    if {t.m_Uuid.AsString() for t in board.GetTracks()} != originals|{x["uuid"] for x in added}:
        raise RuntimeError("Unexpected routing changes")
    after=r.connectivity(board)
    if after!=before-2:raise RuntimeError(f"Emitter repairs must close exactly two opens: {before}->{after}")
    h=json.loads((r.RUNS/"trial01/handoff.json").read_text(encoding="utf-8"))
    kelvin=k.verify(board,h["reviewed_locked_seed"])
    return {"status":"TWO_POWER_OPENS_REPAIRED_IN_MEMORY_NOT_DRC_QUALIFIED","added":added,
            "unconnected_before":before,"unconnected_after":after,"kelvin":kelvin,
            "native_nonrouting_identity_preserved":True,"existing_copper_not_modified":True,
            "required_followup":"Refill zones if present and run native DRC after integration."}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source",type=Path,default=r.BOARD);ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args();src,dst=args.source.resolve(),args.output.resolve()
    if dst==src or dst==r.BOARD.resolve() or dst.exists():raise RuntimeError("Use a new copy path, never main/source PCB")
    source_sha=r.sha(src);board=r.p.LoadBoard(str(src));result=add_repairs(board)
    dst.parent.mkdir(parents=True,exist_ok=True)
    if not r.p.SaveBoard(str(dst),board,True):raise RuntimeError("Native output copy save failed")
    if r.sha(src)!=source_sha:raise RuntimeError("Source changed during copy operation")
    result.update(source=str(src),source_sha256=source_sha,output=str(dst),output_sha256=r.sha(dst),tool_sha256=r.sha(__file__))
    r.save_json(dst.with_suffix(".repairs.json"),result);print(json.dumps(result,indent=2))


if __name__=="__main__":main()
