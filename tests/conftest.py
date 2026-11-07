"""Shared realistic upstream CLI fixtures; no physical device is opened."""

from importlib.util import find_spec

import pytest


@pytest.fixture
def hass_config_dir(tmp_path):
    directory = tmp_path / "ha_config"
    directory.mkdir(exist_ok=True)
    return str(directory)


@pytest.fixture(autouse=True)
def isolated_legacy_config(hass_config_dir, monkeypatch):
    if find_spec("pytest_homeassistant") is None:
        from pathlib import Path

        from pytest_homeassistant_custom_component import common

        monkeypatch.setattr(common, "get_test_config_dir", lambda *parts: str(Path(hass_config_dir).joinpath(*parts)))

SERIAL = "0000000000000000a06063c8234e935f"
INFO = f"""hackrf_info version: 2024.02.1
libhackrf version: 2024.02.1 (0.9)
Found HackRF
Index: 0
Serial number: {SERIAL}
Board ID Number: 2 (HackRF One)
Firmware Version: 2024.02.1 (API:1.08)
Hardware Revision: r10
Hardware supported by installed firmware:
    HackRF One
"""
CSV = "2026-11-05, 18:03:12.123456, 430000000, 435000000, 1000000.00, 20, -65, -60, -30, -66, -70\n"
HELP = "[-d serial] [-1] [-a amp] [-p antenna] [-f range] [-w bin_width] [-l lna] [-g vga]"


@pytest.fixture
def device():
    from custom_components.hackrf_radio.radio import parse_info

    return parse_info(INFO)[0].as_dict()


@pytest.fixture
def spectrum():
    from custom_components.hackrf_radio.const import DEFAULT_SETTINGS, SAFETY_POLICY
    from custom_components.hackrf_radio.radio import parse_sweep

    return parse_sweep(CSV, DEFAULT_SETTINGS) | {"serial": SERIAL, "safety_policy": SAFETY_POLICY}
