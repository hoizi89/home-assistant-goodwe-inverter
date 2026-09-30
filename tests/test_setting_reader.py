"""Tests of the deferred and throttled reading of the inverter settings."""

from __future__ import annotations

import asyncio
from itertools import pairwise
import time

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.goodwe.const import DOMAIN

from .conftest import SERIAL, FakeInverter

DELAY = 0.5
PAUSE = 0.1

# (platform, unique_id) of all entities the fake inverter supports
EXPECTED_ENTITIES = {
    ("button", f"goodwe-synchronize_clock-{SERIAL}"),
    ("button", f"goodwe-start_inverter-{SERIAL}"),
    ("button", f"goodwe-stop_inverter-{SERIAL}"),
    ("number", f"goodwe-grid_export_limit-{SERIAL}"),
    ("number", f"goodwe-battery_discharge_depth-{SERIAL}"),
    ("number", f"goodwe-soc_upper_limit-{SERIAL}"),
    ("number", f"goodwe-battery_discharge_depth_offline-{SERIAL}"),
    ("number", f"goodwe-eco_mode_power-{SERIAL}"),
    ("number", f"goodwe-eco_mode_soc-{SERIAL}"),
    ("number", f"goodwe-fast_charging_power-{SERIAL}"),
    ("number", f"goodwe-fast_charging_soc-{SERIAL}"),
    ("number", f"goodwe-battery_soc_protection-{SERIAL}"),
    ("select", f"goodwe-operation_mode-{SERIAL}"),
    ("switch", f"load_control-{SERIAL}"),
    ("switch", f"grid_export_limit_switch-{SERIAL}"),
    ("switch", f"backup_supply_switch-{SERIAL}"),
}
UNSUPPORTED_ENTITIES = {
    ("number", f"goodwe-ems_power_limit-{SERIAL}"),
    ("select", f"goodwe-ems_mode-{SERIAL}"),
    ("switch", f"fast_charging_switch-{SERIAL}"),
    ("switch", f"dod_holding_switch-{SERIAL}"),
}
# Setting reads in the expected order (button, number, select, switch)
EXPECTED_READS = [
    "time",
    "start",
    "stop",
    "grid_export_limit",
    "battery_discharge_depth",
    "soc_upper_limit",
    "battery_discharge_depth_offline",
    "eco_mode_1",
    "eco_mode_1",
    "fast_charging_power",
    "fast_charging_soc",
    "ems_power_limit",
    "battery_soc_protection",
    "work_mode",
    "eco_mode_1",
    "ems_mode",
    "load_control_switch",
    "grid_export",
    "fast_charging",
    "backup_supply",
    "dod_holding",
]


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="GoodWe",
        unique_id=SERIAL,
        version=2,
        data={"host": "192.0.2.1", "protocol": "UDP", "model_family": "ET"},
    )


def _entities(hass: HomeAssistant) -> set[tuple[str, str]]:
    return {
        (entity.domain, entity.unique_id)
        for entity in er.async_get(hass).entities.values()
        if entity.platform == DOMAIN and hass.states.get(entity.entity_id)
    }


async def _setup(hass: HomeAssistant) -> tuple[MockConfigEntry, float]:
    entry = _entry()
    entry.add_to_hass(hass)
    start = time.monotonic()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, start


async def _wait_for(condition, timeout: float = 10) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "Timeout waiting for condition"
        await asyncio.sleep(0.02)


async def test_setup_returns_without_reading_settings(
    hass: HomeAssistant, fake_inverter: FakeInverter, fast_timing
) -> None:
    """Platform setup does not read any setting, services are registered."""
    entry, start = await _setup(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert time.monotonic() - start < DELAY
    assert fake_inverter.setting_requests() == []
    assert _entities(hass) == set()
    assert hass.services.has_service(DOMAIN, "set_parameter")
    assert hass.services.has_service(DOMAIN, "get_parameter")

    # set_parameter works right away (e.g. automation at Home Assistant start).
    # The fake inverter has no sensors, so register the device the sensor
    # entities would otherwise register.
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, SERIAL)}
    )
    await hass.services.async_call(
        DOMAIN,
        "set_parameter",
        {"device_id": device.id, "parameter": "grid_export_limit", "value": 5000},
        blocking=True,
    )
    assert fake_inverter.writes == [("grid_export_limit", 5000)]
    assert fake_inverter.setting_requests() == []

    await hass.config_entries.async_unload(entry.entry_id)


