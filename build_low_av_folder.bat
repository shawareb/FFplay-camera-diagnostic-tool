@echo off
setlocal
cd /d "%~dp0"

echo ====================================================
echo  RTSP Camera Diagnostic Tool - Folder Build
echo  (Lower AV false-positive risk than single-file EXE)
echo ====================================================
echo.

echo [1/3] Installing runtime dependencies...
python -m pip install -r requirements.txt --upgrade
if errorlevel 1 (
  echo ERROR: Failed to install requirements.
  exit /b 1
)

echo [2/3] Installing PyInstaller...
python -m pip install pyinstaller --upgrade
if errorlevel 1 (
  echo ERROR: Failed to install PyInstaller.
  exit /b 1
)

echo [3/3] Building one-folder EXE package...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --noupx ^
  --windowed ^
  --onedir ^
  --icon "assets\camera_icon.ico" ^
  --add-data "assets\camera_icon.png;assets" ^
  --add-data "assets\camera_icon.ico;assets" ^
  --hidden-import fpdf ^
  --hidden-import fpdf.enums ^
  --hidden-import matplotlib ^
  --hidden-import matplotlib.pyplot ^
  --hidden-import matplotlib.backends.backend_agg ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import PIL.ImageFont ^
  --name "RTSP-Camera-Diagnostic-Folder" ^
  app.py

if errorlevel 1 (
  echo ERROR: Build failed. See output above for details.
  exit /b 1
)

echo.
echo [4/4] Preparing offline runtime bundle (FFmpeg required, GStreamer optional)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare_offline_runtime_bundle.ps1"
if errorlevel 1 (
  echo WARNING: Offline runtime bundle was not created. Folder build is still complete.
)

echo.
echo ====================================================
echo  Build COMPLETE!
echo  Output folder: %~dp0dist\RTSP-Camera-Diagnostic-Folder\
echo  Run: %~dp0dist\RTSP-Camera-Diagnostic-Folder\RTSP-Camera-Diagnostic-Folder.exe
echo ====================================================
endlocal
