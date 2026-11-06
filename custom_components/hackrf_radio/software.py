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

from .firmware import BLOCKING_PHASES, _durable_json
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
                 "prepare_install": {"apps"}, "install": {"plan_id", "confirmed"}}


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


async def run_installer(commands: list[list[str]]) -> None:
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
        await _run_packages(commands, environment)
        if any("soapyremote-server" in command for command in commands):
            for unit in ("soapyremote-server.service", "SoapySDRServer.service"):
                mask = UNIT_DIRECTORY / unit
                if not mask.is_symlink() or mask.readlink() != Path("/dev/null"):
                    raise RadioError("Packaged SoapyRemote service did not retain its ownership-protection mask; inspect systemd manually")
    finally:
        if created_policy:
            policy.unlink()


async def _run_packages(commands: list[list[str]], environment: dict) -> None:
    for command in commands:
        process = await asyncio.create_subprocess_exec(*command, env=environment,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async def drain(child=process):
            while await child.stdout.read(65536):
                pass
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
            raise RadioError("Package installation failed; inspect APT/dpkg on the USB host before retrying")


class Applications:
    def __init__(self, lock: asyncio.Lock, directory: Path, *, allow_install: bool = False, allow_launch: bool = False,
                 soapy_bind: str = "127.0.0.1") -> None:
        self.lock = lock
        self.directory = Path(directory)
        self.allow_install = allow_install
        self.allow_launch = allow_launch
        self.soapy_bind = soapy_bind_address(soapy_bind)
        self._process = None
        self._plan = None
        self._install_job = None
        self._install_state = None

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
        links = await self._read("links.json", {})
        def catalog():
            return [asdict(app) | {"installed": bool(app.executables) and all(shutil.which(name) for name in app.executables),
                                   "web_url": links.get(app.id, "")} for app in CATALOG.values()]
        return {"owner": owner, "installer": install, "catalog": await asyncio.to_thread(catalog),
                "install_enabled": install_available(self.allow_install),
                "launch_enabled": self.allow_launch,
                "soapy_bind": self.soapy_bind,
                "running": self._process is not None and self._process.returncode is None,
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
        serial, app = validate_serial(serial), software_for(app_id)
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
        return await self.status()

    async def release(self, radio, serial: str, confirmed_closed: bool) -> dict:
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
        return await self.status()

    async def launch(self, radio, serial: str, app_id: str, confirmed: bool) -> dict:
        app = software_for(app_id)
        if not self.allow_launch or not app.launch:
            raise RadioError("App launching is disabled on this USB host or this app uses its upstream launcher")
        executable = shutil.which(app.launch[0])
        if not executable:
            raise RadioError("Install the application on the USB host before launching it")
        if app.desktop and (os.geteuid() == 0 or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))):
            raise RadioError("Desktop apps need an unprivileged USB-host desktop session; HA OS/headless containers do not provide one")
        await self.acquire(radio, serial, app_id, confirmed)
        # Preserve normal desktop configuration, but never hand the bridge token
        # or Supervisor credentials to a third-party program.
        environment = {key: value for key, value in os.environ.items()
                       if not any(word in key.upper() for word in ("TOKEN", "PASSWORD", "SECRET", "API_KEY"))}
        try:
            args = (f"--bind={self.soapy_bind}:55132",) if app.id == "soapy_remote" else app.launch[1:]
            self._process = await asyncio.create_subprocess_exec(executable, *args, env=environment,
                                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                                                 start_new_session=True)
        except OSError as err:
            # Retain ownership until explicit release, even on uncertain startup.
            raise RadioError("Application failed to launch; check the USB-host session, then return control explicitly") from err
        return await self.status()

    async def stop(self) -> dict:
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
        # Do not automatically return USB or resume monitoring after exit.
        return await self.status()

    async def save_link(self, app_id: str, value: str) -> dict:
        software_for(app_id)
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
            await run_installer(commands)
            self._install_state = {"phase": "complete", "message": "Package installation finished; applications were not started"}
        except (OSError, RadioError):
            self._install_state = {"phase": "recovery_required", "message": "Inspect APT/dpkg on the USB host before resuming radio use"}
        finally:
            try:
                await self._save("install.json", self._install_state)
            finally:
                self._install_job = None
                self.lock.release()

    async def close(self) -> None:
        while self._install_job:
            try:
                await asyncio.shield(self._install_job)
            except asyncio.CancelledError:
                continue
        await self.stop()


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
