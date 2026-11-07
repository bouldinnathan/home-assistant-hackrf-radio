# First run and upgrade troubleshooting

## Fresh installation

Use a Linux USB host with Docker/Compose installed. HACS installs the HA
integration; HA OS uses this separate bridge for native USB access.

1. Clone this repository and open a terminal in its directory.
2. Connect a USB data cable. In Mayhem, select **HackRF** on the PortaPack.
   Close competing SDR apps and finish any existing firmware work.
3. Run `lsusb` and identify the actual bus/device path. For example, Bus 001
   Device 005 corresponds to `/dev/bus/usb/001/005`.
4. Prepare a fresh receiver configuration, replacing the example device and
   bind address with this host's values:

   ```sh
   python3 scripts/setup_bridge.py \
     --device /dev/bus/usb/001/005 --bind 192.168.1.10
   docker compose -f compose.bridge.yaml up -d --build
   ```

   The helper validates device-node metadata without opening USB, generates
   independent admin/receive tokens, and creates a mode-0600 `.env`. Existing
   configuration is preserved. Loopback is the default when `--bind` is omitted;
   another computer cannot reach that loopback listener. Use the USB host's
   actual LAN/VPN address when HA is elsewhere. Do not expose the bridge publicly.
5. Add `https://github.com/bouldinnathan/home-assistant-hackrf-radio` in HACS
   **Custom repositories**, category **Integration**. Download **HackRF Radio**
   and restart HA. Then add it through **Settings → Devices & services**.
6. Choose **USB bridge**, enter `http://USB_HOST_IP:8099`, and use
   **HACKRF_BRIDGE_TOKEN** from `.env`. `HACKRF_RECEIVE_TOKEN` is for scripts;
   the wizard rejects it before probing USB. Select the full serial.
7. Try **Sweep now**, then explicitly enable monitoring when ready. Initial
   setup never flashes firmware and never automatically arms monitoring.

Local USB is an alternative where compatible tools and USB permissions exist
inside the HA runtime. An ordinary host package install does not install tools
inside an HA container. See the [main installation guide](../README.md).

## Fresh setup with user programs

On standard Linux Docker, use your ordinary account (UID >= 1000), with
permission to run Docker. Do not run this helper with sudo:

```sh
python3 scripts/setup_bridge.py --programs \
  --device /dev/bus/usb/001/005 --bind 192.168.1.10
docker compose -f compose.bridge.yaml -f compose.user.yaml up -d --build
```

This builds the **bridge-user** image using your UID/GID. It supplies the USB
node's supplementary group and mounts owned `bridge-data` storage, a writable
home, and read-only `bridge-programs` code. The root filesystem remains read-only
and all capabilities remain dropped. Reviewed software is installed at image
build time. The default program image includes SoapyRemote and inspectrum;
`HACKRF_APPS` selects the other reviewed recipes.

The helper creates a harmless **Program setup check** profile that prints a
`program_status: ready` result without opening USB. Connect HA, launch that
profile in **Apps & your code**, and inspect the result and USB-host activity.
Replace/add profiles in `bridge-programs/programs.json` for your actual code.
Use paths as seen **inside the container**, such as `/programs/decoder.py`.
Dependencies must exist in the image; host virtual environments are not portable
container installations. Debian GNU Radio Python programs use the packaged
`/usr/bin/python3`; the bridge itself uses `/usr/local/bin/python`.

The ordinary lean Docker image still runs as root and deliberately cannot
launch custom code. Choose the user overlay for that feature. Neither profile
provides a desktop display. GUI software needs a separate unprivileged graphical
host session. Rootless Docker/user-namespace mappings need corresponding bind
ownership and have not been validated by this profile's tests.

The helper is for **fresh storage**. It refuses an existing `.env` or a nonempty
data directory. `--env-file`, `--data-dir` and `--programs-dir` select other
locations. An alternate env file must also be supplied to Compose with
`--env-file`. Never replace existing firmware/ownership state with empty storage
to bypass recovery blocks.

## Recovery and connection changes

- **Existing handoff:** the wizard shows **Return existing USB ownership**.
  Close external programs, type the displayed full serial and confirm. If you
  explicitly select Stop, it stops only a program owned by that bridge. The
  server checks the reviewed ownership lease under its process-control lock,
  then verifies device identity/health before returning USB. A changed lease,
  running unmanaged program or maintenance block prevents recovery. Recovery
  requires bridge 0.5 or newer; older bridges must be upgraded first.
- **Failed setup before an entry exists:** open the HA sidebar and use
  **Read setup diagnostics** on the empty-state card. Setup attempts are recorded
  without exposing tokens. A bridge that never started must be diagnosed through
  its Docker/service console logs.
- **Changed bridge IP/URL:** use the integration's **Reconfigure** action in
  Settings → Devices & services. Supply the URL/admin token. The same full serial
  must be present; existing entry identity and receive options are preserved.
  Finish firmware/package maintenance before reconfiguration.
- **Bad custom JSON:** all custom profiles are disabled and an actionable
  `profile_error` appears in the panel/logs. Basic device setup, receive work and
  existing process cleanup remain available. Fix the file; the catalog refresh
  reloads it. A custom profile with a missing working directory is rejected
  before USB ownership changes.
- **USB reset/reconnect:** Docker can assign a new device number. Update
  `HACKRF_USB_DEVICE` and recreate the container only after firmware write/readback
  and boot verification have finished. This mapping is still explicit.

The supplied Compose profiles share the `hackrf-radio-host-lock` volume. Their
bridge processes hold an advisory lease for their entire lifetime, so another
supplied bridge cannot concurrently start against different data volumes.
Native bridges default to `/tmp/hackrf-radio-host.lock`; `--host-lock` selects a
shared coordination file. Containers/native services must use the same mounted
file to coordinate across those environments. The empty lock file contains no
credentials and permits different container UIDs to claim it. It is not an
authorization or hardware-access boundary. Unmanaged SDR programs must still
be closed or explicitly handed USB ownership.

## Updating an existing installation

Update HACS **and** the USB-host checkout/image. HACS does not rebuild the bridge.
Wait for maintenance to finish, retain persistent storage, and rebuild with
your existing Compose files and `.env`. Do not run the fresh setup helper over
an existing deployment, remove its volumes, or clear recovery journals.

When migrating from the root image's named data volume to user-mode bind
storage, stop the bridge only after all maintenance has completed. Copy the
**entire** original volume, including firmware backups, applications, ownership,
receipts and activity databases, into the target directory. Set copied file
ownership to the configured unprivileged UID/GID, retain the original volume,
and preserve tokens. Configure the user overlay's UID/GID, USB group and absolute
data/program directories in the existing `.env`. Verify retained state before
resuming use. The fresh helper intentionally does not automate this migration.

## Logs and validation limits

Each source keeps **2,000 recent records plus up to 500 independently retained
warnings/errors**. Protected records remain visible after a debug-output flood;
duplicates are removed from the combined view. Pagination/export includes up to
2,500 unique records. A live writer's storage failures are shown by the HA log
view even if its existing database remains readable. Corrupt files are preserved.

CI covers HA 2025.1.4, 2026.7.4 and 2026.11.0, frontend behavior, Docker software
images and non-root offline program/result/log execution. Physical RF reception,
firmware flashing, power interruptions and ARM USB operation still require real
hardware validation. Custom results transfer the latest summary; high-rate
decoders should retain their own packet stream. The managed receive API keeps
amplifier/bias power off, but separate user code controls its own native RF
settings. Follow the [physical device safety guidance](DEVICE_SAFETY.md).
