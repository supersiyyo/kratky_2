import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.capture.recordings import recording_path, timing_path
from app.capture.state import atomic_write_json
from app.common.config import config_from_mapping
from app.common.paths import capture_state_path
from app.dashboard.server import create_app
from app.offload.google_drive import DeviceAuthorization
from app.offload.service import credential_path, ledger_path, pending_auth_path
from app.offload.ledger import OffloadLedger
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
    assert b"Storage &amp; Offload" in response.data


def test_offload_page_explains_when_google_is_not_configured(tmp_path: Path) -> None:
    app = create_app(config_from_mapping(valid_mapping(tmp_path)))
    client = app.test_client()
    page = client.get("/offload")
    status = client.get("/api/offload/status")

    assert page.status_code == 200
    assert b"Connect Google Drive" in page.data
    assert status.status_code == 200
    assert status.get_json()["configured"] is False
    assert status.get_json()["connected"] is False
    assert status.get_json()["service_enabled"] is False


def test_administrator_can_upload_oauth_json_without_exposing_secret(
    tmp_path: Path,
) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    client = create_app(config).test_client()
    secret = "private-google-client-secret"
    credentials = json.dumps(
        {
            "installed": {
                "client_id": "1234567890-example.apps.googleusercontent.com",
                "client_secret": secret,
            }
        }
    ).encode()

    response = client.post(
        "/api/offload/credentials",
        data={"credentials": (io.BytesIO(credentials), "oauth-client.json")},
        content_type="multipart/form-data",
    )
    status = client.get("/api/offload/status")

    assert response.status_code == 200
    assert status.get_json()["configured"] is True
    assert status.get_json()["service_enabled"] is False
    assert secret not in status.get_data(as_text=True)
    assert secret in credential_path(config).read_text(encoding="utf-8")


def test_replacing_oauth_json_requires_confirmation(tmp_path: Path) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    client = create_app(config).test_client()
    payload = json.dumps(
        {
            "installed": {
                "client_id": "first.apps.googleusercontent.com",
                "client_secret": "first-secret",
            }
        }
    ).encode()
    assert client.post(
        "/api/offload/credentials",
        data={"credentials": (io.BytesIO(payload), "first.json")},
        content_type="multipart/form-data",
    ).status_code == 200

    replacement = json.dumps(
        {
            "installed": {
                "client_id": "second.apps.googleusercontent.com",
                "client_secret": "second-secret",
            }
        }
    ).encode()
    denied = client.post(
        "/api/offload/credentials",
        data={"credentials": (io.BytesIO(replacement), "second.json")},
        content_type="multipart/form-data",
    )

    assert denied.status_code == 409
    assert "first-secret" in credential_path(config).read_text(encoding="utf-8")


