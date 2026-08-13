from __future__ import annotations

import hashlib
import io
import json
import queue
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.capture.recordings import EXPECTED_CAMERAS, RecordingDay, timing_path


@dataclass(frozen=True, slots=True)
class ArchiveSource:
    path: Path
    name: str
    kind: str
    camera: str | None = None


def archive_sources(
    day: RecordingDay,
    recording_root: Path,
    sensor_directory: Path,
) -> tuple[ArchiveSource, ...]:
    sources: list[ArchiveSource] = []
    for camera_name in EXPECTED_CAMERAS:
        for recording in day.cameras[camera_name].recordings:
            _require_within(recording.path, recording_root)
            sources.append(
                ArchiveSource(
                    recording.path,
                    f"{camera_name}/{recording.path.name}",
                    "recording",
                    camera_name,
                )
            )
            sidecar = timing_path(recording.path)
            if sidecar.is_file():
                _require_within(sidecar, recording_root)
                sources.append(
                    ArchiveSource(
                        sidecar,
                        f"{camera_name}/{sidecar.name}",
                        "timing",
                        camera_name,
                    )
                )
    if day.sensor_path is not None:
        _require_within(day.sensor_path, sensor_directory)
        sources.append(
            ArchiveSource(
                day.sensor_path,
                f"sensors/{day.sensor_path.name}",
                "sensor_history",
            )
        )
    return tuple(sources)


def stream_day_archive(
    day: RecordingDay,
    recording_root: Path,
    sensor_directory: Path,
    timezone: str,
    app_version: str = "unknown",
) -> Iterator[bytes]:
    """Stream a ZIP64 archive without building a second copy on disk."""
    sources = archive_sources(day, recording_root, sensor_directory)
    chunks: queue.Queue[bytes | BaseException | object] = queue.Queue(maxsize=8)
    cancelled = threading.Event()
    finished = object()
    writer = _QueueWriter(chunks, cancelled)

    def produce() -> None:
        try:
            entries: list[dict[str, object]] = []
            warnings = list(day.missing_components)
            with zipfile.ZipFile(
                writer,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
            ) as archive:
                for source in sources:
                    digest = hashlib.sha256()
                    size = 0
                    with source.path.open("rb") as source_file:
                        with archive.open(source.name, "w", force_zip64=True) as target:
                            while block := source_file.read(1024 * 1024):
                                digest.update(block)
                                size += len(block)
                                target.write(block)
                    entries.append(
                        {
                            "name": source.name,
                            "kind": source.kind,
                            "camera": source.camera,
                            "size_bytes": size,
                            "sha256": digest.hexdigest(),
                        }
                    )
                for camera_name in EXPECTED_CAMERAS:
                    for recording in day.cameras[camera_name].recordings:
                        if not timing_path(recording.path).is_file():
                            warnings.append(f"missing timing sidecar: {recording.path.name}")
                manifest = {
                    "schema_version": 1,
                    "date": day.day,
                    "timezone": timezone,
                    "exported_at": datetime.now(ZoneInfo(timezone)).isoformat(),
                    "app_version": app_version,
                    "status": "complete" if day.complete else "incomplete",
                    "cameras": {
                        name: {
                            "recording_count": len(day.cameras[name].recordings),
                            "size_bytes": day.cameras[name].total_bytes,
                        }
                        for name in EXPECTED_CAMERAS
                    },
                    "sensor_history": {
                        "included": day.sensor_path is not None,
                        "name": day.sensor_path.name if day.sensor_path else None,
                    },
                    "warnings": warnings,
                    "entries": entries,
                }
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
                )
        except BaseException as exc:
            _put(chunks, exc, cancelled)
        finally:
            _put(chunks, finished, cancelled)

    producer = threading.Thread(
        target=produce,
        name=f"archive-{day.day}",
        daemon=True,
    )
    producer.start()
    try:
        while True:
            item = chunks.get()
            if item is finished:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        cancelled.set()
        producer.join(timeout=2)


class _QueueWriter(io.RawIOBase):
    def __init__(
        self,
        chunks: queue.Queue[bytes | BaseException | object],
        cancelled: threading.Event,
    ) -> None:
        super().__init__()
        self._chunks = chunks
        self._cancelled = cancelled
        self._position = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._position

    def write(self, data: bytes | bytearray) -> int:
        value = bytes(data)
        if value and not _put(self._chunks, value, self._cancelled):
            raise BrokenPipeError("archive consumer disconnected")
        self._position += len(value)
        return len(value)


def _put(
    chunks: queue.Queue[bytes | BaseException | object],
    value: bytes | BaseException | object,
    cancelled: threading.Event,
) -> bool:
    while not cancelled.is_set():
        try:
            chunks.put(value, timeout=0.25)
            return True
        except queue.Full:
            continue
    return False


def _require_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"archive source is outside its configured root: {path}") from exc
