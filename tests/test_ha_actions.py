"""Actual HA action dispatch, permissions, result events, persistence and logs."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from custom_components.hackrf_radio.const import DEFAULT_SETTINGS, DOMAIN
from custom_components.hackrf_radio.radio import RadioError
from tests.ha_helpers import MockConfigEntry

from .conftest import SERIAL


@pytest.fixture
async def loaded(hass, device, spectrum, enable_custom_integrations):
    client = AsyncMock(devices=AsyncMock(return_value=[device]), sweep=AsyncMock(return_value=spectrum),
                       firmware_status=AsyncMock(return_value={"phase": "idle", "active": False, "catalog": []}),
                       application_status=AsyncMock(return_value={"owner": {"phase": "home_assistant"}, "installer": {"phase": "idle"}, "catalog": []}))
    entry = MockConfigEntry(domain=DOMAIN, title="Workbench", unique_id=SERIAL,
                            data={"transport": "bridge", "serial": SERIAL, "url": "http://private-host:8099", "token": "private-token-value"}, options=DEFAULT_SETTINGS)
    entry.add_to_hass(hass)
    with patch("custom_components.hackrf_radio.client.create_client", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, client
        await hass.config_entries.async_unload(entry.entry_id)


async def test_automation_actions_results_sensor_presets_and_persistent_logs(hass, loaded):
    entry, client = loaded
    base = {"entry_id": entry.entry_id}
    events = []
    unsubscribe = hass.bus.async_listen("hackrf_radio_result", events.append)
    await hass.services.async_call(DOMAIN, "sweep", base, blocking=True)
    assert entry.runtime_data.spectrum["peak_db"] == -30
    result = await hass.services.async_call(DOMAIN, "report_result", base | {"source": "garden_decoder", "data": {"temperature_c": 21.5}}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert result["result"]["data"] == {"temperature_c": 21.5}
    assert len(events) == 1 and events[0].data["entry_id"] == entry.entry_id
    assert hass.states.get("sensor.workbench_custom_result").attributes["measurements"] == {"temperature_c": 21.5}
    await hass.services.async_call(DOMAIN, "preset", base | {"action": "save", "name": "Garden", "settings": {"start_mhz": 431}}, blocking=True)
    await hass.services.async_call(DOMAIN, "preset", base | {"action": "apply", "name": "Garden"}, blocking=True)
    assert entry.options["start_mhz"] == 431 and not entry.runtime_data.monitoring
    history = await hass.services.async_call(DOMAIN, "get_activity", base, blocking=True, return_response=True)
    assert any(item["event"] == "sweep.completed" for item in history["records"])
    assert "private-token-value" not in json.dumps(history) and "private-host" not in json.dumps(history)
    # A new coordinator loads the same state, rather than depending on a browser.
    from custom_components.hackrf_radio.coordinator import HackRFCoordinator

    restarted = HackRFCoordinator(hass, entry, client)
    await restarted.async_initialize()
    assert restarted.user_presets["Garden"]["start_mhz"] == 431
    assert restarted.last_result["data"]["temperature_c"] == 21.5
    await restarted.async_shutdown()
    unsubscribe()


async def test_action_permissions_and_confirmation_cannot_be_bypassed(hass, loaded, hass_read_only_user):
    entry, client = loaded
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "application", {"entry_id": entry.entry_id, "action": "stop"},
                                       blocking=True, context=Context(user_id=hass_read_only_user.id))
    client.application_action.assert_not_awaited()
    client.application_action.side_effect = RadioError("Confirm the configured program before launch")
    with pytest.raises(HomeAssistantError, match="Confirm"):
        await hass.services.async_call(DOMAIN, "application", {"entry_id": entry.entry_id, "action": "launch", "app_id": "user_decoder"}, blocking=True)
    assert client.application_action.call_args.args[-1] == {"app_id": "user_decoder", "confirmed": False}
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "report_result", {"entry_id": entry.entry_id, "source": "decoder", "data": {"token": "private"}}, blocking=True)


async def test_host_results_emit_once_and_skip_other_radios(hass, loaded):
    entry, client = loaded
    events = []
    unsubscribe = hass.bus.async_listen("hackrf_radio_result", events.append)
    value = {"id": "a" * 32, "serial": SERIAL, "source": "user_decoder", "timestamp": "2026-11-05T12:30:00+00:00", "data": {"signal": "detected"}}
    client.application_status.return_value = {"owner": {"phase": "external"}, "installer": {"phase": "idle"}, "catalog": [], "result": value}
    await entry.runtime_data.async_applications("status")
    await entry.runtime_data.async_applications("status")
    await hass.async_block_till_done()
    assert len(events) == 1
    # A measurement submitted through HA must not make the bridge's old result
    # look new again on the next poll.
    await entry.runtime_data.async_report_result("manual_decoder", {"value": 1})
    await entry.runtime_data.async_applications("status")
    await hass.async_block_till_done()
    assert len(events) == 2
    assert entry.runtime_data.last_result["source"] == "manual_decoder"
    client.application_status.return_value["result"] = value | {"id": "b" * 32, "serial": "f" * 32}
    await entry.runtime_data.async_applications("status")
    await hass.async_block_till_done()
    assert len(events) == 2
    unsubscribe()


async def test_activity_websocket_remains_usable_for_unloaded_entry(hass, loaded, hass_ws_client):
    entry, _client = loaded
    ws = await hass_ws_client(hass)
    await entry.runtime_data.activity.record("test.retained", "Connection failed earlier", level="warning")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await ws.send_json({"id": 1, "type": "hackrf_radio/activity", "entry_id": entry.entry_id, "search": "failed earlier"})
    response = await ws.receive_json()
    assert response["success"] and len(response["result"]["records"]) == 1


async def test_failed_result_storage_does_not_publish_or_forget_the_pending_result(hass, loaded):
    entry, _client = loaded
    coordinator = entry.runtime_data
    events = []
    unsubscribe = hass.bus.async_listen("hackrf_radio_result", events.append)
    with patch.object(coordinator._store, "async_save", side_effect=OSError("disk full")), pytest.raises(RadioError, match="storage"):
        await coordinator.async_report_result("decoder", {"value": 1})
    await hass.async_block_till_done()
    assert events == [] and coordinator.last_result is None and coordinator._result_id is None
    await coordinator.async_report_result("decoder", {"value": 1})
    await hass.async_block_till_done()
    assert len(events) == 1
    unsubscribe()