async def test_settings_read_one_by_one_after_delay(
    hass: HomeAssistant, fake_inverter: FakeInverter, fast_timing
) -> None:
    """Settings are read after the delay, one by one, with pauses."""
    entry, start = await _setup(hass)

    await _wait_for(
        lambda: len(fake_inverter.setting_requests()) == len(EXPECTED_READS)
    )
    await hass.async_block_till_done()

    reads = fake_inverter.setting_requests()
    assert [name for name, _ in reads] == EXPECTED_READS
    assert reads[0][1] - start >= DELAY
    gaps = [b[1] - a[1] for a, b in pairwise(reads)]
    assert min(gaps) >= PAUSE * 0.9
    assert fake_inverter.max_active == 1

    # Supported settings have entities, unsupported ones do not
    assert _entities(hass) == EXPECTED_ENTITIES
    assert not _entities(hass) & UNSUPPORTED_ENTITIES
    state = hass.states.get(
        er.async_get(hass).async_get_entity_id(
            "number", DOMAIN, f"goodwe-battery_discharge_depth-{SERIAL}"
        )
    )
    assert float(state.state) == 50

    await hass.config_entries.async_unload(entry.entry_id)


async def test_reads_do_not_overlap_with_other_requests(
    hass: HomeAssistant, fake_inverter: FakeInverter, fast_timing
) -> None:
    """Coordinator refreshes and setting reads never overlap."""
    fake_inverter.request_duration = 0.15
    entry, _ = await _setup(hass)
    coordinator = entry.runtime_data.coordinator

    await _wait_for(lambda: len(fake_inverter.setting_requests()) > 0)
    for _ in range(5):
        await coordinator.async_refresh()
    await _wait_for(
        lambda: len(fake_inverter.setting_requests()) == len(EXPECTED_READS)
    )

    assert fake_inverter.max_active == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_read_does_not_stop_the_chain(
    hass: HomeAssistant, fake_inverter: FakeInverter, fast_timing
) -> None:
    """A read without response is retried later, the others continue."""
    fake_inverter.failures = {"soc_upper_limit": 1, "grid_export": 5}
    entry, _ = await _setup(hass)

    # All reads + 1 successful retry of soc_upper_limit + 2 failed retries of grid_export
    await _wait_for(
        lambda: len(fake_inverter.setting_requests()) == len(EXPECTED_READS) + 3
    )
    await asyncio.sleep(0.5)
    await hass.async_block_till_done()

    reads = [name for name, _ in fake_inverter.setting_requests()]
    assert reads[: len(EXPECTED_READS)] == EXPECTED_READS
    assert reads[len(EXPECTED_READS) :] == [
        "soc_upper_limit",
        "grid_export",
        "grid_export",
    ]
    assert _entities(hass) == EXPECTED_ENTITIES - {
        ("switch", f"grid_export_limit_switch-{SERIAL}")
    }

    await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_cancels_reads(
    hass: HomeAssistant, fake_inverter: FakeInverter, fast_timing
) -> None:
    """Unloading the entry before the delay cancels the setting reads."""
    entry, _ = await _setup(hass)
    task = entry.runtime_data.setting_reader._task
    assert task is not None and not task.done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await asyncio.sleep(DELAY + 0.3)

    assert task.cancelled()
    assert fake_inverter.setting_requests() == []
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_unload_while_reading(
    hass: HomeAssistant, fake_inverter: FakeInverter, fast_timing
) -> None:
    """Unloading the entry while reading stops further reads."""
    entry, _ = await _setup(hass)
    await _wait_for(lambda: len(fake_inverter.setting_requests()) >= 3)

    assert await hass.config_entries.async_unload(entry.entry_id)
    count = len(fake_inverter.setting_requests())
    await asyncio.sleep(1)

    assert len(fake_inverter.setting_requests()) == count < len(EXPECTED_READS)


async def test_reads_wait_for_home_assistant_started(
    hass: HomeAssistant, fake_inverter: FakeInverter, fast_timing
) -> None:
    """During Home Assistant startup, the delay starts once it has started."""
    hass.set_state(CoreState.starting)
    entry, _ = await _setup(hass)

    await asyncio.sleep(DELAY + 0.3)
    assert fake_inverter.setting_requests() == []
    assert entry.runtime_data.setting_reader._task is None

    hass.set_state(CoreState.running)
    started = time.monotonic()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await _wait_for(
        lambda: len(fake_inverter.setting_requests()) == len(EXPECTED_READS)
    )

    assert fake_inverter.setting_requests()[0][1] - started >= DELAY
    await hass.config_entries.async_unload(entry.entry_id)
