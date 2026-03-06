param()

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    $scriptPath = $MyInvocation.MyCommand.Path
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
    exit
}

$distRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeRoot = Join-Path $distRoot "offline-runtime-packages\RTSP-Diagnostic-Runtimes-Offline"
$runtimeInstaller = Join-Path $runtimeRoot "Install-Runtimes.ps1"
$folderExe = Join-Path $distRoot "RTSP-Camera-Diagnostic-Folder\RTSP-Camera-Diagnostic-Folder.exe"
$portableExe = Join-Path $distRoot "RTSP-Camera-Diagnostic-Portable.exe"

if (-not (Test-Path $runtimeInstaller)) {
    throw "Runtime installer not found: $runtimeInstaller"
}

Write-Host "Installing GStreamer and FFmpeg runtime files..." -ForegroundColor Cyan
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runtimeInstaller

Write-Host ""
Write-Host "Installation completed." -ForegroundColor Green
Write-Host "Recommended app: $folderExe"
Write-Host "Portable app:   $portableExe"
Write-Host ""
Write-Host "You can now open either EXE."
