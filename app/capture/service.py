from __future__ import annotations

import json
import os
import signal
import socketserver
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.capture.retention import prune_expired, storage_report
from app.capture.state import PauseStore, atomic_write_json
from app.capture.worker import CameraWorker
from app.common.config import AppConfig, load_config
from app.common.models import CameraStatus, CaptureEvent, CaptureSnapshot
from app.common.paths import (
    capture_state_path,
    control_socket_path,
    ensure_runtime_directories,
    persistent_capture_state_path,
)


def git_version(repo: Path | None = None) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=repo or Path(__file__).resolve().parents[2],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class CaptureManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.timezone = ZoneInfo(config.deployment.timezone)
        ensure_runtime_directories(config)
        self.pause_store = PauseStore(persistent_capture_state_path(config))
        self.paused = self.pause_store.load()
        self.workers = {
            name: CameraWorker(name, camera, config)
            for name, camera in config.cameras.items()
        }
        self.events: list[CaptureEvent] = []
        self.version = git_version()
        self.running = True
        self.lock = threading.RLock()
        self._last_retention = 0.0
        self._storage: dict[str, Any] = {}

    def _targets(self, camera: str | None) -> list[str]:
        if camera in (None, "all"):
            return list(self.workers)
        if camera not in self.workers:
            raise ValueError(f"unknown camera: {camera}")
        return [camera]

    def command(self, action: str, camera: str | None = None) -> dict[str, Any]:
        if action not in {"pause", "resume", "restart", "status"}:
            raise ValueError(f"unknown action: {action}")
        with self.lock:
            if action == "status":
                return self.snapshot()
            targets = self._targets(camera)
            for name in targets:
                worker = self.workers[name]
                if action == "pause":
                    self.paused.add(name)
                    worker.pause_recording()
                elif action == "resume":
                    self.paused.discard(name)
                    worker.retry_at = 0
                elif action == "restart":
                    worker.restart_recording(paused=name in self.paused)
                self.events.append(CaptureEvent.now(name, action, f"{action} requested"))
            self.events = self.events[-100:]
            self.pause_store.save(self.paused)
            self.write_snapshot()
            return {"ok": True, "action": action, "cameras": targets}

    def active_paths(self) -> set[Path]:
        return {
            worker.current_recording
            for worker in self.workers.values()
            if worker.current_recording is not None
        }

    def tick(self) -> None:
        now = datetime.now(self.timezone)
        with self.lock:
            for name, worker in self.workers.items():
                event = worker.tick(now, paused=name in self.paused)
                if event in {"exit", "rollover", "stale", "recovered"}:
                    if event == "recovered":
                        detail = (
                            f"recording recovered; estimated gap "
                            f"{worker.runtime.last_gap_seconds or 0:.1f} seconds"
                        )
                    else:
                        detail = worker.runtime.last_error or event
                    self.events.append(
                        CaptureEvent.now(name, event, detail)
                    )
            if time.monotonic() - self._last_retention >= 60:
                self._retention(now)
                self._last_retention = time.monotonic()
            self.events = self.events[-100:]
            self.write_snapshot()

    def _retention(self, now: datetime) -> None:
        active = self.active_paths()
        removed = prune_expired(
            self.config.storage.root,
            self.config.storage.retention_days,
            active,
            now,
        )
        report = storage_report(
            self.config.storage.root,
            active,
            self.config.storage.minimum_free_gib,
            self.config.storage.capacity_warning_days,
            self.config.deployment.mode == "development",
            now,
        )
        self._storage = report.to_dict()
        if removed:
            self.events.append(
                CaptureEvent.now("all", "retention", f"removed {len(removed)} expired files")
            )
        if report.reserve_reached:
            for name, worker in self.workers.items():
                if name not in self.paused:
                    self.paused.add(name)
                    worker.stop(paused=True)
            self.pause_store.save(self.paused)
            self.events.append(
                CaptureEvent.now(
                    "all", "reserve", "recording paused because free-space reserve was reached"
                )
            )

    def snapshot(self) -> dict[str, Any]:
        snapshot = CaptureSnapshot(
            datetime.now(self.timezone).isoformat(),
            self.version,
            {name: worker.runtime for name, worker in self.workers.items()},
            self.events,
            self._storage,
        )
        return snapshot.to_dict()

    def write_snapshot(self) -> None:
        atomic_write_json(capture_state_path(self.config), self.snapshot())

    def shutdown(self) -> None:
        with self.lock:
            self.running = False
            for name, worker in self.workers.items():
                worker.stop(paused=name in self.paused)
            self.write_snapshot()


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            request = json.loads(self.rfile.readline(8192))
            response = self.server.manager.command(  # type: ignore[attr-defined]
                str(request.get("action", "")), request.get("camera")
            )
        except (ValueError, json.JSONDecodeError) as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode())


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: Path, manager: CaptureManager):
        path.unlink(missing_ok=True)
        self.manager = manager
        super().__init__(str(path), _ControlHandler)
        os.chmod(path, 0o660)


def main() -> None:
    manager = CaptureManager(load_config())
    socket_path = control_socket_path(manager.config)
    server = ControlServer(socket_path, manager)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def stop(_signum: int, _frame: object) -> None:
        manager.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while manager.running:
            manager.tick()
            time.sleep(1)
    finally:
        server.shutdown()
        server.server_close()
        socket_path.unlink(missing_ok=True)
        manager.shutdown()


if __name__ == "__main__":
    main()
