"""Shared entity base for the Wolt tracker."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WoltCoordinator, WoltState


class WoltEntity(CoordinatorEntity[WoltCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WoltCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="Wolt",
            manufacturer="Wolt",
            model="Order tracker",
        )

    @property
    def state_data(self) -> WoltState:
        return self.coordinator.data or WoltState()
