# Eltec Test Rig Xubuntu fleet setup

Use a clean Xubuntu installation plus the scripts in this repository. This is
safer and easier to update than cloning a configured disk: a disk image can also
clone machine IDs, usernames, credentials, old results, and the wrong fixture's
reference calibration.

The installer is safe to rerun. It installs the production **Eltec Test Rig**
launcher, whose sensor-version selector starts either the Model 405 M22 or
Model 406 MCA qualified workflow. Legacy standalone launchers remain isolated.

## Choose the Xubuntu release

For new 64-bit PC installations, use **Xubuntu 26.04 LTS Desktop (amd64)** after
one pilot computer passes the software suite and a real-fixture test. It is the
current Xubuntu LTS and is supported through April 2029. The inspected working
computer is Ubuntu 24.04.4 with Python 3.12, so **Xubuntu 24.04 LTS** is the
closest already-exercised fallback, but Xubuntu's support for it ends in April
2027.

- [Official Xubuntu 26.04 download and support information](https://xubuntu.org/release/26.04/)
- [Official Xubuntu 26.04 release notes](https://xubuntu.org/releasedocs/26.04/release-notes/)
- [Official Xubuntu 24.04 download and support information](https://xubuntu.org/release/24.04/)

Use the Desktop image, an amd64 computer, and a **1920×1080 display at 100%
scale** as the fleet baseline. The tester's 1100×740 minimum client area is
borderline on 1366×768 after XFCE's panel and window decorations.

## Before erasing an existing station

Test results and the reference calibration are outside the Git repository, so
an application update preserves them. An OS reimage does not. Back them up to a
mounted USB or network location first:

```bash
cd "$HOME/Eltec406mca"
./backup_eltec_results.sh "/media/$USER/ELTEC_BACKUP"
```

Wait for the success message and safely eject/unmount removable media before
erasing the computer.

The archive includes both `~/Documents/Eltec_405M22_Test_Results` and
`~/Documents/Eltec_406MCA_Test_Results` plus timestamped station metadata: the
hostname, a hashed machine identity, application commit, install record, and
deployment history. A reference-calibration file belongs to the same physical
fixture, reference sensor, and emitter assembly; never copy one fixture's
calibration to a different fixture.

After reinstalling Xubuntu and rerunning setup, restore the archive. The safe
default restores CSVs/snapshots but excludes fixture calibration:

```bash
./restore_eltec_results.sh "/media/$USER/ELTEC_BACKUP/eltec-test-rig-results-TIME.tar.gz"
```

Only for the same physical fixture/reference/emitter assembly, preserve its
calibration with:

```bash
./restore_eltec_results.sh --same-fixture "/media/$USER/ELTEC_BACKUP/eltec-test-rig-results-TIME.tar.gz"
```

Restore verifies the sibling SHA-256 file and refuses to overwrite any existing
result. The backup command requires an explicit destination so an operator does
not accidentally keep the only backup on the disk being erased.

## Install a fresh computer

Install Xubuntu, create the normal technician account, sign in, connect to the
internet, and open Terminal. Do not run the Eltec setup itself with `sudo`.
Use the same technician username on every station when practical; snapshot
paths recorded in CSV files are absolute and are easier to compare that way.

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates
git clone https://github.com/JCastel358/Eltec406mca.git "$HOME/Eltec406mca"
cd "$HOME/Eltec406mca"
git checkout --detach "APPROVED_FULL_COMMIT_OR_RELEASE_TAG"
./setup_xubuntu.sh
```

Replace `APPROVED_FULL_COMMIT_OR_RELEASE_TAG` with the reviewed revision sent
with the fleet release. Do not leave several production stations following a
mutable branch if they must remain identical. A one-off engineering pilot may
test the current `main` with `./setup_xubuntu.sh --allow-mutable-checkout`, but
record the resulting commit before comparing data. Normal setup refuses a
mutable branch checkout.

The first two package commands are only the bootstrap needed to download the
repository. `setup_xubuntu.sh` then installs and verifies the complete runtime:

- Python 3, Tk, NumPy, pyserial, and Matplotlib;
- desktop notifications, launcher validation, XDG helpers, and USB diagnostics;
- `dialout` serial-port access for the signed-in technician;
- the unified Eltec Test Rig desktop and Applications-menu launchers;
- both model-specific result locations;
- Python compilation plus 282 unified/model tests (9 display/Windows-only
  skips on the headless Xubuntu check) and the provisioning safety suite;
- a local installation record containing the OS and Git commit.

Reruns install only missing named packages; they do not upgrade the station's
existing Python stack as a side effect of an application release. Normal OS
security updates remain a separate maintenance-window responsibility. The
installation record captures the exact installed package versions for audits.
The first installation requires internet access or an organization-managed
Ubuntu package mirror. The small Git bundles described below are application
updates; they are not a complete offline Xubuntu/package image.

The full repository must remain together. The production selector bundles both
model applications and their shared analysis code under `tech_app/eltec_rig`;
copying only an individual entry-point file is not a complete installation.

To preview the setup without changing the computer:

```bash
./setup_xubuntu.sh --dry-run
```

To also install the legacy standalone v6.1 launcher on an engineering station:

```bash
./setup_xubuntu.sh --with-experimental
```

Production stations should use the default command and get only the unified
launcher. Rerunning the default also removes this repository's old standalone
v6, v6.1, and v6.1 failure-calibration launchers if installed previously.

## First login and fixture check

If setup added the account to `dialout`, sign out and back in or reboot. Then
connect the ESP32 fixture with a good USB data cable and close Arduino Serial
Monitor or any other serial application.

```bash
cd "$HOME/Eltec406mca"
./doctor_xubuntu.sh --gui --hardware
```

The GUI option opens and closes the unified selector in the technician's
logged-in XFCE session without starting a measurement. The hardware option
opens and resets the ESP32 through both model backends: Model 405 M22 verifies
the gain-1/unbuffered firmware baseline and Model 406 MCA verifies the runtime
`FE,V19` gain-2/buffered switch. It always closes with PWM forced off. Battery
monitoring is currently disabled because the unified fixture no longer has a
valid monitored battery divider. This is a safe serial/firmware/front-end
check, not a full fixture acceptance. The tracked unified firmware is v3.0;
v2.1 also supports both models. A tester computer does not need Arduino IDE/CLI
for daily work; boards should arrive already flashed. Firmware flashing remains
a separate engineering procedure using `Arduino/Eltec/Eltec.ino` and board
target `esp32:esp32:esp32doit-devkit-v1`.

Launch **Eltec Test Rig** from the desktop, choose the sensor model, and follow
that model's batch screen. Before production DUTs, complete every calibration
or known-good step that model currently enables. Historical numbers in
`status.md` must not be installed as a baseline. Record one known-good DUT for
each model used on the station; this exercises the model-specific front end,
AIN inputs, PWM cadence, stream integrity, and result writing.

## Update stations

Close the tester first. With internet access:

```bash
cd "$HOME/Eltec406mca"
./update_xubuntu.sh --revision "APPROVED_FULL_COMMIT_OR_RELEASE_TAG"
```

The updater preserves untracked files and all external results, refuses to
overwrite tracked local edits, and accepts only a fast-forward update. It first
stages the incoming commit in a temporary Git worktree, prepares any declared
Ubuntu packages, and runs the selector, both model, and provisioning suites. Only a verified
candidate is merged into the live checkout; its launcher is then installed.
Online mode rejects mutable branch names and requires the full reviewed commit
SHA or an existing release tag. The exact SHA/tag is fetched from the selected
remote, so a different commit that merely exists in the local object database
cannot be activated.

This repository does not yet have production release tags. Create an annotated
tag for the first fleet release or distribute its full 40-character commit SHA;
do not run unattended automatic updates on test stations.

### Send an update by USB

On the source computer, first commit the reviewed changes and check out the
approved commit or tag. Then create a pinned Git bundle, manifest, and checksum:

```bash
cd "$HOME/Eltec406mca"
./make_update_bundle.sh
```

The builder prints the full approved commit and an exact station command. Copy
all three generated files (`.bundle`, `.bundle.manifest`, and `.bundle.sha256`)
to USB. On each station, use that printed commit SHA:

```bash
cd "$HOME/Eltec406mca"
./update_xubuntu.sh \
  --bundle "/media/$USER/USB/eltec-test-rig-update-COMMIT.bundle" \
  --revision "APPROVED_FULL_40_CHARACTER_COMMIT_SHA"
```

The station verifies the bundle and manifest hashes, Git object integrity, the
manifest's pinned commit, and the separately entered full SHA. It then requires
a fast-forward and performs the same setup and tests as an online update. USB
bundles skip `apt-get` automatically so application-only updates work without
internet. If a reviewed release adds an Ubuntu package, connect that station to
its package mirror and add `--refresh-apt` to the bundle update command.

The sibling checksum detects accidental corruption; it does not authenticate a
publisher because all three files travel together. Distribute bundles through a
trusted channel and separately communicate the approved full commit SHA or use
a reviewed signed release tag.

### Validate and, if needed, roll back

After every update, run `./doctor_xubuntu.sh --gui --hardware`, then complete
the relevant in-app calibration/known-good fixture checks before releasing the station back
to production. If software passed but the real fixture rejects the release,
return to the commit recorded immediately before the update:

```bash
cd "$HOME/Eltec406mca"
./rollback_xubuntu.sh --last
```

Rollback first verifies the older checkout and its software suites, switches
to that detached commit, and repairs the launcher. Results and fixture
calibration stay untouched. Afterward, repeat the GUI/hardware and known-good
fixture checks. A release manager can instead name a reviewed older full SHA or
local tag with `--revision`; unrelated commits and branch names are rejected.

## Routine checks and logs

Software-only health check:

```bash
./doctor_xubuntu.sh
```

Check a particular connected port:

```bash
./doctor_xubuntu.sh --port /dev/ttyUSB0
```

Relevant locations:

```text
405 M22 results:     ~/Documents/Eltec_405M22_Test_Results/405m22_esp32/
406 MCA results:     ~/Documents/Eltec_406MCA_Test_Results/v6_1_esp32/
Launcher log:        ~/.local/state/eltec-rig/launcher.log
Install record:      ~/.local/state/eltec-rig/install-info.txt
Deployment history:  ~/.local/state/eltec-rig/deployment-history.log
Rollback record:      ~/.local/state/eltec-rig/rollback-target.txt
Menu launcher:       ~/.local/share/applications/com.eltec.test-rig.desktop
```

No LabJack driver, custom udev rule, system service, Pillow, database, or network
connection is needed for daily unified-rig testing. Linux's standard USB serial drivers
support the fixture's CP210x bridge. Avoid USB hubs and charge-only cables.

## Production review items not changed by provisioning

Provisioning reproduces the current code; it does not change measurement
policy. Before declaring a fleet release, resolve these repository
inconsistencies with the test owner:

- Model 405 M22's reference gate is currently disabled until the fixture has
  channel-isolated buffering; follow the operator/emitter checks in its README.
- Confirm the active Model 406 MCA reference, sensitivity, SNR, and stability
  policies against the latest qualification evidence in `status.md`.

The scripts intentionally leave those engineering decisions unchanged.
