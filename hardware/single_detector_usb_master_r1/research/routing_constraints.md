# Candidate routing constraints

The preliminary board is 86 x 145 mm, four copper layers, with the original
connector positions. It has not been routed or released. `configure_routing.py`
writes explicit netclasses and native custom rules; `route_board.py` verifies
their effective native/DSN interpretation. Geometry follows the current
[JLC capabilities](https://jlcpcb.com/capabilities/pcb-capabilities); preferred
widths are engineering choices and require actual path review.

| Item | Candidate setting |
|---|---:|
| General copper clearance / minimum signal width | 0.20 / 0.20 mm |
| Through via diameter / drill | 0.60 / 0.30 mm |
| Copper-to-routed-edge clearance | 0.50 mm |
| Analog / ordinary signal preferred width | 0.25 mm |
| Local supply preferred width | 0.40 mm |
| Battery/emitter preferred width | 1.0 mm; short pad neckdowns require review |
| Visible silk text height / stroke / pad clearance | 1.0 / 0.15 / 0.15 mm |

Only F.Cu and B.Cu carry signal tracks. In1.Cu and In2.Cu are reserved for ground
reference/thermal copper. The emitter ground remains a different net from the
detector/USB ground. Local emitter copper areas must not interrupt the analog or
SPI return paths. The source-derived ESP antenna corridor excludes copper and
new components on all layers; actual module/vendor RF and enclosure qualification
remain open.

Critical local layout must be checked before accepting a route: LT3041 output
ceramics at its output, OUTS Kelvin connection at their positive terminals,
SET return at their ground, input bypass nearby; amplifier feedback and bypass
short; comparator divider/reference close; emitter regulator thermal copper and
high-current returns away from sensitive inputs. Automatic connectivity alone
does not prove these conditions.

Netclass widths are preferences, not automatic minimum-width constraints;
explicit native rules provide the required floors. See the
[KiCad 10 PCB manual](https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html).

Initial native rule parsing on the placed candidate reported only silk/text
violations, expected unrouted connections, and zero schematic parity issues.
Final validation must use the routed, refilled, saved board with no unresolved
electrical errors or unexplained exclusions.

A copy-only native probe also enabled the four normally ignored applicable
checks: missing courtyards, off-center track/via junctions, symbol footprint
filter mismatches and footprint/pad technology mismatches. The placed candidate
still had zero violations, 462 expected unconnected edges and zero parity issues.
The configuration helper now enables those checks explicitly. The unused
length-tuning-profile geometry check retains KiCad's default ignored status;
this design uses no length-tuning profiles. Final validation rejects other
ignored checks, configured exclusions, warnings and errors.
