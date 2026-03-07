# RTSP Camera Diagnostic Tool

A Windows desktop tool for **troubleshooting IP / CCTV camera streams**.  
Use it to detect lags, frame drops, packet loss, firewall-related delays, and bandwidth problems — then share a polished PDF or JSON report with your team or vendor.

---

## Screenshots

### Main Window (idle)
![Main window](docs/screenshots/app-main-window.png)

### Running Diagnostic
![Running diagnostic](docs/screenshots/app-running.png)

### Generated PDF Report Sample
![PDF report sample](docs/screenshots/report-sample.png)

---

## Why use this tool for camera troubleshooting?

Modern IP cameras stream video over RTSP.  When the video lags, freezes, or drops frames the root cause is often one of the following:

| Symptom | Likely cause |
|---------|-------------|
| Visible lag / delay building up | High bandwidth, overloaded switch or NVR |
| Frames freezing momentarily | Packet loss (firewall dropping UDP packets) |
| Stream disconnects / reconnects | Firewall idle-timeout killing the TCP/UDP session |
| Slow stream start | Firewall deep-packet inspection, or camera CPU overload |
| Intermittent drops at specific times | Network congestion, QoS not configured |
| High "Missed Packets" counter | UDP packets blocked or lost between camera and recorder |

This tool gives you **objective, timestamped evidence** for every one of these scenarios.  
You can hand the PDF report to your network team and say *"the firewall is dropping UDP packets — here is the proof"*.

---

## What is new in this version

- **ONVIF camera identity lookup** — automatically queries the camera over HTTP/ONVIF on startup and shows manufacturer, model, firmware version, and serial number.
- **MAC address discovery** — resolves the camera's MAC address via ARP and includes it in the report, making it easy to trace which switch port or VLAN the camera is on.
- Refreshed modern GUI with stronger visual hierarchy and cleaner metric readability.
- Improved live chart visuals (frames received vs. dropped, bandwidth timeline).
- Transport probing: the tool now tests TCP, UDP-unicast, and UDP-multicast independently before the main diagnostic run.
- Updated documentation and build scripts.

---

## Key Features

| Feature | Details |
|---------|---------|
| **Live metrics** | Frames received, expected frames, estimated drops, FFmpeg drops/dups, bandwidth (now + avg), FPS jitter, startup latency, missed RTP packets, health score (0–100) |
| **Transport probing** | Tests TCP, UDP-unicast, and UDP-multicast so you know which paths work before running a full diagnostic |
| **Camera identity** | ONVIF lookup for manufacturer / model / firmware, MAC address via ARP |
| **Health score** | Single 0–100 score combining drop rate, jitter, missed packets, startup latency |
| **Report outputs** | Color PDF with KPI cards, timeline charts, bandwidth histogram, frame-distribution pie, per-second telemetry table, warning samples |
| **JSON output** | Machine-readable full diagnostic — useful for scripting or archiving |
| **Live preview** | Optional FFplay or GStreamer side-by-side video preview during the test |
| **Engine support** | FFmpeg (recommended) and GStreamer (optional) |
| **No-audio support** | Video-only cameras (no audio track) work without any extra configuration |
| **Firewall-friendly URL encoding** | Passwords containing `@` or other reserved URL characters are automatically percent-encoded |

---

## Diagnostic Metrics Explained

### Health Score (0–100)

The health score is reduced by each of the following:

- Drop rate (up to −45 points)
- FPS jitter (up to −15 points)
- Bandwidth spikes (up to −10 points)
- Missed RTP packets (up to −10 points)
- High startup latency > 3 s (up to −8 points)

**Grade thresholds:** Excellent ≥ 90 · Good ≥ 70 · Fair ≥ 50 · Poor < 50  
A perfect stream scores 100. Each penalty is capped independently, so a stream suffering all five problems simultaneously will still score above 0 (minimum ~12). Streams with no issues at all typically score 95–100.

### Firewall / Network Diagnostics

