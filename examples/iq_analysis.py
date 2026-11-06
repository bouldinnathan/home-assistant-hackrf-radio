"""Analyze a bounded ci8 file and optionally publish its summary to HackRF Radio.

Runs entirely on a saved file, without opening USB or requiring Home Assistant.
Copy this program and adapt examples/programs.json to your account's paths.
"""

import argparse
import json
import math
from pathlib import Path


def analyze(path: Path) -> dict:
    with path.open("rb") as source:
        raw = source.read(8_000_001)
    if not 2048 <= len(raw) <= 8_000_000 or len(raw) % 2:
        raise ValueError("Use a complete ci8 capture of 1024–4000000 complex samples")
    samples = memoryview(raw).cast("b")
    count = len(raw) // 2
    peak = max(math.hypot(samples[index], samples[index + 1]) for index in range(0, len(raw), 2))
    return {"complex_samples": count, "mean_i": round(sum(samples[::2]) / count, 3),
            "mean_q": round(sum(samples[1::2]) / count, 3), "peak_iq_magnitude": round(peak, 3)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    print(json.dumps({"hackrf_radio_result": analyze(args.capture)}), flush=True)
