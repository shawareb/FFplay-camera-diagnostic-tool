# Installation and Runtime Notes

## System Requirements

| Requirement | Notes |
|-------------|-------|
| OS | Windows 10 or Windows 11 |
| **FFmpeg** (`ffmpeg.exe`) | **Mandatory** — used for all RTSP diagnostics |
| FFprobe (`ffprobe.exe`) | Recommended — provides richer stream metadata (codec, FPS, resolution) |
| FFplay (`ffplay.exe`) | Optional — required only for FFplay live preview |
| GStreamer | Optional — required only if using the GStreamer engine or GStreamer preview |
| Python 3.10+ | Only when running from source; **not needed** when using the portable EXE |

---

## Install FFmpeg (recommended method)

```powershell
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
```

**Reopen your terminal / PowerShell session** after installation so the updated PATH is picked up.

### Verify FFmpeg is on PATH

```powershell
ffmpeg -version
ffprobe -version
ffplay -version
```

### Manual / offline install

Download a full FFmpeg build from <https://www.gyan.dev/ffmpeg/builds/> and extract to:

```text
C:\ffmpeg\bin\ffmpeg.exe
C:\ffmpeg\bin\ffprobe.exe
C:\ffmpeg\bin\ffplay.exe
```

The app auto-detects binaries both from PATH and from `C:\ffmpeg\bin`.

---

## Install GStreamer (optional)

Only needed if you want to use the **GStreamer engine** or **GStreamer live preview**.

Download the runtime installer from <https://gstreamer.freedesktop.org/download/> (choose the *Runtime* installer for your architecture).  
Install to the default path — the app will find the binaries automatically.

---

## Run from source

```powershell
# Install Python dependencies
python -m pip install -r requirements.txt

# Launch the app
python app.py
```

Or double-click `run_diagnostic_tool.bat`.

### Python package requirements

| Package | Purpose |
|---------|---------|
| `fpdf2` | PDF report generation |
| `Pillow` | Chart image rendering inside PDF |
| `matplotlib` | Optional (Pillow-based charts are used by default) |

---

## Run the portable EXE

```text
dist\RTSP-Camera-Diagnostic-Portable.exe
```

No Python installation needed.  The EXE bundles all required Python packages.

---

## Build the portable EXE

```powershell
build_standalone_exe.bat
```

Output: `dist\RTSP-Camera-Diagnostic-Portable.exe`

An offline runtime bundle is also created at:

```text
dist\offline-runtime-packages\RTSP-Diagnostic-Runtimes-Offline\
```

---

## Deploy to another PC

1. Share the full `dist` folder (ZIP recommended).
2. On the target PC, run `dist\Install-On-Other-PC.ps1` as **Administrator**.
3. Launch `RTSP-Camera-Diagnostic-Portable.exe`.

The installer script handles FFmpeg (and optionally GStreamer) from the offline bundle.

---

## Firewall / network considerations for diagnostics

The tool connects **outbound** from your PC to the camera's IP address.  No inbound ports need to be opened on the PC running this tool.

| Protocol | Default port | Notes |
|----------|-------------|-------|
| RTSP/TCP | 554 | Primary signalling and data channel when TCP transport is chosen |
| RTSP/UDP | 554 (signalling) + ephemeral RTP/RTCP ports | Used when UDP or UDP-multicast transport is chosen; firewalls often block ephemeral UDP ports |
| HTTP / ONVIF | 80, 8080 | Used only for the ONVIF camera-identity lookup at diagnostic start |

If RTSP over UDP is blocked by the firewall you will see rising **Missed Packets** and low health scores.  Switching to TCP transport usually resolves this.

