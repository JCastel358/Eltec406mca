# Creates a Desktop shortcut for the Eltec 406MCA Emitter Tester, using the ELTEC
# logo as the icon. Builds a proper multi-size .ico from eltec_logo.png with the
# built-in Windows .NET imaging (System.Drawing) - no Pillow / pip install needed.
#
# Run once (right-click > "Run with PowerShell", or:
#   powershell -ExecutionPolicy Bypass -File ".\Create Desktop Shortcut.ps1").

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$launcher  = Join-Path $scriptDir 'Run 406MCA Emitter Tester.bat'

# Find the logo: script-local assets first, then an assets\ folder at any ancestor
# directory (the shared repo-root assets\ lives a few levels up).
$logoPng = $null
$searchDir = $scriptDir
while ($searchDir -and -not $logoPng) {
    $candidate = Join-Path $searchDir 'assets\eltec_logo.png'
    if (Test-Path $candidate) { $logoPng = $candidate }
    $searchDir = Split-Path -Parent $searchDir
}

function Convert-PngToIco {
    param([string]$PngPath, [string]$IcoPath)

    Add-Type -AssemblyName System.Drawing
    $src = [System.Drawing.Image]::FromFile($PngPath)
    try {
        $sizes  = @(256, 48, 32, 16)
        $images = New-Object System.Collections.Generic.List[byte[]]

        foreach ($size in $sizes) {
            $bmp = New-Object System.Drawing.Bitmap($size, $size, `
                [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $g = [System.Drawing.Graphics]::FromImage($bmp)
            $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
            $g.PixelOffsetMode   = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $g.Clear([System.Drawing.Color]::Transparent)

            # Fit the (non-square) logo onto the square canvas, keeping aspect ratio.
            $ratio = [Math]::Min($size / $src.Width, $size / $src.Height)
            $w = [int]($src.Width  * $ratio)
            $h = [int]($src.Height * $ratio)
            $x = [int](($size - $w) / 2)
            $y = [int](($size - $h) / 2)
            $g.DrawImage($src, $x, $y, $w, $h)
            $g.Dispose()

            $ms = New-Object System.IO.MemoryStream
            $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
            $bmp.Dispose()
            $images.Add($ms.ToArray())
            $ms.Dispose()
        }

        # Assemble the .ico container (each entry is a PNG frame, Vista+ compatible).
        $out = New-Object System.IO.MemoryStream
        $bw  = New-Object System.IO.BinaryWriter($out)
        $bw.Write([UInt16]0)              # reserved
        $bw.Write([UInt16]1)              # type = icon
        $bw.Write([UInt16]$images.Count)  # image count

        $offset = 6 + (16 * $images.Count)
        for ($i = 0; $i -lt $images.Count; $i++) {
            $size = $sizes[$i]
            $byteDim = if ($size -ge 256) { 0 } else { $size }  # 0 means 256 in ICO
            $bw.Write([byte]$byteDim)          # width
            $bw.Write([byte]$byteDim)          # height
            $bw.Write([byte]0)                 # palette color count
            $bw.Write([byte]0)                 # reserved
            $bw.Write([UInt16]1)               # color planes
            $bw.Write([UInt16]32)              # bits per pixel
            $bw.Write([UInt32]$images[$i].Length)
            $bw.Write([UInt32]$offset)
            $offset += $images[$i].Length
        }
        foreach ($img in $images) { $bw.Write($img) }
        $bw.Flush()
        [System.IO.File]::WriteAllBytes($IcoPath, $out.ToArray())
        $bw.Dispose(); $out.Dispose()
    }
    finally {
        $src.Dispose()
    }
}

# Build (or rebuild) the .ico next to the logo.
$iconPath = $null
if ($logoPng) {
    $icoPath = [System.IO.Path]::ChangeExtension($logoPng, '.ico')
    try {
        Convert-PngToIco -PngPath $logoPng -IcoPath $icoPath
        $iconPath = $icoPath
        Write-Host "Built icon: $icoPath"
    } catch {
        Write-Warning "Could not build .ico from the logo ($($_.Exception.Message))."
        if (Test-Path $icoPath) { $iconPath = $icoPath }  # reuse an existing one if present
    }
} else {
    Write-Warning "eltec_logo.png not found - the shortcut will use the default icon."
}

$desktop  = [Environment]::GetFolderPath('Desktop')
$lnkPath  = Join-Path $desktop 'Eltec 406MCA Emitter Tester.lnk'

$shell    = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.TargetPath       = $launcher
$shortcut.WorkingDirectory = $scriptDir
$shortcut.Description       = 'Eltec 406MCA Emitter Tester'
if ($iconPath) { $shortcut.IconLocation = "$iconPath,0" }
$shortcut.Save()

Write-Host "Created shortcut: $lnkPath"
Write-Host "  Target: $launcher"
if ($iconPath) { Write-Host "  Icon:   $iconPath" }
