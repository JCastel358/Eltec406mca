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
   the whole point of the v3.0 baseline is that ONE build serves every model
   (the 405 M22 uses the boot defaults; the 406 MCA sends ``FE,V19``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# The firmware the shared bench board should run for this app. Every entry
# below works on it; per-model needs are selected at runtime over serial.
REQUIRED_FIRMWARE = "v3.0 (v2.1/v2.2 also work; v2.0 and older lack the FE commands the 406 MCA path needs)"


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
)

DEFAULT_VERSION_KEY = SENSOR_VERSIONS[0].key


def get_version(key: str) -> SensorVersion:
    for version in SENSOR_VERSIONS:
        if version.key == key:
            return version
    raise KeyError(f"Unknown sensor version key: {key!r}")


def version_keys() -> tuple[str, ...]:
    return tuple(version.key for version in SENSOR_VERSIONS)
