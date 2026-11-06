"""Admin-only panel commands; return no connection URLs or access tokens."""

from pathlib import Path

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback

from .const import DOMAIN
from .radio import RadioError, validate_settings


def coordinator_for(hass, entry_id):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        raise RadioError("Radio is not loaded; check Settings > Devices & services")
    return entry.runtime_data


async def async_read_activity(hass, entry_id, source="home_assistant", **filters):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise RadioError("Unknown radio")
    if source == "home_assistant":
        # Setup/reconnect failures must remain diagnosable after entry unload.
        from .activity import ActivityLog

        readers = hass.data.setdefault(DOMAIN, {}).setdefault("activity_readers", {})
        journal = readers.setdefault(entry.entry_id, ActivityLog(Path(hass.config.path(".storage", DOMAIN, entry.entry_id, "activity.sqlite")), "home_assistant"))
        return await journal.read(**filters)
    return await coordinator_for(hass, entry_id).async_activity(source=source, **filters)


@callback
def async_register_commands(hass):
    for handler in (list_radios, get_radio, control_radio, firmware_radio, applications_radio, capture_radio, activity_radio, preset_radio):
        websocket_api.async_register_command(hass, handler)


@websocket_api.websocket_command({vol.Required("type"): "hackrf_radio/list"})
@websocket_api.require_admin
@callback
def list_radios(hass, connection, msg):
    connection.send_result(msg["id"], [
        {"entry_id": entry.entry_id, "name": entry.title, "loaded": entry.state is ConfigEntryState.LOADED}
        for entry in hass.config_entries.async_entries(DOMAIN)
    ])


@websocket_api.websocket_command({vol.Required("type"): "hackrf_radio/get", vol.Required("entry_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def get_radio(hass, connection, msg):
    try:
        coordinator = coordinator_for(hass, msg["entry_id"])
        await coordinator.async_applications("status")
        await coordinator.async_firmware("status")
        connection.send_result(msg["id"], coordinator.snapshot(include_spectrum=True))
    except RadioError as err:
        connection.send_error(msg["id"], "radio_unavailable", str(err))


@websocket_api.websocket_command({
    vol.Required("type"): "hackrf_radio/control", vol.Required("entry_id"): str,
    vol.Required("action"): vol.In(["sweep", "start", "stop", "configure"]),
    vol.Optional("settings"): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
async def control_radio(hass, connection, msg):
    try:
        coordinator = coordinator_for(hass, msg["entry_id"])
        action = msg["action"]
        if action == "configure":
            settings = validate_settings(msg.get("settings", {}))
            await coordinator.async_apply_settings(settings)
            hass.config_entries.async_update_entry(coordinator.entry, options=settings)
        elif action == "sweep":
            await coordinator.async_scan_once()
        else:
            await coordinator.async_set_monitoring(action == "start")
        connection.send_result(msg["id"], coordinator.snapshot(include_spectrum=True))
    except (RadioError, ValueError) as err:
        connection.send_error(msg["id"], "radio_error", str(err))


@websocket_api.websocket_command({
    vol.Required("type"): "hackrf_radio/firmware", vol.Required("entry_id"): str,
    vol.Required("action"): vol.In(["status", "prepare", "install", "verify"]),
    vol.Optional("target"): str, vol.Optional("plan_id"): str, vol.Optional("confirmation"): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
async def firmware_radio(hass, connection, msg):
    try:
        fields = {"status": set(), "prepare": {"target"}, "install": {"plan_id", "confirmation"}, "verify": set()}
        values = {key: value for key, value in msg.items() if key not in {"id", "type", "entry_id", "action"}}
        if set(values) != fields[msg["action"]]:
            raise RadioError("Firmware request fields do not match its action")
        coordinator = coordinator_for(hass, msg["entry_id"])
        result = await coordinator.async_firmware(msg["action"], **values)
        connection.send_result(msg["id"], result)
    except (RadioError, ValueError) as err:
        connection.send_error(msg["id"], "firmware_error", str(err))


@websocket_api.websocket_command({
    vol.Required("type"): "hackrf_radio/applications", vol.Required("entry_id"): str,
    vol.Required("action"): vol.In(["status", "handoff", "release", "launch", "stop", "link", "prepare_install", "install"]),
    vol.Optional("values", default={}): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
async def applications_radio(hass, connection, msg):
    try:
        coordinator = coordinator_for(hass, msg["entry_id"])
        result = await coordinator.async_applications(msg["action"], **msg["values"])
        connection.send_result(msg["id"], result)
    except (RadioError, ValueError, TypeError) as err:
        connection.send_error(msg["id"], "application_error", str(err))


@websocket_api.websocket_command({
    vol.Required("type"): "hackrf_radio/capture", vol.Required("entry_id"): str,
    vol.Required("settings"): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
async def capture_radio(hass, connection, msg):
    try:
        result = await coordinator_for(hass, msg["entry_id"]).async_capture(msg["settings"])
        connection.send_result(msg["id"], result)
    except (RadioError, ValueError) as err:
        connection.send_error(msg["id"], "capture_error", str(err))


@websocket_api.websocket_command({
    vol.Required("type"): "hackrf_radio/activity", vol.Required("entry_id"): str,
    vol.Optional("source", default="home_assistant"): vol.In(["home_assistant", "usb_host"]),
    vol.Optional("level", default="all"): vol.In(["all", "debug", "info", "warning", "error"]),
    vol.Optional("search", default=""): vol.All(str, vol.Length(max=120)),
    vol.Optional("limit", default=200): vol.All(int, vol.Range(min=1, max=500)),
    vol.Optional("before", default=0): vol.All(int, vol.Range(min=0)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def activity_radio(hass, connection, msg):
    try:
        filters = {key: value for key, value in msg.items() if key not in {"id", "type", "entry_id"}}
        connection.send_result(msg["id"], await async_read_activity(hass, msg["entry_id"], **filters))
    except (RadioError, ValueError) as err:
        connection.send_error(msg["id"], "activity_error", str(err))


@websocket_api.websocket_command({
    vol.Required("type"): "hackrf_radio/preset", vol.Required("entry_id"): str,
    vol.Required("action"): vol.In(["save", "apply", "delete"]), vol.Required("name"): str,
    vol.Optional("settings"): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
async def preset_radio(hass, connection, msg):
    try:
        values = {key: value for key, value in msg.items() if key not in {"id", "type", "entry_id"}}
        connection.send_result(msg["id"], await coordinator_for(hass, msg["entry_id"]).async_preset(**values))
    except (RadioError, ValueError) as err:
        connection.send_error(msg["id"], "preset_error", str(err))
