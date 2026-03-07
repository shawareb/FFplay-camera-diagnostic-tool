param(
    [string]$RepoRoot = $PSScriptRoot,
    [string]$FfmpegSourceRoot = "C:\ffmpeg",
    [string]$GstreamerSourceRoot = "C:\Program Files\gstreamer\1.0\msvc_x86_64"
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host $Text -ForegroundColor Cyan
}

function Ensure-ZipFromFolder {
    param(
        [Parameter(Mandatory = $true)][string]$SourceFolder,
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$ValidationRelativePath
    )

    if (-not (Test-Path (Join-Path $SourceFolder $ValidationRelativePath))) {
        throw "Required file missing: $(Join-Path $SourceFolder $ValidationRelativePath)"
    }

    if (Test-Path $ZipPath) {
        Remove-Item -Path $ZipPath -Force
    }

    Compress-Archive -Path (Join-Path $SourceFolder "*") -DestinationPath $ZipPath -CompressionLevel Optimal -Force
}

$templateRoot = Join-Path $RepoRoot "offline-runtime-packages\RTSP-Diagnostic-Runtimes-Offline"
$distRuntimeRoot = Join-Path $RepoRoot "dist\offline-runtime-packages\RTSP-Diagnostic-Runtimes-Offline"
$distPackages = Join-Path $distRuntimeRoot "packages"

if (-not (Test-Path $templateRoot)) {
    throw "Template runtime folder not found: $templateRoot"
}

Write-Step "Preparing dist offline runtime folder..."
New-Item -ItemType Directory -Force -Path $distPackages | Out-Null
Copy-Item -Path (Join-Path $templateRoot "Install-Runtimes.ps1") -Destination (Join-Path $distRuntimeRoot "Install-Runtimes.ps1") -Force
Copy-Item -Path (Join-Path $templateRoot "README.txt") -Destination (Join-Path $distRuntimeRoot "README.txt") -Force

Write-Step "Bundling FFmpeg from $FfmpegSourceRoot ..."
$ffmpegZip = Join-Path $distPackages "ffmpeg-local-offline.zip"
Ensure-ZipFromFolder -SourceFolder $FfmpegSourceRoot -ZipPath $ffmpegZip -ValidationRelativePath "bin\ffmpeg.exe"
Write-Host "Created: $ffmpegZip" -ForegroundColor Green

if (Test-Path (Join-Path $GstreamerSourceRoot "bin\gst-launch-1.0.exe")) {
    Write-Step "Bundling GStreamer from $GstreamerSourceRoot ..."
    $tempGstRoot = Join-Path $env:TEMP ("rtsp_diag_gst_" + [Guid]::NewGuid().ToString("N"))
    $tempLayout = Join-Path $tempGstRoot "1.0\msvc_x86_64"
    New-Item -ItemType Directory -Force -Path $tempLayout | Out-Null
    try {
        robocopy $GstreamerSourceRoot $tempLayout /MIR /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        if ($LASTEXITCODE -gt 7) {
            throw "Failed to stage GStreamer files (robocopy exit code: $LASTEXITCODE)"
        }
        $gstZip = Join-Path $distPackages "gstreamer-msvc_x86_64-local-offline.zip"
        if (Test-Path $gstZip) {
            Remove-Item -Path $gstZip -Force
        }
        Compress-Archive -Path (Join-Path $tempGstRoot "*") -DestinationPath $gstZip -CompressionLevel Optimal -Force
        Write-Host "Created: $gstZip" -ForegroundColor Green
    }
    finally {
        if (Test-Path $tempGstRoot) {
            Remove-Item -Path $tempGstRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
else {
    Write-Host ""
    Write-Host "GStreamer source not found. FFmpeg-only offline bundle created." -ForegroundColor Yellow
}

Write-Step "Offline runtime bundle is ready."
Write-Host "Copy this full folder to target PC:" -ForegroundColor Gray
Write-Host (Join-Path $RepoRoot "dist") -ForegroundColor Gray
