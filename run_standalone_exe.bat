@echo off
cd /d "%~dp0"
if exist "dist\RTSP-Camera-Diagnostic-Portable.exe" (
  start "" "dist\RTSP-Camera-Diagnostic-Portable.exe"
  exit /b 0
)
if exist "dist\RTSP-Camera-Diagnostic.exe" (
  start "" "dist\RTSP-Camera-Diagnostic.exe"
  exit /b 0
)
if not exist "dist\RTSP-Camera-Diagnostic-Portable.exe" (
  echo Standalone EXE not found.
  echo Build it first using: build_standalone_exe.bat
  pause
  exit /b 1
)
