from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.capture.recordings import list_recordings, next_hour, recording_path


TZ = ZoneInfo("America/Los_Angeles")


def test_recording_path_uses_local_day_and_never_overwrites(tmp_path: Path) -> None:
    now = datetime(2026, 7, 25, 14, 37, 18, tzinfo=TZ)
    first = recording_path(tmp_path, "water", now)
    first.touch()
    second = recording_path(tmp_path, "water", now)
    assert first.name == "water-2026-07-25_14-37-18.mkv"
    assert second.name == "water-2026-07-25_14-37-18-1.mkv"
    assert second.parent == tmp_path / "2026-07-25" / "water"


def test_next_hour_crosses_midnight() -> None:
    now = datetime(2026, 7, 25, 23, 59, 30, tzinfo=TZ)
    assert next_hour(now) == datetime(2026, 7, 26, 0, 0, tzinfo=TZ)


def test_active_recording_is_excluded(tmp_path: Path) -> None:
    path = recording_path(
        tmp_path, "water", datetime(2026, 7, 25, 9, tzinfo=TZ)
    )
    path.write_bytes(b"video")
    assert list_recordings(tmp_path, TZ, "2026-07-25", {path}) == []
    assert len(list_recordings(tmp_path, TZ, "2026-07-25", set())) == 1
