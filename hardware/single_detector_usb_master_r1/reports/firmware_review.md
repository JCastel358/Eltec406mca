# USB-master firmware independent review

Reviewed 2026-09-09 against the original `Arduino/Eltec/Eltec.ino`, `tools/prepare_firmware.py`, its final derived sketch, and the actual 405 M22, 406MCA and 449 M18 Python backends. All five identified code issues are corrected in this snapshot; no unresolved actionable firmware-code finding remains from this review. Source/generator/sketch files were read only. No serial port was opened and no firmware was regenerated, compiled or flashed by this reviewer. The root task's completed native compilation records were independently inspected below.

Current reviewed hashes:

| Input | SHA-256 |
| --- | --- |
| Original Eltec.ino | `ded93d6270e217e0b35a20470bb8e9490321be4eca5b9ffb6a42077f57c0fb54` |
| prepare_firmware.py | `1b0abbda03dbe4e792bdcce2e8ecdb9ad84b07d36ad7a0fbf584163ddd8b4c10` |
| Eltec_USB_Master.ino | `753437ecdca873d92a8e545318515d49c33812aac60535c2385d4d08cb5bdd0c` |

## Reproducible host-side checks

`tools/check_firmware_protocol.py --require-current-sketch` passes **24 checks, zero failures**. Results and source hashes are in `reports/firmware_protocol_checks.json`.

The script applies the generator's literal replacements in memory without executing its filesystem-writing code. It verifies that the generated sketch is current. It imports each actual app backend, replaces only transport endpoints, and calls its real identity validation, handshake, command parser and stop/drain logic. Tests cover:

- v3.3 identity acceptance by all three backends, both with and without a READY banner.
- Normal READY/IDN handshake and the unavailable-power error followed by IDN.
- Unchanged STATUS wire format and actual command-response acceptance.
- Reproduction of the original END-before-ERR problem: `stop_stream()` accepts END and leaves the following fault unread.
- Corrected ERR-before-END order: `stop_stream()` raises a protocol error before accepting termination.
- Rejection of a fault response to OFFSET.
- Original source, generator and derived sketch remaining byte-identical during the check.

The pass result is a protocol-compatibility result. It is not a simulation of MCU interrupt latency, SPI timing, power sequencing or electrical behavior; code-review dispositions are recorded separately below. The test script never connects serial, regenerates firmware, invokes a compiler or uploads hardware.

## Original findings and present disposition

1. **Brief pulse lost by CHANGE/current-level ISR: corrected.** The final sketch uses RISING at line 824; the ISR at lines 230–233 unconditionally latches `powerFaultSeen` and increments `carrierPowerEpoch`. Its handling of a delivered edge no longer depends on the pin remaining HIGH when the callback runs. Minimum electrically detectable pulse width still requires hardware validation.
2. **Last scalar sample/delay and REF restoration were unguarded: corrected.** The median function now checks readiness after its final SPI transfer, optional delay and sort. REF verifies restoration and readiness, reports failure and marks `adsOk=false` rather than returning a finite result on restoration failure. The final OFFSET response check is also present as documented under F4.
3. **Pending fault could follow a successful STREAM,END: corrected at the software stop boundary.** STREAM,START snapshots a power epoch and refuses a faulted start. Power service retains `streamPowerFault` before clearing streaming. STREAM,STOP captures the sticky fault/epoch/current readiness while interrupts are disabled and sends ERR before END when invalid. The END overrun field also becomes nonzero for an invalid capture. Host reproduction confirms why this order matters.

The stop decision represents the capture's atomic software end boundary. A genuinely later fault after that boundary must not retroactively invalidate earlier complete valid samples. Do not add an unconditional post-END epoch comparison that changes this behavior. Pending hardware interrupt timing and GPIO qualification still need a bench fault-injection check.

## Follow-up findings resolved in the final snapshot

### F4 — OFFSET response boundary: corrected

Final sketch lines 704–710: the handler tests `isnan(v) || powerFaultSeen || !carrierPowerReady()` immediately before selecting its response. It emits `ERR,offset invalid or ADS1256 timeout` on a captured fault instead of transmitting the finite value returned by the helper. This closes the previously identified return-to-response window at the defined response check boundary.

No software check retroactively changes bytes transmitted before a later independent power event. The remaining bench test verifies event/response ordering rather than assuming compilation proves it.

Validation: inject the power ISR immediately after `readMedianVolts()` returns but before the OFFSET handler selects its reply. Expect ERR with no OFFSET value.

### F5 — Fault during final FE register readback: corrected

Final sketch lines 617–626: FE success requires `adsApplyFrontEnd() && !powerFaultSeen && carrierPowerReady()`. A fault during final STATUS/ADCON readback can therefore no longer be acknowledged merely because default-low MISO matches FE,V20's zero register fields.

