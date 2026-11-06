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
