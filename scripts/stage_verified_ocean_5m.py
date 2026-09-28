from __future__ import annotations

"""No-database validation entry point for the independent Ocean 5m stage.

This command verifies the downloader marker and archive, validates CSV rows,
and prints a stage/audit summary.  It deliberately has no worker loop, no
ClickHouse connection, and no formal-promotion path.  A future controlled
writer may inject ``ClickHouseOceanStageSink`` after this contract is approved.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.ocean_stage import (
    LocalOceanStageSink,
    ocean_day_audit_ddl,
    ocean_stage_ddl,
    stage_verified_archive,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument(
        "--accepted-stock",
        action="append",
        default=[],
        help="Canonical code such as 600000.SH; repeatable; no master-table lookup is performed.",
    )
    parser.add_argument(
        "--accepted-stock-file",
        type=Path,
        help="UTF-8 text file with one canonical stock code per line; read-only.",
    )
    parser.add_argument(
        "--local-root",
        type=Path,
        help="Optional local isolated evidence root; writes only atomic JSONL/manifest files, never DB.",
    )
    parser.add_argument("--print-schema", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    accepted = list(args.accepted_stock)
    if args.accepted_stock_file:
        accepted.extend(
            line.strip()
            for line in args.accepted_stock_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    if not accepted:
        print("accepted_stock_whitelist_required", file=sys.stderr)
        return 2
    if args.print_schema:
        print(ocean_stage_ddl())
        print()
        print(ocean_day_audit_ddl())
    sink = LocalOceanStageSink(args.local_root) if args.local_root else None
    report = stage_verified_archive(args.archive, accepted_stock_codes=accepted, sink=sink)
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
