"""Adaptive poller that derives the "courier close" signal."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WoltApi, WoltApiError, WoltAuthError
from .const import (
    CONF_ACTIVE_INTERVAL,
    CONF_CLOSE_DISTANCE,
    CONF_CLOSE_ETA,
    CONF_COURIER_INTERVAL,
    CONF_IDLE_INTERVAL,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_CLOSE_DISTANCE,
    DEFAULT_CLOSE_ETA,
    DEFAULT_COURIER_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    STATUS_READY,
    TERMINAL_STATUSES,
)

_LOGGER = logging.getLogger(__name__)


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


@dataclass
class WoltState:
    """What the entities read."""

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


class WoltCoordinator(DataUpdateCoordinator[WoltState]):
    """Slow when idle, fast while a courier is on the way."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: WoltApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_IDLE_INTERVAL),
        )
        self.api = api
        self.entry = entry
        self.apply_options(entry.options)
        self._purchase_id: str | None = None
        self._close_latched_for: str | None = None

    def apply_options(self, options: dict[str, Any]) -> None:
        self.close_distance = float(options.get(CONF_CLOSE_DISTANCE, DEFAULT_CLOSE_DISTANCE))
        self.close_eta = timedelta(minutes=float(options.get(CONF_CLOSE_ETA, DEFAULT_CLOSE_ETA)))
        self._idle = timedelta(seconds=int(options.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)))
        self._active = timedelta(seconds=int(options.get(CONF_ACTIVE_INTERVAL, DEFAULT_ACTIVE_INTERVAL)))
        self._courier = timedelta(
            seconds=int(options.get(CONF_COURIER_INTERVAL, DEFAULT_COURIER_INTERVAL))
        )

    async def _async_update_data(self) -> WoltState:
        try:
            return await self._poll(self.data or WoltState())
        except WoltAuthError as err:
            # Marks entities unavailable and starts HA's re-authentication flow.
            raise ConfigEntryAuthFailed(str(err)) from err
        except WoltApiError as err:
            raise UpdateFailed(str(err)) from err

    async def _poll(self, prev: WoltState) -> WoltState:
        state = WoltState(last_update=datetime.now(tz=timezone.utc))

        # 1. Which order (if any) are we following?
        need_discovery = self._purchase_id is None or (
            prev.status in TERMINAL_STATUSES and prev.purchase_id == self._purchase_id
        )
        if need_discovery:
            active = await self.api.async_active_orders()
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

        # 3. Derive "courier close" (latched per order until it ends).
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

        # 4. Poll rate for the next cycle.
        if state.status in TERMINAL_STATUSES:
            self.update_interval = self._idle
        elif state.status == STATUS_READY and state.courier_lat is not None:
            self.update_interval = self._courier
        else:
            self.update_interval = self._active
        return state
