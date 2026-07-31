from pathlib import Path


def test_sensor_service_uses_writable_working_directory() -> None:
    unit = (
        Path(__file__).parents[2] / "systemd" / "kratky-sensors.service"
    ).read_text(encoding="utf-8")

    assert "WorkingDirectory=/run/kratky" in unit
    assert "Environment=PYTHONPATH=/home/kratky/kratky-monitor" in unit
    assert "dev-i2c" not in unit
    assert "After=local-fs.target kratky-capture.service" in unit


def test_installation_verification_retries_dashboard_startup() -> None:
    script = (
        Path(__file__).parents[2] / "scripts" / "verify-installation.sh"
    ).read_text(encoding="utf-8")

    assert 'check_retry "dashboard responds" 15 1' in script
