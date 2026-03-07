param(
    [string]$FfmpegInstallRoot = "C:\ffmpeg",
    [string]$GstreamerInstallRoot = "C:\gstreamer"
)

$ErrorActionPreference = "Stop"

function Add-MachinePathEntry {
    param([string]$Entry)
    if ([string]::IsNullOrWhiteSpace($Entry)) { return }
    if (-not (Test-Path $Entry)) { return }

    $current = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if ([string]::IsNullOrWhiteSpace($current)) {
        [Environment]::SetEnvironmentVariable("Path", $Entry, "Machine")
        return
    }

    $parts = $current.Split(";") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    if ($parts -contains $Entry) { return }

    $updated = ($parts + $Entry) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $updated, "Machine")
}

function Install-FromZip {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$DestinationRoot,
        [Parameter(Mandatory = $true)][string]$ValidationRelativePath
    )

    $tempDir = Join-Path $env:TEMP ("rtsp_diag_unpack_" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
    try {
        Expand-Archive -Path $ZipPath -DestinationPath $tempDir -Force

        $candidateRoots = @($tempDir)
        $childDirs = Get-ChildItem -Path $tempDir -Directory -ErrorAction SilentlyContinue
        foreach ($d in $childDirs) { $candidateRoots += $d.FullName }

        $selectedRoot = $null
        foreach ($root in $candidateRoots) {
            $checkPath = Join-Path $root $ValidationRelativePath
            if (Test-Path $checkPath) {
                $selectedRoot = $root
                break
            }
        }

        if (-not $selectedRoot) {
            throw "Package is invalid. Missing required file: $ValidationRelativePath in $ZipPath"
        }

        New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null
        robocopy $selectedRoot $DestinationRoot /MIR /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        if ($LASTEXITCODE -gt 7) {
            throw "Failed to copy package content to $DestinationRoot (robocopy exit code: $LASTEXITCODE)"
        }
    }
    finally {
        if (Test-Path $tempDir) {
            Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

$runtimeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$packagesRoot = Join-Path $runtimeRoot "packages"

if (-not (Test-Path $packagesRoot)) {
    throw "Packages folder not found: $packagesRoot"
}

$ffmpegZip = Get-ChildItem -Path $packagesRoot -Filter "ffmpeg*.zip" -File -ErrorAction SilentlyContinue | Select-Object -First 1
$gstZip = Get-ChildItem -Path $packagesRoot -Filter "gstreamer*.zip" -File -ErrorAction SilentlyContinue | Select-Object -First 1
$gstMsi = Get-ChildItem -Path $packagesRoot -Filter "gstreamer*.msi" -File -ErrorAction SilentlyContinue | Select-Object -First 1

if (-not $ffmpegZip -and -not (Test-Path (Join-Path $FfmpegInstallRoot "bin\ffmpeg.exe"))) {
    throw "FFmpeg package not found and ffmpeg is not already installed."
}

Write-Host "Offline runtime installer root: $runtimeRoot" -ForegroundColor DarkGray
Write-Host "Packages folder: $packagesRoot" -ForegroundColor DarkGray

if ($ffmpegZip) {
    Write-Host "Installing FFmpeg from: $($ffmpegZip.Name)" -ForegroundColor Cyan
    Install-FromZip -ZipPath $ffmpegZip.FullName -DestinationRoot $FfmpegInstallRoot -ValidationRelativePath "bin\ffmpeg.exe"
}
else {
    Write-Host "FFmpeg package not found. Existing install detected, keeping current FFmpeg." -ForegroundColor Yellow
}

if ($gstZip) {
    Write-Host "Installing GStreamer from ZIP: $($gstZip.Name)" -ForegroundColor Cyan
    Install-FromZip -ZipPath $gstZip.FullName -DestinationRoot $GstreamerInstallRoot -ValidationRelativePath "1.0\msvc_x86_64\bin\gst-launch-1.0.exe"
}
elseif ($gstMsi) {
    Write-Host "Installing GStreamer from MSI: $($gstMsi.Name)" -ForegroundColor Cyan
    $msiArgs = @("/i", "`"$($gstMsi.FullName)`"", "/qn", "/norestart")
    $proc = Start-Process -FilePath "msiexec.exe" -ArgumentList $msiArgs -PassThru -Wait
    if ($proc.ExitCode -ne 0) {
        throw "GStreamer MSI install failed with exit code $($proc.ExitCode)"
    }
}
else {
    Write-Host "GStreamer package not included. This is fine if you use FFmpeg engine only." -ForegroundColor Yellow
}

$ffmpegBin = Join-Path $FfmpegInstallRoot "bin"
$gstBin = Join-Path $GstreamerInstallRoot "1.0\msvc_x86_64\bin"
Add-MachinePathEntry -Entry $ffmpegBin
Add-MachinePathEntry -Entry $gstBin

$env:Path = "$ffmpegBin;$gstBin;$($env:Path)"

Write-Host ""
Write-Host "Runtime installation finished." -ForegroundColor Green
if (Test-Path (Join-Path $ffmpegBin "ffmpeg.exe")) {
    Write-Host "FFmpeg: OK ($ffmpegBin)"
}
else {
    Write-Host "FFmpeg: NOT FOUND after install." -ForegroundColor Red
}
if (Test-Path (Join-Path $gstBin "gst-launch-1.0.exe")) {
    Write-Host "GStreamer: OK ($gstBin)"
}
else {
    Write-Host "GStreamer: Not installed (optional)." -ForegroundColor Yellow
}
