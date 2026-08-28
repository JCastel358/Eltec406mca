"""
Sensor-version registry for the unified Eltec test rig.

One entry per testable sensor model. The selector GUI
(``eltec_rig_tester.py``) builds its dropdown from ``SENSOR_VERSIONS`` and
launches the selected entry's application as a subprocess, so every model
keeps its own qualified test flow, thresholds, filter-setup dropdowns, CSV
format, and results folder exactly as validated.

Adding a new sensor version later
---------------------------------
1. Create a new application directory next to ``m405m22``/``m406mca`` (copy
   the closest existing model and adapt its spec constants, or start fresh).
   Its entry-point script must run standalone with the directory as cwd.
2. Append a ``SensorVersion`` entry below pointing at that script. The
   dropdown, persistence, and launcher tests pick it up automatically.
3. If the model needs new firmware behavior (another PWM frequency, another
   ADS1256 front end, a new channel), extend ``Arduino/Eltec/Eltec.ino`` with
   a runtime command the app sends after connect - do NOT fork the firmware:
   the whole point of the unified v3.x baseline is that ONE build serves
   every model (the 405 M22 uses the boot defaults; the 406 MCA sends
   ``FE,V19``; the 449 M18 sends ``PWM,DUTY``). Bump ``REQUIRED_FIRMWARE``
   below and the model's ``MINIMUM_FIRMWARE_VERSION`` when a new command is
   required.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# The firmware the shared bench board should run for this app. Every entry
# below works on it; per-model needs are selected at runtime over serial.
REQUIRED_FIRMWARE = (
    "v3.2 (PWM,DUTY for the 449 M18's 20/80 drive; the 405 M22 and 406 MCA "
    "modes also run on v2.1-v3.1, the app sends PIN,<n> itself)"
)


@dataclass(frozen=True)
class SensorVersion:
    key: str                 # stable id (saved as the remembered selection)
    display_name: str        # dropdown text
    summary: str             # one-line description under the dropdown
    details: tuple[str, ...] # bullet lines shown in the description panel
    app_dir: str             # directory relative to this package
    app_script: str          # entry-point script inside app_dir
    results_note: str        # where this model's batches are stored

    @property
    def app_dir_path(self) -> Path:
        return PACKAGE_DIR / self.app_dir

    @property
    def app_script_path(self) -> Path:
        return self.app_dir_path / self.app_script


SENSOR_VERSIONS: tuple[SensorVersion, ...] = (
    SensorVersion(
        key="405m22",
        display_name="Model 405 M22 (1 Hz, TP412)",
        summary="High-gain pyroelectric detector, specified at 1 Hz, tested per TP412.",
        details=(
            "Emitter: 1 Hz / 50% PWM for the DUT; the AIN1 reference phases run at 10 Hz.",
            "Flow: offset fail-fast → reference gate → emitter-off noise test → sensitivity/polarity.",
            "Filter setups (-625 / -628 / -629) are chosen per batch inside the app.",
            "Lot-500 calibration applied: sensitivity factor 4.30, 15% noise window allowance.",
            "ADS1256 front end: firmware boot default (gain 1, buffer off; offsets read to ~5 V).",
        ),
        app_dir="m405m22",
        app_script="eltec_405m22_esp32_tester.py",
        results_note="Documents/Eltec_405M22_Test_Results/405m22_esp32",
    ),
    SensorVersion(
        key="406mca",
        display_name="Model 406 MCA (10 Hz)",
        summary="406MCA detector on the v6.1 adaptive-stability test flow.",
        details=(
            "Emitter: 10 Hz / 50% PWM (firmware boot default) for reference and DUT.",
            "Flow: reference gate → offset → three-attempt 10/20 adaptive stability capture.",
            "Filter setups are chosen per batch inside the app.",
            "ADS1256 front end: the app sends FE,V19 after connect to restore the qualified",
            "gain-2 buffered front end (±2.5 V) the 406MCA thresholds were validated on.",
        ),
        app_dir="m406mca",
        app_script="eltec_406mca_esp32_tester.py",
        results_note="Documents/Eltec_406MCA_Test_Results/v6_1_esp32",
    ),
    SensorVersion(
        key="449m18",
        display_name="Model 449 M18 (5 Hz + 18 Hz, TP443)",
        summary=(
            "449M18 frequency tracking per TP443: sensitivity at 5 Hz and at 18 Hz, "
            "then the 18/5 ratio."
        ),
        details=(
            "Emitter: 5 Hz then 18 Hz, both 20% ON / 80% OFF (the legacy fixture's 20/80 "
            "blade); the app sends PWM,FREQ + PWM,DUTY (firmware v3.2 required).",
            "Flow: offset read → 5 Hz capture → 18 Hz capture → ratio 18/5 → TP443 specs 1-4.",
            "Limits: ≥ 1.2 V at 5 Hz, ≥ 0.72 V at 18 Hz, ratio 0.70-1.30, ratio ≤ 0.72 flags",
            "the tray for 100% measurement — applied on legacy-equivalent values (raw × a",
            "per-frequency fixture factor). CALIBRATION PENDING: factors not derived yet, so",
            "verdicts record raw readings + raw ratio and the limits are not enforced.",
            "ADS1256 front end: firmware boot default (gain 1, buffer off).",
        ),
        app_dir="m449m18",
        app_script="eltec_449m18_esp32_tester.py",
        results_note="Documents/Eltec_449M18_Test_Results/449m18_esp32",
    ),
)

DEFAULT_VERSION_KEY = SENSOR_VERSIONS[0].key


def get_version(key: str) -> SensorVersion:
    for version in SENSOR_VERSIONS:
        if version.key == key:
            return version
    raise KeyError(f"Unknown sensor version key: {key!r}")


def version_keys() -> tuple[str, ...]:
    return tuple(version.key for version in SENSOR_VERSIONS)
