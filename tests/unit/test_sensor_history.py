from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.sensors.history import append_history, daily_history_path, load_history
from app.sensors.state import SensorSection, SensorSnapshot


TZ = ZoneInfo("America/Los_Angeles")


def snapshot(at: datetime, air_f: float, ph: float) -> SensorSnapshot:
    return SensorSnapshot(
        at.isoformat(),
        "OK",
        SensorSection(
            values={
                "air_temperature_c": (air_f - 32) * 5 / 9,
                "air_temperature_f": air_f,
                "relative_humidity_percent": 61.2,
                "co2_ppm": 512,
                "light_lux": 1840,
            },
            status="OK",
        ),
        SensorSection(
            values={
                "temperature_c": 22.3,
                "ph": ph,
                "electrical_conductivity_us_cm": 1430,
                "moisture_percent": 3.2,
                "nitrogen_mg_kg": 0,
                "phosphorus_mg_kg": 0,
                "potassium_mg_kg": 0,
            },
            status="OK",
        ),
    )


def test_one_second_samples_round_trip_through_daily_history(tmp_path: Path) -> None:
    first = datetime(2026, 7, 26, 8, 34, 17, tzinfo=TZ)
    second = first + timedelta(seconds=1)
    path = daily_history_path(tmp_path, first)

    append_history(path, snapshot(first, 78.4, 6.2))
    append_history(path, snapshot(second, 78.5, 6.3))
    samples = load_history(tmp_path, first, second)

    assert path.name == "sensors-2026-07-26.csv"
    assert [sample["timestamp"] for sample in samples] == [
        first.isoformat(),
        second.isoformat(),
    ]
    assert samples[0]["environment"]["values"]["air_temperature_f"] == 78.4
    assert samples[1]["water"]["values"]["ph"] == 6.3
