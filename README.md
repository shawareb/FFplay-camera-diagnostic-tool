# RTSP Camera Frame Drop Diagnostic

> **A Windows desktop tool for checking the health of CCTV and IP cameras over your network.**
> It runs a timed FFmpeg or GStreamer test, shows live metrics while the test runs, and produces a
> colour-coded PDF + JSON report you can share or archive.

---

## Screenshots

### Main window (idle)

![App main window – idle state](docs/screenshots/app-main-window.png)

### Running a live diagnostic

![App during a diagnostic run](docs/screenshots/app-running.png)

### Sample diagnostic report

![Sample PDF/report output](docs/screenshots/report-sample.png)

---

## What it does

| Area | Details |
|---|---|
| **Live metrics** | Frames received, estimated drops, FFmpeg-reported drops & duplicates, bandwidth (now + avg), FPS jitter, startup latency, missed RTP packets, stream health score |
| **Transport probing** | Tests TCP, UDP unicast, and UDP multicast before the run so you know which paths work |
| **Engines** | FFmpeg (default) or GStreamer – switch with one click |
| **Reports** | JSON file + colour PDF with KPI cards, timeline chart, expected-vs-received frame chart, drop-rate chart, bandwidth distribution chart, frame-distribution pie chart, warning category chart, and a live RTSP snapshot image |
| **Live preview** | Optional FFplay (or GStreamer) side-by-side preview window during the test |
| **No-audio cameras** | Fully supported – the pipeline maps the video stream only, so video-only RTSP streams work without errors |
| **Credentials with special characters** | `@` and other reserved characters inside passwords are automatically URL-encoded before FFmpeg/FFplay are launched |

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Windows 10 / 11** | The GUI uses Tkinter; a pre-built `.exe` is provided so Python is optional on target machines |
| **FFmpeg** (ffmpeg, ffprobe, ffplay) | Install with `winget` (see below) or place binaries at `C:\ffmpeg\bin\` |
| **Python 3.11+** *(source only)* | Not required if you run the pre-built EXE |
| **GStreamer** *(optional)* | Only needed if you want to use the GStreamer engine or preview |

### Install FFmpeg with winget (recommended)

```powershell
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
```

Open a **new** terminal after installation so `ffmpeg` appears on `PATH`.
The app also auto-detects the Winget install path directly, so you do not need to set `PATH` manually.

---

## Quick start

### Option A – Run from source

```powershell
# 1. Clone or download this repository
# 2. Install Python dependencies
python -m pip install -r requirements.txt

