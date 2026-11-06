"""Bounded raw I/Q receive captures for external applications."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from uuid import uuid4

from .radio import RadioBusy, RadioError, validate_serial

MAX_CAPTURE_BYTES = 8_000_000
MAX_CAPTURES = 4
CAPTURE_DEFAULTS = {"frequency_hz": 433_920_000, "sample_rate": 8_000_000,
                    "samples": 1_000_000, "lna_gain": 16, "vga_gain": 16}


def capture_settings(values: dict) -> dict:
    if not isinstance(values, dict) or values.keys() - CAPTURE_DEFAULTS.keys():
        raise RadioError("Unknown capture parameters; transmit, RF amplifier and antenna bias controls are not accepted")
    settings = CAPTURE_DEFAULTS | values
    if any(type(value) is not int for value in settings.values()):
        raise RadioError("Capture parameters must be whole numbers")
    if settings["sample_rate"] not in {8_000_000, 10_000_000, 12_500_000, 16_000_000, 20_000_000}:
        raise RadioError("Choose an 8, 10, 12.5, 16 or 20 Msps receive sample rate")
    if not 1024 <= settings["samples"] <= MAX_CAPTURE_BYTES // 2:
        raise RadioError("A capture must contain 1024–4000000 complex samples (at most 8 MB)")
    half = settings["sample_rate"] // 2
    if not 1_000_000 + half <= settings["frequency_hz"] <= 6_000_000_000 - half:
        raise RadioError("Keep the whole receive band inside 1 MHz–6 GHz")
    if settings["lna_gain"] not in range(0, 41, 8) or settings["vga_gain"] not in range(0, 63, 2):
        raise RadioError("Use LNA 0–40 dB in steps of 8 and VGA 0–62 dB in steps of 2")
    return settings


def capture_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise RadioError("Invalid capture identifier")
    return value


async def capture(radio, serial: str, values: dict) -> dict:
    serial, settings = validate_serial(serial), capture_settings(values)
    if radio._applications is None:
        raise RadioError("Configure persistent application storage on the USB host to enable direct captures")
    if radio._closed or radio._lock.locked():
        raise RadioBusy("USB host is busy or closed")
    if radio._firmware:
        await radio._firmware.assert_receive_allowed(serial)
    folder = radio._applications.directory / "captures"
    identifier = uuid4().hex
    path = folder / f"{identifier}.cs8"
    metadata_path = folder / f"{identifier}.json"
    async with radio._lock:
        await radio._assert_managed_access()
        radio._task = asyncio.current_task()
        try:
            devices = await radio._devices()
            device = next((device for device in devices if device.serial == serial), None)
            if device is None or not device.supported:
                raise RadioError("Selected supported HackRF is absent; another radio will not be substituted")
            help_text = await radio._run(["hackrf_transfer", "-h"])
            if not all(re.search(rf"(?<!\w){re.escape(flag)}\b", help_text) for flag in ("-d", "-r", "-n", "-f", "-s", "-a", "-p", "-l", "-g")):
                raise RadioError("hackrf_transfer lacks the required receive/safety flags")

            def prepare_storage():
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                for old in sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[MAX_CAPTURES - 1:]:
                    if re.fullmatch(r"[0-9a-f]{32}\.json", old.name):
                        old.with_suffix(".cs8").unlink(missing_ok=True)
                        old.unlink()
                if shutil.disk_usage(folder).free < MAX_CAPTURE_BYTES + 1_000_000:
                    raise RadioError("Insufficient storage for a bounded receive capture")
            await asyncio.to_thread(prepare_storage)
            await radio._run(["hackrf_transfer", "-d", serial, "-r", str(path),
                              "-f", str(settings["frequency_hz"]), "-s", str(settings["sample_rate"]),
                              "-n", str(settings["samples"]), "-a", "0", "-p", "0",
                              "-l", str(settings["lna_gain"]), "-g", str(settings["vga_gain"])])
            size = (await asyncio.to_thread(path.stat)).st_size
            if size != settings["samples"] * 2:
                raise RadioError("I/Q capture was incomplete or exceeded the requested size")
            raw = await asyncio.to_thread(path.read_bytes)
            if len(raw) != size:
                raise RadioError("I/Q capture changed while it was being read")
            result = {"capture_id": identifier, "serial": serial, "format": "ci8", "byte_count": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest(), "captured_at": datetime.now(UTC).isoformat(),
                      "settings": settings, "rf_amp": False, "antenna_bias": False}
            from .firmware import _durable_json

            await asyncio.to_thread(_durable_json, metadata_path, result)
            return result
        finally:
            radio._task = None
            # Partial captures cannot be downloaded after cancellation/failure.
            if not metadata_path.exists():
                await asyncio.to_thread(path.unlink, missing_ok=True)


async def capture_data(radio, serial: str, identifier: str) -> tuple[dict, bytes]:
    serial, identifier = validate_serial(serial), capture_id(identifier)
    if radio._applications is None:
        raise RadioError("Direct capture storage is unavailable")
    folder = radio._applications.directory / "captures"
    try:
        metadata = json.loads(await asyncio.to_thread((folder / f"{identifier}.json").read_text))
        path = folder / f"{identifier}.cs8"
        if metadata.get("serial") != serial or metadata.get("capture_id") != identifier or path.is_symlink():
            raise ValueError
        if not 2048 <= path.stat().st_size <= MAX_CAPTURE_BYTES:
            raise ValueError
        raw = await asyncio.to_thread(path.read_bytes)
        if len(raw) != metadata["byte_count"] or hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
            raise ValueError
        return metadata, raw
    except (OSError, KeyError, ValueError, TypeError) as err:
        raise RadioError("Capture is missing, expired, belongs to another radio, or failed integrity verification") from err
