from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.capture.state import atomic_write_json, read_json
from app.common.config import AppConfig, load_config
from app.common.paths import (
    capture_state_path,
    control_socket_path,
    preview_path,
    sensor_state_path,
)
from app.timelapse.render import combined_timelapse_path, timelapse_root
from app.offload.google_drive import (
    AuthorizationPending,
    CredentialStore,
    GoogleDriveError,
    TokenStore,
    credentials_from_mapping,
)
from app.offload.ledger import OffloadLedger
from app.offload.service import (
    build_google,
    configured_credentials,
    credential_path,
    ledger_path,
    offload_state_path,
    pending_auth_path,
    provision_project,
    token_path,
)


def _active_paths(capture: dict[str, Any]) -> set[Path]:
    result: set[Path] = set()
    for camera in capture.get("cameras", {}).values():
        if camera.get("current_recording"):
            result.add(Path(camera["current_recording"]))
    return result


def send_command(config: AppConfig, action: str, camera: str | None) -> dict[str, Any]:
    payload = (json.dumps({"action": action, "camera": camera}) + "\n").encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(control_socket_path(config)))
            client.sendall(payload)
            chunks = bytearray()
            while b"\n" not in chunks and len(chunks) < 65536:
                data = client.recv(8192)
                if not data:
                    break
                chunks.extend(data)
        return json.loads(bytes(chunks).splitlines()[0])
    except (OSError, json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError(f"capture manager is unavailable: {exc}") from exc


def create_app(config: AppConfig | None = None) -> Flask:
    app_config = config or load_config()
    app = Flask(__name__)
    app.config["KRATKY_CONFIG"] = app_config
    timezone = ZoneInfo(app_config.deployment.timezone)

    def status_payload() -> dict[str, Any]:
        capture = read_json(
            capture_state_path(app_config),
            {
                "updated_at": None,
                "version": "unknown",
                "cameras": {},
                "events": [],
                "storage": {},
                "control_lock": {
                    "locked": True,
                    "unlocked_until": None,
                    "timeout_seconds": 60,
                    "history": [],
                },
            },
        )
        sensors = read_json(
            sensor_state_path(app_config),
            {"updated_at": None, "status": "UNAVAILABLE", "environment": {}, "water": {}},
        )
        return {"capture": capture, "sensors": sensors}

    def offload_payload() -> dict[str, Any]:
        credentials = configured_credentials(app_config)
        configured = credentials is not None
        ledger = OffloadLedger(ledger_path(app_config))
        state = read_json(offload_state_path(app_config), {})
        if not app_config.offload.enabled:
            state = {"status": "SETUP_ONLY"}
        pending = read_json(pending_auth_path(app_config), None)
        pending_public = (
            {
                "user_code": pending.get("user_code"),
                "verification_url": pending.get("verification_url"),
                "interval": pending.get("interval", 5),
            }
            if isinstance(pending, dict)
            else None
        )
        return {
            "configured": configured,
            "service_enabled": app_config.offload.enabled,
            "credential": credentials.public_dict() if credentials else None,
            "connected": TokenStore(token_path(app_config)).load() is not None,
            "authorization_pending": isinstance(pending, dict),
            "authorization": pending_public,
            "service": state,
            **ledger.summary(),
        }

    @app.after_request
    def no_cache(response):  # type: ignore[no-untyped-def]
        if request.path.startswith("/api/") or request.path.startswith("/preview/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/")
    def index():  # type: ignore[no-untyped-def]
        return render_template("index.html", config=app_config)

    @app.get("/api/status")
    def status():  # type: ignore[no-untyped-def]
        return jsonify(status_payload())

    @app.get("/offload")
    def offload():  # type: ignore[no-untyped-def]
        return render_template("offload.html", config=app_config)

    @app.get("/api/offload/status")
    def offload_status():  # type: ignore[no-untyped-def]
        return jsonify(offload_payload())

    @app.post("/api/offload/credentials")
    def offload_credentials():  # type: ignore[no-untyped-def]
        uploaded = request.files.get("credentials")
        if uploaded is None or not uploaded.filename:
            return jsonify({"ok": False, "error": "choose a Google OAuth JSON file"}), 400
        existing = configured_credentials(app_config)
        if existing and request.form.get("confirm") != "REPLACE":
            return jsonify({"ok": False, "error": "replacement confirmation is required"}), 409
        raw = uploaded.stream.read(65537)
        if len(raw) > 65536:
            return jsonify({"ok": False, "error": "OAuth JSON must be 64 KiB or smaller"}), 413
        try:
            value = json.loads(raw.decode("utf-8"))
            credentials = credentials_from_mapping(value)
        except (UnicodeDecodeError, json.JSONDecodeError, GoogleDriveError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        directory = credential_path(app_config).parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        CredentialStore(credential_path(app_config)).save(credentials)
        TokenStore(token_path(app_config)).clear()
        pending_auth_path(app_config).unlink(missing_ok=True)
        OffloadLedger(ledger_path(app_config)).clear_destination()
        return jsonify({"ok": True, "credential": credentials.public_dict()})

    @app.post("/api/offload/credentials/remove")
    def offload_credentials_remove():  # type: ignore[no-untyped-def]
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "REMOVE":
            return jsonify({"ok": False, "error": "confirmation is required"}), 400
        CredentialStore(credential_path(app_config)).clear()
        TokenStore(token_path(app_config)).clear()
        pending_auth_path(app_config).unlink(missing_ok=True)
        OffloadLedger(ledger_path(app_config)).clear_destination()
        return jsonify({"ok": True})

    @app.post("/api/offload/connect")
    def offload_connect():  # type: ignore[no-untyped-def]
        if configured_credentials(app_config) is None:
            return jsonify({"ok": False, "error": "Google OAuth application is not configured"}), 503
        body = request.get_json(silent=True) or {}
        project_name = " ".join(str(body.get("project_name", "")).split()).strip()
        if not project_name or len(project_name) > 120:
            return jsonify({"ok": False, "error": "enter a project name"}), 400
        try:
            oauth, _drive = build_google(app_config)
            authorization = oauth.start()
            atomic_write_json(
                pending_auth_path(app_config),
                {
                    "device_code": authorization.device_code,
                    "project_name": project_name,
                    "auto_cleanup": bool(body.get("auto_cleanup", True))
                    if app_config.offload.enabled
                    else False,
                    **authorization.public_dict(),
                },
                mode=0o600,
            )
        except GoogleDriveError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        return jsonify({"ok": True, **authorization.public_dict()})

    @app.post("/api/offload/connect/status")
    def offload_connect_status():  # type: ignore[no-untyped-def]
        pending = read_json(pending_auth_path(app_config), None)
        if not isinstance(pending, dict) or not pending.get("device_code"):
            return jsonify({"ok": False, "error": "no authorization is pending"}), 404
        try:
            oauth, drive = build_google(app_config)
            if TokenStore(token_path(app_config)).load() is None:
                oauth.finish(str(pending["device_code"]))
            result = provision_project(
                OffloadLedger(ledger_path(app_config)),
                drive,
                str(pending["project_name"]),
                bool(pending.get("auto_cleanup", True)),
            )
            pending_auth_path(app_config).unlink(missing_ok=True)
        except AuthorizationPending:
            return jsonify({"ok": True, "pending": True}), 202
        except (GoogleDriveError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        return jsonify({"ok": True, "pending": False, **result})

    @app.post("/api/offload/pause")
    def offload_pause():  # type: ignore[no-untyped-def]
        body = request.get_json(silent=True) or {}
        paused = bool(body.get("paused", True))
        OffloadLedger(ledger_path(app_config)).set_setting("paused", paused)
        return jsonify({"ok": True, "paused": paused})

    @app.post("/api/offload/cleanup")
    def offload_cleanup():  # type: ignore[no-untyped-def]
        if not app_config.offload.enabled:
            return jsonify({"ok": False, "error": "cleanup is disabled in preview setup mode"}), 409
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled", False))
        OffloadLedger(ledger_path(app_config)).set_setting("auto_cleanup", enabled)
        return jsonify({"ok": True, "auto_cleanup": enabled})

    @app.post("/api/offload/disconnect")
    def offload_disconnect():  # type: ignore[no-untyped-def]
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "DISCONNECT":
            return jsonify({"ok": False, "error": "confirmation is required"}), 400
        TokenStore(token_path(app_config)).clear()
        pending_auth_path(app_config).unlink(missing_ok=True)
        OffloadLedger(ledger_path(app_config)).clear_destination()
        return jsonify({"ok": True})

    @app.post("/api/control")
    def control():  # type: ignore[no-untyped-def]
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        camera = body.get("camera")
        if action not in {"pause", "resume", "restart"}:
            return jsonify({"ok": False, "error": "invalid action"}), 400
        if camera not in {None, "all", *app_config.cameras.keys()}:
            return jsonify({"ok": False, "error": "invalid camera"}), 400
        try:
            result = send_command(app_config, action, camera)
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        if result.get("ok"):
            return jsonify(result)
        return jsonify(result), (423 if result.get("code") == "controls_locked" else 400)

    @app.post("/api/control-lock")
    def control_lock():  # type: ignore[no-untyped-def]
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in {"lock", "unlock"}:
            return jsonify({"ok": False, "error": "invalid lock action"}), 400
        try:
            result = send_command(app_config, action, None)
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.get("/preview/<camera>.jpg")
    def preview(camera: str):  # type: ignore[no-untyped-def]
        if camera not in app_config.cameras:
            abort(404)
        path = preview_path(app_config, camera)
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="image/jpeg", conditional=False)

    @app.get("/recordings")
    def recordings():  # type: ignore[no-untyped-def]
        if selected_day := request.args.get("day"):
            return redirect(url_for("recording_day_detail", day=selected_day))
        days: list[dict[str, Any]] = []
        root = timelapse_root(app_config)
        if root.is_dir():
            for directory in sorted(root.iterdir(), reverse=True):
                if not directory.is_dir() or not _valid_day(directory.name):
                    continue
                combined = combined_timelapse_path(app_config, directory.name)
                if not combined.is_file():
                    continue
                days.append(
                    {
                        "day": directory.name,
                        "filename": combined.name,
                        "size": combined.stat().st_size,
                    }
                )
        return render_template(
            "recording_days.html",
            days=days,
        )

    @app.get("/recordings/<day>")
    def recording_day_detail(day: str):  # type: ignore[no-untyped-def]
        if not _valid_day(day):
            abort(404)
        combined = combined_timelapse_path(app_config, day)
        if not combined.is_file():
            abort(404)
        return render_template(
            "recordings.html",
            day=day,
            timelapse={
                "filename": combined.name,
                "size": combined.stat().st_size,
            },
        )

    @app.get("/recordings/timelapse/<day>/combined.mp4")
    def recording_day_timelapse(day: str):  # type: ignore[no-untyped-def]
        if not _valid_day(day):
            abort(404)
        path = combined_timelapse_path(app_config, day)
        if not path.is_file():
            abort(404)
        return send_file(
            path,
            mimetype="video/mp4",
            conditional=True,
            download_name=path.name,
        )

    return app


def _valid_day(value: str) -> bool:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except ValueError:
        return False


def main() -> None:
    config = load_config()
    create_app(config).run(
        host=config.dashboard.host,
        port=config.dashboard.port,
        threaded=True,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
