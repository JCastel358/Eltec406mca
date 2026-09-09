# Exact USB-switch evidence

Checked2026-09-09. The user's [ASINB0FCS47Y8Q listing](https://www.amazon.com/RIITOP-Type-C-Extension-Support-Charging/dp/B0FCS47Y8Q) identifies a one-foot USB-C male-to-female extension with a switch and explicitly advertises data support. The matching [RIITOP manufacturer page](https://www.riitop.com/collections/usb-c-cables-1/products/riitop-usb-type-c-extension-cable-with-on-off-switch-1ft-usb-3-1-type-c-male-to-female-extension-cable-with-on-off-switch-support-video-data-pd-charging) also advertises data operation and connected-device on/off control.

This establishes advertised data capability, so the cable is not identified as charging-only. RIITOP limits the additional male-to-male cable to1.5m for reliable operation. Confirm actual serial enumeration and downstream5V removal with the received cable and ESP32.

Neither inspected page provides the internal contact schematic or proves which of VBUS, CC, data and ground are physically interrupted. Do not claim all conductors are switched or that ground is isolated. The carrier design senses downstream moduleVIN/USB5V and keeps its required detector/USB ground continuous; emitter ground remains optically separated on the board.

The pages disagree about the high-power PD current rating, which is not used by this5V USB-powered ESP32 design. The extension's PD capability does not itself negotiate a higher voltage. No cable internals, loaded voltage drop or switch transients have been measured.
