import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

import pytest

from custom_components.hackrf_radio.const import DEFAULT_SETTINGS
from custom_components.hackrf_radio.radio import (
    LocalRadio,
    RadioBusy,
    RadioError,
    parse_info,
    parse_sweep,
    sweep_arguments,
    validate_settings,
)

from .conftest import CSV, HELP, INFO, SERIAL


@pytest.mark.parametrize("changes", [
    {"amp": True}, {"antenna_enable": 1}, {"transmit": True}, {"extra_args": "-a 1"},
    {"start_mhz": 0}, {"end_mhz": 6001}, {"start_mhz": 440, "end_mhz": 430},
    {"start_mhz": 5901, "end_mhz": 6000}, {"start_mhz": 5990, "end_mhz": 6000},
    {"end_mhz": 600}, {"lna_gain": 7}, {"vga_gain": 63}, {"lna_gain": True},
    {"vga_gain": 4.5}, {"bin_width": 2444}, {"bin_width": 2445, "end_mhz": 530},
    {"threshold_db": float("nan")}, {"scan_interval": 0}, {"threshold_db": 10},
])
def test_settings_reject_unsafe_and_malformed_input(changes):
    with pytest.raises(ValueError):
        validate_settings(DEFAULT_SETTINGS | changes)


def test_command_is_fixed_receive_only_and_targets_exact_serial():
    command = sweep_arguments(SERIAL.upper(), DEFAULT_SETTINGS)
    assert command[0] == "hackrf_sweep"
    assert command[command.index("-d") + 1] == SERIAL
    assert command[command.index("-a") + 1] == "0"
    assert command[command.index("-p") + 1] == "0"
    assert "-1" in command
    assert "-t" not in command
    for serial in ["", SERIAL[-8:], "--help", SERIAL + ";touch /tmp/unsafe"]:
        with pytest.raises(ValueError):
            sweep_arguments(serial, DEFAULT_SETTINGS)


@pytest.mark.parametrize(("board", "firmware", "revision"), [
    ("2 (HackRF One)", "2024.02.1", "r10"),
    ("4 (HackRF One)", "2023.01.1", "r9"),
    ("5 (HackRF Pro)", "2026.01.3", "r1.2.1"),
    ("2 (HackRF One)", "n_250606", "r8"),
])
def test_device_variations(board, firmware, revision):
    text = INFO.replace("2 (HackRF One)", board).replace("2024.02.1 (API", firmware + " (API").replace("r10", revision)
    device = parse_info(text)[0]
    assert device.supported
    assert device.firmware == firmware
    assert device.revision == revision


@pytest.mark.parametrize("suffix", [
    "\nError: Firmware does not support HackRF One r9.", "\nSelf-test FAIL:\nRF supply failure",
])
def test_firmware_and_selftest_failures_block_capture(suffix):
    assert not parse_info(INFO + suffix)[0].supported


def test_old_firmware_unknown_boards_clones_and_missing_revision():
    assert not parse_info(INFO.replace("API:1.08", "API:1.01"))[0].supported
    assert not parse_info(INFO.replace("2 (HackRF One)", "1 (Jawbreaker)"))[0].supported
    clone = parse_info(INFO.replace("Hardware Revision: r10", "Hardware does not appear to have been manufactured by Great Scott Gadgets."))[0]
    assert clone.supported
    assert clone.warning
    assert "Unknown" in clone.revision
    with pytest.raises(RadioError, match="Duplicate"):
        parse_info(INFO + INFO)
    with pytest.raises(RadioError, match="No accessible"):
        parse_info("No HackRF boards found.")


def test_interleaved_bins_are_sorted_cropped_and_finite():
    text = CSV + CSV.replace("430000000, 435000000", "435000000, 440000000")
    result = parse_sweep(text, DEFAULT_SETTINGS)
    assert result["frequencies"] == sorted(result["frequencies"])
    assert result["peak_db"] == -30
    assert result["peak_frequency_mhz"] == 432.5
    assert result["bin_count"] == 10
    trimmed = parse_sweep(text, DEFAULT_SETTINGS | {"end_mhz": 433})
    assert trimmed["bin_count"] == 3
    result = parse_sweep(CSV.replace("-30", "nan"), DEFAULT_SETTINGS)
    assert result["bin_count"] == 4
    for invalid in ["", "bad,row", CSV.replace("1000000.00", "0"), CSV.replace("1000000.00", "nan")]:
        with pytest.raises(RadioError):
            parse_sweep(invalid, DEFAULT_SETTINGS)


