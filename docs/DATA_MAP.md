# Data map — where results, captures and calibration files live

**Nothing the apps record is stored inside this repository.** Every batch CSV,
attempt log, waveform snapshot, raw noise capture and reference-calibration
file is written to a per-model folder under the signed-in user's `Documents`.
This file maps those folders, the naming conventions, the in-repo evidence
that backs the production constants, and the backup routine.

Reconciled with the Windows bench laptop on **2026-08-28**.

---

## 1. Results roots (one per sensor model)

| Model | Root (Windows `%USERPROFILE%\Documents\…`, Xubuntu `~/Documents/…`) | Set in |
| --- | --- | --- |
| 405 M22 | `Eltec_405M22_Test_Results\405m22_esp32\` | `sensor_versions.py` / `m405m22` tester |
| 406 MCA | `Eltec_406MCA_Test_Results\v6_1_esp32\` (the folder name is the historical v6.1 build id — it is the correct, live location) | `m406mca` tester |
| 449 M18 | `Eltec_449M18_Test_Results\449m18_esp32\` | `m449m18` tester |

The apps create these on first save. A batch started on Windows is readable on
Xubuntu and vice versa (same CSV format, same layout). Re-entering an existing
batch number resumes it at the next sensor.

### Inside each root

```
<root>\
├── <prefix>_lot_<lot>.csv              one verdict row per saved sensor (the production record)
├── <prefix>_lot_<lot>_attempts.csv     one row per event: measured / measure_error / remeasure /
│                                       skipped / resumed / saved  (v2.0 attempt history)
├── autosave\                           in-progress batch state (crash recovery)
├── waveform_snapshots\lot_<lot>\       PNG per "Capture waveform" (+ cycle CSV sidecars);
│                                       automatic for failing noise captures / unstable drives
├── noise_captures\lot_<lot>\           405 M22 only: <sensor>_noise_raw.csv + .npz — the RAW
│                                       1000 SPS emitter-off record (opt-in button, and automatic
│                                       whenever any window goes over the limit)
├── calibration\*_cycles.csv            stability_calibration.py evidence captures (engineering)
└── reference_sensor_calibration.json   AIN1 reference baseline (only meaningful while the
                                        reference gate is on — it is OFF on every model today)
