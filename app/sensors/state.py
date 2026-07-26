from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SensorSection:
    values: dict[str, float | int | None] = field(default_factory=dict)
    status: str = "STARTING"
    error: str | None = None
    last_success_at: str | None = None


@dataclass(slots=True)
class SensorSnapshot:
    updated_at: str
    status: str
    environment: SensorSection
    water: SensorSection

    @classmethod
    def starting(cls, now: datetime) -> "SensorSnapshot":
        return cls(
            now.isoformat(),
            "STARTING",
            SensorSection(),
            SensorSection(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
