import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.capture.recordings import recording_path, timing_path
from app.capture.state import atomic_write_json
from app.common.config import config_from_mapping
from app.offload.google_drive import GoogleDriveError, TokenStore
from app.offload.ledger import OffloadLedger
from app.offload.service import (
    OffloadService,
    ledger_path,
    offload_state_path,
    token_path,
)
from app.sensors.history import append_history, daily_history_path
from app.timelapse.render import (
    OUTPUT_FPS,
    OUTPUT_FRAMES,
    OUTPUT_SECONDS,
    TimelapseError,
    daily_output_paths,
)
from tests.unit.test_config import valid_mapping
from tests.unit.test_sensor_history import snapshot


TZ = ZoneInfo("America/Los_Angeles")


class FakeDrive:
    def __init__(self) -> None:
        self.counter = 0
        self.pending: dict[str, object] = {}
        self.files: dict[str, dict[str, object]] = {}

    def create_folder(self, name: str, parent_id: str | None = None):
        self.counter += 1
        return {
            "id": f"folder-{self.counter}",
            "name": name,
            "webViewLink": f"https://drive.test/folder-{self.counter}",
        }

    def begin_upload(self, name: str, parent_id: str, size: int, **_kwargs):
        session = f"session-{self.counter}-{name}"
        self.pending[session] = (name, parent_id, size)
        return session

    def upload(self, session_url, source, size, offset, chunk_size, progress=None):
        assert offset == 0
        contents = source.read()
        assert len(contents) == size
        if progress:
            progress(size)
        self.counter += 1
        file_id = f"file-{self.counter}"
        name, _parent, _expected = self.pending[session_url]
        self.files[file_id] = {
            "id": file_id,
            "name": name,
            "size": str(size),
            "md5Checksum": hashlib.md5(contents, usedforsecurity=False).hexdigest(),
            "webViewLink": f"https://drive.test/{file_id}",
            "trashed": False,
        }
        return {"id": file_id}

    def file_metadata(self, file_id: str):
        return self.files[file_id]


class CorruptingDrive(FakeDrive):
    def file_metadata(self, file_id: str):
        metadata = dict(super().file_metadata(file_id))
        metadata["md5Checksum"] = "0" * 32
        return metadata


class ToggleCorruptDrive(FakeDrive):
    corrupt = False

    def file_metadata(self, file_id: str):
        metadata = dict(super().file_metadata(file_id))
        if self.corrupt:
            metadata["md5Checksum"] = "0" * 32
        return metadata


