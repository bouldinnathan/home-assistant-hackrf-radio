"""One radio, one capture at a time; preserve measurement timestamps."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from uuid import uuid4

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .activity import CURRENT_OPERATION, ActivityLog
from .const import DOMAIN, PRESETS, SAFETY_POLICY
from .firmware import BLOCKING_PHASES
from .radio import AuthenticationError, RadioBusy, RadioError, validate_settings

_LOGGER = logging.getLogger(__name__)


def audited(category):
    def decorate(function):
        @wraps(function)
        async def call(self, *args, **kwargs):
            action = args[0] if args and isinstance(args[0], str) else category
            if category == "monitoring" and args:
                action = "start" if args[0] else "stop"
            if action == "status":
                return await function(self, *args, **kwargs)
            operation = CURRENT_OPERATION.get() or uuid4().hex
            context = CURRENT_OPERATION.set(operation)
            await self.activity.record(category + ".started", "Home Assistant operation started", operation_id=operation,
                                       details={"action": action})
            try:
                try:
                    result = await function(self, *args, **kwargs)
                except asyncio.CancelledError:
                    await self.activity.record(category + ".cancelled", "Home Assistant receive/request operation cancelled", operation_id=operation)
                    raise
                except Exception as err:
                    await self.activity.record(category + ".failed", str(err) if isinstance(err, (RadioError, ValueError)) else "Unexpected operation failure",
                                               level="error", operation_id=operation, details={"exception": type(err).__name__})
                    raise
                await self.activity.record(category + ".completed", "Home Assistant operation completed", operation_id=operation,
                                           details={"action": action})
                return result
            finally:
                CURRENT_OPERATION.reset(context)
        return call
    return decorate


def serialized_applications(function):
    @wraps(function)
    async def call(self, *args, **kwargs):
        async with self._application_lock:
            return await function(self, *args, **kwargs)
    return call


class HackRFCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client) -> None:
        self.entry = entry
        self.client = client
        self.serial = entry.data["serial"]
        self.settings = validate_settings(dict(entry.options))
        self.monitoring = False  # Never restore a running radio after restart.
        self.device = None
        self.spectrum = None
        self._operation: asyncio.Task | None = None
        self._closing = False
        self._closed = False
        self._shutdown_lock = asyncio.Lock()
        self._stopping = False
        self.firmware = {"phase": "idle", "active": False, "catalog": []}
        self.applications = {"available": False, "owner": {"phase": "home_assistant"}, "installer": {"phase": "idle"}, "catalog": []}
        self.last_capture = None
        self.last_result = None
        self.user_presets = {}
        self._result_id = None
        self._host_result_id = None
        self._connected_state = None
        self._application_phase = None
        self._state_lock = asyncio.Lock()
        self._application_lock = asyncio.Lock()
        self.activity = ActivityLog(Path(hass.config.path(".storage", DOMAIN, entry.entry_id, "activity.sqlite")),
                                    "home_assistant", secrets=(entry.data.get("token", ""), entry.data.get("url", ""), self.serial))
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.preferences")
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=timedelta(seconds=self.settings["scan_interval"]))

    async def async_initialize(self):
        stored = await self._store.async_load() or {}
        if not isinstance(stored, dict):
            await self.activity.record("preferences.invalid", "Saved radio preferences are invalid; inspect HA storage", level="warning")
            stored = {}
        self._result_id = stored.get("result_id")
        self._host_result_id = stored.get("host_result_id")
        from .programs import normalized_result

        if stored.get("last_result"):
            try:
                self.last_result = normalized_result(stored["last_result"], self.activity.secrets)
            except RadioError:
                await self.activity.record("preferences.invalid_result", "Saved custom result is invalid; inspect HA storage", level="warning")
        presets = stored.get("presets", {})
        if isinstance(presets, dict):
            for name, values in list(presets.items())[:20]:
                try:
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
                        raise ValueError
                    self.user_presets[name.strip()] = validate_settings(values)
                except ValueError:
                    await self.activity.record("preferences.invalid_preset", "An invalid saved receive profile was ignored; inspect HA storage", level="warning", deduplicate=True)
        await self.activity.record("integration.started", "Home Assistant radio loaded; monitoring starts off")

    async def _save_preferences(self):
        await self._store.async_save({"result_id": self._result_id, "host_result_id": self._host_result_id,
                                      "last_result": self.last_result, "presets": self.user_presets})

    def snapshot(self, include_spectrum: bool = False) -> dict:
        spectrum = self.spectrum
        if spectrum and not include_spectrum:
            spectrum = {key: value for key, value in spectrum.items() if key not in ("frequencies", "powers")}
        if spectrum and include_spectrum:
            # Keep the maximum in each bucket so narrow peaks survive UI reduction.
            frequencies, powers = spectrum["frequencies"], spectrum["powers"]
            stride = max(1, (len(powers) + 1023) // 1024)
            indices = [max(range(start, min(start + stride, len(powers))), key=powers.__getitem__)
                       for start in range(0, len(powers), stride)]
            spectrum = spectrum | {"frequencies": [frequencies[index] for index in indices],
                                   "powers": [powers[index] for index in indices]}
        return {
            "entry_id": self.entry.entry_id, "name": self.entry.title, "serial": self.serial,
            "transport": self.entry.data["transport"], "device": self.device,
            "monitoring": self.monitoring, "busy": self._operation is not None,
            "connected": self.last_update_success and not self.applications_blocked, "fresh": self.has_fresh_spectrum,
            "error": str(self.last_exception) if self.last_exception else None,
            "settings": self.settings, "spectrum": spectrum, "presets": PRESETS,
            "safety_policy": SAFETY_POLICY,
            "firmware": self.firmware,
            "applications": self.applications,
            "capture": self.last_capture,
            "result": self.last_result, "user_presets": self.user_presets,
        }

    @property
    def has_fresh_spectrum(self) -> bool:
        if not self.spectrum:
            return False
        age = (datetime.now(UTC) - datetime.fromisoformat(self.spectrum["timestamp"])).total_seconds()
        return self.last_update_success and not self.applications_blocked and 0 <= age <= max(30, self.settings["scan_interval"] * 2 + 20)

    async def _fetch(self, scan: bool) -> dict:
        if self.applications_blocked:
            raise RadioBusy("External application or software maintenance owns the USB host")
        if self._closing or self._stopping:
            raise RadioError("Radio is stopping")
        if self._operation:
            raise RadioBusy("A capture is already in progress")

        async def operation():
            if scan:
                self.spectrum = await self.client.sweep(self.serial, self.settings)
            else:
                devices = await self.client.devices()
                self.device = next((device for device in devices if device["serial"] == self.serial), None)
                if not self.device:
                    raise RadioError("Selected serial is absent; connect the configured device")
                if not self.device["supported"]:
                    raise RadioError(self.device["blocked_reason"])

        self._operation = asyncio.create_task(operation())
        try:
            await self._operation
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # Stop cancelled only the child; keep the HA update machinery alive.
        finally:
            self._operation = None
        return self.snapshot()

    async def _async_update_data(self) -> dict:
        if self._closing or self._stopping or self._operation:
            return self.snapshot()
        try:
            await self.async_applications("status")
            if self.applications_blocked:
                self.monitoring = False
                return self.snapshot()
            await self.async_firmware("status")
            if self.firmware["phase"] in BLOCKING_PHASES:
                self.monitoring = False
                return self.snapshot()
            result = await self._fetch(self.monitoring)
            if self._connected_state is not True:
                await self.activity.record("connection.online", "Configured radio is accessible and compatible")
            self._connected_state = True
            return result
        except AuthenticationError as err:
            self.monitoring = False
            await self.activity.record("connection.authentication", str(err), level="error", deduplicate=True)
            self._connected_state = False
            raise ConfigEntryAuthFailed(str(err)) from err
        except RadioError as err:
            # Disarm on error: reconnection never resumes scanning on its own.
            self.monitoring = False
            await self.activity.record("connection.failed", str(err), level="warning", deduplicate=True)
            self._connected_state = False
            raise UpdateFailed(str(err)) from err

    @audited("sweep")
    async def async_scan_once(self) -> None:
        if self.applications_blocked:
            raise RadioBusy("Return USB control from external software before receiving")
        if self.firmware["phase"] in BLOCKING_PHASES:
            raise RadioBusy("Firmware maintenance blocks receiving")
        try:
            data = await self._fetch(True)
        except RadioError as err:
            self.monitoring = False
            self.async_set_update_error(UpdateFailed(str(err)))
            raise
        self.async_set_updated_data(data)

    @audited("monitoring")
    async def async_set_monitoring(self, enabled: bool) -> None:
        if self._closing and enabled:
            raise RadioError("Radio connection is closed")
        if enabled:
            if self._stopping:
                raise RadioBusy("Radio is stopping")
            self.monitoring = True
            try:
                await self.async_scan_once()
            except BaseException:
                self.monitoring = False
                raise
        else:
            self.monitoring = False
            self._stopping = True
            try:
                await self.client.stop()
                if task := self._operation:
                    task.cancel()
                    with suppress(asyncio.CancelledError, RadioError):
                        await task
            finally:
                self._stopping = False
        self.async_update_listeners()

    @audited("settings")
    async def async_apply_settings(self, values: dict) -> None:
        settings = validate_settings(values)
        if settings == self.settings:
            return
        await self.async_set_monitoring(False)
        self.settings = settings
        self.spectrum = None
        self.update_interval = timedelta(seconds=settings["scan_interval"])
        await self.activity.record("settings.saved", "Receive settings saved; monitoring remains off", details=settings)
        self.async_set_updated_data(self.snapshot())

    @audited("firmware")
    async def async_firmware(self, action: str, **values) -> dict:
        if action != "status" and self.applications_blocked:
            raise RadioBusy("Return control and finish software maintenance before firmware operations")
        if action == "status":
            status = await self.client.firmware_status(self.serial)
            if not isinstance(status, dict) or not isinstance(status.get("phase"), str):
                raise RadioError("Invalid firmware status from USB host")
            self.firmware = status
        elif action == "prepare":
            await self.async_set_monitoring(False)
            self.spectrum = None
            await self.client.firmware_prepare(self.serial, values["target"])
            self.firmware = await self.client.firmware_status(self.serial)
        elif action == "install":
            await self.async_set_monitoring(False)
            self.firmware = await self.client.firmware_install(self.serial, values["plan_id"], values["confirmation"])
        elif action == "verify":
            self.firmware = await self.client.firmware_verify(self.serial)
            await self.async_request_refresh()
        else:
            raise RadioError("Unknown firmware action")
        self.async_update_listeners()
        return self.snapshot(include_spectrum=True)

    @property
    def applications_blocked(self) -> bool:
        return (self.applications.get("owner", {}).get("phase", "home_assistant") != "home_assistant"
                or self.applications.get("installer", {}).get("phase") in {"installing", "recovery_required"})

    @audited("application")
    @serialized_applications
    async def async_applications(self, action: str, **values) -> dict:
        from .software import validate_action

        validate_action(action, values)
        if action == "status":
            state = await self.client.application_status()
            if not isinstance(state, dict) or not isinstance(state.get("owner"), dict):
                raise RadioError("Invalid application ownership status from USB host")
            self.applications = state
        else:
            if action in {"handoff", "launch", "release", "install"}:
                await self.async_set_monitoring(False)
            self.applications = await self.client.application_action(self.serial, action, values)
        phase = (self.applications.get("owner", {}).get("phase"), self.applications.get("installer", {}).get("phase"),
                 self.applications.get("running"), self.applications.get("last_exit"))
        if phase != self._application_phase:
            await self.activity.record("application.state", "USB-host application status changed", details={"owner": phase[0], "installer": phase[1], "running": phase[2]})
            self._application_phase = phase
            self.hass.bus.async_fire("hackrf_radio_activity", {"entry_id": self.entry.entry_id, "owner": phase[0], "installer": phase[1], "running": phase[2]})
        result = self.applications.get("result")
        if isinstance(result, dict) and result.get("serial") == self.serial and result.get("id") != self._host_result_id and result.get("data"):
            try:
                await self.async_accept_result(result, host=True)
            except RadioError as err:
                await self.activity.record("result.rejected", str(err), level="warning", deduplicate=True)
        self.async_update_listeners()
        return self.snapshot(include_spectrum=True)

    @audited("capture")
    async def async_capture(self, settings: dict) -> dict:
        if self.applications_blocked or self.firmware["phase"] in BLOCKING_PHASES:
            raise RadioBusy("Finish external application/firmware maintenance before direct receive capture")
        await self.async_set_monitoring(False)
        self.last_capture = await self.client.direct_capture(self.serial, settings)
        self.async_update_listeners()
        return self.snapshot(include_spectrum=True)

    async def async_accept_result(self, result: dict, *, host: bool = False):
        from .programs import normalized_result

        result = normalized_result(result, self.activity.secrets)
        async with self._state_lock:
            previous = self._result_id, self._host_result_id, self.last_result
            if host:
                if result["id"] == self._host_result_id:
                    return
                self._host_result_id = result["id"]
            duplicate = result["id"] == self._result_id
            self._result_id = result["id"]
            self.last_result = result
            try:
                await self._save_preferences()
            except Exception as err:
                self._result_id, self._host_result_id, self.last_result = previous
                raise RadioError("Could not save custom measurements; inspect Home Assistant storage") from err
            if duplicate:
                return result
        self.hass.bus.async_fire("hackrf_radio_result", {"entry_id": self.entry.entry_id, **result})
        await self.activity.record("result.received", "Custom program measurements received", details={"source": result["source"]})
        self.async_update_listeners()
        return result

    @audited("result")
    async def async_report_result(self, source: str, data: dict):
        return await self.async_accept_result({"id": uuid4().hex, "source": source, "timestamp": datetime.now(UTC).isoformat(), "data": data})

    @audited("preset")
    async def async_preset(self, action: str, name: str, settings: dict | None = None):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise RadioError("Preset names must contain 1–60 characters")
        name = name.strip()
        if action == "apply":
            if name not in self.user_presets:
                raise RadioError("Unknown saved preset")
            values = self.user_presets[name]
            await self.async_apply_settings(values)
            self.hass.config_entries.async_update_entry(self.entry, options=values)
        elif action in {"save", "delete"}:
            async with self._state_lock:
                if action == "save":
                    if name not in self.user_presets and len(self.user_presets) >= 20:
                        raise RadioError("At most 20 saved presets are supported")
                    self.user_presets[name] = validate_settings(settings if settings is not None else self.settings)
                else:
                    self.user_presets.pop(name, None)
                await self._save_preferences()
        else:
            raise RadioError("Unknown preset action")
        self.async_update_listeners()
        return self.snapshot(include_spectrum=True)

    async def async_activity(self, source: str = "home_assistant", **filters):
        if source == "home_assistant":
            return await self.activity.read(**filters)
        if source != "usb_host":
            raise RadioError("Invalid log source")
        return await self.client.activity_read(**filters)

    async def async_on_stop(self, event) -> None:
        await self.async_shutdown()

    async def async_shutdown(self) -> None:
        self._closing = True
        async with self._shutdown_lock:
            if self._closed:
                return
            await super().async_shutdown()
            try:
                await self.async_set_monitoring(False)
            finally:
                await self.client.close()
                self._closed = True
