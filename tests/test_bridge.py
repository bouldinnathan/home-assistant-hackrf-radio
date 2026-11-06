import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.hackrf_radio.bridge import create_app
from custom_components.hackrf_radio.client import BridgeClient, validate_spectrum, validate_url
from custom_components.hackrf_radio.const import DEFAULT_SETTINGS, SAFETY_POLICY
from custom_components.hackrf_radio.radio import AuthenticationError, LocalRadio, RadioError

from .conftest import SERIAL

pytestmark = pytest.mark.usefixtures("socket_enabled")
TOKEN = "local-test-token-with-at-least-32-characters"


@pytest.fixture
def radio(device, spectrum):
    return AsyncMock(devices=AsyncMock(return_value=[device]), sweep=AsyncMock(return_value=spectrum))


async def test_authentication_and_client_roundtrip(aiohttp_client, radio):
    http = await aiohttp_client(create_app(TOKEN, radio))
    assert (await http.get("/v1/devices")).status == 401
    radio.devices.assert_not_awaited()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())) as session:
        client = BridgeClient(session, str(http.make_url("/")), TOKEN)
        assert (await client.devices())[0]["serial"] == SERIAL
        assert (await client.sweep(SERIAL, DEFAULT_SETTINGS))["peak_db"] == -30
        bad = BridgeClient(session, str(http.make_url("/")), "wrong")
        with pytest.raises(AuthenticationError):
            await bad.devices()


async def test_bridge_rejects_power_flags_and_arbitrary_operations(aiohttp_client, monkeypatch):
    radio = LocalRadio()
    run = AsyncMock()
    monkeypatch.setattr(radio, "_run", run)
    http = await aiohttp_client(create_app(TOKEN, radio))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = await http.post("/v1/sweep", json={
        "serial": SERIAL, "settings": {"amp": True}, "operation_id": "a" * 32,
    }, headers=headers)
    assert response.status == 400
    run.assert_not_awaited()
    for path in ["/v1/flash", "/v1/transmit", "/v1/reset", "/v1/command"]:
        assert (await http.post(path, json={}, headers=headers)).status == 404
    assert (await http.post("/v1/sweep", data="{bad", headers=headers)).status == 400


async def test_stop_only_cancels_the_matching_operation(aiohttp_client, radio):
    started = asyncio.Event()

    async def block(*args):
        started.set()
        await asyncio.Event().wait()

    radio.sweep.side_effect = block
    http = await aiohttp_client(create_app(TOKEN, radio))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    request = asyncio.create_task(http.post("/v1/sweep", headers=headers, json={
        "serial": SERIAL, "settings": DEFAULT_SETTINGS, "operation_id": "a" * 32,
    }))
    await asyncio.wait_for(started.wait(), 2)
    assert (await http.post("/v1/stop", headers=headers, json={"operation_id": "b" * 32})).status == 200
    assert not request.done()
    assert (await http.post("/v1/stop", headers=headers, json={"operation_id": "a" * 32})).status == 200
    assert (await asyncio.wait_for(request, 2)).status == 409
    # Stop arriving before the matching HTTP sweep is fenced too.
    assert (await http.post("/v1/sweep", headers=headers, json={
        "serial": SERIAL, "settings": DEFAULT_SETTINGS, "operation_id": "b" * 32,
    })).status == 409


async def test_remote_client_stop(aiohttp_client, radio):
    started = asyncio.Event()

    async def block(*args):
        started.set()
        await asyncio.Event().wait()

    radio.sweep.side_effect = block
    http = await aiohttp_client(create_app(TOKEN, radio))
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())) as session:
        client = BridgeClient(session, str(http.make_url("/")), TOKEN)
        task = asyncio.create_task(client.sweep(SERIAL, DEFAULT_SETTINGS))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(client.stop(), 2)
        assert task.done()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("url", ["file:///tmp/device", "ftp://host", "http://user:secret@host", "http://host/?token=secret", "http://host/#x", "http://"])
def test_connection_urls_are_validated(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize("change", [
    {"serial": "f" * 32}, {"safety_policy": "unsafe"}, {"powers": [float("nan")]},
    {"frequencies": [1]}, {"timestamp": "not-a-date"}, {"timestamp": "2026-11-05T00:00:00"},
    {"settings": {"amp": True}},
])
def test_untrusted_bridge_spectra_rejected(spectrum, change):
    with pytest.raises(RadioError):
        validate_spectrum(deepcopy(spectrum) | change, SERIAL, DEFAULT_SETTINGS)


def test_bridge_requires_token_and_locks_policy():
    for token in ["", "short", "x" * 32 + "\n", "é" * 32]:
        with pytest.raises(ValueError):
            create_app(token)
    assert SAFETY_POLICY == "receive_only_v1"
