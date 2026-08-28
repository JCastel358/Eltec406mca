# How the 405 M22 noise test actually filters

**Scope:** the noise capture and verdict in `tech_app/eltec_rig/` (the unified
Eltec Test Rig app), Model 405 M22 path only.
**Status of this document:** every number below was measured from the shipping
code and the saved captures on 2026-08-26, not copied from design notes.
**Companion figure:** `405M22_noise_filter_chain.png` (same folder).

---

## 0. Executive summary

The 405 M22 noise verdict is **not** taken on the raw 1000 SPS trace. The
capture passes through a three-stage chain before any pass/fail decision:

| # | Stage | What it is | Effect |
|---|---|---|---|
| 1 | Anti-alias decimation | Kaiser windowed-sinc FIR, 621 taps, 20:1 (1000 to 50 SPS) | Low-pass, flat to 22 Hz, >= 60 dB stopband from 28 Hz |
| 2 | Per-window detrend | Least-squares line (mean **and** slope) removed from each 1 s window | High-pass, -3 dB at 0.85 Hz |
| 3 | Windowed pk-pk rule | Twenty 1 s windows vs a 429 uV limit | PASS iff <= 3 windows exceed it |

The net effect is that **verdicts are judged over roughly 0.85-22 Hz**. That
band was never specified as such - it is the incidental product of two stages
chosen for other reasons (instrument-noise reduction, and DC-settling
rejection). Knowing it matters, because the part's own noise lives at
0.5-5 Hz, i.e. partly *underneath* the high-pass corner.

Only the 405 M22 has this test. The 406 MCA path has no emitter-off noise
capture and no filtering at all - its only noise-related gate is
`MIN_SIGNAL_TO_NOISE_RATIO = 1.5` applied to the driven capture.

---

## 1. Where the numbers come from (the limit)

The TP412 spec is **300 mV pk-pk read on the legacy bench scope**, behind the
legacy amplifier chain. This rig measures at the **sensor pin**, so the spec
must be referred back through that chain:

```
NOISE_LEGACY_PP_LIMIT_MV      = 300.0    # TP412, at the legacy scope
NOISE_EFFECTIVE_CHAIN_FACTOR  = 700.0    # measured, same-part cross-check
NOISE_PP_LIMIT_MV             = 300/700  = 0.428571 mV  (~429 uV at the pin)
```

The amplifier's nameplate gain is 4000x (`NOISE_LEGACY_AMPLIFIER_GAIN`), but a
same-part cross-measurement on 2026-08-13 pinned the *effective* end-to-end
display factor at ~620-830x, and 700 was adopted. **This discrepancy is still
unexplained and is the single largest open risk in the whole noise gate** -
see section 7.

---

## 2. The capture (before any filtering)

Emitter OFF, DUT on AIN0, ADS1256 at 1000 SPS, PGA 1, +/-5 V range
(LSB ~= 0.6 uV).

1. **Adaptive quiet wait**, 3 s minimum (`NOISE_WAIT_BEFORE_CAPTURE_S`) to
   20 s maximum (`NOISE_WAIT_MAX_S`). The capture starts once 2 consecutive
   1 s block-mean deltas stay <= 0.107 mV (`NOISE_BASELINE_SETTLE_DELTA_MV`,
   = limit/4). Streamed but discarded. If the level is still moving at 20 s
   the capture starts anyway and the report flags
   `noise_baseline_settled = NO` - **this wait can never fail a part**, it
   only delays the start.
2. **Capture 20 s** (`NOISE_CAPTURE_SECONDS`) = 20,000 raw samples.
   The operator can select a **60 s soak** (`NOISE_SOAK_CAPTURE_SECONDS`)
   per part for suspect or historically noisy units.

The full raw capture is retained in memory so the operator can save it
(`*_noise_raw.npz` / `.csv`) - which is what makes every analysis in this
report reproducible after the fact.

---

## 3. Stage 1 - anti-alias decimation (the low-pass)

**Function:** `stability_analysis.decimate_antialiased(waveform, 20)`
**Replaced:** `decimate_boxcar` (a plain 20-sample moving average), retired
from the verdict on 2026-08-20; see section 6.

