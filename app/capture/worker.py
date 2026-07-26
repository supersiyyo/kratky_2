from __future__ import annotations

import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from app.capture.ffmpeg import build_ffmpeg_command
from app.capture.recordings import next_hour, recording_path
from app.common.config import AppConfig, CameraConfig
from app.common.models import CameraRuntime, CameraStatus
from app.common.paths import preview_path


class CameraWorker:
    BACKOFF = (2, 5, 10, 30)

    def __init__(
        self,
        name: str,
        camera: CameraConfig,
        config: AppConfig,
        process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ):
        self.name = name
        self.camera = camera
        self.config = config
        self.process_factory = process_factory
        self.process: subprocess.Popen[bytes] | None = None
        self.current_recording: Path | None = None
        self.rollover_at: datetime | None = None
        self.retry_at: float = 0
        self.failure_count = 0
        self.gap_started_at: datetime | None = None
        self._started_process_at: datetime | None = None
        self.runtime = CameraRuntime(
            name=name,
            status=CameraStatus.STARTING if camera.enabled else CameraStatus.PLANNED,
            enabled=camera.enabled,
            required=camera.required,
        )

    def start(self, now: datetime) -> None:
        if not self.camera.enabled:
            self.runtime.status = CameraStatus.PLANNED
            return
        device = Path(self.camera.device or "")
        if not device.exists():
            self._failed(now, f"camera device is missing: {device}")
            return
        recording = recording_path(self.config.storage.root, self.name, now)
        preview = preview_path(self.config, self.name)
        preview.unlink(missing_ok=True)
        command = build_ffmpeg_command(self.camera, recording, preview)
        try:
            self.process = self.process_factory(
                command.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self._failed(now, f"could not start FFmpeg: {exc}")
            return
        self.current_recording = recording
        self.rollover_at = next_hour(now)
        self._started_process_at = now
        self.runtime.status = CameraStatus.STARTING
        self.runtime.current_recording = str(recording)
        self.runtime.started_at = now.isoformat()
        self.runtime.last_error = None

    def _failed(self, now: datetime, message: str) -> None:
        if self.gap_started_at is None:
            self.gap_started_at = now
        self.process = None
        self.current_recording = None
        self.runtime.current_recording = None
        self.runtime.last_error = message
        self.runtime.reconnects += 1
        delay = self.BACKOFF[min(self.failure_count, len(self.BACKOFF) - 1)]
        self.failure_count += 1
        self.retry_at = now.timestamp() + delay
        self.runtime.next_retry_at = datetime.fromtimestamp(
            self.retry_at, now.tzinfo
        ).isoformat()
        self.runtime.status = (
            CameraStatus.RECONNECTING if self.runtime.reconnects > 1 else CameraStatus.ERROR
        )

    def stop(self, paused: bool = False) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=self.config.runtime.shutdown_timeout_seconds)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=5)
        self.process = None
        self.current_recording = None
        self.rollover_at = None
        self._started_process_at = None
        self.runtime.current_recording = None
        self.runtime.status = (
            CameraStatus.PLANNED
            if not self.camera.enabled
            else CameraStatus.PAUSED if paused else CameraStatus.STARTING
        )

    def tick(self, now: datetime, paused: bool = False) -> str | None:
        if not self.camera.enabled:
            self.runtime.status = CameraStatus.PLANNED
            return None
        if paused:
            if self.process:
                self.stop(paused=True)
            self.runtime.status = CameraStatus.PAUSED
            return None
        if self.process is None:
            if now.timestamp() >= self.retry_at:
                self.start(now)
                return "start"
            return None
        code = self.process.poll()
        if code is not None:
            self._failed(now, f"FFmpeg exited with status {code}")
            return "exit"
        if self.rollover_at and now >= self.rollover_at:
            self.stop()
            self.start(now)
            return "rollover"
        preview = preview_path(self.config, self.name)
        try:
            modified = preview.stat().st_mtime
            age = now.timestamp() - modified
            self.runtime.last_frame_at = datetime.fromtimestamp(
                modified, now.tzinfo
            ).isoformat()
            if age <= self.config.runtime.stale_after_seconds:
                recovered = self.gap_started_at is not None
                if recovered:
                    self.runtime.last_gap_seconds = round(
                        (now - self.gap_started_at).total_seconds(), 1
                    )
                    self.gap_started_at = None
                self.runtime.status = CameraStatus.RECORDING
                self.failure_count = 0
                self.runtime.next_retry_at = None
                if recovered:
                    return "recovered"
            else:
                self.stop()
                self._failed(now, f"preview frame is stale by {age:.1f} seconds")
                return "stale"
        except OSError:
            started = self._started_process_at or now
            if (now - started).total_seconds() > self.config.runtime.stale_after_seconds:
                self.stop()
                self._failed(now, "FFmpeg produced no preview frame")
                return "stale"
            self.runtime.status = CameraStatus.STARTING
        return None
