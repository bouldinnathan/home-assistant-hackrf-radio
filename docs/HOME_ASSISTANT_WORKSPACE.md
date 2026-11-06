# Your code, Home Assistant integrations and persistent logs

HackRF Radio 0.4 adds **Connecting & integrations** and **Activity & logs** to
the HA sidebar. **Apps & your code** includes host-configured programs, their
running/exit status and their latest optional measurements. The sidebar is
administrator-only. Normal HA actions let automations and other systems use
the same validation, USB ownership and firmware gates as the panel.

## Connecting

1. Install **HackRF Radio** through the HACS custom repository
   `https://github.com/bouldinnathan/home-assistant-hackrf-radio`, category
   **Integration**, then restart HA.
2. Attach the HackRF to a Linux USB host using a data-capable USB cable. On a
   Mayhem PortaPack, select **HackRF** from its device menu. Normal console/DFU
   mode is not a receiver connection.
3. On that Linux host, clone the repository, copy `.env.example` to `.env`,
   generate independent bridge and optional receive tokens, and run `lsusb`.
   Set the actual device path (`Bus 001 Device 005` corresponds to
   `/dev/bus/usb/001/005`), bridge token and host LAN bind address in `.env`.
4. Run `docker compose -f compose.bridge.yaml up -d --build`. Keep the named
   volume: it contains firmware backups, ownership and USB-host logs. Restrict
   the bridge to trusted clients; use HTTPS across untrusted networks.
5. In HA, open **Settings → Devices & services → Add integration → HackRF Radio**,
   choose **USB bridge**, enter `http://USB_HOST_IP:8099` and the administrative
   token, select the full serial and acknowledge the RF-input precautions.
6. Open the sidebar and try **Sweep now**. Monitoring starts off. After a reset
   that changes the Docker USB device number, update the mapping only after
   firmware write/readback has finished.

HA OS uses the separate bridge; HACS cannot install native USB tools into HA
OS. Local USB is supported where the tools and USB permissions exist inside
the HA runtime. See the [complete installation guide](../README.md).

## Configure your own programs

The operator of the USB host chooses executable paths, fixed arguments and
working directories in a JSON file. The HA browser sends a profile ID; it
cannot upload executable code or change launch commands. Programs run with
the operator's ordinary OS permissions, so this is **not a code sandbox**.
Only configure code you intend to run on that host.

Example `/home/radio/programs.json`:

```json
[
  {
    "id": "user_decoder",
    "name": "My decoder",
    "description": "My own receive decoder",
    "argv": ["/home/radio/venv/bin/python", "/home/radio/decoder.py", "--serial", "{serial}"],
    "cwd": "/home/radio",
    "access": "usb",
    "publish_results": true
  }
]
```

Run a native bridge as an **unprivileged account** with upstream USB/udev
permissions, Python 3.13+, compatible HackRF host tools and `aiohttp`:

```sh
python -m custom_components.hackrf_radio.bridge \
  --host YOUR_USB_HOST_LAN_IP --port 8099 \
  --firmware-dir /home/radio/hackrf-data/firmware \
  --applications-dir /home/radio/hackrf-data/applications \
  --custom-programs /home/radio/programs.json
```

Set `HACKRF_BRIDGE_TOKEN` in this service's environment. Start from the
repository root or an environment containing the integration package. A
profile file explicitly enables its custom launches; `--allow-app-launch`
separately enables the built-in software launch recipes. The default Docker
image runs as root and deliberately cannot launch user code. Use a native
unprivileged bridge or configure an unprivileged container with the required
USB permissions, program files and persistent storage.

For **local USB**, place profiles in `hackrf_radio_programs.json` in HA's
configuration directory. HA must run unprivileged. Profiles are re-read when
the catalog is refreshed. A missing file disables custom profiles; an invalid
file produces an actionable error rather than partially loading commands.

In **Apps & your code**, select your profile, acknowledge its settings and
choose **Launch on USB host**. Stop it with **Stop managed app**. Custom IDs
must start with `user_`; up to 32 profiles are supported. Executables and
working directories must be absolute paths. Arguments are an array, with
`{serial}` as the only substitution. Scripts, compiled applications, GNU
Radio flowgraphs and foreground container commands can all be profiles.
Do not daemonize or detach a managed program.

- `access: "usb"` reserves the USB host before launch. HA probes, receiving
  and firmware maintenance pause until you close the program and explicitly
  choose **Return control to HA**. An exit or host restart does not release it.
- `access: "offline"` leaves HA USB ownership intact. Use this for file
  analysis or a client of the authenticated receive API. Such programs must
  avoid direct USB access; this declaration is not an OS permission boundary.
- `desktop: true` additionally requires an unprivileged graphical host session.
- `publish_results: true` opts into the measurement protocol below. It defaults
  to false, so a program may run independently of HA integrations.

The child inherits a small allowlist of desktop/locale/path environment
variables, plus `HACKRF_SERIAL` and `PYTHONUNBUFFERED`. Bridge, Supervisor and
other credential variables are excluded. If your code needs a receive token,
have it read a protected file owned by the program's account. Never put a token
in a profile argument, saved web URL or printed output.

External code controls its own RF settings. Receive-only amplifier/bias locks
apply to the managed receive API; they cannot constrain a separate program
with native USB access. Protect the RF input physically.

## Publish measurements when wanted

With a profile's `publish_results` enabled, emit one newline-terminated JSON
object on stdout:

