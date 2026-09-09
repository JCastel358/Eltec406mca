# SPI power-domain interface

Engineering candidate, 2026-09-09. This stage is integrated in the editable schematic. It is not a qualification of the complete power sequence or unknown module tolerances.

Three **TI SN74LVC2G125DCTR** dual buffers implement the five SPI/DRDY paths. U50/U51 outputs are supplied by carrier `ADC_IO_3V3`; U52 outputs are supplied by `ESP_3V3`. Both use the common ground. The exact DCT pin map is 1=1OE,2=1A,3=2Y,4=GND,5=2A,6=1Y,7=2OE,8=VCC. One unused channel is disabled with its input grounded and output unconnected. The part specifies Ioff up to10uA at VCC=0. Its similar quad name SN74LVC125A does not establish this capability. [TI SN74LVC2G125 data sheet](https://www.ti.com/lit/ds/symlink/sn74lvc2g125.pdf)

Separate default-high OE nets prevent unintended connection between the local supply rails. Q50/Q51 pull the respective enables low only when the power stage produces `SPI_PERMIT`. Each NPN has a10k base resistor and100k base-to-ground resistor; each collector has a10k pull-up to its own buffer supply. onsemi MMBT3904LT1G uses pin1base,2emitter,3collector. [onsemi data sheet](https://www.onsemi.com/download/data-sheet/pdf/mmbt3904lt1-d.pdf)

Every receiver has a10k default resistor; CS goes to the receiving3.3V rail and other paths to ground. The expected worst specified10uA off-state output leakage through10.1k raises a low-default node by0.101V. This is a calculated isolated-path bound, not a measurement of aggregate board leakage. A33ohm series resistor at each buffer output provides initial edge damping. Verify the actual module's DVDD, propagation/load margin, power ramps and signals with a scope before release.

The DCT footprint is generated from TI's current package land drawing, separately from generic SM8 names. Candidate source identities are TI C206035 and onsemi C81464; these do not reserve JLC assembly stock. Module/socket pin relationships and buffer paths are checked independently against KiCad's exported XML by `tools/check_interfaces.py`.
