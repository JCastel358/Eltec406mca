"""Bounded native-copy/real-router Kelvin preservation probe; main PCB untouched.

The four-component routing fixture deliberately omits unrelated component
obstacles. Its imported copy is evidence of seed behavior, never a board for
fabrication. Native DRC is run on the full placed board plus the seed only.
Run with KiCad's Python; creates a fresh reports/kelvin_probe_<timestamp> tree.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import route_board as r
import protected_kelvin as k


def rejects(callback, label):
    try:
        callback()
    except (RuntimeError, ValueError):
        return {"case": label, "rejected": True}
    raise AssertionError("Unsafe case was accepted: " + label)


def main():
    folder = r.ROOT / "reports" / ("kelvin_probe_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    folder.mkdir()
    source = r.input_hashes()
    b = r.p.LoadBoard(str(r.BOARD))
    r.assert_unrouted_four_layers(b)
    r.apply_project_netclasses(b)
    original_identity = r.identity(b)
    spec = k.manifest(b, r.sha(r.BOARD))
    k.seed_board(b, spec)
    assert r.identity(b) == original_identity
    cases = [rejects(lambda: r.assert_unrouted_four_layers(b), "default export refuses existing seed")]
    r.save_json(folder / "reviewed_seed.json", spec)
    preview = folder / "seeded_copy.kicad_pcb"
    assert r.p.SaveBoard(str(preview), b, True)

    def fresh():
        return r.p.LoadBoard(str(preview))

    def add_track(board, start, end):
        t = r.p.PCB_TRACK(board)
        t.SetStart(r.p.VECTOR2I(*start)); t.SetEnd(r.p.VECTOR2I(*end))
        t.SetWidth(200000); t.SetLayer(r.p.F_Cu)
        t.SetNetCode(k.pad(board, "U10", "12").GetNetCode()); board.Add(t)

    for name, start, end in (
        ("local OUTS to OUT13 short", spec["track"]["start_nm"], k.xy(k.pad(b,"U10","13").GetPosition())),
        ("intermediate sense branch", [36500000,-7510000], [36500000,-10000000]),
    ):
        altered = fresh(); add_track(altered, start, end)
        cases.append(rejects(lambda: k.verify(altered,spec), name))
    altered=fresh(); v=r.p.PCB_VIA(altered)
    v.SetPosition(r.p.VECTOR2I(36500000,-7510000)); v.SetWidth(600000);v.SetDrill(300000)
    v.SetLayerPair(r.p.F_Cu,r.p.B_Cu);v.SetNetCode(k.pad(altered,"U10","12").GetNetCode());altered.Add(v)
    cases.append(rejects(lambda:k.verify(altered,spec), "intermediate via"))
    for name, mutation in (("unlocked seed", lambda t:t.SetLocked(False)),
                           ("changed seed width",lambda t:t.SetWidth(250000)),
                           ("changed seed endpoint",lambda t:t.SetEnd(r.p.VECTOR2I(38500000,-7500000)))):
        altered=fresh();mutation(list(altered.GetTracks())[0])
        cases.append(rejects(lambda:k.verify(altered,spec),name))
    allowed=fresh();add_track(allowed,spec["track"]["end_nm"],[40000000,-7525000]);k.verify(allowed,spec)

    raw=folder/"native.dsn";assert r.p.ExportSpecctraDSN(b,str(raw))
    tree=r.parse(r.constrain_dsn(raw.read_text(encoding="utf-8"),spec))
    st=r.children(tree,"structure")[0]
    ai=st.index(r.children(st,"autoroute_settings")[0])
    assert all(ai<st.index(item) for item in r.children(st,"keepout"))
    (folder/"full_guarded.dsn").write_text(r.emit(tree)+"\n",encoding="utf-8")
    # Use native real pads/placements. Only the unrelated routing obstacles
    # are removed; do not introduce net aliases or virtual component pins.
    tree[1]="kelvin_fixture";keep={"U10","C11","C13","C39"}
    placement=r.children(tree,"placement")[0]
    for c in list(r.children(placement,"component")):
        for item in list(r.children(c,"place")):
            if item[1].strip('"') not in keep:c.remove(item)
        if not r.children(c,"place"):placement.remove(c)
    network=r.children(tree,"network")[0]
    for n in list(r.children(network,"net")):
        pins=r.children(n,"pins")[0]
        pins[1:]=[pin for pin in pins[1:] if pin.strip('"').rsplit('-',1)[0] in keep]
        if len(pins)==1:network.remove(n)
    names={n[1] for n in r.children(network,"net")}
    for c in r.children(network,"class"):
        c[2:]=[x for x in c[2:] if isinstance(x,list) or x in names]
    dsn=folder/"fixture.dsn";dsn.write_text(r.emit(tree)+"\n",encoding="utf-8")
    ses=folder/"fixture.ses";profile=folder/"router_profile";profile.mkdir()
    runtime=r.runtime_evidence()
    command=[str(r.JAVA),"-jar",str(r.JAR),"-de",str(dsn),"-do",str(ses),
             "--gui.enabled=false","--api_server.enabled=false",f"--user_data_path={profile}",
             "-da","-mp","2","-mt","1","--logging.console.level=INFO"]
    with (folder/"router.log").open("w",encoding="utf-8") as log:
        proc=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,
                              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        try:code=proc.wait(timeout=45)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait();raise RuntimeError("Bounded fixture routing timed out")
    assert code==0
    session=r.session_evidence(ses,1)
    imported=fresh();r.apply_project_netclasses(imported)
    assert r.p.ImportSpecctraSES(imported,str(ses))
    post=k.verify(imported,spec)
    assert r.identity(imported)==original_identity
    layers=r.outer_tracks_only(imported)
    candidate=folder/"fixture_import_NOT_FOR_FABRICATION.kicad_pcb"
    assert r.p.SaveBoard(str(candidate),imported,True)
    k.verify(r.p.LoadBoard(str(candidate)),spec)

    for name, content in (("empty SES",""),("non-session output","(pcb fake)"),
                           ("no wiring SES","(session fake (routes (network_out)))")):
        invalid=folder/(name.replace(" ","_")+".ses");invalid.write_text(content,encoding="utf-8")
        cases.append(rejects(lambda:r.session_evidence(invalid,1),name))
    changed={"source_hashes":dict(source)}
    changed["source_hashes"][str(r.BOARD.relative_to(r.ROOT))]="changed"
    cases.append(rejects(lambda:r.assert_source_unchanged(changed),"source hash mismatch"))

    # Full-board native geometry check of the seed, with real project settings
    # and library definitions copied to a self-contained project-shaped tree.
    project=folder/"project";project.mkdir()
    native=project/r.BOARD.name;shutil.copyfile(preview,native)
    for path in [*r.PROJECT.glob("*.kicad_pro"),*r.PROJECT.glob("*.kicad_dru"),r.PROJECT/"fp-lib-table"]:
        shutil.copyfile(path,project/path.name)
    shutil.copytree(r.ROOT/"libraries",folder/"libraries")
    drc=folder/"seed_native_drc.json"
    cli=Path(sys.executable).parent/"kicad-cli.exe"
    proc=subprocess.run([str(cli),"pcb","drc","--format","json","--severity-all","--all-track-errors",
                         "-o",str(drc),str(native)],capture_output=True,text=True,timeout=45,
                        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    assert proc.returncode==0,proc.stdout+proc.stderr
    report=json.loads(drc.read_text(encoding="utf-8"))
    violations=report.get("violations",[])
    assert not violations,violations
    assert r.input_hashes()==source,"Main source changed during probe"
    result={"status":"COPY_PROBE_PASSED_NOT_A_FULL_BOARD_ROUTE","source_hashes":source,
            "main_board_unchanged":True,"runtime":runtime,"seed":post,"session":session,
            "fixture_routing":layers,"native_seed_drc_violations":len(violations),
            "native_seed_unconnected_items":len(report.get("unconnected_items",[])),
            "negative_cases":cases,"accepted_join_only_inside_capacitor_pad":True,
            "nonrouting_native_identity_retained":True,
            "limitations":"Four-component router fixture excludes other placement obstacles; its import is not a usable full-board route. Full-board DRC was seed-only. No copper planes filled."}
    r.save_json(folder/"result.json",result)
    print(json.dumps({"folder":str(folder),**result},indent=2))


if __name__=="__main__":main()
