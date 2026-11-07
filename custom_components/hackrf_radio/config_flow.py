"""Guided, verified connection setup modeled on MeshNet's config-entry flow."""

from __future__ import annotations

from pathlib import Path

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

from .activity import ActivityLog, redact
from .client import create_client, validate_url
from .const import DEFAULT_SETTINGS, DOMAIN
from .radio import AuthenticationError, RadioBusy, RadioError, validate_serial, validate_settings


class OwnershipRecovery(RadioError):
    """The operator must explicitly close external USB ownership before setup."""


def settings_schema(values: dict) -> vol.Schema:
    return vol.Schema({vol.Required(key, default=values.get(key, default)): int
                       for key, default in DEFAULT_SETTINGS.items()})


class HackRFConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._connection: dict = {}
        self._devices: list[dict] = []
        self._recovery = {}
        self._detail = ""

    async def async_step_user(self, user_input=None):
        return self.async_show_menu(step_id="user", menu_options=["bridge", "local"])

    async def _discover(self, data: dict) -> None:
        self._connection = data
        client = create_client(self.hass, data)
        try:
            state = await client.setup_status()
            applications = state["applications"]
            if applications.get("owner", {}).get("phase", "home_assistant") != "home_assistant":
                self._recovery = applications
                raise OwnershipRecovery
            if applications.get("installer", {}).get("phase") in {"installing", "recovery_required"}:
                raise RadioBusy("Finish or inspect software maintenance on the USB host before setup")
            tools = state.get("tools", {})
            if any(tools.get(name) is False for name in ("hackrf_info", "hackrf_sweep")):
                raise RadioError("Required HackRF tools are missing inside the USB-host runtime; install them there or use the supplied Docker bridge")
            self._devices = await client.devices()
        finally:
            await client.close()
        if not any(device["supported"] for device in self._devices):
            raise RadioError("No compatible device; see bridge logs or device diagnostics for firmware/self-test failures")

    async def _failure(self, error, kind="cannot_connect"):
        self._detail = redact(str(error), (self._connection.get("token", ""), self._connection.get("url", "")))
        readers = self.hass.data.setdefault(DOMAIN, {}).setdefault("activity_readers", {})
        journal = readers.setdefault("setup", ActivityLog(Path(self.hass.config.path(".storage", DOMAIN, "setup", "activity.sqlite")), "setup"))
        await journal.record("setup." + kind, self._detail or "Connection setup failed", level="warning", deduplicate=True)

    async def _discovered(self):
        if self.source in {"reauth", "reconfigure"}:
            entry = self._get_reauth_entry() if self.source == "reauth" else self._get_reconfigure_entry()
            if not any(device["serial"] == entry.data["serial"] for device in self._devices):
                raise RadioError("The configured serial is absent; the existing radio identity was preserved")
            runtime = getattr(entry, "runtime_data", None)
            if runtime and (runtime.firmware.get("active") or runtime.applications.get("installer", {}).get("phase") == "installing"):
                raise RadioBusy("Finish firmware/package maintenance before changing the connection")
            return self.async_update_reload_and_abort(entry, data_updates=self._connection)
        return await self.async_step_device()

    async def async_step_local(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await self._discover({"transport": "local"})
                return await self._discovered()
            except OwnershipRecovery:
                return await self.async_step_recover()
            except RadioBusy as err:
                await self._failure(err, "maintenance_busy")
                errors["base"] = "maintenance_busy"
            except RadioError as err:
                await self._failure(err)
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="local", data_schema=vol.Schema({}), errors=errors, description_placeholders={"detail": self._detail})

    async def async_step_bridge(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await self._discover({"transport": "bridge", "url": validate_url(user_input["url"]), "token": user_input["token"]})
                return await self._discovered()
            except OwnershipRecovery:
                return await self.async_step_recover()
            except AuthenticationError as err:
                await self._failure(err, "invalid_auth")
                errors["base"] = "invalid_auth"
            except RadioBusy as err:
                await self._failure(err, "maintenance_busy")
                errors["base"] = "maintenance_busy"
            except RadioError as err:
                await self._failure(err)
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["url"] = "invalid_url"
        return self.async_show_form(step_id="bridge", data_schema=vol.Schema({
            vol.Required("url", default=(user_input or {}).get("url", "http://")): str,
            vol.Required("token"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }), errors=errors, description_placeholders={"detail": self._detail})

    async def async_step_recover(self, user_input=None):
        errors = {}
        owner = self._recovery.get("owner", {})
        if user_input is not None:
            client = create_client(self.hass, self._connection)
            try:
                serial = validate_serial(user_input.get("serial_confirmation", ""))
                if serial != owner.get("serial") or user_input.get("confirmed_closed") is not True:
                    raise RadioError("Confirm the exact owner serial and that external software has closed its USB connections")
                setup = await client.setup_status()
                fresh = setup["applications"]
                current = fresh.get("owner", {})
                if current.get("phase") != "home_assistant":
                    if current.get("serial") != serial or current.get("lease_id") != owner.get("lease_id"):
                        self._recovery = fresh
                        raise RadioError("USB ownership changed; review the current owner before proceeding")
                    if setup.get("atomic_recovery") is not True:
                        raise RadioError("Upgrade the USB bridge to 0.5 or newer for setup ownership recovery; the existing handoff was preserved")
                    await client.application_action(serial, "recover", {"lease_id": owner.get("lease_id"), "confirmed_closed": True,
                                                                        "stop_managed_program": user_input.get("stop_managed_program") is True})
            except (RadioError, ValueError) as err:
                await self._failure(err, "recovery_failed")
                errors["base"] = "recovery_failed"
            finally:
                await client.close()
            if not errors:
                try:
                    await self._discover(self._connection)
                    return await self._discovered()
                except OwnershipRecovery:
                    return await self.async_step_recover()
                except RadioError as err:
                    await self._failure(err, "recovery_failed")
                    errors["base"] = "recovery_failed"
        return self.async_show_form(step_id="recover", data_schema=vol.Schema({
            vol.Required("serial_confirmation"): str,
            vol.Required("confirmed_closed", default=False): bool,
            vol.Optional("stop_managed_program", default=False): bool,
        }), errors=errors, description_placeholders={"serial": self._recovery.get("owner", {}).get("serial", "unknown"),
                                                    "detail": self._detail})

    async def async_step_reconfigure(self, user_input=None):
        errors = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            try:
                await self._discover({"transport": "bridge", "url": validate_url(user_input["url"]), "token": user_input["token"]})
                return await self._discovered()
            except OwnershipRecovery:
                return await self.async_step_recover()
            except AuthenticationError as err:
                await self._failure(err, "invalid_auth")
                errors["base"] = "invalid_auth"
            except RadioError as err:
                await self._failure(err)
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["url"] = "invalid_url"
        return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema({
            vol.Required("url", default=(user_input or {}).get("url", entry.data.get("url", "http://"))): str,
            vol.Required("token"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }), errors=errors, description_placeholders={"detail": self._detail})

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
                return await self._discovered()
            except OwnershipRecovery:
                return await self.async_step_recover()
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
