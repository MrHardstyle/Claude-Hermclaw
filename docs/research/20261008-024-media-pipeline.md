# 20261008-024 – Media-Pipeline (Bild/Video) auf der GTX 1080

## Sources
- NVIDIA-Forum NVENC GTX 1080: https://forums.developer.nvidia.com/t/gtx-1080-h264-nvenc-no-nvenc-capable-devices-found/80937
- FFmpeg NVENC 2026: https://tutorials.technology/tutorials/ffmpeg-nvenc-hardware-acceleration-2026.html
- ComfyUI-Leitfaden (RamNode): https://ramnode.com/guides/comfyui
- ComfyUI API-Automatisierung: https://eastondev.com/blog/en/posts/ai/20260724-comfyui-api-batch-automation/

## Relevant facts
- Pascal-NVENC: `h264_nvenc`, `hevc_nvenc`; FFmpeg-Build muss zur NVENC-API des 580-Treibers passen.
- ComfyUI: `/prompt`, `/history`, `/view`, WebSocket-Fortschritt; 8 GB reichen für SD 1.5/SDXL-Basisworkflows; Pascal ohne schnelles FP16.

## Decision fixed by architecture
Getrennte Media-Pipeline, Video Priorität 100, Bild 90, sichere AI-Drain/Unload.

## Implementation consequences
- Media-Worker führt Jobs über konfigurierbare **Backends** aus: `ffmpeg` (Video-Transcode/Render) und `comfyui` (Bild-Workflows über HTTP-API) – keine Änderung bestehender Videoverarbeitung, nur über expliziten Media-Step.
- Vor Start: Video-Lease, Drain der AI-Leases (Checkpoint-Anforderung), Unload großer Modelle, danach Freigabe und Resume.
