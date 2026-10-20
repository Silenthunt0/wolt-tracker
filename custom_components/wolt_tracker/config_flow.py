"""UI setup: paste a refresh token, we validate it with one real refresh."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import WoltApi, WoltApiError, WoltAuthError, jwt_claims
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACTIVE_INTERVAL,
    CONF_CLOSE_DISTANCE,
    CONF_CLOSE_ETA,
    CONF_COURIER_INTERVAL,
    CONF_EXPIRES_AT,
    CONF_IDLE_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_CLOSE_DISTANCE,
    DEFAULT_CLOSE_ETA,
    DEFAULT_COURIER_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REFRESH_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


def _clean_token(raw: str) -> str:
    """Accept the cookie value as pasted: strip quotes and URL-encoded quotes."""
    return raw.strip().replace("%22", "").strip('"').strip()


async def _validate(hass: HomeAssistant, refresh_token: str) -> dict[str, Any]:
    """Refresh once. Returns the entry data (rotated tokens + Wolt user id)."""
    api = WoltApi(async_get_clientsession(hass), refresh_token=refresh_token)
    await api.async_refresh()
    claims = jwt_claims(api.access_token or "")
    return {
        CONF_REFRESH_TOKEN: api.refresh_token,
        CONF_ACCESS_TOKEN: api.access_token,
        CONF_EXPIRES_AT: api.expires_at,
        CONF_USER_ID: (claims.get("user") or {}).get("id"),
    }


class WoltConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = _clean_token(user_input[CONF_REFRESH_TOKEN])
            try:
                data = await _validate(self.hass, token)
            except WoltAuthError:
                errors["base"] = "invalid_auth"
            except WoltApiError:
                errors["base"] = "cannot_connect"
            else:
                if data[CONF_USER_ID]:
                    await self.async_set_unique_id(data[CONF_USER_ID])
                    self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Wolt", data=data)
        return self.async_show_form(step_id="user", data_schema=TOKEN_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = _clean_token(user_input[CONF_REFRESH_TOKEN])
            try:
                data = await _validate(self.hass, token)
            except WoltAuthError:
                errors["base"] = "invalid_auth"
            except WoltApiError:
                errors["base"] = "cannot_connect"
            else:
                entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(entry, data={**entry.data, **data})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=TOKEN_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WoltOptionsFlow()


class WoltOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        o = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CLOSE_DISTANCE, default=o.get(CONF_CLOSE_DISTANCE, DEFAULT_CLOSE_DISTANCE)
                ): NumberSelector(
                    NumberSelectorConfig(min=20, max=5000, step=10, unit_of_measurement="m", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_CLOSE_ETA, default=o.get(CONF_CLOSE_ETA, DEFAULT_CLOSE_ETA)
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=30, step=0.5, unit_of_measurement="min", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_IDLE_INTERVAL, default=o.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)
                ): NumberSelector(
                    NumberSelectorConfig(min=30, max=900, step=10, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_ACTIVE_INTERVAL, default=o.get(CONF_ACTIVE_INTERVAL, DEFAULT_ACTIVE_INTERVAL)
                ): NumberSelector(
                    NumberSelectorConfig(min=10, max=300, step=5, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_COURIER_INTERVAL, default=o.get(CONF_COURIER_INTERVAL, DEFAULT_COURIER_INTERVAL)
                ): NumberSelector(
                    NumberSelectorConfig(min=5, max=120, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
