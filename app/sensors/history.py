from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.sensors.state import SensorSnapshot


SECTION_FIELDS = {
    "environment": (
        "air_temperature_c",
        "air_temperature_f",
        "relative_humidity_percent",
        "co2_ppm",
        "light_lux",
    ),
    "water": (
        "temperature_c",
        "ph",
        "electrical_conductivity_us_cm",
        "moisture_percent",
        "nitrogen_mg_kg",
        "phosphorus_mg_kg",
        "potassium_mg_kg",
    ),
}


def daily_history_path(directory: Path, now: datetime) -> Path:
    return directory / f"sensors-{now:%Y-%m-%d}.csv"


def snapshot_row(snapshot: SensorSnapshot) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": snapshot.updated_at,
        "status": snapshot.status,
        "environment_status": snapshot.environment.status,
        "water_status": snapshot.water.status,
    }
    for section_name, fields in SECTION_FIELDS.items():
        section = getattr(snapshot, section_name)
        for field in fields:
            row[f"{section_name}_{field}"] = section.values.get(field)
    return row


def append_history(path: Path, snapshot: SensorSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = snapshot_row(snapshot)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(values))
        if not exists:
            writer.writeheader()
        writer.writerow(values)


def _days(start: datetime, end: datetime) -> Iterable[datetime]:
    current = start.replace(hour=0, minute=0, second=0, microsecond=0)
    final = end.replace(hour=0, minute=0, second=0, microsecond=0)
    while current <= final:
        yield current
        current += timedelta(days=1)


def _number(value: str | None) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _sample(row: dict[str, str]) -> dict[str, Any] | None:
    try:
        timestamp = datetime.fromisoformat(row["timestamp"])
    except (KeyError, ValueError):
        return None
    result: dict[str, Any] = {
        "timestamp": timestamp.isoformat(),
        "status": row.get("status") or "UNKNOWN",
    }
    for section_name, fields in SECTION_FIELDS.items():
        values = {
            field: _number(row.get(f"{section_name}_{field}"))
            for field in fields
        }
        result[section_name] = {
            "status": row.get(f"{section_name}_status") or "UNKNOWN",
            "values": values,
        }
    return result


def load_history(directory: Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    paths = [daily_history_path(directory, day) for day in _days(start, end)]
    legacy_paths = {
        directory / f"sensors-{day:%Y-%m}.csv" for day in _days(start, end)
    }
    samples: dict[str, dict[str, Any]] = {}
    for path in [*legacy_paths, *paths]:
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    sample = _sample(row)
                    if sample is None:
                        continue
                    timestamp = datetime.fromisoformat(sample["timestamp"])
                    if start <= timestamp <= end:
                        samples[sample["timestamp"]] = sample
        except OSError:
            continue
    return sorted(samples.values(), key=lambda sample: sample["timestamp"])
