# Eltec 406MCA Failure Calibration — v6.1 Base

This is an isolated engineering build for comparing the v6.1 tester's verdicts
with technician-confirmed ground truth. It keeps the normal ESP32/ADS1256
hardware sequence and v6.1 measurement policy, then collects structured
feedback on the verdict page.

> **Experiment only — not for production acceptance.** Use
> `tech_app/v6_1_esp32/` for ordinary v6.1 evaluation work and
> `tech_app/v6_esp32/` for the current production workflow. This build collects
> evidence; it does not automatically change thresholds or production code.

## What this build is for

Use the known-failed sensor set, including any sensor that may actually pass,
to answer two separate questions:

1. Did the application produce the correct PASS/FAIL verdict?
2. If it failed the sensor, did it give the correct failure reason?

The application prediction and the technician annotation must remain separate.
Do not change a ground-truth answer merely to agree with the application. Mark
an uncertain sensor as uncertain and explain why; uncertainty is more useful
than a forced label.

The full study folder is the deliverable. Keep the batch CSV, autosave data,
waveform images, and sample/cycle diagnostic CSV files together so a later code
change can be checked against the original signals instead of only the final
labels.

## Technician workflow

1. Give every physical sensor a permanent, unique specimen ID such as `S01`
   through `S20`. Reuse that ID for every repeat measurement.
2. Start a study/batch, enter the technician name, and choose the normal filter
   setup.
3. If required, calibrate the fixed reference unit with a known-good/new
   emitter. Do not use one of the unknown DUTs as the reference standard.
4. Load and measure the DUT exactly as in v6.1. The reference check, offset
   read, adaptive DUT capture, and PASS/FAIL calculation are unchanged.
5. On the verdict page, record the technician's ground-truth verdict. Confirm
   whether the application verdict and reason were correct; when they were not,
   select the expected reason and explain the disagreement.
6. Save the record before moving to the next sensor. For another independent
   acquisition, use **Save + Repeat Same Specimen**; the app requires every run
   to be reviewed first so a later measurement cannot hide an earlier verdict.

Hardware versus simulator mode is frozen when the batch starts. If the app is
closed before a reviewed result reaches the batch CSV, starting that same batch
again offers to recover the immutable run evidence and the autosaved review
fields; declining recovery leaves them untouched and prevents overwrite.

A PASS ground truth is valid even when the sensor arrived in the suspected-fail
set. A sensor can also be marked uncertain when its independent diagnosis is
not strong enough to call.

## Recommended 20-sensor study

- Run each specimen at least twice, preferably three times, reseating it
  between runs.
- Add independently confirmed known-good DUT controls and repeat them across
  the session. A mostly-failed 20-sensor set is useful for correcting failure
  reasons, but by itself cannot estimate the false-reject rate on good parts.
- Randomize the order rather than testing all repeats back-to-back.
- Keep the filter/setup fixed unless the study intentionally evaluates more
  than one setup; document every intentional change.
- Record the expected electrical symptom separately from a physical history
  such as “dropped.” The tester can learn from waveform, offset, sensitivity,
  polarity, noise, and stability evidence; it cannot directly observe a
  handling history.
- Record ambiguity and mixed symptoms in the explanation instead of forcing a
  single overconfident cause.
- Reserve several specimens as a holdout set. Do not use their labels while
  tuning rules; use them afterward to check that an apparent improvement
  generalizes beyond the sensors used for tuning.
- Do not mix simulator rows with hardware evidence. Simulator mode is for UI
  training and automated checks only.

After collection, preserve or archive the entire
`v6_1_failure_calibration` results directory. The raw diagnostic sidecars are
needed to replay proposed threshold and classification changes.

## Study handoff checks

`calibration_dataset.py` is a standalone, read-only standard-library utility.
Summarize the hardware batch rows (the `simulator/` directory and synthetic
rows are excluded automatically):

```bash
python3 tech_app/v6_1_failure_calibration/calibration_dataset.py summarize
python3 tech_app/v6_1_failure_calibration/calibration_dataset.py summarize /path/to/v6_1_failure_calibration
```

It prints JSON counts for saved rows, unique physical specimens, app-versus-
ground-truth verdicts, review classifications, and app-mode-to-truth-mode
comparisons.

Before copying the study, verify the complete finalized record chain:

```bash
python3 tech_app/v6_1_failure_calibration/calibration_dataset.py verify
python3 tech_app/v6_1_failure_calibration/calibration_dataset.py verify /path/to/v6_1_failure_calibration
```

For each hardware run, `verify` requires exactly one matching batch row and a
sibling `review.json`. It verifies the manifest and review hashes recorded in
the CSV, cross-checks the run/specimen/app-prediction/ground-truth fields across
all three records, checks exported measurement/reference/stability/policy
values against the manifest, and recomputes every evidence artifact's size and SHA-256.
Simulator folders and synthetic rows are excluded. Missing, mismatched, or
unreadable entries are printed as JSON and produce a nonzero exit.

Bring back the **entire**
`~/Documents/Eltec_406MCA_Test_Results/v6_1_failure_calibration/` folder—not
only the summary JSON or batch CSV. Keep its `evidence/` run directories,
`run_manifest.json`, `review.json`, waveform images, and sample/cycle CSVs in
place. Current manifests and CSV rows contain portable relative paths, so the
copied folder can be verified at its new location; legacy absolute-only
artifact entries are safely rebased to their manifest's run directory.

