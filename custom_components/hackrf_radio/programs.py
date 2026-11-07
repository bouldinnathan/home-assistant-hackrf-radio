"""User-owned launch profiles, loaded only from a USB-host configuration file."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .radio import RadioError


def load_profiles(path: Path | None, required: bool = False) -> dict:
    if path is None:
        return {}
    try:
        if not path.exists():
            if required:
                raise FileNotFoundError
            return {}
        if path.stat().st_size > 65536:
            raise ValueError
        data = json.loads(path.read_text())
        if not isinstance(data, list) or len(data) > 32:
            raise ValueError
        profiles = {}
        for item in data:
            if not isinstance(item, dict) or set(item) - {"id", "name", "description", "argv", "cwd", "desktop", "access", "publish_results"}:
                raise ValueError
            identifier, name, argv = item["id"], item["name"], item["argv"]
            if not isinstance(identifier, str) or not re.fullmatch(r"user_[a-z0-9_]{1,48}", identifier) or identifier in profiles:
                raise ValueError
            if not isinstance(name, str) or not 1 <= len(name) <= 80:
                raise ValueError
            if (not isinstance(argv, list) or not 1 <= len(argv) <= 32 or not all(isinstance(arg, str) and len(arg) <= 2048 and "\0" not in arg for arg in argv)
                    or not Path(argv[0]).is_absolute() or "{" in argv[0] or "}" in argv[0]):
                raise ValueError
            for argument in argv:
                if "{" in argument.replace("{serial}", "") or "}" in argument.replace("{serial}", ""):
                    raise ValueError
            cwd = item.get("cwd")
            if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute() or "\0" in cwd):
                raise ValueError
            if item.get("access", "usb") not in {"usb", "offline"}:
                raise ValueError
            if any(type(item.get(field, False)) is not bool for field in ("desktop", "publish_results")):
                raise ValueError
            description = item.get("description", "User-provided program; configured by the USB-host operator.")
            if not isinstance(description, str) or len(description) > 500:
                raise ValueError
            profiles[identifier] = {"id": identifier, "name": name, "argv": tuple(argv), "cwd": cwd,
                                    "description": description, "desktop": item.get("desktop", False),
                                    "access": item.get("access", "usb"), "publish_results": item.get("publish_results", False)}
        return profiles
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise RadioError("Custom program profiles are invalid; inspect the USB-host JSON file (absolute executable, user_ ID, fixed arguments)") from err


def result_data(value: dict, secrets=()) -> dict:
    """Small, flat measurements only; never execute data from a decoder."""
    import math

    from .activity import SECRET_NAME, redact

    if not isinstance(value, dict) or not 1 <= len(value) <= 16:
        raise ValueError("Results need 1–16 measurement fields")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key) or SECRET_NAME.search(key):
            raise ValueError("Invalid measurement field name")
        if item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("Measurement values must be scalar")
        if type(item) in (int, float) and (not math.isfinite(item) or abs(item) > 1e15):
            raise ValueError("Measurement must be finite and bounded")
        if isinstance(item, str):
            if len(item) > 200:
                raise ValueError("Measurement text is too long")
            item = redact(item, secrets)
        result[key] = item
    return result


def normalized_result(value, secrets=()):
    from datetime import UTC, datetime

    try:
        if not isinstance(value, dict) or not re.fullmatch(r"[0-9a-f]{32}", value["id"]):
            raise ValueError
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value["source"]):
            raise ValueError
        timestamp = datetime.fromisoformat(value["timestamp"])
        if timestamp.tzinfo is None:
            raise ValueError
        return {"id": value["id"], "source": value["source"], "timestamp": timestamp.astimezone(UTC).isoformat(),
                "data": result_data(value["data"], secrets)}
    except (KeyError, ValueError, TypeError, OverflowError) as err:
        raise RadioError("Invalid custom measurements: use a source ID, aware timestamp and bounded scalar fields") from err
