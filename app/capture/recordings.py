from __future__ import annotations

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
        start = parse_start(path, timezone)
        if start is None:
            continue
        stat = path.stat()
        end = datetime.fromtimestamp(stat.st_mtime, timezone)
        items.append(Recording(path.parent.name, path, start, end, stat.st_size))
    return sorted(items, key=lambda item: (item.start, item.camera))
