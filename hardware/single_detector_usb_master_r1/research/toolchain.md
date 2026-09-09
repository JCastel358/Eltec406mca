# Installed KiCad toolchain and automation capability

Verified 2026-09-09 using the actual installed executables and bundled libraries. This report establishes tool capability; it is not an ERC/DRC or release report for the replacement circuit.

## Reproduce

From this project directory in PowerShell:

```powershell
& 'C:\Program Files\KiCad\10.0\bin\python.exe' .\tools\probe_kicad.py
```

The script writes `reports/toolchain_probe.json`, batches independent CLI help requests, and runs native probes on disposable temporary boards. It never saves the original board, installs software, or runs an external autorouter. Its exit code is nonzero if a native functional probe fails. Full command arguments, outputs, capability signatures, original hash-before/hash-after, router hashes, and scope limits are recorded in JSON.

## Verified results

| Capability | Evidence/result |
|---|---|
| Installed KiCad CLI | `C:\Program Files\KiCad\10.0\bin\kicad-cli.exe`, version **10.0.6** |
| Bundled board scripting | `C:\Program Files\KiCad\10.0\bin\python.exe`, Python **3.11.5**, native `pcbnew` import |
| Standard footprint libraries | `C:\Program Files\KiCad\10.0\share\kicad\footprints`; `FootprintLoad` loaded standard 0805 footprints |
| Original board read | `LoadBoard` succeeded; SHA256 identical before/after; original UUID and schematic paths available |
| Native board save/reopen | `SaveBoard` followed by `LoadBoard` preserved two fixture footprint UUIDs and schematic paths; generated board format **20260206**, generator version **10.0** |
| Schematic relationship | `FOOTPRINT.SetPath(pcbnew.KIID_PATH('/sheet-uuid/symbol-uuid'))` round-tripped; `FindFootprintByPath` found the expected footprint |
| Netclasses | `board.GetDesignSettings().m_NetSettings.SetNetclass` and `SetNetclassPatternAssignment`, followed by `SynchronizeNetsAndNetClasses(False)`, applied 0.75 mm width / 0.25 mm clearance to a named fixture net |
| Connectivity | `BuildConnectivity`; `GetConnectivity().GetUnconnectedCount(False)` detected one fixture airwire, then zero after an explicit test connection |
| Copper fill | `ZONE_FILLER(board).Fill(board.Zones())` returned true and produced nonzero filled area on a temporary fixture |
| Specctra export | `ExportSpecctraDSN(board, filename)` successfully produced a nonempty DSN containing the fixture net |
| Specctra import | `ImportSpecctraSES(board, filename)` exists with native documentation. Not executed: no authentic routed session was supplied to this probe |
| CLI discovery | All 16 requested CLI help commands returned exit code 0 |

Seven native probes passed. Native scripting is an editing mechanism, not a substitute for the final independent CLI checks or visual review.

### KiCad 10 API details that matter

- `GetUnconnectedCount` requires its boolean argument (`False` includes all unconnected items). Omitting it raises `TypeError` in this installation.
- `GetAllNetClasses()` may expose a mixture of Python `str` and SWIG `wxString` keys. Convert names to `str` before sorting or serializing.
- A valid footprint schematic path includes the schematic hierarchy/root UUID and symbol-instance UUID. A matching reference alone does not prove parity.
- The netclass assignment probe proves in-memory behavior. Persist netclasses/patterns in the project `.kicad_pro` and confirm their effective rules in final CLI validation; merely saving the board is not evidence that project settings were persisted.
- No importable `eeschema` Python editor was found. Schematic authoring must use the supported editable file format with correct embedded/project symbols and UUID relationships, then reopen/check/export through KiCad.

## CLI checking and exports

This installed CLI supports:

```powershell
& 'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe' sch erc --format json --severity-all --exit-code-violations --output reports/erc.json DESIGN.kicad_sch
& 'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe' pcb drc --format json --severity-all --schematic-parity --refill-zones --save-board --exit-code-violations --output reports/drc.json DESIGN.kicad_pcb
```

These are checked command forms, not a claim that the new design has passed. `--save-board` must accompany `--refill-zones`. Keep the design schematic/project names associated with the board so the parity check can find the correct source. Preserve and explain warning/exclusion records rather than hiding them.

Available schematic export commands include native netlist, BOM, PDF, and SVG. Available PCB exports include Gerbers, drill, component positions, PDF, SVG, STEP, IPC-D-356, IPC-2581, and ODB++. The CLI does **not** list DSN export or SES import; use the observed native `pcbnew` Specctra APIs for those operations. The CLI does not automatically design a schematic or route a PCB.

## Existing external-router runtime

Java and Freerouting are absent from PATH, but a related user project contains them at:

`C:\Users\JoseCastelblanco\Documents\Eltec_50Piece_board\hardware\ir_array_rev_b\tools`

Read-only evidence:

- `java/jdk-25.0.4.1+1-jre/bin/java.exe -version` returned **OpenJDK Temurin 25.0.4.1+1-LTS**, exit code 0.
- `freerouting-2.2.4.jar` exists, SHA256 `f5ed374182900ccc78e473518bbb9f6b869f4a07159495f663a76f52bb10523b`; the `GlobalSettings.class` UTF-8 constant pool contains version `2.2.4`. Manifest build revision is `20f1a72e546b9b23c7ba5127086885cfacbdd4be`.
- `freerouting-2.1.0.jar` exists, SHA256 `2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def`; the corresponding class contains version `2.1.0`.
- JAR metadata was read without executing either router. The manifest itself says `Implementation-Version: unspecified`, so the filenames alone were not used as version proof.
- Neither the other project nor its runtime/configuration was modified. No router was installed or launched. A production reproduction workflow must explicitly record or package its chosen router/runtime dependency instead of silently relying on this other project's location.

## Remaining validation scope

Run actual-board electrical/geometry checks after authoring and routing. This probe does not establish actual-board ERC/DRC/parity, routing completeness, allowed analog/power clearances, correct component mapping, manufactured-fit geometry, or final assembly polarity. SES import and preservation of protected routes still require a real routing-session round trip before relying on that workflow.
