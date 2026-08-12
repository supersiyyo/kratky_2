from __future__ import annotations

import hashlib
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.capture.archive import archive_sources
from app.capture.recordings import list_recording_days
from app.capture.state import atomic_write_json, read_json
from app.common.config import AppConfig, load_config
from app.common.paths import capture_state_path
from app.offload.google_drive import (
    CredentialStore,
    GoogleDriveClient,
    GoogleDriveError,
    GoogleOAuth,
    OAuthClientCredentials,
    TokenStore,
)
from app.offload.ledger import OffloadLedger


def offload_directory(config: AppConfig) -> Path:
    return config.runtime.state_dir / "offload"


def ledger_path(config: AppConfig) -> Path:
    return offload_directory(config) / "ledger.sqlite3"


def token_path(config: AppConfig) -> Path:
    return offload_directory(config) / "google-token.json"


def credential_path(config: AppConfig) -> Path:
    return offload_directory(config) / "google-oauth-client.json"


def pending_auth_path(config: AppConfig) -> Path:
    return offload_directory(config) / "pending-authorization.json"


def offload_state_path(config: AppConfig) -> Path:
    return config.runtime.run_dir / "offload-state.json"


def configured_credentials(config: AppConfig) -> OAuthClientCredentials | None:
    stored = CredentialStore(credential_path(config)).load()
    if stored:
        return stored
    if config.offload.oauth_client_id and config.offload.oauth_client_secret:
        return OAuthClientCredentials(
            config.offload.oauth_client_id,
            config.offload.oauth_client_secret,
        )
    return None


def build_google(config: AppConfig) -> tuple[GoogleOAuth, GoogleDriveClient]:
    credentials = configured_credentials(config)
    if not credentials:
        raise GoogleDriveError("Google OAuth application is not configured")
    oauth = GoogleOAuth(credentials, TokenStore(token_path(config)))
    return oauth, GoogleDriveClient(oauth)


def provision_project(
    ledger: OffloadLedger,
    drive: GoogleDriveClient,
    project_name: str,
    auto_cleanup: bool,
) -> dict[str, Any]:
    clean_name = " ".join(project_name.split()).strip()
    if not clean_name or len(clean_name) > 120:
        raise ValueError("project name must be between 1 and 120 characters")
    root = drive.create_folder(clean_name)
    folders = {
        name: drive.create_folder(name, str(root["id"]))
        for name in ("raw", "timelapse-daily", "final")
    }
    ledger.set_setting("project_name", clean_name)
    ledger.set_setting("project_folder_id", root["id"])
    ledger.set_setting("project_web_link", root.get("webViewLink"))
    ledger.set_setting("auto_cleanup", auto_cleanup)
    ledger.set_setting("paused", False)
    for name, folder in folders.items():
        ledger.set_setting(f"folder:{name}", folder["id"])
    return {
        "project_name": clean_name,
        "project_folder_id": root["id"],
        "project_web_link": root.get("webViewLink"),
    }


