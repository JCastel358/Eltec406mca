"""Add only the reviewed U10 OUT13-to-OUT14 stub, on an explicit output copy.

The parent may call add_out_stub(board) during its guarded integration. This
CLI refuses the source/main PCB as an output; it never adds GND/thermal vias.
Refill affected planes and run native DRC after integration.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import uuid
import route_board as r
import protected_kelvin as k

STUB_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL,"eltec-single-master-r1-U10-OUT13-OUT14-reviewed-v1"))
START=[35450000,-8000000]
END=[35450000,-8500000]
WIDTH=250000


def add_out_stub(board):
    handoff=json.loads((r.RUNS/"trial01/handoff.json").read_text(encoding="utf-8"))
    spec=handoff["reviewed_locked_seed"]
    k.verify(board,spec)
    a,z=k.pad(board,"U10","13"),k.pad(board,"U10","14")
    if k.xy(a.GetPosition())!=START or k.xy(z.GetPosition())!=END:
        raise RuntimeError("U10 OUT pad positions changed; review this stub again")
    if any(x.GetNetname()!="ADC_5V_HELD" or not x.IsOnLayer(r.p.F_Cu) for x in (a,z)):
        raise RuntimeError("U10 OUT net or physical side changed")
    if any(t.m_Uuid.AsString()==STUB_UUID for t in board.GetTracks()):
        raise RuntimeError("Reviewed output stub already exists; refusing a duplicate")
    before_identity=r.identity(board)
    before=r.connectivity(board)
    previous={t.m_Uuid.AsString() for t in board.GetTracks()}
    t=r.p.PCB_TRACK(board);t.SetUuid(r.p.KIID(STUB_UUID))
    t.SetStart(r.p.VECTOR2I(*START));t.SetEnd(r.p.VECTOR2I(*END))
    t.SetWidth(WIDTH);t.SetLayer(r.p.F_Cu);t.SetNetCode(a.GetNetCode());t.SetLocked(True)
    board.Add(t)
    check=k.verify(board,spec)
    if r.identity(board)!=before_identity:
        raise RuntimeError("Stub insertion changed native non-routing identity")
    if {x.m_Uuid.AsString() for x in board.GetTracks()}!=previous|{STUB_UUID}:
        raise RuntimeError("Unexpected routing additions/removals")
    after=r.connectivity(board)
    if after!=before-1:
        raise RuntimeError(f"Reviewed stub did not close exactly one open: {before} -> {after}")
    return {"status":"STUB_ADDED_IN_MEMORY_NOT_DRC_QUALIFIED",
            "stub":k.track_signature(t),"unconnected_before":before,"unconnected_after":after,
            "kelvin":check,"native_nonrouting_identity_unchanged":True,
            "ground_or_thermal_changes":False,
            "required_followup":"Refill planes if present and run native DRC against the integrated board."}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source",type=Path,default=r.BOARD)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    src,dst=args.source.resolve(),args.output.resolve()
    if dst==src or dst==r.BOARD.resolve() or dst.exists():
        raise RuntimeError("Output must be a new review copy, never the source/main PCB")
    source_hash=r.sha(src)
    b=r.p.LoadBoard(str(src));result=add_out_stub(b)
    dst.parent.mkdir(parents=True,exist_ok=True)
    if not r.p.SaveBoard(str(dst),b,True):raise RuntimeError("Native copy save failed")
    loaded=r.p.LoadBoard(str(dst))
    h=json.loads((r.RUNS/"trial01/handoff.json").read_text(encoding="utf-8"))
    k.verify(loaded,h["reviewed_locked_seed"])
    if r.sha(src)!=source_hash:raise RuntimeError("Source changed during copy operation")
    result.update(source=str(src),source_sha256=source_hash,output=str(dst),output_sha256=r.sha(dst),
                  tool_sha256=r.sha(__file__),source_unchanged=True)
    r.save_json(dst.with_suffix(".stub.json"),result)
    print(json.dumps(result,indent=2))


if __name__=="__main__":main()
