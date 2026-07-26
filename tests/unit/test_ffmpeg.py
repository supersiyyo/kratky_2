from pathlib import Path

from app.capture.ffmpeg import build_ffmpeg_command
from app.common.config import CameraConfig


def test_ffmpeg_selects_once_then_splits_archive_and_preview(tmp_path: Path) -> None:
    camera = CameraConfig(True, True, "/dev/video-water")
    command = build_ffmpeg_command(
        camera, tmp_path / "archive.mkv", tmp_path / "latest.jpg"
    )
    graph = command.argv[command.argv.index("-filter_complex") + 1]
    assert graph.count("fps=fps=1") == 1
    assert "split=2[archive][preview]" in graph
    assert command.argv.count("-an") == 2
    assert "libx265" in command.argv
    assert command.argv[-1].endswith("latest.jpg")


def test_output_is_mkv_at_one_fps() -> None:
    command = build_ffmpeg_command(
        CameraConfig(True, True, "/dev/video0"),
        Path("/tmp/test.mkv"),
        Path("/tmp/test.jpg"),
    )
    assert command.argv[command.argv.index("-r") + 1] == "1"
    assert command.argv[command.argv.index("-f", command.argv.index("-r")) + 1] == "matroska"