# 3. Launch the app
python app.py
```

Or simply double-click **`run_diagnostic_tool.bat`**.

### Option B – Run the pre-built EXE

The repository ships a ready-to-run portable EXE:

```
dist\RTSP-Camera-Diagnostic-Portable.exe
```

No Python installation required on the machine running the EXE.
FFmpeg binaries must still be present (see [Prerequisites](#prerequisites)).

---

## How to run a diagnostic

1. **Enter the full RTSP URL** of your camera, for example:
   ```
   rtsp://admin:password@192.168.1.64:554/Streaming/Channels/101
   ```

2. **Set the test duration** in seconds (e.g. `60`).

3. **Choose the RTSP transport mode:**

   | Mode | When to use |
   |---|---|
   | `auto` | Let the tool try TCP → UDP → UDP-multicast automatically |
   | `tcp` | Most reliable; use when UDP causes problems |
   | `udp` | UDP unicast; lower latency on good networks |
   | `udp_multicast` | For multicast RTSP streams |

4. **Choose the diagnostic engine:**

   | Engine | Description |
   |---|---|
   | `ffmpeg` | FFprobe metadata + FFmpeg timed diagnostics (recommended) |
   | `gstreamer` | GStreamer metadata + GStreamer diagnostics |

5. *(Optional)* Click **Start Live Preview** to open a side-by-side video window.

6. *(Optional)* Check **Auto-start live preview during test** to open the preview automatically.

7. *(Optional)* Click **Browse** to choose where reports are saved.

8. Click **Start Diagnostic** and watch the live panel update in real time:

   - Frames Received / Drops
   - Bandwidth (current and average)
   - FPS jitter
   - Startup latency
   - RTP missed-packet indicators
   - Selected transport + stream counts (total / video / audio)
   - Health score + grade
   - Live frame and bandwidth charts

9. When the test finishes, a **JSON** file and a **PDF report** are saved automatically.
   Click **Open Last Report** to view the PDF immediately.

---

## Understanding the live metrics

| Metric | Meaning |
|---|---|
| **Status** | `Idle`, `Running`, or `Done` |
| **Transport** | The RTSP transport actually used (TCP / UDP / UDP-multicast) |
| **Stream** | Primary video stream summary (codec, resolution, frame rate) |
| **Streams (T/V/A)** | Total / Video / Audio stream count reported by FFprobe |
| **Frames Received** | Cumulative video frames decoded by FFmpeg |
| **Expected Frames** | `nominal FPS × elapsed seconds` |
| **Estimated Drops** | `Expected – Received` (capped so FFmpeg end-of-run overshoot doesn't inflate the count) |
| **FFmpeg Drops** | Drop counter reported directly by FFmpeg's progress output |
| **FFmpeg Duplicates** | Duplicate-frame counter from FFmpeg |
| **Warning Count** | FFmpeg/GStreamer log lines classified as warnings |
| **Bandwidth Now / Avg** | Estimated from FFmpeg stream-copy telemetry (`total_size`) |
| **FPS Jitter** | Standard deviation of the per-sample frame rate |
| **Startup Latency** | Seconds from launch until the first frame arrived |
| **Missed Packets** | Cumulative RTP missed-packet count parsed from FFmpeg logs |
| **Health** | Score 0–100 + grade: Excellent (≥90), Good (≥75), Fair (≥55), Poor (<55) |

---

## Understanding the report

### PDF report contents

| Section | Contents |
|---|---|
| **KPI Cards** | Health score, frames received, estimated drops, bandwidth, FPS jitter, startup latency |
| **Diagnostic Summary** | Engine, transport, codec, resolution, stream counts, warning count, freeze events |
| **Stream Inventory** | Every stream (index, type, codec, profile, resolution, FPS, bitrate) |
| **Bandwidth Statistics** | Average, minimum, maximum, P95 |
| **Live Dashboard Graph** | Bar chart of frames-per-sample (green) and drops-per-sample (red) overlaid with a bandwidth line (blue) |
| **Expected vs Received Frames** | Cumulative line chart |
| **Drop Timeline** | Cumulative drop curve + per-sample drop bars |
| **Bandwidth Distribution** | Histogram |
| **Quality Timeline** | Realtime FPS curve and health-score curve |
| **Frame Distribution Pie** | Received vs dropped |
| **Warning Category Chart** | Breakdown of warning types (timeout, packet loss, decode, RTSP protocol, buffering, etc.) |
| **RTSP Snapshot** | One captured frame from the camera |

### JSON report fields

```jsonc
{
  "app": "RTSP Camera Frame Drop Diagnostic",
  "timestamp": "2025-06-15T14:22:01",
  "rtsp_url": "rtsp://...",
  "engine": "ffmpeg",
  "transport_selected": "tcp",
  "duration_sec": 60,
  "health_score": 97,
  "health_grade": "Excellent",
  "frames_received": 1500,
  "estimated_drop_frames": 0,
  "drop_rate_percent": 0.0,
  "bandwidth_avg_kbps": 1828,
  "fps_jitter": 0.12,
  "startup_latency_sec": 0.31,
  "missed_packets_total": 0,
  "streams": [ ... ],
  "timeline": [ ... ],
  "warnings_sampled": [ ... ]
}
```

---

## Build a standalone EXE

Build and distribute a single self-contained EXE on any machine that has Python:

```powershell
build_standalone_exe.bat
```

The output appears at `dist\RTSP-Camera-Diagnostic-Portable.exe`.

Copy **only the EXE** to another Windows machine — Python is not required there.
For full functionality, keep FFmpeg binaries at:

```
C:\ffmpeg\bin\ffmpeg.exe
C:\ffmpeg\bin\ffprobe.exe   ← recommended for stream metadata
C:\ffmpeg\bin\ffplay.exe    ← optional, for live preview
```

> **Antivirus false positives?**
> One-file PyInstaller EXEs sometimes trigger heuristic AV alerts.
> Build a one-folder package instead — lower AV false-positive risk:
>
> ```powershell
> build_low_av_folder.bat
> ```
>
> Then use `dist\RTSP-Camera-Diagnostic-Folder\RTSP-Camera-Diagnostic-Folder.exe`.

---

## Deploy to another PC (offline bundle)

The `dist\` folder includes:

| File | Purpose |
|---|---|
| `RTSP-Camera-Diagnostic-Portable.exe` | The portable app |
| `Install-On-Other-PC.ps1` | PowerShell script that installs FFmpeg + GStreamer from offline packages |
| `README-DEPLOY.txt` | Step-by-step deployment instructions |

Copy the entire `dist\` folder to the target PC, then:

1. Right-click `Install-On-Other-PC.ps1` → **Run with PowerShell**.
2. Allow administrator access when Windows prompts.
3. Launch the EXE.

---

## GStreamer support

The app auto-detects GStreamer from these common paths:

```
C:\Program Files\gstreamer\1.0\msvc_x86_64\bin
C:\gstreamer\1.0\msvc_x86_64\bin
gstreamer\1.0\msvc_x86_64\bin   ← next to the EXE
```

It also sets `GST_PLUGIN_PATH`, `GST_PLUGIN_SCANNER`, and `GI_TYPELIB_PATH` automatically at launch.

---

## Common questions

**Why does "Bitrate" show `N/A` in the logs?**
FFmpeg's `null` muxer often omits bitrate. The tool uses `total_size` telemetry to estimate live bandwidth independently, so the **Bandwidth** fields in the report should always show a real value.

**Why might Estimated Drops differ from FFmpeg Drops?**
Estimated Drops = `(nominal FPS × elapsed) − received frames`.
FFmpeg Drops = FFmpeg's internal drop counter (decoder-level).
Both are shown so you can cross-reference.

**The stream works fine but health score is below 90 — why?**
The score is reduced by warning messages, FPS jitter, startup latency, and missed RTP packets — not only by frame drops. A score of 75–89 is still "Good".

**Password contains `@` or other special characters?**
The app URL-encodes credentials automatically (`@` → `%40`, etc.) before passing the URL to FFmpeg/FFplay.

**The window is too small on my screen.**
The main window is fully scrollable. Resize or scroll to reach all controls.

---

## Repository contents

```
app.py                            ← full source code (single file)
requirements.txt                  ← Python dependencies (fpdf2, matplotlib)
run_diagnostic_tool.bat           ← launch from source
build_standalone_exe.bat          ← build portable one-file EXE
build_low_av_folder.bat           ← build one-folder EXE (lower AV risk)
assets/
  camera_icon.png                 ← app icon (PNG)
  camera_icon.ico                 ← app icon (ICO, used by EXE build)
docs/screenshots/
  app-main-window.png             ← UI screenshot – idle state
  app-running.png                 ← UI screenshot – diagnostic in progress
  report-sample.png               ← sample PDF report layout
dist/
  RTSP-Camera-Diagnostic-Portable.exe   ← pre-built portable EXE
  Install-On-Other-PC.ps1               ← offline deployment script
  README-DEPLOY.txt                     ← deployment instructions
```

---

## License

This project is released for free use and sharing. See the repository for full details.
