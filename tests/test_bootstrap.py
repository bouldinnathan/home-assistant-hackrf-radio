"""First-run configuration uses metadata only and preserves existing files."""

import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.setup_bridge import prepare


def device_stat(path, actual):
    def get(value, *args, **kwargs):
        if value == path:
            return SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_gid=44)
        return actual(value, *args, **kwargs)
    return get


def test_first_run_generates_private_distinct_credentials_and_nonroot_profiles(tmp_path):
    device = Path("/dev/bus/usb/001/005")
    env = tmp_path / ".env"
    data, programs = tmp_path / "data", tmp_path / "programs"
    with patch.object(Path, "stat", device_stat(device, Path.stat)), patch("os.getuid", return_value=1000), patch("os.getgid", return_value=1000):
        values = prepare(device, "192.168.1.10", env, True, data, programs)
    assert values["HACKRF_BRIDGE_TOKEN"] != values["HACKRF_RECEIVE_TOKEN"]
    assert len(values["HACKRF_BRIDGE_TOKEN"]) >= 32
    assert env.stat().st_mode & 0o777 == 0o600
    assert values["HACKRF_USB_GID"] == 44 and values["HACKRF_UID"] == 1000
    assert (data / "home").is_dir()
    profile = json.loads((programs / "programs.json").read_text())[0]
    assert profile["access"] == "offline" and profile["publish_results"]
    assert json.loads(subprocess.check_output([sys.executable, str(programs / "status.py")])) == {"hackrf_radio_result": {"program_status": "ready"}}
    before = env.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        prepare(device, "192.168.1.10", env, True, data, programs)
    assert env.read_bytes() == before


@pytest.mark.parametrize("address", ["0.0.0.0", "8.8.8.8", "example.com", "::"])
def test_setup_rejects_public_wildcard_and_ambiguous_bind_addresses(tmp_path, address):
    with pytest.raises(ValueError):
        prepare(Path("/dev/bus/usb/001/005"), address, tmp_path / ".env")
    assert not (tmp_path / ".env").exists()


def test_host_lease_rejects_live_duplicate_then_reuses_stale_empty_file(tmp_path):
    from custom_components.hackrf_radio.host_lock import HostLease
    from custom_components.hackrf_radio.radio import RadioError

    path = tmp_path / "bridge.lock"
    with HostLease(path):
        with pytest.raises(RadioError, match="lease"):
            with HostLease(path):
                pass
    with HostLease(path):
        assert path.read_bytes() == b""
    data = tmp_path / "data"
    data.write_text("preserve this file")
    with pytest.raises(RadioError):
        with HostLease(data):
            pass
    link = tmp_path / "symlink.lock"
    link.symlink_to(data)
    with pytest.raises(RadioError):
        with HostLease(link):
            pass
    assert data.read_text() == "preserve this file"


def test_root_program_setup_and_existing_state_are_never_silently_replaced(tmp_path):
    device = Path("/dev/bus/usb/001/005")
    data = tmp_path / "data"
    data.mkdir()
    journal = data / "owner.json"
    journal.write_text('{"phase":"external"}')
    with patch.object(Path, "stat", device_stat(device, Path.stat)), patch("os.getuid", return_value=0):
        with pytest.raises(ValueError, match="unprivileged"):
            prepare(device, "127.0.0.1", tmp_path / ".env", True)
    with patch.object(Path, "stat", device_stat(device, Path.stat)), patch("os.getuid", return_value=1000):
        with pytest.raises(ValueError, match="not empty"):
            prepare(device, "127.0.0.1", tmp_path / ".env", True, data, tmp_path / "programs")
    assert journal.read_text() == '{"phase":"external"}'
    assert not (tmp_path / ".env").exists()
