import asyncio
import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from custom_components.hackrf_radio import control, software
from custom_components.hackrf_radio.bridge import create_app
from custom_components.hackrf_radio.client import BridgeClient
from custom_components.hackrf_radio.radio import LocalRadio, RadioBusy, RadioError

from .conftest import CSV, HELP, INFO, SERIAL

TOKEN = "administrative-test-token-at-least-32-characters"
RECEIVE_TOKEN = "separate-receive-test-token-at-least-32-characters"
TRANSFER_HELP = "-d serial -r file -n samples -f frequency -s rate -a amp -p bias -l lna -g vga"


@pytest.fixture
def radio(tmp_path, monkeypatch):
    radio = LocalRadio(firmware_directory=tmp_path / "firmware")

    async def run(args, **kwargs):
        if args == ["hackrf_info"]:
            return INFO
        if args == ["hackrf_sweep", "-h"]:
            return HELP
        if args == ["hackrf_transfer", "-h"]:
            return TRANSFER_HELP
        if args[0] == "hackrf_transfer":
            assert args[args.index("-a") + 1] == args[args.index("-p") + 1] == "0"
            assert "-t" not in args and "-w" not in args and "-R" not in args
            assert args[args.index("-d") + 1] == SERIAL
            count = int(args[args.index("-n") + 1])
            Path(args[args.index("-r") + 1]).write_bytes(b"\x01\xff" * count)
            return "captured"
        if args[0] == "hackrf_sweep":
            return CSV
        raise AssertionError(args)

    monkeypatch.setattr(radio, "_run", AsyncMock(side_effect=run))
    return radio


async def test_persistent_usb_handoff_blocks_all_ha_probes_and_requires_return(radio):
    state = await radio.application_action(SERIAL, "handoff", {"app_id": "gqrx", "confirmed": True})
    assert state["owner"]["serial"] == SERIAL
    assert state["owner"]["phase"] == "external"
    radio._run.reset_mock()
    for operation in [radio.devices(), radio.sweep(SERIAL, {}), radio.direct_capture(SERIAL, {})]:
        with pytest.raises(RadioBusy):
            await operation
    radio._run.assert_not_awaited()
    restarted = LocalRadio(firmware_directory=radio._firmware.directory)
    restarted._run = radio._run
    with pytest.raises(RadioBusy):
        await restarted.devices()
    with pytest.raises(RadioError, match="confirm"):
        await restarted.application_action(SERIAL, "release", {"confirmed_closed": False})
    with pytest.raises(RadioError, match="Only the radio"):
        await restarted.application_action("f" * 32, "release", {"confirmed_closed": True})
    assert (await restarted.application_action(SERIAL, "release", {"confirmed_closed": True}))["owner"]["phase"] == "home_assistant"
    assert (await restarted.devices())[0]["serial"] == SERIAL


async def test_failed_usb_return_preserves_ownership(radio):
    await radio.application_action(SERIAL, "handoff", {"app_id": "external", "confirmed": True})
    radio._run.side_effect = RadioError("Another app still owns USB")
    with pytest.raises(RadioError):
        await radio.application_action(SERIAL, "release", {"confirmed_closed": True})
    assert (await radio.application_status())["owner"]["phase"] == "external"


async def test_firmware_operations_are_blocked_before_any_probe_during_handoff(radio):
    await radio.application_action(SERIAL, "handoff", {"app_id": "external", "confirmed": True})
    radio._run.reset_mock()
    with pytest.raises(RadioBusy):
        await radio.firmware_prepare(SERIAL, "stock-one-2026.01.3")
    radio._run.assert_not_awaited()


async def test_handoff_rejects_unconfirmed_unknown_app_and_firmware_lock(radio):
    for app, confirmed in [("gqrx", False), ("gqrx", 1), ("/bin/sh", True)]:
        with pytest.raises(RadioError):
            await radio.application_action(SERIAL, "handoff", {"app_id": app, "confirmed": confirmed})
    radio._run.assert_not_awaited()
    radio._firmware._states[SERIAL] = {"phase": "awaiting_reset", "serial": SERIAL}
    with pytest.raises(RadioBusy):
        await radio.application_action(SERIAL, "handoff", {"app_id": "gqrx", "confirmed": True})
    radio._run.assert_not_awaited()


