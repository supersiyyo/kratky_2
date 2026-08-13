from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.capture.recordings import (
    list_recording_days,
    list_recordings,
    next_hour,
    recording_day,
    recording_path,
    timing_path,
)


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


def test_day_requires_both_cameras_and_sensor_history_to_be_complete(
    tmp_path: Path,
) -> None:
    sensor_directory = tmp_path / "sensors"
    now = datetime(2026, 7, 25, 13, 2, tzinfo=TZ)
    water = recording_path(tmp_path, "water", now)
    water.write_bytes(b"water")
    timing_path(water).write_text("{}", encoding="utf-8")

    partial = recording_day(tmp_path, sensor_directory, TZ, "2026-07-25")
    assert partial is not None
    assert partial.complete is False
    assert partial.downloadable is True
    assert partial.missing_components == (
        "environment recordings",
        "sensor history",
    )

    environment = recording_path(tmp_path, "environment", now)
    environment.write_bytes(b"environment")
    timing_path(environment).write_text("{}", encoding="utf-8")
    sensor_directory.mkdir()
    (sensor_directory / "sensors-2026-07-25.csv").write_text(
        "timestamp,status\n",
        encoding="utf-8",
    )

    complete = recording_day(tmp_path, sensor_directory, TZ, "2026-07-25")
    assert complete is not None
    assert complete.complete is True
    assert complete.missing_components == ()


def test_days_are_derived_and_sorted_newest_first(tmp_path: Path) -> None:
    sensor_directory = tmp_path / "sensors"
    for day in (24, 26):
        path = recording_path(
            tmp_path,
            "water",
            datetime(2026, 7, day, 8, tzinfo=TZ),
        )
        path.write_bytes(b"video")

    days = list_recording_days(tmp_path, sensor_directory, TZ)

    assert [item.day for item in days] == ["2026-07-26", "2026-07-24"]