### Why decimate at all

At 1000 SPS the ADS1256's own input noise is ~30-50 uV pk-pk per 1 s window -
already a large fraction of the 429 uV budget before the part contributes -
and the raw ~500 Hz bandwidth admits mains and EMI the band-limited legacy
amplifier never saw. Averaging 20:1 drops white instrument noise by sqrt(20)
and brings the bench-measured fixture floor to **4.2 uV median window pk-pk,
about 1% of the limit**.

### The filter

A Kaiser windowed-sinc low-pass, designed once per decimation factor and
cached. Pure standard library (no scipy); `math.sumprod` fast path.

```
taps            621   (odd, symmetric, exactly linear phase)
sum(taps)       1.000000000000   (exact unity DC gain)
passband edge   0.886 x post-decimation Nyquist  = 22.15 Hz
stopband edge   1.12  x post-decimation Nyquist  = 28.0  Hz
stopband target 60 dB
cost            12 ms per 20 s capture, 34 ms per 60 s soak
```

The passband edge was deliberately set to **0.886 x Nyquist so that the
-3 dB corner lands on 22.15 Hz - exactly where the old boxcar's corner was.**
The judged bandwidth is therefore unchanged; only the stopband improved.

Output timing is also identical to the boxcar: one output per 20-raw-sample
block, centred on the block, so all downstream window and clip index maths is
untouched.

### Measured response (input rate 1000 SPS)

| Frequency | FIR (current) | Boxcar (old) | Aliases to |
|---|---|---|---|
| 0.5 Hz | -0.0 dB | -0.0 dB | - |
| 1 Hz | -0.0 dB | -0.0 dB | - |
| 5 Hz | -0.0 dB | -0.1 dB | - |
| 10 Hz | 0.0 dB | **-0.6 dB** | - |
| 20 Hz | 0.0 dB | **-2.4 dB** | - |
| 22 Hz | 0.0 dB | -3.0 dB | - |
| 25 Hz | -5.6 dB | -3.9 dB | - |
| 28 Hz | **-60.1 dB** | -5.1 dB | 22 Hz |
| 40 Hz | **-72.7 dB** | -12.6 dB | 10 Hz |
| 60 Hz (mains) | **-84.1 dB** | **-16.1 dB** | **10 Hz** |
| 120 Hz | -85.0 dB | -17.8 dB | 20 Hz |
| 180 Hz | -91.7 dB | -21.0 dB | 20 Hz |
| 500 Hz | -94.0 dB | -240 dB (null) | 0 Hz |

Two things this table shows, both of which changed real readings:

- **The boxcar leaked badly above Nyquist.** After decimation to 50 SPS the
  new Nyquist is 25 Hz; everything above it folds back. 60 Hz mains landed
  at 10 Hz attenuated by only 16 dB and was counted as part noise.
- **The boxcar also drooped genuine in-band content** - 0.6 dB at 10 Hz,
  2.4 dB at 20 Hz - so real noise in the upper half of the declared band was
  *under*-counted. The FIR is flat there.

### Edge handling

Padding is **odd reflection** (`2*x[edge] - x[i]`), which continues a linear
trend exactly. A linear settling ramp therefore passes through the filter to
machine precision (verified: max deviation 2e-16 V), leaving it intact for
the per-window detrend to remove. Simple mirror padding would have curled the
ends and manufactured pk-pk in the first and last windows.

---

## 4. Stage 2 - per-window detrend (the high-pass)

**Function:** `stability_analysis.detrend_window_segments`

Each 1 s window (50 samples at 50 SPS) gets a least-squares straight line -
**mean and slope** - fitted and subtracted. This was added on 2026-08-13
because a real part passed while showing "worst 1.0 mV", which turned out to
be pure DC settling: the offset had not finished settling when the window
started, and the within-window slope inflated the pk-pk. It is the rigorous
version of "take a fresh offset every second", and it mirrors the legacy
AC-coupled amplifier, which never showed the scope any DC drift.

