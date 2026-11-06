import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import Unauthorized
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hackrf_radio import async_setup_entry, async_unload_entry
from custom_components.hackrf_radio.const import DEFAULT_SETTINGS, DOMAIN
from custom_components.hackrf_radio.coordinator import HackRFCoordinator
from custom_components.hackrf_radio.diagnostics import async_get_config_entry_diagnostics
from custom_components.hackrf_radio.radio import RadioError
from custom_components.hackrf_radio.sensor import HackRFSensor
from custom_components.hackrf_radio.websocket_api import control_radio, firmware_radio, list_radios

from .conftest import SERIAL


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="My HackRF", unique_id=SERIAL,
                            data={"transport": "local", "serial": SERIAL}, options=DEFAULT_SETTINGS)
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def client(device, spectrum):
    return AsyncMock(devices=AsyncMock(return_value=[device]), sweep=AsyncMock(return_value=spectrum),
                     firmware_status=AsyncMock(return_value={"phase": "idle", "active": False, "catalog": []}))


async def test_real_config_flow_requires_safety_and_prevents_duplicate_serial(hass, device):
    client = AsyncMock(devices=AsyncMock(return_value=[device]))
    with patch("custom_components.hackrf_radio.config_flow.create_client", return_value=client), \
            patch("custom_components.hackrf_radio.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] == "menu"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "local"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "device"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "name": "Test radio", "serial": SERIAL, "acknowledge_input_safety": False,
        })
        assert result["errors"] == {"acknowledge_input_safety": "safety_required"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "name": "Test radio", "serial": SERIAL, "acknowledge_input_safety": True,
        })
        assert result["type"] == "create_entry"
        assert result["data"]["serial"] == SERIAL
        assert result["options"] == DEFAULT_SETTINGS
        await hass.async_block_till_done()
        again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        again = await hass.config_entries.flow.async_configure(again["flow_id"], {"next_step_id": "local"})
        again = await hass.config_entries.flow.async_configure(again["flow_id"], {})
        again = await hass.config_entries.flow.async_configure(again["flow_id"], {
            "name": "Duplicate", "serial": SERIAL, "acknowledge_input_safety": True,
        })
        assert again["reason"] == "already_configured"


async def test_coordinator_disarms_errors_and_settings_changes(hass, entry, client):
    coordinator = HackRFCoordinator(hass, entry, client)
    assert coordinator.monitoring is False
    await coordinator._async_update_data()
    assert coordinator.device["serial"] == SERIAL
    await coordinator.async_set_monitoring(True)
    assert coordinator.monitoring
    assert coordinator.has_fresh_spectrum
    await coordinator.async_apply_settings(DEFAULT_SETTINGS | {"start_mhz": 431})
    assert coordinator.monitoring is False
    assert coordinator.spectrum is None
    assert coordinator.settings["start_mhz"] == 431
    client.sweep.side_effect = RadioError("Device disconnected")
    with pytest.raises(RadioError):
        await coordinator.async_set_monitoring(True)
    assert coordinator.monitoring is False
    assert not coordinator.last_update_success
    await coordinator.async_shutdown()


async def test_real_options_flow_rejects_bad_gain_then_saves(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], DEFAULT_SETTINGS | {"lna_gain": 7})
    assert result["errors"] == {"base": "invalid_settings"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], DEFAULT_SETTINGS | {"lna_gain": 24})
    assert result["type"] == "create_entry"
    assert entry.options["lna_gain"] == 24


async def test_bridge_config_flow_handles_auth_failure(hass):
    from custom_components.hackrf_radio.radio import AuthenticationError

    client = AsyncMock(devices=AsyncMock(side_effect=AuthenticationError("Invalid token")))
    with patch("custom_components.hackrf_radio.config_flow.create_client", return_value=client):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "bridge"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": "http://127.0.0.1:8099", "token": "wrong"})
        assert result["errors"] == {"base": "invalid_auth"}
        client.close.assert_awaited_once()


async def test_coordinator_stop_during_capture_and_shutdown(hass, entry, client):
    coordinator = HackRFCoordinator(hass, entry, client)
    started = asyncio.Event()

    async def block(*args):
        started.set()
        await asyncio.Event().wait()

    client.sweep.side_effect = block
    task = asyncio.create_task(coordinator.async_set_monitoring(True))
    await started.wait()
    await coordinator.async_set_monitoring(False)
    await asyncio.wait_for(task, 2)
    assert not coordinator.monitoring
    await coordinator.async_shutdown()
    client.close.assert_awaited_once()


async def test_diagnostics_and_sensor_states_omit_secrets_and_fft_arrays(hass, entry, client, spectrum):
    coordinator = HackRFCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    coordinator.spectrum = spectrum
    assert HackRFSensor(coordinator, "peak_db", "Peak level", "dB").native_value == -30
    snapshot = coordinator.snapshot()
    assert "frequencies" not in snapshot["spectrum"]
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    encoded = json.dumps(diagnostic)
    assert SERIAL not in encoded
    assert "token" not in encoded
    coordinator.spectrum = spectrum | {"timestamp": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    assert HackRFSensor(coordinator, "peak_db", "Peak level", "dB").native_value is None
    await coordinator.async_shutdown()


async def test_setup_failure_closes_resources_and_unload_stops_work(hass, entry, client):
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    with patch("custom_components.hackrf_radio.client.create_client", return_value=client), \
            patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=RuntimeError("platform failure")), \
            patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
        with pytest.raises(RuntimeError, match="platform failure"):
            await async_setup_entry(hass, entry)
    client.close.assert_awaited_once()
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
        assert await async_unload_entry(hass, entry)


async def test_platform_rollback_failure_still_closes_radio(hass, entry, client):
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    with patch("custom_components.hackrf_radio.client.create_client", return_value=client), \
            patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=RuntimeError("setup failure")), \
            patch.object(hass.config_entries, "async_unload_platforms", side_effect=RuntimeError("rollback failure")):
        with pytest.raises(RuntimeError, match="rollback failure"):
            await async_setup_entry(hass, entry)
    client.close.assert_awaited_once()


async def test_websocket_requires_admin(hass):
    connection = type("Connection", (), {"user": type("User", (), {"is_admin": False})(), "send_error": lambda *args: None})()
    with pytest.raises(Unauthorized):
        list_radios(hass, connection, {"id": 1})
    with pytest.raises(Unauthorized):
        firmware_radio(hass, connection, {"id": 2, "action": "install"})


async def test_panel_settings_reject_unsafe_flags_before_changes(hass, entry, client):
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    coordinator = HackRFCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    connection = type("Connection", (), {
        "user": type("User", (), {"is_admin": True})(),
        "send_error": lambda *args: None,
        "send_result": lambda *args: None,
        "async_handle_exception": lambda *args: None,
    })()
    with patch.object(type(connection), "send_error") as error:
        control_radio(hass, connection, {"id": 1, "entry_id": entry.entry_id, "action": "configure", "settings": {"amp": True}})
        await hass.async_block_till_done()
        error.assert_called_once()
        assert "locked off" in error.call_args.args[-1]
    assert coordinator.settings == DEFAULT_SETTINGS
    await coordinator.async_shutdown()