## V6.1 measurement policy retained by this build

The DUT is captured in one uninterrupted AIN0/PWM-sync stream. Every attempt
requires 10 consecutive robust-peak deltas at or below `0.100 mV`, followed by
20 measurement cycles that must remain at or below the same threshold.

At startup this build verifies the expected hashes of the shared verdict
engine, stability analysis, and tracked settings. If any rule source changes,
measurement is blocked until a new calibration app/ruleset version is created,
preventing two decision policies from being mixed in one study.

| Attempt | Qualification | Official measurement | Over-threshold measurement delta |
| --- | ---: | ---: | --- |
| 1 | 10 consecutive deltas | 20 cycles | Discard and start attempt 2 |
| 2 | 10 consecutive deltas | 20 cycles | Discard and start attempt 3 |
| 3 | 10 consecutive deltas | 20 cycles | Report unstable |

The 20-second deadline governs qualification and requalification. A complete
measurement window that starts after timely qualification may finish after the
deadline. Sensitivity, polarity, noise, and SNR use only the successful
attempt's 20-cycle window.

## Reference gate

The AIN1 reference-unit gate remains mandatory:

1. Check the 6 V battery and force PWM off.
2. Turn PWM on and immediately stream AIN1.
3. Require five consecutive robust-peak deltas at or below `0.250 mV`.
4. Average the next five complete cycles.
5. Require the result to remain inside the fixed `+/-25%` reference window
   before accessing AIN0.

Calibration files created with the former `+/-10%` policy are automatically
loaded with the current `+/-25%` window; a new reference calibration is not
required solely for this policy change. The calibration-study application is
versioned as `.4`, so use a new batch ID if an existing batch contains `.1`,
`.2`, or `.3` rows. This keeps acquisition and failure-selection policies
distinguishable during later analysis.

The default primary failure mode follows measurement order: out-of-range
offset (`HO`, `D`, or `LO`) takes priority, followed by instability, then
waveform failures. Waveform capture still continues after an official offset
failure so secondary sensitivity, polarity, SNR, and stability evidence is
retained. With an in-range offset, low sensitivity plus failed SNR/no coherent
response is classified as `GO/D`; a coherent under-amplitude response remains
`LS`.

This build's writable calibration location is isolated with its other results:

```text
~/Documents/Eltec_406MCA_Test_Results/v6_1_failure_calibration/reference_sensor_calibration.json
```

If a compatible v6.1/v6 calibration is offered as a read-only fallback, its
source must remain unchanged. Calibration and invalidation performed by this
build write only to the failure-calibration location.

## Data and launcher isolation

```text
Results:      ~/Documents/Eltec_406MCA_Test_Results/v6_1_failure_calibration/
Launcher log: ~/.local/state/eltec-406mca-failure-cal-v6-1/launcher.log
Menu entry:   ~/.local/share/applications/com.eltec.406mca-failure-cal-v6-1.desktop
Desktop icon: ~/Desktop/Eltec 406MCA Failure Calibration — v6.1 Base.desktop
```

Do not move study CSVs into the v6 or v6.1 results directories. Their schemas
and purposes differ, and mixing them would make agreement analysis unreliable.

## Run

From the repository root:

```bash
./tech_app/v6_1_failure_calibration/run_eltec_406mca_esp32_tester.sh
```

Or from this directory:

```bash
./run_eltec_406mca_esp32_tester.sh
```

Install the optional per-user Xubuntu desktop/menu launcher:

```bash
./tech_app/v6_1_failure_calibration/install_xubuntu_launcher.sh
```

Remove only this experimental launcher:

```bash
./tech_app/v6_1_failure_calibration/install_xubuntu_launcher.sh --uninstall
```

## Hardware and dependencies

Use `Arduino/Eltec/Eltec.ino` v1.7 or newer and the current fixture wiring in
`Arduino/Eltec/ESP32_ADS1256_Wiring_v1_7.md`:

- GPIO25: fixed 10 Hz / 50 percent emitter PWM;
- ADS1256 AIN0: buffered DUT sensor;
- ADS1256 AIN1: permanently mounted reference sensor;
- ADS1256 AIN7: 6 V SLA battery divider.

Required packages are Python 3, Tkinter, NumPy, and pyserial. Matplotlib is
optional for higher-quality waveform snapshots.

```bash
sudo apt install python3 python3-tk python3-numpy python3-serial
sudo apt install python3-matplotlib libnotify-bin desktop-file-utils xdg-user-dirs
```

The signed-in user must be able to access `/dev/ttyUSB*` or `/dev/ttyACM*`,
normally through the `dialout` group. Close Arduino Serial Monitor,
`live_waveform.py`, production testers, and other serial applications before
starting this build.

`stability_calibration.py` is a separate engineering utility for collecting
known-good peak-delta evidence. It does not collect verdict ground truth, issue
production verdicts, or edit `stability_settings.json`.

## Verification

Run the isolated suite from the repository root:

```bash
python3 -m unittest discover -s tech_app/v6_1_failure_calibration/tests -v
```

Before field use, also rerun the unchanged v6.1 and v6 suites to confirm that
the experimental fork did not alter either existing application.
