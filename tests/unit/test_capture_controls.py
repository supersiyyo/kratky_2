from datetime import datetime, timedelta
from pathlib import Path

from app.capture.service import CaptureManager
from app.common.config import config_from_mapping
from tests.unit.test_config import valid_mapping


def manager_for(tmp_path: Path) -> CaptureManager:
    return CaptureManager(config_from_mapping(valid_mapping(tmp_path)))


def test_controls_start_locked_and_relock_after_action(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)

    blocked = manager.command("pause", "water")
    assert blocked == {
        "ok": False,
        "code": "controls_locked",
        "error": "capture controls are locked",
    }

    unlocked = manager.command("unlock")
    assert unlocked["control_lock"]["locked"] is False

    paused = manager.command("pause", "all")
    assert paused == {"ok": True, "action": "pause", "cameras": ["water"]}
    state = manager.snapshot()["control_lock"]
    assert state["locked"] is True
    assert [event["kind"] for event in state["history"][-2:]] == [
        "control_unlock",
        "control_lock",
    ]


def test_controls_automatically_lock_after_timeout(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    manager.command("unlock")
    manager.control_unlocked_until = (
        datetime.now(manager.timezone) - timedelta(seconds=1)
    )

    state = manager.snapshot()["control_lock"]

    assert state["locked"] is True
    assert state["unlocked_until"] is None
    assert state["history"][-1]["detail"] == (
        "controls locked after 60-second timeout"
    )


def test_disabled_camera_cannot_be_targeted(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    manager.command("unlock")

    try:
        manager.command("resume", "environment")
    except ValueError as exc:
        assert str(exc) == "camera is not enabled: environment"
    else:
        raise AssertionError("disabled camera target was accepted")
