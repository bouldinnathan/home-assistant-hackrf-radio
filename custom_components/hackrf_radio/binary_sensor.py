"""Connectivity and fresh signal-threshold events for automations."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .entity import HackRFEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([HackRFConnected(entry.runtime_data), HackRFSignal(entry.runtime_data)])


class HackRFConnected(HackRFEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "connected", "Connected")

    @property
    def available(self):
        return True

    @property
    def is_on(self):
        return self.coordinator.last_update_success


class HackRFSignal(HackRFEntity, BinarySensorEntity):
    _attr_icon = "mdi:signal"

    def __init__(self, coordinator):
        super().__init__(coordinator, "signal_detected", "Signal detected")

    @property
    def is_on(self):
        if not self.coordinator.has_fresh_spectrum:
            return None
        return self.coordinator.spectrum["peak_db"] >= self.coordinator.settings["threshold_db"]
