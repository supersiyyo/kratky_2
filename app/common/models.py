from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class CameraStatus(StrEnum):
    PLANNED = "PLANNED"
    STARTING = "STARTING"
    RECORDING = "RECORDING"
    PAUSED = "PAUSED"
    RECONNECTING = "RECONNECTING"
    STALE = "STALE"
    ERROR = "ERROR"


@dataclass(slots=True)
class CameraRuntime:
    name: str
    status: CameraStatus
    enabled: bool
    required: bool
    last_frame_at: str | None = None
    current_recording: str | None = None
    started_at: str | None = None
    reconnects: int = 0
    last_gap_seconds: float | None = None
    last_error: str | None = None
    next_retry_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


@dataclass(slots=True)
class CaptureEvent:
    timestamp: str
    camera: str
    kind: str
    detail: str

    @classmethod
    def now(cls, camera: str, kind: str, detail: str) -> "CaptureEvent":
        return cls(datetime.now().astimezone().isoformat(), camera, kind, detail)


@dataclass(slots=True)
class CaptureSnapshot:
    updated_at: str
    version: str
    cameras: dict[str, CameraRuntime]
    events: list[CaptureEvent] = field(default_factory=list)
    storage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "version": self.version,
            "cameras": {name: camera.to_dict() for name, camera in self.cameras.items()},
            "events": [asdict(event) for event in self.events],
            "storage": self.storage,
        }
