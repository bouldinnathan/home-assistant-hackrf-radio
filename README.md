# HackRF Radio for Home Assistant

A HACS custom integration for **receive-only spectrum monitoring** with a HackRF One or compatible HackRF Pro. It follows the config-entry, custom sidebar panel, WebSocket and entity patterns in [your MeshNet integration](https://github.com/bouldinnathan/home-assistant-mesh-radio).

The **HackRF Radio** sidebar includes spectrum/waterfall views, band/gain controls, periodic monitoring, reviewed firmware maintenance, an **Apps & your code** workspace, **Connecting & integrations** instructions, **Activity & logs**, and a software library. External apps can use a scoped receive API, explicitly take USB ownership, or use an optional managed SoapyRemote server. Native HA entities provide peak level, peak frequency, median noise floor, bin count, last sweep, connectivity, signal threshold, a monitoring switch, a sweep button, an application-owner sensor and a custom-result sensor.

## Device protection

**Software cannot guarantee protection against excessive RF input, external DC, ESD, faulty hardware or modified firmware.** Great Scott Gadgets specifies a **−5 dBm maximum input for HackRF One**. Use suitable external attenuation, filtering and DC isolation for the actual connected equipment, and keep the receiver away from strong nearby transmitters. A displayed FFT level is relative, uncalibrated **dB**, not an input-power measurement in dBm. It cannot enforce that physical limit.

This version enforces the following in both the integration and USB bridge:

- The RF amplifier and antenna bias power are **locked OFF**, explicitly passed as `-a 0 -p 0` on every sweep. Only IF/LNA and baseband/VGA receive gains are adjustable.
- Receiving uses device information, tool help and one-shot `hackrf_sweep` commands. Separate firmware maintenance permits only reviewed, confirmed `hackrf_spiflash` backup/write/readback operations. There are no transmit, replay, DFU, reset, clock or arbitrary register-write endpoints.
- The complete hardware serial is mandatory. Missing devices never fall back to the first USB device; duplicate serials are rejected.
- Reported incompatible firmware, failed self-tests, unsupported boards, old sweep APIs and missing CLI safety flags block scanning. Firmware writes require an explicit reviewed plan, exact serial confirmation and working persistent backup storage. Downgrades, family conversion and automatic recovery are blocked.
- One command at a time runs on a USB host. Captures have time and output limits, and cancellation signals and reaps the subprocess.
- Monitoring starts off after setup/restart, stops on errors, and stops when settings change. A recovered connection does not automatically arm the receiver.

Read [the safety rationale, compatibility notes and upstream GitHub references](docs/DEVICE_SAFETY.md) before connecting RF equipment.

## Installation

Home Assistant **2025.1.4 or later** is required. The software is tested against 2025.1.4 and 2026.7.4. Physical hardware has not been available for this initial implementation; compatibility decisions are covered by simulated upstream tool output and actual subprocess/HTTP tests.

### HACS

Repository: [bouldinnathan/home-assistant-hackrf-radio](https://github.com/bouldinnathan/home-assistant-hackrf-radio). Add this URL to HACS as a custom repository.

1. In HACS, open **Custom repositories** and add `https://github.com/bouldinnathan/home-assistant-hackrf-radio` as **Integration**.
2. Download **HackRF Radio** and restart Home Assistant.
3. Start the USB bridge below if using HA OS.
4. Open **Settings → Devices & services → Add integration → HackRF Radio**.
5. Choose the connection, select the exact serial and acknowledge the RF-input precautions.
6. Open **HackRF Radio** in the sidebar and select **Sweep now**. Enable monitoring when ready.

### Manual

Copy `custom_components/hackrf_radio` into `/config/custom_components/hackrf_radio`, restart HA, then add the integration through Settings. Alternatively, extract `dist/hackrf_radio-0.4.0.zip` into `/config/custom_components/`. The resulting manifest must be at `/config/custom_components/hackrf_radio/manifest.json`.

## Connecting the USB device

### Home Assistant OS: use the bridge

HACS installs Python and frontend files. It does **not** install native USB utilities into HA OS. Run the included authenticated bridge on a Linux computer with the HackRF attached, then select **USB bridge** during setup. This can be the HA Docker host or another LAN computer. No HA add-on is required or included.

On that Linux computer, from this repository:

```sh
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
lsusb
```

Edit `.env` with the generated token, the HackRF's `/dev/bus/usb/BBB/DDD` path and the host's LAN IP as `HACKRF_BRIDGE_BIND`. The default bind is loopback only. Then:

```sh
docker compose -f compose.bridge.yaml up -d --build
```

The image builds the **host software only** from the checksum-verified official HackRF **v2026.01.3** release. It includes `hackrf_info`, `hackrf_sweep`, `hackrf_transfer`, `hackrf_spiflash` and libhackrf. Firmware images are downloaded only when preparing an update, and are checked against the reviewed catalog. Docker build itself never contacts a device. Build/network access is required. Compose maps one USB device, drops capabilities and uses a read-only root filesystem, with a persistent `hackrf-firmware` volume for backups, journals, app ownership and bounded captures. Do not delete that volume during an update or recovery. If a manual reset changes the USB device number, update `.env` and recreate the container after the write/readback phase is finished.

Enter `http://YOUR_USB_HOST_LAN_IP:8099` and the token in HA. Restrict access to trusted hosts on your LAN. Use a TLS reverse proxy for untrusted networks; the bridge client verifies HTTPS certificates and refuses redirects. Never forward the bridge to the public Internet.

For a non-Docker bridge, install compatible HackRF host tools and `aiohttp` in a Python environment on the USB host, grant the service user USB access using upstream udev rules, set `HACKRF_BRIDGE_TOKEN`, and run:

```sh
python -m custom_components.hackrf_radio.bridge --host 127.0.0.1 --port 8099 --firmware-dir ./firmware-data
```

Use the host's LAN address instead of `127.0.0.1` when HA is elsewhere. The module needs Python 3.13+ and `aiohttp`; it does not need Home Assistant installed. Copy the `custom_components/hackrf_radio` directory or run from the repository root.

### Home Assistant Core / Container: local USB

Select **Local USB** only when `hackrf_info` and `hackrf_sweep` are available **inside the Home Assistant process environment** and the process can access the USB device. Having the utilities on the Docker host is insufficient. Installing native packages in an ephemeral HA container is lost on container replacement; the bridge is usually easier to maintain.

For a PortaPack running Mayhem, select **HackRF** from its device menu first. Normal Mayhem USB serial-console mode and DFU mode are different interfaces and are not supported by this integration. Mode switching and reset remain manual. Firmware maintenance preserves the installed stock/Mayhem family.

## Firmware updates

Open **Firmware maintenance** in the sidebar. Updates are explicitly started by an administrator; receiving remains disabled throughout maintenance.

1. Select the reviewed release for the physical board and installed firmware family, then **Prepare update**. This reads identity, checks host tools, downloads the official archive, verifies archive and image SHA256/size, and checks the firmware's platform/version/DFU header. Preparation never erases or writes the radio.
2. Review the model, current/target version, image hash and full serial. Confirm stable USB power, the physical hardware profile, and—when using Mayhem—the matching SD-card content. Type the full serial, then select **Back up and install firmware** within ten minutes.
3. The USB host rechecks identity, saves and fsyncs a complete SPI backup and journal, rechecks identity again, writes with the tool's default compatibility protection, and reads back the entire flash. The image and erased tail must both match. No compatibility override or automatic reset is used.
4. After **awaiting reset** appears, manually reset/reconnect the radio, select HackRF mode again if needed, and select **Verify boot after manual reset**. The exact serial, board ID, reported version and health must match. Only then is receiving unlocked; monitoring remains off.

Reviewed targets in version 0.2.0 are **stock 2026.01.3** for standard HackRF One/Pro and **Mayhem v2.4.0** for their matching PortaPack configurations. This is a curated catalog, not permission to install arbitrary new GitHub releases. Nightly/custom versions, downgrades, equal-version reinstalls, stock/Mayhem conversion, pre-2021.03.1 CPLD migrations, DFU recovery, PortaRF and modified flash hardware require their upstream manual procedures. A PortaPack cannot be reliably detected physically over this USB interface; verify the hardware yourself before confirming.

Firmware maintenance requires official host tools **2026.01.3 or newer** and persistent storage. Local USB stores receipts and backups under `/config/.storage/hackrf_radio/firmware`; the Docker bridge uses `/var/lib/hackrf/firmware` in its named volume. The panel displays the relative backup filename and SHA256. Retain them. Updating an older bridge is necessary to enable firmware maintenance; receiving still works with a bridge that lacks these endpoints.

Closing the browser or a receive Stop request does not cancel a firmware job. Normal bridge/HA shutdown waits for local jobs; Compose allows ten minutes for graceful shutdown. **Power loss, USB unplugging, forced termination and broken hardware can still interrupt flashing and require manual recovery.** Keep the USB host powered, close other SDR applications, and avoid restarting/upgrading HA or the bridge until readback completes. A failed write/readback or interrupted journal blocks receiving and automatic retry. There is no automatic rollback; use the saved backup and [official recovery instructions](https://hackrf.readthedocs.io/en/latest/updating_firmware.html).

## Receive settings and automations

Use **Configure** on the integration or the sidebar's **Receive settings** form. Frequencies use whole MHz endpoints within **1–6000 MHz**, with a span of at most **100 MHz**. This deliberately uses a common conservative range for One/Pro variants. Upstream rounds sweeps to 20 MHz tuning steps; ranges whose rounded endpoint exceeds 6000 MHz are rejected. Extra bins outside your requested band are discarded.

Defaults are 430–440 MHz, 100 kHz FFT bins, IF/LNA 16 dB, VGA 16 dB, a 15-second interval and a −40 dB activity threshold. The minimum interval is 5 seconds. Sweeps run sequentially, so long captures can extend the effective interval. The median of the current sweep is shown as the noise-floor estimate. It is not a calibrated receiver noise-floor measurement.

For a device named **Workbench**, the main entities are `switch.workbench_monitoring`, `button.workbench_sweep_now` and `binary_sensor.workbench_signal_detected`. Actual IDs depend on your chosen name and HA's entity registry. See [the automation example](examples/automations.yaml). Signal detection uses the strongest finite bin anywhere in the selected band; it does not decode packets or identify a transmitter. Measurements become unknown when stale or unavailable when disconnected. The last-sweep timestamp preserves the time of the last successful capture.

The panel is admin-only and uses HA's authenticated WebSocket connection. Bridge credentials remain on the backend and are excluded from panel responses and diagnostics. Full FFT arrays are kept out of entity states and recorder history; the panel receives at most 1024 peak-preserving points. The waterfall stores up to 80 sweeps in the current browser session.

## Other apps and direct control

Open **Apps & your code** for a library of **17 SDR projects plus a generic external-app handoff**. It includes Gqrx, GNU Radio, CubicSDR, SoapyRemote, inspectrum, SDR++, SDRangel, SatDump, SigDigger, URH, QSpectrumAnalyzer, SDR#, SDR Console, radioconda and more, with upstream project/download links and optional saved web links.

- **Direct receive API:** authenticated scripts can request spectrum or signed 8-bit I/Q captures with frequency, sample-rate and IF/VGA settings. Captures are capped at 8 MB and can be downloaded through the bridge or the HA panel. A separate `HACKRF_RECEIVE_TOKEN` cannot flash firmware, install software, launch apps or change ownership.
- **USB handoff:** pause HA USB operations before a native app opens the device. Ownership survives restarts, blocks firmware/capture activity and must be returned explicitly after the app closes. External apps control their own RF/amp/bias/TX settings; the integration's receive locks cannot constrain them.
- **Managed launch:** explicitly opt in on the USB-host bridge to launch fixed catalog profiles. GUI apps need an unprivileged desktop session. SoapyRemote can provide native live tuning/streaming; it defaults to loopback and requires trusted VPN/firewall configuration for a private-address deployment.
- **Installation:** five reviewed Debian/Ubuntu recipes cover SoapyRemote, Gqrx, CubicSDR, GNU Radio and inspectrum. Review the plan in the panel or CLI. Runtime installation requires a writable root bridge with explicit installer opt-in. Windows/macOS and other apps use upstream installers; HACS cannot install programs on your browser computer.

For an optional image preinstalled with SoapyRemote and inspectrum:

```sh
docker compose -f compose.bridge.yaml -f compose.tools.yaml up -d --build
```

Set `HACKRF_APPS=soapy_remote,gqrx,cubicsdr,gnuradio,inspectrum` in `.env` to build a larger tools image. A headless container still has no desktop display, and no unauthenticated Soapy port is published. See [app setup, installation policies and the direct-control API](docs/APPS_AND_CONTROL.md), including an [external receive client](examples/receive_client.py).

## Your code, integrations and activity logs

Configure trusted programs on the USB host with `--custom-programs /absolute/path/programs.json`, or use `hackrf_radio_programs.json` in HA's configuration directory for local USB. Programs appear in the application selector and can be started/stopped from HA. Fixed commands run as an unprivileged account, with a restricted inherited environment. USB profiles acquire persistent exclusive ownership; offline/API profiles leave HA ownership intact and must avoid direct USB access. Commands cannot be uploaded or edited through a bridge/browser endpoint.

Programs can optionally emit bounded measurements to `sensor.YOUR_RADIO_custom_result` and the `hackrf_radio_result` event. Use HA actions for scheduled sweeps/recordings, app control, saved receive profiles and external result reporting. Existing HA MQTT, Node-RED, webhook and notification integrations can consume those events; no additional integration is required for standalone code.

**Activity & logs** shows persistent, filtered HA and USB-host history with UTC timestamps, correlated operation IDs, program stdout/stderr, exit codes and firmware phases. Each source retains 2,000 records with a bounded SQLite file. Noisy programs are rate limited, credentials and terminal controls are redacted, and filtered history can be exported as JSON. Storage failures are reported without interrupting firmware writes. Review arbitrary application output before sharing an export.

See [connection instructions, program profiles, HA actions, optional integrations and logging details](docs/HOME_ASSISTANT_WORKSPACE.md), including an [example profile](examples/programs.json) and [file-analysis program](examples/iq_analysis.py).

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| No devices | USB cable/power, permissions, USB passthrough, native tool installation and PortaPack HackRF mode |
| Busy | Close other SDR applications; only one capture is allowed per bridge/HA USB host |
| External app owns USB | Close it and explicitly return control in Apps & your code; the ownership record does not auto-expire |
| App launch/install disabled | Check host opt-in, desktop/root requirements, storage and read-only-container limits in the app guide |
| Firmware or self-test block | Read the exact identity and upstream compatibility documentation; a failed self-test is not repaired automatically |
| Firmware recovery required | Retain the persistent backup/journal; inspect the failure and follow upstream recovery instructions |
| Old/missing tool flags | Install current compatible host tools; the Docker bridge pins v2026.01.3 |
| Authentication failure | Match the token in HA and the USB-host environment; reauthentication is supported |
| Bridge address changed | Remove and re-add the integration with the new URL; the serial identity remains unchanged |
| Unexpected peaks or strong noise | Reduce IF/VGA gain; check antenna, filtering, nearby transmitters and input protection |
| Settings rejected near 6 GHz | Account for the tool's 20 MHz sweep rounding; for example 5980–6000 MHz fits, while 5990–6000 MHz rounds beyond the limit |

## Development

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/pytest
npm ci --ignore-scripts
npm test
python3 scripts/build_release.py
```

Tests cover device variants, argument validation, subprocess lifetimes, bridge HTTP/authentication, HA config flows, complete entity/panel setup and unload, WebSocket authorization, stale measurements and frontend DOM behavior. Firmware tests exercise checksums/headers, identity changes, explicit confirmation, persistent backup, readback failure, cancellation, duplicate requests, restart recovery and full HA/bridge update flows using simulated flash storage. Real release downloads and all four image headers/hashes have also been checked without writing to hardware. The CI workflow tests the minimum HA version and builds the Docker image. A physical HackRF has not been available for a real flash/boot test.

MIT licensed. HackRF and PortaPack names refer to their respective projects; this integration is independent of Great Scott Gadgets and the Mayhem project.
