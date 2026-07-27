from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request, send_file

from app.capture.recordings import list_recordings, recording_timing
from app.capture.state import read_json
from app.common.config import AppConfig, load_config
from app.common.paths import (
    capture_state_path,
    control_socket_path,
    preview_path,
    sensor_state_path,
)
from app.sensors.history import load_history


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
            {"updated_at": None, "version": "unknown", "cameras": {}, "events": [], "storage": {}},
        )
        sensors = read_json(
            sensor_state_path(app_config),
            {"updated_at": None, "status": "UNAVAILABLE", "environment": {}, "water": {}},
        )
        return {"capture": capture, "sensors": sensors}

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
        day = request.args.get("day") or datetime.now(timezone).strftime("%Y-%m-%d")
        if len(day) != 10:
            abort(400)
        capture = status_payload()["capture"]
        items = list_recordings(
            app_config.storage.root, timezone, day, _active_paths(capture)
        )
        item_dicts: list[dict[str, Any]] = []
        previous_end: dict[str, datetime] = {}
        for item in items:
            rendered = item.to_dict(app_config.storage.root)
            gap = (
                max(0.0, (item.start - previous_end[item.camera]).total_seconds())
                if item.camera in previous_end
                else 0.0
            )
            rendered["gap_before_seconds"] = gap if gap > 5 else 0
            rendered["restart_boundary"] = bool(item.start.minute or item.start.second)
            previous_end[item.camera] = item.end
            item_dicts.append(rendered)
        active = [
            camera for camera in capture.get("cameras", {}).values()
            if camera.get("current_recording")
            and Path(camera["current_recording"]).parent.parent.name == day
        ]
        return render_template(
            "recordings.html",
            day=day,
            recordings=item_dicts,
            active=active,
            cameras=app_config.cameras,
        )

    @app.get("/recordings/file/<path:relative>")
    def recording_file(relative: str):  # type: ignore[no-untyped-def]
        root = app_config.storage.root.resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            abort(404)
        capture = status_payload()["capture"]
        if path in {item.resolve() for item in _active_paths(capture)}:
            abort(409, "recording is still active")
        if not path.is_file() or path.suffix.lower() != ".mkv":
            abort(404)
        return send_file(path, mimetype="video/x-matroska", conditional=True)

    @app.get("/recordings/review/<path:relative>")
    def recording_review(relative: str):  # type: ignore[no-untyped-def]
        root = app_config.storage.root.resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            abort(404)
        capture = status_payload()["capture"]
        if path in {item.resolve() for item in _active_paths(capture)}:
            abort(409, "recording is still active")
        if not path.is_file() or path.suffix.lower() != ".mkv":
            abort(404)
        timing = recording_timing(path, timezone)
        if timing is None:
            abort(404)
        first_frame_at, last_frame_at, approximate = timing
        samples = load_history(
            app_config.runtime.sensor_dir,
            first_frame_at - timedelta(seconds=1),
            last_frame_at + timedelta(seconds=1),
        )
        return render_template(
            "review.html",
            filename=path.name,
            relative=path.relative_to(root).as_posix(),
            first_frame_at=first_frame_at.isoformat(),
            last_frame_at=last_frame_at.isoformat(),
            timezone=app_config.deployment.timezone,
            approximate=approximate,
            samples=samples,
        )

    return app


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