@pytest.mark.parametrize("values", [
    {"transmit": True}, {"amp": 1}, {"antenna_bias": True}, {"args": "-t /tmp/file"},
    {"frequency_hz": 1_000_000}, {"frequency_hz": 6_000_000_000},
    {"samples": 0}, {"samples": 4_000_001}, {"samples": True}, {"sample_rate": 2_000_000},
    {"lna_gain": 7}, {"vga_gain": 63}, {"frequency_hz": float("nan")},
])
def test_capture_rejects_unsafe_or_unbounded_controls(values):
    with pytest.raises(RadioError):
        control.capture_settings(values)


@pytest.mark.parametrize("value", ["0.0.0.0", "8.8.8.8", "::", "example.com", "127.0.0.1;sh"])
def test_soapy_bind_rejects_public_wildcard_and_command_fragments(value):
    with pytest.raises(RadioError):
        software.soapy_bind_address(value)


def test_soapy_bind_accepts_explicit_private_addresses():
    for address in ["127.0.0.1", "10.8.0.10", "172.16.0.8", "192.168.1.2"]:
        assert software.soapy_bind_address(address) == address


async def test_raw_iq_capture_metadata_integrity_and_serial_binding(radio):
    result = await radio.direct_capture(SERIAL, {"samples": 2048})
    assert result["format"] == "ci8" and result["byte_count"] == 4096
    assert not result["rf_amp"] and not result["antenna_bias"]
    metadata, raw = await radio.capture_data(SERIAL, result["capture_id"])
    assert hashlib.sha256(raw).hexdigest() == metadata["sha256"]
    assert raw == b"\x01\xff" * 2048
    for serial, identifier in [("f" * 32, result["capture_id"]), (SERIAL, "../owner.json")]:
        with pytest.raises(RadioError):
            await radio.capture_data(serial, identifier)
    path = radio._applications.directory / "captures" / f"{result['capture_id']}.cs8"
    path.write_bytes(raw[:-2] + b"\0\0")
    with pytest.raises(RadioError, match="integrity"):
        await radio.capture_data(SERIAL, result["capture_id"])


async def test_only_four_captures_retained(radio):
    results = [await radio.direct_capture(SERIAL, {"samples": 1024}) for _ in range(5)]
    with pytest.raises(RadioError, match="expired"):
        await radio.capture_data(SERIAL, results[0]["capture_id"])
    assert len((await radio.capture_data(SERIAL, results[-1]["capture_id"]))[1]) == 2048


async def test_incomplete_capture_removed(radio):
    original = radio._run.side_effect

    async def short(args, **kwargs):
        result = await original(args, **kwargs)
        if args[0] == "hackrf_transfer" and "-r" in args:
            Path(args[args.index("-r") + 1]).write_bytes(b"short")
        return result

    radio._run.side_effect = short
    with pytest.raises(RadioError, match="incomplete"):
        await radio.direct_capture(SERIAL, {})
    assert not list((radio._applications.directory / "captures").glob("*"))


@pytest.mark.parametrize("apps", [["not-an-app"], ["sdrsharp"], ["gqrx;touch /tmp/bad"], [], ["gqrx", "gqrx"], [{}]])
def test_installer_only_accepts_reviewed_package_recipes(apps):
    with pytest.raises(RadioError):
        software.apt_commands(apps)


async def test_install_plan_is_read_only_and_requires_host_opt_in(radio):
    with patch("asyncio.create_subprocess_exec") as spawn:
        result = await radio.application_action(SERIAL, "prepare_install", {"apps": ["gqrx", "gnuradio", "inspectrum"]})
        assert result["plan"]["packages"] == ["gnuradio", "gqrx-sdr", "gr-osmosdr", "inspectrum", "soapysdr-module-hackrf"]
        spawn.assert_not_called()
    with pytest.raises(RadioError, match="allow-software-install"):
        await radio.application_action(SERIAL, "install", {"plan_id": result["plan"]["plan_id"], "confirmed": True})


