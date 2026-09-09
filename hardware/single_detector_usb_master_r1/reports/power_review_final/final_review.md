# Final power copper review

Reviewed native board SHA-256 `2ab4937f2ec558227736722549489c8d60b382db2a86436887f104acd336f4e2`. This is the integrated, refilled candidate recorded in `reports/final_ground_integration/integration.json`. The read-only reproducible audit is `tools/audit_power_routing.py`; its machine-readable result is `final_review.json` beside this file. The source hash remained unchanged throughout the audit.

**Result: no concrete power-copper defect was found in this bounded review.** All 20 reviewed output, emitter-master and bypass additions retain their exact native routing geometry. All 11 original local ground stitches, the two U10 ground-pin bridges, three additional input/output bypass stitches, and the final C12/C16 stitches are present. Every checked ground via contacts its intended actual filled net on both inner layers. The filled detector and emitter ground domains remain separate. The integration's native DRC reports **0 violations, 0 unconnected items and 0 schematic-parity issues**; the independent connectivity check also reports zero unconnected items.

## Regulator and returns

The locked 0.20 mm F.Cu Kelvin trace from U10 OUTS12 to C11 positive remains exact. The protected corridor contains no additional same-net or foreign conductor. Output power reaches C11 through the independently routed OUT14 branch; the reviewed 0.25 mm OUT13-to-OUT14 stub closes the second physical output pin without joining the sense trace beside the IC.

U10 ground pins 10/11 connect directly to its exposed pad. Two outside-pad 0.60/0.30 mm thermal vias at (34, -9.45) and (34, -4.55) connect through 1.0 mm F.Cu stubs into the filled GND planes. The ordinary through vias are outside the soldered exposed pad; the existing divided paste aperture is preserved. This provides the reviewed electrical and thermal path, while actual junction temperature remains a bench qualification.

| Return | Reviewed final copper |
|---|---|
| C11 output anchor | Ground pad to local via (38.5, -11.675), 1.2 mm long and 0.60 mm wide. |
| C13/C39 output ceramics | Each has its reviewed 1.2 mm, 0.60 mm local ground stub and contact to both inner GND fills. |
| C36 input bypass | Ground pad to local via (29.5, -10.675), 1.2 mm long and 0.60 mm wide. |
| C15/C37/C38 SET capacitors | Each has its local 0.30 mm ground stub to a via at y = 2.325 mm. R39 has a separate local ground-plane connection. |
| C12 reservoir | New GND via (26.475, -20.05), reached directly from C12.2 by a 1.95 mm, 1.0 mm F.Cu stub. This removes reliance on the previous approximately 15 mm narrow surface return. |
| C16 comparator bypass | New GND via (64.5, 38.5), reached directly from C16.2 by a 1.235 mm, 0.30 mm F.Cu stub. The existing direct U18–C16 supply and ground routes remain intact. |

The comparator already had a direct local bypass loop: approximately 1.714 mm from U18 VCC to C16 positive and 3.659 mm from U18 GND to C16 negative. The added C16 stitch improves the connection of that loop to its reference planes; the earlier distant via was not evidence of an electrically absent bypass. C12/C16 additions do not move components or alter the original routed conductors.

The SET node remains compact and separated from emitter ground. The actual SET9-to-R10 route is approximately 2.54 mm; C37 is reached in approximately 4.73 mm, with C15 on the continuing branch. These routed lengths, rather than only pad-to-pad distances, were considered. No further copper edit is requested by this review.

## Remaining qualification limits

This review closes the identified routing and local-return work. It does **not** establish a production-qualified measurement instrument.

- The regulator's accepted static upper corner is approximately 5.24536 V, leaving about **4.64 mV** below the ADS1256's 5.25 V operating maximum. That is a useful measured-prototype target, but insufficient evidence of guaranteed load-release or module-filter transient compliance. The nominal setting has more headroom; an actual unit's position within the tolerance range must be measured.
- Measure actual module AVDD relative to its AGND, including startup, conversion changes, PDWN, abort, USB removal/reapplication and low-battery retries. Header voltage alone does not characterize the module's internal inductor/filter. Peak voltage plus instrument uncertainty must remain within the specified operating range while conversions are accepted. The undervoltage permission circuit does not detect upper-rail excursions.
- Supply operating limits and absolute-maximum protection are separate: exceeding 5.25 V briefly is a measurement-specification failure even if the ADS1256's 6 V AVDD absolute maximum is not exceeded. AIN-to-AVDD shutdown limits require their own measured checks. DRC does not prove these temporal electrical conditions.
- Verify reservoir effective capacitance/ESR, assembled held-rail load and transient step, actual module filter/DCR, analog grounding timing, regulator startup/inrush and repeated-cycle temperature with the real batteries and leads. Existing limits in the electrical report remain conditional acceptance targets where manufacturer or module evidence is incomplete.
- Qualify noise, offset, repeatability and emitter/sensor operation on the assembled rig. Low-battery hardware retry can occur; the revised firmware stops a test on a power fault and does not automatically resume emission after recovery.

Manufacturer limits and the underlying corner reasoning are recorded in `research/power_architecture.md` and `research/power_placement_review.md`. This final copper review introduces no new component selection or expanded electrical guarantee.
