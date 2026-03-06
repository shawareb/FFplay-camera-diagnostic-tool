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

echo Building one-folder EXE package (lower AV false-positive risk)...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --noupx ^
  --windowed ^
  --onedir ^
  --icon "assets\camera_icon.ico" ^
  --add-data "assets\camera_icon.png;assets" ^
  --add-data "assets\camera_icon.ico;assets" ^
  --name "RTSP-Camera-Diagnostic-Folder" ^
  app.py

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

echo Build completed.
echo Output folder: %~dp0dist\RTSP-Camera-Diagnostic-Folder\
endlocal
