from pathlib import Path

from app.capture.state import PauseStore, atomic_write_json, read_json


def test_atomic_json_and_pause_persistence(tmp_path: Path) -> None:
    path = tmp_path / "state" / "capture.json"
    atomic_write_json(path, {"ok": True})
    assert read_json(path, {}) == {"ok": True}
    store = PauseStore(path)
    store.save({"environment", "water"})
    assert store.load() == {"environment", "water"}
    assert not list(path.parent.glob("*.tmp"))
