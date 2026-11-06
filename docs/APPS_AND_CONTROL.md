# External apps, direct receive API and software installation

The **Apps & direct control** tab has three distinct paths:

| Path | Use it for | Hardware owner |
| --- | --- | --- |
| Managed receive API | Scripts and programs requesting bounded spectrum or raw I/Q captures | HackRF Radio; RF amp and antenna bias are forced off |
| USB handoff | Native desktop SDR applications and custom software using libhackrf directly | The external app; it controls its own RF settings |
| SoapyRemote | Native SoapySDR clients that need live tuning and streaming from the USB host | The external SoapyRemote server, under an explicit USB handoff |

These interfaces are not interchangeable. SDR# and SDR Console do not automatically become REST clients. Raw capture export is a file workflow; SoapyRemote supplies live streaming for clients that support it. A web link opens an already running app UI; it does not install or start software on the computer running your browser.

## USB handoff and managed launch

1. In the panel, choose the app or **Other external app**.
2. Close other SDR software and acknowledge that external apps control their own RF settings.
3. Choose **Hand off USB**, then open the app yourself on the USB host. Alternatively choose **Launch on USB host** if that host has explicitly enabled managed launch and the catalog executable is installed.
4. In the external app, select the exact HackRF serial shown by HA. Desktop launch profiles open the app; they do not silently change its saved configuration or choose a USB index.
5. Close the external app, or stop the process started by the bridge with **Stop managed app**. Confirm its USB connections are closed, then choose **Return control to HA**.

Handoff pauses all HA USB probes, scans, direct captures and firmware operations on that USB host. `hackrf_info` enumerates multiple devices, so continuing to probe another radio on the same host could still disturb the external app. The persistent ownership record has no automatic expiry. A bridge or HA restart never resumes device probes merely because the original browser disconnected. Returning control performs an explicit device-access/identity/health check; failure preserves the handoff. Monitoring remains off after a successful return.

**External applications can enable transmit, RF amplification or bias power. HackRF Radio cannot enforce its receive safeguards inside another program.** Review the external app's settings and protect the actual RF connection. Software does not prevent excessive RF input, external DC, ESD or hardware faults. Firmware maintenance remains exclusive and cannot start while USB is handed off.

Managed launch is disabled by default. Enable it on a native USB-host bridge:

```sh
python -m custom_components.hackrf_radio.bridge \
  --host 127.0.0.1 --port 8099 \
  --firmware-dir ./firmware-data \
  --applications-dir ./applications-data \
  --allow-app-launch
```

Run desktop apps from an **unprivileged desktop session** with the appropriate display environment. Root GUI launch is blocked. HA OS and a headless Docker container do not provide a desktop. The bridge starts only fixed catalog launch commands; there is no arbitrary executable, shell or argument endpoint. It tracks only its own child process and never kills a PID recovered from a persistent file. Stopping an app does not automatically release ownership.

## Software library and installation

The library contains **17 software projects plus a generic external-app handoff**. Project/install links come from the upstream project or vendor. Linux package installation is available for the following reviewed recipes:

| App | Debian/Ubuntu packages | Managed launch |
| --- | --- | --- |
| SoapyRemote | `soapyremote-server`, `soapysdr-module-hackrf`, `soapysdr-tools` | Loopback server by default |
| Gqrx | `gqrx-sdr` | Desktop |
| CubicSDR | `cubicsdr`, `soapysdr-module-hackrf` | Desktop |
| GNU Radio Companion | `gnuradio`, `gr-osmosdr`, `soapysdr-module-hackrf` | Desktop |
| inspectrum | `inspectrum` | Desktop/file analysis |

The other catalog entries provide upstream installation/download links: SDR++, SDRangel, SatDump, SigDigger, Universal Radio Hacker, QSpectrumAnalyzer, HackRF Spectrum Analyzer, SDR#, SDR Console, radioconda, gr-air-modes and Mayhem Hub. Windows/macOS installation uses these links. Arch/Manjaro and other distributions use their native package or upstream procedures; the managed installer does not run APT on them. Driver/plugin support and GUI compatibility depend on the chosen upstream build. Finding an executable does not certify its hardware configuration.

