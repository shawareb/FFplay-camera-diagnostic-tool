# Installation and Runtime Notes

## Mandatory

- Windows 10/11
- FFmpeg installed (`ffmpeg.exe`)

## Recommended

- `ffprobe.exe` for richer metadata.
- `ffplay.exe` for live preview.

## Optional

- GStreamer (`gst-launch-1.0` / `gst-play-1.0`) only if using GStreamer engine.

## FFmpeg-only compatibility

This application works correctly with FFmpeg only.
If GStreamer is unavailable, keep engine set to `ffmpeg`.

## Typical FFmpeg locations

- `C:\ffmpeg\bin\ffmpeg.exe`
- `C:\ffmpeg\bin\ffprobe.exe`
- `C:\ffmpeg\bin\ffplay.exe`

## Source setup

```powershell
python -m pip install -r requirements.txt
python app.py
```

## Build portable EXE

```powershell
build_standalone_exe.bat
```
