from pathlib import Path


def test_sensor_service_uses_writable_working_directory() -> None:
    unit = (
        Path(__file__).parents[2] / "systemd" / "kratky-sensors.service"
    ).read_text(encoding="utf-8")

    assert "WorkingDirectory=/run/kratky" in unit
    assert "Environment=PYTHONPATH=/home/kratky/kratky-monitor" in unit