### Optional preinstalled Docker image

The lean bridge remains the default. To install reviewed software into an optional image:

```sh
docker compose -f compose.bridge.yaml -f compose.tools.yaml up -d --build
```

The default tools profile installs SoapyRemote and inspectrum. Set this in `.env` to build a larger image:

```dotenv
HACKRF_APPS=soapy_remote,gqrx,cubicsdr,gnuradio,inspectrum
```

GNU Radio and desktop dependencies make this image substantially larger. Programs are installed **inside the USB-host container**, not on your browser computer. A headless image does not display GUI apps. The optional overlay enables fixed app launch profiles, but only the headless SoapyRemote profile can launch without a desktop. It remains bound to the container's loopback interface; no unauthenticated Soapy port is published.

APT post-install service startup is suppressed. The Debian `soapyremote-server.service` and upstream `SoapySDRServer.service` are masked so package installation cannot create a second, unmanaged USB owner. Existing administrator service policies/configurations are not overwritten. Review the service policy in the package plan if you already run a Soapy service.

### Native USB-host installation

Review the exact package plan first:

```sh
python -m custom_components.hackrf_radio.software plan \
  --apps soapy_remote,gqrx,cubicsdr,gnuradio,inspectrum
```

Then run the module as root from the Python environment that contains the bridge code and aiohttp:

```sh
sudo /absolute/path/to/python -m custom_components.hackrf_radio.software install \
  --apps soapy_remote,gqrx,cubicsdr,gnuradio,inspectrum --confirm
```

The current directory or Python module path must contain `custom_components/hackrf_radio`. This uses distribution-signed APT repositories, `apt-get update`, and a fixed `apt-get install --no-remove --no-install-recommends` package list. It does not add PPAs, execute downloaded installer scripts, or perform a full system upgrade. Dependencies may be installed. Close other SDR programs before installation and do not interrupt package writes.

For installation from the HA panel, an administrator must explicitly run the USB-host bridge with `--allow-software-install` as root on a writable Debian/Ubuntu host. **Review selected packages** displays the plan; **Install reviewed packages** requires confirmation and a plan less than ten minutes old. The read-only Docker runtime deliberately disables this route; use the tools image instead. Normal HA local-USB mode has no root installer opt-in.

Package jobs hold the host's maintenance lock, survive the initiating browser request, and are checked against firmware maintenance before starting. A failed or interrupted package job blocks USB use until the host is inspected. There is no automatic `apt --fix-broken`, service unmasking or reboot. Once the package manager is healthy and no unmanaged SDR service is running, stop the bridge, retain the receipt for diagnosis, reset its `applications/install.json` to `{"phase":"idle"}`, and restart the bridge. Do not alter a live installation journal.

## Live SoapyRemote control

On a native USB-host bridge, install SoapyRemote/SoapyHackRF, enable managed launch, choose **SoapyRemote**, and confirm/launch. Its fixed command is:

```sh
SoapySDRServer --bind=127.0.0.1:55132
```

Use a Soapy-capable client with these device args (replace the serial):

```text
driver=remote,remote=127.0.0.1:55132,remote:driver=hackrf,remote:serial=FULL_32_CHARACTER_SERIAL
```

The default loopback server works for clients on the USB host or in the same container network namespace. For a client on another computer, the USB-host operator can explicitly choose the host's private VPN/LAN address, for example:

```sh
python -m custom_components.hackrf_radio.bridge \
  --host 127.0.0.1 --firmware-dir ./firmware-data \
  --allow-app-launch --soapy-bind 10.8.0.10
```

```text
driver=remote,remote=10.8.0.10:55132,remote:driver=hackrf,remote:serial=FULL_32_CHARACTER_SERIAL,remote:prot=tcp
```

