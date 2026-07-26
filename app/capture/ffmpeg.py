from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.common.config import CameraConfig


@dataclass(frozen=True, slots=True)
class CaptureCommand:
    argv: list[str]
    preview: Path
    frame_size: int


@dataclass(frozen=True, slots=True)
class RecorderCommand:
    argv: list[str]
    recording: Path


def _dimensions(camera: CameraConfig) -> tuple[int, int]:
    width, height = camera.resolution.split("x", 1)
    return int(width), int(height)


def build_capture_command(
    camera: CameraConfig,
    preview: Path,
    ffmpeg: str = "/usr/bin/ffmpeg",
) -> CaptureCommand:
    """Keep V4L2 open and emit one raw archive frame plus one preview per second."""
    if not camera.device:
        raise ValueError("enabled camera has no device")
    width, height = _dimensions(camera)
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
        "-pix_fmt", "yuv420p",
        "-f", "rawvideo",
        "pipe:1",
        "-map", "[preview_scaled]",
        "-an",
        "-c:v", "mjpeg",
        "-q:v", "4",
        "-f", "image2",
        "-update", "1",
        "-atomic_writing", "1",
        str(preview),
    ]
    return CaptureCommand(argv, preview, width * height * 3 // 2)


def build_recorder_command(
    camera: CameraConfig,
    recording: Path,
    ffmpeg: str = "/usr/bin/ffmpeg",
) -> RecorderCommand:
    """Encode raw frames supplied by the persistent capture process."""
    argv = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel", "warning",
        "-f", "rawvideo",
        "-pixel_format", "yuv420p",
        "-video_size", camera.resolution,
        "-framerate", str(camera.archive_fps),
        "-i", "pipe:0",
        "-map", "0:v:0",
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
    ])
    return RecorderCommand(argv, recording)
