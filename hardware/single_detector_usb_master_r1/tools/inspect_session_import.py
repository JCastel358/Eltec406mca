"""Read-only diagnosis of the native session import preservation guard."""
import json
import pcbnew as p
import route_board as r

folder = r.RUNS/'trial01'
record = json.loads((folder/'handoff.json').read_text())
r.assert_source_unchanged(record)
b = p.LoadBoard(str(r.BOARD))
r.apply_project_netclasses(b)
r.apply_recorded_seed(b, record)
before, classes = r.identity(b), r.native_netclasses(b)
assert p.ImportSpecctraSES(b, str(folder/'routed.ses'))
after, new_classes = r.identity(b), r.native_netclasses(b)
diffs = []
def compare(a, z, path='root'):
    if type(a) != type(z):
        diffs.append([path, str(a)[:500], str(z)[:500]])
    elif isinstance(a, list):
        if len(a) != len(z):
            diffs.append([path+'/length', len(a), len(z)])
        for i, (x, y) in enumerate(zip(a,z)):
            compare(x,y,path+'/'+str(i))
    elif isinstance(a, dict):
        for key in sorted(set(a)|set(z)):
            compare(a.get(key),z.get(key),path+'/'+str(key))
    elif a != z:
        diffs.append([path,a,z])
compare(before, after, 'native')
compare(classes,new_classes,'classes')
report = {'same_native':before==after,'same_classes':classes==new_classes,
          'differences':diffs,'unconnected_after':r.connectivity(b)}
(folder/'import_difference.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report,indent=2))
r.assert_source_unchanged(record)
