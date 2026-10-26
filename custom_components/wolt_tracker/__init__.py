"""Wolt order tracker: polls Wolt's (unofficial) consumer API, derives a
"courier is close" signal from the courier's live GPS position, and watches
the support chat for messages from Wolt's agents.

Set up from the UI (Settings → Devices & services → Add integration → Wolt).
Services `wolt_tracker.reply` and `wolt_tracker.mark_read` act on the open
support conversation; nothing in here ever answers support on its own.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import WoltApi, WoltApiError
from .const import (
    ATTR_CONVERSATION_ID,
    ATTR_MESSAGE,
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    SERVICE_MARK_READ,
    SERVICE_REPLY,
    SUPPORT_STATUS_OPENED,
)
from .coordinator import WoltCoordinator, WoltState

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

REPLY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_CONVERSATION_ID): cv.string,
    }
)
MARK_READ_SCHEMA = vol.Schema({vol.Optional(ATTR_CONVERSATION_ID): cv.string})

type WoltConfigEntry = ConfigEntry[WoltCoordinator]


def _coordinator(hass: HomeAssistant) -> WoltCoordinator:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data
    raise ServiceValidationError("The Wolt order tracker is not set up or not loaded")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the support-chat services (they act on whichever entry is loaded)."""

    async def _reply(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        state = coordinator.data or WoltState()
        text = call.data[ATTR_MESSAGE].strip()
        if not text:
            raise ServiceValidationError("The reply is empty")
        conversation_id = call.data.get(ATTR_CONVERSATION_ID)
        if not conversation_id:
            if not state.support_conversation_id or state.support_status != SUPPORT_STATUS_OPENED:
                raise ServiceValidationError("There is no open Wolt support conversation to reply to")
            conversation_id = state.support_conversation_id
        try:
            await coordinator.api.async_support_send(conversation_id, text)
        except WoltApiError as err:
            raise HomeAssistantError(f"Wolt did not accept the reply: {err}") from err
        _LOGGER.info("Sent a reply to Wolt support (conversation %s)", conversation_id)
        await coordinator.async_request_refresh()

    async def _mark_read(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        state = coordinator.data or WoltState()
        conversation_id = call.data.get(ATTR_CONVERSATION_ID) or state.support_conversation_id
        if not conversation_id or conversation_id != state.support_conversation_id:
            return
        if not state.support_unread_ids:
            return
        try:
            await coordinator.api.async_support_mark_read(conversation_id, list(state.support_unread_ids))
        except WoltApiError as err:
            raise HomeAssistantError(f"Could not mark the Wolt messages as read: {err}") from err
        await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_REPLY, _reply, schema=REPLY_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_MARK_READ, _mark_read, schema=MARK_READ_SCHEMA)
    return True


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
