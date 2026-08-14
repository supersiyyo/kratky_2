from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from app.capture.state import atomic_write_json


RECORDING_RE = re.compile(
    r"^(?P<camera>[a-zA-Z0-9_-]+)-(?P<date>\d{4}-\d{2}-\d{2})_"
    r"(?P<time>\d{2}-\d{2}-\d{2})(?:-(?P<suffix>\d+))?\.mkv$"
)
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXPECTED_CAMERAS = ("water", "environment")


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


@dataclass(frozen=True, slots=True)
class CameraDay:
    camera: str
    recordings: tuple[Recording, ...]
    active_paths: tuple[Path, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.recordings)


@dataclass(frozen=True, slots=True)
class RecordingDay:
    day: str
    cameras: dict[str, CameraDay]
    sensor_path: Path | None

    @property
    def recordings(self) -> tuple[Recording, ...]:
        return tuple(
            item
            for name in EXPECTED_CAMERAS
            for item in self.cameras[name].recordings
        )

    @property
    def total_bytes(self) -> int:
        sensor_bytes = self.sensor_path.stat().st_size if self.sensor_path else 0
        return sum(camera.total_bytes for camera in self.cameras.values()) + sensor_bytes

    @property
    def has_active_recording(self) -> bool:
        return any(camera.active_paths for camera in self.cameras.values())

    @property
    def missing_components(self) -> tuple[str, ...]:
        missing = [
            f"{name} recordings"
            for name in EXPECTED_CAMERAS
            if not self.cameras[name].recordings
        ]
        if self.sensor_path is None:
            missing.append("sensor history")
        missing_timing = sum(
            not timing_path(recording.path).is_file()
            for recording in self.recordings
        )
        if missing_timing:
            missing.append(f"timing metadata for {missing_timing} recording(s)")
        return tuple(missing)

    @property
    def complete(self) -> bool:
        return not self.has_active_recording and not self.missing_components

    @property
    def downloadable(self) -> bool:
        # Partial historical days remain exportable so older data is never stranded.
        return bool(self.recordings) and not self.has_active_recording


def parse_start(path: Path, timezone: ZoneInfo) -> datetime | None:
    match = RECORDING_RE.match(path.name)
    if not match:
        return None
    return datetime.strptime(
        f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H-%M-%S"
    ).replace(tzinfo=timezone)


def timing_path(recording: Path) -> Path:
    return recording.with_suffix(".timing.json")


def recover_missing_timing_sidecars(
    root: Path,
    timezone: ZoneInfo,
    before_day: str,
    active_paths: set[Path] | None = None,
    *,
    packet_counter: Callable[[Path], int] | None = None,
) -> tuple[Path, ...]:
    """Recover finalized prior-day 1 fps recordings interrupted before metadata write."""
    active = {path.resolve() for path in (active_paths or set())}
    count_packets = packet_counter or _recording_packet_count
    recovered: list[Path] = []
    for directory in sorted(root.glob("????-??-??")):
        if not directory.is_dir() or directory.name >= before_day:
            continue
        for recording in sorted(directory.glob("*/*.mkv")):
            sidecar = timing_path(recording)
            if sidecar.exists() or recording.resolve() in active:
                continue
            match = RECORDING_RE.fullmatch(recording.name)
            start = parse_start(recording, timezone)
            if (
                match is None
                or start is None
                or match.group("camera") != recording.parent.name
                or recording.parent.name not in EXPECTED_CAMERAS
                or not recording.is_file()
                or recording.stat().st_size < 1
            ):
                continue
            frame_count = count_packets(recording)
            if frame_count < 1:
                raise ValueError(f"recording contains no video frames: {recording}")
            last = start + timedelta(seconds=frame_count - 1)
            atomic_write_json(
                sidecar,
                {
                    "camera": recording.parent.name,
                    "first_frame_at": start.isoformat(),
                    "last_frame_at": last.isoformat(),
                    "frame_count": frame_count,
                    "recovered": True,
                    "recovery_source": "ffprobe_packet_count_at_1_fps",
                    "recovered_at": datetime.now(timezone).isoformat(),
                },
                mode=0o644,
            )
            recovered.append(sidecar)
    return tuple(recovered)


def _recording_packet_count(path: Path, ffprobe: str = "ffprobe") -> int:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-count_packets",
                "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_packets",
                "-of", "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(json.loads(result.stdout)["streams"][0]["nb_read_packets"])
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError(f"could not recover timing metadata from {path}") from exc


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


def recording_day(
    root: Path,
    sensor_directory: Path,
    timezone: ZoneInfo,
    day: str,
    active_paths: set[Path] | None = None,
) -> RecordingDay | None:
    if not DAY_RE.fullmatch(day):
        return None
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None

    active = tuple(path for path in (active_paths or set()) if _path_day(path) == day)
    items = list_recordings(root, timezone, day, set(active))
    cameras = {
        name: CameraDay(
            camera=name,
            recordings=tuple(item for item in items if item.camera == name),
            active_paths=tuple(path for path in active if path.parent.name == name),
        )
        for name in EXPECTED_CAMERAS
    }
    sensor_path = sensor_directory / f"sensors-{day}.csv"
    if not sensor_path.is_file():
        sensor_path = None
    if not items and not active and sensor_path is None:
        return None
    return RecordingDay(day, cameras, sensor_path)


def list_recording_days(
    root: Path,
    sensor_directory: Path,
    timezone: ZoneInfo,
    active_paths: set[Path] | None = None,
) -> list[RecordingDay]:
    days: set[str] = set()
    try:
        days.update(path.name for path in root.iterdir() if path.is_dir())
    except OSError:
        pass
    try:
        days.update(
            match.group(1)
            for path in sensor_directory.glob("sensors-????-??-??.csv")
            if (match := re.fullmatch(r"sensors-(\d{4}-\d{2}-\d{2})\.csv", path.name))
        )
    except OSError:
        pass
    days.update(
        day
        for path in (active_paths or set())
        if (day := _path_day(path)) is not None
    )
    result = [
        value
        for day in days
        if (value := recording_day(
            root, sensor_directory, timezone, day, active_paths
        )) is not None
    ]
    return sorted(result, key=lambda item: item.day, reverse=True)


def _path_day(path: Path) -> str | None:
    try:
        day = path.parent.parent.name
    except IndexError:
        return None
    return day if DAY_RE.fullmatch(day) else None
