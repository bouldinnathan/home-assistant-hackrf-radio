"""Regression coverage for first-run auth, recovery, shared ownership and logs."""

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE

from custom_components.hackrf_radio.activity import ActivityLog
from custom_components.hackrf_radio.bridge import create_app
from custom_components.hackrf_radio.client import BridgeClient, create_client
from custom_components.hackrf_radio.const import DEFAULT_SETTINGS, DOMAIN
from custom_components.hackrf_radio.radio import AuthenticationError, LocalRadio, RadioError
from custom_components.hackrf_radio.websocket_api import async_read_activity
from tests.ha_helpers import MockConfigEntry

from .conftest import HELP, INFO, SERIAL

ADMIN = "administrative-token-with-at-least-thirty-two-characters"
RECEIVE = "separate-receive-token-with-at-least-thirty-two-characters"
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def start_bridge_flow(hass, client, token=ADMIN):
    with patch("custom_components.hackrf_radio.config_flow.create_client", return_value=client):
        flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"next_step_id": "bridge"})
        return await hass.config_entries.flow.async_configure(flow["flow_id"], {"url": "http://bridge:8099", "token": token})


@pytest.mark.usefixtures("socket_enabled")
async def test_receive_token_is_rejected_before_any_usb_probe(hass, aiohttp_client, tmp_path):
    radio = LocalRadio(application_directory=tmp_path / "apps")
    radio._run = AsyncMock()
    http = await aiohttp_client(create_app(ADMIN, radio, receive_token=RECEIVE))
    client = BridgeClient(http.session, str(http.make_url("")), RECEIVE)
    with pytest.raises(AuthenticationError):
        await client.setup_status()
    flow = await start_bridge_flow(hass, client, RECEIVE)
    assert flow["step_id"] == "bridge" and flow["errors"] == {"base": "invalid_auth"}
    radio._run.assert_not_awaited()
    journal = hass.data[DOMAIN]["activity_readers"]["setup"]
    assert RECEIVE not in json.dumps(await journal.read())


@pytest.mark.usefixtures("socket_enabled")
async def test_bridge_handoff_recovery_requires_confirmation_then_validates_device(hass, aiohttp_client, tmp_path):
    radio = LocalRadio(application_directory=tmp_path / "apps")
    radio._run = AsyncMock(side_effect=lambda args, **kw: HELP if args[-1] == "-h" else INFO)
    await radio.application_action(SERIAL, "handoff", {"app_id": "external", "confirmed": True})
    radio._run.reset_mock()
    async def setup_status():
        return {"applications": await radio.application_status(), "tools": {}, "atomic_recovery": True}
    radio.setup_status = setup_status
    http = await aiohttp_client(create_app(ADMIN, radio))
    client = BridgeClient(http.session, str(http.make_url("")), ADMIN)
    flow = await start_bridge_flow(hass, client)
    assert flow["step_id"] == "recover"
    radio._run.assert_not_awaited()
    with patch("custom_components.hackrf_radio.config_flow.create_client", return_value=client):
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"serial_confirmation": SERIAL, "confirmed_closed": False})
        assert flow["errors"]["base"] == "recovery_failed"
        assert (await radio.application_status())["owner"]["phase"] == "external"
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"serial_confirmation": SERIAL, "confirmed_closed": True})
        assert flow["step_id"] == "device"
        assert (await radio.application_status())["owner"]["phase"] == "home_assistant"
    assert all(call.args[0][0] == "hackrf_info" or call.args[0] == ["hackrf_sweep", "-h"] for call in radio._run.call_args_list)


async def test_changed_handoff_lease_cannot_stop_a_new_program(hass):
    owner = {"phase": "external", "serial": SERIAL, "lease_id": "a" * 32}
    client = AsyncMock(setup_status=AsyncMock(return_value={"applications": {"owner": owner}, "tools": {}}))
    flow = await start_bridge_flow(hass, client)
    assert flow["step_id"] == "recover"
    client.setup_status.return_value = {"applications": {"owner": owner | {"lease_id": "b" * 32}}, "tools": {}}
    with patch("custom_components.hackrf_radio.config_flow.create_client", return_value=client):
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"serial_confirmation": SERIAL, "confirmed_closed": True, "stop_managed_program": True})
    assert flow["errors"]["base"] == "recovery_failed"
    client.application_action.assert_not_awaited()


