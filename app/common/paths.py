from __future__ import annotations

from pathlib import Path

from app.common.config import AppConfig


def ensure_runtime_directories(config: AppConfig) -> None:
    for path in (
        config.storage.root,
        config.runtime.run_dir,
        config.runtime.state_dir,
        config.runtime.sensor_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def capture_state_path(config: AppConfig) -> Path:
    return config.runtime.run_dir / "capture-state.json"


def persistent_capture_state_path(config: AppConfig) -> Path:
    return config.runtime.state_dir / "capture-state.json"


def control_socket_path(config: AppConfig) -> Path:
    return config.runtime.run_dir / "capture-control.sock"


def sensor_state_path(config: AppConfig) -> Path:
    return config.runtime.run_dir / "sensor-state.json"


def preview_path(config: AppConfig, camera: str) -> Path:
    return config.runtime.run_dir / f"{camera}-latest.jpg"