| Metric | What it tells you |
|--------|------------------|
| **Missed Packets** | UDP packets lost between the camera and this PC. A rising count strongly suggests the firewall is dropping packets or there is congestion. Switch to TCP transport to bypass UDP filtering. |
| **Startup Latency** | Time from sending the RTSP DESCRIBE request to receiving the first decoded frame. >5 s often means firewall deep-packet inspection is holding the stream open, or the camera CPU is overloaded. |
| **FPS Jitter %** | Variation in the per-second frame arrival rate. High jitter with zero drops can indicate network buffering or QoS misconfiguration upstream. |
| **Bandwidth Now / Avg** | Real-time and average bitrate in kbps. A consistent gap between "Now" and "Avg" may indicate the firewall is throttling the stream. |
| **Estimated Drops** | Frames that were expected based on the declared FPS but never arrived. Increases when the camera, network, or recorder is overwhelmed. |
| **Transport (TCP vs UDP)** | TCP is reliable (firewall can still block it, but packets are not silently dropped). UDP is lower latency but loses packets when blocked. The tool probes both. |

### Automatic Recommendations in the PDF Report

The PDF report includes an *Executive Summary* with plain-English recommendations such as:

- *"HIGH DROP RATE: Switch to TCP transport (more reliable than UDP for high-loss networks). Check network switch bandwidth and camera firmware."*
- *"PACKET LOSS DETECTED: Check the physical network path to the camera. Look for faulty cables, overloaded switches, or Wi-Fi interference."*
- *"SLOW STARTUP: High startup latency may indicate the camera is slow to respond or the network path has high latency. Check camera CPU usage and network ping times."*

---

## Runtime Requirements

| Requirement | Notes |
|-------------|-------|
| OS | Windows 10 or 11 |
| **FFmpeg** (`ffmpeg.exe`) | Required for diagnostics |
| FFprobe (`ffprobe.exe`) | Recommended — enables richer stream metadata |
| FFplay (`ffplay.exe`) | Optional — required only for live preview |
| GStreamer | Optional — only needed if you choose the GStreamer engine or preview |

> FFmpeg-only installation works perfectly for all diagnostic features.  
> If GStreamer is not installed, keep the engine set to **ffmpeg**.

---

## Install FFmpeg

```powershell
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
```

Then **reopen your terminal / session** so the new PATH is picked up.

The app auto-detects FFmpeg from PATH and from common local locations including `C:\ffmpeg\bin`.

---

## Quick Start

### Option A — Run from source (Python required)

```powershell
python -m pip install -r requirements.txt
python app.py
```

Or double-click `run_diagnostic_tool.bat`.

### Option B — Run portable EXE (no Python needed)

```text
dist\RTSP-Camera-Diagnostic-Portable.exe
```

Python is **not** required on target PCs when using the EXE.

---

## How to Run a Diagnostic

1. Paste the RTSP URL of your camera (e.g. `rtsp://admin:password@192.168.1.64/stream1`).
2. Set the test duration (60 seconds is a good starting point; use 300 s or more for stability checks).
3. Choose RTSP transport:
   - `auto` — let FFmpeg negotiate (best for quick tests)
   - `tcp` — reliable, recommended if you suspect firewall/UDP issues
   - `udp` — lower latency, but packets can be silently dropped by firewalls
   - `udp_multicast` — for cameras that push multicast streams
4. Choose engine (`ffmpeg` is recommended for most cameras).
5. Optionally click **Start FFmpeg Preview** to watch the live video while the test runs.
6. Click **Start Diagnostic**.
7. When the run completes, click **Open Last Report** (or find the PDF/JSON in the Reports Folder).

### Firewall troubleshooting workflow

```
Step 1 — Run with transport = auto  →  note the health score and missed packets
Step 2 — Run with transport = tcp   →  if score improves, UDP is blocked by firewall
Step 3 — Run with transport = udp   →  compare missed-packet counts between TCP and UDP
Step 4 — Read the "Recommendations" section in the PDF for next steps
```

---

## Build Portable EXE

```powershell
build_standalone_exe.bat
```

Output:

```text
dist\RTSP-Camera-Diagnostic-Portable.exe
```

The build script also prepares an offline runtime bundle at:

```text
dist\offline-runtime-packages\RTSP-Diagnostic-Runtimes-Offline\
```

### Lower AV False-Positive Option

If the single-file EXE is flagged by antivirus heuristics, build as a folder distribution instead:

```powershell
build_low_av_folder.bat
```

Then use `dist\RTSP-Camera-Diagnostic-Folder\RTSP-Camera-Diagnostic-Folder.exe`.

---

## Distribution to Other PCs

1. Share the full `dist` folder (ZIP it for easy transfer).
2. On the target PC, run `dist\Install-On-Other-PC.ps1` as Administrator.
3. Launch `RTSP-Camera-Diagnostic-Portable.exe` (or the folder EXE).

The offline installer includes the FFmpeg package and optionally the GStreamer package.  
FFmpeg-only installation is sufficient for all diagnostic features.

Recommended FFmpeg binary locations:

```text
C:\ffmpeg\bin\ffmpeg.exe
C:\ffmpeg\bin\ffprobe.exe
C:\ffmpeg\bin\ffplay.exe
```

---

## Repository Structure

```text
app.py                          ← main application (single file)
requirements.txt                ← Python dependencies
build_standalone_exe.bat        ← builds RTSP-Camera-Diagnostic-Portable.exe
build_low_av_folder.bat         ← builds one-folder (low AV false-positive) variant
run_diagnostic_tool.bat         ← run from source shortcut
run_standalone_exe.bat          ← run the built EXE shortcut
assets/                         ← icon files
docs/
  INSTALLATION.md               ← detailed install notes
  screenshots/                  ← UI screenshots
dist/                           ← pre-built EXE and offline runtime bundle
offline-runtime-packages/       ← offline FFmpeg / GStreamer installers
```

---

## Future Enhancements (ideas for VS Code)

The following features are not yet implemented and are good candidates for local development and testing before pushing back here:

1. **Ping / ICMP latency test to camera IP** — show round-trip time alongside RTSP metrics to distinguish network latency (delay in the path between PC and camera) from encoder delay (time the camera itself spends compressing a video frame before transmitting it).
2. **Firewall port scanner** — check whether ports 554 (RTSP), 8554, and 8000 are reachable from this host before starting the stream, so the user gets a clear error rather than a timeout.
3. **MTU / path-MTU discovery** — send probe packets of varying sizes to detect firewall MSS clamping, which often causes silent frame drops on UDP.
4. **Traceroute / hop count display** — show the number of network hops to the camera, helping pinpoint where latency is introduced.
5. **Email / webhook alert on health drop** — send an email or HTTP POST when the health score falls below a configurable threshold during a run.
6. **Multi-camera batch diagnostic** — enter a list of RTSP URLs and run all of them sequentially (or in parallel), then produce a comparison summary PDF.
7. **Dark mode UI toggle** — add a light/dark theme switch in the settings area.
8. **Camera preset / profile saving** — save frequently tested cameras (URL, transport, FPS, duration) as named presets to the app config file.
9. **Historical trend comparison** — load two saved JSON reports and display a side-by-side delta so you can see whether camera health improved after a change.
10. **Scheduled / recurring diagnostic** — let the user define a cron-style schedule (e.g. every night at 2 AM) so cameras are automatically monitored without manual intervention.
11. **QoS DSCP marking detection** — check whether the stream packets arrive with the expected DSCP value, which helps verify QoS policy is applied end-to-end.
12. **Stream bitrate alert threshold** — warn the user during a live run if bandwidth drops below a set floor (e.g. below 50% of average), indicating a live stream degradation event.

> These are development suggestions only.  Test each feature locally in VS Code before opening a pull request.

---

## Notes for Contributors

- Keep GUI changes in `app.py` focused on readability and operator workflow.
- Validate both source run and EXE run before release.
- If updating release artifacts, rebuild the EXE and test on a clean machine.
- Bump `APP_VERSION` and `BUILD_DATE` in `app.py` on every release.
