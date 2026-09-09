# Choosing the AIN1 reference unit — bench procedure

`reference_candidate_qualifier.py` measures candidate 406MCA detectors the
way the production 406 MCA app measures a part, records everything, and
ranks the candidates for the job of **permanently mounted reference unit**
on AIN1 (the emitter-health gate, `REFERENCE_GATE_ENABLED`). The buffer board
now has one independent op-amp per channel, and the gate went back **on for
all three models on 2026-09-09** — ahead of the crosstalk re-check, which is
still outstanding (CALIBRATION_RECORD §2.4). So the remaining work below is
live production work, not preparation: until the chosen part is mounted and a
fresh **Calibrate reference unit** has been run, each rig refuses its stored
(pre-isolation) baseline and locks testing.

## Precision retest on AIN0 (recommended for the final selection)

Use this mode for refD, refC and refA. The part stays in the **AIN0 DUT
socket throughout its session**; no AIN1 access is needed. Close the tester
and other serial-port apps, seat the candidate, then run one command at a
time and swap the candidate only after its command finishes:

```powershell
python engineer_tools/reference_unit/reference_candidate_qualifier.py capture --precision --label refD
python engineer_tools/reference_unit/reference_candidate_qualifier.py capture --precision --label refC
python engineer_tools/reference_unit/reference_candidate_qualifier.py capture --precision --label refA
python engineer_tools/reference_unit/reference_candidate_qualifier.py compare --precision refD refC refA --show
```

Each session takes **30 readings**, all driven at **10 Hz / 50% duty**.
Twenty cycles means two seconds of measurement **after** stabilization;
30 readings means 30 separate emitter activations to test repeatability.

1. Prompt once to seat the part. Let the insertion offset settle once.
2. Start the emitter and acquire the existing production DUT measurement:
   ten confirming deltas at the tracked DUT threshold, then 20 fresh cycles,
   including production stability retries and signal-quality checks.
3. On the same saved waveform, select the **earlier reference reading** using
   the app's reference settings (currently five confirming deltas at 0.250 mV,
   then the mean of five fresh cycle peak-to-peak values). This is a replay
   on AIN0, not a change to the production capture policy or AIN1 routing.
4. Keep the emitter driven for a further **20 s** to measure hold drift and
   exercise heating. Switch it off and take the production offset re-read;
   do not add the screening mode's extra wait for a quiet offset.
5. Leave the part seated. Alternate minimum emitter-off intervals of
   **2, 10 and 60 s** before the remaining readings. This probes rapid use,
   ordinary pauses and longer pauses without grouping all cold readings at
   one end of the session. Actual off-times are saved, including processing
   and offset-read time. The first reading is reported separately as `initial`.

Budget approximately **25 minutes per candidate** with typical 3–6 s driven
captures. This is a repeatable thermal exercise on the reference candidate,
not an exact replay of every model's DUT sequence. Keep optics, supply,
airflow and the test options the same for all three candidates. A second
complete session on another day adds useful evidence about longer-term
consistency; do not reseat during either session.

`--runs N`, `--hold-s S` and `--off-intervals-s 2,10,60` customize this mode.
Use identical options for all candidates. Fewer than 30 readings produces a
warning; one reading cannot estimate precision. `--port COM3` selects a
port explicitly. `--auto` skips the initial seating prompt only.

Results go to **`~/Documents/Eltec_ReferenceCandidates/precision/<label>/`**.
`--out` changes the parent of `precision`, for both capture and compare.
Original insertion-screening results are retained and never pooled with
precision records. Each reading saves its raw NPZ, five-cycle selection,
20-cycle results, interval, run number, planned session length and session ID.
Ctrl+C preserves completed readings. An interrupted session is excluded from
ranking; repeat it in a fresh `--out` folder and use that same folder for the
other candidates. Stream faults still use the production bounded retries;
retry counts remain visible because retries can alter the heating history.

The precision table reports:

| Column | Meaning |
| --- | --- |
| `ref CV %` | Sample standard deviation / mean across complete five-cycle readings, in percent; lower is more repeatable |
| `max dev %` | Largest absolute five-cycle deviation from that candidate's own mean; not an accuracy specification or deviation from a calibrated baseline |
| `ready s`, `worst s` | Median and worst PWM-on time through the fifth fresh cycle; transport/UI completion overhead is excluded |
| `20cy CV %` | Between-reading CV of the later production 20-cycle sensitivities |
| `pause span %` | Highest minus lowest repeated off-interval group mean, divided by the overall reference mean; excludes the initial group |
| `early/late %` | Median signed difference between early reference and later production sensitivity; consistent bias can be calibrated, so this is not ranked as an accuracy error |
| `drift %/min` | Median absolute slope over the 20 s drive holds; a short-hold slope, not a prediction of indefinite drift |
| `retries`, `prod pass` | Recorded stream retries and the original production checks passed |

Condition-specific means, CVs and sample counts print below the table. The
CSV also retains SNR, within-window cycle CV, early/late difference spread
and the original diagnostics. The precision plot shows reading deviations,
completion times, early/late differences and hold drift in acquisition order.

Precision ranking uses weighted metric ranks: five-cycle reading CV **5**,
worst deviation **3**, 20-cycle reading CV **2**, hold drift **2**, worst
ready time **1**, median ready time **1**. Insertion settling and offset
recovery do not earn ranking points. Ranks use 0.01 percentage-point bins
and 0.1 s timing bins to avoid credit for tiny timing differences; full
precision is retained in the files. These are engineering preferences, not
production acceptance limits or statistical significance tests.

All original production disqualifiers remain, including a failed 20-cycle
measurement; a missing five-cycle result or incomplete session also prevents
ranking. The comparison refuses different drive/hold/pause settings,
firmware, channels or mixed hardware/simulation data. Explicitly requested
candidate labels must all have matching data.

After choosing, validate the winner on AIN1 and calibrate it there. AIN0
repeatability cannot establish AIN1 mounting performance or channel isolation.
The production gate flags, thresholds and calibration factors are unchanged.

## Original insertion screening

Without `--precision`, the original three-run reseating procedure below is
preserved. It measures these additional screening properties:

| Property | Why it matters for a reference | Metric in the report |
| --- | --- | --- |
| Offset settles fast after insertion | a reference read is taken at the start of every test; a slow settler keeps the whole bench waiting | `settle s` (time until the level stays within ±10 mV of its final value), `band s`, `prod s` (what the production rule would decide) |
| Offset recovers fast after the emitter | the emitter heats the part every test; the next read must not see the previous test's heat | `recov s`, `shift mV` |
| 10 Hz response stabilizes fast, every time | the reference capture waits for stability inside the 20 s deadline | `stab s`, and any run that never stabilized disqualifies |
| Same amplitude every time | the gate is a ±25 % window around the calibrated baseline; run-to-run spread eats that window | `run CV %` (run-to-run), `cyc CV %` (within the 20 official cycles) |
| No drift while driven | the reference is read while warm | `drift %/min` over the drive hold |
| Clean signal, stable polarity, passes the production test | anything the app itself would reject is no reference | `SNR`, `pol`, `pass` |

## Setup

* Bench ESP32 + ADS1256 rig on COM3 (auto-detected), firmware v2.1 or newer
  (the tool sends `FE,V19` through the production backend exactly as the app
  does; a v1.9 legacy board is refused).
* Candidates go one at a time into the **DUT socket (AIN0)** — the default
  `--channel sensor`. That is the strictest, most discriminating test (DUT
  stability rules) and the socket makes swapping five parts quick.
  `--channel ref` measures a part that is already on the AIN1 mount instead
  (`REF?` / `STREAM,START,REF`, same threshold and cycle count).
* Nothing else on the port: close the tester app, the readout and the live
  viewer first.
* Output root: `~/Documents/Eltec_ReferenceCandidates/<label>/` (change with
  `--out`; a production `Eltec_*_Test_Results` folder is refused).

## Run the insertion screening

For every candidate (label it by its marking, e.g. `refA` … `refE`):

```bash
python engineer_tools/reference_unit/reference_candidate_qualifier.py capture --label refA --runs 3
```

Each run prompts *"remove and RE-SEAT the part, then press Enter"* — do it,
so every run starts with a real insertion event. The run then takes, per
phase:

| Phase | What happens | Typical time |
| --- | --- | --- |
| A | `OFFSET?` polled once a second until the level is quiet for 8 s (max `--offset-max-s`, 60 s) | 10–60 s |
| B | production capture: `PIN,33`, `PWM,ON` (10 Hz / 50 %), `read_waveform_until_stable` with the DUT settings, stream retries and front-end re-checks as in the app | up to 22 s |
| C | drive hold, emitter still on (`--hold-s`, 20 s; `0` skips) | 20 s |
| D | `PWM,OFF`, the production `wait_for_settled_offset` live, then polling until quiet again | 5–60 s |

Three runs of five candidates is roughly 30 minutes. Each run writes
`<label>_run<n>_<stamp>.json` (every number plus the offset and per-cycle
series) and `.npz` (the raw waveforms — `waveform_v`, `sync`,
`sample_rate_hz`, the hold, the offset series).

Then:

```bash
python engineer_tools/reference_unit/reference_candidate_qualifier.py compare
```

prints the table, the weights, the disqualifications and a recommendation,
and writes `comparison_<stamp>.csv` + `.png` (offset settle curves, post-
emitter recovery, per-cycle pk-pk vs PWM-on time, sensitivity per run)
under the output root. `compare refA refC --show` limits it to some labels
and opens the plot window.

## How the ranking works

Candidates are **disqualified** (listed, never ranked) when any run failed
the production test, never stabilized, never entered the 0.3–1.2 V offset
band, or the polarity changed between runs. The rest are ranked per metric
(ties share the average rank; a missing value takes the worst rank), each
rank is multiplied by its weight and the smallest total wins:

| Metric | Weight |
| --- | ---: |
| offset settle s (median over runs) | 3 |
| worst offset settle s | 1 |
| post-emitter recovery s | 2 |
| stabilization s | 2 |
| run-to-run sensitivity CV % | 3 |
| hold drift %/min (absolute) | 2 |
| cycle CV % | 1 |
| 1 / SNR | 1 |

The weights are `SCORE_WEIGHTS` at the top of the script and are printed
with every table. They are an opinion about what a reference is for, not
a calibration constant: read the per-metric columns before deciding, and
treat a recommendation that is "not the best on" a column you care about as
a question, not an answer.

## After choosing

1. Mount the chosen part on AIN1.
2. Re-check crosstalk with the new buffer (TI OPA2196), reproducing the
   original trigger: with a **shorted DUT** seated and the emitter driven,
   `REF?` and an AIN1 stream must not move (the 2026-08-17 finding was
   ~4.94 mV → ~0.30 mV, about −90 %). Through the app only the 406 MCA path
   reads AIN1 with the DUT seated — calibrate first, then load the shorted
   part and Start: clean = "Reference unit passed" then the "Is a sensor
   loaded?" prompt; crosstalk = "outside its window" and a lockout (now also
   a `measure_error` row in `_attempts.csv`). The 405/449 reject a ≈0 V part
   before AIN1 is read, so for them use `Arduino/Eltec/esp32_rig_readout.py ref`
   / `--channel ref` captures with the shorted DUT in place.
3. `REFERENCE_GATE_ENABLED = True` is already set on all three models
   (2026-09-09). Run **Calibrate reference unit** fresh on each rig — the
   schema bump (405/449 v5, 406 v3) refuses every older baseline, so this is
   required before any part can be tested — and record the chosen part, the
   step-2 crosstalk numbers and the new baseline in CALIBRATION_RECORD
   §2.4 / §5 with a CHANGELOG entry.
4. **If step 2 fails** — AIN1 still moves with the DUT — set
   `REFERENCE_GATE_ENABLED = False` again in each model rather than widening
   `REFERENCE_TOLERANCE_PERCENT`: a reference that tracks the loaded part is
   measuring the wrong thing, and no tolerance makes it trustworthy. The
   gate-off path is still unit-tested in all three models.

## Without hardware

```bash
python engineer_tools/reference_unit/reference_candidate_qualifier.py capture --simulate --label demo --sim-profile fast --runs 2 --out <scratch dir>
```

runs a simulated candidate (`fast`, `slow`, `jittery`, `high_offset`,
`never_stable`) on a virtual clock in a second; the suite's round-trip test
does the same.
