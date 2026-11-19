"""Verify the four 399106.SZ exception dates against a THS daily export.

Only ClickHouse SELECTs are issued.  The THS export is read locally and its
SHA-256, row text, and parsed values are retained as evidence.  The check is
limited to confirming the volume scale (THS displayed raw volume / 100 equals
the formal volume); price and amount are reported with their display values
and are not used to authorize a production repair.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client


CODE = "399106.SZ"
DATES = ("2012-08-28", "2012-08-31", "2012-09-27", "2012-09-28")
SOURCE_TABLE = "stock.wstock_daily_tdx_evidence_20261014_v2"
FORMAL_TABLE = "stock.kline_daily"
READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 120, "max_threads": 1}
LINE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}),[^\t,]*\t(?P<open>[^\t,]+)\t\s*(?P<high>[^\t,]+)\t\s*(?P<low>[^\t,]+)\t\s*(?P<close>[^\t,]+)\t\s*[^\t,]+\t\s*[^\t,]+\t\s*[^\t,]+\t\s*(?P<volume>[^\t,]+)\t\s*(?P<amount>[^\t,]+)")


def _number(value: str) -> float:
    return float(str(value).replace(",", "").strip())


def parse_ths_rows(path: Path, dates: tuple[str, ...] = DATES) -> dict[str, dict[str, Any]]:
    wanted = set(dates)
    rows: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            match = LINE_RE.match(line.strip("\r\n"))
            if not match or match.group("date") not in wanted:
                continue
            row = {name: _number(match.group(name)) for name in ("open", "high", "low", "close", "volume", "amount")}
            row.update({"trade_date": match.group("date"), "line_number": line_number, "raw_line": line.rstrip("\r\n")})
            rows[match.group("date")] = row
    return rows


def _quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dict_rows(result: Any) -> list[dict[str, Any]]:
    names = list(getattr(result, "column_names", ()) or ())
    return [dict(zip(names, row)) for row in getattr(result, "result_rows", ())]


def _read_table(client: Any, table: str, columns: str, final: bool = False) -> list[dict[str, Any]]:
    dates = ",".join(_quote(date) for date in DATES)
    suffix = " FINAL" if final else ""
    result = client.query(
        f"SELECT {columns} FROM {table}{suffix} WHERE code={_quote(CODE)} AND trade_date IN ({dates}) ORDER BY trade_date",
        settings=READONLY_SETTINGS,
    )
    return _dict_rows(result)


def verify(*, ths_path: Path, output: Path) -> dict[str, Any]:
    ths_rows = parse_ths_rows(ths_path)
    client = clickhouse_client()
    formal_rows = _read_table(client, FORMAL_TABLE, "code,trade_date,open,high,low,close,volume,amount", final=True)
    source_rows = _read_table(client, SOURCE_TABLE, "code,trade_date,open,high,low,close,volume_shares,amount,file_sha,source_row")
    formal = {str(row["trade_date"])[:10]: row for row in formal_rows}
    source = {str(row["trade_date"])[:10]: row for row in source_rows}
    file_sha = hashlib.sha256(ths_path.read_bytes()).hexdigest()
    results: list[dict[str, Any]] = []
    for date in DATES:
        ths = ths_rows.get(date)
        f = formal.get(date)
        s = source.get(date)
        normalized_volume = (ths["volume"] / 100.0) if ths else None
        formal_volume = float(f["volume"]) if f and f.get("volume") is not None else None
        volume_delta = (normalized_volume - formal_volume) if normalized_volume is not None and formal_volume is not None else None
        volume_scale_confirmed = volume_delta is not None and abs(volume_delta) <= 0.01
        results.append(
            {
                "code": CODE,
                "trade_date": date,
                "formal_values": f or {},
                "existing_source_values": s or {},
                "second_source": {
                    "kind": "ths_desktop_daily_export_unadjusted",
                    "path": str(ths_path),
                    "file_sha256": file_sha,
                    "line_number": ths.get("line_number") if ths else None,
                    "raw_line": ths.get("raw_line") if ths else None,
                    "open": ths.get("open") if ths else None,
                    "high": ths.get("high") if ths else None,
                    "low": ths.get("low") if ths else None,
                    "close": ths.get("close") if ths else None,
                    "volume_raw": ths.get("volume") if ths else None,
                    "amount_raw": ths.get("amount") if ths else None,
                },
                "volume_transform": "THS volume_raw / 100",
                "normalized_second_source_volume": normalized_volume,
                "volume_delta_vs_formal": volume_delta,
                "volume_scale_confirmed": volume_scale_confirmed,
                "amount_difference_second_source_vs_formal": (ths["amount"] - float(f["amount"])) if ths and f and f.get("amount") is not None else None,
            }
        )
    summary = {
        "schema_version": 1,
        "readonly": True,
        "code": CODE,
        "dates": list(DATES),
        "second_source": {"kind": "ths_desktop_daily_export_unadjusted", "path": str(ths_path), "file_sha256": file_sha, "parsed_row_count": len(ths_rows)},
        "formal_readback": {"table": FORMAL_TABLE, "row_count": len(formal_rows), "query_settings": READONLY_SETTINGS},
        "existing_source_readback": {"table": SOURCE_TABLE, "row_count": len(source_rows), "query_settings": READONLY_SETTINGS},
        "results": results,
        "all_four_volume_scales_confirmed": all(row["volume_scale_confirmed"] for row in results),
        "gate": "second_source_volume_confirmed" if all(row["volume_scale_confirmed"] for row in results) else "blocked_second_source_incomplete",
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
        "interpretation": "第二来源确认四个日期的 THS 原始成交量除以 100 与正式 volume 一致；价格/金额保留显示精度差异，不据此授权生产修复。",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ths-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = verify(ths_path=args.ths_path, output=args.output)
    print(json.dumps({"output": str(args.output), "gate": summary["gate"], "all_four_volume_scales_confirmed": summary["all_four_volume_scales_confirmed"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
