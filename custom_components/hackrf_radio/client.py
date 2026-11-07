"""Authenticated bridge client and connection factory."""

from __future__ import annotations

import asyncio
import json
import math
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import aiohttp
from yarl import URL

from .activity import CURRENT_OPERATION
from .const import COMMAND_TIMEOUT, DOMAIN, MAX_BINS, MAX_OUTPUT_BYTES, SAFETY_POLICY
from .radio import AuthenticationError, LocalRadio, RadioBusy, RadioError, validate_serial, validate_settings


class FirmwareUnavailable(RadioError):
    """An older bridge can still receive without supporting firmware updates."""


def validate_url(value: str) -> str:
    try:
        url = URL(value)
        if (url.scheme not in ("http", "https") or not url.host or url.user is not None
                or url.query_string or url.fragment):
            raise ValueError
        _ = url.port
    except (ValueError, TypeError) as err:
        raise ValueError("Use an HTTP(S) bridge URL without credentials, query or fragment") from err
    return str(url).rstrip("/")


def validate_spectrum(result: dict, serial: str, settings: dict) -> dict:
    """Treat remote data as untrusted, including NaN/Infinity and device identity."""
    try:
        if result["serial"] != serial or result["safety_policy"] != SAFETY_POLICY:
            raise ValueError
        frequencies, powers = result["frequencies"], result["powers"]
        if (not isinstance(frequencies, list) or not isinstance(powers, list)
                or not 1 <= len(frequencies) == len(powers) <= MAX_BINS
                or validate_settings(result["settings"]) != settings):
            raise ValueError
        if not all(type(value) in (float, int) and math.isfinite(value) for value in frequencies + powers):
            raise ValueError
        if (frequencies != sorted(set(frequencies))
                or frequencies[0] < settings["start_mhz"] * 1e6
                or frequencies[-1] >= settings["end_mhz"] * 1e6):
            raise ValueError
        timestamp = datetime.fromisoformat(result["timestamp"])
        if timestamp.tzinfo is None:
            raise ValueError
        index = max(range(len(powers)), key=powers.__getitem__)
        return {
            "timestamp": timestamp.isoformat(), "frequencies": frequencies, "powers": powers,
            "peak_db": powers[index], "peak_frequency_mhz": frequencies[index] / 1e6,
            "noise_floor_db": sorted(powers)[len(powers) // 2], "bin_count": len(powers),
            "settings": settings, "serial": serial, "safety_policy": SAFETY_POLICY,
        }
    except (KeyError, TypeError, ValueError, OverflowError) as err:
        raise RadioError("Bridge returned an invalid spectrum or mismatched device/safety policy") from err


class BridgeClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = validate_url(url)
        self._token = token
        self._operation: str | None = None
        self._task: asyncio.Task | None = None

    async def _request(self, method: str, path: str, data: dict | None = None, timeout: float = COMMAND_TIMEOUT + 10) -> dict:
        try:
            async with self._session.request(
                method, self._url + path, json=data,
                headers={"Authorization": f"Bearer {self._token}", "X-HackRF-Operation-ID": CURRENT_OPERATION.get() or uuid4().hex},
                timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise AuthenticationError("Bridge authentication failed; check the access token")
                if response.status == 409:
                    raise RadioBusy("Bridge is busy or the sweep was stopped")
                if response.status == 404:
                    raise FirmwareUnavailable("Upgrade the USB bridge to enable firmware maintenance")
                if response.status != 200:
                    raise RadioError(f"Bridge request failed (HTTP {response.status}); check its logs, USB permissions and tool compatibility")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_OUTPUT_BYTES:
                        raise RadioError("Bridge response exceeded the maximum size")
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get("safety_policy") != SAFETY_POLICY:
                    raise RadioError("Bridge has an incompatible safety policy; use the matching bridge version")
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise RadioError(f"Cannot reach a compatible HackRF bridge ({type(err).__name__}); check its URL, TLS certificate and connection") from err

    async def devices(self) -> list[dict]:
        result = await self._request("GET", "/v1/devices")
        devices = result.get("devices")
        if not isinstance(devices, list) or not devices or len(devices) > 32:
            raise RadioError("Bridge returned no accessible devices")
        try:
            serials = [validate_serial(device["serial"]) for device in devices]
            if len(set(serials)) != len(serials):
                raise ValueError
            for device in devices:
                if (type(device["supported"]) is not bool or type(device["board_id"]) is not int
                        or not all(isinstance(device[key], str) for key in ("model", "firmware", "api", "revision", "warning", "blocked_reason"))):
                    raise ValueError
        except (KeyError, TypeError, ValueError) as err:
            raise RadioError("Bridge returned invalid device metadata") from err
        return devices

    async def sweep(self, serial: str, settings: dict) -> dict:
        serial, settings = validate_serial(serial), validate_settings(settings)
        if self._operation:
            raise RadioBusy("A sweep is already in progress")
        self._operation = uuid4().hex
        self._task = asyncio.current_task()
        try:
            result = await self._request("POST", "/v1/sweep", {
                "serial": serial, "settings": settings, "operation_id": self._operation,
            })
            return validate_spectrum(result, serial, settings)
        except asyncio.CancelledError:
            with suppress(RadioError):
                await self._request("POST", "/v1/stop", {"operation_id": self._operation}, timeout=5)
            raise
        finally:
            self._operation = None
            self._task = None

    async def stop(self) -> None:
        if operation := self._operation:
            # Exact operation IDs prevent one entry stopping another radio.
            try:
                await self._request("POST", "/v1/stop", {"operation_id": operation}, timeout=5)
            finally:
                if (task := self._task) and task is not asyncio.current_task():
                    task.cancel()
                    with suppress(asyncio.CancelledError, RadioError):
                        await task

    async def close(self) -> None:
        await self.stop()

    async def firmware_status(self, serial: str) -> dict:
        try:
            result = await self._request("POST", "/v1/firmware/status", {"serial": validate_serial(serial)})
        except FirmwareUnavailable as err:
            return {"phase": "unavailable", "message": str(err), "catalog": [], "active": False}
        return result["firmware"]

    async def firmware_prepare(self, serial: str, target: str) -> dict:
        result = await self._request("POST", "/v1/firmware/prepare", {"serial": validate_serial(serial), "target": target}, timeout=180)
        return result["plan"]

    async def firmware_install(self, serial: str, plan_id: str, confirmation: dict) -> dict:
        result = await self._request("POST", "/v1/firmware/install", {"serial": validate_serial(serial), "plan_id": plan_id, "confirmation": confirmation})
        return result["firmware"]

    async def firmware_verify(self, serial: str) -> dict:
        result = await self._request("POST", "/v1/firmware/verify", {"serial": validate_serial(serial)})
        return result["firmware"]

    async def application_status(self) -> dict:
        try:
            result = await self._request("POST", "/v1/applications/status", {})
        except FirmwareUnavailable:
            from dataclasses import asdict

            from .software import CATALOG

            return {"available": False, "owner": {"phase": "home_assistant"}, "catalog": [asdict(app) | {"installed": False} for app in CATALOG.values()],
                    "installer": {"phase": "idle"}, "running": False, "plan": None,
                    "install_enabled": False, "launch_enabled": False}
        return result["applications"]

    async def application_action(self, serial: str, action: str, values: dict) -> dict:
        from .software import validate_action

        validate_action(action, values)
        result = await self._request("POST", f"/v1/applications/{action}", {"serial": validate_serial(serial), **values})
        return result["applications"]

    async def activity_read(self, **filters) -> dict:
        from urllib.parse import urlencode

        try:
            return (await self._request("GET", "/v1/activity?" + urlencode(filters)))["activity"]
        except FirmwareUnavailable:
            return {"records": [], "next_before": 0, "storage_error": "Upgrade the USB bridge to view host activity", "retention": 0}

    async def setup_status(self) -> dict:
        try:
            result = await self._request("GET", "/v1/setup")
            state = result.get("setup")
        except FirmwareUnavailable:
            # Applications/status is also admin-only on receive-token capable
            # older bridges. Validate the role before exposing device setup.
            state = {"applications": await self.application_status(), "tools": {}, "legacy": True}
        if not isinstance(state, dict) or not isinstance(state.get("applications"), dict):
            raise RadioError("Invalid setup capabilities from the USB bridge")
        owner = state["applications"].get("owner")
        if not isinstance(owner, dict) or not isinstance(owner.get("phase"), str) or owner["phase"] not in {"home_assistant", "external"}:
            raise RadioError("Invalid ownership status from the USB bridge")
        tools = state.get("tools", {})
        installer = state["applications"].get("installer", {"phase": "idle"})
        if (not isinstance(tools, dict) or len(tools) > 32
                or not all(isinstance(key, str) and type(value) is bool for key, value in tools.items())
                or not isinstance(installer, dict) or not isinstance(installer.get("phase"), str)
                or installer["phase"] not in {"idle", "complete", "installing", "recovery_required"}):
            raise RadioError("Invalid setup tool/maintenance status from the USB bridge")
        if owner["phase"] == "external":
            import re

            try:
                validate_serial(owner["serial"])
                if not isinstance(owner.get("lease_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", owner["lease_id"]):
                    raise ValueError
            except (KeyError, ValueError) as err:
                raise RadioError("External USB ownership metadata is invalid; inspect the bridge state before setup") from err
        return state

    async def direct_capture(self, serial: str, settings: dict) -> dict:
        from .control import capture_settings

        settings = capture_settings(settings)
        if self._operation:
            raise RadioBusy("A radio operation is already in progress")
        self._operation, self._task = uuid4().hex, asyncio.current_task()
        try:
            result = await self._request("POST", "/v1/capture", {"serial": validate_serial(serial), "settings": settings, "operation_id": self._operation})
            return result["capture"]
        except asyncio.CancelledError:
            with suppress(RadioError):
                await self._request("POST", "/v1/stop", {"operation_id": self._operation}, timeout=5)
            raise
        finally:
            self._operation, self._task = None, None

    async def capture_data(self, serial: str, identifier: str) -> tuple[dict, bytes]:
        from .control import MAX_CAPTURE_BYTES, capture_id

        identifier = capture_id(identifier)
        try:
            async with self._session.get(self._url + f"/v1/captures/{validate_serial(serial)}/{identifier}",
                                         headers={"Authorization": f"Bearer {self._token}"},
                                         timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False) as response:
                if response.status != 200:
                    raise RadioError("Receive capture is unavailable from the bridge")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_CAPTURE_BYTES:
                        raise RadioError("Receive capture exceeded the maximum size")
                import hashlib

                digest = hashlib.sha256(data).hexdigest()
                if len(data) < 2048 or len(data) % 2 or response.headers.get("X-HackRF-SHA256") != digest:
                    raise RadioError("Receive capture failed size or SHA256 verification")
                return {"capture_id": identifier, "serial": serial, "sha256": digest}, bytes(data)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RadioError("Cannot download the receive capture") from err


def create_client(hass, data: dict):
    if data["transport"] == "local":
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP

        from .software import Applications

        lock = hass.data.setdefault(DOMAIN, {}).setdefault("usb_lock", asyncio.Lock())
        programs = Path(hass.config.path("hackrf_radio_programs.json"))
        if "applications" not in hass.data[DOMAIN]:
            applications = Applications(lock, Path(hass.config.path(".storage", DOMAIN, "applications")), profiles_path=programs)
            hass.data[DOMAIN]["applications"] = applications

            async def close_host_programs(_event):
                await applications.close()

            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_host_programs)
        applications = hass.data[DOMAIN]["applications"]
        # Entries and discovery connections borrow the HA-host manager. Only
        # explicit app control or host shutdown may stop its owned processes.
        return LocalRadio(lock, Path(hass.config.path(".storage", DOMAIN, "firmware")), applications=applications, owns_applications=False)
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    return BridgeClient(async_get_clientsession(hass), data["url"], data["token"])
