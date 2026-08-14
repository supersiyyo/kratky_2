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


def test_offload_service_is_low_priority_and_owns_verified_cleanup() -> None:
    unit = (
        Path(__file__).parents[2] / "systemd" / "kratky-offload.service"
    ).read_text(encoding="utf-8")

    assert "Nice=10" in unit
    assert "IOSchedulingClass=idle" in unit
    assert "app.offload.service" in unit
    assert "/var/lib/kratky/recordings" in unit
    assert "ExecCondition=" in unit


def test_capture_service_allows_both_encoders_to_finalize_on_shutdown() -> None:
    unit = (
        Path(__file__).parents[2] / "systemd" / "kratky-capture.service"
    ).read_text(encoding="utf-8")

    assert "TimeoutStopSec=120" in unit
