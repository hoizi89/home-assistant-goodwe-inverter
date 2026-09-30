"""Deferred and throttled reading of the inverter settings for Goodwe."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any

from goodwe import RequestFailedException
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.start import async_at_started

from .const import PLATFORMS

_LOGGER = logging.getLogger(__name__)


class SettingReadFailed(Exception):
    """Indicates the inverter did not respond to a setting read request.

    It is intentionally neither an InverterError nor a ValueError, so the platforms
    do not mistake it for an unsupported setting and the read is retried later.
    """


class GoodweSettingReader:
    """Read the initial values of the inverter settings one by one.

    Reading all settings (number, select, switch and button entities) at once
    during the platform setup floods weak (WiFi) dongles right when Home Assistant
    starts. Instead, the platforms register a job per setting (or group of settings)
    here, the jobs are started with a delay after Home Assistant has started and
    each read request is separated by a pause. A job adds its entity only once its
    read succeeded, so unsupported settings still do not create any entity.
    Jobs failing due to communication errors are retried later.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        delay: float,
        pause: float,
        retry_delay: float,
        retries: int,
    ) -> None:
        """Initialize the setting reader."""
        self._hass = hass
        self._entry = entry
        self._delay = delay
        self._pause = pause
        self._retry_delay = retry_delay
        self._retries = retries
        self._jobs: list[tuple[int, str, Callable[[], Awaitable[None]]]] = []
        self._reads = 0
        self._unsub_started: CALLBACK_TYPE | None = None
        self._task: asyncio.Task | None = None

    @callback
    def add(
        self, platform: Platform, name: str, job: Callable[[], Awaitable[None]]
    ) -> None:
        """Register a job reading setting(s) and adding the entity (if supported).

        The job has to use async_read() for all its requests to the inverter.
        """
        self._jobs.append((PLATFORMS.index(platform), name, job))

    async def async_read(self, getter: Callable[..., Awaitable[Any]], *args) -> Any:
        """Read a setting from the inverter, keeping a pause after previous read."""
        if self._reads:
            await asyncio.sleep(self._pause)
        self._reads += 1
        try:
            return await getter(*args)
        except RequestFailedException as err:
            raise SettingReadFailed(err.message) from err

    @callback
    def async_start(self) -> None:
        """Start reading the settings once Home Assistant has started."""
        self._unsub_started = async_at_started(self._hass, self._async_started)

    @callback
    def async_stop(self) -> None:
        """Stop reading the settings (e.g. when the config entry is unloaded)."""
        if self._unsub_started:
            self._unsub_started()
            self._unsub_started = None
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    @callback
    def _async_started(self, hass: HomeAssistant) -> None:
        """Create the background task reading the settings."""
        self._unsub_started = None
        self._task = self._entry.async_create_background_task(
            hass,
            self._async_read_settings(),
            f"{self._entry.title} settings reader",
        )

    async def _async_read_settings(self) -> None:
        """Run all the registered jobs one after another."""
        # Platforms are set up concurrently, run their jobs in a stable order
        jobs = [(name, job) for _, name, job in sorted(self._jobs, key=lambda j: j[0])]
        self._jobs.clear()
        _LOGGER.info(
            "Reading %d inverter settings in %d seconds", len(jobs), self._delay
        )
        await asyncio.sleep(self._delay)

        for attempt in range(self._retries + 1):
            if attempt:
                _LOGGER.info(
                    "Retrying to read %d inverter settings in %d seconds",
                    len(jobs),
                    self._retry_delay,
                )
                await asyncio.sleep(self._retry_delay)
            failed = []
            for name, job in jobs:
                try:
                    await job()
                except SettingReadFailed as err:
                    _LOGGER.debug("Failed to read inverter setting %s: %s", name, err)
                    failed.append((name, job))
                except Exception:
                    _LOGGER.exception(
                        "Unexpected error reading inverter setting %s", name
                    )
            jobs = failed
            if not jobs:
                _LOGGER.info("Inverter settings read (%d requests)", self._reads)
                return

        _LOGGER.warning(
            "Could not read inverter settings %s, their entities are not available"
            " until the integration is reloaded",
            ", ".join(name for name, _ in jobs),
        )
