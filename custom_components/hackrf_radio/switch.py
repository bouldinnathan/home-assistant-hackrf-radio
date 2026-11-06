"""Explicitly armed periodic receive scans, always off after restart/errors."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import HackRFEntity
from .radio import RadioError


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HackRFMonitoring(entry.runtime_data)])


class HackRFMonitoring(HackRFEntity, SwitchEntity):
    _attr_icon = "mdi:radar"

    def __init__(self, coordinator):
        super().__init__(coordinator, "monitoring", "Monitoring")

    @property
    def is_on(self):
        return self.coordinator.monitoring

    async def async_turn_on(self, **kwargs):
        try:
            await self.coordinator.async_set_monitoring(True)
        except RadioError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs):
        try:
            await self.coordinator.async_set_monitoring(False)
        except RadioError as err:
            raise HomeAssistantError(str(err)) from err