```python
print('{"hackrf_radio_result":{"temperature_c":21.5,"signal":"detected"}}', flush=True)
```

Use 1–16 fields, lowercase identifiers, finite numbers, booleans, null or short
strings. Objects, arrays, credential-named fields and oversized values are
rejected. The host stores the latest measurement with its source, timestamp,
serial and ID. HA polls it even during a USB handoff and deduplicates events
across restarts. HA exposes:

- `sensor.YOUR_RADIO_custom_result`: measurement time, with `source` and
  `measurements` attributes.
- `sensor.YOUR_RADIO_application_owner`: USB owner, with running state,
  program ID, last exit and package-maintenance phase.
- `hackrf_radio_result`: an HA event containing `entry_id`, `id`, `source`,
  `timestamp` and `data`.
- `hackrf_radio_activity`: an HA event when ownership/installer/running state
  changes, containing `entry_id`, `owner`, `installer` and `running`.

This interface transfers the **latest measurement** at HA's polling interval;
high-rate decoders should store packets themselves and publish summaries.
Log rate limits also apply to measurement lines. Programs can use the existing
receive HTTP API without HA; see [the direct-control guide](APPS_AND_CONTROL.md).

An existing system may instead call the HA action `hackrf_radio.report_result`
through HA's authenticated REST/WebSocket API. An administrative HA account is
required. No HA credential is automatically given to launched programs.

## Automations, schedules and optional adapters

In the automation editor, add a time/event/state trigger, then search actions
for **HackRF Radio**. The Connecting & integrations tab shows the selected
radio's actual config-entry ID for YAML examples.

| Action | Purpose |
| --- | --- |
| `hackrf_radio.sweep` | One receive sweep |
| `hackrf_radio.capture` | Bounded I/Q recording; response contains metadata |
| `hackrf_radio.monitoring` | Explicitly start/stop periodic sweeps |
| `hackrf_radio.configure` | Save validated settings; stop monitoring |
| `hackrf_radio.application` | Launch/stop a configured app, handoff/release USB |
| `hackrf_radio.preset` | Save/apply/delete a named receive profile |
| `hackrf_radio.report_result` | Publish custom measurements |
| `hackrf_radio.get_activity` | Read a filtered/paginated activity page |

All actions require `entry_id`. Capture requires a `settings` object with the
same parameters as the panel. Application `launch`/`handoff` require `app_id`
and `confirmed: true`; `release` requires `confirmed_closed: true`. Automated
release should be used only where the automation can establish that the
external program closed its USB handles; device-access validation still runs.
Firmware flashing remains a separate reviewed, confirmed panel workflow.

Use a time trigger with `sweep` or `capture` to schedule work. A monitoring
action with `enabled: true` is an explicit receiver start; error/reconnection
handling never starts it automatically. Package/firmware jobs retain priority.
Saved profiles hold all receive settings and persist across restarts. Applying
one stops monitoring; at most 20 profiles per radio are retained.

Node-RED can listen for `hackrf_radio_result` through its existing HA connection.
MQTT, notifications and outgoing webhooks use HA's existing integrations. For
example, after configuring MQTT in HA, adapt this automation:

```yaml
alias: Publish my decoder summary
trigger:
  - platform: event
    event_type: hackrf_radio_result
    event_data:
      source: user_decoder
action:
  - action: mqtt.publish
    data:
      topic: hackrf/my_decoder/summary
      payload: "{{ trigger.event.data.data | to_json }}"
      retain: false
```

Filter by `entry_id` as well when several radios use the same source name.
These integrations are optional; installing HackRF Radio does not configure
an MQTT broker, Node-RED instance or webhook destination.

## Robust logs

Open **Activity & logs** and select **Home Assistant** or **USB host & programs**.
Filter by severity and literal text, inspect older records or download filtered
JSON. Auto-refresh runs while the view is open; loading older records pauses it.
HA history remains readable for an unloaded entry, including failed setup.
Remote host history requires a reachable bridge with the new log endpoint.

Each source retains **2,000 records** in an SQLite database, with an **8,192-page
storage ceiling** (normally 32 MiB). Transactions use FULL synchronous commits
and transactional retention. Files are mode 0600; each write/read runs off the
HA event loop. Records contain UTC timestamps, source, severity, event, message
and an operation ID. HA passes that ID to the bridge so related entries can be
matched across sources; firmware jobs retain it after the initiating request.
Capture downloads, commands, ownership changes, program exits, firmware phases
and connection/authentication failures have stable event names.

Program stdout/stderr lines are bounded and drained continuously. Each stream
records at most **20 lines per 10 seconds**, with a count of suppressed lines.
Overlong lines are omitted. Descendants holding output pipes open after the
leader exits produce a warning and cannot leave the output monitor hanging.
Repeated connection/authentication failures are suppressed for 60 seconds.
Routine output stays in the activity view; warnings/errors also reach HA's or
the bridge's normal Python logger.

Known credential values, common credential assignments, bearer headers, URLs,
serial-looking strings and terminal control characters are redacted. The launcher does not write profile
command lines to the activity history. **Review exports
before sharing:** arbitrary program output can contain private application
data that cannot be identified automatically. Decoder measurements are also
available to the HA users/integrations authorized to access its events/states.

Disk-full, permission and corruption errors produce a storage-error status and
a stable logger warning. They never erase the database or interrupt a firmware
write. Receiving and firmware safety journals remain independent. Inspect the
host before repairing history; retain a corrupt database for diagnosis. Keep
the HA config and bridge persistent volume in your normal backup plan.
