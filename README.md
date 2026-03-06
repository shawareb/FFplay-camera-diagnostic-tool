# RTSP Camera Frame Drop Diagnostic

Desktop GUI diagnostic tool for RTSP camera streams.

## Repository Contents

This GitHub repository includes:
- the current source code
- build scripts and assets
- the latest portable EXE: `dist\RTSP-Camera-Diagnostic-Portable.exe`

It does not include the offline runtime bundle (`ffmpeg` package copies / GStreamer installer bundle) because those files exceed normal GitHub repository file-size limits.

It runs a timed FFmpeg test, shows live frame/drop indicators, and generates:
- JSON report
- PDF report

Optional:
- FFplay live preview window during diagnostics
- PDF infographics (timeline charts, pie chart, warning chart, and stream snapshot)
- camera app icon + EXE icon

## Installed FFmpeg status

FFmpeg was installed on this PC using:

```powershell
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
```

If `ffmpeg` is not recognized in your current terminal session, open a new terminal window.
This app also auto-detects the Winget install path directly.

## Quick start

From the repository root:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Or double-click:

`run_diagnostic_tool.bat`

Run it from this repository root, or use `run_diagnostic_tool.bat`.

## How to use

1. Enter full RTSP URL.
2. Enter test duration in seconds.
3. Select RTSP transport mode:
   - `auto` (tries TCP, then UDP unicast, then UDP multicast)
   - `tcp`
   - `udp` (unicast)
   - `udp_multicast` (multicast)
4. Select engine:
   - `ffmpeg` for FFprobe metadata + FFmpeg timed diagnostics/reporting
   - `gstreamer` for GStreamer metadata + GStreamer timed diagnostics/reporting
5. (Optional) click **Start Live Preview**.
6. (Optional) enable auto-start live preview during test.
7. Choose report output folder (optional).
8. Click **Start Diagnostic**.
9. Watch live report values:
   - Frames Received / Drops
   - Bandwidth now + average
   - FPS jitter
   - Startup latency
   - RTP missed packet indicators
   - Selected transport + stream counts (total/video/audio)
   - Health score
   - Live charts for bandwidth, received frames, and estimated dropped frames
10. At completion, JSON and PDF reports are saved automatically.

## Run without Python installed

Build a standalone Windows executable on a machine that has Python:

```powershell
build_standalone_exe.bat
```

Then use:

- `dist\RTSP-Camera-Diagnostic-Portable.exe`

You can copy only the `.exe` to another Windows machine (Python is not required there).
For best compatibility on the other PC, keep FFmpeg binaries in:

- `C:\ffmpeg\bin\ffmpeg.exe`
- `C:\ffmpeg\bin\ffprobe.exe` (recommended for full metadata)
- `C:\ffmpeg\bin\ffplay.exe` (optional for live preview)

No-audio cameras are fully supported.
The diagnostic pipeline maps only the video stream for frame-drop analysis, so cameras with video-only RTSP still run without errors.

The window is responsive and scrollable for smaller desktops or low-height screens.
GStreamer preview is auto-detected from these common locations:

- `C:\Program Files\gstreamer\1.0\msvc_x86_64\bin`
- `C:\gstreamer\1.0\msvc_x86_64\bin`
- `gstreamer\1.0\msvc_x86_64\bin` next to the EXE

The app also sets the GStreamer plugin-scanner/plugin-path environment automatically when it launches GStreamer.

If your antivirus flags one-file EXEs, build a one-folder package instead:

```powershell
build_low_av_folder.bat
```

## Notes

- Why bitrate can be `N/A`:
  - FFmpeg `null` output often reports `bitrate=N/A` by design.
  - This tool now uses FFmpeg stream-copy telemetry (`total_size`) to estimate live bandwidth, so bandwidth should not stay `0 kbps` during active streaming.

- RTSP credentials with `@` in password:
  - Credentials that contain reserved URL characters can break URL parsing.
  - The app auto-encodes credentials internally (for example `@` -> `%40`) before launching FFmpeg/FFplay.

- `Estimated Drops` is calculated from: `(nominal FPS * elapsed) - received frames`.
- The timed analytics clock is capped to the requested duration so FFmpeg end-of-run timestamp overshoot does not create false tail-end drops.
- `FFmpeg Drops` comes directly from FFmpeg progress counters.
- Deep diagnostics include:
  - RTSP transport diagnostics (TCP/UDP unicast/UDP multicast probe results)
  - delivery type classification (unicast vs multicast)
  - requested engine vs actual validation/analytics backends
  - selected transport used in the run
  - stream inventory (stream number/index, type, codec, video/audio attributes)
  - bandwidth stats (avg/min/max/p95)
  - FPS stability + jitter
  - startup latency
  - freeze event detection
  - warning breakdown categories
  - RTP missed packet indicators
  - computed stream health score
  - GStreamer runtime details when GStreamer is selected:
    - decoder element
    - device context
    - progress phases
    - wall-clock drift vs media clock
    - tag-based bitrate telemetry
- PDF output includes:
  - color KPI cards
  - live dashboard graph from the app window
  - expected vs received frame chart
  - drop timeline chart
  - bandwidth distribution chart
  - media-clock vs wall-clock chart when available
  - frame distribution pie chart
  - warning category chart
  - one RTSP snapshot image
- `Open Last Report` opens the latest generated PDF when PDF export succeeds. If PDF export fails, it falls back to the JSON file.
- If the stream is unstable, warning lines are counted and sampled in the final report.
