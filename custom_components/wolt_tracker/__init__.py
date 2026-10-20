"""Wolt order tracker: polls Wolt's (unofficial) consumer API and derives a
"courier is close" signal from the courier's live GPS position.

YAML only:

wolt_tracker:
  refresh_token: !secret wolt_refresh_token
  close_distance_m: 400
  close_eta_minutes: 3
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    AUTH_FAILED_INTERVAL,
    AUTH_URL,
    CONF_ACTIVE_INTERVAL,
    CONF_CLOSE_DISTANCE,
    CONF_CLOSE_ETA,
    CONF_COURIER_INTERVAL,
    CONF_IDLE_INTERVAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_CLOSE_DISTANCE,
    DEFAULT_CLOSE_ETA,
    DEFAULT_COURIER_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    NOTIFICATION_AUTH,
    ORDER_STATUS_IN_PROGRESS,
    ORDERS_URL,
    SERVICE_SET_REFRESH_TOKEN,
    STATUS_READY,
    STORAGE_KEY,
    STORAGE_VERSION,
    TERMINAL_STATUSES,
    TOKEN_REFRESH_MARGIN,
    TRACKING_URL,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_REFRESH_TOKEN): cv.string,
                vol.Optional(CONF_CLOSE_DISTANCE, default=DEFAULT_CLOSE_DISTANCE): vol.All(
                    vol.Coerce(float), vol.Range(min=20)
                ),
                vol.Optional(CONF_CLOSE_ETA, default=DEFAULT_CLOSE_ETA): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional(CONF_IDLE_INTERVAL, default=DEFAULT_IDLE_INTERVAL): cv.positive_int,
                vol.Optional(CONF_ACTIVE_INTERVAL, default=DEFAULT_ACTIVE_INTERVAL): cv.positive_int,
                vol.Optional(CONF_COURIER_INTERVAL, default=DEFAULT_COURIER_INTERVAL): cv.positive_int,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

API_HEADERS = {
    "platform": "Web",
    "client-version": "1.16.150",
    "clientversionnumber": "1.16.150",
    "app-language": "en",
    "origin": "https://wolt.com",
    "referer": "https://wolt.com/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
}


class WoltAuthError(Exception):
    """The refresh token was rejected; a new login is needed."""


class WoltApiError(Exception):
    """Transient or unexpected API failure."""


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _mongo_date(value: Any) -> datetime | None:
    """Wolt serialises datetimes as {"$date": <epoch ms>}."""
    if isinstance(value, dict) and isinstance(value.get("$date"), (int, float)):
        return datetime.fromtimestamp(value["$date"] / 1000, tz=timezone.utc)
    return None


class WoltApi:
    """Thin client around the three endpoints we need, with token rotation.

    Wolt rotates the refresh token on every refresh and keeps only the newest
    one alive, so every rotation is persisted immediately and this client must
    be the sole user of its token chain (never share it with a browser).
    """

    def __init__(self, hass: HomeAssistant, seed_refresh_token: str) -> None:
        self._hass = hass
        self._session = async_get_clientsession(hass)
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        self._seed = seed_refresh_token
        self._refresh_token: str | None = None
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        # Use the stored (rotated) token unless the YAML seed changed since we
        # last seeded, which means the user re-logged-in and pasted a new one.
        if data.get("seed") == self._seed and data.get("refresh_token"):
            self._refresh_token = data["refresh_token"]
            self._access_token = data.get("access_token")
            self._expires_at = float(data.get("expires_at") or 0)
        else:
            self._refresh_token = self._seed
            self._access_token = None
            self._expires_at = 0.0

    async def async_set_refresh_token(self, token: str) -> None:
        async with self._lock:
            self._refresh_token = token
            self._access_token = None
            self._expires_at = 0.0
            await self._save()

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "seed": self._seed,
                "refresh_token": self._refresh_token,
                "access_token": self._access_token,
                "expires_at": self._expires_at,
            }
        )

    async def _refresh(self) -> None:
        if not self._refresh_token:
            raise WoltAuthError("no refresh token configured")
        data = {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        try:
            async with self._session.post(
                AUTH_URL,
                data=data,
                headers={"origin": "https://wolt.com"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (400, 401, 403):
                    body = await resp.text()
                    raise WoltAuthError(f"refresh rejected ({resp.status}): {body[:200]}")
                if resp.status != 200:
                    raise WoltApiError(f"refresh failed with HTTP {resp.status}")
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise WoltApiError(f"refresh request failed: {err}") from err
        try:
            self._access_token = payload["access_token"]
            self._refresh_token = payload["refresh_token"]
            self._expires_at = time.time() + float(payload.get("expires_in", 1800))
        except (KeyError, TypeError) as err:
            raise WoltApiError(f"unexpected refresh payload: {payload!r}") from err
        await self._save()
        _LOGGER.debug("Wolt access token refreshed, valid for %s s", payload.get("expires_in"))

    async def _ensure_token(self) -> str:
        async with self._lock:
            if not self._access_token or time.time() > self._expires_at - TOKEN_REFRESH_MARGIN:
                await self._refresh()
            assert self._access_token
            return self._access_token

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        token = await self._ensure_token()
        for attempt in (0, 1):
            headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
            try:
                async with self._session.get(
                    url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        # Access token revoked early: force one refresh and retry.
                        async with self._lock:
                            await self._refresh()
                            token = self._access_token or ""
                        continue
                    if resp.status != 200:
                        raise WoltApiError(f"GET {url} -> HTTP {resp.status}")
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise WoltApiError(f"GET {url} failed: {err}") from err
        raise WoltApiError(f"GET {url} kept returning 401")

    async def async_active_purchase_ids(self) -> list[dict[str, Any]]:
        """Return the in-progress orders from the orders page (newest first)."""
        data = await self._get(ORDERS_URL, params={"limit": 5})
        orders = data.get("orders") if isinstance(data, dict) else None
        if not isinstance(orders, list):
            raise WoltApiError(f"orders payload has no list: {str(data)[:200]}")
        return [
            o
            for o in orders
            if isinstance(o, dict)
            and (o.get("telemetry") or {}).get("order_status_type") == ORDER_STATUS_IN_PROGRESS
            and o.get("purchase_id")
        ]

    async def async_tracking(self, purchase_id: str) -> dict[str, Any]:
        data = await self._get(TRACKING_URL.format(purchase_id=purchase_id))
        if not isinstance(data, dict) or "order_details" not in data:
            raise WoltApiError(f"tracking payload malformed: {str(data)[:200]}")
        return data


@dataclass
class WoltState:
    """What the entities read."""

    auth_ok: bool = True
    purchase_id: str | None = None
    status: str | None = None  # received/acknowledged/production/ready/delivered/rejected
    venue_name: str | None = None
    order_number: str | None = None
    item_count: int | None = None
    eta: datetime | None = None
    eta_min: datetime | None = None
    eta_max: datetime | None = None
    dropoff_lat: float | None = None
    dropoff_lon: float | None = None
    courier_lat: float | None = None
    courier_lon: float | None = None
    courier_heading: float | None = None
    courier_vehicle: str | None = None
    courier_delivering_mine: bool | None = None
    courier_distance_m: float | None = None
    courier_close: bool = False
    last_update: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class WoltCoordinator(DataUpdateCoordinator[WoltState]):
    """Adaptive poller: slow when idle, fast while a courier is on the way."""

    def __init__(self, hass: HomeAssistant, api: WoltApi, conf: dict[str, Any]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=conf[CONF_IDLE_INTERVAL]),
        )
        self.api = api
        self.close_distance = float(conf[CONF_CLOSE_DISTANCE])
        self.close_eta = timedelta(minutes=float(conf[CONF_CLOSE_ETA]))
        self._idle = timedelta(seconds=conf[CONF_IDLE_INTERVAL])
        self._active = timedelta(seconds=conf[CONF_ACTIVE_INTERVAL])
        self._courier = timedelta(seconds=conf[CONF_COURIER_INTERVAL])
        self._purchase_id: str | None = None
        self._close_latched_for: str | None = None
        self._auth_notified = False

    def _set_auth(self, ok: bool, reason: str = "") -> None:
        if ok:
            if self._auth_notified:
                persistent_notification.async_dismiss(self.hass, NOTIFICATION_AUTH)
                self._auth_notified = False
            return
        if not self._auth_notified:
            persistent_notification.async_create(
                self.hass,
                "Wolt rejected the refresh token, so courier tracking has stopped. "
                "Log in to wolt.com in a private window, copy the new refresh token and "
                f"call `{DOMAIN}.{SERVICE_SET_REFRESH_TOKEN}` (or update the secret and "
                f"restart).\n\n{reason}",
                title="Wolt tracker needs a new login",
                notification_id=NOTIFICATION_AUTH,
            )
            self._auth_notified = True

    async def _async_update_data(self) -> WoltState:
        prev = self.data or WoltState()
        try:
            state = await self._poll(prev)
        except WoltAuthError as err:
            _LOGGER.warning("Wolt auth failed: %s", err)
            self._set_auth(False, str(err))
            self.update_interval = AUTH_FAILED_INTERVAL
            # Keep the last known order data visible but flag the auth problem.
            prev.auth_ok = False
            return prev
        except WoltApiError as err:
            raise UpdateFailed(str(err)) from err
        self._set_auth(True)
        state.auth_ok = True
        return state

    async def _poll(self, prev: WoltState) -> WoltState:
        state = WoltState(last_update=datetime.now(tz=timezone.utc))

        # 1. Which order (if any) are we following?
        need_discovery = self._purchase_id is None or (
            prev.status in TERMINAL_STATUSES and prev.purchase_id == self._purchase_id
        )
        if need_discovery:
            active = await self.api.async_active_purchase_ids()
            self._purchase_id = active[0]["purchase_id"] if active else None
            if not active:
                self._close_latched_for = None
                self.update_interval = self._idle
                return state

        assert self._purchase_id
        state.purchase_id = self._purchase_id

        # 2. Live tracking for that order.
        data = await self.api.async_tracking(self._purchase_id)
        od = data.get("order_details") or {}
        state.status = od.get("status")
        state.venue_name = od.get("venue_name")
        state.order_number = od.get("order_number")
        items = od.get("items")
        state.item_count = len(items) if isinstance(items, list) else None
        state.eta = _mongo_date(od.get("delivery_eta"))
        state.eta_min = _mongo_date(od.get("delivery_eta_min"))
        state.eta_max = _mongo_date(od.get("delivery_eta_max"))
        coords = ((od.get("delivery_location") or {}).get("coordinates") or {}).get("coordinates")
        if isinstance(coords, list) and len(coords) == 2:
            state.dropoff_lon, state.dropoff_lat = float(coords[0]), float(coords[1])

        drivers = [d for d in (data.get("drivers") or []) if isinstance(d, dict)]
        mine = [d for d in drivers if d.get("delivering_your_order")] or drivers
        if mine:
            d = mine[0]
            loc = d.get("location")
            if isinstance(loc, list) and len(loc) == 2:
                state.courier_lon, state.courier_lat = float(loc[0]), float(loc[1])
            state.courier_heading = d.get("heading")
            state.courier_vehicle = d.get("vehicle")
            state.courier_delivering_mine = bool(d.get("delivering_your_order"))
            if eta := _mongo_date(d.get("delivery_eta")):
                state.eta = eta
            if None not in (state.courier_lat, state.dropoff_lat):
                state.courier_distance_m = round(
                    haversine_m(state.courier_lon, state.courier_lat, state.dropoff_lon, state.dropoff_lat)
                )

        # 3. Derive "courier close".
        if state.status in TERMINAL_STATUSES:
            close = False
            self._close_latched_for = None
        else:
            close = self._close_latched_for == state.purchase_id
            if (
                not close
                and state.status == STATUS_READY
                and state.courier_delivering_mine
                and state.courier_distance_m is not None
            ):
                by_distance = state.courier_distance_m <= self.close_distance
                # A past ETA means "late", not "close"; only a near-future ETA counts.
                by_eta = (
                    state.eta is not None
                    and timedelta(0) <= (state.eta - state.last_update) <= self.close_eta
                )
                if by_distance or by_eta:
                    close = True
                    self._close_latched_for = state.purchase_id
                    _LOGGER.info(
                        "Wolt courier close: %s m, eta %s (order %s)",
                        state.courier_distance_m,
                        state.eta,
                        state.purchase_id,
                    )
        state.courier_close = close

        # 4. Pick the poll rate for the next cycle.
        if state.status in TERMINAL_STATUSES:
            self.update_interval = self._idle
        elif state.status == STATUS_READY and state.courier_lat is not None:
            self.update_interval = self._courier
        else:
            self.update_interval = self._active
        return state


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config.get(DOMAIN)
    if conf is None:
        return True

    api = WoltApi(hass, conf[CONF_REFRESH_TOKEN])
    await api.async_load()
    coordinator = WoltCoordinator(hass, api, conf)
    hass.data[DOMAIN] = coordinator

    async def _set_token(call: ServiceCall) -> None:
        await api.async_set_refresh_token(call.data[CONF_REFRESH_TOKEN].strip())
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_REFRESH_TOKEN,
        _set_token,
        schema=vol.Schema({vol.Required(CONF_REFRESH_TOKEN): cv.string}),
    )

    # Don't block startup on the network; entities show unavailable until the
    # first successful poll.
    await coordinator.async_refresh()
    for platform in PLATFORMS:
        hass.async_create_task(discovery.async_load_platform(hass, platform, DOMAIN, {}, config))
    return True
