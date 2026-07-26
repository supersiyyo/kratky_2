from pathlib import Path

from app.capture.ffmpeg import build_capture_command, build_recorder_command
from app.common.config import CameraConfig


def test_capture_selects_once_and_keeps_preview_separate(tmp_path: Path) -> None:
    camera = CameraConfig(True, True, "/dev/video-water")
    command = build_capture_command(camera, tmp_path / "latest.jpg")
    graph = command.argv[command.argv.index("-filter_complex") + 1]

    assert graph.count("fps=fps=1") == 1
    assert "split=2[archive][preview]" in graph
    assert command.argv.count("-an") == 2
    assert "libx265" not in command.argv
    assert "pipe:1" in command.argv
    assert command.argv[-1].endswith("latest.jpg")
    assert command.frame_size == 1920 * 1080 * 3 // 2


def test_recorder_reads_raw_frames_and_writes_one_fps_mkv() -> None:
    command = build_recorder_command(
        CameraConfig(True, True, "/dev/video0"),
        Path("/tmp/test.mkv"),
    )

    assert command.argv[command.argv.index("-i") + 1] == "pipe:0"
    assert command.argv[command.argv.index("-r") + 1] == "1"
    assert "libx265" in command.argv
    assert command.argv[
        command.argv.index("-f", command.argv.index("-r")) + 1
    ] == "matroska"
    assert Path(command.argv[-1]).name == "test.mkv"