```

`<prefix>` is `405m22_esp32`, `406mca_esp32` or `449m18_esp32`. The 449 M18
snapshots carry `_5hz` / `_18hz` suffixes.

CSV columns are documented in each model README
([405](../tech_app/eltec_rig/m405m22/README.md),
[406](../tech_app/eltec_rig/m406mca/README.md),
[449](../tech_app/eltec_rig/m449m18/README.md)). Older batch files keep their
original header when columns are added later — rows stay aligned, new columns
are simply absent. A `NOT MEASURED` row (rig fault, nothing recorded) has empty
measurement columns and `failure_mode_tag = NM`; it is excluded from yield.

### Provenance suffixes seen on the bench laptop

Files were occasionally renamed by hand to keep history:
`_superseded_<date>_<reason>`, `_backup_all50_<date>`, `_hold_…`,
`_new15_only_backup`, `…_crosstalk_contaminated_<date>.json.bak`. Treat any
such file as **frozen evidence** — never delete or "clean up" without checking
[`CALIBRATION_RECORD.md`](CALIBRATION_RECORD.md) for what it backs. Prefer this
convention for future manual archiving: `<original name>_<YYYYMMDD>_<reason>`.

---

## 2. What is on the bench laptop today (2026-08-28)

| Folder | Size | Contents that matter |
| --- | --- | --- |
| `Eltec_405M22_Test_Results\405m22_esp32\` | ~15 MB, 75 files | **lot 500** (`405m22_esp32_lot_500.csv` + `_backup_all50_20260818` + `_superseded_20260817_reference_crosstalk`), test lots (`lot_test`, `lot_test2`, `lot_test_2`), `noise_captures\` (39 files, 9.7 MB — the raw captures behind the 15 % rule, the 60 s soak and the FIR change), `waveform_snapshots\` (18 files), `noise_experiments\` (2026-08-13 bench A/B: `part_in`, `part_out`, `covered`, `fan_off` `.npz` + `noise_experiment.py`), copies of the lot-500 Excel pair data, `reference_sensor_calibration_crosstalk_contaminated_20260817.json.bak` |
| `Eltec_406MCA_Test_Results\v6_1_esp32\` | ~16 KB | `406mca_esp32_lot_test.csv` + attempts, `reference_sensor_calibration.json` (historical baseline, unused while the gate is off) |
| `Eltec_449M18_Test_Results\449m18_esp32\` | ~5 KB | one attempts log — no production batch yet (calibration pending) |

Related folders outside this repository:

- `Documents\Eltec_IR_Telescope\` — the two-detector IR telescope: its own git
  repository (firmware v2.2 with `STREAM,START,BOTH`, app, session data).
  Unrelated to the sensor rig except that the same ESP32 board is reflashed
  between the two.
- `Documents\406_mca_data\` and `Documents\rename_to_Eltec_TestRig.cmd` —
  LabJack-era loose data and a leftover rename script. Candidates for archiving
  or deletion; not referenced by anything in this repository.

---

## 3. Evidence kept inside the repository

| Path | What it is | Backs |
| --- | --- | --- |
| `analysis/405M22_Data/405M22_Data_old_fixture.xlsx`, `405M22_Data_new_fixture.csv.xlsx` | lot-500 paired measurements, legacy fixture vs this rig (2026-08-17) | sensitivity factor **4.30**, noise allowance **15 %** |
| `analysis/reports/405M22_Noise_Filtering_Explained.md` (+ `.docx`, two PNGs) | explainer for the noise pipeline passband and the anti-alias FIR (2026-08-20) | noise verdict band, FIR change |
| `analysis/reports/LabJack_T7_406MCA_Buffer.docx`, `406MCA_Original_Buffer_Issue_Explanation_Readable.docx` | LabJack-era buffer write-ups | historical background for the op-amp buffer stage |
| `analysis/reports/406MCA_SNR_Analysis_Report.docx`, `406MCA_SNR_Group_Summary.csv` | outputs of the retired v1 SNR sweep analysis | historical |

The raw captures themselves (`.npz`) are **not** in git (they are ignored by
`.gitignore`) — see §5.

---

## 4. Tools that read this data

| Tool | Reads | Purpose |
| --- | --- | --- |
| `engineer_tools/replot_noise_capture.py` | `noise_captures/**/*_noise_raw.npz|.csv` | Replays a saved raw capture through the exact production pipeline and through alternative bands / boxcars; prints a verdict-comparison table and a PNG per capture. This is why raw captures must never be lost — any future limit or band change can be re-judged on real parts. `--boxcar 20` reproduces the pre-2026-08-20 pipeline. |
| `engineer_tools/filter_response_analysis.py` | (synthetic + a measured capture spectrum) | Characterises the pipeline's passband and aliasing; tests legacy-amplifier passband hypotheses. |
| `tech_app/eltec_rig/<model>/stability_calibration.py` | live rig → `calibration/*_cycles.csv` | Collects known-good peak-delta evidence for the stability threshold (`capture`, `summarize`). Engineering only — never issues verdicts. |

---

## 5. Backup — required, not optional

The ~15 MB of captures on the bench laptop is the **only copy** of the
evidence behind three production constants (the 4.30 factor, the 15 % noise
rule, the 60 s soak). The laptop is a single point of failure.

Routine (owner: the rig engineer):

1. **After every calibration lot and at least monthly**, zip the three roots:
   `Eltec_405M22_Test_Results`, `Eltec_406MCA_Test_Results`,
   `Eltec_449M18_Test_Results` → `Eltec_TestResults_backup_<YYYY-MM-DD>.zip`.
2. Copy the zip to the company share / cloud drive (not the same laptop).
3. Open the zip once and confirm `lot_500` CSVs and `noise_captures\lot_500\`
   are inside.
4. Note the backup date in [`CHANGELOG.md`](../CHANGELOG.md) when a
   calibration lot was added.

Keep the lot-500 files permanently. Test-lot files (`lot_test*`) can be pruned
once backed up, but the `noise_captures` under them are still useful for
replay work.

Also back up `analysis/405M22_Data/` implicitly — it is in git, so pushing to
the remote covers it.
