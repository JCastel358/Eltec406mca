# engineer_tools — engineering-only tools, one folder per topic

Nothing here issues a part verdict or is run by a technician. Every tool is
run from the repository root with the bench Python (stdlib + numpy;
matplotlib optional for plots) and reads the production code of the rig it
belongs to **by import, never by copy**, so a change in an app is picked up
automatically. Outputs go to explicit paths or to their own folder under
`Documents` — never into an `Eltec_*_Test_Results` evidence folder unless the
tool's docstring says so (the parity tool writes `calibration/` under the
array results root, by design; see `docs/DATA_MAP.md`).

| Folder | Tool | What it is for | Rig |
| --- | --- | --- | --- |
| `noise_band/` | `replot_noise_capture.py` | Replay saved raw noise captures (405 `*_noise_raw.npz`, array `tray_*_raw.npz`) through the production pipeline and any other band / boxcar; verdict comparison table + PNG per capture. | 405 M22, 40623 array |
| `noise_band/` | `filter_response_analysis.py` | Measure the noise pipeline's real passband and aliasing; test legacy-amplifier passband hypotheses against a capture's spectrum. | 405 M22 |
| `array_parity/` | `array_noise_parity.py` | Derive the array rig's pin-level noise limits from a lot measured on both the legacy fixture and the array rig (CALIBRATION_RECORD §4b.2). | 40623 array |
| `emitter/` | `emitter_waveform_comparison.py` | Legacy chopper vs rig emitter waveform *shape* (rise/fall, harmonics) — the 18 Hz pre-qualification for the 449 M18 app. | 449 M18 |
| `reference_unit/` | `reference_candidate_qualifier.py` | Measure candidate 406MCA detectors through the production 406 MCA path (offset settling, 10 Hz response stabilization, repeatability, drive-hold drift, post-emitter recovery) and rank them for the permanently mounted AIN1 reference unit. `README.md` in the folder is the bench procedure. | 406 MCA (single rig) |

Each moved tool keeps its own docstring with the exact command lines. Tests:
`array_rig/m40623/tests/test_engineer_tools_array.py` (parity, replot) and
`single_detector_rig/m406mca/tests/test_reference_candidate_qualifier.py`
(reference-unit qualifier, simulated round trip).

Adding a tool: put it in the folder of the topic it belongs to (or a new
folder if none fits), set `REPO_ROOT = Path(__file__).resolve().parents[2]`
and import the production module it needs from the model directory, add a
row here and in `docs/DATA_MAP.md` §4 / `docs/ENGINEER_HANDOVER.md` §9, and
give it a test in the suite of the rig whose code it imports.
