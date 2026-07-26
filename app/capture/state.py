from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, value: Any, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


class PauseStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> set[str]:
        raw = read_json(self.path, {"paused": []})
        paused = raw.get("paused", []) if isinstance(raw, dict) else []
        return {str(name) for name in paused}

    def save(self, paused: set[str]) -> None:
        atomic_write_json(self.path, {"paused": sorted(paused)})