It is also, unavoidably, a **high-pass filter**. Because block detrending is
not shift-invariant it has no closed-form transfer function; the response
below is the ensemble ratio over uniformly distributed signal phase:

| Frequency | Fraction kept | dB |
|---|---|---|
| 0.05 Hz | 0.004 | -48.7 |
| 0.10 Hz | 0.015 | -36.7 |
| 0.20 Hz | 0.057 | -24.8 |
| 0.30 Hz | 0.126 | -18.0 |
| 0.50 Hz | 0.319 | **-9.9** |
| 0.75 Hz | 0.601 | -4.4 |
| **0.85 Hz** | **0.705** | **-3.0** |
| 1.00 Hz | 0.834 | -1.6 |
| 1.50 Hz | 0.974 | -0.2 |
| 2.00 Hz and above | ~0.97-1.00 | ~0 |

**The -3 dB corner is 0.85 Hz.** Intuition: a 0.2 Hz sine completes only a
fifth of a cycle inside a 1 s window, so it looks like a sloped line and the
fit eats it; a 2 Hz sine completes two full cycles, the best-fit line through
it is flat, and it survives untouched.

> **Consequence worth flagging.** The part's own noise is documented as
> dominated by 0.5-5 Hz. The detrend attenuates the bottom of that band by
> ~10 dB at 0.5 Hz and ~18 dB at 0.3 Hz. **The gate under-counts genuine
> low-frequency part noise**, and it does so by design, as the price of DC
> rejection. Any future move to a narrower, legacy-matched band must
> re-examine this corner rather than inherit it.

---

## 5. Stage 3 - the windowed peak-to-peak rule

**Function:** `stability_analysis.analyze_noise_capture_band_limited`

The detrended, band-limited trace is cut into 1 s windows
(`NOISE_WINDOW_S = 1.0`; 20 windows in a standard capture, 60 in a soak) and
each window's peak-to-peak is compared with the 429 uV limit.

```
PASS  iff  windows_over <= int(windows_total * max_over_fraction)

standard 20 s : max_over_fraction 0.15  ->  3 of 20 allowed
60 s soak     : max_over_fraction 0.05  ->  3 of 60 allowed  (same ABSOLUTE 3)
```

The soak deliberately holds the allowance at the **same absolute count**, not
the same percentage. Rationale (2026-08-18, from the lot-500 re-run): one
environmental bang spans only 1-3 windows regardless of capture length, while
genuine burst noise recurs and accumulates windows. Tripling observation time
while holding the count constant is what discriminates the two.

TP412 itself allows *no* excursion over the limit; the 15% allowance is a
deliberate relaxation for these very sensitive parts, calibrated on
2026-08-17 against the lot-500 fixture comparison.

**Clipping is checked on the RAW samples**, never the filtered trace: a
window whose raw samples touch `NOISE_CLIP_LIMIT_V = 4.9 V` counts as
over-limit regardless of its filtered pk-pk, because averaging a railed input
produces a quiet-looking DC level that would otherwise hide the fault.

A noise FAIL is **immediate** - the sensitivity capture is skipped and
`N - Noisy` is preselected.

### What is recorded

Verdict display is deliberately **PASS/FAIL only**, with no voltage on
screen: this rig reads uV at the pin while the legacy station reads mV behind
its amplifier, so any on-screen magnitude invited a false comparison. Nothing
is lost - every level goes to the CSV (`noise_worst_pp_mv`,
`noise_median_pp_mv`, `noise_windows_over`, `noise_over_percent`,
`noise_pp_limit_mv`, `noise_analysis_rate_hz`, `noise_baseline_settled`,
`noise_settle_s`, `noise_capture_s`) and a failing capture auto-saves a PNG.

---

## 6. What changed on 2026-08-20, and the evidence

### The defect

The pipeline's effective passband had never been measured. When it was, the
20:1 boxcar turned out to be a poor anti-alias filter: its sidelobes reject
only ~13 dB, so content above the 25 Hz fold point came back into the judged
band. Measured contamination - boxcar decimation compared against an ideal
brick-wall decimation of the same capture:

