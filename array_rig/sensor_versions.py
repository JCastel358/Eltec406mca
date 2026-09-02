"""
Sensor-version registry for the Eltec ARRAY test rig (50 positions, DAQ).

The mirror of ``single_detector_rig/sensor_versions.py`` for the second rig.
One entry per detector model the array PCB can hold. The selector GUI
(``eltec_array_tester.py``) builds its dropdown from ``SENSOR_VERSIONS`` and
launches the selected entry's application as a subprocess, so every model
keeps its own qualified flow, limits, CSV format and results folder.

Adding a new model later
------------------------
1. Copy the closest model directory next to ``m40623`` (``daq_backend.py``,
   ``array_analysis.py``, the tester, the probe, launchers, tests) and adapt
   its spec constants - the copy-per-model rule of the single rig applies
   here too, and nothing is ever shared with ``single_detector_rig/``.
   The entry-point script must run standalone with the directory as cwd.
2. Append a ``SensorVersion`` entry below. The dropdown, persistence and the
   glue tests pick it up automatically (``tests/test_eltec_array_rig.py``
   lists the expected keys - update it).
3. Add the suite to ``run_all_tests.py``, a section to
   ``docs/CALIBRATION_RECORD.md`` (CALIBRATION PENDING until a paired lot
   exists - the 40623 shows the pattern) and the results root to
   ``docs/DATA_MAP.md``.

The two rigs each have a module called ``sensor_versions``; they are never
imported into one process (each rig's tests run in their own interpreter).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# The hardware every entry below runs on. There is no firmware to flash: the
# DAQ loads its own firmware from the ACCES driver package at plug-in.
REQUIRED_HARDWARE = (
    "ACCES USB-AIO16-64MA DAQ-PACK (VID 0x1605 PID 0x8145) with the ACCES "
    "'USB-AIO16-64MA Install' driver package (AIOUSB.dll, 64-bit) and the "
    "50-position buffer PCB on CH0-CH49"
)


@dataclass(frozen=True)
class SensorVersion:
    key: str                 # stable id (saved as the remembered selection)
    display_name: str        # dropdown text
    summary: str             # one-line description under the dropdown
    details: tuple[str, ...] # bullet lines shown in the description panel
    app_dir: str             # directory relative to this package
    app_script: str          # entry-point script inside app_dir
    results_note: str        # where this model's trays are stored

    @property
    def app_dir_path(self) -> Path:
        return PACKAGE_DIR / self.app_dir

    @property
    def app_script_path(self) -> Path:
        return self.app_dir_path / self.app_script


SENSOR_VERSIONS: tuple[SensorVersion, ...] = (
    SensorVersion(
        key="40623",
        display_name="Model 40623 array (50 positions, TP120)",
        summary=(
            "Model 40623 detectors, fifty at a time: TP120 offset check and noise "
            "test on the DAQ array rig. CALIBRATION PENDING."
        ),
        details=(
            "Flow: power on -> live offsets (high offsets turn red: pull them) -> lock the "
            "tray -> 5 min stabilisation -> 60 s noise capture -> save.",
            "Offset: TP120 0.3-1.2 V, PROVISIONAL until the PCB loading is confirmed "
            "against fixture 9000054 (+8 V, 100 kOhm).",
            "Noise: measured at the pin in the single rig's 0.85-22 Hz band and recorded; "
            "no pin-level limit yet (TP120's 10.0-37.9 mV are DMM readings behind the "
            "legacy amplifier + rectifier-hold) - tiles show 'no limit yet'.",
            "The raw 1000 SPS capture of all 50 channels is saved with every tray so the "
            "limits can be derived later without re-measuring.",
            "Sensitivity / polarity (3 Hz chopper) is not implemented yet: no emitter board.",
        ),
        app_dir="m40623",
        app_script="eltec_40623_array_tester.py",
        results_note="Documents/Eltec_40623_Test_Results/40623_array_daq",
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
