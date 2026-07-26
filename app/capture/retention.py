from __future__ import annotations

import shutil
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


GIB = 1024 ** 3


@dataclass(frozen=True, slots=True)
class StorageReport:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    recording_bytes: int
    represented_hours: float
    recent_daily_bytes: float | None
    estimated_retention_days: float | None
    provisional_two_camera_days: float | None
    reserve_bytes: int
    reserve_reached: bool
    capacity_warning: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def finalized_files(root: Path, active_paths: set[Path]) -> list[Path]:
    active = {path.resolve() for path in active_paths}
    return [
        path for path in root.glob("*/*/*.mkv")
        if path.is_file() and path.resolve() not in active
    ]


def prune_expired(
    root: Path,
    retention_days: int,
    active_paths: set[Path],
    now: datetime,
) -> list[Path]:
    cutoff = now.timestamp() - timedelta(days=retention_days).total_seconds()
    removed: list[Path] = []
    for path in finalized_files(root, active_paths):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def storage_report(
    root: Path,
    active_paths: set[Path],
    minimum_free_gib: float,
    warning_days: float,
    development: bool,
    now: datetime,
    sample_days: int = 7,
) -> StorageReport:
    usage = shutil.disk_usage(root)
    cutoff = now.timestamp() - timedelta(days=sample_days).total_seconds()
    files = [
        path for path in finalized_files(root, active_paths)
        if path.stat().st_mtime >= cutoff
    ]
    recording_bytes = sum(path.stat().st_size for path in files)
    if files:
        earliest = min(path.stat().st_mtime for path in files)
        represented_hours = max((now.timestamp() - earliest) / 3600, 1 / 60)
        recent_daily = recording_bytes / represented_hours * 24
    else:
        represented_hours = 0.0
        recent_daily = None
    reserve_bytes = int(minimum_free_gib * GIB)
    practical_capacity = max(0, usage.free + recording_bytes - reserve_bytes)
    retention = practical_capacity / recent_daily if recent_daily else None
    provisional = retention / 2 if development and retention is not None else retention
    warning_value = provisional if development else retention
    return StorageReport(
        usage.total,
        usage.used,
        usage.free,
        recording_bytes,
        represented_hours,
        recent_daily,
        retention,
        provisional,
        reserve_bytes,
        usage.free <= reserve_bytes,
        warning_value is not None and warning_value < warning_days,
    )
