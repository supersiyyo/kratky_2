from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(ValueError):
    """Raised when configuration cannot be used safely."""


@dataclass(frozen=True, slots=True)
class DeploymentConfig:
    mode: str = "development"
    timezone: str = "America/Los_Angeles"


@dataclass(frozen=True, slots=True)
class StorageConfig:
    root: Path = Path("/var/lib/kratky/recordings")
    retention_days: int = 30
    minimum_free_gib: float = 2.0
    capacity_warning_days: float = 35.0


@dataclass(frozen=True, slots=True)
class CameraConfig:
    enabled: bool
    required: bool
    device: str | None
    resolution: str = "1920x1080"
    input_format: str = "mjpeg"
    input_fps: int = 10
    archive_fps: int = 1
    preview_fps: int = 1
    preview_width: int = 960
    encoder: str = "libx265"
    crf: int = 28
    preset: str = "veryfast"


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    run_dir: Path = Path("/run/kratky")
    state_dir: Path = Path("/var/lib/kratky/state")
    sensor_dir: Path = Path("/var/lib/kratky/sensors")
    stale_after_seconds: int = 3
    shutdown_timeout_seconds: int = 15


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True, slots=True)
class SensorConfig:
    enabled: bool = True
    interval_seconds: float = 1.0
    history_interval_seconds: float = 1.0
    modbus_device: str = "/dev/ttyUSB0"
    modbus_slave: int = 1


@dataclass(frozen=True, slots=True)
class AppConfig:
    deployment: DeploymentConfig
    storage: StorageConfig
    cameras: dict[str, CameraConfig]
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    sensors: SensorConfig = field(default_factory=SensorConfig)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _resolution(value: str, label: str) -> str:
    pieces = value.lower().split("x")
    if len(pieces) != 2 or not all(piece.isdigit() and int(piece) > 0 for piece in pieces):
        raise ConfigError(f"{label} must use WIDTHxHEIGHT")
    return value.lower()


def config_from_mapping(raw: dict[str, Any]) -> AppConfig:
    deployment_raw = _mapping(raw.get("deployment"), "deployment")
    storage_raw = _mapping(raw.get("storage"), "storage").copy()
    runtime_raw = _mapping(raw.get("runtime"), "runtime").copy()
    dashboard_raw = _mapping(raw.get("dashboard"), "dashboard")
    sensors_raw = _mapping(raw.get("sensors"), "sensors")
    cameras_raw = _mapping(raw.get("cameras"), "cameras")

    deployment = DeploymentConfig(**deployment_raw)
    if deployment.mode not in {"development", "production"}:
        raise ConfigError("deployment.mode must be development or production")
    try:
        ZoneInfo(deployment.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown timezone: {deployment.timezone}") from exc

    storage = StorageConfig(
        root=Path(storage_raw.pop("root", "/var/lib/kratky/recordings")),
        **storage_raw,
    )
    if storage.retention_days < 1 or storage.minimum_free_gib < 0:
        raise ConfigError("retention_days must be positive and minimum_free_gib non-negative")

    cameras: dict[str, CameraConfig] = {}
    if not cameras_raw:
        raise ConfigError("at least one camera must be configured")
    allowed_presets = {
        "ultrafast", "superfast", "veryfast", "faster", "fast",
        "medium", "slow", "slower", "veryslow",
    }
    for name, value in cameras_raw.items():
        camera_raw = _mapping(value, f"cameras.{name}").copy()
        if "enabled" not in camera_raw or "required" not in camera_raw:
            raise ConfigError(f"cameras.{name} requires enabled and required")
        camera_raw["resolution"] = _resolution(
            str(camera_raw.get("resolution", "1920x1080")),
            f"cameras.{name}.resolution",
        )
        camera = CameraConfig(**camera_raw)
        if camera.enabled and not camera.device:
            raise ConfigError(f"cameras.{name}.device is required when enabled")
        if camera.archive_fps != 1 or camera.preview_fps != 1:
            raise ConfigError(f"cameras.{name} archive_fps and preview_fps must both be 1")
        if camera.encoder not in {"libx265", "hevc_v4l2m2m"}:
            raise ConfigError(f"cameras.{name}.encoder must produce H.265/HEVC")
        if camera.preset not in allowed_presets:
            raise ConfigError(f"cameras.{name}.preset is not a supported x265 preset")
        cameras[str(name)] = camera

    if deployment.mode == "production":
        missing = [name for name, camera in cameras.items() if camera.required and not camera.enabled]
        if missing:
            raise ConfigError(
                "production requires all required cameras enabled: " + ", ".join(missing)
            )
        if storage.minimum_free_gib < 10:
            raise ConfigError("production minimum_free_gib must be at least 10")

    runtime = RuntimeConfig(
        run_dir=Path(runtime_raw.pop("run_dir", "/run/kratky")),
        state_dir=Path(runtime_raw.pop("state_dir", "/var/lib/kratky/state")),
        sensor_dir=Path(runtime_raw.pop("sensor_dir", "/var/lib/kratky/sensors")),
        **runtime_raw,
    )
    dashboard = DashboardConfig(**dashboard_raw)
    sensors = SensorConfig(**sensors_raw)
    return AppConfig(deployment, storage, cameras, runtime, dashboard, sensors)


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or os.environ.get("KRATKY_CONFIG", "/etc/kratky/config.yaml"))
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")
    return config_from_mapping(raw)
