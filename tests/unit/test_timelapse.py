from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.capture.recordings import Recording
from app.timelapse.render import (
    OUTPUT_FRAMES,
    _ass_time,
    _sensor_coverage,
    _sensor_line,
    _write_timestamp_subtitles,
    plan_camera,
)


TZ = ZoneInfo("America/Los_Angeles")


def recording(path: Path, start: datetime, seconds: int) -> Recording:
    return Recording(
        camera="water",
        path=path,
        start=start,
        end=start + timedelta(seconds=seconds - 1),
        size=1,
    )


def test_camera_plan_uses_fixed_calendar_day_and_marks_missing_time() -> None:
    day_start = datetime(2026, 7, 31, tzinfo=TZ)
    path = Path("partial.mkv")
    source = recording(path, day_start + timedelta(hours=8), 16 * 60 * 60)

    plan = plan_camera("water", [source], day_start, {path: 16 * 60 * 60})

    assert plan.recorded_samples == 600
    assert len(plan.missing_indices) == 300
    assert plan.missing_indices[0] == 0
    assert plan.missing_indices[-1] == 299
    assert sum(len(values) for values in plan.selections.values()) == 600
    assert OUTPUT_FRAMES == 900


def test_camera_plan_aligns_output_samples_to_recording_frame_indices() -> None:
    day_start = datetime(2026, 8, 1, tzinfo=TZ)
    path = Path("complete.mkv")
    source = recording(path, day_start, 86400)

    plan = plan_camera("water", [source], day_start, {path: 86400})
    selections = plan.selections[path]

    assert not plan.missing_indices
    assert selections[0].source_index == 0
    assert selections[1].source_index == 96
    assert selections[-1].source_index == 86304


def test_camera_plan_bridges_only_short_recording_rollover_gaps() -> None:
    day_start = datetime(2026, 8, 1, tzinfo=TZ)
    first_path = Path("hour-00.mkv")
    second_path = Path("hour-01.mkv")
    first = Recording(
        "water",
        first_path,
        day_start + timedelta(seconds=3),
        day_start + timedelta(minutes=59, seconds=59),
        1,
    )
    second = Recording(
        "water",
        second_path,
        day_start + timedelta(hours=1, seconds=3),
        day_start + timedelta(hours=1, minutes=59, seconds=59),
        1,
    )

    plan = plan_camera(
        "water",
        [first, second],
        day_start,
        {first_path: 3597, second_path: 3597},
    )

    assert 0 not in plan.missing_indices
    assert plan.selections[first_path][0].source_index == 0


def test_camera_plan_keeps_long_gaps_visible() -> None:
    day_start = datetime(2026, 8, 1, tzinfo=TZ)
    path = Path("late.mkv")
    source = recording(path, day_start + timedelta(minutes=10), 600)

    plan = plan_camera("water", [source], day_start, {path: 600})

    assert 0 in plan.missing_indices


def test_ass_time_rounds_to_centiseconds() -> None:
    assert _ass_time(0) == "0:00:00.00"
    assert _ass_time(1 / 30) == "0:00:00.03"
    assert _ass_time(30) == "0:00:30.00"


def test_timestamp_subtitles_have_fixed_1080p_style(tmp_path: Path) -> None:
    path = tmp_path / "timestamps.ass"

    _write_timestamp_subtitles(path, datetime(2026, 7, 31, tzinfo=TZ))

    contents = path.read_text(encoding="utf-8")
    assert "PlayResX: 1920" in contents
    assert "PlayResY: 1080" in contents
    assert "Style: Timestamp,DejaVu Sans,28" in contents
    assert "Style: Sensor,DejaVu Sans,23" in contents
    assert r"{\an5\pos(960,850)}2026-07-31 00:00:00" in contents
    assert r"{\an4\pos(60,925)}{\b1}ENVIRONMENT{\b0}  Unavailable" in contents
    assert r"{\an4\pos(60,990)}{\b1}WATER{\b0}  Unavailable" in contents
    assert "2026-07-31 00:00:00" in contents


def test_sensor_lines_format_existing_environment_and_water_models() -> None:
    sample = {
        "environment": {
            "values": {
                "air_temperature_f": 71.784,
                "relative_humidity_percent": 76.231,
                "co2_ppm": 482,
                "light_lux": 2.752,
            }
        },
        "water": {
            "values": {
                "temperature_c": 26.0,
                "ph": 6.61,
                "electrical_conductivity_us_cm": 418,
                "moisture_percent": 46.0,
                "nitrogen_mg_kg": 29,
                "phosphorus_mg_kg": 41,
                "potassium_mg_kg": 83,
            }
        },
    }

    environment = _sensor_line(sample, "environment")
    water = _sensor_line(sample, "water")

    assert "Air 71.8 °F" in environment
    assert "CO₂ 482 ppm" in environment
    assert "pH 6.61" in water
    assert "N/P/K 29/41/83 mg/kg" in water
    assert _sensor_coverage([sample, None]) == {
        "total_frames": 2,
        "matched_frames": 1,
        "environment_frames": 1,
        "water_frames": 1,
        "maximum_sample_age_seconds": 3,
    }
