from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.sensors.service import SensorHardware


def test_i2c_initialization_error_is_reported_for_environment_sensors() -> None:
    hardware = SensorHardware.__new__(SensorHardware)
    hardware.config = SimpleNamespace(
        sensors=SimpleNamespace(interval_seconds=1.0)
    )
    hardware.tsl = None
    hardware.scd4x = None
    hardware.water = None
    hardware.init_errors = {
        "i2c": "I2C unavailable: permission denied",
        "water": "water probe unavailable",
    }
    hardware.last_environment = {
        "air_temperature_c": None,
        "air_temperature_f": None,
        "relative_humidity_percent": None,
        "co2_ppm": None,
        "light_lux": None,
    }
    hardware.last_water = {
        "temperature_c": None,
        "ph": None,
        "electrical_conductivity_us_cm": None,
        "moisture_percent": None,
        "nitrogen_mg_kg": None,
        "phosphorus_mg_kg": None,
        "potassium_mg_kg": None,
    }
    hardware.environment_last_success = None
    hardware.water_last_success = None

    snapshot = hardware.read(
        datetime(2026, 7, 25, 22, tzinfo=ZoneInfo("America/Los_Angeles"))
    )

    assert "permission denied" in (snapshot.environment.error or "")
