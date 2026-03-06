Copy this whole dist folder to the other PC.

Steps on the other PC:
1. Right-click Install-On-Other-PC.ps1 and Run with PowerShell.
2. Allow administrator access when Windows asks.
3. After installation finishes, open:
   - Recommended: RTSP-Camera-Diagnostic-Folder\RTSP-Camera-Diagnostic-Folder.exe
   - Alternative: RTSP-Camera-Diagnostic-Portable.exe

What gets installed:
- GStreamer from offline-runtime-packages\RTSP-Diagnostic-Runtimes-Offline
- FFmpeg, FFprobe, FFplay to C:\FFMPEG\bin

Important:
- Keep the dist folder structure unchanged.
- The folder build is recommended for lower antivirus false-positive risk.
