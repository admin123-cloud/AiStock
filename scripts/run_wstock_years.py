"""Batch-run the WStock repair pipeline across every exported year.

For each year directory that exists and contains data, run the same five-gate
verification used for 2006 and stage the accepted bars. Nothing is written to
any formal kline table or to the review ledger.

Usage:
    python scripts/run_wstock_years.py --years 2000 2001 ... --write
    python scripts/run_wstock_years.py --years 2000-2007 --write
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path

SCRIPT = ROOT / "scripts" / "e2e_verify_wstock_repairs.py"
BASE = Path(r"E:\AStockHistory\wstock\输出\来源归档")


def parse_years(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", default="2000-2007")
    ap.add_argument("--tail", default="6")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    years = parse_years(args.years)
    summary = {}
    for year in years:
        d = BASE / f"out_{year}"
        n = sum(1 for _ in d.rglob("*.csv")) if d.exists() else 0
        if n == 0:
            summary[year] = {"status": "no_export", "files": 0}
            print(f"[{year}] no export found in {d} - skipped")
            continue
        cmd = [args.python, str(SCRIPT), "--year", str(year), "--tail", args.tail]
        if args.write:
            cmd.append("--write")
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = proc.returncode == 0
        entry = {"status": "ok" if ok else "failed", "files": n, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}
        try:
            entry["result"] = json.loads(proc.stdout)
        except Exception:
            pass
        summary[year] = entry
        print(f"[{year}] files={n} status={entry['status']} {entry.get('result', {}).get('gates', '')}")

    payload = {"years": years, "tail": args.tail, "write": args.write, "summary": summary}
    out = report_path("purchased_history", "wstock", f"batch_years_{'-'.join(map(str, years))}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    agg: Counter[str] = Counter()
    for y, e in summary.items():
        if e.get("result"):
            for k, v in e["result"].get("gates", {}).items():
                agg[k] += v
    print("\n=== aggregate gates ===")
    print(json.dumps(dict(agg), ensure_ascii=False, indent=2))
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
