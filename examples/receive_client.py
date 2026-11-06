"""Receive a bounded .cs8 I/Q capture using the bridge's scoped receive token.

HACKRF_RECEIVE_TOKEN must be set in the environment. This client needs aiohttp.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import aiohttp
from yarl import URL


async def receive(url: str, serial: str, frequency: int, samples: int, output: Path):
    parsed = URL(url)
    if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.user is not None or parsed.query_string or parsed.fragment:
        raise ValueError("Use an HTTP(S) bridge URL without credentials or query parameters")
    token = os.environ["HACKRF_RECEIVE_TOKEN"]
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}, timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(str(parsed).rstrip("/") + "/v1/capture", json={
            "serial": serial, "operation_id": uuid4().hex,
            "settings": {"frequency_hz": frequency, "sample_rate": 8_000_000, "samples": samples,
                         "lna_gain": 16, "vga_gain": 16},
        }, allow_redirects=False) as response:
            response.raise_for_status()
            capture = (await response.json())["capture"]
        if not isinstance(capture["capture_id"], str) or len(capture["capture_id"]) != 32:
            raise ValueError("Invalid capture response")
        async with session.get(str(parsed).rstrip("/") + f"/v1/captures/{serial}/{capture['capture_id']}", allow_redirects=False) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > 8_000_000:
                    raise ValueError("Oversized receive capture")
        if len(raw) != capture["byte_count"] or hashlib.sha256(raw).hexdigest() != capture["sha256"]:
            raise ValueError("Capture size or SHA256 verification failed")
        output.write_bytes(raw)
        output.with_suffix(".json").write_text(json.dumps(capture, indent=2) + "\n")
        print(f"Saved {len(raw)} bytes to {output}; format ci8, sample rate 8 Msps")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("serial")
    parser.add_argument("--frequency", type=int, default=433_920_000)
    parser.add_argument("--samples", type=int, default=1_000_000)
    parser.add_argument("--output", type=Path, default=Path("capture.cs8"))
    args = parser.parse_args()
    asyncio.run(receive(args.url, args.serial, args.frequency, args.samples, args.output))
