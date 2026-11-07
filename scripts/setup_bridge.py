"""Prepare a fresh Docker bridge configuration without opening USB or flashing."""

import argparse
import ipaddress
import json
import os
import re
import secrets
import shlex
import stat
from pathlib import Path


def quote(value):
    text = str(value)
    if any(char in text for char in "'\\\r\n\0"):
        raise ValueError("Choose a path without quotes, backslashes or control characters")
    return "'" + text + "'"


def write_new(path, text):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(text)


def prepare(device: Path, bind: str, env_file: Path, programs: bool = False, data_dir: Path | None = None,
            programs_dir: Path | None = None):
    if env_file.exists() or env_file.is_symlink():
        raise ValueError("Environment file already exists; it was preserved. Use a different --env-file for a fresh setup")
    address = ipaddress.IPv4Address(bind)
    if not any(address in ipaddress.IPv4Network(network) for network in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("Bind to loopback or an actual private LAN/VPN IPv4 address")
    if not re.fullmatch(r"/dev/bus/usb/\d{3}/\d{3}", str(device)):
        raise ValueError("Use the actual /dev/bus/usb/BBB/DDD path from lsusb")
    metadata = device.stat()
    if not stat.S_ISCHR(metadata.st_mode):
        raise ValueError("The USB path is not a character device")
    if programs and os.getuid() < 1000:
        raise ValueError("Run program setup from an ordinary unprivileged Linux account (UID >= 1000)")
    values = {"HACKRF_BRIDGE_TOKEN": secrets.token_urlsafe(32), "HACKRF_RECEIVE_TOKEN": secrets.token_urlsafe(32),
              "HACKRF_BRIDGE_BIND": str(address), "HACKRF_USB_DEVICE": str(device), "HACKRF_APPS": "soapy_remote,inspectrum"}
    if programs:
        data_dir = (data_dir or Path.cwd() / "bridge-data").absolute()
        programs_dir = (programs_dir or Path.cwd() / "bridge-programs").absolute()
        quote(data_dir)
        quote(programs_dir)
        # Existing storage must be reviewed/migrated deliberately. Never create
        # a fresh config that conceals a pending firmware or ownership journal.
        if data_dir.exists() and any(data_dir.iterdir()):
            raise ValueError("Data directory is not empty; preserve and migrate existing bridge state before switching profiles")
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (data_dir / "home").mkdir(exist_ok=True, mode=0o700)
        programs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile = programs_dir / "programs.json"
        if not profile.exists():
            sample = programs_dir / "status.py"
            if sample.exists() or sample.is_symlink():
                raise ValueError("An existing status.py was preserved; provide your own programs.json")
            write_new(sample, 'import json\nprint(json.dumps({"hackrf_radio_result": {"program_status": "ready"}}), flush=True)\n')
            write_new(profile, json.dumps([{"id": "user_status", "name": "Program setup check", "argv": ["/usr/local/bin/python", "/programs/status.py"],
                                          "access": "offline", "publish_results": True}], indent=2) + "\n")
        values |= {"HACKRF_UID": os.getuid(), "HACKRF_GID": os.getgid(), "HACKRF_USB_GID": metadata.st_gid,
                   "HACKRF_DATA_DIR": str(data_dir), "HACKRF_PROGRAMS_DIR": str(programs_dir)}
    write_new(env_file, "\n".join(f"{key}={quote(value)}" for key, value in values.items()) + "\n")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--programs", action="store_true", help="Prepare the non-root program image and a harmless offline readiness profile")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--programs-dir", type=Path)
    args = parser.parse_args()
    try:
        prepare(args.device, args.bind, args.env_file, args.programs, args.data_dir, args.programs_dir)
    except (ValueError, OSError) as err:
        parser.error(str(err))
    print(f"Prepared {args.env_file} with independent credentials and the exact USB node. No USB device was opened.")
    overlay = " -f compose.user.yaml" if args.programs else ""
    print(f"Review {args.env_file}, then: docker compose --env-file {shlex.quote(str(args.env_file))} -f compose.bridge.yaml{overlay} up -d --build")
    print("Use the administrative HACKRF_BRIDGE_TOKEN from that file in HA. Set --bind to the USB host LAN address when HA is elsewhere.")


if __name__ == "__main__":
    main()
