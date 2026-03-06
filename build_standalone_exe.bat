@echo off
setlocal
cd /d "%~dp0"

echo Installing runtime dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install requirements.
  exit /b 1
)

echo Installing PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 (
  echo Failed to install PyInstaller.
  exit /b 1
)

echo Building standalone EXE...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --noupx ^
  --windowed ^
  --onefile ^
  --icon "assets\camera_icon.ico" ^
  --add-data "assets\camera_icon.png;assets" ^
  --add-data "assets\camera_icon.ico;assets" ^
  --name "RTSP-Camera-Diagnostic-Portable" ^
  app.py

if errorlevel 1 (
  echo EXE build failed.
  exit /b 1
)

echo Build completed.
echo EXE path: %~dp0dist\RTSP-Camera-Diagnostic-Portable.exe
endlocal