def test_preview_authorization_creates_project_folders_but_forces_cleanup_off(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    client = create_app(config).test_client()
    credentials = json.dumps(
        {
            "installed": {
                "client_id": "preview.apps.googleusercontent.com",
                "client_secret": "preview-secret",
            }
        }
    ).encode()
    assert client.post(
        "/api/offload/credentials",
        data={"credentials": (io.BytesIO(credentials), "preview.json")},
        content_type="multipart/form-data",
    ).status_code == 200

    class OAuth:
        def start(self):
            return DeviceAuthorization(
                "device-code", "USER-CODE", "https://google.test/device", 1800, 5
            )

        def finish(self, _device_code):
            return {"refresh_token": "refresh"}

    class Drive:
        def __init__(self):
            self.created = []

        def create_folder(self, name, parent_id=None):
            self.created.append((name, parent_id))
            identifier = f"folder-{len(self.created)}"
            return {"id": identifier, "webViewLink": f"https://drive.test/{identifier}"}

    drive = Drive()
    monkeypatch.setattr(
        "app.dashboard.server.build_google", lambda _config: (OAuth(), drive)
    )

    started = client.post(
        "/api/offload/connect",
        json={"project_name": "Lettuce Study", "auto_cleanup": True},
    )
    assert started.status_code == 200
    assert started.get_json()["user_code"] == "USER-CODE"
    assert json.loads(pending_auth_path(config).read_text(encoding="utf-8"))[
        "auto_cleanup"
    ] is False

    completed = client.post("/api/offload/connect/status")
    ledger = OffloadLedger(ledger_path(config))
    assert completed.status_code == 200
    assert [item[0] for item in drive.created] == [
        "Lettuce Study",
        "raw",
        "timelapse-daily",
        "final",
    ]
    assert ledger.setting("project_name") == "Lettuce Study"
    assert ledger.setting("auto_cleanup") is False
    cleanup = client.post("/api/offload/cleanup", json={"enabled": True})
    assert cleanup.status_code == 409
    assert ledger.setting("auto_cleanup") is False


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


def _complete_recording_day(config, first: datetime) -> dict[str, Path]:
    recordings: dict[str, Path] = {}
    for camera, contents in (("water", b"water-video"), ("environment", b"air-video")):
        recording = recording_path(config.storage.root, camera, first)
        recording.write_bytes(contents)
        atomic_write_json(
            timing_path(recording),
            {
                "camera": camera,
                "first_frame_at": first.isoformat(),
                "last_frame_at": (first + timedelta(seconds=2)).isoformat(),
                "frame_count": 3,
            },
        )
        recordings[camera] = recording
    append_history(
        daily_history_path(config.runtime.sensor_dir, first),
        snapshot(first, 78.4, 6.2),
    )
    return recordings


def test_recordings_page_groups_water_environment_and_sensors_by_day(
    tmp_path: Path,
) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    first = datetime(2026, 7, 25, 8, tzinfo=ZoneInfo("America/Los_Angeles"))
    _complete_recording_day(config, first)
    client = create_app(config).test_client()

    index_response = client.get("/recordings")
    detail_response = client.get("/recordings/2026-07-25")

    assert index_response.status_code == 200
    assert b"Recordings by day" in index_response.data
    assert b"COMPLETE" in index_response.data
    assert b"Water" in index_response.data
    assert b"Environment" in index_response.data
    assert b"Sensor data" in index_response.data
    assert detail_response.status_code == 200
    assert detail_response.data.count(b">Review</a>") == 2


def test_daily_archive_streams_both_cameras_timing_sensors_and_manifest(
    tmp_path: Path,
) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    first = datetime(2026, 7, 25, 8, tzinfo=ZoneInfo("America/Los_Angeles"))
    recordings = _complete_recording_day(config, first)

    response = create_app(config).test_client().get(
        "/recordings/archive/2026-07-25.zip"
    )

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert "kratky-2026-07-25.zip" in response.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        names = set(archive.namelist())
        assert f"water/{recordings['water'].name}" in names
        assert f"water/{timing_path(recordings['water']).name}" in names
        assert f"environment/{recordings['environment'].name}" in names
        assert f"environment/{timing_path(recordings['environment']).name}" in names
        assert "sensors/sensors-2026-07-25.csv" in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["status"] == "complete"
        assert manifest["cameras"]["water"]["recording_count"] == 1
        water_entry = next(
            item
            for item in manifest["entries"]
            if item["name"] == f"water/{recordings['water'].name}"
        )
        assert water_entry["sha256"] == hashlib.sha256(b"water-video").hexdigest()
    assert list(config.storage.root.rglob("*.zip")) == []


def test_daily_archive_rejects_a_day_with_an_active_recording(
    tmp_path: Path,
) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    first = datetime(2026, 7, 25, 8, tzinfo=ZoneInfo("America/Los_Angeles"))
    recordings = _complete_recording_day(config, first)
    atomic_write_json(
        capture_state_path(config),
        {
            "cameras": {
                "water": {
                    "name": "water",
                    "current_recording": str(recordings["water"]),
                }
            }
        },
    )

    response = create_app(config).test_client().get(
        "/recordings/archive/2026-07-25.zip"
    )

    assert response.status_code == 409


def test_daily_archive_rejects_an_invalid_date(tmp_path: Path) -> None:
    app = create_app(config_from_mapping(valid_mapping(tmp_path)))
    response = app.test_client().get("/recordings/archive/2026-99-99.zip")
    assert response.status_code == 404


def test_unstarted_archive_response_releases_the_download_slot(
    tmp_path: Path,
) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    first = datetime(2026, 7, 25, 8, tzinfo=ZoneInfo("America/Los_Angeles"))
    _complete_recording_day(config, first)
    app = create_app(config)

    with app.test_request_context("/recordings/archive/2026-07-25.zip"):
        abandoned = app.view_functions["recording_day_archive"]("2026-07-25")
        abandoned.close()

    response = app.test_client().get("/recordings/archive/2026-07-25.zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.testzip() is None
