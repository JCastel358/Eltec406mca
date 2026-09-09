# Assembly strategy and sourcing evidence

Date checked: 2026-09-09. Status: preliminary engineering research, not a quote, approved BOM, or fabrication release. Only public read-only sources were consulted; nothing was uploaded, purchased, reserved, or sent to a manufacturer.

## Population boundary

The user's required assembly boundary is all board components populated except the ESP32 development module, the ADS1256 module, and the two barrel jacks. Treat **module sockets as populated components separate from the omitted modules**. The native source audit established that the detector/emitter interfaces are copper wire solder lands, not purchased connectors. Preserve these lands and the existing harness; no fictitious connector belongs in the assembly BOM. Do not silently widen the customer soldering list to every through-hole part.

Recommended project representation:

| Item | Board representation | Assembly treatment |
| --- | --- | --- |
| ESP32 development board | Separate documentation/physical envelope plus two electrically named female socket strips | Socket strips populated; ESP32 module customer supplied and inserted |
| ADS1256 breakout | Separate documentation/physical envelope plus individually identified socket headers matching its actual pins | Sockets populated; module customer supplied and inserted |
| Two barrel jacks | Electrical symbols and exact footprints once their mechanical drawing is confirmed | Excluded from assembly BOM/CPL; listed in customer installation BOM |
| Detector/reference and emitter connections | Original three/two-wire copper solder lands with native pin coordinates | Copper features; excluded from purchased BOM/CPL |
| Sequencer, protection, regulators, passives, trim control, indicators | Ordinary electrical symbols and exact sourced footprints | Populated |

An existing combined module footprint does not by itself procure or locate separate socket strips correctly. The editable design and exported BOM/CPL must identify each physically assembled connector. Likewise, a symbol's generic `Barrel_Jack` value is insufficient to select a drill pattern.

## Viable service and layout strategy

