"""Shared entity base for the Wolt tracker."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WoltCoordinator, WoltState
from .const import DOMAIN


class WoltEntity(CoordinatorEntity[WoltCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WoltCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "wolt")},
            name="Wolt",
            manufacturer="Wolt",
            model="Order tracker",
            entry_type=None,
        )

    @property
    def state_data(self) -> WoltState:
        return self.coordinator.data or WoltState()
