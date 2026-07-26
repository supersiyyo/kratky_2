import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
def test_generated_one_fps_hevc_has_no_audio(tmp_path: Path) -> None:
    output = tmp_path / "smoke.mkv"
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=3",
            "-vf", "fps=1", "-an", "-c:v", "libx265", "-preset", "ultrafast",
            "-x265-params", "log-level=error", str(output),
        ],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip("local FFmpeg lacks a usable libx265 encoder")
    probe = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_name,codec_type,avg_frame_rate",
            "-of", "default=noprint_wrappers=1", str(output),
        ],
        text=True,
    )
    assert "codec_name=hevc" in probe
    assert "codec_type=audio" not in probe
    assert "avg_frame_rate=1/1" in probe
