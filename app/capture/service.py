from __future__ import annotations

import json
import os
import signal
import socketserver
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta
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
    CONTROL_UNLOCK_SECONDS = 60

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
        self.control_locked = True
        self.control_unlocked_until: datetime | None = None
        self.control_lock_history: list[CaptureEvent] = []
        self.version = git_version()
        self.running = True
        self.lock = threading.RLock()
        self._last_retention = 0.0
        self._storage: dict[str, Any] = {}
        self._record_control_lock_event(
            "control_lock",
            "controls locked on service start",
        )

    def _targets(self, camera: str | None) -> list[str]:
        if camera in (None, "all"):
            return [
                name for name, worker in self.workers.items()
                if worker.camera.enabled
            ]
        if camera not in self.workers:
            raise ValueError(f"unknown camera: {camera}")
        if not self.workers[camera].camera.enabled:
            raise ValueError(f"camera is not enabled: {camera}")
        return [camera]

    def _record_control_lock_event(self, kind: str, detail: str) -> None:
        event = CaptureEvent(
            datetime.now(self.timezone).isoformat(),
            "system",
            kind,
            detail,
        )
        self.control_lock_history.append(event)
        self.control_lock_history = self.control_lock_history[-20:]
        self.events.append(event)
        self.events = self.events[-100:]

    def _set_control_locked(self, locked: bool, detail: str) -> None:
        if self.control_locked == locked:
            return
        self.control_locked = locked
        if locked:
            self.control_unlocked_until = None
            self._record_control_lock_event("control_lock", detail)
        else:
            self.control_unlocked_until = (
                datetime.now(self.timezone)
                + timedelta(seconds=self.CONTROL_UNLOCK_SECONDS)
            )
            self._record_control_lock_event("control_unlock", detail)

    def _expire_control_lock(self) -> None:
        if (
            not self.control_locked
            and self.control_unlocked_until is not None
            and datetime.now(self.timezone) >= self.control_unlocked_until
        ):
            self._set_control_locked(True, "controls locked after 60-second timeout")

    def _control_lock_snapshot(self) -> dict[str, Any]:
        self._expire_control_lock()
        return {
            "locked": self.control_locked,
            "unlocked_until": (
                self.control_unlocked_until.isoformat()
                if self.control_unlocked_until else None
            ),
            "timeout_seconds": self.CONTROL_UNLOCK_SECONDS,
            "history": [asdict(event) for event in self.control_lock_history],
        }

    def command(self, action: str, camera: str | None = None) -> dict[str, Any]:
        if action not in {
            "pause", "resume", "restart", "status", "lock", "unlock"
        }:
            raise ValueError(f"unknown action: {action}")
        with self.lock:
            self._expire_control_lock()
            if action == "status":
                return self.snapshot()
            if action == "unlock":
                self._set_control_locked(False, "controls manually unlocked")
                self.write_snapshot()
                return {
                    "ok": True,
                    "action": action,
                    "control_lock": self._control_lock_snapshot(),
                }
            if action == "lock":
                self._set_control_locked(True, "controls manually locked")
                self.write_snapshot()
                return {
                    "ok": True,
                    "action": action,
                    "control_lock": self._control_lock_snapshot(),
                }
            if self.control_locked:
                return {
                    "ok": False,
                    "code": "controls_locked",
                    "error": "capture controls are locked",
                }
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
            self._set_control_locked(True, f"controls locked after {action}")
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
        # When verified offload is enabled, it exclusively owns deletion.
        # Age-based retention cannot prove that a remote copy exists.
        removed = [] if self.config.offload.enabled else prune_expired(
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
        result = snapshot.to_dict()
        result["control_lock"] = self._control_lock_snapshot()
        return result

    def write_snapshot(self) -> None:
        atomic_write_json(capture_state_path(self.config), self.snapshot())

    def shutdown(self) -> None:
        with self.lock:
            self.running = False
            threads = [
                threading.Thread(
                    target=worker.stop,
                    kwargs={"paused": name in self.paused},
                    name=f"kratky-stop-{name}",
                )
                for name, worker in self.workers.items()
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
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


_ThreadingUnixStreamServer = getattr(
    socketserver,
    "ThreadingUnixStreamServer",
    socketserver.ThreadingTCPServer,
)


class ControlServer(_ThreadingUnixStreamServer):
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
