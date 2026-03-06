RTSP Camera Diagnostic - Deployment Guide

Copy this full dist folder to the target PC.

Minimum requirement:
- FFmpeg must be installed on the target PC.
- FFmpeg-only setup is fully supported.

Optional requirement:
- GStreamer is only needed if user selects GStreamer engine/preview.

Deployment steps on target PC:
1. Install FFmpeg (or confirm ffmpeg.exe is available).
2. Optionally run Install-On-Other-PC.ps1 if you want bundled runtime installation.
3. Launch one of:
   - RTSP-Camera-Diagnostic-Portable.exe
   - RTSP-Camera-Diagnostic-Folder\RTSP-Camera-Diagnostic-Folder.exe (if folder build exists)

Recommended FFmpeg paths:
- C:\ffmpeg\bin\ffmpeg.exe
- C:\ffmpeg\bin\ffprobe.exe (recommended)
- C:\ffmpeg\bin\ffplay.exe (optional preview)

Important:
- Keep the dist folder structure unchanged when sharing full bundle.
- One-folder build is usually less likely to trigger AV false positives.
