# Protected LT3041 OUTS routing

The reviewed mechanism keeps U10.12 (OUTS) separate from load-current copper until C11.1, despite the schematic deliberately assigning OUTS and OUT to the same `ADC_5V_HELD` net. No component, pin or schematic net was invented or changed.

The native seed is one locked, 0.20 mm F.Cu track from (35.45, -7.50) to (38.50, -7.525) mm. Its length is 3.0501 mm. This equals the project's current 0.20 mm minimum width. The complete placed board plus this seed passed native KiCad DRC with **0 violations / 461 unconnected items**, compared with 462 unconnected before seeding. No source-board copper was changed during the probe.

## Explicit workflow

Use KiCad's bundled Python with `tools/route_board.py`:

```
route_board.py export <new-run-name> --reviewed-kelvin-seed
route_board.py run <new-run-name> --passes 8 --timeout 600
route_board.py import <new-run-name>
```

The explicit export flag supports only `LT3041_U10_12_TO_C11_1_V1`. It constructs the reviewed seed in memory from the actual pads, checks the project minimum width, saves a native preview and records its geometry/UUID in the handoff. The main PCB remains untouched until a successful guarded import. Without the flag, the original placement-only workflow remains; both modes refuse pre-existing source tracks or copper zones. This is not a general incremental-routing permission.

Before the actual export, the parent sets the native drill/place origin to (12,115) mm and finalizes project rule severities. The handoff records this origin. Hashes cover the board, native schematic/project/rules/libraries, interface and placement manifests, and the routing/seed helper source. Any change after export requires a new export. A run also records the handoff hash; the import checks it, the DSN and the SES before loading copper. Native non-routing identity and layer/netclass guards remain active.

## Obstacle and independent verification

KiCad 10.0.6 exports the locked seed as `(type fix)`. The DSN adds one **netless** F.Cu obstacle spanning native x=35.00..37.40 mm and y=-7.725..-7.275 mm. It covers U10.12 and the intermediate sense trace, and terminates 0.25 mm inside C11's positive pad. New current-carrying copper can meet the sense connection only at the capacitor pad beyond the protected corridor.

The obstacle intentionally overlaps the already fixed trace. It is a routing instruction in the DSN, not a fabricated native copper feature or a same-net native keepout that could invalidate the seed. In the pinned router, a netless obstacle shares no net with an incoming trace or via, so the common `ADC_5V_HELD` name gives no exemption. See the exact [ObstacleArea implementation](https://github.com/freerouting/freerouting/blob/20f1a72e546b9b23c7ba5127086885cfacbdd4be/src/main/java/app/freerouting/board/ObstacleArea.java).

The importer separately checks the native result. It requires the exact seed UUID, endpoints, width, net, layer and locked state, and rejects extra F.Cu traces/arcs, vias, foreign pads or copper drawings in the protected rectangle. Its conservative geometric test applies regardless of net. It also refuses an overlapping F.Cu copper zone. The same checks run again after save/reload. Re-run `protected_kelvin.verify(board, handoff["reviewed_locked_seed"])` after later manual copper/plane edits; those later edits cannot rely on ordinary same-net DRC alone. Inner/B.Cu planes do not create a galvanic branch without a via, but their analog coupling and return continuity still require review.

## Router parser correction

The pinned Freerouting 2.2.4 revision only consumes `autoroute_settings` while its layer structure is still uninitialized. Appending that block after KiCad's existing antenna keepout produced an empty internal board, a zero exit status and a zero-byte SES. The wrapper now places settings immediately after the four layer declarations and before any keepout. This behavior follows the exact [Structure parser, lines866–870](https://github.com/freerouting/freerouting/blob/20f1a72e546b9b23c7ba5127086885cfacbdd4be/src/main/java/app/freerouting/io/specctra/parser/Structure.java#L866), and was reproduced with native versus modified DSN copies.

Zero-byte/non-session outputs and sessions without wire paths despite expected connections are now rejected. A native import must also reduce the remaining unconnected count. Exit code zero by itself is not routing evidence.

## Probe evidence and limits

The reproducible helper `tools/probe_kelvin_seed.py` creates a fresh report folder, never writes the main PCB, and calls the pinned local router with GUI/API disabled. The passing evidence is `reports/kelvin_probe_20260909T150447231174Z/result.json`:

- Two-pass real routing of the four-component U10/C11/C13/C39 fixture produced a 5013-byte session, 15 routed wire paths and 24 imported F.Cu segments with no vias or inner-layer routes.
- The locked seed retained exact native identity and geometry after import and save/reload. All non-routing native content remained identical.
- Eleven negative checks rejected a local U10.12→U10.13 branch, an intermediate branch, an intermediate via, unlocked/changed seed geometry, empty/non-session/no-wiring output, source-hash mismatch and default export of existing copper.
- A branch at the intended C11 positive pad was accepted. Native DRC of the full placed board with only the seed gave zero violations.

The four-component fixture omits unrelated component obstacles to bound the capability test. Its imported copy is explicitly **not for fabrication** and is not evidence that the complete board is routed or DRC-clean. Full-board routing, plane construction, Kelvin ground returns, exposed-pad thermal connections, regulator/comparator power loops and analog separation remain to be reviewed on the actual final copper. The narrow regulator upper-voltage margin remains a measured prototype qualification issue described in `power_placement_review.md`; locking OUTS does not certify transient AVDD compliance.
