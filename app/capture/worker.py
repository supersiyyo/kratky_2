from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable

from app.capture.ffmpeg import build_capture_command, build_recorder_command
from app.capture.recordings import next_hour, recording_path
from app.common.config import AppConfig, CameraConfig
from app.common.models import CameraRuntime, CameraStatus
from app.common.paths import preview_path


LOGGER = logging.getLogger("kratky.capture.worker")


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
        self.capture_process: subprocess.Popen[bytes] | None = None
        self.recording_process: subprocess.Popen[bytes] | None = None
        self.current_recording: Path | None = None
        self.rollover_at: datetime | None = None
        self.retry_at: float = 0
        self.recording_retry_at: float = 0
        self.failure_count = 0
        self.recording_failure_count = 0
        self.gap_started_at: datetime | None = None
        self._capture_started_at: datetime | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._recorder_lock = threading.Lock()
        self._frame_size = 0
        self.runtime = CameraRuntime(
            name=name,
            status=CameraStatus.STARTING if camera.enabled else CameraStatus.PLANNED,
            enabled=camera.enabled,
            required=camera.required,
        )

    def start_capture(self, now: datetime) -> None:
        if not self.camera.enabled:
            self.runtime.status = CameraStatus.PLANNED
            return
        device = Path(self.camera.device or "")
        if not device.exists():
            self._capture_failed(now, f"camera device is missing: {device}")
            return
        preview = preview_path(self.config, self.name)
        preview.unlink(missing_ok=True)
        command = build_capture_command(self.camera, preview)
        try:
            process = self.process_factory(
                command.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            self._capture_failed(now, f"could not start camera capture: {exc}")
            return
        if process.stdout is None:
            process.kill()
            process.wait(timeout=5)
            self._capture_failed(now, "camera capture did not expose its frame pipe")
            return
        self.capture_process = process
        self._frame_size = command.frame_size
        self._capture_started_at = datetime.now(now.tzinfo)
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._forward_frames,
            args=(process.stdout,),
            name=f"kratky-{self.name}-frames",
            daemon=True,
        )
        self._reader_thread.start()
        self.runtime.status = CameraStatus.STARTING
        self.runtime.last_error = None
        self.runtime.next_retry_at = None

    def start_recording(self, now: datetime) -> None:
        if self.recording_process is not None or self.capture_process is None:
            return
        recording = recording_path(self.config.storage.root, self.name, now)
        command = build_recorder_command(self.camera, recording)
        try:
            process = self.process_factory(
                command.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            self._recording_failed(now, f"could not start recorder: {exc}")
            return
        if process.stdin is None:
            process.kill()
            process.wait(timeout=5)
            self._recording_failed(now, "recorder did not expose its frame pipe")
            return
        with self._recorder_lock:
            self.recording_process = process
        self.current_recording = recording
        self.rollover_at = next_hour(now)
        self.runtime.status = CameraStatus.STARTING
        self.runtime.current_recording = str(recording)
        self.runtime.started_at = now.isoformat()
        self.runtime.last_error = None
        self.runtime.next_retry_at = None

    def _read_frame(self, stream: BinaryIO) -> bytes | None:
        frame = bytearray()
        while len(frame) < self._frame_size and not self._reader_stop.is_set():
            chunk = stream.read(self._frame_size - len(frame))
            if not chunk:
                return None
            frame.extend(chunk)
        return bytes(frame) if len(frame) == self._frame_size else None

    @staticmethod
    def _write_frame(stream: BinaryIO, frame: bytes) -> None:
        view = memoryview(frame)
        while view:
            written = stream.write(view)
            if not written:
                raise BrokenPipeError("recorder frame pipe closed")
            view = view[written:]

    def _forward_frames(self, stream: BinaryIO) -> None:
        while not self._reader_stop.is_set():
            frame = self._read_frame(stream)
            if frame is None:
                return
            with self._recorder_lock:
                process = self.recording_process
                target = process.stdin if process is not None else None
                if process is None or process.poll() is not None or target is None:
                    continue
                try:
                    self._write_frame(target, frame)
                except (BrokenPipeError, OSError):
                    LOGGER.warning("%s recorder frame pipe closed", self.name)

    @staticmethod
    def _wait_or_terminate(
        process: subprocess.Popen[bytes],
        timeout: float,
    ) -> None:
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)

    def stop_recording(self) -> None:
        with self._recorder_lock:
            process = self.recording_process
            self.recording_process = None
            if process is not None and process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        if process is not None and process.poll() is None:
            self._wait_or_terminate(
                process,
                self.config.runtime.shutdown_timeout_seconds,
            )
        self.current_recording = None
        self.rollover_at = None
        self.runtime.current_recording = None

    def stop_capture(self) -> None:
        process = self.capture_process
        self.capture_process = None
        self._reader_stop.set()
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                self._wait_or_terminate(
                    process,
                    self.config.runtime.shutdown_timeout_seconds,
                )
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2)
        self._reader_thread = None
        self._capture_started_at = None

    def pause_recording(self) -> None:
        self.stop_recording()
        self.runtime.status = (
            CameraStatus.PLANNED if not self.camera.enabled else CameraStatus.PAUSED
        )

    def restart_recording(self, paused: bool = False) -> None:
        self.stop_recording()
        self.recording_retry_at = 0
        self.runtime.status = (
            CameraStatus.PLANNED
            if not self.camera.enabled
            else CameraStatus.PAUSED if paused else CameraStatus.STARTING
        )

    def stop(self, paused: bool = False) -> None:
        self.stop_recording()
        self.stop_capture()
        self.runtime.status = (
            CameraStatus.PLANNED
            if not self.camera.enabled
            else CameraStatus.PAUSED if paused else CameraStatus.STARTING
        )

    def _capture_failed(self, now: datetime, message: str) -> None:
        if self.gap_started_at is None:
            self.gap_started_at = now
        self.stop_recording()
        self.stop_capture()
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

    def _recording_failed(self, now: datetime, message: str) -> None:
        if self.gap_started_at is None:
            self.gap_started_at = now
        self.stop_recording()
        self.runtime.last_error = message
        self.runtime.reconnects += 1
        delay = self.BACKOFF[
            min(self.recording_failure_count, len(self.BACKOFF) - 1)
        ]
        self.recording_failure_count += 1
        self.recording_retry_at = now.timestamp() + delay
        self.runtime.next_retry_at = datetime.fromtimestamp(
            self.recording_retry_at, now.tzinfo
        ).isoformat()
        self.runtime.status = (
            CameraStatus.RECONNECTING if self.runtime.reconnects > 1 else CameraStatus.ERROR
        )

    def _preview_is_fresh(self, now: datetime) -> bool:
        preview = preview_path(self.config, self.name)
        try:
            modified = preview.stat().st_mtime
        except OSError:
            started = self._capture_started_at or now
            if (now - started).total_seconds() > self.config.runtime.stale_after_seconds:
                self._capture_failed(now, "camera produced no preview frame")
            else:
                self.runtime.status = CameraStatus.STARTING
            return False
        age = now.timestamp() - modified
        self.runtime.last_frame_at = datetime.fromtimestamp(
            modified, now.tzinfo
        ).isoformat()
        if age > self.config.runtime.stale_after_seconds:
            self._capture_failed(now, f"preview frame is stale by {age:.1f} seconds")
            return False
        self.failure_count = 0
        self.runtime.next_retry_at = None
        return True

    def tick(self, now: datetime, paused: bool = False) -> str | None:
        if not self.camera.enabled:
            self.runtime.status = CameraStatus.PLANNED
            return None
        if self.capture_process is None:
            if now.timestamp() >= self.retry_at:
                self.start_capture(now)
                return "start"
            return None
        capture_code = self.capture_process.poll()
        if capture_code is not None:
            self._capture_failed(now, f"camera capture exited with status {capture_code}")
            return "exit"
        if not self._preview_is_fresh(now):
            return "stale" if self.capture_process is None else None
        if paused:
            if self.recording_process is not None:
                self.pause_recording()
            self.runtime.status = CameraStatus.PAUSED
            self.runtime.last_error = None
            return None
        if self.recording_process is not None:
            recording_code = self.recording_process.poll()
            if recording_code is not None:
                self._recording_failed(
                    now,
                    f"recorder exited with status {recording_code}",
                )
                return "exit"
        if self.recording_process is None:
            if now.timestamp() >= self.recording_retry_at:
                self.start_recording(now)
                return "start"
            return None
        if self.rollover_at and now >= self.rollover_at:
            self.restart_recording()
            self.start_recording(datetime.now(now.tzinfo))
            return "rollover"
        recovered = self.gap_started_at is not None
        if recovered:
            self.runtime.last_gap_seconds = round(
                (now - self.gap_started_at).total_seconds(),
                1,
            )
            self.gap_started_at = None
        self.recording_failure_count = 0
        self.runtime.status = CameraStatus.RECORDING
        self.runtime.last_error = None
        self.runtime.next_retry_at = None
        return "recovered" if recovered else None
