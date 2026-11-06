"""Shared device identity for all native HA entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class HackRFEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial}_{key}"
        self._attr_name = name
        device = coordinator.device or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.serial)}, name=coordinator.entry.title,
            manufacturer="HackRF compatible", model=device.get("model", "HackRF"),
            sw_version=device.get("firmware"), hw_version=device.get("revision"),
            serial_number=coordinator.serial,
        )
