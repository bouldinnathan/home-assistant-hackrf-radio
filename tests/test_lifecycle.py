"""Full HA setup, entities, panel registration, WebSocket and unload."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hackrf_radio.const import DEFAULT_SETTINGS, DOMAIN

from .conftest import SERIAL


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_full_integration_lifecycle(hass, device, spectrum, hass_ws_client):
    client = AsyncMock(devices=AsyncMock(return_value=[device]), sweep=AsyncMock(return_value=spectrum),
                       firmware_status=AsyncMock(return_value={"phase": "idle", "active": False, "catalog": []}),
                       application_status=AsyncMock(return_value={"owner": {"phase": "home_assistant"}, "installer": {"phase": "idle"}, "catalog": []}))
    entry = MockConfigEntry(domain=DOMAIN, title="Workbench", unique_id=SERIAL,
                            data={"transport": "bridge", "serial": SERIAL, "url": "http://private-host:8099", "token": "secret-token"},
                            options=DEFAULT_SETTINGS)
    entry.add_to_hass(hass)
    with patch("custom_components.hackrf_radio.client.create_client", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert not entry.runtime_data.monitoring
        assert len([state for state in hass.states.async_all() if state.entity_id.startswith(("sensor.workbench_", "binary_sensor.workbench_", "button.workbench_", "switch.workbench_"))]) == 11
        panel = hass.data["frontend_panels"]["hackrf-radio"]
        assert panel.require_admin
        ws = await hass_ws_client(hass)
        await ws.send_json({"id": 1, "type": "hackrf_radio/control", "entry_id": entry.entry_id, "action": "sweep"})
        result = await ws.receive_json()
        assert result["success"]
        assert result["result"]["spectrum"]["peak_db"] == -30
        assert "secret-token" not in json.dumps(result)
        assert "private-host" not in json.dumps(result)
        await hass.async_block_till_done()
        assert float(hass.states.get("sensor.workbench_peak_level").state) == -30
        assert hass.states.get("binary_sensor.workbench_signal_detected").state == "on"
        await ws.send_json({"id": 2, "type": "hackrf_radio/control", "entry_id": entry.entry_id, "action": "configure", "settings": DEFAULT_SETTINGS | {"lna_gain": 24}})
        result = await ws.receive_json()
        assert result["success"]
        await hass.async_block_till_done()
        assert entry.options["lna_gain"] == 24
        assert entry.runtime_data.spectrum is None
        assert await hass.config_entries.async_unload(entry.entry_id)
        client.close.assert_awaited_once()
        assert entry.state is ConfigEntryState.NOT_LOADED
