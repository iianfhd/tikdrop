TikDrop v1.1 PATCH

1. Stop TikDrop with CTRL+C in the terminal that runs run.bat.
2. Extract this ZIP directly into your existing tikdrop-v1-full folder.
3. Approve replacing the files when Windows asks.
4. Open a NEW PowerShell window in that folder and run: .\run.bat
5. Open http://127.0.0.1:8000/api/health
   ffmpeg_available and ffprobe_available should both be true.
6. Open http://127.0.0.1:8000 and test MP3, then test Auto, 1080p, 720p, 480p.

No need to run setup.bat again; Python dependencies have not changed.

Changes:
- Automatically locates Gyan FFmpeg installed by WinGet even if PATH in the shell is stale.
- Adds FFmpeg/FFprobe paths to the backend health response.
- Downloads the best available source first; fixed qualities are then applied as an output cap.
- 720p/480p no longer fail only because TikTok does not expose an exact rendition; FFmpeg downscales the source when needed.
- Does not upscale low-resolution sources or invent detail.
- Shows the source and final dimensions in the result message.
