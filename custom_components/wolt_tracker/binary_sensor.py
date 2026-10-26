"""Binary sensors: courier close, order active, support unread."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WoltConfigEntry
from .const import TERMINAL_STATUSES
from .entity import WoltEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WoltConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            WoltCourierCloseSensor(coordinator),
            WoltOrderActiveSensor(coordinator),
            WoltSupportUnreadSensor(coordinator),
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


class WoltSupportUnreadSensor(WoltEntity, BinarySensorEntity):
    """On while the latest message in the open support chat was written by a
    Wolt agent and has no read receipt from you (reading it in the Wolt app
    or calling wolt_tracker.mark_read turns it off)."""

    _attr_name = "Support unread"
    _attr_icon = "mdi:message-alert"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "support_unread")

    @property
    def is_on(self) -> bool:
        return self.state_data.support_unread

    @property
    def extra_state_attributes(self):
        s = self.state_data
        return {
            "conversation_id": s.support_conversation_id,
            "purchase_id": s.support_purchase_id,
            "venue": s.venue_name if s.support_purchase_id == s.purchase_id else None,
            "agent": s.support_agent,
            "message": s.support_message if s.support_unread else None,
            "message_id": s.support_message_id if s.support_unread else None,
            "received_at": s.support_message_at.isoformat() if s.support_unread and s.support_message_at else None,
            "unread_count": len(s.support_unread_ids),
        }
