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
$runtimePackages = Join-Path $runtimeRoot "packages"
$folderExe = Join-Path $distRoot "RTSP-Camera-Diagnostic-Folder\RTSP-Camera-Diagnostic-Folder.exe"
$portableExe = Join-Path $distRoot "RTSP-Camera-Diagnostic-Portable.exe"

if (-not (Test-Path $runtimeInstaller)) {
    throw @"
Runtime installer not found:
$runtimeInstaller

Fix:
1) Rebuild using build_standalone_exe.bat (or build_low_av_folder.bat)
2) Confirm this folder exists in dist:
   offline-runtime-packages\RTSP-Diagnostic-Runtimes-Offline\
"@
}

if (-not (Test-Path $runtimePackages)) {
    throw @"
Runtime package folder not found:
$runtimePackages

Fix:
1) Run prepare_offline_runtime_bundle.ps1 in the project root
2) Copy the full dist folder again to the other PC
"@
}

Write-Host "Installing GStreamer and FFmpeg runtime files..." -ForegroundColor Cyan
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runtimeInstaller

Write-Host ""
Write-Host "Installation completed." -ForegroundColor Green
Write-Host "Recommended app: $folderExe"
Write-Host "Portable app:   $portableExe"
Write-Host ""
Write-Host "You can now open either EXE."
