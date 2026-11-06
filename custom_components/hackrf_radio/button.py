"""One bounded receive sweep exposed through the standard button service."""

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import HackRFEntity
from .radio import RadioError


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HackRFSweep(entry.runtime_data)])


class HackRFSweep(HackRFEntity, ButtonEntity):
    _attr_icon = "mdi:radar"

    def __init__(self, coordinator):
        super().__init__(coordinator, "sweep", "Sweep now")

    async def async_press(self):
        try:
            await self.coordinator.async_scan_once()
        except RadioError as err:
            raise HomeAssistantError(str(err)) from err
