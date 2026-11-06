# Device protection and compatibility

This integration controls a radio receiver attached to real RF hardware. Its software constraints reduce avoidable software mistakes; they cannot make an unsafe antenna connection safe or verify the circuitry of an unknown clone. No physical HackRF was attached during initial development, so the supported identities below describe software capability checks rather than hardware certification.

## Upstream evidence and implemented decisions

| Evidence | Decision in this integration |
| --- | --- |
| [Great Scott Gadgets' HackRF One input limit](https://hackrf.readthedocs.io/en/latest/hackrf_one.html#maximum-input-power) specifies −5 dBm and recommends external attenuation | Show the limit during setup and in the panel. Never infer input safety from relative FFT readings. Never raise the limit when the RF amp is off. |
| [Official GitHub issue #974](https://github.com/greatscottgadgets/hackrf/issues/974) documents RF-amplifier damage and reports involving nearby transmitters and external DC | RF amplifier and antenna bias are locked off. Explain attenuation, transmitter separation and suitable DC blocking. DC protection depends on the actual external equipment. |
| [Official receive-gain documentation](https://hackrf.readthedocs.io/en/latest/setting_gain.html) gives IF/LNA 0–40 in 8 dB steps and VGA 0–62 in 2 dB steps | Enforce those ranges and start at IF=16, VGA=16, RF amp off. Reject invalid steps rather than silently round. |
| [Upstream sweep tool](https://github.com/greatscottgadgets/hackrf/blob/v2026.01.3/host/hackrf-tools/src/hackrf_sweep.c) exposes receive sweeps, serial selection, power flags, one-shot mode and 20 MHz range rounding | Use only a fixed `hackrf_sweep -d SERIAL -1 -a 0 -p 0 ...` command. Check help flags first and enforce the rounded 6000 MHz endpoint. |
| [libhackrf sweep implementation](https://github.com/greatscottgadgets/hackrf/blob/v2026.01.3/host/libhackrf/src/hackrf.c) requires USB API 1.02 | Reject older sweep APIs. Report Mayhem firmware strings verbatim rather than comparing them as release dates. |
| [Official hardware revisions](https://github.com/greatscottgadgets/hackrf/blob/main/docs/source/list_of_hardware_revisions.rst) identify component changes in r9/r10 and revision detection added in r6 | Parse board ID, revision, firmware and API independently; tolerate missing revision fields on older boards. |
| [Upstream hackrf_info](https://github.com/greatscottgadgets/hackrf/blob/v2026.01.3/host/hackrf-tools/src/hackrf_info.c) can report incompatible firmware or a failed self-test | Fail closed on these reports before a receive command. Do not treat an info-command exit code of zero alone as proof of a healthy device. |
| [Mayhem's HackRF-mode documentation](https://github.com/portapack-mayhem/mayhem-firmware/wiki/HackRF) explains USB SDR mode and Mayhem firmware version strings | Discover radios in manually selected HackRF mode; firmware maintenance preserves the installed stock/Mayhem family. |
| [Official HackRF Pro documentation](https://hackrf.readthedocs.io/en/latest/hackrf_pro.html) describes backward compatibility and enhanced hardware | Accept known Pro-compatible board IDs while using the same conservative 1–6000 MHz receive profile. Do not assume all Pro/clone protections exist. |

The container pins [official host release v2026.01.3](https://github.com/greatscottgadgets/hackrf/releases/tag/v2026.01.3) by archive SHA256, builds only `host/`, and copies the information/sweep/SPI-flash executables and shared library into the runtime. Firmware is downloaded separately during explicit preparation. Native distribution packages may lag support for newer boards. Firmware is never silently updated or downgraded.

## Firmware maintenance

Version 0.2.0 adds an admin-only prepare/install/verify workflow. The reviewed catalog has four fixed images: stock One/Pro 2026.01.3 and Mayhem One/Pro v2.4.0. Both the official archive digest and extracted image digest/length are pinned. The image must have a valid `HACKRFFW` header, structure version 1, non-DFU mode, the matching board-platform bit and the expected version string. No arbitrary URL, upload, path, offset, erase command or compatibility override is accepted through the API.

The same serial, board ID, revision, running version and health are checked during preparation, before backup and again after backup. A ten-minute plan is required, with typed serial confirmation, physical hardware/family confirmation, stable power confirmation and matching SD-card preparation for Mayhem. Stock and Mayhem are separate families. Firmware may report misleading identities on modified boards; USB metadata cannot verify the actual flash chip or physical PortaPack. PortaRF and modified flash hardware are excluded by the hardware confirmation and upstream manual guidance.

Before any erase, the complete old SPI flash is read, length-checked, rejected if uniformly erased/zero, stored with restrictive permissions and fsynced. A SHA256 receipt and write-intent journal are persisted, including the relevant directory entries. The native command is exactly `hackrf_spiflash -d FULL_SERIAL -w VERIFIED_IMAGE`, retaining upstream compatibility checks. It never uses `-i`, reset, raw addresses or a partial-write mode. The complete flash is then read again and compared with the reviewed image plus its expected erased tail. Success at this stage is **awaiting_reset**, not completion: manual reset/reconnect and a separate identity/health/version check are required before receiving is unlocked.

The upstream [SPI-flash tool source](https://github.com/greatscottgadgets/hackrf/blob/v2026.01.3/host/hackrf-tools/src/hackrf_spiflash.c) checks image/platform compatibility before erase, but does not itself compare a complete post-write readback. This integration supplies the backup, durable state and readback checks. The [Mayhem update guide](https://github.com/portapack-mayhem/mayhem-firmware/wiki/Update-firmware) also requires release-matched SD content for USB flashing; this integration cannot update a PortaPack SD card through HackRF mode and requires the user to prepare it separately.

Firmware jobs belong to the USB host rather than the initiating HTTP/WebSocket request. A disconnected browser, lost install response, retried plan ID or receive Stop cannot launch a second write or cancel the existing write. The native write process has no cancellation/timeout kill. Normal shutdown waits; forced process termination, USB loss and power failure remain real recovery risks. The persistent Docker volume and longer shutdown grace reduce those risks without eliminating them.

A write/readback error or an interrupted/unreadable active journal enters **recovery_required**, blocks receiving and prevents automatic retries. The backup is retained; there is no automatic rollback, reset or DFU attempt. Nightly/custom version strings, equal/newer versions, stock/Mayhem conversion, unknown boards and old firmware requiring CPLD migration are blocked from this workflow. Follow the official manual procedure for those cases.

## Variations

| Device or situation | Behavior |
| --- | --- |
| HackRF One board IDs 2 / 4, including r9 and r10 | Allowed if full identity, API and host-tool checks pass; a reported firmware mismatch or self-test failure blocks scanning |
| HackRF Pro / compatible board ID 5 | Same receive profile; wider tuning and Pro-specific controls are not exposed |
| Older One with no hardware-revision field | Missing revision is displayed as unknown; supported sweep API and required tool flags remain mandatory |
| PortaPack H1/H2/H4/H4M with Mayhem | Must be in the device's HackRF mode; Mayhem version strings are accepted without attempting firmware conversion |
| Third-party board reported by firmware | Display an unverified-hardware warning; software cannot identify its actual protection components |
| Unrecognized board ID, Jawbreaker or rad1o | Blocked rather than guessing board-specific limits |
| DFU bootloader or normal Mayhem console | No accessible supported HackRF; no automatic reset or recovery |
| Multiple devices | Explicit full serial selection, duplicate serial rejection and serialized operations; no first-device fallback |
| USB unplug, permissions error, incompatible tool or capture error | Fail the capture, mark availability and turn monitoring off; a later successful info poll permits manual rearming |
| Receive stop, settings change or capture timeout | Stop the matching receive operation; firmware jobs are separate and cannot be stopped this way |
| Entry unload while flashing | Refuse unloading until the background job finishes |
| Normal USB-host/HA shutdown while flashing | Wait for the local firmware job; a forced termination can still require manual recovery |

## Operational limits

The common profile accepts whole MHz ranges, spans of 1–100 MHz, 2445–5000000 Hz requested FFT bin widths and no more than 20000 estimated capture bins. The upstream tool chooses an achievable FFT width; the parser uses the actual width in its CSV output. Invalid or non-finite data is rejected or excluded; points outside the requested band are dropped. The UI reduces large spectra using maximum-power buckets to retain peaks.

A local command has a 20-second timeout and a 2 MB limit per output stream. On interruption, SIGINT allows upstream cleanup; after a two-second grace period a hung process is killed and reaped. A killed or unplugged device may still need manual investigation; the integration does not claim to inspect analog power state. Other SDR applications can independently change hardware state, so only one application should own the device.

Stopping a remote capture uses an opaque per-operation ID, preventing a request for one entry from stopping an unrelated bridge operation. Stop IDs also fence a reordered stop-before-start request. The bridge never starts autonomous background monitoring: every receive capture corresponds to one authenticated HTTP request, so losing HA connectivity cannot leave a bridge monitoring loop running indefinitely.

HA stores the bridge token in its normal config-entry storage. Protect HA backups and the bridge `.env`. The panel and diagnostics omit the token and host URL; the admin WebSocket API accepts only named receive actions. HTTP bearer authentication requires a trusted network or HTTPS to protect the token in transit.
