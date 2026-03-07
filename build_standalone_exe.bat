@echo off
setlocal
cd /d "%~dp0"

echo =====================================================
echo  RTSP Camera Diagnostic Tool - Portable EXE Builder
echo =====================================================
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

echo [3/3] Building portable single-file EXE...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --noupx ^
  --windowed ^
  --onefile ^
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
  --name "RTSP-Camera-Diagnostic-Portable" ^
  app.py

if errorlevel 1 (
  echo ERROR: EXE build failed. See output above for details.
  exit /b 1
)

echo.
echo [4/4] Preparing offline runtime bundle (FFmpeg required, GStreamer optional)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare_offline_runtime_bundle.ps1"
if errorlevel 1 (
  echo WARNING: Offline runtime bundle was not created. EXE is still built.
)

echo.
echo =====================================================
echo  Build COMPLETE!
echo  EXE: %~dp0dist\RTSP-Camera-Diagnostic-Portable.exe
echo =====================================================
endlocal
