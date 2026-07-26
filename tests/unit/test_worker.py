from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from app.capture.worker import CameraWorker
from app.common.config import config_from_mapping
from app.common.models import CameraStatus
from tests.unit.test_config import valid_mapping


class FakeProcess:
    _next_pid = 1000

    def __init__(self, *, capture: bool = False) -> None:
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.stdin = None if capture else BytesIO()
        self.stdout = BytesIO() if capture else None
        self.returncode: int | None = None
        self.waited = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9


def worker_for(tmp_path: Path) -> CameraWorker:
    config = config_from_mapping(valid_mapping(tmp_path))
    return CameraWorker("water", config.cameras["water"], config)


def test_pause_finalizes_recorder_but_keeps_camera_open(tmp_path: Path) -> None:
    worker = worker_for(tmp_path)
    capture = FakeProcess(capture=True)
    recorder = FakeProcess()
    worker.capture_process = capture  # type: ignore[assignment]
    worker.recording_process = recorder  # type: ignore[assignment]
    worker.current_recording = tmp_path / "active.mkv"

    worker.pause_recording()

    assert worker.capture_process is capture
    assert capture.poll() is None
    assert worker.recording_process is None
    assert recorder.waited
    assert worker.runtime.status is CameraStatus.PAUSED


def test_rollover_replaces_only_recorder(tmp_path: Path) -> None:
    worker = worker_for(tmp_path)
    capture = FakeProcess(capture=True)
    old_recorder = FakeProcess()
    new_recorder = FakeProcess()
    worker.capture_process = capture  # type: ignore[assignment]
    worker.recording_process = old_recorder  # type: ignore[assignment]
    worker.current_recording = tmp_path / "old.mkv"
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    worker.rollover_at = now - timedelta(seconds=1)
    worker._capture_started_at = now - timedelta(minutes=5)
    preview = worker.config.runtime.run_dir / "water-latest.jpg"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"preview")
    worker.process_factory = lambda *args, **kwargs: new_recorder  # type: ignore[assignment]

    event = worker.tick(now)

    assert event == "rollover"
    assert worker.capture_process is capture
    assert capture.poll() is None
    assert old_recorder.waited
    assert worker.recording_process is new_recorder
    assert worker.current_recording is not None
    assert worker.current_recording != tmp_path / "old.mkv"
