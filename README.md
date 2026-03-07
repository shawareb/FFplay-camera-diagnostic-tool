# RTSP Camera Frame Drop Diagnostic

Windows desktop tool to test RTSP camera stream quality, monitor live metrics, and generate shareable PDF and JSON diagnostics.

## What is improved in this version

- Refreshed modern GUI styling with stronger visual hierarchy.
- Cleaner metric readability and action buttons.
- Improved live chart visuals.
- Updated documentation for source builds and portable EXE distribution.

## Key Features

- Live metrics: received frames, expected frames, estimated drops, FFmpeg drops/dups, bandwidth, jitter, startup latency, health score.
- Transport probing: TCP, UDP, and UDP multicast checks.
- Engine support:
  - FFmpeg (default and recommended)
  - GStreamer (optional)
- Report outputs:
  - JSON report
  - Color PDF report with charts and stream summary
- Optional live preview window.

## Runtime Requirements

- OS: Windows 10/11
- FFmpeg: required
  - `ffmpeg.exe` required for diagnostics
  - `ffprobe.exe` recommended for better stream metadata
  - `ffplay.exe` optional for live preview
- GStreamer: optional
  - Only required if you choose GStreamer engine/preview

Important behavior:

- FFmpeg-only installation works fine.
- If GStreamer is not installed, keep engine set to `ffmpeg`.

## Install FFmpeg (recommended)

```powershell
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
```

Then reopen terminal/session.

The app can detect FFmpeg from PATH and common local locations (including `C:\ffmpeg\bin`).

## Quick Start

### Option A: Run from source

```powershell
python -m pip install -r requirements.txt
python app.py
```

Or run `run_diagnostic_tool.bat`.

### Option B: Run portable EXE

Use:

```text
dist\RTSP-Camera-Diagnostic-Portable.exe
```

Python is not required on target PCs for EXE usage.

## Build Portable EXE

```powershell
build_standalone_exe.bat
```

Output:

```text
dist\RTSP-Camera-Diagnostic-Portable.exe
```

## Lower AV False-Positive Option

If one-file EXE is flagged by antivirus heuristics, build one-folder distribution:

```powershell
build_low_av_folder.bat
```

Use `dist\RTSP-Camera-Diagnostic-Folder\RTSP-Camera-Diagnostic-Folder.exe`.

## How to Run a Diagnostic

1. Enter RTSP URL.
2. Set test duration.
3. Choose transport (`auto`, `tcp`, `udp`, `udp_multicast`).
4. Choose engine (`ffmpeg` recommended).
5. Optional: start preview.
6. Click `Start Diagnostic`.
7. Open generated PDF/JSON from report folder.

## Distribution to Other PCs

When sharing to another machine:

1. Share `dist\RTSP-Camera-Diagnostic-Portable.exe` (or full `dist` bundle).
2. Ensure FFmpeg is installed on that target PC.
3. GStreamer is optional and only needed for GStreamer engine mode.

Recommended FFmpeg binary locations:

```text
C:\ffmpeg\bin\ffmpeg.exe
C:\ffmpeg\bin\ffprobe.exe
C:\ffmpeg\bin\ffplay.exe
```

## Repository Structure

```text
app.py
requirements.txt
build_standalone_exe.bat
build_low_av_folder.bat
run_diagnostic_tool.bat
run_standalone_exe.bat
assets/
docs/
dist/
```

## Notes for Contributors

- Keep GUI changes in `app.py` focused on readability and operator workflow.
- Validate both source run and EXE run before release.
- If updating release artifacts, rebuild EXE and test on a clean machine if possible.