| Capture | Aliased-in residual | Note |
|---|---|---|
| 500-27 | 1.0% | negligible |
| 500-44 | 2.2% | negligible |
| test2-1 | 2.8% | negligible |
| 500-13 | 4.7% | |
| 500-18 | 5.8% | |
| 500-12 | 7.2% | |
| test-22_3 | 29.7% | |
| test-23 | 35.6% | |
| test-22_5 | 36.5% | |
| test-22_4 | 38.1% | |
| test-22_2 | 38.7% | |
| **test-22** | **41.0%** | **worst case** |

On the quiet lot-500 production parts the error was harmless. On the
interference-heavy bench captures, **up to 41% of the "noise" being judged
was phantom energy folded down from above 25 Hz** - energy the legacy
AC-coupled chain never displayed.

### The fix

`analyze_noise_capture_band_limited` now decimates through
`decimate_antialiased` instead of `decimate_boxcar`. Same passband, same
output timeline, >= 60 dB stopband. `decimate_boxcar` is retained for the
live preview (display-only, cheap enough to run inside the capture loop) and
for A/B comparison.

### Validation

Every saved raw capture was replayed under both decimators. **Each capture
reproduces its stored verdict exactly under the decimator that was in force
when it was recorded** - captures from 2026-08-18 match the boxcar replay to
the uV, captures from 2026-08-24 and 2026-08-26 match the FIR replay to the
uV. The lot-500 calibration anchors are unaffected:

| Capture | Recorded | Boxcar replay | FIR replay | Stored |
|---|---|---|---|---|
| 500-12 | 08-18 | 187 uV, 0/20 PASS | 188 uV, 0/20 PASS | PASS |
| 500-13 | 08-18 | 254 uV, 0/20 PASS | 272 uV, 0/20 PASS | PASS |
| 500-18 | 08-18 | 205 uV, 0/20 PASS | 218 uV, 0/20 PASS | PASS |
| 500-27 | 08-18 | 1224 uV, 18/20 FAIL | 1230 uV, 18/20 FAIL | FAIL |
| **500-44** | 08-18 | **501 uV, 1/20 PASS** | **502 uV, 1/20 PASS** | **PASS** |
| 500-44_2 | 08-18 | 266 uV, 0/20 PASS | 258 uV, 0/20 PASS | PASS |
| test-22 | 08-18 | 729 uV, 20/20 FAIL | 689 uV, 20/20 FAIL | FAIL |
| test-22_3 | 08-24 | 804 uV, 1/20 PASS | 940 uV, 2/20 PASS | PASS |
| test-22_4 | 08-24 | 935 uV, 12/20 FAIL | 983 uV, 15/20 FAIL | FAIL |
| test-22_5 | 08-24 | 418 uV, 0/20 PASS | 482 uV, 1/20 PASS | PASS |
| test-23 | 08-24 | 898 uV, 11/20 FAIL | 1011 uV, 14/20 FAIL | FAIL |
| **test2-1** | 08-26 | **1361 uV, 3/20 PASS** | **1341 uV, 4/20 FAIL** | **FAIL** |

**`test2-1` is the one part whose verdict differs between the two filters**,
and it is worth understanding rather than dismissing. It sits exactly on the
boundary: 3 over-windows (allowed) under the boxcar, 4 (one too many) under
the FIR. The FIR reads it as failing because the boxcar had been *drooping*
genuine in-band content in the 10-22 Hz region. Its band-resolved energy
shows the part also carries **92 uV RMS at 0.85-5 Hz - the highest genuine
low-frequency noise of any capture in the set**, against 55 uV for the
noisiest passing lot-500 part. On that evidence the FAIL looks defensible on
real part noise, not on filter artifacts.

### Readings move, verdicts mostly do not

The FIR is not uniformly "lower". It removes phantom aliased energy (pushing
readings down) *and* stops under-counting genuine 10-22 Hz content (pushing
them up). Which dominates depends on where a capture's energy sits - hence
test-22 falling 729 to 689 uV while test-23 rises 898 to 1011 uV.

