"""Bounded receive-only interface to the upstream HackRF tools.

RF amp and antenna power are explicitly disabled on every sweep. Firmware
maintenance is isolated in firmware.py, with reviewed images and durable jobs.
No register, clock, reset, transmit, shell, or user-supplied CLI arguments exist.
"""

from __future__ import annotations

import asyncio
import csv
import math
import re
import signal
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

from .const import COMMAND_TIMEOUT, DEFAULT_SETTINGS, MAX_BINS, MAX_OUTPUT_BYTES, SAFETY_POLICY


class RadioError(Exception):
    """An actionable failure with a stable, non-secret message."""


class RadioBusy(RadioError):
    """Another operation already owns the radio."""


class AuthenticationError(RadioError):
    """The bridge rejected authentication."""


def validate_serial(value: Any) -> str:
    """Require a complete hardware serial; never select by USB index/suffix."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise ValueError("Select a complete 32-character HackRF serial number")
    return value.lower()


def validate_settings(values: dict[str, Any]) -> dict[str, int]:
    """One strict boundary shared by UI, options, WebSocket and bridge."""
    if not isinstance(values, dict) or set(values) - DEFAULT_SETTINGS.keys():
        raise ValueError("Unknown settings; RF amplifier and antenna power are locked off")
    settings = DEFAULT_SETTINGS | values
    for key, value in settings.items():
        if type(value) is not int:
            raise ValueError(f"{key} must be a whole number")
    start, end = settings["start_mhz"], settings["end_mhz"]
    if not 1 <= start < end <= 6000 or end - start > 100:
        raise ValueError("Choose 1–6000 MHz with a positive span of at most 100 MHz")
    # Upstream rounds each sweep up to a whole 20 MHz tuning step.
    if start + math.ceil((end - start) / 20) * 20 > 6000:
        raise ValueError("The tool's rounded 20 MHz sweep would exceed 6000 MHz; lower the start frequency")
    width = settings["bin_width"]
    if not 2445 <= width <= 5_000_000 or math.ceil((end - start + 20) * 1_000_000 / width) > MAX_BINS:
        raise ValueError("Choose 2445–5000000 Hz bins and at most 20000 bins per sweep")
    if settings["lna_gain"] not in range(0, 41, 8) or settings["vga_gain"] not in range(0, 63, 2):
        raise ValueError("LNA gain must be 0–40 in steps of 8; VGA gain 0–62 in steps of 2")
    if not 5 <= settings["scan_interval"] <= 3600 or not -150 <= settings["threshold_db"] <= 0:
        raise ValueError("Scan interval must be 5–3600 seconds; threshold must be -150–0 dB")
    return settings


@dataclass(frozen=True)
class Device:
    """Reported identity and capability decision; no firmware guessing by date."""

    serial: str
    board_id: int
    model: str
    firmware: str
    api: str
    revision: str = "Unknown (older firmware/board)"
    warning: str = ""
    blocked_reason: str = ""

    @property
    def supported(self) -> bool:
        return not self.blocked_reason

    def as_dict(self) -> dict:
        return asdict(self) | {"supported": self.supported}


def parse_info(output: str) -> list[Device]:
    """Handle old/new tools, One/r9/Pro, clones and Mayhem version strings."""
    devices = []
    for block in output.split("Found HackRF")[1:]:
        serial = re.search(r"Serial number:\s*([0-9a-fA-F]{32})\b", block)
        board = re.search(r"Board ID Number:\s*(\d+)\s*\(([^)]+)\)", block)
        firmware = re.search(r"Firmware Version:\s*(.*?)\s*\(API:\s*([0-9a-fA-F]+\.[0-9a-fA-F]+)\)", block)
        if not serial or not board or not firmware:
            # Busy/inaccessible devices often have a serial but no metadata.
            continue
        revision = re.search(r"Hardware Revision:\s*([^\r\n]+)", block)
        reason = ""
        if int(board[1]) not in (2, 4, 5):
            reason = "Unsupported board; only HackRF One and HackRF Pro compatible boards are enabled"
        elif tuple(int(part, 16) for part in firmware[2].split(".")) < (1, 2):
            reason = "Firmware lacks sweep API 1.02; follow the upstream manual migration procedure"
        elif "Error: Firmware does not support" in block or "Self-test FAIL" in block:
            reason = "Device reports incompatible firmware or failed self-test; scanning is blocked"
        warnings = []
        if "does not appear to have been manufactured" in block:
            warnings.append("Third-party board reported; manufacturer and protection circuitry are unverified")
        if "Hardware revision not recognized" in block or "Hardware revision not yet detected" in block:
            warnings.append("Hardware revision is unrecognized; verify board and firmware manually")
        devices.append(Device(validate_serial(serial[1]), int(board[1]), board[2], firmware[1], firmware[2],
                              revision[1].strip() if revision else Device.__dataclass_fields__["revision"].default,
                              "; ".join(warnings), reason))
    if len({device.serial for device in devices}) != len(devices):
        raise RadioError("Duplicate serial numbers detected; disconnect duplicate/cloned devices before continuing")
    if not devices:
        raise RadioError("No accessible HackRF found. Check USB permissions, cable, other SDR apps and PortaPack HackRF mode")
    return devices


def sweep_arguments(serial: str, settings: dict) -> list[str]:
    """Build the entire allowlisted command; power flags cannot be overridden."""
    serial = validate_serial(serial)
    settings = validate_settings(settings)
    return ["hackrf_sweep", "-d", serial, "-1", "-a", "0", "-p", "0",
            "-f", f"{settings['start_mhz']}:{settings['end_mhz']}",
            "-w", str(settings["bin_width"]), "-l", str(settings["lna_gain"]),
            "-g", str(settings["vga_gain"])]


def parse_sweep(output: str, settings: dict) -> dict:
    """Sort interleaved FFT rows, trim rounded edges and reject invalid data."""
    settings = validate_settings(settings)
    points: dict[float, float] = {}
    for row in csv.reader(StringIO(output)):
        if not row:
            continue
        try:
            low, high, width = (float(row[index].strip()) for index in (2, 3, 4))
            powers = [float(value.strip()) for value in row[6:]]
            if (not all(math.isfinite(value) for value in (low, high, width))
                    or width <= 0 or high <= low or not powers or len(powers) > MAX_BINS
                    or abs((high - low) / width - len(powers)) > 1.1):
                raise ValueError
            for index, power in enumerate(powers):
                frequency = low + (index + 0.5) * width
                if math.isfinite(power) and settings["start_mhz"] * 1e6 <= frequency < settings["end_mhz"] * 1e6:
                    points[frequency] = power
            if len(points) > MAX_BINS:
                raise ValueError
        except (ValueError, IndexError, OverflowError) as err:
            raise RadioError("Malformed sweep output; check host tool and firmware compatibility") from err
    if not points:
        raise RadioError("Sweep returned no finite spectrum data")
    frequencies = sorted(points)
    powers = [points[frequency] for frequency in frequencies]
    peak_index = max(range(len(powers)), key=powers.__getitem__)
    ordered = sorted(powers)
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "frequencies": frequencies,
        "powers": powers,
        "peak_db": powers[peak_index],
        "peak_frequency_mhz": frequencies[peak_index] / 1e6,
        "noise_floor_db": ordered[len(ordered) // 2],
        "bin_count": len(powers),
        "settings": settings,
    }


class LocalRadio:
    """Serialize access and reap children on errors, timeout, cancellation or stop."""

    def __init__(self, lock: asyncio.Lock | None = None, firmware_directory: Path | None = None,
                 applications=None, application_directory: Path | None = None,
                 allow_install: bool = False, allow_launch: bool = False, soapy_bind: str = "127.0.0.1",
                 profiles_path: Path | None = None, owns_applications: bool = True) -> None:
        from .activity import ActivityLog

        self._lock = lock if lock is not None else asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._capabilities_checked = False
        self._firmware = None
        self._applications = applications
        self._owns_applications = owns_applications
        log_directory = application_directory or (firmware_directory.parent / "applications" if firmware_directory else None)
        self.activity = applications.activity if applications else ActivityLog((log_directory / "activity.sqlite") if log_directory else None)
        if firmware_directory is not None:
            from .firmware import FirmwareManager

            self._firmware = FirmwareManager(self, firmware_directory)
        if self._applications is None and (application_directory is not None or firmware_directory is not None):
            from .software import Applications

            directory = application_directory if application_directory is not None else firmware_directory.parent / "applications"
            self._applications = Applications(self._lock, directory, allow_install=allow_install, allow_launch=allow_launch,
                                              soapy_bind=soapy_bind, profiles_path=profiles_path, activity=self.activity, profiles_required=profiles_path is not None)

    async def _assert_managed_access(self) -> None:
        if self._applications:
            await self._applications.gate()

    async def _run(self, args: list[str], timeout: float = COMMAND_TIMEOUT) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as err:
            raise RadioError("HackRF tools unavailable; install hackrf on the USB host or use the bridge") from err

        async def read_limited(stream) -> bytes:
            data = bytearray()
            while chunk := await stream.read(65536):
                data.extend(chunk)
                if len(data) > MAX_OUTPUT_BYTES:
                    raise RadioError("HackRF output exceeded the bounded capture size")
            return bytes(data)

        readers = [asyncio.create_task(read_limited(process.stdout)), asyncio.create_task(read_limited(process.stderr))]
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
            # -h exits with failure in several upstream releases.
            if process.returncode and args not in (["hackrf_sweep", "-h"], ["hackrf_spiflash", "-h"], ["hackrf_transfer", "-h"]):
                await self.activity.record("usb.command_failed", stderr.decode("utf-8", errors="replace") or "HackRF command failed",
                                           level="error", details={"tool": args[0], "returncode": process.returncode}, deduplicate=True)
                if args[0] == "hackrf_spiflash":
                    raise RadioError("SPI flash read failed; inspect USB access and the firmware maintenance status")
                raise RadioError("HackRF command failed; check USB access, firmware support, and whether another SDR app owns the device")
            return (stdout + (stderr if args[-1] == "-h" else b"")).decode("utf-8", errors="replace")
        except TimeoutError as err:
            await self.activity.record("usb.timeout", "HackRF command exceeded its deadline", level="warning", details={"tool": args[0]}, deduplicate=True)
            if args[0] == "hackrf_spiflash":
                raise RadioError("SPI flash read timed out; inspect firmware maintenance status and retain the backup") from err
            raise RadioError("HackRF operation timed out; scanning stopped without resetting or flashing the device") from err
        finally:
            # Readers cancelled by a timeout must be replaced so full pipes
            # cannot prevent the child from exiting during cleanup.
            drains = []
            for reader, stream in zip(readers, (process.stdout, process.stderr), strict=True):
                if reader.done():
                    async def drain(pipe=stream):
                        while await pipe.read(65536):
                            pass
                    drains.append(asyncio.create_task(drain()))
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.send_signal(signal.SIGINT)
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await asyncio.gather(*drains, return_exceptions=True)

    async def _devices(self) -> list[Device]:
        devices = parse_info(await self._run(["hackrf_info"]))
        if not self._capabilities_checked:
            help_text = await self._run(["hackrf_sweep", "-h"])
            if not all(re.search(rf"(?<!\w){re.escape(flag)}\b", help_text) for flag in ("-d", "-1", "-a", "-p", "-f", "-w", "-l", "-g")):
                raise RadioError("hackrf_sweep lacks required safety/selection flags; install compatible host tools manually")
            self._capabilities_checked = True
        return devices

    async def devices(self) -> list[dict]:
        if self._closed:
            raise RadioError("Radio connection is closed")
        if self._lock.locked():
            raise RadioBusy("Radio is busy; wait for the current sweep")
        async with self._lock:
            await self._assert_managed_access()
            self._task = asyncio.current_task()
            try:
                return [device.as_dict() for device in await self._devices()]
            finally:
                self._task = None

    async def sweep(self, serial: str, settings: dict) -> dict:
        serial = validate_serial(serial)
        args = sweep_arguments(serial, settings)
        if self._firmware:
            await self._firmware.assert_receive_allowed(serial)
        if self._closed:
            raise RadioError("Radio connection is closed")
        if self._lock.locked():
            raise RadioBusy("Radio is busy; overlapping captures are blocked")
        async with self._lock:
            await self._assert_managed_access()
            self._task = asyncio.current_task()
            try:
                device = next((item for item in await self._devices() if item.serial == serial), None)
                if device is None:
                    raise RadioError("Selected serial is absent; refusing to operate another radio")
                if not device.supported:
                    raise RadioError(device.blocked_reason)
                result = parse_sweep(await self._run(args), settings)
                return result | {"serial": serial, "safety_policy": SAFETY_POLICY}
            finally:
                self._task = None

    async def stop(self) -> None:
        """Cancel the owner and wait until its subprocess has actually exited."""
        if (task := self._task) and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError, RadioError):
                await task

    async def firmware_status(self, serial: str) -> dict:
        if not self._firmware:
            return {"phase": "unavailable", "message": "Configure persistent firmware storage on the USB host", "catalog": [], "active": False}
        return await self._firmware.status(serial)

    async def firmware_prepare(self, serial: str, target: str) -> dict:
        if not self._firmware:
            raise RadioError("Persistent firmware storage is required")
        return await self._firmware.prepare(serial, target)

    async def firmware_install(self, serial: str, plan_id: str, confirmation: dict) -> dict:
        if not self._firmware:
            raise RadioError("Persistent firmware storage is required")
        return await self._firmware.install(serial, plan_id, confirmation)

    async def firmware_verify(self, serial: str) -> dict:
        if not self._firmware:
            raise RadioError("Persistent firmware storage is required")
        return await self._firmware.verify_boot(serial)

    async def direct_capture(self, serial: str, settings: dict) -> dict:
        from .control import capture

        return await capture(self, serial, settings)

    async def capture_data(self, serial: str, identifier: str) -> tuple[dict, bytes]:
        from .control import capture_data

        return await capture_data(self, serial, identifier)

    async def activity_read(self, **filters) -> dict:
        return await self.activity.read(**filters)

    async def setup_status(self) -> dict:
        import shutil

        tools = await asyncio.to_thread(lambda: {name: bool(shutil.which(name)) for name in
                                                ("hackrf_info", "hackrf_sweep", "hackrf_transfer", "hackrf_spiflash")})
        return {"applications": await self.application_status(), "tools": tools, "atomic_recovery": True}

    async def application_status(self) -> dict:
        if not self._applications:
            from .software import CATALOG

            return {"available": False, "owner": {"phase": "home_assistant"},
                    "catalog": [asdict(app) | {"installed": False} for app in CATALOG.values()],
                    "installer": {"phase": "idle"}, "running": False, "plan": None,
                    "install_enabled": False, "launch_enabled": False}
        return await self._applications.status() | {"available": True}

    async def application_action(self, serial: str, action: str, values: dict) -> dict:
        if not self._applications:
            raise RadioError("Configure persistent application storage on the USB host")
        manager = self._applications
        if action == "handoff":
            return await manager.acquire(self, serial, values["app_id"], values["confirmed"])
        if action == "release":
            return await manager.release(self, serial, values["confirmed_closed"])
        if action == "recover":
            return await manager.recover(self, serial, values["lease_id"], values["confirmed_closed"], values["stop_managed_program"])
        if action == "launch":
            return await manager.launch(self, serial, values["app_id"], values["confirmed"])
        if action == "stop":
            return await manager.stop()
        if action == "link":
            return await manager.save_link(values["app_id"], values["url"])
        if action == "prepare_install":
            return await manager.prepare_install(values["apps"])
        if action == "install":
            return await manager.install(self, values["plan_id"], values["confirmed"])
        raise RadioError("Unknown application action")

    async def close(self) -> None:
        self._closed = True
        await self.stop()
        if self._firmware:
            await self._firmware.wait()
        if self._applications and self._owns_applications:
            await self._applications.close()
