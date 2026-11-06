"""Standalone USB bridge: python -m custom_components.hackrf_radio.bridge.

Runs on the Linux host with the USB device, independently of Home Assistant.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import re
from collections import deque
from pathlib import Path

from aiohttp import web

from .const import SAFETY_POLICY, VERSION
from .radio import LocalRadio, RadioBusy, RadioError


def create_app(token: str, radio: LocalRadio | None = None, firmware_directory: Path | None = None,
               application_directory: Path | None = None, allow_install: bool = False, allow_launch: bool = False,
               receive_token: str = "", soapy_bind: str = "127.0.0.1") -> web.Application:
    if len(token) < 32 or not token.isascii() or any(character.isspace() for character in token):
        raise ValueError("HACKRF_BRIDGE_TOKEN must contain at least 32 non-whitespace ASCII characters")
    if receive_token and (len(receive_token) < 32 or not receive_token.isascii()
                          or any(character.isspace() for character in receive_token) or receive_token == token):
        raise ValueError("HACKRF_RECEIVE_TOKEN must be a different random token of at least 32 non-whitespace ASCII characters")
    radio = radio or LocalRadio(firmware_directory=firmware_directory, application_directory=application_directory,
                               allow_install=allow_install, allow_launch=allow_launch, soapy_bind=soapy_bind)
    active: dict[str, asyncio.Task] = {}
    cancelled: deque[str] = deque(maxlen=256)

    @web.middleware
    async def authenticate(request, handler):
        supplied = request.headers.get("Authorization", "")
        admin = hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode())
        receive = bool(receive_token) and hmac.compare_digest(supplied.encode(), f"Bearer {receive_token}".encode())
        if not admin and not receive:
            return web.json_response({"error": "Unauthorized"}, status=401)
        if not admin and not (request.path in {"/v1/devices", "/v1/sweep", "/v1/capture", "/v1/stop"} or request.path.startswith("/v1/captures/")):
            return web.json_response({"error": "Receive token cannot perform administrative actions"}, status=403)
        try:
            return await handler(request)
        except RadioBusy:
            return web.json_response({"error": "Radio busy or sweep cancelled"}, status=409)
        except RadioError as err:
            return web.json_response({"error": str(err)}, status=503)
        except (ValueError, TypeError, KeyError):
            return web.json_response({"error": "Invalid request"}, status=400)

    app = web.Application(middlewares=[authenticate], client_max_size=8192)

    def result(data: dict):
        return web.json_response(data | {"safety_policy": SAFETY_POLICY, "version": VERSION})

    async def devices(request):
        return result({"devices": await radio.devices()})

    def operation_id(data: dict) -> str:
        value = data["operation_id"]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError
        return value

    async def sweep(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"serial", "settings", "operation_id"}:
            raise ValueError
        operation = operation_id(data)
        if operation in active or operation in cancelled:
            raise RadioBusy
        task = asyncio.create_task(radio.sweep(data["serial"], data["settings"]))
        active[operation] = task
        try:
            return result(await task)
        except asyncio.CancelledError:
            raise RadioBusy from None
        finally:
            active.pop(operation, None)

    async def stop(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"operation_id"}:
            raise ValueError
        operation = operation_id(data)
        cancelled.append(operation)  # Also fence a stop arriving before its sweep.
        if task := active.get(operation):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return result({"stopped": True})

    async def cleanup(app):
        for task in list(active.values()):
            task.cancel()
        await asyncio.gather(*list(active.values()), return_exceptions=True)
        await radio.close()

    async def firmware(request):
        data = await request.json()
        action = request.match_info["action"]
        fields = {"status": {"serial"}, "prepare": {"serial", "target"},
                  "install": {"serial", "plan_id", "confirmation"}, "verify": {"serial"}}
        if not isinstance(data, dict) or action not in fields or set(data) != fields[action]:
            raise ValueError
        if action == "status":
            return result({"firmware": await radio.firmware_status(data["serial"])})
        if action == "prepare":
            return result({"plan": await radio.firmware_prepare(data["serial"], data["target"])})
        if action == "install":
            return result({"firmware": await radio.firmware_install(data["serial"], data["plan_id"], data["confirmation"])})
        return result({"firmware": await radio.firmware_verify(data["serial"])})

    async def applications(request):
        from .software import validate_action

        data = await request.json()
        action = request.match_info["action"]
        if action == "status":
            validate_action(action, data)
            return result({"applications": await radio.application_status()})
        if not isinstance(data, dict) or "serial" not in data:
            raise ValueError
        from .radio import validate_serial

        serial = validate_serial(data["serial"])
        values = {key: value for key, value in data.items() if key != "serial"}
        validate_action(action, values)
        return result({"applications": await radio.application_action(serial, action, values)})

    async def capture(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"serial", "settings", "operation_id"}:
            raise ValueError
        operation = operation_id(data)
        if operation in active or operation in cancelled:
            raise RadioBusy
        task = asyncio.create_task(radio.direct_capture(data["serial"], data["settings"]))
        active[operation] = task
        try:
            return result({"capture": await task})
        except asyncio.CancelledError:
            raise RadioBusy from None
        finally:
            active.pop(operation, None)

    async def capture_file(request):
        metadata, raw = await radio.capture_data(request.match_info["serial"], request.match_info["capture_id"])
        return web.Response(body=raw, content_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="hackrf-{metadata["capture_id"]}.cs8"',
            "X-HackRF-Format": "ci8", "X-HackRF-SHA256": metadata["sha256"],
        })

    app.router.add_get("/v1/devices", devices)
    app.router.add_post("/v1/sweep", sweep)
    app.router.add_post("/v1/stop", stop)
    app.router.add_post("/v1/firmware/{action}", firmware)
    app.router.add_post("/v1/applications/{action}", applications)
    app.router.add_post("/v1/capture", capture)
    app.router.add_get("/v1/captures/{serial}/{capture_id}", capture_file)
    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Receive-only HackRF bridge (amp and antenna power locked off)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--firmware-dir", type=Path, help="Persistent directory for firmware backup and update journals")
    parser.add_argument("--applications-dir", type=Path, help="Persistent app ownership, package plans, links and receive captures")
    parser.add_argument("--allow-software-install", action="store_true", help="Opt in to reviewed APT installation on a root Debian/Ubuntu USB host")
    parser.add_argument("--allow-app-launch", action="store_true", help="Opt in to fixed software launch profiles on this USB host")
    parser.add_argument("--soapy-bind", default="127.0.0.1", help="SoapyRemote bind IP: loopback by default; a trusted private VPN/LAN IP requires firewall protection")
    args = parser.parse_args()
    token = os.environ.get("HACKRF_BRIDGE_TOKEN", "")
    try:
        app = create_app(token, firmware_directory=args.firmware_dir, application_directory=args.applications_dir,
                         allow_install=args.allow_software_install, allow_launch=args.allow_app_launch,
                         receive_token=os.environ.get("HACKRF_RECEIVE_TOKEN", ""), soapy_bind=args.soapy_bind)
    except (ValueError, RadioError) as err:
        parser.error(str(err))
    web.run_app(app, host=args.host, port=args.port, access_log=None, handler_cancellation=True)


if __name__ == "__main__":
    main()