async def test_package_job_owns_host_and_survives_request_completion(radio, monkeypatch):
    manager = radio._applications
    manager.allow_install = True
    monkeypatch.setattr(software, "APT_EXECUTABLE", sys.executable)
    monkeypatch.setattr(software.os, "geteuid", lambda: 0)
    started, release = asyncio.Event(), asyncio.Event()

    async def install(commands, activity=None):
        started.set()
        await release.wait()

    monkeypatch.setattr(software, "run_installer", install)
    plan = (await manager.prepare_install(["inspectrum"]))["plan"]
    assert (await manager.install(radio, plan["plan_id"], True))["installer"]["phase"] == "installing"
    await started.wait()
    with pytest.raises(RadioBusy):
        await radio.devices()
    release.set()
    await manager._install_job
    assert (await manager.status())["installer"]["phase"] == "complete"
    assert not radio._lock.locked()


async def test_package_service_start_policy_and_soapy_masks(tmp_path, monkeypatch):
    policy = tmp_path / "policy-rc.d"
    units = tmp_path / "units"
    monkeypatch.setattr(software, "SERVICE_POLICY", policy)
    monkeypatch.setattr(software, "UNIT_DIRECTORY", units)

    async def packages(commands, environment, activity=None):
        assert "exit 101" in policy.read_text()
        assert environment["DEBIAN_FRONTEND"] == "noninteractive"
        assert (units / "soapyremote-server.service").readlink() == Path("/dev/null")

    monkeypatch.setattr(software, "_run_packages", packages)
    await software.run_installer(software.apt_commands(["soapy_remote"]))
    assert not policy.exists()
    assert (units / "soapyremote-server.service").is_symlink()
    policy.write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(RadioError, match="existing service-start policy"):
        await software.run_installer(software.apt_commands(["soapy_remote"]))
    assert policy.read_text().endswith("exit 0\n")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "http://u:p@host/", "https://host/?token=secret", "https://host/#token"])
def test_app_links_reject_executable_schemes_and_credentials(url):
    with pytest.raises(RadioError):
        software.link_url(url)


async def test_web_link_persists_without_probe(radio):
    await radio.application_action(SERIAL, "link", {"app_id": "sdrangel", "url": "http://192.168.1.20:8091/"})
    radio._run.assert_not_awaited()
    manager = software.Applications(radio._lock, radio._applications.directory)
    app = next(app for app in (await manager.status())["catalog"] if app["id"] == "sdrangel")
    assert app["web_url"] == "http://192.168.1.20:8091/"


