from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


class OffloadLedger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS days (
                    day TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'LOCAL',
                    expected_files INTEGER NOT NULL DEFAULT 0,
                    verified_files INTEGER NOT NULL DEFAULT 0,
                    local_bytes INTEGER NOT NULL DEFAULT 0,
                    drive_folder_id TEXT,
                    drive_web_link TEXT,
                    verified_at TEXT,
                    cleanup_at TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS files (
                    local_path TEXT PRIMARY KEY,
                    day TEXT NOT NULL REFERENCES days(day),
                    camera TEXT,
                    kind TEXT NOT NULL,
                    relative_name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    md5 TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    drive_file_id TEXT,
                    drive_web_link TEXT,
                    upload_uri TEXT,
                    upload_offset INTEGER NOT NULL DEFAULT 0,
                    verified_at TEXT,
                    local_removed_at TEXT,
                    error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS files_day_status
                    ON files(day, status);
                """
            )

    def setting(self, key: str, default: Any = None) -> Any:
        with self.connection() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def clear_destination(self) -> None:
        with self.connection() as db:
            db.execute(
                "DELETE FROM settings WHERE key IN "
                "('project_name','project_folder_id','project_web_link') "
                "OR key LIKE 'folder:%'"
            )

    def register_day(
        self,
        day: str,
        files: list[dict[str, Any]],
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO days(day, expected_files, local_bytes) VALUES(?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "expected_files = excluded.expected_files, local_bytes = excluded.local_bytes",
                (day, len(files) + 1, sum(int(item["size"]) for item in files)),
            )
            for item in files:
                db.execute(
                    """
                    INSERT INTO files(
                        local_path, day, camera, kind, relative_name, size
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(local_path) DO UPDATE SET
                        size = excluded.size,
                        relative_name = excluded.relative_name
                    """,
                    (
                        str(item["path"]),
                        day,
                        item.get("camera"),
                        item["kind"],
                        item["relative_name"],
                        int(item["size"]),
                    ),
                )
            self._refresh_day(db, day)

    def register_manifest(self, day: str, path: Path, size: int) -> None:
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO files(local_path, day, kind, relative_name, size)
                VALUES(?, ?, 'manifest', 'manifest.json', ?)
                ON CONFLICT(local_path) DO UPDATE SET size = excluded.size
                """,
                (str(path), day, size),
            )
            self._refresh_day(db, day)

    def pending_file(self) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT files.* FROM files
                JOIN days USING(day)
                WHERE files.status NOT IN ('VERIFIED', 'LOCAL_REMOVED')
                ORDER BY files.day, CASE files.kind WHEN 'manifest' THEN 1 ELSE 0 END,
                         files.relative_name
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def files_for_day(self, day: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM files WHERE day = ? ORDER BY relative_name", (day,)
            ).fetchall()
        return [dict(row) for row in rows]

    def source_files_verified(self, day: str) -> bool:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status IN ('VERIFIED','LOCAL_REMOVED') THEN 1 ELSE 0 END) AS done
                FROM files WHERE day = ? AND kind != 'manifest'
                """,
                (day,),
            ).fetchone()
        return bool(row and row["total"] and row["total"] == row["done"])

    def update_upload(
        self,
        local_path: str,
        *,
        status: str,
        md5: str | None = None,
        upload_uri: str | None = None,
        upload_offset: int | None = None,
        drive_file_id: str | None = None,
        drive_web_link: str | None = None,
        error: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "md5": md5,
            "upload_uri": upload_uri,
            "upload_offset": upload_offset,
            "drive_file_id": drive_file_id,
            "drive_web_link": drive_web_link,
            "error": error,
        }
        assignments = ["status = :status"]
        for key in tuple(values)[1:]:
            if values[key] is not None:
                assignments.append(f"{key} = :{key}")
        if status == "VERIFIED":
            assignments.append("verified_at = :now")
            values["now"] = datetime.now().astimezone().isoformat()
        if status == "ERROR":
            assignments.append("retry_count = retry_count + 1")
        values["local_path"] = local_path
        with self.connection() as db:
            db.execute(
                f"UPDATE files SET {', '.join(assignments)} WHERE local_path = :local_path",
                values,
            )
            row = db.execute(
                "SELECT day FROM files WHERE local_path = ?", (local_path,)
            ).fetchone()
            if row:
                self._refresh_day(db, row["day"])

    def set_day_folder(self, day: str, file_id: str, web_link: str | None) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE days SET drive_folder_id = ?, drive_web_link = ? WHERE day = ?",
                (file_id, web_link, day),
            )

    def day(self, day: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM days WHERE day = ?", (day,)).fetchone()
        return dict(row) if row else None

    def mark_cleanup(self, day: str, removed_paths: list[str]) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connection() as db:
            for path in removed_paths:
                db.execute(
                    "UPDATE files SET status='LOCAL_REMOVED', local_removed_at=? "
                    "WHERE local_path=? AND status='VERIFIED'",
                    (now, path),
                )
            db.execute("UPDATE days SET cleanup_at=? WHERE day=?", (now, day))
            self._refresh_day(db, day)

    def mark_file_removed(self, local_path: str) -> None:
        """Persist each deletion so cleanup can resume safely after interruption."""
        now = datetime.now().astimezone().isoformat()
        with self.connection() as db:
            row = db.execute(
                "SELECT day FROM files WHERE local_path=? AND kind='recording'",
                (local_path,),
            ).fetchone()
            if not row:
                return
            db.execute(
                "UPDATE files SET status='LOCAL_REMOVED', local_removed_at=? "
                "WHERE local_path=? AND status='VERIFIED'",
                (now, local_path),
            )
            self._refresh_day(db, row["day"])

    def summary(self) -> dict[str, Any]:
        with self.connection() as db:
            days = [
                dict(row)
                for row in db.execute("SELECT * FROM days ORDER BY day DESC").fetchall()
            ]
            current = db.execute(
                "SELECT relative_name, day, size, upload_offset, status, error "
                "FROM files WHERE status NOT IN ('VERIFIED','LOCAL_REMOVED') "
                "ORDER BY day, relative_name LIMIT 1"
            ).fetchone()
        return {
            "project_name": self.setting("project_name"),
            "project_folder_id": self.setting("project_folder_id"),
            "project_web_link": self.setting("project_web_link"),
            "auto_cleanup": bool(self.setting("auto_cleanup", True)),
            "paused": bool(self.setting("paused", False)),
            "days": days,
            "current": dict(current) if current else None,
        }

    def _refresh_day(self, db: sqlite3.Connection, day: str) -> None:
        counts = db.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('VERIFIED','LOCAL_REMOVED') THEN 1 ELSE 0 END) AS verified,
                   SUM(CASE WHEN status='LOCAL_REMOVED' THEN 1 ELSE 0 END) AS removed,
                   SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS errors
            FROM files WHERE day=?
            """,
            (day,),
        ).fetchone()
        total = int(counts["total"] or 0)
        verified = int(counts["verified"] or 0)
        errors = int(counts["errors"] or 0)
        error_row = db.execute(
            "SELECT error FROM files WHERE day=? AND status='ERROR' "
            "AND error IS NOT NULL ORDER BY relative_name LIMIT 1",
            (day,),
        ).fetchone()
        error = error_row["error"] if error_row else None
        expected = db.execute(
            "SELECT expected_files FROM days WHERE day=?", (day,)
        ).fetchone()["expected_files"]
        if errors:
            status = "ERROR"
        elif total >= expected and verified >= expected:
            status = "LOCAL_REMOVED" if counts["removed"] else "DRIVE_VERIFIED"
        elif verified:
            status = "UPLOADING"
        else:
            status = "LOCAL"
        db.execute(
            "UPDATE days SET status=?, verified_files=?, error=?, verified_at="
            "CASE WHEN ?='DRIVE_VERIFIED' THEN COALESCE(verified_at, ?) ELSE verified_at END "
            "WHERE day=?",
            (
                status,
                verified,
                error,
                status,
                datetime.now().astimezone().isoformat(),
                day,
            ),
        )
