"""Sensors: order status, courier distance, courier ETA."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN
from .entity import WoltEntity


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    coordinator = hass.data[DOMAIN]
    async_add_entities(
        [
            WoltOrderStatusSensor(coordinator),
            WoltCourierDistanceSensor(coordinator),
            WoltCourierEtaSensor(coordinator),
        ]
    )


class WoltOrderStatusSensor(WoltEntity, SensorEntity):
    _attr_name = "Order status"
    _attr_icon = "mdi:moped"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "order_status")

    @property
    def native_value(self) -> str:
        s = self.state_data
        return s.status or ("idle" if s.purchase_id is None else "unknown")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.state_data
        return {
            "purchase_id": s.purchase_id,
            "venue": s.venue_name,
            "order_number": s.order_number,
            "item_count": s.item_count,
            "eta": s.eta.isoformat() if s.eta else None,
            "eta_min": s.eta_min.isoformat() if s.eta_min else None,
            "eta_max": s.eta_max.isoformat() if s.eta_max else None,
            "auth_ok": s.auth_ok,
            "last_update": s.last_update.isoformat() if s.last_update else None,
        }


class WoltCourierDistanceSensor(WoltEntity, SensorEntity):
    """Distance from the courier to the drop-off. Carries lat/lon so the map
    card can show the courier."""

    _attr_name = "Courier distance"
    _attr_icon = "mdi:map-marker-distance"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "courier_distance")

    @property
    def native_value(self) -> float | None:
        return self.state_data.courier_distance_m

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.state_data
        return {
            "latitude": s.courier_lat,
            "longitude": s.courier_lon,
            "heading": s.courier_heading,
            "vehicle": s.courier_vehicle,
            "delivering_your_order": s.courier_delivering_mine,
            "dropoff_latitude": s.dropoff_lat,
            "dropoff_longitude": s.dropoff_lon,
        }


class WoltCourierEtaSensor(WoltEntity, SensorEntity):
    _attr_name = "Courier ETA"
    _attr_icon = "mdi:clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "courier_eta")

    @property
    def native_value(self):
        return self.state_data.eta
