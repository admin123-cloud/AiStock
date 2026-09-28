"""Confirm whether .wdz is stream-cipher encrypted, and rule out simple XOR/CFB reuse.

Tests:
  1. Compare byte-pair statistics against a known-good large sample (a real ZIP
     deflate stream) to show .wdz is *not* merely compressed.
  2. Test for repeating keystream: if the same key were reused with no IV, two
     files that share an identical plaintext prefix (e.g. the same leading
     stock/date) would share ciphertext. Compare aligned windows across files.
  3. Check whether XOR of the first N bytes of two files contains structure.
  4. Verify u32[0] is not a length field by correlating with real content.

Read-only.
"""

from __future__ import annotations

import collections
import zlib
from pathlib import Path

WDZ = Path(r"E:\AStockHistory\wstock\原始压缩包\WDZ")
ZIP_SAMPLE = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按月归档\2006-12\20061215_5min.zip")


def pair_stats(data: bytes) -> tuple[int, int, float]:
    counts = collections.Counter(data)
    n = len(data)
    exp = n / 256.0
    chi = sum((counts.get(i, 0) - exp) ** 2 / exp for i in range(256))
    pairs = len(set(zip(data, data[1:])))
    return len(set(data)), pairs, chi


def main() -> None:
    wdz = sorted(WDZ.glob("*.wdz"))
    a = wdz[2].read_bytes()[16 : (1 << 20) + 16]
    b = wdz[5].read_bytes()[16 : (1 << 20) + 16]

    print("=== .wdz payload (offset16, 1MB) ===")
    d, p, c = pair_stats(a)
    print(f"  distinct bytes={d}  distinct pairs={p}/65536  chi2={c:.1f}")

    print("\n=== ZIP deflate control (real compressed data) ===")
    z = ZIP_SAMPLE.read_bytes()[1000 : 1000 + (1 << 20)]
    d, p, c = pair_stats(z)
    print(f"  distinct bytes={d}  distinct pairs={p}/65536  chi2={c:.1f}")

    print("\n=== keystream-reuse test: XOR of two .wdz payloads ===")
    x = bytes(i ^ j for i, j in zip(a[:65536], b[:65536]))
    d, p, c = pair_stats(x)
    print(f"  XOR distinct bytes={d}  distinct pairs={p}/65536  chi2={c:.1f}")
    print("  (if a keystream repeated, XOR would show LOW entropy / few pairs)")

    print("\n=== is u32[0] a length? vs true payload size ===")
    for f in wdz:
        raw = f.read_bytes()
        import struct
        (u0,) = struct.unpack("<I", raw[:4])
        print(f"  {f.name}: u32[0]={u0} size={len(raw)} ratio={len(raw)/u0:.1f}")


if __name__ == "__main__":
    main()
