"""Fixtures for the Goodwe integration tests.

Run from the repository root, e.g.:

uv run --python 3.13 --with homeassistant==2026.2.3 --with goodwe==0.4.10 \
    --with pytest-homeassistant-custom-component pytest tests
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import patch

from goodwe import OperationMode, RequestFailedException
from goodwe.inverter import EMSMode
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

if sys.platform == "win32":
    # The asyncio event loop needs (TCP) sockets on Windows
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None

SERIAL = "9010KETU000W0000"

# Settings the fake inverter supports and their values
SUPPORTED_SETTINGS: dict[str, Any] = {
    "grid_export_limit": 10000,
    "battery_discharge_depth": 50,
    "soc_upper_limit": 100,
    "battery_discharge_depth_offline": 20,
    "fast_charging_power": 50,
    "fast_charging_soc": 90,
    "battery_soc_protection": 1,
    "work_mode": 0,
    "load_control_switch": 1,
    "grid_export": 1,
    "backup_supply": 0,
    "time": 0,
    "start": 0,
    "stop": 0,
}
# ems_power_limit, ems_mode, fast_charging and dod_holding are not supported


class FakeEcoMode:
    """Fake eco mode setting."""

    power = -50
    soc = 80

    def get_power(self) -> int:
        """Return eco mode power."""
        return self.power


class FakeSetting:
    """Fake setting definition."""

    def __init__(self, id_: str, unit: str) -> None:
        """Initialize fake setting."""
        self.id_ = id_
        self.unit = unit


class FakeInverter:
    """Fake inverter recording all requests.

    Every request goes through _read_from_socket(), like in the goodwe library,
    so the request serialization of the integration applies to it.
    """

    def __init__(self) -> None:
        """Initialize fake inverter."""
        self.serial_number = SERIAL
        self.model_name = "GW10KN-ET"
        self.firmware = "1"
        self.arm_firmware = "2"
        self.rated_power = 10000
        self.requests: list[tuple[str, float]] = []
        self.writes: list[tuple[str, Any]] = []
        self.failures: dict[str, int] = {}
        self.active = 0
        self.max_active = 0
        self.request_duration = 0.02

    def set_keep_alive(self, keep_alive: bool) -> None:
        """Set keep alive."""

    def sensors(self) -> tuple:
        """Return no sensors."""
        return ()

    def settings(self) -> tuple:
        """Return settings definitions."""
        return (FakeSetting("grid_export_limit", "W"),)

    async def _read_from_socket(self, command: str) -> Any:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.request_duration)
        finally:
            self.active -= 1
        self.requests.append((command, time.monotonic()))
        if self.failures.get(command):
            self.failures[command] -= 1
            raise RequestFailedException("No response", 1)
        if command == "runtime_data":
            return {}
        if command == "eco_mode_1":
            return FakeEcoMode()
        if command.startswith("write:"):
            return None
        if command not in SUPPORTED_SETTINGS:
            raise ValueError(f'Unknown setting "{command}"')
        return SUPPORTED_SETTINGS[command]

    async def read_runtime_data(self) -> dict[str, Any]:
        """Read runtime data."""
        return await self._read_from_socket("runtime_data")

    async def read_setting(self, setting_id: str) -> Any:
        """Read setting."""
        return await self._read_from_socket(setting_id)

    async def write_setting(self, setting_id: str, value: Any) -> None:
        """Write setting."""
        self.writes.append((setting_id, value))
        await self._read_from_socket(f"write:{setting_id}")

    async def get_grid_export_limit(self) -> int:
        """Read grid export limit."""
        return await self.read_setting("grid_export_limit")

    async def get_ongrid_battery_dod(self) -> int:
        """Read on-grid battery DoD."""
        return 100 - await self.read_setting("battery_discharge_depth")

    async def get_operation_modes(self, include_emulated: bool) -> tuple:
        """Return supported operation modes (no request)."""
        return (OperationMode.GENERAL, OperationMode.ECO)

    async def get_operation_mode(self) -> OperationMode:
        """Read operation mode."""
        return OperationMode(await self.read_setting("work_mode"))

    async def get_ems_mode(self) -> EMSMode:
        """Read EMS mode."""
        return EMSMode(await self.read_setting("ems_mode"))

    def setting_requests(self) -> list[tuple[str, float]]:
        """Return the setting read requests."""
        return [
            r
            for r in self.requests
            if r[0] != "runtime_data" and not r[0].startswith("write:")
        ]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    return


@pytest.fixture
def fake_inverter() -> FakeInverter:
    """Return fake inverter used by the integration."""
    inverter = FakeInverter()

    async def _connect(**kwargs) -> FakeInverter:
        return inverter

    with patch("custom_components.goodwe.connect", _connect):
        yield inverter


@pytest.fixture
def fast_timing():
    """Shorten the setting read delays."""
    with (
        patch("custom_components.goodwe.SETTINGS_READ_DELAY", 0.5),
        patch("custom_components.goodwe.SETTINGS_READ_PAUSE", 0.1),
        patch("custom_components.goodwe.SETTINGS_READ_RETRY_DELAY", 0.3),
        patch("custom_components.goodwe.SETTINGS_READ_RETRIES", 2),
    ):
        yield
