"""Wolt order tracker: polls Wolt's (unofficial) consumer API, derives a
"courier is close" signal from the courier's live GPS position, and watches
the support chat for messages from Wolt's agents.

Set up from the UI (Settings → Devices & services → Add integration → Wolt).
Services `wolt_tracker.reply` and `wolt_tracker.mark_read` act on the open
support conversation; `wolt_tracker.rate` and `wolt_tracker.dismiss_rating`
act on the delivered order waiting for its rating. Nothing in here ever
answers support or rates an order on its own.
"""

from __future__ import annotations

import logging
from typing import Any

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
    ATTR_DELIVERY_COMMENT,
    ATTR_DELIVERY_RATING,
    ATTR_DELIVERY_TAGS,
    ATTR_FOOD_COMMENT,
    ATTR_FOOD_RATING,
    ATTR_FOOD_TAGS,
    ATTR_MESSAGE,
    ATTR_PURCHASE_ID,
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    RATING_MAX,
    RATING_MIN,
    RATING_SECTIONS,
    SERVICE_DISMISS_RATING,
    SERVICE_MARK_READ,
    SERVICE_RATE,
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

_RATING = vol.All(vol.Coerce(int), vol.Range(min=RATING_MIN, max=RATING_MAX))
_TAGS = vol.All(cv.ensure_list, [cv.string])
RATE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_PURCHASE_ID): cv.string,
        vol.Optional(ATTR_FOOD_RATING): _RATING,
        vol.Optional(ATTR_FOOD_TAGS): _TAGS,
        vol.Optional(ATTR_FOOD_COMMENT): cv.string,
        vol.Optional(ATTR_DELIVERY_RATING): _RATING,
        vol.Optional(ATTR_DELIVERY_TAGS): _TAGS,
        vol.Optional(ATTR_DELIVERY_COMMENT): cv.string,
    }
)
DISMISS_RATING_SCHEMA = vol.Schema({vol.Optional(ATTR_PURCHASE_ID): cv.string})

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

    def _pending_order(call: ServiceCall, coordinator: WoltCoordinator) -> str:
        state = coordinator.data or WoltState()
        purchase_id = call.data.get(ATTR_PURCHASE_ID) or state.rating_purchase_id
        if not purchase_id:
            raise ServiceValidationError("No delivered Wolt order is waiting for a rating")
        return purchase_id

    async def _rate(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        purchase_id = _pending_order(call, coordinator)
        given = {
            "food": (call.data.get(ATTR_FOOD_RATING), call.data.get(ATTR_FOOD_TAGS) or [], call.data.get(ATTR_FOOD_COMMENT)),
            "delivery": (
                call.data.get(ATTR_DELIVERY_RATING),
                call.data.get(ATTR_DELIVERY_TAGS) or [],
                call.data.get(ATTR_DELIVERY_COMMENT),
            ),
        }
        if all(rating is None for rating, _, _ in given.values()):
            raise ServiceValidationError("Give at least one of food_rating or delivery_rating (1 to 5)")
        try:
            template = await coordinator.async_rating_template(purchase_id)
        except WoltApiError as err:
            raise HomeAssistantError(f"Could not load the Wolt rating form: {err}") from err

        review: dict[str, Any] = {}
        for section in RATING_SECTIONS:
            form = template.get(section)
            rating, tags, comment = given[section]
            if form is None:
                if rating is not None:
                    raise ServiceValidationError(f"This order has no {section} rating step")
                continue
            if rating is None:
                review[section] = {"skipped": True, "variant_name": form.get("variant_name"), "attribution": []}
                continue
            allowed = {
                (t["name"] or "").casefold(): t["key"] for t in (form.get("tags") or {}).get(rating, [])
            }
            allowed.update({t["key"].casefold(): t["key"] for t in (form.get("tags") or {}).get(rating, [])})
            keys: list[str] = []
            for tag in tags:
                if not tag.strip():
                    continue
                key = allowed.get(tag.strip().casefold())
                if key is None:
                    names = ", ".join(sorted({t["name"] or t["key"] for t in (form.get("tags") or {}).get(rating, [])}))
                    raise ServiceValidationError(
                        f"'{tag}' is not a Wolt tag for a {section} rating of {rating}"
                        + (f"; choose from: {names}" if names else "; this rating takes no tags")
                    )
                if key not in keys:
                    keys.append(key)
            entry: dict[str, Any] = {
                "rating": rating,
                "variant_name": form.get("variant_name"),
                "attribution": keys,
                "skipped": False,
            }
            if comment and comment.strip():
                entry["comment"] = comment.strip()
            review[section] = entry

        try:
            await coordinator.api.async_review_submit(purchase_id, review)
        except WoltApiError as err:
            raise HomeAssistantError(f"Wolt did not accept the rating: {err}") from err
        _LOGGER.info("Rated Wolt order %s: %s", purchase_id, {k: v.get("rating", "skipped") for k, v in review.items()})
        try:
            await coordinator.api.async_unsubscribe_order(purchase_id)
        except WoltApiError as err:
            _LOGGER.warning("Rating saved but the prompt could not be closed: %s", err)
        coordinator.forget_rating(purchase_id)
        await coordinator.async_request_refresh()

    async def _dismiss_rating(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        purchase_id = _pending_order(call, coordinator)
        try:
            await coordinator.api.async_unsubscribe_order(purchase_id)
        except WoltApiError as err:
            raise HomeAssistantError(f"Could not dismiss the Wolt rating prompt: {err}") from err
        _LOGGER.info("Dismissed the rating prompt for Wolt order %s", purchase_id)
        coordinator.forget_rating(purchase_id)
        await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_REPLY, _reply, schema=REPLY_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_MARK_READ, _mark_read, schema=MARK_READ_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RATE, _rate, schema=RATE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DISMISS_RATING, _dismiss_rating, schema=DISMISS_RATING_SCHEMA)
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
