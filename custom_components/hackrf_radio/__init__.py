"""HackRF Radio integration. Imports stay lazy so the bridge runs without HA."""

from __future__ import annotations

from pathlib import Path

from .const import DOMAIN, PLATFORMS, VERSION


def _config_schema(config):
    # Keep Home Assistant imports lazy so the standalone USB bridge needs only
    # aiohttp, while declaring UI-only configuration to HA and hassfest.
    from homeassistant.helpers import config_validation as cv

    return cv.config_entry_only_config_schema(DOMAIN)(config)


CONFIG_SCHEMA = _config_schema


async def async_setup(hass, config) -> bool:
    from homeassistant.components import panel_custom
    from homeassistant.components.http import StaticPathConfig

    from .http_api import CaptureView
    from .services import async_register_services
    from .websocket_api import async_register_commands

    hass.data.setdefault(DOMAIN, {})
    async_register_commands(hass)
    async_register_services(hass)
    hass.http.register_view(CaptureView)
    await hass.http.async_register_static_paths([
        StaticPathConfig("/hackrf_radio_static", str(Path(__file__).parent / "frontend"), cache_headers=False),
    ])
    await panel_custom.async_register_panel(
        hass, webcomponent_name="hackrf-radio-panel", frontend_url_path="hackrf-radio",
        module_url=f"/hackrf_radio_static/hackrf-radio-panel.js?v={VERSION}",
        sidebar_title="HackRF Radio", sidebar_icon="mdi:radio-tower", require_admin=True,
    )
    return True


async def async_setup_entry(hass, entry) -> bool:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    from .client import create_client
    from .coordinator import HackRFCoordinator

    coordinator = HackRFCoordinator(hass, entry, create_client(hass, entry.data))
    entry.runtime_data = coordinator
    forwarding = False
    try:
        await coordinator.async_initialize()
        await coordinator.async_config_entry_first_refresh()
        forwarding = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        try:
            if forwarding:
                await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        finally:
            await coordinator.async_shutdown()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_on_stop))
    return True


async def async_unload_entry(hass, entry) -> bool:
    # Stop USB work before entities disappear; preserve resources if unload fails.
    await entry.runtime_data.async_firmware("status")
    if entry.runtime_data.firmware.get("active"):
        return False
    await entry.runtime_data.async_set_monitoring(False)
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_shutdown()
    return True


async def _async_update_options(hass, entry) -> None:
    await entry.runtime_data.async_apply_settings(dict(entry.options))
