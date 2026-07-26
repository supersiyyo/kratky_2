from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.common.config import CameraConfig


@dataclass(frozen=True, slots=True)
class FfmpegCommand:
    argv: list[str]
    recording: Path
    preview: Path


def build_ffmpeg_command(
    camera: CameraConfig,
    recording: Path,
    preview: Path,
    ffmpeg: str = "/usr/bin/ffmpeg",
) -> FfmpegCommand:
    if not camera.device:
        raise ValueError("enabled camera has no device")
    width, height = camera.resolution.split("x", 1)
    filter_graph = (
        f"[0:v]fps=fps={camera.archive_fps}:round=down,"
        f"scale={width}:{height}:flags=lanczos,split=2[archive][preview];"
        f"[preview]scale={camera.preview_width}:-2:flags=fast_bilinear[preview_scaled]"
    )
    argv = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel", "warning",
        "-f", "v4l2",
        "-input_format", camera.input_format,
        "-framerate", str(camera.input_fps),
        "-video_size", camera.resolution,
        "-thread_queue_size", "512",
        "-i", camera.device,
        "-filter_complex", filter_graph,
        "-map", "[archive]",
        "-an",
        "-c:v", camera.encoder,
    ]
    if camera.encoder == "libx265":
        argv.extend(["-preset", camera.preset, "-crf", str(camera.crf)])
    argv.extend([
        "-pix_fmt", "yuv420p",
        "-r", str(camera.archive_fps),
        "-f", "matroska",
        str(recording),
        "-map", "[preview_scaled]",
        "-an",
        "-c:v", "mjpeg",
        "-q:v", "4",
        "-f", "image2",
        "-update", "1",
        "-atomic_writing", "1",
        str(preview),
    ])
    return FfmpegCommand(argv, recording, preview)
