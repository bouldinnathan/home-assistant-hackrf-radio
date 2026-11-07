import asyncio
import hashlib
import io
import json
import os
import struct
import sys
import tarfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.hackrf_radio import firmware as fw
from custom_components.hackrf_radio.bridge import create_app
from custom_components.hackrf_radio.client import BridgeClient
from custom_components.hackrf_radio.radio import LocalRadio, RadioBusy, RadioError, parse_info

from .conftest import HELP, INFO, SERIAL

TARGET = "stock-one-2026.01.3"
CONFIRM = {"serial": SERIAL, "stable_power": True, "hardware_confirmed": True, "sdcard_updated": False}
HOST_INFO = INFO.replace("hackrf_info version: 2024.02.1", "hackrf_info version: 2026.01.3")
FLASH_HELP = "-d --device -r --read -w --write -i --no-check: skip compatibility check"


def make_image(release, platforms=10, dfu=0):
    image = bytearray(b"\xff" * 2048)
    struct.pack_into("<8sHHI", image, 0x400, b"HACKRFFW", 1, dfu, platforms)
    image[0x410:0x430] = release.version.encode().ljust(32, b"\0")
    return bytes(image)


def archive_for(release, image):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        info = tarfile.TarInfo(release.member)
        info.size = len(image)
        archive.addfile(info, io.BytesIO(image))
    raw = buffer.getvalue()
    return raw, replace(release, archive_size=len(raw), archive_sha256=hashlib.sha256(raw).hexdigest())


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Emulates real SPI storage while exercising the production state machine."""
    release = fw.CATALOG[TARGET]
    image = make_image(release)
    release = replace(release, size=len(image), sha256=hashlib.sha256(image).hexdigest())
    archive, release = archive_for(release, image)
    monkeypatch.setitem(fw.CATALOG, TARGET, release)
    monkeypatch.setattr(fw, "download_release", AsyncMock(return_value=archive))
    radio = LocalRadio(firmware_directory=tmp_path / "persistent")
    state = {"info": HOST_INFO, "written": False, "writes": 0, "corrupt": False, "short_backup": False}

    async def run(args, **kwargs):
        if args == ["hackrf_info"]:
            return state["info"]
        if args == ["hackrf_spiflash", "-h"]:
            return FLASH_HELP
        if args == ["hackrf_sweep", "-h"]:
            return HELP
        assert args[:4] == ["hackrf_spiflash", "-d", SERIAL, "-r"]
        path = Path(args[4])
        data = (image + b"\xff" * (0x100000 - len(image))) if state["written"] else b"original".ljust(0x100000, b"\xff")
        if state["corrupt"] and state["written"]:
            data = data[:-1] + b"x"
        if state["short_backup"] and not state["written"]:
            data = data[:100]
        path.write_bytes(data)
        return "ok"

    async def write(args):
        assert args[:4] == ["hackrf_spiflash", "-d", SERIAL, "-w"]
        assert Path(args[4]).read_bytes() == image
        assert "-i" not in args and "-R" not in args
        state["written"] = True
        state["writes"] += 1

    monkeypatch.setattr(radio, "_run", AsyncMock(side_effect=run))
    monkeypatch.setattr(radio._firmware, "_write", AsyncMock(side_effect=write))
    return radio, state, release


async def prepare(radio):
    return await radio.firmware_prepare(SERIAL, TARGET)


async def install(radio, plan):
    result = await radio.firmware_install(SERIAL, plan["plan_id"], CONFIRM)
    assert result["active"]
    await radio._firmware.wait()
    return await radio.firmware_status(SERIAL)


async def test_backup_write_readback_manual_reset_and_boot_verification(rig):
    radio, state, release = rig
    plan = await prepare(radio)
    assert not state["written"]  # Prepare is read-only on hardware.
    result = await install(radio, plan)
    assert result["phase"] == "awaiting_reset"
    backup = radio._firmware.directory / result["backup"]
    assert len(backup.read_bytes()) == 0x100000
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == result["backup_sha256"]
    assert backup.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RadioBusy):
        await radio._firmware.assert_receive_allowed(SERIAL)
    with pytest.raises(RadioError, match="has not booted"):
        await radio.firmware_verify(SERIAL)
    state["info"] = state["info"].replace("Firmware Version: 2024.02.1", "Firmware Version: 2026.01.3")
    assert (await radio.firmware_verify(SERIAL))["phase"] == "complete"
    await radio._firmware.assert_receive_allowed(SERIAL)
    replay = await radio.firmware_install(SERIAL, plan["plan_id"], CONFIRM)
    assert replay["phase"] == "complete"
    assert state["writes"] == 1


@pytest.mark.parametrize("change", [
    {"serial": "f" * 32}, {"stable_power": False}, {"hardware_confirmed": False},
    {"stable_power": 1}, {"extra_args": "-i"},
])
async def test_explicit_review_required_before_any_write(rig, change):
    radio, state, _ = rig
    plan = await prepare(radio)
    with pytest.raises(RadioError, match="Confirm"):
        await radio.firmware_install(SERIAL, plan["plan_id"], CONFIRM | change)
    assert state["writes"] == 0


async def test_expired_plans_and_arbitrary_targets_rejected(rig):
    radio, state, _ = rig
    with pytest.raises(RadioError, match="reviewed"):
        await radio.firmware_prepare(SERIAL, "https://attacker/firmware.bin")
    plan = await prepare(radio)
    radio._firmware._plans[SERIAL]["public"]["expires_at"] = time.time() - 1
    with pytest.raises(RadioError, match="expired"):
        await radio.firmware_install(SERIAL, plan["plan_id"], CONFIRM)
    assert not state["written"]


@pytest.mark.parametrize("changed", [
    HOST_INFO.replace("Firmware Version: 2024.02.1", "Firmware Version: 2023.01.1"),
    HOST_INFO.replace(SERIAL, "f" * 32),
    HOST_INFO.replace("Hardware Revision: r10", "Hardware Revision: r9"),
])
async def test_identity_rechecked_at_install(rig, changed):
    radio, state, _ = rig
    plan = await prepare(radio)
    state["info"] = changed
    result = await install(radio, plan)
    assert result["phase"] == "failed"
    assert state["writes"] == 0


async def test_incomplete_backup_prevents_erasing(rig):
    radio, state, _ = rig
    plan = await prepare(radio)
    state["short_backup"] = True
    assert (await install(radio, plan))["phase"] == "failed"
    assert state["writes"] == 0


async def test_mayhem_requires_explicit_sdcard_confirmation(rig):
    radio, state, _ = rig
    plan = await prepare(radio)
    radio._firmware._plans[SERIAL]["public"]["family"] = "mayhem"
    with pytest.raises(RadioError, match="SD-card"):
        await radio.firmware_install(SERIAL, plan["plan_id"], CONFIRM)
    assert not state["written"]


async def test_low_persistent_storage_blocks_flash(rig, monkeypatch):
    radio, state, _ = rig
    plan = await prepare(radio)
    monkeypatch.setattr(fw.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 0})())
    result = await install(radio, plan)
    assert result["phase"] == "failed"
    assert not state["written"]


async def test_flash_failure_never_retries_or_resumes_receiving(rig, monkeypatch):
    radio, state, _ = rig
    plan = await prepare(radio)
    monkeypatch.setattr(radio._firmware, "_write", AsyncMock(side_effect=RadioError("USB disconnected during flash")))
    result = await install(radio, plan)
    assert result["phase"] == "recovery_required"
    with pytest.raises(RadioBusy):
        await prepare(radio)
    assert (await radio.firmware_install(SERIAL, plan["plan_id"], CONFIRM))["phase"] == "recovery_required"
    radio._firmware._write.assert_awaited_once()


async def test_readback_checks_erased_tail_and_survives_restart(rig):
    radio, state, _ = rig
    plan = await prepare(radio)
    state["corrupt"] = True
    result = await install(radio, plan)
    assert result["phase"] == "recovery_required"
    restarted = fw.FirmwareManager(radio, radio._firmware.directory)
    assert (await restarted.status(SERIAL))["phase"] == "recovery_required"
    with pytest.raises(RadioBusy):
        await restarted.assert_receive_allowed(SERIAL)


async def test_pending_boot_can_be_verified_after_restart(rig):
    radio, state, _ = rig
    result = await install(radio, await prepare(radio))
    assert result["phase"] == "awaiting_reset"
    restarted = fw.FirmwareManager(radio, radio._firmware.directory)
    state["info"] = state["info"].replace("Firmware Version: 2024.02.1", "Firmware Version: 2026.01.3")
    assert (await restarted.verify_boot(SERIAL))["phase"] == "complete"


@pytest.mark.parametrize("phase", ["writing", "verifying", "backing_up"])
async def test_interrupted_journal_is_fail_closed(rig, phase):
    radio, _, _ = rig
    path = radio._firmware._journal(SERIAL)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"serial": SERIAL, "phase": phase}))
    assert (await radio.firmware_status(SERIAL))["phase"] == "recovery_required"


async def test_cannot_scan_while_job_holds_usb_lock(rig, monkeypatch):
    radio, _, _ = rig
    started, release = asyncio.Event(), asyncio.Event()

    async def block(args):
        started.set()
        await release.wait()

    monkeypatch.setattr(radio._firmware, "_write", block)
    plan = await prepare(radio)
    await radio.firmware_install(SERIAL, plan["plan_id"], CONFIRM)
    await started.wait()
    assert radio._lock.locked()
    with pytest.raises(RadioBusy):
        await radio.sweep(SERIAL, {})
    await radio.stop()  # Receive stop does not cancel the flash job.
    assert (await radio.firmware_status(SERIAL))["active"]
    release.set()
    await radio._firmware.wait()


@pytest.mark.parametrize(("target", "board", "current"), [
    ("stock-one-2026.01.3", 2, "2024.02.1"), ("stock-one-2026.01.3", 4, "2023.01.1"),
    ("stock-pro-2026.01.3", 5, "2025.07.2"),
    ("mayhem-one-2.4.0", 2, "v2.3.2"), ("mayhem-pro-2.4.0", 5, "v2.3.2"),
])
def test_supported_family_and_board_variants(target, board, current):
    device = replace(parse_info(INFO)[0], board_id=board, firmware=current)
    fw.validate_target(device, fw.CATALOG[target])


@pytest.mark.parametrize(("target", "current", "board"), [
    (TARGET, "n_250606", 2), (TARGET, "v2.3.2", 2), (TARGET, "2026.01.3", 2),
    (TARGET, "2027.01.1", 2), (TARGET, "2018.01.1", 2), (TARGET, "2024.02.1", 5),
    ("mayhem-one-2.4.0", "2024.02.1", 2), ("mayhem-one-2.4.0", "v2.4.0", 2),
])
def test_cross_family_unknown_downgrade_old_cpld_and_wrong_board_blocked(target, current, board):
    with pytest.raises(RadioError):
        fw.validate_target(replace(parse_info(INFO)[0], firmware=current, board_id=board), fw.CATALOG[target])


@pytest.mark.parametrize(("platforms", "dfu"), [(16, 0), (10, 1)])
def test_image_header_checks_are_independent_of_hash(rig, platforms, dfu):
    _, _, release = rig
    image = make_image(release, platforms=platforms, dfu=dfu)
    release = replace(release, sha256=hashlib.sha256(image).hexdigest())
    with pytest.raises(RadioError, match="header"):
        fw.validate_image(image, release, 2)


async def test_checksum_corruption_stops_before_flash(rig, monkeypatch):
    radio, state, _ = rig
    monkeypatch.setattr(fw, "download_release", AsyncMock(return_value=b"corrupt archive"))
    with pytest.raises(RadioError, match="SHA256"):
        await prepare(radio)
    assert not state["written"]


async def test_write_subprocess_is_not_killed_by_cancellation(rig, tmp_path, monkeypatch):
    radio, _, _ = rig
    # Exercise the real protected subprocess path, using a non-hardware script.
    pidfile = tmp_path / "pid"
    script = tmp_path / "fake-flash.py"
    script.write_text(f"import os,time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(.3)\nprint('done')\n")
    task = asyncio.create_task(fw.FirmwareManager._write(radio._firmware, [sys.executable, str(script)]))
    async with asyncio.timeout(3):
        while not pidfile.exists():
            await asyncio.sleep(.01)
    task.cancel()
    await asyncio.wait_for(asyncio.shield(task), 3)
    assert not task.cancelled()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.usefixtures("socket_enabled")
async def test_authenticated_bridge_firmware_end_to_end(aiohttp_client, rig):
    radio, state, _ = rig
    token = "test-firmware-token-at-least-32-characters"
    http = await aiohttp_client(create_app(token, radio))
    assert (await http.post("/v1/firmware/install", json={})).status == 401
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())) as session:
        client = BridgeClient(session, str(http.make_url("/")), token)
        plan = await client.firmware_prepare(SERIAL, TARGET)
        assert not state["written"]
        await client.firmware_install(SERIAL, plan["plan_id"], CONFIRM)
        await radio._firmware.wait()
        assert (await client.firmware_status(SERIAL))["phase"] == "awaiting_reset"
        state["info"] = state["info"].replace("Firmware Version: 2024.02.1", "Firmware Version: 2026.01.3")
        assert (await client.firmware_verify(SERIAL))["phase"] == "complete"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_real_ha_firmware_panel_workflow(hass, hass_ws_client, rig):
    from unittest.mock import patch

    from custom_components.hackrf_radio.const import DEFAULT_SETTINGS, DOMAIN
    from tests.ha_helpers import MockConfigEntry

    radio, state, _ = rig
    entry = MockConfigEntry(domain=DOMAIN, title="Firmware bench", unique_id=SERIAL,
                            data={"transport": "local", "serial": SERIAL}, options=DEFAULT_SETTINGS)
    entry.add_to_hass(hass)
    with patch("custom_components.hackrf_radio.client.create_client", return_value=radio):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        ws = await hass_ws_client(hass)
        await ws.send_json({"id": 1, "type": "hackrf_radio/firmware", "entry_id": entry.entry_id, "action": "prepare", "target": TARGET})
        result = await ws.receive_json()
        assert result["success"]
        plan = result["result"]["firmware"]["plan"]
        assert state["writes"] == 0
        await ws.send_json({"id": 2, "type": "hackrf_radio/firmware", "entry_id": entry.entry_id,
                            "action": "install", "plan_id": plan["plan_id"], "confirmation": CONFIRM})
        assert (await ws.receive_json())["success"]
        await radio._firmware.wait()
        assert (await radio.firmware_status(SERIAL))["phase"] == "awaiting_reset"
        state["info"] = state["info"].replace("Firmware Version: 2024.02.1", "Firmware Version: 2026.01.3")
        await ws.send_json({"id": 3, "type": "hackrf_radio/firmware", "entry_id": entry.entry_id, "action": "verify"})
        result = await ws.receive_json()
        assert result["success"]
        assert result["result"]["firmware"]["phase"] == "complete"
        assert not result["result"]["monitoring"]
        assert await hass.config_entries.async_unload(entry.entry_id)