On failure, software restores the requested previous PGA/buffer state, marks `adsOk=false`, and reports that ADC reinitialization is required. It does not falsely claim that an immediate unverified restoration succeeded. The existing service retries initialization and verification before later live commands are accepted.

Validation: apply FE,V20 and inject a power edge during final STATUS/ADCON readback with returned bytes zero. Expect failure, not `OK,FE,gain=1,buf=0`; verify that subsequent stable-power reinitialization applies a defined configuration before measurements resume.

## Native compile result and warnings

`reports/firmware_validation.json` records successful Arduino CLI compilation (return code 0) for `esp32:esp32:esp32`, Arduino-ESP32 core 3.3.11, with all warnings enabled. Its firmware hash matches this reviewed snapshot. The compile uses 291,568 bytes of program storage and 22,972 bytes of global RAM. `reports/firmware_compile.log` contains exactly two sketch warnings, both `-Wvolatile` deprecations of incrementing a volatile-qualified value:

| Location in final sketch | Operation | Assessment |
| --- | --- | --- |
| line 233, `onCarrierPowerChange()` | `carrierPowerEpoch++` | New fault counter; only this ISR increments it after static initialization. Main code reads/snapshots it and does not write it. |
| line 560, `onAdsDrdyFalling()` | `streamDrdyOverruns++` | Inherited operation from original firmware line 531. Main code resets it inside the interrupt-disabled STREAM,START critical section and snapshots it inside the STOP critical section. |

These are language-deprecation diagnostics, not compilation failures or evidence of a new correctness defect in the present ISR/main-loop design. There is one ISR incrementing each counter, and the foreground reset of the overrun counter excludes that ISR. No competing task or second ISR writer was found. `volatile` does not make an arbitrary read-modify-write atomic or synchronize multiple cores; adding another writer would require appropriate synchronization. Changing the increment syntax solely to suppress this warning would not improve atomicity. The existing target-specific ISR behavior should still be exercised by the bench fault/timing tests.

The native compile passed with these two warnings, the original source remained unchanged, and no hardware upload occurred. Compilation and the 24 protocol checks do not establish physical rig qualification.

## Recovery and compatibility observations

- GPIO35 is input-only and actively driven by an ESP-powered Schmitt NAND status gate; LOW means power ready. The external pull-down bounds power-off leakage (`Ioff`); it is not an external pull-up or the old open-collector-ready arrangement. The final firmware uses plain `INPUT`, with no internal pull, and GPIO35 is not in the allowed emitter-output pin list. Logic operation/readiness during ESP supply ramp still belongs to hardware qualification.
- A captured fault clears streaming and pending sample state, drives PWM/gate off, marks the ADC unavailable, and retains capture invalidity. Streaming and PWM are not automatically restarted after recovery.
- Stable ready power leads to ADC initialization and channel calibration/readback, with retry scheduling. Reinitialization uses the selected `pgaSensor` and `adsBufferOn`, so a previously selected FE,V19 is preserved across a carrier power cycle while the ESP32 remains running.
- The successful IDN reply establishes serial identity, not analog readiness. All three actual backends accept it without READY. Following the first command, READY is suppressed and STATUS has no readiness field. Late battery insertion/recovery therefore requires retrying a live command or an explicit future readiness protocol if the app needs to display that state.
- Existing STATUS formatting is unchanged. Version v3.3 satisfies the current backends' minimum-version checks. The 406MCA backend still performs FE,V19 after connection and FE? verification before measurement.
- `BAT?` now intentionally returns ERR because no battery divider is fitted. Battery monitoring is disabled in all three active GUIs and their calibration helpers, so this does not interrupt their current normal flow. A legacy tool explicitly requesting BAT? will receive the documented error.

## Remaining hardware/firmware validation scenarios

1. Start with batteries unavailable: IDN/STATUS remain accessible, live commands reject, late valid power reinitializes without automatically driving the emitter or restarting capture.
2. Select FE,V19, cycle carrier power while USB remains attached, and verify the selected front end and fresh sensor channel before the next capture.
3. Fault during DRDY wait, streaming SPI read, final scalar SPI read, final scalar delay, REF channel restoration and the two corrected reply boundaries above.
4. Inject a brief delivered HIGH pulse while delaying the ISR until the input has returned LOW; ensure the sticky event/epoch still invalidates the old capture.
5. Fault immediately before and during STREAM,STOP; an invalid capture must produce ERR before END. Separately fault after the completed stop boundary and verify that a previously valid capture is not retroactively changed.
6. Begin a new capture after successful recovery: count/pending sample state must reset and no queued sample from the invalidated capture may be accepted as part of it.

The independent hardware shutdown circuitry remains responsible for electrical isolation and safe power collapse; firmware protection does not replace it.
