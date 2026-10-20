"""Binary sensors: courier close, order active, auth problem."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN, TERMINAL_STATUSES
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
            WoltCourierCloseSensor(coordinator),
            WoltOrderActiveSensor(coordinator),
            WoltAuthProblemSensor(coordinator),
        ]
    )


class WoltCourierCloseSensor(WoltEntity, BinarySensorEntity):
    """On from the moment the courier is within the configured distance / ETA
    until the order reaches a terminal state."""

    _attr_name = "Courier close"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "courier_close")

    @property
    def is_on(self) -> bool:
        return self.state_data.courier_close

    @property
    def extra_state_attributes(self):
        s = self.state_data
        return {
            "distance_m": s.courier_distance_m,
            "eta": s.eta.isoformat() if s.eta else None,
            "purchase_id": s.purchase_id,
            "venue": s.venue_name,
        }


class WoltOrderActiveSensor(WoltEntity, BinarySensorEntity):
    _attr_name = "Order active"
    _attr_icon = "mdi:shopping"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "order_active")

    @property
    def is_on(self) -> bool:
        s = self.state_data
        return s.purchase_id is not None and s.status not in TERMINAL_STATUSES


class WoltAuthProblemSensor(WoltEntity, BinarySensorEntity):
    _attr_name = "Auth problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "auth_problem")

    @property
    def is_on(self) -> bool:
        return not self.state_data.auth_ok

    @property
    def available(self) -> bool:
        # Must stay available while auth is broken, otherwise the problem is invisible.
        return True
