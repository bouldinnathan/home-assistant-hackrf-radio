"""Home Assistant actions for automations, Node-RED and user-owned systems."""

import voluptuous as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from .const import DOMAIN
from .radio import RadioError, validate_settings
from .websocket_api import async_read_activity, coordinator_for


@callback
def async_register_services(hass):
    entry = {vol.Required("entry_id"): str}
    schemas = {
        "sweep": entry,
        "capture": entry | {vol.Required("settings"): dict},
        "monitoring": entry | {vol.Required("enabled"): bool},
        "configure": entry | {vol.Required("settings"): dict},
        "application": entry | {vol.Required("action"): vol.In(["launch", "stop", "handoff", "release"]),
                                vol.Optional("app_id"): str, vol.Optional("confirmed", default=False): bool,
                                vol.Optional("confirmed_closed", default=False): bool},
        "preset": entry | {vol.Required("action"): vol.In(["save", "apply", "delete"]), vol.Required("name"): str,
                           vol.Optional("settings"): dict},
        "report_result": entry | {vol.Required("source"): str, vol.Required("data"): dict},
        "get_activity": entry | {vol.Optional("source", default="home_assistant"): vol.In(["home_assistant", "usb_host"]),
                                 vol.Optional("level", default="all"): vol.In(["all", "debug", "info", "warning", "error"]),
                                 vol.Optional("search", default=""): str, vol.Optional("limit", default=200): vol.All(int, vol.Range(min=1, max=500)),
                                 vol.Optional("before", default=0): vol.All(int, vol.Range(min=0))},
    }

    async def handle(call):
        # HA 2025's admin-service helper discards responses. Register through
        # the public response-capable registry and enforce the same permission
        # check here on every action, including reads. Internal automations
        # have no user ID and remain authorized by HA.
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.is_admin:
                raise Unauthorized(context=call.context)
        data = dict(call.data)
        data.pop("entry_id")
        try:
            if call.service == "get_activity":
                return await async_read_activity(hass, call.data["entry_id"], **data)
            coordinator = coordinator_for(hass, call.data["entry_id"])
            if call.service == "sweep":
                await coordinator.async_scan_once()
            elif call.service == "capture":
                return await coordinator.async_capture(data["settings"])
            elif call.service == "monitoring":
                await coordinator.async_set_monitoring(data["enabled"])
            elif call.service == "configure":
                settings = validate_settings(data["settings"])
                await coordinator.async_apply_settings(settings)
                hass.config_entries.async_update_entry(coordinator.entry, options=settings)
            elif call.service == "application":
                action = data["action"]
                values = ({"app_id": data["app_id"], "confirmed": data["confirmed"]} if action in {"launch", "handoff"} else
                          {"confirmed_closed": data["confirmed_closed"]} if action == "release" else {})
                return await coordinator.async_applications(action, **values)
            elif call.service == "preset":
                return await coordinator.async_preset(**data)
            elif call.service == "report_result":
                return {"result": await coordinator.async_report_result(**data)}
        except (RadioError, ValueError, KeyError, TypeError) as err:
            # Use stable public errors; never include arbitrary host exception text.
            raise HomeAssistantError(str(err) if isinstance(err, (RadioError, ValueError)) else "Invalid HackRF action fields") from err
        return coordinator.snapshot()

    for name, schema in schemas.items():
        hass.services.async_register(DOMAIN, name, handle, vol.Schema(schema), supports_response=SupportsResponse.OPTIONAL)