@pytest.mark.parametrize("access", ["offline", "usb"])
async def test_local_discovery_and_other_client_close_preserve_program_until_host_stop(hass, access):
    profiles = Path(hass.config.path("hackrf_radio_programs.json"))
    profiles.parent.mkdir(exist_ok=True, parents=True)
    profiles.write_text(json.dumps([{"id": "user_check", "name": "Running program", "access": access,
                                    "argv": [sys.executable, "-c", "import time; time.sleep(60)"]}]))
    owner = create_client(hass, {"transport": "local"})
    probe = create_client(hass, {"transport": "local"})
    owner._run = AsyncMock(side_effect=lambda args, **kw: HELP if args[-1] == "-h" else INFO)
    try:
        with patch("os.geteuid", return_value=1000):
            await owner.application_action(SERIAL, "launch", {"app_id": "user_check", "confirmed": True})
        process = owner._applications._process
        probe._run = AsyncMock(side_effect=lambda args, **kw: HELP if args[-1] == "-h" else INFO)
        if access == "usb":
            with pytest.raises(RadioError):
                await probe.devices()
        else:
            assert (await probe.devices())[0]["serial"] == SERIAL
        await probe.close()
        await owner.close()
        assert process.returncode is None
        await hass.async_stop()
        assert process.returncode is not None
    finally:
        await owner._applications.close()


async def test_active_log_reader_shows_write_failure_and_recovery(hass, tmp_path):
    entry = MockConfigEntry(domain=DOMAIN, data={"transport": "local", "serial": SERIAL})
    entry.add_to_hass(hass)
    journal = ActivityLog(tmp_path / "activity.sqlite")
    entry.runtime_data = SimpleNamespace(activity=journal)
    await journal.record("test.initial", "Existing readable record")
    with patch.object(journal, "_connect", side_effect=sqlite3.OperationalError("full disk")):
        await journal.record("test.lost", "Cannot save this record")
    state = await async_read_activity(hass, entry.entry_id)
    assert state["records"] and state["storage_error"]
    await journal.record("test.recovered", "Write access restored")
    assert (await async_read_activity(hass, entry.entry_id))["storage_error"] is None


async def test_invalid_program_config_does_not_block_basic_radio_status_or_discovery(tmp_path):
    profiles = tmp_path / "programs.json"
    profiles.write_text("{invalid json")
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profiles)
    radio._run = AsyncMock(side_effect=lambda args, **kw: HELP if args[-1] == "-h" else INFO)
    state = await radio.application_status()
    assert state["profile_error"]
    assert not any(app["id"].startswith("user_") for app in state["catalog"])
    assert (await radio.devices())[0]["serial"] == SERIAL
    with pytest.raises(RadioError, match="profiles"):
        await radio.application_action(SERIAL, "launch", {"app_id": "user_check", "confirmed": True})
    await radio.close()


async def test_reconfigure_preserves_serial_options_and_checks_identity(hass, device):
    entry = MockConfigEntry(domain=DOMAIN, title="Workbench", unique_id=SERIAL, data={"transport": "bridge", "serial": SERIAL, "url": "http://old:8099", "token": ADMIN}, options=DEFAULT_SETTINGS)
    entry.add_to_hass(hass)
    client = AsyncMock(setup_status=AsyncMock(return_value={"applications": {"owner": {"phase": "home_assistant"}}, "tools": {}}), devices=AsyncMock(return_value=[device | {"serial": "f" * 32}]))
    with patch("custom_components.hackrf_radio.config_flow.create_client", return_value=client), patch("homeassistant.config_entries.ConfigEntries.async_reload", new=AsyncMock(return_value=True)):
        flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id})
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"url": "http://new:8099", "token": ADMIN})
        assert flow["errors"] == {"base": "cannot_connect"}
        assert entry.data["url"] == "http://old:8099"
        client.devices.return_value = [device]
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"url": "http://new:8099", "token": ADMIN})
        assert flow["reason"] == "reconfigure_successful"
        assert entry.data["url"] == "http://new:8099" and entry.unique_id == SERIAL
        assert entry.options == DEFAULT_SETTINGS
        await hass.async_block_till_done()


async def test_warning_history_survives_a_debug_output_flood(tmp_path):
    journal = ActivityLog(tmp_path / "activity.sqlite", retention=20)
    await journal.record("test.important", "Keep this hardware warning", level="warning")
    for index in range(60):
        await journal.record("test.output", str(index), level="debug")
    state = await journal.read()
    assert state["retention"] == 25
    assert any(record["event"] == "test.important" for record in state["records"])
    assert len([record for record in state["records"] if record["level"] == "debug"]) == 20


@pytest.mark.parametrize("state", [None, {}, {"applications": []},
    {"applications": {"owner": {"phase": []}}},
    {"applications": {"owner": {"phase": "external", "serial": SERIAL}}},
    {"applications": {"owner": {"phase": "home_assistant"}}, "tools": []},
    {"applications": {"owner": {"phase": "home_assistant"}, "installer": {"phase": []}}},
])
async def test_setup_rejects_malformed_remote_capabilities_without_crashing(state):
    client = BridgeClient(AsyncMock(), "http://bridge:8099", ADMIN)
    with patch.object(client, "_request", new=AsyncMock(return_value={"setup": state})), pytest.raises(RadioError):
        await client.setup_status()
