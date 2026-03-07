RTSP Diagnostic Offline Runtimes
================================

This folder is used by:
dist\Install-On-Other-PC.ps1

Expected package files under:
offline-runtime-packages\RTSP-Diagnostic-Runtimes-Offline\packages\

Supported package names:
- ffmpeg*.zip
- gstreamer*.zip  (preferred for full offline copy)
- gstreamer*.msi  (optional alternative)

Notes:
- FFmpeg is required for diagnostics.
- GStreamer is optional (only needed for GStreamer engine/preview).
- If GStreamer package is missing, installer continues with FFmpeg only.
