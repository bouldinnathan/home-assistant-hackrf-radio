"""Reviewed software links, fixed package recipes and USB-host app ownership.

Installing/launching is opt-in on the USB host. An external ownership record
survives restarts and never expires into an automatic HA device probe.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import shutil
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from yarl import URL

from .activity import SECRET_NAME, ActivityLog, diagnostic_output
from .firmware import BLOCKING_PHASES, _durable_json
from .programs import load_profiles, result_data
from .radio import RadioBusy, RadioError, parse_info, validate_serial

APT_EXECUTABLE = "/usr/bin/apt-get"
SERVICE_POLICY = Path("/usr/sbin/policy-rc.d")
UNIT_DIRECTORY = Path("/etc/systemd/system")


@dataclass(frozen=True)
class Software:
    id: str
    name: str
    category: str
    description: str
    website: str
    install_url: str
    systems: tuple[str, ...]
    packages: tuple[str, ...] = ()
    executables: tuple[str, ...] = ()
    launch: tuple[str, ...] = ()
    desktop: bool = True


CATALOG = {app.id: app for app in (
    Software("external", "Other external app", "USB handoff", "Reserve USB for your own native SDR application or script; close it before returning control.",
             "https://github.com/bouldinnathan/home-assistant-hackrf-radio", "https://hackrf.readthedocs.io/en/latest/software_support.html", ("Linux", "Windows", "macOS")),
    Software("soapy_remote", "SoapyRemote", "Remote control", "Native SoapySDR clients can tune and stream from the USB host. Loopback is the default; private-network binding needs an explicit host setting and trusted firewall/VPN.",
             "https://github.com/pothosware/SoapyRemote", "https://github.com/pothosware/SoapyRemote/wiki", ("Linux", "Windows", "macOS"),
             ("soapyremote-server", "soapysdr-module-hackrf", "soapysdr-tools"), ("SoapySDRServer",), ("SoapySDRServer", "--bind=127.0.0.1:55132"), False),
    Software("gqrx", "Gqrx", "Receiver", "Desktop receiver with audio demodulation and spectrum display. Choose the configured HackRF serial in its input dialog.",
             "https://www.gqrx.dk/", "https://www.gqrx.dk/download", ("Linux", "macOS"), ("gqrx-sdr",), ("gqrx",), ("gqrx",)),
    Software("cubicsdr", "CubicSDR", "Receiver", "SoapySDR desktop receiver. Select the exact serial, or connect through SoapyRemote.",
             "https://github.com/cjcliffe/CubicSDR", "https://github.com/cjcliffe/CubicSDR/releases", ("Linux", "Windows", "macOS"),
             ("cubicsdr", "soapysdr-module-hackrf"), ("CubicSDR",), ("CubicSDR",)),
    Software("gnuradio", "GNU Radio Companion", "DSP development", "Build receive/DSP flowgraphs using gr-osmosdr or SoapySDR; also consumes exported I/Q captures.",
             "https://www.gnuradio.org/", "https://wiki.gnuradio.org/index.php/InstallingGR", ("Linux", "Windows", "macOS"),
             ("gnuradio", "gr-osmosdr", "soapysdr-module-hackrf"), ("gnuradio-companion",), ("gnuradio-companion",)),
    Software("inspectrum", "inspectrum", "Capture analysis", "Inspect exported signed 8-bit interleaved I/Q files (.cs8). Does not need a live USB device.",
             "https://github.com/miek/inspectrum", "https://github.com/miek/inspectrum#installation", ("Linux", "macOS"),
             ("inspectrum",), ("inspectrum",), ("inspectrum",)),
    Software("sdrpp", "SDR++", "Receiver", "Cross-platform receiver with native HackRF and remote-source modules; use the upstream build for your OS.",
             "https://github.com/AlexandreRouma/SDRPlusPlus", "https://github.com/AlexandreRouma/SDRPlusPlus/releases", ("Linux", "Windows", "macOS", "Android"),
             (), ("sdrpp",), ("sdrpp",)),
    Software("sdrangel", "SDRangel", "DSP and decoding", "Many demodulators and decoders, plus remote-device support. External app can also transmit; review its device settings.",
             "https://github.com/f4exb/sdrangel", "https://github.com/f4exb/sdrangel/releases", ("Linux", "Windows", "macOS"),
             (), ("sdrangel",), ("sdrangel",)),
    Software("satdump", "SatDump", "Satellite reception", "Satellite receive and processing pipelines with HackRF support in appropriate builds.",
             "https://github.com/SatDump/SatDump", "https://github.com/SatDump/SatDump/releases", ("Linux", "Windows", "macOS", "Android"),
             (), ("satdump-ui",), ("satdump-ui",)),
    Software("sigdigger", "SigDigger", "Signal analysis", "Digital signal analyzer; HackRF live input uses SoapySDR/SoapyHackRF.",
             "https://github.com/BatchDrake/SigDigger", "https://github.com/BatchDrake/SigDigger/releases", ("Linux", "Windows", "macOS"),
             (), ("SigDigger",), ("SigDigger",)),
    Software("urh", "Universal Radio Hacker", "Protocol analysis", "Inspect receive captures and wireless protocols. Has transmit features outside this integration's receive safeguards.",
             "https://github.com/jopohl/urh", "https://github.com/jopohl/urh#installation", ("Linux", "Windows", "macOS"),
             (), ("urh",), ("urh",)),
    Software("qspectrumanalyzer", "QSpectrumAnalyzer", "Spectrum analysis", "Desktop spectrum plots using hackrf_sweep and other backends. Review current upstream installation dependencies.",
             "https://github.com/xmikos/qspectrumanalyzer", "https://github.com/xmikos/qspectrumanalyzer#installation", ("Linux", "Windows", "macOS"),
             (), ("qspectrumanalyzer",), ("qspectrumanalyzer",)),
    Software("hackrf_analyzer", "HackRF Spectrum Analyzer", "Spectrum analysis", "Third-party desktop spectrum analyzer built around HackRF sweep mode.",
             "https://github.com/pavsa/hackrf-spectrum-analyzer", "https://github.com/pavsa/hackrf-spectrum-analyzer/releases", ("Linux", "Windows")),
    Software("sdrsharp", "SDR#", "Receiver", "Windows receiver. HackRF support depends on the selected build and device plugin; check upstream compatibility.",
             "https://airspy.com/", "https://airspy.com/download/", ("Windows",)),
    Software("sdr_console", "SDR Console", "Receiver", "Windows SDR suite; follow the vendor's HackRF device setup instructions.",
             "https://www.sdr-radio.com/Console", "https://www.sdr-radio.com/download", ("Windows",)),
    Software("radioconda", "radioconda", "Software distribution", "Cross-platform installer for a software-radio environment, including GNU Radio and SDR components.",
             "https://github.com/radioconda/radioconda-installer", "https://github.com/radioconda/radioconda-installer/releases", ("Linux", "Windows", "macOS")),
    Software("gr_air_modes", "gr-air-modes", "ADS-B decoding", "GNU Radio Mode-S/ADS-B tools with source integration described upstream.",
             "https://github.com/bistromath/gr-air-modes", "https://github.com/bistromath/gr-air-modes#readme", ("Linux", "macOS")),
    Software("mayhem_hub", "Mayhem Hub", "PortaPack tools", "Browser tools for PortaPack/Mayhem. Requires its normal USB-console mode, not HackRF SDR mode.",
             "https://hackrf.app/", "https://github.com/portapack-mayhem/mayhem-firmware/wiki", ("Linux", "Windows", "macOS")),
)}

ACTION_FIELDS = {"status": set(), "handoff": {"app_id", "confirmed"}, "release": {"confirmed_closed"},
                 "launch": {"app_id", "confirmed"}, "stop": set(), "link": {"app_id", "url"},
                 "prepare_install": {"apps"}, "install": {"plan_id", "confirmed"},
                 "recover": {"lease_id", "confirmed_closed", "stop_managed_program"}}


def validate_action(action: str, values: dict) -> None:
    if not isinstance(action, str) or action not in ACTION_FIELDS or not isinstance(values, dict) or set(values) != ACTION_FIELDS[action]:
        raise RadioError("Application request fields do not match its action")


def software_for(app_id: str) -> Software:
    if not isinstance(app_id, str) or app_id not in CATALOG:
        raise RadioError("Choose an application from the reviewed software catalog")
    return CATALOG[app_id]


def package_recipe(app_ids: list[str]) -> list[str]:
    if (not isinstance(app_ids, list) or not 1 <= len(app_ids) <= 8
            or not all(isinstance(app_id, str) for app_id in app_ids) or len(set(app_ids)) != len(app_ids)):
        raise RadioError("Choose one to eight different applications")
    packages = set()
    for app_id in app_ids:
        app = software_for(app_id)
        if not app.packages:
            raise RadioError(f"{app.name} uses its upstream download/install guide, not the managed APT installer")
        packages.update(app.packages)
    return sorted(packages)


def apt_commands(app_ids: list[str]) -> list[list[str]]:
    packages = package_recipe(app_ids)
    return [[APT_EXECUTABLE, "update"],
            [APT_EXECUTABLE, "install", "-y", "--no-remove", "--no-install-recommends", "--", *packages]]


def link_url(value: str) -> str:
    if value == "":
        return ""
    try:
        url = URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.user is not None or url.query_string or url.fragment:
            raise ValueError
        _ = url.port
    except (TypeError, ValueError) as err:
        raise RadioError("Use an HTTP(S) app URL without credentials, query string or fragment") from err
    return str(url)


def soapy_bind_address(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as err:
        raise RadioError("SoapyRemote bind must be a literal loopback or RFC1918 IPv4 address") from err
    if not any(address in ipaddress.IPv4Network(network) for network in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise RadioError("SoapyRemote does not accept public, wildcard or multicast bind addresses")
    return str(address)


def install_available(allowed: bool) -> bool:
    return (allowed and Path(APT_EXECUTABLE).is_file() and os.geteuid() == 0
            and not os.statvfs("/").f_flag & os.ST_RDONLY)


async def run_installer(commands: list[list[str]], activity: ActivityLog | None = None) -> None:
    """Package writes finish independently of the requesting browser."""
    environment = os.environ.copy()
    environment["DEBIAN_FRONTEND"] = "noninteractive"
    policy = SERVICE_POLICY
    created_policy = False
    # Prevent package post-install scripts from starting an unmanaged SDR
    # service. Existing administrator policies are never overwritten.
    if policy.exists():
        lines = [line.strip() for line in policy.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if lines != ["exit 101"]:
            raise RadioError("An existing service-start policy needs manual review before managed installation")
    else:
        with policy.open("x") as output:
            output.write("#!/bin/sh\nexit 101\n")
        policy.chmod(0o755)
        created_policy = True
    try:
        if any("soapyremote-server" in command for command in commands):
            for unit in ("soapyremote-server.service", "SoapySDRServer.service"):
                mask = UNIT_DIRECTORY / unit
                mask.parent.mkdir(parents=True, exist_ok=True)
                if mask.is_symlink() and mask.readlink() == Path("/dev/null"):
                    pass
                elif mask.exists() or mask.is_symlink():
                    raise RadioError("SoapySDRServer has an existing service configuration; stop and manage it manually")
                else:
                    mask.symlink_to("/dev/null")
        await _run_packages(commands, environment, activity)
        if any("soapyremote-server" in command for command in commands):
            for unit in ("soapyremote-server.service", "SoapySDRServer.service"):
                mask = UNIT_DIRECTORY / unit
                if not mask.is_symlink() or mask.readlink() != Path("/dev/null"):
                    raise RadioError("Packaged SoapyRemote service did not retain its ownership-protection mask; inspect systemd manually")
    finally:
        if created_policy:
            policy.unlink()


async def _run_packages(commands: list[list[str]], environment: dict, activity: ActivityLog | None = None) -> None:
    for command in commands:
        if activity:
            await activity.record("packages.step_started", "Running reviewed package-manager step", details={"step": command[1]})
        process = await asyncio.create_subprocess_exec(*command, env=environment,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        tail = bytearray()
        truncated = False
        async def drain(child=process, output=tail):
            nonlocal truncated
            while chunk := await child.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 1000:
                    truncated = True
                    del output[:-1000]
            return await child.wait()
        task = asyncio.create_task(drain())
        while True:
            try:
                code = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    task = asyncio.create_task(drain())
        if code:
            if activity:
                await activity.record("packages.tool_failed", diagnostic_output(tail, truncated), level="error",
                                      details={"step": command[1], "returncode": code})
            raise RadioError("Package installation failed; inspect APT/dpkg on the USB host before retrying")


class Applications:
    def __init__(self, lock: asyncio.Lock, directory: Path, *, allow_install: bool = False, allow_launch: bool = False,
                 soapy_bind: str = "127.0.0.1", profiles_path: Path | None = None, activity: ActivityLog | None = None,
                 profiles_required: bool = False) -> None:
        self.lock = lock
        self.directory = Path(directory)
        self.allow_install = allow_install
        self.allow_launch = allow_launch
        self.soapy_bind = soapy_bind_address(soapy_bind)
        self._process = None
        self._plan = None
        self._install_job = None
        self._install_state = None
        self.profiles_path = profiles_path
        self.activity = activity or ActivityLog(self.directory.parent / "activity.sqlite")
        self._profiles = {}
        self._watcher = None
        self._launch_lock = asyncio.Lock()
        self._running_app = None
        self._last_exit = None
        self._result = None
        self._stop_job = None
        self._profile_error = None
        self.profiles_required = profiles_required
        self._recovery_job = None

    async def _catalog(self):
        try:
            self._profiles = await asyncio.to_thread(load_profiles, self.profiles_path, self.profiles_required)
            self._profile_error = None
        except RadioError as err:
            self._profiles = {}
            self._profile_error = str(err)
            await self.activity.record("app.profiles_invalid", self._profile_error, level="warning", deduplicate=True)
        return CATALOG | {identifier: Software(identifier, profile["name"], "Your programs", profile["description"],
                                               "", "", ("USB host",), (), (profile["argv"][0],), ("host-configured",), profile["desktop"])
                          for identifier, profile in self._profiles.items()}

    async def _software_for(self, app_id):
        catalog = await self._catalog()
        if not isinstance(app_id, str) or app_id not in catalog:
            if isinstance(app_id, str) and app_id.startswith("user_") and self._profile_error:
                raise RadioError(self._profile_error)
            raise RadioError("Choose a reviewed app or a program configured on the USB host")
        return catalog[app_id]

    async def _read(self, name: str, default: dict) -> dict:
        def read():
            try:
                value = json.loads((self.directory / name).read_text())
                if not isinstance(value, dict):
                    raise ValueError
                return value
            except FileNotFoundError:
                return default.copy()
            except (ValueError, OSError):
                raise RadioBusy("Application ownership/storage is unreadable; inspect the USB host before using the radio") from None
        return await asyncio.to_thread(read)

    async def _save(self, name: str, value: dict) -> None:
        await asyncio.to_thread(_durable_json, self.directory / name, value)

    async def gate(self) -> None:
        owner = await self._read("owner.json", {"phase": "home_assistant"})
        install = self._install_state or await self._read("install.json", {"phase": "idle"})
        if owner.get("phase") != "home_assistant":
            raise RadioBusy("External application owns this USB host; close it and explicitly return control to Home Assistant")
        if install.get("phase") not in {"idle", "complete"}:
            raise RadioBusy("Software maintenance blocks USB access; finish or inspect the package installation")

    async def status(self) -> dict:
        owner = await self._read("owner.json", {"phase": "home_assistant"})
        install = self._install_state or await self._read("install.json", {"phase": "idle"})
        if install.get("phase") == "installing" and self._install_job is None:
            install = install | {"phase": "recovery_required", "message": "Previous package job was interrupted; inspect APT/dpkg manually"}
        if install.get("phase") not in {"idle", "complete", "installing", "recovery_required"}:
            install = {"phase": "recovery_required", "message": "Package status is invalid; inspect persistent application storage"}
        try:
            links = await self._read("links.json", {})
        except RadioError:
            links = {}
            await self.activity.record("app.links_unreadable", "Saved app links are unreadable; inspect application storage", level="warning", deduplicate=True)
        try:
            result = self._result or await self._read("result.json", {})
        except RadioError:
            result = {}
            await self.activity.record("app.result_unreadable", "Saved custom measurements are unreadable; inspect application storage", level="warning", deduplicate=True)
        software = await self._catalog()
        def catalog():
            entries = []
            for app in software.values():
                custom = app.id in self._profiles
                installed = bool(app.executables) and all(shutil.which(name) for name in app.executables)
                reason = ("Use this application's own launcher" if not app.launch else
                          "Program launch is not enabled on this USB host" if not (self.allow_launch or custom) else
                          "Install the executable on the USB host first" if not installed else
                          "Custom programs require an unprivileged USB-host account" if custom and os.geteuid() == 0 else
                          "Desktop launch requires an unprivileged graphical host session" if app.desktop and (os.geteuid() == 0 or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))) else "")
                entries.append(asdict(app) | {"installed": installed, "web_url": links.get(app.id, ""), "custom": custom,
                                             "access": self._profiles.get(app.id, {}).get("access", "usb"),
                                             "launch_enabled": not bool(reason), "launch_reason": reason})
            return entries
        return {"owner": owner, "installer": install, "catalog": await asyncio.to_thread(catalog),
                "install_enabled": install_available(self.allow_install),
                "launch_enabled": self.allow_launch or bool(self._profiles),
                "soapy_bind": self.soapy_bind,
                "running": self._process is not None and self._process.returncode is None,
                "running_app": self._running_app, "last_exit": self._last_exit,
                "profile_error": self._profile_error,
                "result": result,
                "plan": self._plan.copy() if self._plan else None}

    async def _healthy_device(self, radio, serial: str):
        if radio._firmware:
            state = await radio._firmware.status(serial)
            if state["phase"] in BLOCKING_PHASES:
                raise RadioBusy("Finish firmware maintenance before handing over the device")
        devices = parse_info(await radio._run(["hackrf_info"]))
        device = next((device for device in devices if device.serial == serial), None)
        if device is None or not device.supported:
            raise RadioError("The selected healthy HackRF is not accessible; ownership was not changed")
        return device

    async def acquire(self, radio, serial: str, app_id: str, confirmed: bool) -> dict:
        async with self._launch_lock:
            return await self._acquire(radio, serial, app_id, confirmed)

    async def _acquire(self, radio, serial: str, app_id: str, confirmed: bool) -> dict:
        serial, app = validate_serial(serial), await self._software_for(app_id)
        if confirmed is not True:
            raise RadioError("Confirm that the external app controls its own RF settings and that other SDR apps are closed")
        if self.lock.locked() or radio._closed:
            raise RadioBusy("USB host is busy or closed")
        async with self.lock:
            await self.gate()
            if radio._firmware and await asyncio.to_thread(self._firmware_blocked, radio._firmware.directory):
                raise RadioBusy("Finish firmware maintenance on this host before using external apps")
            await self._healthy_device(radio, serial)
            await self._save("owner.json", {"phase": "external", "serial": serial, "app_id": app.id,
                                             "name": app.name, "lease_id": uuid4().hex, "since": time.time()})
        await self.activity.record("app.handoff", "USB control handed to external software", details={"app": app.id})
        return await self.status()

    async def release(self, radio, serial: str, confirmed_closed: bool) -> dict:
        async with self._launch_lock:
            return await self._release(radio, serial, confirmed_closed)

    async def _release(self, radio, serial: str, confirmed_closed: bool) -> dict:
        serial = validate_serial(serial)
        if confirmed_closed is not True:
            raise RadioError("Close the external app, then confirm it has released USB before returning control")
        if self._process is not None and self._process.returncode is None:
            raise RadioBusy("Stop the managed app before returning control")
        if self.lock.locked():
            raise RadioBusy("USB host is busy")
        async with self.lock:
            owner = await self._read("owner.json", {"phase": "home_assistant"})
            if owner.get("phase") == "home_assistant":
                return await self.status()
            if owner.get("serial") != serial:
                raise RadioError("Only the radio which owns this handoff can return control")
            await self._healthy_device(radio, serial)
            await self._save("owner.json", {"phase": "home_assistant"})
        await self.activity.record("app.release", "USB control returned after device identity and health check")
        return await self.status()

    async def recover(self, radio, serial, lease_id, confirmed_closed, stop_managed_program):
        serial = validate_serial(serial)
        if confirmed_closed is not True or type(stop_managed_program) is not bool or not isinstance(lease_id, str):
            raise RadioError("Confirm the reviewed ownership and closed external USB connections")
        if self._recovery_job and not self._recovery_job.done():
            raise RadioBusy("USB ownership recovery is already in progress")

        async def recover_owned():
            try:
                async with self._launch_lock:
                    current = await self._read("owner.json", {"phase": "home_assistant"})
                    if current.get("phase") != "external" or current.get("serial") != serial or current.get("lease_id") != lease_id:
                        raise RadioBusy("USB ownership changed; review the current owner again")
                    if stop_managed_program:
                        await self._stop()
                    return await self._release(radio, serial, True)
            except Exception as err:
                await self.activity.record("app.recovery_failed", str(err) if isinstance(err, RadioError) else "USB recovery did not finish; inspect current ownership and host storage",
                                           level="warning", details={"exception": type(err).__name__})
                raise

        self._recovery_job = asyncio.create_task(recover_owned())
        # Retain cleanup after a disconnected setup browser, and consume a
        # background failure even if that browser is no longer awaiting it.
        self._recovery_job.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return await asyncio.shield(self._recovery_job)

    async def launch(self, radio, serial: str, app_id: str, confirmed: bool) -> dict:
        async with self._launch_lock:
            return await self._launch(radio, serial, app_id, confirmed)

    async def _launch(self, radio, serial: str, app_id: str, confirmed: bool) -> dict:
        serial = validate_serial(serial)
        app = await self._software_for(app_id)
        profile = self._profiles.get(app_id)
        if not (self.allow_launch or profile) or not app.launch:
            raise RadioError("App launching is disabled on this USB host or this app uses its upstream launcher")
        if self._process is not None and self._process.returncode is None:
            raise RadioBusy("Stop the current managed program before starting another")
        if self._watcher:
            await self._watcher
        if profile and os.geteuid() == 0:
            raise RadioError("User programs must run under an unprivileged USB-host account")
        executable = shutil.which(profile["argv"][0] if profile else app.launch[0])
        if not executable:
            raise RadioError("Install the application on the USB host before launching it")
        if app.desktop and (os.geteuid() == 0 or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))):
            raise RadioError("Desktop apps need an unprivileged USB-host desktop session; HA OS/headless containers do not provide one")
        if profile and profile["cwd"] is not None and not await asyncio.to_thread(Path(profile["cwd"]).is_dir):
            raise RadioError("The configured program working directory is absent; USB ownership was not changed")
        if confirmed is not True:
            raise RadioError("Confirm the configured program and its RF settings before launch")
        if not profile or profile["access"] == "usb":
            await self._acquire(radio, serial, app_id, confirmed)
        else:
            # Offline/API programs may coexist with HA receive work, but never
            # start during firmware or package maintenance.
            await self.gate()
            if radio._closed or self.lock.locked() or (radio._firmware and await asyncio.to_thread(self._firmware_blocked, radio._firmware.directory)):
                raise RadioBusy("Finish USB/firmware maintenance before starting this program")
        # Preserve normal desktop configuration, but never hand the bridge token
        # or Supervisor credentials to a third-party program.
        allowed_environment = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "DISPLAY", "WAYLAND_DISPLAY",
                               "DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER", "QT_QPA_PLATFORM", "LD_LIBRARY_PATH", "VIRTUAL_ENV", "CONDA_PREFIX"}
        environment = {key: value for key, value in os.environ.items()
                       if (key in allowed_environment or key.startswith(("LC_", "XDG_"))) and not SECRET_NAME.search(key)}
        environment["HACKRF_SERIAL"] = serial
        environment["PYTHONUNBUFFERED"] = "1"
        try:
            args = ([arg.replace("{serial}", serial) for arg in profile["argv"][1:]] if profile else
                    (f"--bind={self.soapy_bind}:55132",) if app.id == "soapy_remote" else app.launch[1:])
            self._process = await asyncio.create_subprocess_exec(executable, *args, env=environment,
                                                                 cwd=profile["cwd"] if profile else None,
                                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                                 start_new_session=True)
        except OSError as err:
            # Retain ownership until explicit release, even on uncertain startup.
            raise RadioError("Application failed to launch; check the USB-host session, then return control explicitly") from err
        self._running_app, self._last_exit = app.id, None
        self._watcher = asyncio.create_task(self._watch(self._process, app.id, bool(profile and profile["publish_results"]), serial))
        await self.activity.record("app.started", "Managed program started on the USB host", details={"app": app.id})
        return await self.status()

    async def _watch(self, process, app_id, publish_results, serial):
        async def drain(stream, channel):
            pending = bytearray()
            count, window, dropped = 0, time.monotonic(), 0
            while chunk := await stream.read(4096):
                pending.extend(chunk)
                while b"\n" in pending or len(pending) >= 4096:
                    end = pending.find(b"\n")
                    if end < 0 or end > 4096:
                        end = 4096
                    raw = bytes(pending[:end])
                    del pending[:end + (1 if pending[end:end + 1] == b"\n" else 0)]
                    # Never log fragments of an overlong line, which could split
                    # a secret across the redaction boundary.
                    if end == 4096:
                        raw = b"[overlong program output omitted]"
                        while b"\n" not in pending:
                            more = await stream.read(4096)
                            if not more:
                                pending.clear()
                                break
                            pending = bytearray(more)
                        if b"\n" in pending:
                            del pending[:pending.index(b"\n") + 1]
                    if time.monotonic() - window >= 10:
                        if dropped:
                            await self.activity.record("app.output_limited", "Program output was rate limited", level="warning",
                                                       details={"app": app_id, "omitted_lines": dropped})
                        count, dropped, window = 0, 0, time.monotonic()
                    if count >= 20:
                        dropped += 1
                        continue
                    count += 1
                    text = raw.decode("utf-8", errors="replace")
                    if publish_results and channel == "stdout" and text.lstrip().startswith("{") and "hackrf_radio_result" in text:
                        try:
                            data = json.loads(text)
                            if not isinstance(data, dict) or set(data) != {"hackrf_radio_result"}:
                                raise ValueError
                            await self.publish_result(serial, app_id, data["hackrf_radio_result"])
                            continue
                        except (ValueError, TypeError, RadioError, OSError):
                            await self.activity.record("app.result_rejected", "Program result was invalid or could not be persisted; inspect the program and host storage", level="warning", deduplicate=True)
                    await self.activity.record("app.output", text, level="debug", details={"app": app_id, "stream": channel})
            if pending:
                # A final unterminated line is diagnostic output, never a result.
                await self.activity.record("app.output", pending.decode("utf-8", errors="replace"), level="debug", details={"app": app_id, "stream": channel})
            if dropped:
                await self.activity.record("app.output_limited", "Program output was rate limited", level="warning", details={"app": app_id, "omitted_lines": dropped})
        readers = []
        try:
            readers = [asyncio.create_task(drain(process.stdout, "stdout")), asyncio.create_task(drain(process.stderr, "stderr"))]
            # A daemonized grandchild can hold stdout open after its parent
            # exits. Never let that prevent status, stop or the next launch.
            while process.returncode is None:
                await asyncio.sleep(0.05)
            code = process.returncode
            try:
                await asyncio.wait_for(asyncio.gather(*readers), timeout=3)
            except TimeoutError:
                await self.activity.record("app.open_pipes", "Program exited with inherited output pipes still open; inspect descendant processes before returning USB", level="warning")
                # asyncio streams expose no public close method. The leader has
                # already exited; close only its remaining pipe transports.
                transport = getattr(process, "_transport", None)
                if transport:
                    transport.close()
            self._last_exit = {"app_id": app_id, "returncode": code, "at": time.time()}
            await self.activity.record("app.exited", "Managed program exited; USB handoff requires explicit return",
                                       level="info" if code == 0 else "warning", details={"app": app_id, "returncode": code})
        except Exception:
            await self.activity.record("app.output_error", "Program output monitor failed; stop the managed program and inspect the host", level="error")
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    async def publish_result(self, serial, app_id, data):
        from datetime import UTC, datetime

        result = {"id": uuid4().hex, "serial": validate_serial(serial), "source": app_id,
                  "timestamp": datetime.now(UTC).isoformat(), "data": result_data(data, self.activity.secrets)}
        await self._save("result.json", result)
        self._result = result
        await self.activity.record("app.result", "Program published measurements", details={"app": app_id})

    async def stop(self) -> dict:
        if self._stop_job is None or self._stop_job.done():
            async def stop_owned():
                async with self._launch_lock:
                    return await self._stop()
            self._stop_job = asyncio.create_task(stop_owned())
        return await asyncio.shield(self._stop_job)

    async def _stop(self) -> dict:
        if self._process is not None and self._process.returncode is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self._process.wait()
            except ProcessLookupError:
                await self._process.wait()
        if self._watcher:
            try:
                await asyncio.wait_for(asyncio.shield(self._watcher), timeout=3)
            except TimeoutError:
                self._watcher.cancel()
                await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
        # Do not automatically return USB or resume monitoring after exit.
        return await self.status()

    async def save_link(self, app_id: str, value: str) -> dict:
        await self._software_for(app_id)
        url = link_url(value)
        links = await self._read("links.json", {})
        if url:
            links[app_id] = url
        else:
            links.pop(app_id, None)
        await self._save("links.json", links)
        return await self.status()

    async def prepare_install(self, app_ids: list[str]) -> dict:
        commands = apt_commands(app_ids)
        self._plan = {"plan_id": uuid4().hex, "apps": app_ids.copy(), "commands": commands,
                      "packages": package_recipe(app_ids), "expires_at": time.time() + 600,
                      "service_policy": "Packaged services are not started; SoapySDRServer.service is masked so only explicitly managed launches own USB"}
        return await self.status()

    async def install(self, radio, plan_id: str, confirmed: bool) -> dict:
        if not install_available(self.allow_install):
            raise RadioError("Managed install requires --allow-software-install on a writable root Debian/Ubuntu USB-host bridge; use the reviewed CLI recipe or tools image otherwise")
        if (not self._plan or self._plan["plan_id"] != plan_id or self._plan["expires_at"] < time.time() or confirmed is not True):
            raise RadioError("Review a current package plan and confirm USB-host software installation")
        if self.lock.locked() or radio._closed:
            raise RadioBusy("USB host is busy or closed")
        await self.lock.acquire()
        try:
            await self.gate()
            if radio._firmware and await asyncio.to_thread(self._firmware_blocked, radio._firmware.directory):
                raise RadioBusy("Finish firmware maintenance before installing software")
            commands = apt_commands(self._plan["apps"])
            self._plan = None
            self._install_state = {"phase": "installing", "message": "Installing reviewed packages on the USB host"}
            await self._save("install.json", self._install_state)
            self._install_job = asyncio.create_task(self._install(commands))
        except BaseException:
            self.lock.release()
            raise
        return await self.status()

    @staticmethod
    def _firmware_blocked(directory: Path) -> bool:
        for path in directory.glob("*/state.json"):
            try:
                if json.loads(path.read_text()).get("phase") in BLOCKING_PHASES:
                    return True
            except (OSError, ValueError):
                return True
        return False

    async def _install(self, commands) -> None:
        try:
            await self.activity.record("packages.started", "Installing reviewed packages; USB maintenance lock held")
            await run_installer(commands, self.activity)
            self._install_state = {"phase": "complete", "message": "Package installation finished; applications were not started"}
            await self.activity.record("packages.completed", "Reviewed software installation finished")
        except Exception:
            self._install_state = {"phase": "recovery_required", "message": "Inspect APT/dpkg on the USB host before resuming radio use"}
            await self.activity.record("packages.failed", "Package installation failed; manual package-manager inspection required", level="error")
        except asyncio.CancelledError:
            self._install_state = {"phase": "recovery_required", "message": "Inspect APT/dpkg on the USB host before resuming radio use"}
            await self.activity.record("packages.interrupted", "Package job interrupted outside an active package write; inspect the host", level="error")
        finally:
            try:
                await self._save("install.json", self._install_state)
            finally:
                self._install_job = None
                self.lock.release()

    async def close(self) -> None:
        while self._recovery_job and not self._recovery_job.done():
            try:
                await asyncio.shield(self._recovery_job)
            except asyncio.CancelledError:
                continue
            except RadioError:
                break
        while self._install_job:
            try:
                await asyncio.shield(self._install_job)
            except asyncio.CancelledError:
                continue
        try:
            await self.stop()
        except RadioError:
            # Stop already reaped the owned process before building status.
            # A malformed edited profile or unreadable catalog must not make
            # normal host shutdown fail after that cleanup.
            await self.activity.record("app.shutdown_status", "Managed process cleanup finished; application status needs USB-host inspection", level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reviewed HackRF software package installer (Debian/Ubuntu)")
    parser.add_argument("action", choices=["catalog", "plan", "install"])
    parser.add_argument("--apps", default="soapy_remote,inspectrum", help="Comma-separated reviewed application IDs")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if args.action == "catalog":
        print(json.dumps([asdict(app) for app in CATALOG.values()], indent=2))
        return
    try:
        commands = apt_commands(args.apps.split(","))
        if args.action == "plan":
            print(json.dumps(commands, indent=2))
            return
        if not args.confirm or os.geteuid() != 0 or not Path(APT_EXECUTABLE).is_file():
            parser.error("Installation needs Debian/Ubuntu root and --confirm after reviewing the plan")
        asyncio.run(run_installer(commands))
    except RadioError as err:
        parser.error(str(err))


if __name__ == "__main__":
    main()
