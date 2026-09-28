"""Read-only structural analysis of WStock .wdz files.

Goal: determine the container layout well enough to extract the 5m bars,
so we can test whether WStock data repairs the purchased-history gaps.

This writes nothing outside the report path; it only inspects bytes.
"""

from __future__ import annotations

import collections
import json
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WDZ_DIR = Path(r"E:\AStockHistory\wstock\原始压缩包\WDZ")


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = collections.Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def main() -> int:
    files = sorted(WDZ_DIR.glob("*.wdz"))
    print(f"files={len(files)}")
    out = []
    for path in files:
        raw = path.read_bytes()
        head = raw[:64]
        info = {
            "file": path.name,
            "size": len(raw),
            "head_hex": head[:32].hex(" "),
            "tail_hex": raw[-16:].hex(" "),
            "entropy_first_1m": round(entropy(raw[: 1 << 20]), 4),
            "entropy_all": round(entropy(raw[: 8 << 20]), 4),
        }
        # u32 view of the first 8 words
        info["u32_head"] = list(struct.unpack("<8I", raw[:32]))
        # Does the 8-byte constant at offset 8 recur?
        sig = raw[8:16]
        first_hit = raw.find(sig, 16)
        info["sig_repeat_offset"] = first_hit
        # Common magic-ish 4-byte markers
        out.append(info)
        print(json.dumps(info, ensure_ascii=False))

    # Compare headers for a constant prefix
    heads = [bytes.fromhex(i["head_hex"].replace(" ", "")) for i in out]
    common = 0
    for idx in range(min(len(h) for h in heads)):
        if len({h[idx] for h in heads}) == 1:
            common += 1
        else:
            break
    print(f"common_header_bytes={common}")
    print(f"constant_prefix={heads[0][:common].hex(' ')}")

    # Show the first 256 bytes of the smallest file as a hex dump for eyeballing
    sample = min(files, key=lambda p: p.stat().st_size).read_bytes()[:256]
    print("--- hexdump 0..255 (smallest file) ---")
    for off in range(0, 256, 16):
        chunk = sample[off : off + 16]
        print(f"{off:04d}: {chunk.hex(' ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
