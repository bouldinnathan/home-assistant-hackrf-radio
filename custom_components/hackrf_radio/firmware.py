"""Reviewed firmware catalog and persistent, explicitly confirmed update jobs.

No arbitrary URLs/uploads, compatibility overrides, auto-reset or rollback.
The write process belongs to this manager, never to an HTTP/WS request.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import struct
import tarfile
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import aiohttp
from yarl import URL

from .radio import Device, RadioBusy, RadioError, parse_info, validate_serial

ACTIVE_PHASES = {"backing_up", "writing", "verifying"}
BLOCKING_PHASES = ACTIVE_PHASES | {"awaiting_reset", "recovery_required"}
FLASH_SIZE = {2: 0x100000, 4: 0x100000, 5: 0x400000}
PLATFORM = {2: 2, 4: 8, 5: 16}
PLAN_LIFETIME = 600


@dataclass(frozen=True)
class FirmwareRelease:
    id: str
    family: str
    model: str
    version: str
    boards: tuple[int, ...]
    url: str
    archive_size: int
    archive_sha256: str
    member: str
    size: int
    sha256: str
    release_url: str


_STOCK_URL = "https://github.com/greatscottgadgets/hackrf/releases/download/v2026.01.3/hackrf-2026.01.3.tar.xz"
_STOCK_SHA = "d2b76a1115d9b4df648c29efb2f3c8e80009b7cf9a8adf37abbfdba72101b086"
_MAYHEM_URL = "https://github.com/portapack-mayhem/mayhem-firmware/releases/download/v2.4.0/FIRMWARE_mayhem_v2.4.0.zip"
_MAYHEM_SHA = "62cb7f579423ae64414c70548361dd239a4afc7adbd288ae41fbbbfa14be96bd"
CATALOG = {
    release.id: release for release in (
        FirmwareRelease("stock-one-2026.01.3", "stock", "HackRF One", "2026.01.3", (2, 4),
                        _STOCK_URL, 25945024, _STOCK_SHA, "hackrf-2026.01.3/firmware-bin/hackrf_one_usb.bin", 45720,
                        "1d5f36c702677bc47086403c8e8c8a650d47d892a3c791e2dc9a7fa2bab1d72b",
                        "https://github.com/greatscottgadgets/hackrf/releases/tag/v2026.01.3"),
        FirmwareRelease("stock-pro-2026.01.3", "stock", "HackRF Pro", "2026.01.3", (5,),
                        _STOCK_URL, 25945024, _STOCK_SHA, "hackrf-2026.01.3/firmware-bin/hackrf_pro_usb.bin", 240152,
                        "fd2613e7217c201355a8de7688e8306d3035d7eeea4f05122767bc908223822b",
                        "https://github.com/greatscottgadgets/hackrf/releases/tag/v2026.01.3"),
        FirmwareRelease("mayhem-one-2.4.0", "mayhem", "HackRF One + PortaPack", "v2.4.0", (2, 4),
                        _MAYHEM_URL, 8434462, _MAYHEM_SHA, "firmware/firmware_hackrf.bin", 1048576,
                        "5b06461c411f0d8f1f12fc1506fd9d6db5b2cecd9590281ea18dbb960ba13ec2",
                        "https://github.com/portapack-mayhem/mayhem-firmware/releases/tag/v2.4.0"),
        FirmwareRelease("mayhem-pro-2.4.0", "mayhem", "HackRF Pro + PortaPack", "v2.4.0", (5,),
                        _MAYHEM_URL, 8434462, _MAYHEM_SHA, "firmware/firmware_hpro.bin", 4194304,
                        "dcf2b7c3137b36e7fc35fdc942b95667ece3721a93906093db398373fbbff105",
                        "https://github.com/portapack-mayhem/mayhem-firmware/releases/tag/v2.4.0"),
    )
}


def release_for(value: str) -> FirmwareRelease:
    if not isinstance(value, str) or value not in CATALOG:
        raise RadioError("Select a reviewed release from the firmware catalog")
    return CATALOG[value]


def firmware_family(version: str) -> str:
    if re.fullmatch(r"\d{4}\.\d{2}\.\d+", version):
        return "stock"
    if re.fullmatch(r"v?\d{1,2}\.\d+\.\d+", version):
        return "mayhem"
    raise RadioError("Nightly/custom firmware cannot be compared safely; use its upstream update procedure")


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def validate_target(device: Device, release: FirmwareRelease) -> None:
    if device.board_id not in release.boards or not device.supported:
        raise RadioError("Firmware target does not match a healthy supported board")
    if firmware_family(device.firmware) != release.family:
        raise RadioError("Changing between stock and Mayhem firmware is blocked")
    if release.family == "stock" and _version(device.firmware) < (2021, 3, 1):
        raise RadioError("This old firmware may require a CPLD update; follow the official manual upgrade procedure")
    if _version(device.firmware) >= _version(release.version):
        raise RadioError("Device already has this version or a newer version; downgrades and reinstalls are blocked")


def validate_image(data: bytes, release: FirmwareRelease, board_id: int) -> None:
    if len(data) != release.size or hashlib.sha256(data).hexdigest() != release.sha256:
        raise RadioError("Firmware image size or SHA256 does not match the reviewed release")
    if len(data) < 0x430 or len(data) > FLASH_SIZE.get(board_id, 0):
        raise RadioError("Firmware image does not fit this board")
    magic, version, dfu, platforms = struct.unpack_from("<8sHHI", data, 0x400)
    image_version = data[0x410:0x430].split(b"\0", 1)[0].decode("ascii", errors="replace")
    if (magic != b"HACKRFFW" or version != 1 or dfu != 0
            or not platforms & PLATFORM.get(board_id, 0) or image_version != release.version):
        raise RadioError("Firmware header is incompatible, is a DFU image, or reports the wrong version")


def extract_image(raw: bytes, release: FirmwareRelease, board_id: int) -> bytes:
    if len(raw) != release.archive_size or hashlib.sha256(raw).hexdigest() != release.archive_sha256:
        raise RadioError("Downloaded archive size or SHA256 does not match the reviewed release")
    try:
        if release.url.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                matches = [item for item in archive.infolist() if item.filename == release.member]
                if len(matches) != 1 or matches[0].file_size != release.size or (matches[0].external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError
                data = archive.read(matches[0])
        else:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:xz") as archive:
                matches = [item for item in archive.getmembers() if item.name == release.member]
                if len(matches) != 1 or not matches[0].isfile() or matches[0].size != release.size:
                    raise ValueError
                with archive.extractfile(matches[0]) as member:
                    data = member.read(release.size + 1)
        validate_image(data, release, board_id)
        return data
    except (ValueError, KeyError, OSError, tarfile.TarError, zipfile.BadZipFile) as err:
        raise RadioError("Release archive does not contain the expected regular firmware image") from err


async def download_release(release: FirmwareRelease) -> bytes:
    # GitHub release assets redirect to its signed release-assets HTTPS host.
    # No HA/bridge bearer token or other credentials are sent on this session.
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            url = release.url
            for _ in range(4):
                parsed = URL(url)
                if parsed.scheme != "https" or parsed.host not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"} or parsed.user is not None:
                    raise RadioError("Firmware download redirect is outside the official HTTPS asset hosts")
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = str(response.url.join(URL(response.headers["Location"])))
                        continue
                    if response.status != 200:
                        raise RadioError("Official firmware download failed; no device changes were made")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > release.archive_size:
                            raise RadioError("Firmware download exceeded its reviewed size")
                    return bytes(data)
            raise RadioError("Too many firmware download redirects")
    except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
        raise RadioError("Cannot download the official firmware release; no device changes were made") from err


def _durable_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(data, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    # Persist newly created parent-directory entries as well as the receipt.
    for parent in path.parents:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class FirmwareManager:
    def __init__(self, radio, directory: Path) -> None:
        self.radio = radio
        self.directory = Path(directory)
        self._plans: dict[str, dict] = {}
        self._states: dict[str, dict] = {}
        self._jobs: dict[str, asyncio.Task] = {}
        self._preparing = False

    def _journal(self, serial: str) -> Path:
        return self.directory / serial / "state.json"

    async def status(self, serial: str) -> dict:
        serial = validate_serial(serial)
        if serial not in self._states:
            def read():
                try:
                    state = json.loads(self._journal(serial).read_text())
                    if (not isinstance(state, dict) or state.get("serial") != serial
                            or state.get("phase") not in BLOCKING_PHASES | {"failed", "complete"}):
                        raise ValueError
                    if state["phase"] in ACTIVE_PHASES:
                        state = state | {"phase": "recovery_required", "message": "Previous update was interrupted; inspect the saved backup and follow upstream recovery guidance"}
                    return state
                except FileNotFoundError:
                    return {"phase": "idle", "serial": serial}
                except (OSError, ValueError):
                    return {"phase": "recovery_required", "serial": serial, "message": "Firmware journal is unreadable; manual inspection is required"}
            self._states[serial] = await asyncio.to_thread(read)
        return self._states[serial].copy() | {
            "catalog": [{key: value for key, value in asdict(release).items() if key in {"id", "family", "model", "version", "release_url", "boards"}}
                        for release in CATALOG.values()],
            "active": serial in self._jobs,
            "plan": self._plans[serial]["public"].copy() if serial in self._plans else None,
        }

    async def assert_receive_allowed(self, serial: str) -> None:
        state = await self.status(serial)
        if state["phase"] in BLOCKING_PHASES:
            raise RadioBusy("Firmware maintenance blocks receiving; finish verification or follow recovery guidance")

    async def _device(self, serial: str) -> Device:
        text = await self.radio._run(["hackrf_info"])
        tool = re.search(r"hackrf_info version:\s*(\d{4}\.\d{2}\.\d+)", text)
        if not tool or _version(tool[1]) < (2026, 1, 3):
            raise RadioError("Firmware updates require official HackRF host tools 2026.01.3 or newer")
        device = next((device for device in parse_info(text) if device.serial == serial), None)
        if not device:
            raise RadioError("Selected serial is absent; refusing to update another radio")
        return device

    async def prepare(self, serial: str, target: str) -> dict:
        serial, release = validate_serial(serial), release_for(target)
        await self.assert_receive_allowed(serial)
        if self._preparing or self.radio._lock.locked() or self.radio._closed:
            raise RadioBusy("Radio is busy or closed; stop receiving before preparing firmware")
        self._preparing = True
        try:
            async with self.radio._lock:
                await self.radio._assert_managed_access()
                device = await self._device(serial)
                validate_target(device, release)
                help_text = await self.radio._run(["hackrf_spiflash", "-h"])
                if not all(flag in help_text for flag in ("-d", "-r", "-w", "compatibility")):
                    raise RadioError("Flashing tool lacks required serial, backup or compatibility controls")
            raw = await download_release(release)
            image = await asyncio.to_thread(extract_image, raw, release, device.board_id)
            # Keep reviewed bytes in memory until install; never trust a swapped cache file.
            plan = {
                "plan_id": uuid4().hex, "serial": serial, "target": release.id,
                "current_version": device.firmware, "version": release.version,
                "family": release.family, "model": release.model, "size": len(image),
                "sha256": release.sha256, "release_url": release.release_url,
                "warning": device.warning,
                "expires_at": time.time() + PLAN_LIFETIME,
            }
            self._plans[serial] = {"public": plan, "device": device, "image": image}
            return plan.copy()
        finally:
            self._preparing = False

    async def install(self, serial: str, plan_id: str, confirmation: dict) -> dict:
        serial = validate_serial(serial)
        state = await self.status(serial)
        # A response lost after starting may be retried without flashing twice.
        if state.get("plan_id") == plan_id:
            return state
        await self.assert_receive_allowed(serial)
        plan = self._plans.get(serial)
        if (not plan or plan["public"]["plan_id"] != plan_id or plan["public"]["expires_at"] < time.time()):
            raise RadioError("Firmware plan is missing, changed or expired; prepare it again")
        if (not isinstance(confirmation, dict)
                or set(confirmation) != {"serial", "stable_power", "hardware_confirmed", "sdcard_updated"}
                or confirmation["serial"] != serial
                or confirmation["stable_power"] is not True or confirmation["hardware_confirmed"] is not True
                or type(confirmation["sdcard_updated"]) is not bool
                or (plan["public"]["family"] == "mayhem" and confirmation["sdcard_updated"] is not True)):
            raise RadioError("Confirm the exact serial, stable USB power, hardware family and matching Mayhem SD-card preparation")
        if self.radio._lock.locked() or self.radio._closed:
            raise RadioBusy("Radio is busy or closed")
        # Acquire before returning to HTTP/WS so a scan cannot race the job.
        await self.radio._lock.acquire()
        try:
            await self.radio._assert_managed_access()
        except BaseException:
            self.radio._lock.release()
            raise
        self._plans.pop(serial)
        self._states[serial] = {"phase": "backing_up", "serial": serial, "plan_id": plan_id,
                                "target": plan["public"]["target"], "version": plan["public"]["version"],
                                "board_id": plan["device"].board_id,
                                "message": "Checking identity and saving the complete existing SPI flash"}
        self._jobs[serial] = asyncio.create_task(self._install_job(serial, plan))
        return await self.status(serial)

    async def _save(self, serial: str, phase: str, message: str, **values) -> None:
        self._states[serial] = self._states[serial] | {"phase": phase, "message": message} | values
        await asyncio.to_thread(_durable_json, self._journal(serial), self._states[serial])

    async def _write(self, args: list[str]) -> None:
        """Drain bounded logs; deliberately never kill a running flash write."""
        try:
            process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as err:
            raise RadioError("Could not start the flashing tool") from err
        async def consume():
            while await process.stdout.read(65536):
                pass
            return await process.wait()
        task = asyncio.create_task(consume())
        # The background job is not tied to a browser request. If an external
        # shutdown cancels it, wait for the tool rather than interrupting erase/write.
        while True:
            try:
                code = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    task = asyncio.create_task(consume())
                continue
        if code:
            raise RadioError("Flashing tool failed; keep the backup and follow upstream recovery guidance")

    async def _install_job(self, serial: str, plan: dict) -> None:
        mutated = False
        try:
            release = release_for(plan["public"]["target"])
            device = await self._device(serial)
            if device != plan["device"]:
                raise RadioError("Device identity or firmware changed after preparation; refusing to flash")
            validate_target(device, release)
            validate_image(plan["image"], release, device.board_id)
            folder = self.directory / serial / plan["public"]["plan_id"]

            def setup_files():
                folder.mkdir(parents=True, mode=0o700)
                if shutil.disk_usage(folder).free < FLASH_SIZE[device.board_id] * 3 + len(plan["image"]) + 1_000_000:
                    raise RadioError("Insufficient persistent storage for backup and readback")
                image_file = folder / "firmware.bin"
                with image_file.open("xb") as output:
                    os.chmod(image_file, 0o600)
                    output.write(plan["image"])
                    output.flush()
                    os.fsync(output.fileno())
            await asyncio.to_thread(setup_files)
            await self._save(serial, "backing_up", "Reading the complete existing SPI flash")
            backup = folder / "backup.bin"
            await self.radio._run(["hackrf_spiflash", "-d", serial, "-r", str(backup)], timeout=180)

            def validate_backup():
                data = backup.read_bytes()
                if len(data) != FLASH_SIZE[device.board_id]:
                    raise RadioError("Backup is incomplete; no firmware will be written")
                if data == b"\xff" * len(data) or data == b"\0" * len(data):
                    raise RadioError("Backup contains only erased/zero bytes; no firmware will be written")
                os.chmod(backup, 0o600)
                with backup.open("rb") as stream:
                    os.fsync(stream.fileno())
                descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                return hashlib.sha256(data).hexdigest()
            backup_sha = await asyncio.to_thread(validate_backup)
            if await self._device(serial) != device:
                raise RadioError("Device identity or firmware changed during backup; refusing to flash")
            # Persist intent and backup receipt before launching any erase/write.
            await self._save(serial, "writing", "Writing verified firmware. Keep USB power connected; do not restart the host",
                             backup=f"{serial}/{plan['public']['plan_id']}/backup.bin", backup_sha256=backup_sha)
            mutated = True
            await self._write(["hackrf_spiflash", "-d", serial, "-w", str(folder / "firmware.bin")])
            await self._save(serial, "verifying", "Reading back the complete SPI flash and comparing every byte")
            readback = folder / "readback.bin"
            await self.radio._run(["hackrf_spiflash", "-d", serial, "-r", str(readback)], timeout=180)
            expected = plan["image"] + b"\xff" * (FLASH_SIZE[device.board_id] - len(plan["image"]))
            actual = await asyncio.to_thread(readback.read_bytes)
            if actual != expected:
                raise RadioError("SPI flash readback does not match the expected image and erased tail")
            await self._save(serial, "awaiting_reset", "Flash readback verified. Manually reset/reconnect the device, select HackRF mode if needed, then Verify boot",
                             readback_sha256=hashlib.sha256(actual).hexdigest())
        except (Exception, asyncio.CancelledError) as err:
            phase = "recovery_required" if mutated else "failed"
            message = str(err) if isinstance(err, RadioError) else "Firmware operation failed; inspect the saved backup and host logs before continuing"
            try:
                await self._save(serial, phase, message)
            except OSError:
                # The earlier writing journal remains fail-closed after restart.
                self._states[serial] = self._states[serial] | {"phase": phase, "message": message}
        finally:
            self._jobs.pop(serial, None)
            self.radio._lock.release()

    async def verify_boot(self, serial: str) -> dict:
        state = await self.status(serial)
        if state["phase"] != "awaiting_reset" or serial in self._jobs:
            raise RadioError("A verified flash awaiting reset is required before boot verification")
        if self.radio._lock.locked():
            raise RadioBusy("Radio is busy")
        async with self.radio._lock:
            await self.radio._assert_managed_access()
            device = await self._device(serial)
            if not device.supported or device.board_id != state["board_id"] or device.firmware != state["version"]:
                raise RadioError("The selected device has not booted the expected firmware; keep monitoring off and check reset/mode/USB mapping")
            await self._save(serial, "complete", "Firmware boot verified; monitoring remains off")
        return await self.status(serial)

    async def wait(self) -> None:
        while tasks := list(self._jobs.values()):
            for task in tasks:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
