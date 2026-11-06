"""Diagnostics deliberately omit access tokens, host URLs and radio serials."""

from homeassistant.components.diagnostics import async_redact_data


async def async_get_config_entry_diagnostics(hass, entry):
    snapshot = entry.runtime_data.snapshot()
    snapshot["error"] = type(entry.runtime_data.last_exception).__name__ if entry.runtime_data.last_exception else None
    return async_redact_data(snapshot, {"serial", "entry_id", "name", "backup", "plan_id", "web_url", "lease_id", "soapy_bind"})
