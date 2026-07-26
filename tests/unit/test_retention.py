import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.capture.retention import prune_expired


TZ = ZoneInfo("America/Los_Angeles")


def test_prune_only_deletes_expired_finalized_files(tmp_path: Path) -> None:
    now = datetime(2026, 7, 25, 12, tzinfo=TZ)
    old = tmp_path / "2026-06-01" / "water" / "water-2026-06-01_09-00-00.mkv"
    active = tmp_path / "2026-06-01" / "water" / "water-2026-06-01_10-00-00.mkv"
    recent = tmp_path / "2026-07-25" / "water" / "water-2026-07-25_09-00-00.mkv"
    for path in (old, active, recent):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
    expired_timestamp = (now - timedelta(days=40)).timestamp()
    os.utime(old, (expired_timestamp, expired_timestamp))
    os.utime(active, (expired_timestamp, expired_timestamp))

    removed = prune_expired(tmp_path, 30, {active}, now)

    assert removed == [old]
    assert not old.exists()
    assert active.exists()
    assert recent.exists()
