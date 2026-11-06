"""Small recorder-friendly spectrum summaries; FFT arrays stay out of states."""

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass

from .entity import HackRFEntity

SENSORS = (
    ("peak_db", "Peak level", "dB"),
    ("noise_floor_db", "Noise floor", "dB"),
    ("peak_frequency_mhz", "Peak frequency", "MHz"),
    ("bin_count", "Frequency bins", None),
    ("timestamp", "Last sweep", None),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(HackRFSensor(entry.runtime_data, *description) for description in SENSORS)
    async_add_entities([HackRFApplicationSensor(entry.runtime_data), HackRFResultSensor(entry.runtime_data)])


class HackRFSensor(HackRFEntity, SensorEntity):
    _attr_icon = "mdi:waveform"

    def __init__(self, coordinator, key, name, unit) -> None:
        super().__init__(coordinator, key, name)
        self._key = key
        self._attr_native_unit_of_measurement = unit
        if key == "timestamp":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        spectrum = self.coordinator.spectrum
        if not spectrum:
            return None
        if self._key == "timestamp":
            return datetime.fromisoformat(spectrum["timestamp"])
        if not self.coordinator.has_fresh_spectrum:
            return None
        return round(spectrum[self._key], 3)


class HackRFApplicationSensor(HackRFEntity, SensorEntity):
    _attr_icon = "mdi:application-cog"

    def __init__(self, coordinator):
        super().__init__(coordinator, "application_owner", "Application owner")

    @property
    def native_value(self):
        return self.coordinator.applications.get("owner", {}).get("name", "Home Assistant")

    @property
    def extra_state_attributes(self):
        state = self.coordinator.applications
        return {"running": state.get("running", False), "app_id": state.get("running_app"),
                "last_exit": state.get("last_exit"), "installer_phase": state.get("installer", {}).get("phase")}


class HackRFResultSensor(HackRFEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:code-json"

    def __init__(self, coordinator):
        super().__init__(coordinator, "custom_result", "Custom result")

    @property
    def native_value(self):
        result = self.coordinator.last_result
        return datetime.fromisoformat(result["timestamp"]) if result else None

    @property
    def extra_state_attributes(self):
        result = self.coordinator.last_result or {}
        return {"source": result.get("source"), "measurements": result.get("data", {})}
