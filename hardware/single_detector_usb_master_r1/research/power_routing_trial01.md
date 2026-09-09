# Trial01 power routing review and additive repairs

This review inspected the immutable original import candidate with SHA256 `b8aba916f5bf17d38e75646580d3358245f5783a342c14b3a16379d83a4af60e`, then tested the two emitter repairs against the integrated plane/stitch source `b33a97f84bf1f496620f39c85c54ef9b8a860b96ba0045ccac9a89994dcc416f`. It does not certify a later board revision without checking its recorded hashes and copper.

## Regulator output and grounding

The locked0.20mm U10.12-to-C11.1 sense trace survived full routing unchanged. The router connected U10.14 to C11+ independently through0.40mm power copper: (35.45,-8.50) -> (36.2017,-8.50) -> (36.2892,-8.4125) -> (38.50,-8.4125) -> (38.50,-7.525). The same power branch serves C13 and the bulk reservoir. The sense trace joins this network at the C11 positive pad, as required.

U10.13 remained open. `tools/add_reviewed_power_stubs.py` adds only the0.25mm vertical connection from OUT13(35.45,-8.00) to OUT14(35.45,-8.50). The in-memory helper verifies exact physical pads/net, unchanged native non-routing identity, exactly one additional route and one closed open, and the strict Kelvin corridor. The CLI writes only a new copy. Copy testing reduced12opens to11; the combined U10 repair geometry had no clearance/width errors. Its final integration and native DRC are separately recorded by the parent.

The original route left GND10/11 open and connected EP15 through narrow0.30mm tracks without local thermal vias. The source agent owns the subsequent direct GND10/11-to-EP stubs, external-pad thermal vias and continuous GND planes. The parent reported all ground opens closed in the plane/stitch source above. Do not add the earlier four-via exploratory pattern on top of that integrated solution. The two-agent division avoids duplicate thermal features.

C11/C13/C39/C36 must have short local returns into that plane system. The original unfilled routing used long0.30mm ground detours; those tracks alone were not an adequate basis for a low-inductance regulator loop or the narrow voltage margin. Check actual stitched/filled copper, rather than interpreting the original autorouter connectivity as a completed power layout.

## SET feedback

The actual F.Cu SET route from U10.9 to R10.1 is about2.54mm. C37 is the nearest bypass by routed path, about4.73mm from SET9; the branch continuing to C15 is about9.58mm despite C15's shorter straight-line placement distance. C38 joins the local SET bus near C37. These are routing observations, not additional component changes. All are on the same high-impedance SET node; the quiet ground plane and separation from fast control traces remain relevant. The1mm battery-input trace on B.Cu passes beneath part of this area, making the intervening continuous GND planes useful.

The static regulator upper-headroom and transient-qualification limits in `power_placement_review.md` remain. This routing work does not establish actual ADC-module AVDD overshoot, inductor DCR, startup current or full-temperature performance.

## Emitter master repairs

`tools/add_power_repairs.py` adds only the two missing emitter connections and four outside-pad0.60/0.30mm through vias. Its integration function is `add_repairs(board)`; the CLI writes a new copy and refuses the main/source PCB. Exact pad positions, existing gate junction, routing-item additions, native non-routing identity, two closed opens and Kelvin protection are checked.

- EM_MASTER_GATE uses a short F.Cu stub from the existing C27/R24 gate junction(89.3061,-18.1311) to a via at(89.2,-17.5), a0.25mm B.Cu trace to(89.0625,-24.1), and a short F.Cu stub to Q20.1(89.0625,-22.95).
- EM_BAT_FUSED connects F2.2(90.4,-16) to a via at(90.4,-14.6), then a0.4mm B.Cu neck to(89.8,-14.6),1mm B.Cu copper through(88.8,-14.6) and(83.9375,-19.4625) to a via at(83.9375,-22.2), then a0.4mm short F.Cu neck to Q19.3(83.9375,-21).

The narrow initial fused-via approach is intentional: the existing1mm EM_BAT_SW trunk atx91.4632 would violate clearance to a1mm rounded endpoint atx90.4. The0.4mm neck is only0.6mm long; the bulk of the fused route remains1mm wide. No existing tracks were moved or trimmed.

The passing test used the exact integrated plane/stitch source hash above. After native zone refill, `reports/routing/trial01/emitter_repair_probe_v2/refilled_drc.json` reports **0 violations / 3 unconnected items**, compared with5before the two repairs. The remaining opens belong to OUT13 and the two signal repairs handled separately. Before refill, the newly added vias correctly conflicted with stale inner-plane fill; the helper therefore requires refill and DRC after integration. The report `emitter_repairs_v2_COPY.repairs.json` links source/output/tool hashes and all added items.

## Native import amendment

The full SES import originally changed only R52's placement from(64.666667,97.666667) to(64.6667,97.6667)mm. `tools/import_trial01_roundoff.py` permitted exactly that two-coordinate33nm quantization, restored the original native position, and required complete strict identity plus unchanged routing expressions and connectivity before returning to the untouched original importer. All original hash, backup, Kelvin, layer, class, connectivity and save/reload guards then passed. `import.json` links the amendment report and tool by SHA256. No general placement tolerance was introduced.
