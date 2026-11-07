"""CI smoke test inside the non-root image; does not enumerate/open USB."""

import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from custom_components.hackrf_radio.host_lock import HostLease
from custom_components.hackrf_radio.radio import LocalRadio


async def main():
    assert os.geteuid() >= 1000
    home_test = Path.home() / ".readiness"
    home_test.write_text("ready")
    assert home_test.read_text() == "ready"
    home_test.unlink()
    with TemporaryDirectory() as directory:
        program = Path(directory) / "status.py"
        program.write_text('print(\'{"hackrf_radio_result":{"ready":true}}\', flush=True)\n')
        profile = Path(directory) / "programs.json"
        profile.write_text(json.dumps([{"id": "user_check", "name": "CI readiness check", "access": "offline", "publish_results": True,
                                        "argv": [sys.executable, str(program)]}]))
        radio = LocalRadio(application_directory=Path("/var/lib/hackrf/check"), profiles_path=profile)
        try:
            await radio.application_action("0" * 32, "launch", {"app_id": "user_check", "confirmed": True})
            await asyncio.wait_for(radio._applications._watcher, 10)
            state = await radio.application_status()
            assert state["result"]["data"] == {"ready": True}
            assert state["last_exit"]["returncode"] == 0
            assert any(record["event"] == "app.result" for record in (await radio.activity_read())["records"])
        finally:
            await radio.close()
    print("Non-root offline program, result persistence and activity history passed without USB access")


with HostLease(Path("/run/hackrf/bridge.lock")):
    asyncio.run(main())
