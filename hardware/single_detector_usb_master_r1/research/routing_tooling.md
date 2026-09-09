# Guarded native routing handoff

Status: tooling prepared and exercised on a disposable two-pad fixture. The real candidate PCB has not been exported to DSN, routed, or imported by this task. Electrical, placement, and routing-rule review must finish before these commands are used on it.

`tools/route_board.py` provides separate `export`, `run`, and `import` commands. The source is always the native project PCB. Export writes both untouched native DSN and a derived DSN with four physical copper layers: F.Cu and B.Cu active for signal tracks, In1.Cu and In2.Cu typed as power and explicitly inactive for autorouting. Through vias are allowed; plane construction and filling are separate later operations.

The wrapper is deliberately limited to the first route of a placement-only board. Existing tracks, vias, and non-rule-area copper zones cause refusal; antenna rule areas remain present. Incremental routing of hand-routed power paths needs a separately reviewed workflow that preserves those routes. There is no override that discards them.

## Runtime and native APIs

The existing other-project runtime is read in place and never modified. `reports/routing_tool_probe.json` records full paths, hashes, and a fresh Java version check:

| Dependency | Verified identity | SHA-256 |
| --- | --- | --- |
| Java | Temurin OpenJDK 25.0.4.1+1-LTS | `5808527e3dfc4eb285c7664ba356bc267055ef906e83ed4660d9f7976da7349b` |
| Freerouting | 2.2.4, build revision `20f1a72e546b9b23c7ba5127086885cfacbdd4be` | `f5ed374182900ccc78e473518bbb9f6b869f4a07159495f663a76f52bb10523b` |

The KiCad 10.0.6 native APIs are `ExportSpecctraDSN(BOARD, path)` and `ImportSpecctraSES(BOARD, path)`. They are available through bundled Python, not the KiCad CLI. The disposable native session import preserved footprint UUIDs, schematic paths, pads/nets, outline, settings, and a four-layer rule area while reducing the two-pad fixture's unrouted count from 1 to 0.

The DSN settings follow the parser in the exact archived Freerouting revision: per-layer `active` settings are accepted inside `autoroute_settings`; power layer types are recognized separately from signal layers. The derivative is structurally parsed and reserialized, preserving other DSN expressions. See [pinned AutorouteSettings.java](https://raw.githubusercontent.com/freerouting/freerouting/20f1a72e546b9b23c7ba5127086885cfacbdd4be/src/main/java/app/freerouting/io/specctra/parser/AutorouteSettings.java) and [pinned Structure.java](https://raw.githubusercontent.com/freerouting/freerouting/20f1a72e546b9b23c7ba5127086885cfacbdd4be/src/main/java/app/freerouting/io/specctra/parser/Structure.java).

The CLI invocation follows the related project's already used `scripts/main_autoroute.py`, read without modification. It sets `--gui.enabled=false`, `--api_server.enabled=false`, `-da`, `-mp`, and `-mt 1`, with a new project-owned `--user_data_path=` for every run. It does not reuse the other project's profile. Windows launches use `CREATE_NO_WINDOW`. A hard subprocess timeout terminates an unfinished run and prevents its import; command, log, dependency hashes, DSN/SES hashes, duration, and result are recorded.

## Netclasses and import guards

Explicit project PCB netclasses, including Default, are required before export. Routing with implicit default track widths is not a reviewed routing specification. In this standalone runtime `SETTINGS_MANAGER.LoadProject` returned false in a fixture, so the wrapper explicitly mirrors the project's class widths, clearances, via sizes, differential-pair sizes, priorities, and netclass patterns through the tested `NET_SETTINGS` API. The fixture's custom 0.75 mm class was present in native DSN as a 750 micrometre class rule. Legacy nonempty `netclass_assignments` are refused until a suitable loader is reviewed. Project JSON is never rewritten. Custom `.kicad_dru` constraints are hash guarded but their complete transfer to DSN is not claimed; native DRC remains necessary.

The handoff hashes the PCB, schematic hierarchy, project/rule files, library tables, local symbol/footprint libraries, XML netlist, and design/placement manifests. Changes require a new export. Import requires a completed router result and matching SES/DSN hashes, then creates and verifies a byte-identical PCB backup before invoking the native importer in memory.

Every native non-routing expression is compared before/after import and after candidate save/reload. This includes footprint UUID paths, footprint/pad placement, pads/nets, outline, rule areas, text and board settings. In-memory netclasses are compared and their project bytes are guarded. Any internal-layer signal track or blind/buried via is rejected. The source is replaced atomically only after these checks and a final source-hash check. Unrouted counts before/after are reported; an increase is rejected. The result is explicitly `IMPORTED_NOT_DRC_QUALIFIED`.

## Commands after the electrical and placement review

Run from this new project's root. Use a fresh run identifier for every export attempt. The export and run steps do not modify the native PCB; the separate import command replaces it only after the verified backup and checks.

```powershell
& 'C:/Program Files/KiCad/10.0/bin/python.exe' tools/route_board.py export candidate_01
& 'C:/Program Files/KiCad/10.0/bin/python.exe' tools/route_board.py run candidate_01 --passes 8 --timeout 600
& 'C:/Program Files/KiCad/10.0/bin/python.exe' tools/route_board.py import candidate_01
```

Output is under `reports/routing/candidate_01/`. Review `router.log`, `router_run.json`, and the SES outcome before importing. After import, inspect traces and return paths, construct the separate reference-plane copper, refill, and run native KiCad DRC with schematic parity. Routing completion alone does not qualify analog layout, ground-domain separation, RF clearance, manufacturing rules, or mechanical fit.

## Reproducible fixture validation

```powershell
& 'C:/Program Files/KiCad/10.0/bin/python.exe' tools/check_routing_tool.py
```

Thirteen checks pass in `reports/routing_tool_probe.json`, including a full guarded export/import using a hand-authored disposable SES, verified backup, changed-source refusal, native identity preservation, explicit project-class DSN transfer, and rejection of inner-layer signal tracks. No external autorouter ran during these tests. Actual Freerouting routing behavior on this design remains for the later approved routing step; no real-board DRC success is claimed.
