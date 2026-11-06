"""Durability, bounded retention, privacy, corruption and log access boundaries."""

import asyncio
import json
import sqlite3
import sys
from unittest.mock import patch

import pytest

from custom_components.hackrf_radio.activity import ActivityLog, redact
from custom_components.hackrf_radio.bridge import create_app
from custom_components.hackrf_radio.client import BridgeClient
from custom_components.hackrf_radio.radio import LocalRadio, RadioError

TOKEN = "administrative-secret-of-at-least-thirty-two-characters"
RECEIVE = "receive-secret-of-at-least-thirty-two-characters"


def test_redaction_strips_credentials_urls_serials_and_terminal_control_codes():
    result = redact('token="abc" Authorization: Bearer xyz https://user:password@host/?key=private\n\x00\x1b ' + "a" * 32 + " known-private-value", ("known-private-value",))
    for value in ["abc", "xyz", "https://", "password", "private-value", "a" * 32, "\x1b", "\x00", "\n"]:
        assert value not in result


async def test_concurrent_records_retention_restart_and_correlated_pagination(tmp_path):
    path = tmp_path / "private" / "activity.sqlite"
    activity = ActivityLog(path, secrets=(TOKEN,), retention=10)
    await asyncio.gather(*(activity.record("test.record", f"item {index} {TOKEN}", operation_id="a" * 32) for index in range(30)))
    restarted = ActivityLog(path, retention=10)
    first = await restarted.read(limit=4)
    second = await restarted.read(limit=10, before=first["next_before"])
    assert len(first["records"]) + len(second["records"]) == 10
    sequences = [item["sequence"] for item in first["records"] + second["records"]]
    assert sequences == sorted(set(sequences), reverse=True)
    assert all(item["operation_id"] == "a" * 32 for item in first["records"])
    assert TOKEN not in json.dumps(first)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_size < 32 * 1024 * 1024


async def test_filters_are_literal_bounded_and_duplicate_failures_suppressed(tmp_path):
    activity = ActivityLog(tmp_path / "activity.sqlite")
    await activity.record("test.error", "USB permission problem", level="error", deduplicate=True)
    await activity.record("test.error", "USB permission problem", level="error", deduplicate=True)
    await activity.record("test.debug", "status fine", level="debug")
    assert len((await activity.read())["records"]) == 2
    assert len((await activity.read(level="error", search="PERMISSION"))["records"]) == 1
    assert not (await activity.read(search="%' OR 1=1 --"))["records"]
    for filters in [{"limit": 501}, {"limit": True}, {"before": -1}, {"level": "fatal"}, {"search": "x" * 121}]:
        with pytest.raises(ValueError):
            await activity.read(**filters)


async def test_unicode_log_pages_stay_inside_bridge_response_budget(tmp_path):
    activity = ActivityLog(tmp_path / "activity.sqlite")
    await asyncio.gather(*(activity.record("test.unicode", "📻" * 1000) for _ in range(350)))
    page = await activity.read(limit=500)
    assert len(json.dumps(page).encode()) < 2_000_000
    assert len(page["records"]) < 350
    rest = await activity.read(limit=500, before=page["next_before"])
    assert len(page["records"]) + len(rest["records"]) == 350


async def test_full_disk_degrades_logging_then_recovers_without_affecting_operations(tmp_path, caplog):
    activity = ActivityLog(tmp_path / "activity.sqlite")
    with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("disk full, secret database path")):
        assert await activity.record("test.failure", "An operation finished")
        assert await activity.record("test.failure", "Another operation finished")
        assert (await activity.read())["storage_error"]
    assert caplog.text.count("HackRF activity storage unavailable") == 1
    assert "secret database path" not in caplog.text
    await activity.record("test.recovered", "Logging works again")
    result = await activity.read()
    assert result["storage_error"] is None
    assert result["records"][0]["event"] == "test.recovered"


async def test_corrupt_database_and_symlink_are_preserved_for_manual_inspection(tmp_path):
    path = tmp_path / "activity.sqlite"
    path.write_bytes(b"damaged database")
    activity = ActivityLog(path)
    assert (await activity.read())["storage_error"]
    assert path.read_bytes() == b"damaged database"
    link = tmp_path / "link.sqlite"
    link.symlink_to(path)
    assert (await ActivityLog(link).read())["storage_error"]
    assert path.read_bytes() == b"damaged database"


async def test_package_failure_keeps_useful_redacted_tool_diagnostics(tmp_path):
    from custom_components.hackrf_radio.software import _run_packages

    activity = ActivityLog(tmp_path / "activity.sqlite", secrets=(TOKEN,))
    command = [sys.executable, "-c", f'print("noise"*1000); print("package problem token={TOKEN}"); raise SystemExit(3)']
    with pytest.raises(RadioError):
        await _run_packages([command], {}, activity)
    records = (await activity.read())["records"]
    failure = next(item for item in records if item["event"] == "packages.tool_failed")
    assert "package problem" in failure["message"]
    assert failure["details"]["returncode"] == "3"
    assert TOKEN not in json.dumps(records)


@pytest.mark.usefixtures("socket_enabled")
async def test_host_log_auth_and_correlation_through_actual_http(aiohttp_client, tmp_path):
    radio = LocalRadio(application_directory=tmp_path / "apps")
    http = await aiohttp_client(create_app(TOKEN, radio, receive_token=RECEIVE))
    assert (await http.get("/v1/activity")).status == 401
    assert (await http.get("/v1/activity", headers={"Authorization": f"Bearer {RECEIVE}"})).status == 403
    headers = {"Authorization": f"Bearer {TOKEN}", "X-HackRF-Operation-ID": "b" * 32}
    bad = await http.post("/v1/applications/handoff", json={}, headers=headers)
    assert bad.status == 400
    result = await http.get("/v1/activity?level=warning", headers=headers)
    assert result.status == 200
    data = (await result.json())["activity"]
    invalid = next(item for item in data["records"] if item["event"] == "bridge.invalid_request")
    assert invalid["operation_id"] == "b" * 32
    assert TOKEN not in json.dumps(data) and RECEIVE not in json.dumps(data)
    bridge = BridgeClient(http.session, str(http.make_url("")), TOKEN)
    assert (await bridge.activity_read(level="warning"))["records"]
    assert (await http.get("/v1/activity?limit=100000", headers=headers)).status == 400
