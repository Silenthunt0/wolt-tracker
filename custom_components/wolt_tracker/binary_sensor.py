"""Binary sensors: courier close, order active, support unread, rating pending."""

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
            WoltRatingPendingSensor(coordinator),
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


class WoltRatingPendingSensor(WoltEntity, BinarySensorEntity):
    """On while a delivered order is waiting for its rating (the two-step
    "How was it?" prompt in the Wolt app). The attributes carry the form:
    per step the five labels (value 1..5) and the tags allowed with each
    value, so whoever rates can pick from Wolt's own words. Rating in the
    app, wolt_tracker.rate or wolt_tracker.dismiss_rating turn it off."""

    _attr_name = "Rating pending"
    _attr_icon = "mdi:star-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "rating_pending")

    @property
    def is_on(self) -> bool:
        return self.state_data.rating_pending

    @property
    def extra_state_attributes(self):
        s = self.state_data
        tpl = s.rating_template or {}
        attrs = {
            "purchase_id": s.rating_purchase_id,
            "venue": s.rating_venue,
            "ordered_at": s.rating_ordered_at.isoformat() if s.rating_ordered_at else None,
            "steps": list(tpl),
        }
        for step, data in tpl.items():
            attrs[f"{step}_title"] = data.get("title")
            attrs[f"{step}_ratings"] = data.get("ratings")
            attrs[f"{step}_tags"] = {
                value: [t["name"] or t["key"] for t in tags] for value, tags in (data.get("tags") or {}).items()
            }
        return attrs
