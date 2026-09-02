<#
.SYNOPSIS
    Install Desktop and Start Menu shortcuts for the Eltec 405 M22 ESP32 Tester.

.DESCRIPTION
    Windows counterpart of install_xubuntu_launcher.sh. Creates per-user
    shortcuts only - nothing is written outside %USERPROFILE% and no admin
    rights are needed. Both shortcuts point at
    run_eltec_405m22_esp32_tester.cmd in this folder, so moving or updating the
    checkout keeps working as long as the shortcuts are reinstalled.

.PARAMETER Uninstall
    Remove the shortcuts this script created.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_windows_launcher.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_windows_launcher.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $ScriptDir 'run_eltec_405m22_esp32_tester.cmd'
$AppPath = Join-Path $ScriptDir 'eltec_405m22_esp32_tester.py'
$IconPath = Join-Path $ScriptDir 'assets\eltec_desktop_icon.ico'

# Same display identity as the Xubuntu launcher, so the two hosts look alike.
$DisplayName = 'Eltec 405 M22 ESP32 Tester'

$DesktopDir = [Environment]::GetFolderPath('Desktop')
$StartMenuDir = Join-Path ([Environment]::GetFolderPath('Programs')) 'Eltec'

$DesktopLink = Join-Path $DesktopDir "$DisplayName.lnk"
$StartMenuLink = Join-Path $StartMenuDir "$DisplayName.lnk"

if ($Uninstall) {
    foreach ($link in @($DesktopLink, $StartMenuLink)) {
        if (Test-Path $link) {
            Remove-Item -LiteralPath $link -Force
            "Removed: $link"
        } else {
            "Not present: $link"
        }
    }
    # Only clean up the Eltec folder when this script emptied it.
    if ((Test-Path $StartMenuDir) -and -not (Get-ChildItem -LiteralPath $StartMenuDir -Force)) {
        Remove-Item -LiteralPath $StartMenuDir -Force
        "Removed empty folder: $StartMenuDir"
    }
    exit 0
}

foreach ($required in @($Launcher, $AppPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required file not found: $required"
    }
}

if (-not (Test-Path $StartMenuDir)) {
    New-Item -ItemType Directory -Path $StartMenuDir -Force | Out-Null
}

$shell = New-Object -ComObject WScript.Shell
try {
    foreach ($link in @($DesktopLink, $StartMenuLink)) {
        $shortcut = $shell.CreateShortcut($link)
        # cmd /c keeps the launcher's exit code and its error popup intact while
        # closing the console window as soon as the GUI process is handed off.
        $shortcut.TargetPath = "$env:SystemRoot\System32\cmd.exe"
        $shortcut.Arguments = "/c `"`"$Launcher`"`""
        $shortcut.WorkingDirectory = $ScriptDir
        $shortcut.Description = 'Eltec Model 405 M22 (1 Hz) pyroelectric detector tester - ESP32/ADS1256 rig'
        $shortcut.WindowStyle = 7  # start minimized: the Tk window is the real UI
        if (Test-Path -LiteralPath $IconPath -PathType Leaf) {
            $shortcut.IconLocation = "$IconPath,0"
        }
        $shortcut.Save()
        "Installed: $link"
    }
} finally {
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}

""
"Launcher : $Launcher"
"Log file : $env:LOCALAPPDATA\eltec-405m22-esp32\launcher.log"
"Results  : $([Environment]::GetFolderPath('MyDocuments'))\Eltec_405M22_Test_Results\405m22_esp32"
