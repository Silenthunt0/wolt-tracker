"""Wolt order tracker: polls Wolt's (unofficial) consumer API and derives a
"courier is close" signal from the courier's live GPS position.

Set up from the UI (Settings → Devices & services → Add integration → Wolt).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WoltApi
from .const import CONF_ACCESS_TOKEN, CONF_EXPIRES_AT, CONF_REFRESH_TOKEN
from .coordinator import WoltCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type WoltConfigEntry = ConfigEntry[WoltCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WoltConfigEntry) -> bool:
    async def _persist_tokens(refresh_token: str, access_token: str, expires_at: float) -> None:
        # Wolt rotates the refresh token on every use; losing the newest one
        # means a new login, so write it to the entry straight away.
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_REFRESH_TOKEN: refresh_token,
                CONF_ACCESS_TOKEN: access_token,
                CONF_EXPIRES_AT: expires_at,
            },
        )

    api = WoltApi(
        async_get_clientsession(hass),
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        expires_at=float(entry.data.get(CONF_EXPIRES_AT) or 0),
        on_tokens=_persist_tokens,
    )
    coordinator = WoltCoordinator(hass, entry, api)
    # Raises ConfigEntryAuthFailed → reauth flow, or ConfigEntryNotReady → retry.
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: WoltConfigEntry) -> None:
    """Options changed (token rotation also lands here, so only react to options)."""
    coordinator = entry.runtime_data
    coordinator.apply_options(entry.options)


async def async_unload_entry(hass: HomeAssistant, entry: WoltConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