Use a trusted VPN and firewall access to trusted client addresses. **SoapyRemote is not protected by the bridge bearer tokens** and can expose all Soapy devices on that host; client serial filtering is not a server authorization boundary. The bridge accepts only loopback or RFC1918 literal IPv4 bind addresses, never public/wildcard addresses, but it cannot verify that a private network is trustworthy.

SoapyRemote negotiates separate stream/status sockets on additional ports, including when TCP is selected. A single SSH forward of port 55132 is insufficient for live streaming. The selected host/network namespace and firewall must allow the negotiated connections from trusted clients. A container-loopback server is inaccessible from the Docker host's loopback, and the provided Compose files deliberately do not publish a Soapy port. Use the authenticated HTTP receive API when bounded receive captures meet your needs.

See [SoapyRemote's official device and stream arguments](https://github.com/pothosware/SoapyRemote/wiki). External software controls hardware settings after handoff; its TX/amp/bias capabilities are not filtered by the bridge.

## Authenticated direct receive API

Set a distinct random `HACKRF_RECEIVE_TOKEN` on the bridge. It must differ from `HACKRF_BRIDGE_TOKEN` and contain at least 32 non-whitespace ASCII characters. Keep the admin token in HA. Give external receive programs the receive token, which can use only device information, sweeps, captures, capture downloads and stop. Firmware, app ownership, app launch, saved-link and package endpoints reject it with HTTP 403.

All requests use `Authorization: Bearer …`. Use a trusted network or HTTPS; no bearer token is placed in a query parameter, app link or frontend response. HTTPS verification is enabled, and the bridge client refuses redirects.

| Method and endpoint | Purpose |
| --- | --- |
| `GET /v1/devices` | Accessible HackRF identities; blocked while USB is handed off |
| `POST /v1/sweep` | Existing bounded spectrum sweep |
| `POST /v1/capture` | Bounded signed 8-bit complex receive capture |
| `GET /v1/captures/{serial}/{capture_id}` | Download completed `.cs8` bytes with a SHA256 header |
| `POST /v1/stop` | Stop the matching receive `operation_id`; never cancels a firmware write |

Capture request:

```json
{
  "serial": "FULL_32_CHARACTER_SERIAL",
  "operation_id": "32_LOWERCASE_HEX_CHARACTERS",
  "settings": {
    "frequency_hz": 433920000,
    "sample_rate": 8000000,
    "samples": 1000000,
    "lna_gain": 16,
    "vga_gain": 16
  }
}
```

`operation_id` is a UUID hex string chosen by the caller. The response's `capture` object has the capture ID, byte count, format `ci8`, settings, capture timestamp and SHA256. It contains metadata only; download the bytes using the GET route. Each complex sample is signed `int8 I` followed by signed `int8 Q`; there is no WAV header. Maximum capture size is **8 MB**, with 1024–4000000 complex samples. Allowed rates are 8, 10, 12.5, 16 and 20 Msps. The whole requested band must fit inside 1 MHz–6 GHz. Only the latest four completed captures are retained; canceled/incomplete captures are removed. Samples remain on the USB host and are excluded from HA recorder states.

Unknown keys, transmit parameters, arbitrary CLI arguments, RF amp enable, antenna power and invalid gain steps are rejected. The command always supplies `hackrf_transfer -r … -d FULL_SERIAL -n … -a 0 -p 0`. It shares the same USB lock and firmware/ownership gates as scanning. Overlapping commands receive HTTP 409. An active native app must return USB before this API can receive again.

The admin-only HA endpoint `/api/hackrf_radio/{entry_id}/captures/{capture_id}` downloads the same bytes through HA authentication; the browser does not receive the bridge token. Example external receive client: [examples/receive_client.py](../examples/receive_client.py).

Admin application actions are under `POST /v1/applications/{action}`. Status takes `{}`. Other actions take the serial and exactly the fields shown by the panel: `handoff(app_id, confirmed)`, `release(confirmed_closed)`, `launch(app_id, confirmed)`, `stop()`, `link(app_id, url)`, `prepare_install(apps)` and `install(plan_id, confirmed)`. No action accepts a user-supplied executable or command string.
