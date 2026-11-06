"""One radio, one capture at a time; preserve measurement timestamps."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PRESETS, SAFETY_POLICY
from .firmware import BLOCKING_PHASES
from .radio import AuthenticationError, RadioBusy, RadioError, validate_settings

_LOGGER = logging.getLogger(__name__)


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
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=timedelta(seconds=self.settings["scan_interval"]))

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
            return await self._fetch(self.monitoring)
        except AuthenticationError as err:
            self.monitoring = False
            raise ConfigEntryAuthFailed(str(err)) from err
        except RadioError as err:
            # Disarm on error: reconnection never resumes scanning on its own.
            self.monitoring = False
            raise UpdateFailed(str(err)) from err

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

    async def async_apply_settings(self, values: dict) -> None:
        settings = validate_settings(values)
        if settings == self.settings:
            return
        await self.async_set_monitoring(False)
        self.settings = settings
        self.spectrum = None
        self.update_interval = timedelta(seconds=settings["scan_interval"])
        self.async_set_updated_data(self.snapshot())

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
        self.async_update_listeners()
        return self.snapshot(include_spectrum=True)

    async def async_capture(self, settings: dict) -> dict:
        if self.applications_blocked or self.firmware["phase"] in BLOCKING_PHASES:
            raise RadioBusy("Finish external application/firmware maintenance before direct receive capture")
        await self.async_set_monitoring(False)
        self.last_capture = await self.client.direct_capture(self.serial, settings)
        self.async_update_listeners()
        return self.snapshot(include_spectrum=True)

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