async def test_launch_is_opt_in_and_fixed_loopback_command(radio, monkeypatch):
    with pytest.raises(RadioError, match="disabled"):
        await radio.application_action(SERIAL, "launch", {"app_id": "soapy_remote", "confirmed": True})
    radio._applications.allow_launch = True
    monkeypatch.setattr(software.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("HACKRF_BRIDGE_TOKEN", "do-not-inherit-this-secret")
    process = type("Child", (), {"returncode": None})()
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
        result = await radio.application_action(SERIAL, "launch", {"app_id": "soapy_remote", "confirmed": True})
    assert result["running"]
    assert spawn.call_args.args == ("/usr/bin/SoapySDRServer", "--bind=127.0.0.1:55132")
    assert "HACKRF_BRIDGE_TOKEN" not in spawn.call_args.kwargs["env"]
    assert spawn.call_args.kwargs["start_new_session"]
    radio._applications._process = None


@pytest.mark.usefixtures("socket_enabled")
async def test_receive_token_capture_download_and_admin_boundary(aiohttp_client, radio):
    http = await aiohttp_client(create_app(TOKEN, radio, receive_token=RECEIVE_TOKEN))
    auth = {"Authorization": f"Bearer {RECEIVE_TOKEN}"}
    assert (await http.post("/v1/capture", json={})).status == 401
    for path in ["/v1/applications/install", "/v1/applications/handoff", "/v1/firmware/install"]:
        assert (await http.post(path, json={}, headers=auth)).status == 403
    result = await http.post("/v1/capture", headers=auth, json={"serial": SERIAL, "settings": {"samples": 1024}, "operation_id": "a" * 32})
    assert result.status == 200
    capture = (await result.json())["capture"]
    response = await http.get(f"/v1/captures/{SERIAL}/{capture['capture_id']}", headers=auth)
    assert await response.read() == b"\x01\xff" * 1024
    admin = {"Authorization": f"Bearer {TOKEN}"}
    assert (await http.post("/v1/applications/handoff", headers=admin, json={"serial": SERIAL, "app_id": "external", "confirmed": True})).status == 200
    assert (await http.get("/v1/devices", headers=auth)).status == 409


@pytest.mark.usefixtures("socket_enabled")
async def test_bridge_client_direct_capture_and_app_round_trip(aiohttp_client, radio):
    http = await aiohttp_client(create_app(TOKEN, radio))
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())) as session:
        client = BridgeClient(session, str(http.make_url("/")), TOKEN)
        assert len((await client.application_status())["catalog"]) >= 17
        result = await client.direct_capture(SERIAL, {"samples": 1024})
        assert len((await client.capture_data(SERIAL, result["capture_id"]))[1]) == 2048
        assert (await client.application_action(SERIAL, "handoff", {"app_id": "gqrx", "confirmed": True}))["owner"]["phase"] == "external"
        assert (await client.application_action(SERIAL, "release", {"confirmed_closed": True}))["owner"]["phase"] == "home_assistant"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_ha_capture_download_and_external_pause(hass, hass_ws_client, hass_client, radio):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.hackrf_radio.const import DEFAULT_SETTINGS, DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, title="App bench", unique_id=SERIAL,
                            data={"transport": "local", "serial": SERIAL}, options=DEFAULT_SETTINGS)
    entry.add_to_hass(hass)
    with patch("custom_components.hackrf_radio.client.create_client", return_value=radio):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        ws = await hass_ws_client(hass)
        await ws.send_json({"id": 1, "type": "hackrf_radio/capture", "entry_id": entry.entry_id, "settings": {"samples": 1024}})
        result = await ws.receive_json()
        assert result["success"]
        identifier = result["result"]["capture"]["capture_id"]
        http = await hass_client()
        response = await http.get(f"/api/hackrf_radio/{entry.entry_id}/captures/{identifier}")
        assert response.status == 200
        assert await response.read() == b"\x01\xff" * 1024
        await ws.send_json({"id": 2, "type": "hackrf_radio/applications", "entry_id": entry.entry_id,
                            "action": "handoff", "values": {"app_id": "external", "confirmed": True}})
        assert (await ws.receive_json())["success"]
        radio._run.reset_mock()
        await entry.runtime_data.async_refresh()
        assert not entry.runtime_data.monitoring
        radio._run.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert (await radio.application_status())["owner"]["phase"] == "external"


async def test_capture_http_view_requires_admin(hass):
    from aiohttp import web
    from homeassistant.components.http import KEY_HASS_USER

    from custom_components.hackrf_radio.http_api import CaptureView

    with pytest.raises(web.HTTPForbidden):
        await CaptureView().get({KEY_HASS_USER: type("User", (), {"is_admin": False})()}, "entry", "capture")


async def test_native_capture_cancellation_reaps_process_and_removes_partial_file(tmp_path, monkeypatch):
    script = f'''#!{sys.executable}
import os,sys,time
from pathlib import Path
name=Path(sys.argv[0]).name
if name=="hackrf_info": print({INFO!r})
elif "-h" in sys.argv: print({(HELP + " " + TRANSFER_HELP)!r})
else:
    Path(os.environ["HACKRF_TEST_CAPTURE_PID"]).write_text(str(os.getpid()))
    Path(sys.argv[sys.argv.index("-r")+1]).write_bytes(b"partial")
    time.sleep(60)
'''
    for name in ["hackrf_info", "hackrf_sweep", "hackrf_transfer"]:
        path = tmp_path / name
        path.write_text(script)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("HACKRF_TEST_CAPTURE_PID", str(pidfile))
    radio = LocalRadio(application_directory=tmp_path / "apps")
    task = asyncio.create_task(radio.direct_capture(SERIAL, {}))
    async with asyncio.timeout(3):
        while not pidfile.exists():
            await asyncio.sleep(.01)
    await asyncio.wait_for(radio.stop(), 5)
    assert task.cancelled()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert not list((tmp_path / "apps/captures").glob("*"))
    await radio.close()