async def test_no_fallback_device_or_unsafe_calls(monkeypatch):
    radio = LocalRadio()
    run = AsyncMock(side_effect=[INFO, HELP])
    monkeypatch.setattr(radio, "_run", run)
    with pytest.raises(RadioError, match="absent"):
        await radio.sweep("a" * 32, DEFAULT_SETTINGS)
    assert run.call_count == 2
    assert all(call.args[0][0] in ("hackrf_info", "hackrf_sweep") for call in run.call_args_list)
    run.reset_mock(side_effect=True)
    run.side_effect = [INFO + "\nSelf-test FAIL:", HELP]
    with pytest.raises(RadioError, match="self-test"):
        await radio.sweep(SERIAL, DEFAULT_SETTINGS)
    assert run.call_count == 1  # Capabilities were already checked; no sweep was launched.


@pytest.fixture
def fake_tools(tmp_path, monkeypatch):
    """Real subprocesses exercise pipes, signals and child reaping."""
    script = f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
name = Path(sys.argv[0]).name
if name == "hackrf_info":
    print({INFO!r})
elif "-h" in sys.argv:
    print({HELP!r})
    sys.exit(1)
else:
    Path(os.environ["HACKRF_TEST_PID"]).write_text(str(os.getpid()))
    Path(os.environ["HACKRF_TEST_ARGS"]).write_text(__import__("json").dumps(sys.argv))
    mode = os.environ.get("HACKRF_TEST_MODE", "normal")
    if mode == "hang":
        time.sleep(60)
    if mode == "flood":
        sys.stdout.write("x" * 4000000)
        sys.stdout.flush()
        time.sleep(60)
    print({CSV!r})
'''
    for name in ["hackrf_info", "hackrf_sweep"]:
        path = tmp_path / name
        path.write_text(script)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("HACKRF_TEST_PID", str(tmp_path / "pid"))
    monkeypatch.setenv("HACKRF_TEST_ARGS", str(tmp_path / "args"))
    return tmp_path


async def test_actual_command_execution(fake_tools):
    radio = LocalRadio()
    result = await radio.sweep(SERIAL, DEFAULT_SETTINGS)
    assert result["peak_db"] == -30
    args = json.loads((fake_tools / "args").read_text())
    assert args[args.index("-a") + 1] == args[args.index("-p") + 1] == "0"
    await radio.close()
    with pytest.raises(RadioError, match="closed"):
        await radio.sweep(SERIAL, DEFAULT_SETTINGS)


async def wait_for_pid(path):
    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(.01)
    return int(path.read_text())


async def test_cancellation_reaps_child_and_blocks_overlap(fake_tools, monkeypatch):
    monkeypatch.setenv("HACKRF_TEST_MODE", "hang")
    radio = LocalRadio()
    task = asyncio.create_task(radio.sweep(SERIAL, DEFAULT_SETTINGS))
    pid = await wait_for_pid(fake_tools / "pid")
    with pytest.raises(RadioBusy):
        await radio.sweep(SERIAL, DEFAULT_SETTINGS)
    await asyncio.wait_for(radio.stop(), 5)
    assert task.cancelled()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    monkeypatch.setenv("HACKRF_TEST_MODE", "normal")
    assert (await radio.sweep(SERIAL, DEFAULT_SETTINGS))["serial"] == SERIAL


@pytest.mark.parametrize("mode", ["hang", "flood"])
async def test_timeout_and_output_limit_reap_child(fake_tools, monkeypatch, mode):
    monkeypatch.setenv("HACKRF_TEST_MODE", mode)
    radio = LocalRadio()
    with pytest.raises(RadioError, match="timed out|maximum|bounded"):
        await asyncio.wait_for(radio._run(sweep_arguments(SERIAL, DEFAULT_SETTINGS), timeout=.3), 5)
    pid = int((fake_tools / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