class FailsFirstUploadDrive(FakeDrive):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def upload(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise GoogleDriveError("temporary upload failure")
        return super().upload(*args, **kwargs)


def offload_config(tmp_path: Path):
    raw = valid_mapping(tmp_path)
    raw["cameras"]["environment"] = {
        "enabled": True,
        "required": True,
        "device": "/dev/video-environment",
        "archive_fps": 1,
        "preview_fps": 1,
    }
    raw["offload"] = {
        "enabled": True,
        "oauth_client_id": "test-client.apps.googleusercontent.com",
        "oauth_client_secret": "test-secret",
        "auto_cleanup": True,
        "interval_seconds": 5,
        "upload_chunk_mib": 1,
    }
    return config_from_mapping(raw)


def create_complete_day(config, first: datetime) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for camera in ("water", "environment"):
        path = recording_path(config.storage.root, camera, first)
        path.write_bytes(f"{camera}-video".encode())
        atomic_write_json(
            timing_path(path),
            {
                "first_frame_at": first.isoformat(),
                "last_frame_at": (first + timedelta(seconds=2)).isoformat(),
            },
        )
        paths[camera] = path
    append_history(
        daily_history_path(config.runtime.sensor_dir, first),
        snapshot(first, 78.4, 6.2),
    )
    return paths


def create_valid_timelapses(config, day: str) -> dict[str, Path]:
    paths = daily_output_paths(config, day)
    metadata = {}
    for name in ("water", "environment", "combined"):
        path = paths[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{name}-{day}-h264".encode())
        metadata[name] = {
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "md5": hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest(),
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "frame_rate": "30/1",
            "frame_count": OUTPUT_FRAMES,
            "duration_seconds": 30.0,
        }
    paths["summary"].write_text(
        json.dumps(
            {
                "schema_version": 1,
                "date": day,
                "timezone": config.deployment.timezone,
                "timeline": {
                    "output_seconds": OUTPUT_SECONDS,
                    "output_fps": OUTPUT_FPS,
                    "output_frames": OUTPUT_FRAMES,
                },
                "sensor_overlay": {
                    "total_frames": OUTPUT_FRAMES,
                    "matched_frames": OUTPUT_FRAMES,
                    "environment_frames": OUTPUT_FRAMES,
                    "water_frames": OUTPUT_FRAMES,
                },
                "outputs": metadata,
            }
        ),
        encoding="utf-8",
    )
    return paths


def configure_destination(service: OffloadService) -> None:
    service.ledger.set_setting("project_folder_id", "project")
    service.ledger.set_setting("folder:raw", "raw")
    service.ledger.set_setting("folder:timelapse-daily", "timelapse")


def test_enabled_service_waits_safely_for_dashboard_oauth_setup(tmp_path: Path) -> None:
    raw = valid_mapping(tmp_path)
    raw["offload"] = {"enabled": True}
    config = config_from_mapping(raw)

    service = OffloadService(config)
    service.tick()

    assert service.drive is None
    assert service.ledger.summary()["days"] == []
    assert offload_state_path(config).is_file()
    assert '"status": "NOT_CONFIGURED"' in offload_state_path(config).read_text(
        encoding="utf-8"
    )


def test_complete_day_renders_before_any_source_is_registered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = offload_config(tmp_path)
    first = datetime(2026, 8, 10, 8, tzinfo=TZ)
    create_complete_day(config, first)
    rendered: list[str] = []

    def render(_config, day, **_kwargs):
        rendered.append(day)
        create_valid_timelapses(config, day)

    drive = FakeDrive()
    monkeypatch.setattr("app.offload.service.render_day", render)
    monkeypatch.setattr(
        "app.offload.service.build_google", lambda _config: (object(), drive)
    )
    TokenStore(token_path(config)).save({"refresh_token": "test"})
    service = OffloadService(config)
    configure_destination(service)
    service.ledger.set_setting("auto_cleanup", False)

    service.tick()

    assert rendered == ["2026-08-10"]
    files = service.ledger.files_for_day("2026-08-10")
    assert {item["kind"] for item in files} >= {
        "recording",
        "timing",
        "sensor_history",
        "timelapse",
        "timelapse_summary",
    }


def test_legacy_pending_day_is_not_cleaned_without_timelapse_migration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = offload_config(tmp_path)
    first = datetime(2026, 8, 10, 8, tzinfo=TZ)
    recordings = create_complete_day(config, first)
    drive = FakeDrive()
    monkeypatch.setattr(
        "app.offload.service.build_google", lambda _config: (object(), drive)
    )
    TokenStore(token_path(config)).save({"refresh_token": "test"})
    service = OffloadService(config)
    configure_destination(service)
    service.ledger.register_day(
        "2026-08-10",
        [
            {
                "path": path,
                "camera": camera,
                "kind": "recording",
                "relative_name": f"{camera}/{path.name}",
                "size": path.stat().st_size,
            }
            for camera, path in recordings.items()
        ],
    )

    with pytest.raises(TimelapseError, match="predates automatic timelapse offload"):
        service.tick()

    assert all(path.is_file() for path in recordings.values())
    assert service.ledger.day("2026-08-10")["cleanup_at"] is None


def test_sensor_overlay_gap_blocks_registration_and_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = offload_config(tmp_path)
    first = datetime(2026, 8, 10, 8, tzinfo=TZ)
    recordings = create_complete_day(config, first)
    outputs = create_valid_timelapses(config, "2026-08-10")
    summary = json.loads(outputs["summary"].read_text(encoding="utf-8"))
    summary["sensor_overlay"]["matched_frames"] = OUTPUT_FRAMES - 1
    outputs["summary"].write_text(json.dumps(summary), encoding="utf-8")
    drive = FakeDrive()
    monkeypatch.setattr(
        "app.offload.service.build_google", lambda _config: (object(), drive)
    )
    TokenStore(token_path(config)).save({"refresh_token": "test"})
    service = OffloadService(config)
    configure_destination(service)

    with pytest.raises(TimelapseError, match="sensor coverage"):
        service.tick()

    assert service.ledger.day("2026-08-10") is None
    assert all(path.is_file() for path in recordings.values())


def test_verified_complete_day_removes_only_local_video_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = offload_config(tmp_path)
    first = datetime(2026, 8, 10, 8, tzinfo=TZ)
    recordings = create_complete_day(config, first)
    timelapses = create_valid_timelapses(config, "2026-08-10")
    fake_drive = FakeDrive()
    monkeypatch.setattr(
        "app.offload.service.build_google", lambda _config: (object(), fake_drive)
    )
    TokenStore(token_path(config)).save({"refresh_token": "test"})
    service = OffloadService(config)
    configure_destination(service)
    service.ledger.set_setting("auto_cleanup", True)

    for _ in range(15):
        service.tick()

    day = OffloadLedger(ledger_path(config)).day("2026-08-10")
    assert day is not None
    assert day["status"] == "LOCAL_REMOVED"
    assert day["verified_files"] == day["expected_files"] == 10
    assert not recordings["water"].exists()
    assert not recordings["environment"].exists()
    assert timing_path(recordings["water"]).is_file()
    assert timing_path(recordings["environment"]).is_file()
    assert daily_history_path(config.runtime.sensor_dir, first).is_file()
    assert all(path.is_file() for path in timelapses.values())
    receipt = json.loads(
        (service.directory / "receipts" / "2026-08-10.json").read_text()
    )
    assert receipt["cleanup_performed"] is True
    assert len(receipt["removed_recordings"]) == 2


def test_incomplete_day_is_not_registered_or_deleted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = offload_config(tmp_path)
    first = datetime(2026, 8, 10, 8, tzinfo=TZ)
    water = recording_path(config.storage.root, "water", first)
    water.write_bytes(b"water")
    timing_path(water).write_text("{}", encoding="utf-8")
    fake_drive = FakeDrive()
    monkeypatch.setattr(
        "app.offload.service.build_google", lambda _config: (object(), fake_drive)
    )
    TokenStore(token_path(config)).save({"refresh_token": "test"})
    service = OffloadService(config)
    configure_destination(service)

    service.tick()

    assert service.ledger.day("2026-08-10") is None
    assert water.is_file()


def test_remote_checksum_mismatch_preserves_every_local_video(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = offload_config(tmp_path)
    first = datetime(2026, 8, 10, 8, tzinfo=TZ)
    recordings = create_complete_day(config, first)
    create_valid_timelapses(config, "2026-08-10")
    corrupting_drive = CorruptingDrive()
    monkeypatch.setattr(
        "app.offload.service.build_google", lambda _config: (object(), corrupting_drive)
    )
    TokenStore(token_path(config)).save({"refresh_token": "test"})
    service = OffloadService(config)
    configure_destination(service)

    service.tick()

    day = service.ledger.day("2026-08-10")
    assert day is not None
    assert day["status"] == "ERROR"
    assert day["cleanup_at"] is None
    assert recordings["water"].is_file()
    assert recordings["environment"].is_file()


def test_failed_upload_restarts_with_a_new_session_and_then_verifies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = offload_config(tmp_path)
    first = datetime(2026, 8, 10, 8, tzinfo=TZ)
    recordings = create_complete_day(config, first)
    create_valid_timelapses(config, "2026-08-10")
    drive = FailsFirstUploadDrive()
    monkeypatch.setattr(
        "app.offload.service.build_google", lambda _config: (object(), drive)
    )
    TokenStore(token_path(config)).save({"refresh_token": "test"})
    service = OffloadService(config)
    configure_destination(service)

    service.tick()
    failed = service.ledger.pending_file()
    assert failed is not None
    assert failed["status"] == "ERROR"
    assert failed["upload_uri"] == ""
    assert recordings["environment"].is_file()
    assert recordings["water"].is_file()

    for _ in range(15):
        service.tick()

    assert service.ledger.day("2026-08-10")["status"] == "LOCAL_REMOVED"


def test_cleanup_rechecks_drive_and_preserves_raw_video_if_remote_changed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = offload_config(tmp_path)
    first = datetime(2026, 8, 10, 8, tzinfo=TZ)
    recordings = create_complete_day(config, first)
    create_valid_timelapses(config, "2026-08-10")
    drive = ToggleCorruptDrive()
    monkeypatch.setattr(
        "app.offload.service.build_google", lambda _config: (object(), drive)
    )
    TokenStore(token_path(config)).save({"refresh_token": "test"})
    service = OffloadService(config)
    configure_destination(service)
    service.ledger.set_setting("auto_cleanup", False)

    for _ in range(15):
        service.tick()
    assert service.ledger.day("2026-08-10")["status"] == "DRIVE_VERIFIED"

    drive.corrupt = True
    service.ledger.set_setting("auto_cleanup", True)
    with pytest.raises(GoogleDriveError, match="remote verification changed"):
        service.tick()

    assert all(path.is_file() for path in recordings.values())
    assert service.ledger.day("2026-08-10")["cleanup_at"] is None


def test_cleanup_rechecks_local_md5_before_deleting_any_raw_video(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = offload_config(tmp_path)
    first = datetime(2026, 8, 10, 8, tzinfo=TZ)
    recordings = create_complete_day(config, first)
    create_valid_timelapses(config, "2026-08-10")
    drive = FakeDrive()
    monkeypatch.setattr(
        "app.offload.service.build_google", lambda _config: (object(), drive)
    )
    TokenStore(token_path(config)).save({"refresh_token": "test"})
    service = OffloadService(config)
    configure_destination(service)
    service.ledger.set_setting("auto_cleanup", False)

    for _ in range(15):
        service.tick()
    recordings["water"].write_bytes(b"changed-after-upload")
    service.ledger.set_setting("auto_cleanup", True)

    with pytest.raises(OSError, match="local recording changed"):
        service.tick()

    assert all(path.is_file() for path in recordings.values())
    assert service.ledger.day("2026-08-10")["cleanup_at"] is None
