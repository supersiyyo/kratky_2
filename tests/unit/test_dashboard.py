from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.capture.recordings import recording_path, timing_path
from app.capture.state import atomic_write_json
from app.common.config import config_from_mapping
from app.dashboard.server import create_app
from app.sensors.history import append_history, daily_history_path
from tests.unit.test_sensor_history import snapshot
from tests.unit.test_config import valid_mapping


def test_dashboard_and_planned_camera_render(tmp_path: Path) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    app = create_app(config)
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b">Recordings</a>" in response.data
    assert b"Control Panel" in response.data
    assert response.data.count(b"data-action=") == 3
    assert b"Camera planned" in response.data


def test_control_rejects_unknown_action(tmp_path: Path) -> None:
    app = create_app(config_from_mapping(valid_mapping(tmp_path)))
    response = app.test_client().post("/api/control", json={"action": "delete"})
    assert response.status_code == 400


def test_control_lock_endpoint_forwards_lock_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    actions: list[tuple[str, str | None]] = []

    def fake_send_command(_config, action: str, camera: str | None):
        actions.append((action, camera))
        return {"ok": True, "action": action}

    monkeypatch.setattr("app.dashboard.server.send_command", fake_send_command)
    response = create_app(config).test_client().post(
        "/api/control-lock",
        json={"action": "unlock"},
    )

    assert response.status_code == 200
    assert actions == [("unlock", None)]


def test_control_returns_locked_status(tmp_path: Path, monkeypatch) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    monkeypatch.setattr(
        "app.dashboard.server.send_command",
        lambda *_args: {
            "ok": False,
            "code": "controls_locked",
            "error": "capture controls are locked",
        },
    )

    response = create_app(config).test_client().post(
        "/api/control",
        json={"action": "pause", "camera": "water"},
    )

    assert response.status_code == 423


def test_download_rejects_path_traversal(tmp_path: Path) -> None:
    app = create_app(config_from_mapping(valid_mapping(tmp_path)))
    response = app.test_client().get("/recordings/file/..%2Fsecret.mkv")
    assert response.status_code == 404


def test_review_page_contains_recording_clock_and_sensor_timeline(
    tmp_path: Path,
) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    timezone = ZoneInfo("America/Los_Angeles")
    first = datetime(2026, 7, 26, 8, 34, 17, tzinfo=timezone)
    recording = recording_path(config.storage.root, "water", first)
    recording.write_bytes(b"video")
    atomic_write_json(
        timing_path(recording),
        {
            "camera": "water",
            "first_frame_at": first.isoformat(),
            "last_frame_at": (first + timedelta(seconds=2)).isoformat(),
            "frame_count": 3,
        },
    )
    append_history(
        daily_history_path(config.runtime.sensor_dir, first),
        snapshot(first, 78.4, 6.2),
    )
    relative = recording.relative_to(config.storage.root).as_posix()

    response = create_app(config).test_client().get(
        f"/recordings/review/{relative}"
    )

    assert response.status_code == 200
    assert b"Recording review" in response.data
    assert b"78.4" in response.data
    assert first.isoformat().encode() in response.data


def test_review_rejects_path_traversal(tmp_path: Path) -> None:
    app = create_app(config_from_mapping(valid_mapping(tmp_path)))
    response = app.test_client().get("/recordings/review/..%2Fsecret.mkv")
    assert response.status_code == 404