One systematic shift worth knowing when reading old numbers: **square-edged
signals read ~18% higher** through the flat passband (in-band harmonics no
longer drooped, plus band-edge Gibbs ripple). The synthetic 2 mV test fixture
reads 2.356 mV. Window-over counts - the quantity the verdict actually uses -
are unchanged on every synthetic fixture.

---

## 7. Known limitations and open questions

1. **The 700x vs 4000x gap is unexplained and is NOT a bandwidth effect.**
   Weighting a measured pin spectrum by every AC-coupled 1-pole band-pass
   over a 0.1-12 / 0.1-30 Hz grid, the closest achievable ratio to the
   required 0.175 is 0.235, and only at a degenerate 12 Hz notch that no
   1 Hz-optimised amplifier would have. Physically sensible 1 Hz-centred
   bands give 0.9-1.4. The discrepancy is therefore a **real gain
   difference** - a 10:1 probe setting, an amplifier range switch, or a
   wrong nameplate. Until it is resolved, the 429 uV limit rests on a
   single-part derivation. **This is the highest-value open item.**
2. **The legacy amplifier's passband is unknown.** The 0.85-22 Hz band this
   rig judges over is an artifact of implementation, not a match to the
   legacy instrument. The cheapest way to close this is to photograph the
   amplifier board and read the RC values around the TL084 (input coupling
   capacitor + feedback capacitor give both corners directly); the rigorous
   way is a swept-sine measurement. With the real response known, the rig
   could replicate it digitally instead of scaling through one scalar.
3. **Whether 10-22 Hz interference should count against a part is
   undecided.** The test-2x captures are dominated by a **9.8 Hz spike train
   with harmonics at 19.5, 29.2, 39.0 and 48.8 Hz** - a fixture or
   environmental source, not pyroelectric noise. The FIR correctly stops the
   >= 29 Hz harmonics from folding in, but the 9.8 and 19.5 Hz components are
   genuinely inside the declared band and do count today. If the legacy amp
   rolls off above ~5 Hz, these are false fails against the legacy standard.
   **Finding and removing the 9.8 Hz source is worth doing regardless.**
4. **The detrend's 0.85 Hz corner cuts into the part's own noise band**
   (section 4). It is currently an accepted trade for DC rejection.
5. **The 20 s / 60 s capture lengths are fixed.** Adapting duration to the
   part's measured noise remains future work.
6. `500-27_noise_raw.npz` and `_2` hold **byte-identical waveforms**
   (duplicate save, both 09:39) - worth checking whether re-measure can save
   a stale buffer.

---

## 8. Reproducing everything in this report

Two tools in `engineer_tools/` (repo root):

```bash
# Replay saved captures through the production pipeline and/or custom bands.
# Writes a PNG per capture (raw trace, judged traces vs limit, spectrum)
# plus a verdict-comparison table.
python engineer_tools/replot_noise_capture.py                    # all captures
python engineer_tools/replot_noise_capture.py --band 0.5 5       # custom band
python engineer_tools/replot_noise_capture.py --boxcar 20        # pre-08-20 pipeline

# Measure the pipeline's passband, its aliasing, and test legacy-amp
# passband hypotheses against a real capture's spectrum.
python engineer_tools/filter_response_analysis.py [CAPTURE.npz]
```

Captures live under
`~/Documents/Eltec_405M22_Test_Results/405m22_esp32/noise_captures/`.

### Source map

| What | Where |
|---|---|
| Constants, capture, orchestration | `tech_app/eltec_rig/m405m22/eltec_405m22_esp32_tester.py` (`NOISE_*` block; `read_noise_capture`) |
| FIR design and decimation | `tech_app/eltec_rig/m405m22/stability_analysis.py` (`design_antialias_lowpass_fir`, `decimate_antialiased`) |
| Detrend | same file (`detrend_window_segments`) |
| Verdict rule | same file (`analyze_noise_capture_band_limited`, `analyze_noise_capture`) |
| Tests | `tech_app/eltec_rig/m405m22/tests/test_stability_analysis.py`, `test_v6_integration.py` |

The frozen standalone build `tech_app/405m22_esp32/` still uses the boxcar and
is intentionally left untouched, per the originals-frozen rule.
