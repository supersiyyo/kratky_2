from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


RECORDING_RE = re.compile(
    r"^(?P<camera>[a-zA-Z0-9_-]+)-(?P<date>\d{4}-\d{2}-\d{2})_"
    r"(?P<time>\d{2}-\d{2}-\d{2})(?:-(?P<suffix>\d+))?\.mkv$"
)


def recording_path(root: Path, camera: str, now: datetime) -> Path:
    day = now.strftime("%Y-%m-%d")
    directory = root / day / camera
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{camera}-{now.strftime('%Y-%m-%d_%H-%M-%S')}"
    candidate = directory / f"{stem}.mkv"
    suffix = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{suffix}.mkv"
        suffix += 1
    return candidate


def next_hour(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Recording:
    camera: str
    path: Path
    start: datetime
    end: datetime
    size: int

    def to_dict(self, root: Path) -> dict[str, object]:
        return {
            "camera": self.camera,
            "path": self.path.relative_to(root).as_posix(),
            "filename": self.path.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "size": self.size,
        }


def parse_start(path: Path, timezone: ZoneInfo) -> datetime | None:
    match = RECORDING_RE.match(path.name)
    if not match:
        return None
    return datetime.strptime(
        f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H-%M-%S"
    ).replace(tzinfo=timezone)


def timing_path(recording: Path) -> Path:
    return recording.with_suffix(".timing.json")


def recording_timing(
    recording: Path,
    timezone: ZoneInfo,
) -> tuple[datetime, datetime, bool] | None:
    fallback_start = parse_start(recording, timezone)
    if fallback_start is None:
        return None
    fallback_end = datetime.fromtimestamp(recording.stat().st_mtime, timezone)
    try:
        with timing_path(recording).open("r", encoding="utf-8") as handle:
            timing = json.load(handle)
        first = datetime.fromisoformat(str(timing["first_frame_at"]))
        last = datetime.fromisoformat(str(timing["last_frame_at"]))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return fallback_start, fallback_end, True
    return first, last, False


def list_recordings(
    root: Path,
    timezone: ZoneInfo,
    day: str,
    active_paths: set[Path] | None = None,
) -> list[Recording]:
    active = {path.resolve() for path in (active_paths or set())}
    directory = root / day
    if not directory.is_dir():
        return []
    items: list[Recording] = []
    for path in directory.glob("*/*.mkv"):
        if path.resolve() in active:
            continue
        timing = recording_timing(path, timezone)
        if timing is None:
            continue
        start, end, _approximate = timing
        stat = path.stat()
        items.append(Recording(path.parent.name, path, start, end, stat.st_size))
    return sorted(items, key=lambda item: (item.start, item.camera))
