"""User code is tested using actual child processes, without opening USB."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.hackrf_radio.programs import load_profiles, result_data
from custom_components.hackrf_radio.radio import LocalRadio, RadioBusy, RadioError

from .conftest import HELP, INFO, SERIAL


def write_profile(tmp_path, code, **values):
    script = tmp_path / "decoder.py"
    script.write_text(code)
    path = tmp_path / "programs.json"
    path.write_text(json.dumps([{"id": "user_decoder", "name": "My decoder", "argv": [sys.executable, str(script), "{serial}"], **values}]))
    return path


@pytest.mark.parametrize("change", [
    {"id": "gqrx"}, {"id": "user_../file"}, {"argv": "python script.py"}, {"argv": ["python", "script.py"]},
    {"argv": ["/usr/bin/python", "{command}"]}, {"cwd": "relative"}, {"access": "transmit"}, {"publish_results": 1},
    {"unknown": True}, {"name": ""},
])
def test_profile_validation_rejects_ambiguous_or_remote_commands(tmp_path, change):
    path = write_profile(tmp_path, "pass\n", **change)
    with pytest.raises(RadioError):
        load_profiles(path)


@pytest.mark.parametrize("data", [{"token": "secret"}, {"nested": {"command": "exec"}}, {"bad field": 1}, {"value": float("nan")}, {"value": "x" * 201}])
def test_decoder_results_are_bounded_scalars(data):
    with pytest.raises(ValueError):
        result_data(data)


def test_example_file_analyzer_handles_signed_iq_and_rejects_incomplete_data(tmp_path):
    from examples.iq_analysis import analyze

    path = tmp_path / "capture.cs8"
    path.write_bytes(bytes([127, 128]) * 1024)
    values = analyze(path)
    assert values["complex_samples"] == 1024
    assert values["mean_i"] == 127 and values["mean_q"] == -128
    assert values["peak_iq_magnitude"] == pytest.approx(180.314, abs=0.001)
    path.write_bytes(b"\0" * 2049)
    with pytest.raises(ValueError):
        analyze(path)


async def test_root_custom_launch_is_rejected_before_usb_handoff(tmp_path):
    profile = write_profile(tmp_path, "pass\n")
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    radio._run = AsyncMock(return_value=INFO)
    with patch("os.geteuid", return_value=0), pytest.raises(RadioError, match="unprivileged"):
        await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
    radio._run.assert_not_awaited()


async def test_actual_custom_decoder_handoff_output_results_exit_and_restart(tmp_path, monkeypatch):
    secret = "never-inherit-or-log-this-bridge-credential"
    monkeypatch.setenv("HACKRF_BRIDGE_TOKEN", secret)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "not-a-standard-secret-name-but-still-private")
    profile = write_profile(tmp_path, 'import os,json,sys\nprint("starting decoder", flush=True)\nprint(json.dumps({"hackrf_radio_result":{"temperature_c":21.5,"serial_from_arg":sys.argv[1]}}), flush=True)\nprint("token=private-value",file=sys.stderr,flush=True)\nassert "HACKRF_BRIDGE_TOKEN" not in os.environ\nassert "AWS_ACCESS_KEY_ID" not in os.environ\nassert os.environ["HACKRF_SERIAL"] == sys.argv[1]\n', publish_results=True)
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    radio._run = AsyncMock(return_value=INFO)
    with patch("os.geteuid", return_value=1000):
        state = await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
    assert state["owner"]["phase"] == "external"
    await asyncio.wait_for(radio._applications._watcher, 5)
    state = await radio.application_status()
    assert not state["running"] and state["last_exit"]["returncode"] == 0
    assert state["result"]["data"]["temperature_c"] == 21.5
    assert state["result"]["data"]["serial_from_arg"] == "[identifier redacted]"
    history = await radio.activity_read()
    assert any(item["event"] == "app.output" for item in history["records"])
    assert "private-value" not in json.dumps(history) and secret not in json.dumps(history)
    assert "argv" not in json.dumps(state["catalog"])
    restarted = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    restarted._run = AsyncMock(return_value=INFO)
    assert (await restarted.application_status())["result"] == state["result"]
    with pytest.raises(RadioBusy):
        await restarted.devices()
    await restarted.application_action(SERIAL, "release", {"confirmed_closed": True})
    assert (await restarted.application_status())["owner"]["phase"] == "home_assistant"
    await radio.close()
    await restarted.close()


async def test_offline_program_does_not_take_usb_and_stop_reaps_owned_process(tmp_path):
    profile = write_profile(tmp_path, 'import time\nprint("working",flush=True)\ntime.sleep(60)\n', access="offline")
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    radio._run = AsyncMock(return_value=INFO)
    with patch("os.geteuid", return_value=1000):
        state = await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
        assert state["owner"]["phase"] == "home_assistant"
        with pytest.raises(RadioBusy):
            await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
    process = radio._applications._process
    await radio.application_action(SERIAL, "stop", {})
    assert process.returncode is not None
    assert not (await radio.application_status())["running"]
    await radio.close()


async def test_noisy_output_and_very_long_lines_are_bounded_and_do_not_deadlock(tmp_path):
    profile = write_profile(tmp_path, 'import sys\nprint("x"*100000 + " token=do-not-log-this")\nfor i in range(10000): print("noise",i)\nprint("stderr done",file=sys.stderr)\nraise SystemExit(7)\n', access="offline")
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    with patch("os.geteuid", return_value=1000):
        await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
    await asyncio.wait_for(radio._applications._watcher, 10)
    history = await radio.activity_read()
    assert len(history["records"]) < 30
    assert "do-not-log-this" not in json.dumps(history)
    assert any(item["event"] == "app.output_limited" for item in history["records"])
    assert (await radio.application_status())["last_exit"]["returncode"] == 7
    await radio.close()


async def test_usb_return_cannot_race_between_handoff_and_process_start(tmp_path):
    profile = write_profile(tmp_path, "import time\ntime.sleep(60)\n")
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    radio._run = AsyncMock(return_value=INFO)
    spawning, proceed = asyncio.Event(), asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        spawning.set()
        await proceed.wait()
        return await spawn(*args, **kwargs)

    with patch("os.geteuid", return_value=1000), patch("asyncio.create_subprocess_exec", side_effect=delayed_spawn):
        launch = asyncio.create_task(radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True}))
        await asyncio.wait_for(spawning.wait(), 3)
        release = asyncio.create_task(radio.application_action(SERIAL, "release", {"confirmed_closed": True}))
        await asyncio.sleep(0)
        assert not release.done()
        proceed.set()
        await launch
        with pytest.raises(RadioBusy, match="Stop the managed app"):
            await release
    assert (await radio.application_status())["owner"]["phase"] == "external"
    await radio.application_action(SERIAL, "stop", {})
    await radio.application_action(SERIAL, "release", {"confirmed_closed": True})
    await radio.close()


async def test_disconnected_stop_request_still_finishes_process_cleanup(tmp_path):
    profile = write_profile(tmp_path, "import time\ntime.sleep(60)\n", access="offline")
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    with patch("os.geteuid", return_value=1000):
        await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
    process = radio._applications._process
    signalled = asyncio.Event()
    import os

    killpg = os.killpg

    def signal_owned(*args):
        result = killpg(*args)
        signalled.set()
        return result

    with patch("os.killpg", side_effect=signal_owned):
        request = asyncio.create_task(radio.application_action(SERIAL, "stop", {}))
        await asyncio.wait_for(signalled.wait(), 3)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await asyncio.wait_for(radio._applications._stop_job, 5)
    assert process.returncode is not None
    assert not (await radio.application_status())["running"]
    await radio.close()


async def test_bad_profile_edit_does_not_prevent_managed_process_cleanup(tmp_path):
    profile = write_profile(tmp_path, "import time\ntime.sleep(60)\n", access="offline")
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    with patch("os.geteuid", return_value=1000):
        await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
    process = radio._applications._process
    profile.write_text("{not valid json")
    await radio.close()
    assert process.returncode is not None


async def test_optional_measurement_storage_failure_keeps_draining_program_output(tmp_path):
    profile = write_profile(tmp_path, 'print("normal diagnostic output",flush=True)\nprint(\'{"hackrf_radio_result":{"value":1}}\',flush=True)\nprint("done",flush=True)\n', access="offline", publish_results=True)
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    with patch.object(radio._applications, "_save", side_effect=OSError("full disk")), patch("os.geteuid", return_value=1000):
        await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
        await asyncio.wait_for(radio._applications._watcher, 5)
    state = await radio.application_status()
    assert state["last_exit"]["returncode"] == 0
    assert not state["result"]
    records = (await radio.activity_read())["records"]
    assert any(item["event"] == "app.result_rejected" for item in records)
    assert any(item["message"] == "done" for item in records)
    assert not any(item["event"] == "app.output_error" for item in records)
    await radio.close()


async def test_atomic_recovery_stops_only_confirmed_owner_and_rechecks_health(tmp_path):
    profile = write_profile(tmp_path, "import time\ntime.sleep(60)\n")
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    radio._run = AsyncMock(return_value=INFO)
    with patch("os.geteuid", return_value=1000):
        state = await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
    lease = state["owner"]["lease_id"]
    values = {"lease_id": lease, "confirmed_closed": True, "stop_managed_program": False}
    with pytest.raises(RadioBusy, match="Stop the managed app"):
        await radio.application_action(SERIAL, "recover", values)
    assert radio._applications._process.returncode is None
    radio._run.reset_mock()
    state = await radio.application_action(SERIAL, "recover", values | {"stop_managed_program": True})
    assert state["owner"]["phase"] == "home_assistant"
    assert radio._applications._process.returncode is not None
    assert all(call.args[0] == ["hackrf_info"] for call in radio._run.call_args_list)
    await radio.close()


async def test_atomic_recovery_rejects_a_lease_changed_while_waiting_for_the_control_lock(tmp_path):
    radio = LocalRadio(application_directory=tmp_path / "apps")
    radio._run = AsyncMock(return_value=INFO)
    state = await radio.application_action(SERIAL, "handoff", {"app_id": "external", "confirmed": True})
    manager = radio._applications
    radio._run.reset_mock()
    await manager._launch_lock.acquire()
    request = asyncio.create_task(radio.application_action(SERIAL, "recover", {"lease_id": state["owner"]["lease_id"], "confirmed_closed": True, "stop_managed_program": True}))
    await asyncio.sleep(0)
    await manager._save("owner.json", state["owner"] | {"lease_id": "f" * 32})
    manager._launch_lock.release()
    with pytest.raises(RadioBusy, match="changed"):
        await request
    radio._run.assert_not_awaited()
    assert (await radio.application_status())["owner"]["phase"] == "external"
    await radio.close()


async def test_profile_launch_preflight_reports_root_and_missing_working_directory_without_usb(tmp_path):
    profile = write_profile(tmp_path, "pass\n", cwd=str(tmp_path / "missing"))
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=profile)
    radio._run = AsyncMock(return_value=INFO)
    with patch("os.geteuid", return_value=0):
        app = next(app for app in (await radio.application_status())["catalog"] if app["id"] == "user_decoder")
        assert not app["launch_enabled"] and "unprivileged" in app["launch_reason"]
    with patch("os.geteuid", return_value=1000), pytest.raises(RadioError, match="working directory"):
        await radio.application_action(SERIAL, "launch", {"app_id": "user_decoder", "confirmed": True})
    radio._run.assert_not_awaited()
    assert (await radio.application_status())["owner"]["phase"] == "home_assistant"
    await radio.close()


async def test_explicit_missing_profiles_report_error_but_leave_basic_radio_available(tmp_path):
    radio = LocalRadio(application_directory=tmp_path / "apps", profiles_path=tmp_path / "absent.json")
    assert (await radio.application_status())["profile_error"]
    radio._run = AsyncMock(side_effect=lambda args, **kwargs: HELP if args[-1] == "-h" else INFO)
    assert (await radio.application_status())["owner"]["phase"] == "home_assistant"
    assert (await radio.devices())[0]["serial"] == SERIAL
    await radio.close()
