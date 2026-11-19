"""Deeper analysis of .wdz: is the payload encrypted or compressed?

Checks:
  1. byte-pair repetition (a real compressor leaves structure even at high entropy)
  2. chi-square uniformity
  3. whether the first u32 correlates with file size (length prefix?)
  4. whether XOR of two files' headers yields structure
  5. presence of any recognizable magic after common transforms

Read-only.
"""

from __future__ import annotations

import collections
import math
import struct
import zlib
from pathlib import Path

WDZ = Path(r"E:\AStockHistory\wstock\原始压缩包\WDZ")


def chi_square(data: bytes) -> float:
    counts = collections.Counter(data)
    n = len(data)
    exp = n / 256.0
    return sum((counts.get(i, 0) - exp) ** 2 / exp for i in range(256))


def main() -> None:
    files = sorted(WDZ.glob("*.wdz"))
    print("=== per-file header + size relation ===")
    for p in files:
        raw = p.read_bytes()
        u = struct.unpack("<8I", raw[:32])
        print(f"{p.name}: size={len(raw):>10} u32[0]={u[0]:>3} u32[1]={u[1]:>3} "
              f"size/u32[0]={len(raw)/max(u[0],1):.0f}")

    sample = files[2].read_bytes()[: 4 << 20]
    print(f"\n=== sample {files[2].name} first 4MB ===")
    print(f"chi_square = {chi_square(sample):.1f}  (uniform ~{256*255:.0f} avg)")
    print(f"distinct bytes = {len(set(sample))}/256")

    # Byte-pair entropy: a stream cipher keeps pairs uniform; a compressor does not.
    pairs = collections.Counter(zip(sample, sample[1:]))
    print(f"distinct byte-pairs = {len(pairs)} (max 65536)")

    # Try common wrappers on the raw payload (skip 16-byte constant header)
    for off in (0, 16, 32):
        body = sample[off : off + 1 << 20]
        for name, fn in (
            ("zlib", lambda b: zlib.decompress(b)),
            ("gzip", lambda b: zlib.decompress(b, 16 + zlib.MAX_WBITS)),
            ("raw-deflate", lambda b: zlib.decompress(b, -zlib.MAX_WBITS)),
        ):
            try:
                out = fn(body)
                print(f"  offset {off} {name}: DECOMPRESSED {len(out)} bytes")
            except Exception as exc:
                print(f"  offset {off} {name}: no ({type(exc).__name__})")

    # Is the header XOR of a constant? compare 2000 vs 2002 (same header) and 2004 (differing)
    a = files[0].read_bytes()[:16]
    b = files[4].read_bytes()[:16]  # 2004
    print(f"\nXOR 2000^2004 header: {bytes(x ^ y for x, y in zip(a, b)).hex(' ')}")

    # Look at the second u32: 68 everywhere = 0x44. Possibly a record/version marker.
    print("\n=== u32[1] across files (0x44 = 68?) ===")
    for p in files:
        print(f"  {p.name}: u32[1]={struct.unpack('<I', p.read_bytes()[4:8])[0]}")


if __name__ == "__main__":
    main()