class OffloadService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.timezone = ZoneInfo(config.deployment.timezone)
        self.directory = offload_directory(config)
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.ledger = OffloadLedger(ledger_path(config))
        self.oauth: GoogleOAuth | None = None
        self.drive: GoogleDriveClient | None = None
        self.credential_fingerprint: str | None = None
        self.running = True
        self.last_error: str | None = None

    def tick(self) -> None:
        if not self._ensure_google():
            self.write_state("NOT_CONFIGURED")
            return
        if self.ledger.setting("paused", False):
            self.write_state("PAUSED")
            return
        if not TokenStore(token_path(self.config)).load():
            self.write_state("NOT_CONNECTED")
            return
        if not self.ledger.setting("project_folder_id"):
            self.write_state("NEEDS_PROJECT")
            return
        self._discover_days()
        self._create_ready_manifests()
        item = self.ledger.pending_file()
        if item:
            self.write_state("UPLOADING")
            self._upload_file(item)
        self._cleanup_verified_days()
        self.last_error = None
        self.write_state("IDLE" if not self.ledger.pending_file() else "UPLOADING")

    def _ensure_google(self) -> bool:
        credentials = configured_credentials(self.config)
        if not credentials:
            self.oauth = None
            self.drive = None
            self.credential_fingerprint = None
            return False
        fingerprint = credentials.public_dict()["fingerprint"]
        if self.drive is None or fingerprint != self.credential_fingerprint:
            self.oauth, self.drive = build_google(self.config)
            self.credential_fingerprint = fingerprint
        return True

    def _discover_days(self) -> None:
        now_day = datetime.now(self.timezone).strftime("%Y-%m-%d")
        capture = read_json(capture_state_path(self.config), {"cameras": {}})
        active = {
            Path(value["current_recording"])
            for value in capture.get("cameras", {}).values()
            if value.get("current_recording")
        }
        for day in reversed(
            list_recording_days(
                self.config.storage.root,
                self.config.runtime.sensor_dir,
                self.timezone,
                active,
            )
        ):
            if day.day >= now_day or not day.complete:
                continue
            sources = archive_sources(
                day, self.config.storage.root, self.config.runtime.sensor_dir
            )
            self.ledger.register_day(
                day.day,
                [
                    {
                        "path": source.path,
                        "camera": source.camera,
                        "kind": source.kind,
                        "relative_name": source.name,
                        "size": source.path.stat().st_size,
                    }
                    for source in sources
                ],
            )

    def _create_ready_manifests(self) -> None:
        for day in self.ledger.summary()["days"]:
            day_name = str(day["day"])
            files = self.ledger.files_for_day(day_name)
            if any(item["kind"] == "manifest" for item in files):
                continue
            if not self.ledger.source_files_verified(day_name):
                continue
            manifest_path = self.directory / "manifests" / day_name / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "date": day_name,
                "timezone": self.config.deployment.timezone,
                "created_at": datetime.now(self.timezone).isoformat(),
                "files": [
                    {
                        "name": item["relative_name"],
                        "kind": item["kind"],
                        "camera": item["camera"],
                        "size_bytes": item["size"],
                        "md5": item["md5"],
                        "google_drive_file_id": item["drive_file_id"],
                    }
                    for item in files
                ],
            }
            atomic_write_json(manifest_path, payload, mode=0o600)
            self.ledger.register_manifest(day_name, manifest_path, manifest_path.stat().st_size)

    def _folder_for(self, day: str, relative_name: str) -> str:
        if self.drive is None:
            raise GoogleDriveError("Google Drive is not configured")
        day_key = f"folder:raw:{day}"
        day_folder = self.ledger.setting(day_key)
        if not day_folder:
            raw_folder = self.ledger.setting("folder:raw")
            created = self.drive.create_folder(day, raw_folder)
            day_folder = created["id"]
            self.ledger.set_setting(day_key, day_folder)
            self.ledger.set_day_folder(day, day_folder, created.get("webViewLink"))
        if "/" not in relative_name:
            return str(day_folder)
        section = relative_name.split("/", 1)[0]
        section_key = f"folder:raw:{day}:{section}"
        section_folder = self.ledger.setting(section_key)
        if not section_folder:
            created = self.drive.create_folder(section, day_folder)
            section_folder = created["id"]
            self.ledger.set_setting(section_key, section_folder)
        return str(section_folder)

    def _upload_file(self, item: dict[str, Any]) -> None:
        if self.drive is None:
            raise GoogleDriveError("Google Drive is not configured")
        local_path = Path(item["local_path"])
        if not local_path.is_file():
            self.ledger.update_upload(
                str(local_path), status="ERROR", error="local file is missing"
            )
            return
        size = local_path.stat().st_size
        if size != item["size"]:
            self.ledger.update_upload(
                str(local_path), status="ERROR", error="local file size changed"
            )
            return
        digest = item.get("md5") or _md5(local_path)
        folder_id = self._folder_for(item["day"], item["relative_name"])
        session = item.get("upload_uri") if item["status"] != "ERROR" else None
        offset = int(item.get("upload_offset") or 0) if session else 0
        if not session:
            session = self.drive.begin_upload(local_path.name, folder_id, size)
            self.ledger.update_upload(
                str(local_path),
                status="UPLOADING",
                md5=digest,
                upload_uri=session,
                upload_offset=0,
                error="",
            )

        def progress(value: int) -> None:
            self.ledger.update_upload(
                str(local_path), status="UPLOADING", upload_offset=value
            )

        try:
            with local_path.open("rb") as source:
                uploaded = self.drive.upload(
                    session,
                    source,
                    size,
                    offset,
                    self.config.offload.upload_chunk_mib * 1024 * 1024,
                    progress,
                )
            metadata = self.drive.file_metadata(str(uploaded["id"]))
        except (GoogleDriveError, OSError, KeyError) as exc:
            self.ledger.update_upload(
                str(local_path),
                status="ERROR",
                upload_uri="",
                upload_offset=0,
                error=str(exc),
            )
            return
        if (
            metadata.get("trashed")
            or int(metadata.get("size", -1)) != size
            or str(metadata.get("md5Checksum", "")).lower() != digest.lower()
        ):
            self.ledger.update_upload(
                str(local_path), status="ERROR", error="remote verification failed"
            )
            return
        self.ledger.update_upload(
            str(local_path),
            status="VERIFIED",
            md5=digest,
            drive_file_id=str(metadata["id"]),
            drive_web_link=metadata.get("webViewLink"),
            upload_offset=size,
            error="",
        )

    def _cleanup_verified_days(self) -> None:
        if not self.ledger.setting("auto_cleanup", self.config.offload.auto_cleanup):
            return
        for day in self.ledger.summary()["days"]:
            if day["status"] != "DRIVE_VERIFIED" or day["cleanup_at"]:
                continue
            removed: list[str] = []
            for item in self.ledger.files_for_day(day["day"]):
                if item["kind"] != "recording" or item["status"] != "VERIFIED":
                    continue
                path = Path(item["local_path"])
                path.unlink(missing_ok=True)
                removed.append(str(path))
            self.ledger.mark_cleanup(day["day"], removed)

    def write_state(self, status: str) -> None:
        atomic_write_json(
            offload_state_path(self.config),
            {
                "updated_at": datetime.now(self.timezone).isoformat(),
                "status": status,
                "error": self.last_error,
                **self.ledger.summary(),
            },
        )

    def run(self) -> None:
        while self.running:
            try:
                self.tick()
            except (GoogleDriveError, OSError, ValueError, KeyError) as exc:
                self.last_error = str(exc)
                self.write_state("ERROR")
            for _ in range(int(self.config.offload.interval_seconds * 2)):
                if not self.running:
                    break
                time.sleep(0.5)


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    config = load_config()
    if not config.offload.enabled:
        raise SystemExit("offload service is disabled in configuration")
    service = OffloadService(config)

    def stop(_signum: int, _frame: object) -> None:
        service.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    service.run()


if __name__ == "__main__":
    main()
