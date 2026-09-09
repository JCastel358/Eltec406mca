# Native validation and regeneration

Use the installed KiCad 10.0.6 bundled Python for tools importing `pcbnew`.
Commands below assume PowerShell in this project directory. Native KiCad files
are the editable deliverable; the scripts record how the candidate was built
and checked. Re-running an autorouter need not reproduce identical copper.

```powershell
$kicadPython = 'C:/Program Files/KiCad/10.0/bin/python.exe'
& $kicadPython tools/validate_schematic.py --no-build
```

`--no-build` checks the current native schematic, project and libraries. It
exports a fresh XML netlist, runs all configured ERC severities, then checks the
interface, analog and power pin maps. The report records hashes and conditional
calculation inputs. It does not establish those physical input bounds.

The delivered candidate has an imported routing session and completed ground copper:

```powershell
& $kicadPython tools/validate_board.py --routing-run trial01
& $kicadPython tools/export_assembly.py
& $kicadPython tools/export_fabrication.py
```

Replace `trial01` only with the recorded import used for the current board. The
board validator refills and saves zones, reopens the board, runs native DRC and
schematic parity, and independently rechecks the protected regulator sense
branch. It requires zero violations, unconnected edges and parity differences,
four copper layers, outer-layer signal tracks and through vias. Unknown ignored
checks or configured exclusions fail validation. Inspect `PLAN.md` and the
hash-bound reports for the exact checked revision.

Assembly export checks the exact fitted references, native coordinates,
schematic paths and manufacturer identities. It includes the THT sockets and
trimmer. Remaining catalog IDs and all unverified vendor rotations are reported,
not silently substituted. A BOM/CPL export is not fabrication qualification.

The separate firmware workflow and its actual compile/protocol evidence are in
`firmware/README.md`. Do not replace the original firmware file to reproduce it.

## Regeneration from engineering generators

Use a separate working copy when exploring new circuit or placement revisions.
The default schematic rebuild restores generated symbols and copied footprint
library content; it can overwrite reviewed library silkscreen refinements.
The placement builder deliberately refuses to overwrite routed copper without
its explicit override and a verified backup.

For a reviewed circuit change, build and validate the schematic, configure its
new netclasses, then build placement. Clean and integrate the silkscreen only
after placement passes. Set the fabrication origin to native (12,115) before
routing; newly generated boards already use it. Recheck the schematic with
`--no-build` before routing so the router's netlist/source hashes are stable.

The actual route handoff uses:

```powershell
& $kicadPython tools/route_board.py export NEW_RUN --reviewed-kelvin-seed
& $kicadPython tools/route_board.py run NEW_RUN --passes 8 --timeout 600
& $kicadPython tools/route_board.py import NEW_RUN
```

Each run name is new. The initial-route wrapper requires the unrouted,
four-layer placement and adds only the explicit reviewed U10.12-to-C11.1 locked
sense trace in memory. It rejects empty router sessions, changed source inputs,
altered native identities and branching into the protected sense corridor.
Source PCB replacement occurs only after native import/reopen checks and a
verified backup. An imported route remains unqualified until DRC and copper
review finish.

`add_reference_planes.py --build-copy` creates a review copy with separate ground
domains, thermal connections and local ground stitches. It preserves existing
native geometry and checks against the actual routes. A conflict or missing
connection requires engineering repair, not relaxed clearance rules. Review and
verify a candidate before replacing the main PCB; then refill and run final
native checks. Silkscreen repair also operates on a copy and preserves copper.

The local router runtime is read from the separate `Eltec_50Piece_board` project;
its exact paths and hashes are recorded in every routing run. The other project
is not modified. Another workstation must provide the same verified dependencies
or deliberately requalify its toolchain; the editable KiCad design itself does
not depend on running that router.

Historical trial01 import used `import_trial01_roundoff.py` to restore exactly
one 33 nm coordinate rounding change. Subsequent explicit repairs and integration
reports preserve their input/output hashes. They are revision-specific historical
operations, not commands to rerun over the completed board. The current native
board plus `validate_board.py --routing-run trial01` is the validation entry point.

For new review PDFs, use a new output folder name:

```powershell
& $kicadPython tools/export_board_review.py NEW_REVIEW --phase review_candidate
```

Every PDF page must be rendered and visually inspected before delivery. The
export tool records pending visual review and does not invent that approval.

`exports/fabrication_draft/README.md` describes the guarded draft fabrication
export. No command here places an order, uploads a board or establishes physical
fit, measured sequencing, analog performance or production release.
