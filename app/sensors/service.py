from __future__ import annotations

import csv
import logging
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.capture.state import atomic_write_json
from app.common.config import AppConfig, load_config
from app.common.paths import ensure_runtime_directories, sensor_state_path
from app.sensors.state import SensorSection, SensorSnapshot

LOGGER = logging.getLogger("kratky.sensors")


class SensorHardware:
    """Owns optional I²C and Modbus devices; a failed device does not stop the loop."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.tsl: Any = None
        self.scd4x: Any = None
        self.water: Any = None
        self.init_errors: dict[str, str] = {}
        self.last_environment: dict[str, float | int | None] = {
            "air_temperature_c": None,
            "air_temperature_f": None,
            "relative_humidity_percent": None,
            "co2_ppm": None,
            "light_lux": None,
        }
        self.last_water: dict[str, float | int | None] = {
            "temperature_c": None,
            "ph": None,
            "electrical_conductivity_us_cm": None,
            "moisture_percent": None,
            "nitrogen_mg_kg": None,
            "phosphorus_mg_kg": None,
            "potassium_mg_kg": None,
        }
        self.environment_last_success: datetime | None = None
        self.water_last_success: datetime | None = None
        self._initialize()

    def _initialize(self) -> None:
        try:
            import board
            import busio

            bus = busio.I2C(board.SCL, board.SDA)
        except Exception as exc:
            bus = None
            self.init_errors["i2c"] = f"I²C unavailable: {exc}"

        if bus is not None:
            try:
                import adafruit_tsl2561

                self.tsl = adafruit_tsl2561.TSL2561(bus)
            except Exception as exc:
                self.init_errors["light"] = f"TSL2561 unavailable: {exc}"
            try:
                import adafruit_scd4x

                self.scd4x = adafruit_scd4x.SCD4X(bus)
                try:
                    self.scd4x.stop_periodic_measurement()
                    time.sleep(1)
                except Exception:
                    pass
                self.scd4x.start_periodic_measurement()
            except Exception as exc:
                self.init_errors["air"] = f"SCD41 unavailable: {exc}"

        try:
            import minimalmodbus

            self.water = minimalmodbus.Instrument(
                self.config.sensors.modbus_device,
                self.config.sensors.modbus_slave,
            )
        except Exception as exc:
            self.init_errors["water"] = f"Modbus water probe unavailable: {exc}"
        if self.water is not None:
            self.water.serial.baudrate = 9600
            self.water.serial.timeout = 1

    @staticmethod
    def _message(exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"

    def read(self, now: datetime) -> SensorSnapshot:
        environment = self.last_environment.copy()
        water = self.last_water.copy()
        environment_errors: list[str] = []
        water_errors: list[str] = []
        environment_success = False
        water_success = False
        if self.tsl is not None:
            try:
                environment["light_lux"] = float(self.tsl.lux or 0)
                environment_success = True
            except Exception as exc:
                environment_errors.append(self._message(exc))
        else:
            environment_errors.append(
                self.init_errors.get("light")
                or self.init_errors.get("i2c")
                or "TSL2561 missing"
            )
        if self.scd4x is not None:
            try:
                if self.scd4x.data_ready:
                    temperature = float(self.scd4x.temperature)
                    environment.update({
                        "air_temperature_c": temperature,
                        "air_temperature_f": temperature * 9 / 5 + 32,
                        "relative_humidity_percent": float(self.scd4x.relative_humidity),
                        "co2_ppm": int(self.scd4x.CO2),
                    })
                    environment_success = True
            except Exception as exc:
                environment_errors.append(self._message(exc))
        else:
            environment_errors.append(
                self.init_errors.get("air")
                or self.init_errors.get("i2c")
                or "SCD41 missing"
            )
        if self.water is not None:
            try:
                water.update({
                    "ph": self.water.read_register(6, functioncode=3) / 100.0,
                    "moisture_percent": self.water.read_register(18, functioncode=3) / 10.0,
                    "temperature_c": self.water.read_register(
                        19, functioncode=3, signed=True
                    ) / 10.0,
                    "electrical_conductivity_us_cm": self.water.read_register(
                        21, functioncode=3
                    ),
                    "nitrogen_mg_kg": self.water.read_register(30, functioncode=3),
                    "phosphorus_mg_kg": self.water.read_register(31, functioncode=3),
                    "potassium_mg_kg": self.water.read_register(32, functioncode=3),
                })
                water_success = True
            except Exception as exc:
                water_errors.append(self._message(exc))
        else:
            water_errors.append(self.init_errors.get("water", "water probe missing"))
        if environment_success:
            self.last_environment = environment
            self.environment_last_success = now
        if water_success:
            self.last_water = water
            self.water_last_success = now
        stale_after = max(15.0, self.config.sensors.interval_seconds * 3)

        def section_status(success: bool, errors: list[str], last: datetime | None) -> str:
            if success and not errors:
                return "OK"
            if last is None:
                return "STARTING" if not errors else "DEGRADED"
            if (now - last).total_seconds() > stale_after:
                return "STALE"
            return "DEGRADED" if errors else "OK"

        environment_status = section_status(
            environment_success, environment_errors, self.environment_last_success
        )
        water_status = section_status(water_success, water_errors, self.water_last_success)
        overall = "OK" if environment_status == water_status == "OK" else "DEGRADED"
        if "STALE" in {environment_status, water_status}:
            overall = "STALE"
        return SensorSnapshot(
            now.isoformat(),
            overall,
            SensorSection(
                environment,
                environment_status,
                "; ".join(environment_errors) or None,
                self.environment_last_success.isoformat()
                if self.environment_last_success else None,
            ),
            SensorSection(
                water,
                water_status,
                "; ".join(water_errors) or None,
                self.water_last_success.isoformat() if self.water_last_success else None,
            ),
        )


def append_history(path: Path, snapshot: SensorSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "timestamp": snapshot.updated_at,
        **{f"environment_{key}": value for key, value in snapshot.environment.values.items()},
        **{f"water_{key}": value for key, value in snapshot.water.values.items()},
    }
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(values))
        if not exists:
            writer.writeheader()
        writer.writerow(values)


class SensorService:
    def __init__(self, config: AppConfig, hardware: SensorHardware | None = None):
        self.config = config
        self.timezone = ZoneInfo(config.deployment.timezone)
        self.hardware = hardware or SensorHardware(config)
        self.running = True
        self.last_history = 0.0

    def tick(self) -> SensorSnapshot:
        now = datetime.now(self.timezone)
        snapshot = self.hardware.read(now)
        atomic_write_json(sensor_state_path(self.config), snapshot.to_dict())
        if time.monotonic() - self.last_history >= self.config.sensors.history_interval_seconds:
            history_path = self.config.runtime.sensor_dir / f"sensors-{now:%Y-%m}.csv"
            append_history(history_path, snapshot)
            self.last_history = time.monotonic()
        return snapshot

    def run(self) -> None:
        while self.running:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                # Keep the service alive and let systemd reserve restarts for process-level faults.
                LOGGER.exception("sensor iteration failed")
            delay = max(0.1, self.config.sensors.interval_seconds - (time.monotonic() - started))
            time.sleep(delay)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    ensure_runtime_directories(config)
    service = SensorService(config)

    def stop(_signum: int, _frame: object) -> None:
        service.running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if not config.sensors.enabled:
        atomic_write_json(
            sensor_state_path(config),
            {
                "updated_at": datetime.now(ZoneInfo(config.deployment.timezone)).isoformat(),
                "status": "DISABLED",
                "environment": {
                    "values": {}, "status": "DISABLED", "error": None,
                    "last_success_at": None,
                },
                "water": {
                    "values": {}, "status": "DISABLED", "error": None,
                    "last_success_at": None,
                },
            },
        )
        return
    service.run()


if __name__ == "__main__":
    main()
