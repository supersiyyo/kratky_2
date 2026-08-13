from pathlib import Path

import pytest

from app.common.config import ConfigError, config_from_mapping


def valid_mapping(tmp_path: Path) -> dict:
    return {
        "deployment": {"mode": "development", "timezone": "America/Los_Angeles"},
        "storage": {"root": str(tmp_path), "retention_days": 30, "minimum_free_gib": 2},
        "runtime": {
            "run_dir": str(tmp_path / "run"),
            "state_dir": str(tmp_path / "state"),
            "sensor_dir": str(tmp_path / "sensors"),
        },
        "cameras": {
            "water": {
                "enabled": True,
                "required": True,
                "device": "/dev/video-water",
                "archive_fps": 1,
                "preview_fps": 1,
            },
            "environment": {
                "enabled": False,
                "required": False,
                "device": None,
                "archive_fps": 1,
                "preview_fps": 1,
            },
        },
    }


def test_development_accepts_planned_camera(tmp_path: Path) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    assert config.cameras["environment"].enabled is False
    assert config.cameras["environment"].required is False


def test_exact_one_fps_is_required(tmp_path: Path) -> None:
    raw = valid_mapping(tmp_path)
    raw["cameras"]["water"]["archive_fps"] = 2
    with pytest.raises(ConfigError, match="must both be 1"):
        config_from_mapping(raw)


def test_production_requires_enabled_required_camera(tmp_path: Path) -> None:
    raw = valid_mapping(tmp_path)
    raw["deployment"]["mode"] = "production"
    raw["storage"]["minimum_free_gib"] = 12
    raw["cameras"]["environment"]["required"] = True
    with pytest.raises(ConfigError, match="required cameras"):
        config_from_mapping(raw)


def test_production_enforces_reserve(tmp_path: Path) -> None:
    raw = valid_mapping(tmp_path)
    raw["deployment"]["mode"] = "production"
    with pytest.raises(ConfigError, match="at least 10"):
        config_from_mapping(raw)


def test_offload_can_be_enabled_before_dashboard_oauth_setup(tmp_path: Path) -> None:
    raw = valid_mapping(tmp_path)
    raw["offload"] = {"enabled": True}
    assert config_from_mapping(raw).offload.enabled is True


def test_legacy_oauth_config_requires_id_and_secret_together(tmp_path: Path) -> None:
    raw = valid_mapping(tmp_path)
    raw["offload"] = {"oauth_client_id": "example.apps.googleusercontent.com"}
    with pytest.raises(ConfigError, match="configured together"):
        config_from_mapping(raw)