JLCPCB currently lists mixed SMT and through-hole assembly under **both Economic and Standard PCBA**. Economic permits placement on one side; Standard permits both sides. Thus THT sockets do not by themselves require Standard. A top-side component layout with through-hole female sockets and all semiconductors/passives in SMD is a viable starting point. The current Economic capability table lists 2/4/6 layers, 0.8-1.6 mm thickness, minimum 0402 packages and 0.4 mm IC lead pitch. These are service limits rather than recommended routing dimensions. [JLCPCB PCBA capabilities](https://jlcpcb.com/capabilities/pcb-assembly-capabilities)

Parts explicitly marked **Wave Soldering** in JLCPCB's library are covered by its through-hole/manual assembly flow. Check this per actual manufacturer part number. Some connector pages additionally require an assembly support fixture. That is a quoting/manufacturing consideration, not a reason to mark the connector unpopulated. [JLCPCB assembly FAQ](https://jlcpcb.com/help/article/pcb-assembly-faqs)

Use 0603 or 0805 passives as electrical/space needs warrant; avoiding 0402 here improves visual inspection and later rework. Prefer SOIC/TSSOP/SOT packages for newly selected analog and sequencing parts where the required electrical behavior exists. This is a design recommendation, not a restriction on using a necessary leadless component. Do not downgrade shutdown protection, precision, voltage rating, dissipation, or capacitor effective value to reduce feeder fees.

Keep the socket bodies and future module envelopes clear of the rework access area around the sequencer and input protection. Place assembly references, pin-1 markers, battery polarity, and module insertion orientation where installed modules will not hide every marking. Treat ESP32 antenna clearance, USB plug access, and the ADS1256 module underside component height as mechanical dependencies.

## Catalog classes and procurement interpretation

JLCPCB distinguishes Basic, Preferred Extended, and Extended parts. Its FAQ says Preferred Extended parts are exempt from Economic feeder loading charges. The current price policy charges feeder setup for Extended parts on Economic, and for both Basic and Extended parts on Standard. Part classes change; record the checked date and confirm them when obtaining the eventual quote. Do not embed a fixed assembly price in the release package. [JLCPCB assembly FAQ](https://jlcpcb.com/help/article/pcb-assembly-faqs), [JLCPCB assembly price policy](https://jlcpcb.com/help/article/pcb-assembly-price)

An LCSC catalog entry establishes a concrete component identity and potential supply route, **not** a confirmed JLCPCB assembly match or available allocation. Final sourcing needs the actual MPN, package, JLC part number, approved supply source, stock for the chosen build quantity plus attrition, and assembly type. JLC requires sourced/consigned parts to be received and selected before assembly. No supply source or quantity has been reserved for this project. [JLCPCB component matching guidelines](https://jlcpcb.com/help/article/component-matching-guidelines-for-pcba-orders)

## Existing-circuit candidate evidence

These entries help preserve the original circuitry while selecting assembled packages. They are not electrical approval of the old design or automatic substitution authority. The original schematic PNG identifies OPA2196 buffers, AO3400A emitter FETs, an LTV-847 optocoupler and an adjustable AMS1117 regulator.

| Circuit function | Concrete candidate | What is verified | What remains |
| --- | --- | --- | --- |
| Existing emitter low-side FET | Alpha & Omega AO3400A, **C20917**, SOT-23 | JLC part page lists **Basic**, SMT Assembly, Economic and Standard. Manufacturer datasheet available. | Confirm the chosen gate drive, current, dissipation and pin mapping in the new circuit. It is not a direct positive-lead switch with a USB-only gate drive. |
| Existing four-channel optocoupler | Lite-On **LTV-847**, **C10794**, DIP-16 | JLC page lists **Extended**, Wave Soldering, Economic and Standard. This is a real Lite-On component entry. | Electrical design must check CTR at actual LED current and required off-state behavior; confirm exact DIP lead span/footprint. |
| Existing dual precision buffers | TI **OPA2196IDR**, **C2057972**, SOIC-8 | LCSC identifies this MPN/package. TI lists it active, SOIC D package, 8 pins, 4.5-36 V operating supply. | JLC assembly availability/class was not verified. Keep this sourcing item open; do not substitute a similarly named op amp without analog review. |
| Existing emitter regulator | Exact manufacturer and suffix of AMS1117 adjustable part unresolved | The PNG is not sufficient to distinguish compatible manufacturer/package/capacitor requirements. | Select a datasheet-backed part and check dropout, minimum load, capacitor stability, reverse bias and thermal dissipation under the new two-6.5 V-battery conditions. |

Evidence links: [AO3400A JLC entry](https://jlcpcb.com/partdetail/Alpha_OmegaSemicon-AO3400A/C20917), [AO3400A manufacturer datasheet](https://www.aosmd.com/res/data_sheets/AO3400A.pdf), [LTV-847 JLC entry](https://jlcpcb.com/partdetail/LiteOn-LTV847/C10794), [Lite-On datasheet hosted by LCSC](https://datasheet.lcsc.com/lcsc/1810161110_Lite-On-LTV-847_C10794.pdf), [OPA2196IDR LCSC entry](https://www.lcsc.com/product-detail/operational-amplifier_texas-instruments-opa2196idr_C2057972.html), [TI OPA2196IDR package and status](https://www.ti.com/product/OPA2196/part-details/OPA2196IDR).

**Sourcing trap:** the search result `LTV-847S / C9900002565 / Manufacturer: JLCPCB Assembly` is an assembly-service entry. It does not identify a sourced Lite-On chip. Do not use it as a purchased semiconductor MPN. [JLC assembly-service listing](https://jlcpcb.com/partdetail/JIALICHUANGSMT-LTV847S/C9900002565)

New power-path and sequencing components require their own sourcing check after the electrical architecture is chosen. Large TO-220 IRLB8748 devices are not required by the user's new-board request; their earlier mention concerned an interim perfboard retrofit.

## Socket candidates and mechanical limits

The repository wiring note identifies a 30-pin DOIT ESP32 DevKit V1, but that does not prove its row spacing or USB connector placement. The selected socket for each 15-position row is **Kinghelm KH-2.54FH-1X15P-H8.5, C2932676**. Its manufacturer drawing specifies 2.54 mm pitch, 8.5 mm insulator height, a recommended 1.02 mm PCB hole, 35.56 mm first-to-last pin distance and 38.10 mm overall row length. JLC's exact part page confirms Extended, Wave Soldering, and Economic/Standard assembly. This closes the catalog/process identity for J3/J4, not their physical fit or allocation. [Kinghelm drawing](https://www.kinghelm.net/upload/file/20221115/KH-2.54FH-1X15P-H8.5.pdf), [JLC assembly entry](https://jlcpcb.com/partdetail/3278197-KH_2_54FH_1X15P_H85/C2932676)

J5 uses **Kinghelm KH-2.54FH-1X8P-H8.5, C2905417**. Its exact JLC page likewise confirms Extended, Wave Soldering, and Economic/Standard. A search snapshot included an assembly-fixture notice that was absent from the subsequently opened live page; fixture requirements and cost remain quote-dependent. J6 uses **FG-PM2.54-2-08P-H8.5, C25687193**, a 2×8, 2.54 mm, 8.5 mm-high female header with an LCSC-hosted manufacturer drawing. A JLC assembly match for J6 was not verified. All four sockets remain populated in the assembly boundary. [J5 JLC entry](https://jlcpcb.com/partdetail/3175191-KH_2_54FH_1X8P_H85/C2905417), [J6 LCSC entry](https://www.lcsc.com/product-detail/C25687193.html), [FG manufacturer drawing hosted by LCSC](https://lcsc.com/datasheet/lcsc_datasheet_2407051524_FG-FG-PM2-54-2-08P-H8-5_C25687193.pdf)

Another manufacturer-identified 1x15 option is **Ckmtw B-2200S15P-A120, C124408**. The LCSC entry identifies 2.54 mm pitch and a through-hole 15-position single row. It is a candidate, not a footprint-equivalent replacement until the drawing is checked. [Ckmtw/LCSC entry](https://www.lcsc.com/product-detail/C124408.html)

A concrete example proving the assembly route is **XYECONN PM2.54-1-16P-Z, C53244566**: its JLC entry explicitly lists Wave Soldering, Economic and Standard, and an assembly fixture. It has 16 positions and is **not** a substitute for a 15-pin module row. Its public page showed zero stock and a preorder requirement when checked, so it should not be treated as immediately available. [JLC connector entry](https://jlcpcb.com/partdetail/XYECONN-PM2_54_1_16PZ/C53244566)

The native source and module evidence, rather than the schematic symbol pin ordering alone, govern the ADS1256 socket geometry. The selected socket identities above do not replace the integration team's mechanical checks against the original footprints and actual module.

Detector/reference and emitter copper-land positions and numbering are preserved from the native board audit. They are not purchased screw terminals. Any future connector addition would require a separate drawing and harness compatibility check.

## Export contract

Generate a complete engineering BOM and a separately filtered **JLC assembly BOM**. The assembly BOM should use at least `Comment,Designator,Footprint,LCSC Part #`, plus manufacturer/MPN fields in the engineering BOM. Use exactly one unambiguous part/value/package per line and list all fitted reference designators for that part. Keep the module and barrel-jack omissions in a customer-install list, with their exact footprints and orientation in the assembly drawing. [JLC BOM requirements](https://jlcpcb.com/help/article/bill-of-materials-for-pcb-assembly), [JLC KiCad BOM/CPL guidance](https://jlcpcb.com/help/article/how-to-generate-bom-and-centroid-files-from-kicad-8)

The CPL must include each populated SMT **and THT** component, with `Designator,Mid X,Mid Y,Layer,Rotation`, millimeter coordinates, and counterclockwise-positive degrees. A footprint origin at connector pin 1 is not necessarily its body centroid. Exporter logic must use the intended placement point rather than blindly assuming every origin is already a centroid. Record any part-specific rotation/offset correction and compare it against a top-side assembly view. [JLC pick-and-place requirements](https://jlcpcb.com/help/article/pick-place-file-for-pcb-assembly)

Automated checks should prove that the BOM designator set equals the CPL set, both equal the populated footprint set, no reference appears twice, all coordinates are finite millimeter values within the design envelope, and every omitted item is justified by the user-requested assembly boundary. JLC recognizes assembly parts only when designators are present consistently in both files. Do not let an unmatched socket silently disappear. [JLC BOM/CPL preparation recommendations](https://jlcpcb.com/help/article/advice-for-bom-and-cpl-files-preparation)

Draft fabrication deliverables should include the exact-revision Gerbers, plated/non-plated drill data, board drawing, assembly views with polarity/pin-1 marking, BOM/CPL and omitted-part list, plus reproducible source-to-export checks. Include top/bottom solder mask and paste layers as applicable. A future online placement preview/quote is additional manufacturing evidence; no such online review occurred during this research.

## Material open items for the project decision log

1. Confirm the exact removable ESP32 and ADS1256 module geometry and mating pin lengths from supplied files; keep the initial mechanical model preliminary where uncertain.
2. Confirm the two user-installed barrel-jack MPNs and physical pin polarity. They are omitted from assembly but must still fit the fabricated board.
3. Preserve detector/emitter wire-land polarity and verify existing harness/case-ground connections. No additional connector is selected by this revision.
4. Complete JLC assembly matching for OPA2325IDR and all selected sequencing/protection parts. LCSC identity alone does not close this item.
5. Confirm economic versus standard eligibility after all parts, sides, board geometry and assembly fixture requirements are known. Do not infer the service from through-hole presence alone.
6. Recheck part class and availability against the eventual build quantity before any separately authorized order. Current research provides no reservation or delivery guarantee.

No electrical timing, fit, solderability of an unselected connector, PCB DRC/ERC result, or production qualification is claimed by this note.

## Updated analog sourcing

The revised held-rail driver is OPA2325IDR, not the original OPA2196. LCSC's product metadata identifies [OPA2325IDR/C2058909](https://www.lcsc.com/product-detail/C2058909.html), SOIC-8. This is a procurement identity, not an allocated assembly order. The similarly retrieved C2877688 belongs to a single OPA325 and must not be used for these dual packages.

The precision 33 kΩ resistors are [Yageo RT0805BRD0733KL/C728650](https://www.lcsc.com/product-detail/Chip-Resistor-Surface-Mount_YAGEO-RT0805BRD0733KL_C728650.html), 0805, 0.1%, 25 ppm/°C. The exact 499 Ω counterpart is [RT0805BRD07499RL/C865521](https://www.lcsc.com/product-detail/Pre-ordered-RLCs_YAGEO_C865521.html), also 0805, 0.1%, 25 ppm/°C. These are verified identities; displayed stock varied between product and related-item snapshots, so no available quantity is asserted.

**ADG4613 remains the material sourcing gap.** Analog Devices lists ADG4613BRUZ and ADG4613BRUZ-REEL7 in the same 16-lead TSSOP RU-16 package. No exact LCSC/JLC part number was verified after bounded searches and direct public-page checks. A cached JLC DG412LEDQ page mentions ADG4613BRUZ-REEL7 as a related item but does not expose its ID; the page's **C3008913 belongs to Vishay DG412LEDQ-T1-GE3**, not ADG4613. Similarly, C207399 and C657391 belong to ADG4612, whose control truth table differs. Leave the selected ADG4613 assembly ID blank until a manufacturer- and package-matched JLC sourcing or consignment path is confirmed. Do not call the current BOM fully sourceable. [ADI product models](https://www.analog.com/en/products/adg4613.html), [ADI ordering table](https://www.analog.com/media/en/technical-documentation/data-sheets/adg4612_4613.pdf), [JLC page with the misleading related-item search result](https://jlcpcb.com/partdetail/VishayIntertech-DG412LEDQ_T1GE3/C3008913)

## Verified catalog data and remaining gaps

`verified_catalog.json` is the machine-readable exact-MPN mapping for the exporter. Each record includes the manufacturer, a verified public source, UTC check time, and assembly status. Catalog identity and JLC assembly allocation are separate. No part in this work has been reserved or approved for a build quantity. The exporter must compare manufacturer as well as MPN and package: several suppliers reuse an identical MPN string.

| Item | Verified identity or remaining issue |
| --- | --- |
| Held regulator | LT3041ADE#TRPBF = C7452883; LCSC identity verified, JLC allocation unconfirmed. |
| Reference/comparator | LM4050AIM3-4.1/NOPB = C2156509; TLV3601DCKR = C2974371. The selected AIM reference has an 85°C upper operating envelope. |
| Reference qualifier | TPS3840PL34DBVR = C2871627. Other selected TPS3840 thresholds need their own exact order codes; a different threshold is not a supply substitution. |
| Low-voltage NMOS | Alpha & Omega AO3414 = **C48850**, verified from current LCSC Product metadata. C557941 is a **VBsemi** device and does not prove the AOS 1.8 V on-resistance/capacitance specifications used in the design. JLC allocation remains unconfirmed. |
| ADC 3.3 V LDO | Diodes AP2112K-3.3TRG1 = C51118. C23380830 is **TECH PUBLIC**, a different manufacturer's device with the same MPN text. |
| Remaining power procurement | Current verified identities are in the machine-readable catalog; remaining exact identity and availability items are listed in the final sourcing section below. An empty ID must not be filled by a similar value alone. |
| Remaining assembly approval | J6, ADG4613, and all LCSC-only parts still need a confirmed JLC supply/process match. Every populated part must survive the assembly BOM/CPL filtering. |

The specific links for verified IDs are in `verified_catalog.json`; the electrical/package evidence remains in the emitter and independent power review files. The master manifest was changing during this sourcing pass, so a hard-coded count of missing passive MPNs would be stale. The final exporter should report unresolved identities directly from the regenerated populated-part list.

## Exact USB switch and battery assumptions

The user's [Amazon ASIN B0FCS47Y8Q](https://www.amazon.com/RIITOP-Type-C-Extension-Support-Charging/dp/B0FCS47Y8Q) identifies manufacturer/model **RIITOP USBCFOF-1FT**. The RIITOP product page's structured variant SKU is the same. RIITOP explicitly advertises USB data, including USB 2.0 backward compatibility, plus video and PD support. It describes the switch's marked side as connecting and its unmarked side as disconnecting the USB device. This supports using a data-capable cable for the serial connection; it is not inferred from a charging-only title. [RIITOP manufacturer description](https://www.riitop.com/products/riitop-usb-type-c-extension-cable-with-on-off-switch-1ft-usb-3-1-type-c-male-to-female-extension-cable-with-on-off-switch-support-video-data-pd-charging)

Neither source supplied a contact schematic or stated exactly whether VBUS, CC, D+/D−, SuperSpeed pairs, or ground are switched. Consequently, **VBUS-only switching and all-conductor switching are both unverified**. Commissioning must verify that the actual cable removes the ESP32's 5 V power and that the serial port reappears reliably with both USB plug orientations used by the rig. RIITOP's page says 20 V/3 A while the Amazon listing advertises up to 20 V/5 A; this discrepancy does not establish the contact behavior. No 100 W claim is needed for this rig.

For the user's HeyFuture label **6.4 V, 6 Ah, 38.4 Wh LiFePO4**, an exact manufacturer model/datasheet was not found. The official HeyFuture product catalog examined during this pass listed seven products, with 12 V batteries and a 24 V 6 Ah battery, but no matching 6.4 V 6 Ah pack. Thus full-charge voltage, charger tolerance, BMS undervoltage cutoff, short-circuit current and cutoff timing remain **unverified for this actual pack**. A third-party mention of a 6 V 6 Ah pack supplied with a 7.4 V charger was not accepted as exact-model evidence. [HeyFuture manufacturer catalog](https://heyfuturepower.com/collections/all)

The 6.4 V label is nominal voltage, not a maximum. Any provisional 7.3 V upper-bound calculation in the circuit notes remains an assumption until the actual pack and charger are identified or measured under the relevant conditions. The detector supply remains the user's battery rail as requested; source uncertainty must not silently change its calibration supply.


## Final bounded SPI and power-passive catalog pass

The selected SPI buffer and enable transistor identities are TI SN74LVC2G125DCTR/C206035 and onsemi MMBT3904LT1G/C81464. Their package maps remain DCT-8 and SOT-23 respectively; a generic quad buffer or another manufacturer's transistor is not selected by this sourcing lookup. All ordinary SPI 10 kΩ, 100 kΩ and 33 Ω resistors now have exact Yageo RC0805FR catalog matches. Integration approved changing only ordinary analog C30–C35 and SPI C50–C52 bypass capacitors to Samsung CL21B104KBCNNNC/C1711, 0805 100 nF/50 V/X7R/10%. Timing, SET and LDO stability capacitors are outside that substitution.

New exact capacitor matches: GRM32ER71E226KE15L/C21397 (1210 22 µF/25 V/X7R/10%); CL21B475KAFNNNE/C98195 (0805 4.7 µF/25 V/X7R/10%); GRM31C5C1H104JA01K/C398933 (1206 100 nF/50 V/C0G/5%); GRM1885C1H103JA01D/C85973 (0603 10 nF/50 V/C0G/5%); CL10B104KB8NNNC/C1591 (0603 100 nF/50 V/X7R/10%). The K suffix of the selected 1206 C0G part is significant for catalog matching; do not attach the older L-suffix C97946 to the K MPN.

F1/F2 catalog identities are Littelfuse 0468001.NRHF/C45157 and 046801.5NRHF/C151143. Their 50 A interrupt rating still has to be reconciled with the actual battery pack's prospective short current; verifying their IDs does not prove that pack-level protection condition. The selected emitter-master optocoupler is now the exact packaged Lite-On LTV-817S-TA1-B/C109226.

The obsolete Diodes BZT52C6V8-7-F is no longer the selected D12. The power owner changed it to Diotec BZT52C6V8GW after manufacturer review. No verified LCSC/JLC identity for that Diotec order code was found in this bounded pass. Do not use the earlier Diodes catalog ID with the Diotec MPN.

Most remaining ordinary power resistors now have verified exact Yageo RC0603FR/JR IDs in the JSON file. Remaining unresolved special identities include ADG4613BRUZ, Diotec BZT52C6V8GW, TPS3840PL42DBVR/PL30DBVR, the two TE 10 ppm SET resistors, Nichicon PCJ0J821MCL4GS, precision 221 Ω, and the emitter 210 Ω/7.5 Ω orders. Recompute the final exact list from the regenerated manifest rather than this dated narrative. These parts are not declared unavailable; this research did not establish their exact JLC/LCSC assembly path.


## Final exact-gap closure and prepared global sourcing

Checked 2026-09-09T14:55:23Z. Three additional exact manufacturer/MPN records are now in `verified_catalog.json`: [TE RN73C1J51K1BTDF/C4226875](https://www.lcsc.com/product-detail/C4226875.html), [Yageo RT0603BRD07221RL/C705749](https://www.lcsc.com/product-detail/C705749.html), and [Nichicon PCJ0J821MCL4GS/C2161767](https://www.lcsc.com/product-detail/C2161767.html). Each was validated against the public page's Product metadata, including MPN, manufacturer and SKU. This brings the independent catalog to 53 exact records. C12's public metadata showed out of stock when checked; its resolved identity is not an availability assurance.

The 195-fitted-component export supplied for this pass had 12 unresolved references. These three matches leave **nine references / seven unique MPNs** without verified JLC/LCSC IDs: D12 BZT52C6V8GW; R39 RN73C1J698RBTDF; R100 RC0805FR-07210RL; R101 RC0805FR-077R5L; U12 TPS3840PL42DBVR; U13 TPS3840PL30DBVR; U30/U31/U32 ADG4613BRUZ. This is a research calculation against the supplied export; regenerate the exporter report to update its stored count.

`global_sourcing_prepared.json` and the matching CSV give each exact MPN, designators, per-board quantity, verified manufacturer/distributor order identity, source links and unresolved fields. They include the seven identity gaps plus C12 as an availability fallback. Build quantity, attrition, purchase quantity, JLC library ID and allocation remain blank/null. No row was submitted, purchased, reserved or approved by JLC.

For ADG4613, the ADI ordering guide places **ADG4613BRUZ** and **ADG4613BRUZ-REEL7** in the same RU-16 TSSOP and temperature grade, so the reel suffix changes procurement packaging without changing the intended electrical/package model. The prepared row favors REEL7 for assembly. Verified distributor identities are **505-ADG4613BRUZ-REEL7TR-ND** (tape/reel) and **ADG4613BRUZ-ND** (tube). The LFCSP BCPZ order has a different footprint; ADG4612 has a different truth table. Neither is an approved substitution. [ADI ordering guide, page 22](https://www.analog.com/media/en/technical-documentation/data-sheets/adg4612_4613.pdf), [DigiKey reel order identity](https://www.digikey.com/en/products/detail/analog-devices-inc/ADG4613BRUZ-REEL7/2506915), [DigiKey tube order identity](https://www.digikey.com/en/products/detail/analog-devices-inc/ADG4613BRUZ/2507326)

JLC's documented Global Sourcing route starts with an exact manufacturer-part-number search, supplier selection and an assembly review; accepted parts become available to assembly only after warehouse receipt. Per-board quantities here are not purchase quantities because build size and process attrition remain unresolved. The service documentation establishes a possible route, not acceptance of these specific parts. No supplier option, lead time or quotation for this board has been confirmed. [JLC Global Sourcing process](https://jlcpcb.com/help/article/how-to-use-jlcpcb-global-sourcing-parts-service)

Two rows have only manufacturer order identity, so their procurement uncertainty is larger: Diotec lists BZT52C6V8GW active with zero stock and minimum order 3,000; TE lists RN73C1J698RBTDF active as internal **9-1676970-4**, but the public product page says it is not currently available. No manufacturer inquiry was sent. These remain unresolved sourcing work, not a completed assembly path. [Diotec exact product](https://diotec.com/en/product/BZT52C6V8GW.html), [TE exact product](https://www.te.com/en/product-9-1676970-4.html)

The exact R101 Yageo RC0805FR-077R5L DigiKey page also showed out of stock at the final check. Its listed order identity remains useful for a sourcing search, but does not establish immediate supply. [DigiKey exact R101](https://www.digikey.com/en/products/detail/yageo/RC0805FR-077R5L/728115)
