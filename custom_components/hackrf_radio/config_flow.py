"""Guided, verified connection setup modeled on MeshNet's config-entry flow."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import create_client, validate_url
from .const import DEFAULT_SETTINGS, DOMAIN
from .radio import AuthenticationError, RadioError, validate_serial, validate_settings


def settings_schema(values: dict) -> vol.Schema:
    return vol.Schema({vol.Required(key, default=values.get(key, default)): int
                       for key, default in DEFAULT_SETTINGS.items()})


class HackRFConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._connection: dict = {}
        self._devices: list[dict] = []

    async def async_step_user(self, user_input=None):
        return self.async_show_menu(step_id="user", menu_options=["bridge", "local"])

    async def _discover(self, data: dict) -> None:
        client = create_client(self.hass, data)
        try:
            self._devices = await client.devices()
        finally:
            await client.close()
        if not any(device["supported"] for device in self._devices):
            raise RadioError("No compatible device; see bridge logs or device diagnostics for firmware/self-test failures")
        self._connection = data

    async def async_step_local(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await self._discover({"transport": "local"})
                return await self.async_step_device()
            except RadioError:
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="local", data_schema=vol.Schema({}), errors=errors)

    async def async_step_bridge(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await self._discover({"transport": "bridge", "url": validate_url(user_input["url"]), "token": user_input["token"]})
                return await self.async_step_device()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except RadioError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["url"] = "invalid_url"
        return self.async_show_form(step_id="bridge", data_schema=vol.Schema({
            vol.Required("url", default=(user_input or {}).get("url", "http://")): str,
            vol.Required("token"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }), errors=errors)

    async def async_step_device(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                serial = validate_serial(user_input["serial"])
                if not any(device["serial"] == serial and device["supported"] for device in self._devices):
                    raise ValueError
                if user_input.get("acknowledge_input_safety") is not True:
                    errors["acknowledge_input_safety"] = "safety_required"
                else:
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=user_input["name"].strip() or "HackRF Radio",
                        data=self._connection | {"serial": serial}, options=DEFAULT_SETTINGS.copy(),
                    )
            except ValueError:
                errors["serial"] = "invalid_device"
        options = [SelectOptionDict(value=device["serial"], label=f"{device['model']} · {device['serial']} · {device['firmware']}")
                   for device in self._devices if device["supported"]]
        return self.async_show_form(step_id="device", data_schema=vol.Schema({
            vol.Required("name", default="HackRF Radio"): str,
            vol.Required("serial"): SelectSelector(SelectSelectorConfig(options=options)),
            vol.Required("acknowledge_input_safety", default=False): bool,
        }), errors=errors, description_placeholders={
            "hardware_guidance_url": "https://hackrf.readthedocs.io/en/latest/hackrf_one.html#maximum-input-power",
        })

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                data = dict(entry.data) | {"token": user_input["token"]}
                await self._discover(data)
                if not any(device["serial"] == entry.data["serial"] for device in self._devices):
                    raise RadioError("Configured radio is absent")
                return self.async_update_reload_and_abort(entry, data_updates={"token": user_input["token"]})
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except RadioError:
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({
            vol.Required("token"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HackRFOptionsFlow()


class HackRFOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                settings = validate_settings(user_input)
                return self.async_create_entry(title="", data=settings)
            except ValueError:
                errors["base"] = "invalid_settings"
        return self.async_show_form(step_id="init", data_schema=settings_schema(user_input or dict(self.config_entry.options)), errors=errors)
