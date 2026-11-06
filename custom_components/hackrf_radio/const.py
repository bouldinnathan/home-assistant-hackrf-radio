"""Shared constants; kept independent of Home Assistant for the bridge."""

DOMAIN = "hackrf_radio"
VERSION = "0.3.0"
PLATFORMS = ["sensor", "binary_sensor", "switch", "button"]
DEFAULT_SETTINGS = {
    "start_mhz": 430,
    "end_mhz": 440,
    "bin_width": 100_000,
    "lna_gain": 16,
    "vga_gain": 16,
    "scan_interval": 15,
    "threshold_db": -40,
}
PRESETS = {
    "433 MHz": (430, 440),
    "868 MHz": (863, 870),
    "915 MHz": (902, 928),
    "FM broadcast": (88, 108),
    "2.4 GHz": (2400, 2484),
}
MAX_BINS = 20_000
MAX_OUTPUT_BYTES = 2_000_000
COMMAND_TIMEOUT = 20
SAFETY_POLICY = "receive_only_v1"
