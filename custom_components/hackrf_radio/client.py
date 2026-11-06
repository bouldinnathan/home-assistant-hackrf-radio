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
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise AuthenticationError("Bridge authentication failed; check the access token")
                if response.status == 409:
                    raise RadioBusy("Bridge is busy or the sweep was stopped")
                if response.status == 404:
                    raise FirmwareUnavailable("Upgrade the USB bridge to enable firmware maintenance")
                if response.status != 200:
                    raise RadioError("Bridge request failed; check its logs, USB permissions and tool compatibility")
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
            raise RadioError("Cannot reach a compatible HackRF bridge; check its URL, TLS certificate and connection") from err

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


def create_client(hass, data: dict):
    if data["transport"] == "local":
        lock = hass.data.setdefault(DOMAIN, {}).setdefault("usb_lock", asyncio.Lock())
        return LocalRadio(lock, Path(hass.config.path(".storage", DOMAIN, "firmware")))
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    return BridgeClient(async_get_clientsession(hass), data["url"], data["token"])
