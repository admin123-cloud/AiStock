"""Repair validated historical BSE daily-volume rows from local TDX files.

The affected rows keep correct OHLC and amount values but have a mis-scaled
volume. This script only updates rows whose local TDX bar matches the stored
OHLC and amount, and remains dry-run by default.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


DAY_RECORD = struct.Struct("<IIIIIfII")
PRICE_TOLERANCE = 0.011
AMOUNT_RELATIVE_TOLERANCE = 0.00002
AMOUNT_ABSOLUTE_TOLERANCE = 2.0


@dataclass(frozen=True)
class StoredBar:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass(frozen=True)
class SourceBar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair validated historical BSE daily volume from local TDX day files."
    )
    parser.add_argument("--start-date", default="2022-04-12")
    parser.add_argument("--end-date", default="2022-04-14")
    parser.add_argument("--codes", default="", help="Optional comma-separated BSE codes.")
    parser.add_argument("--tdx-root", default=r"D:\TDX\vipdoc")
    parser.add_argument("--apply", action="store_true", help="Apply validated volume updates.")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--batch-size", type=int, default=200, help="Maximum updates per ClickHouse mutation.")
    return parser.parse_args()


def _quote_sql(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _canonical_code(value: str) -> str:
    code = str(value or "").strip().upper()
    return code if "." in code else f"{code}.BJ"


def _parse_trade_date(raw_value: int) -> date | None:
    text = str(int(raw_value))
    if len(text) != 8:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def read_tdx_day_file(path: Path, start_date: date, end_date: date) -> dict[date, SourceBar]:
    """Read the TDX daily records for the requested date window."""
    result: dict[date, SourceBar] = {}
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(DAY_RECORD.size)
            if not chunk or len(chunk) < DAY_RECORD.size:
                break
            raw_day, open_i, high_i, low_i, close_i, amount_f, volume_i, _reserved = DAY_RECORD.unpack(chunk)
            trade_date = _parse_trade_date(raw_day)
            if trade_date is None or not (start_date <= trade_date <= end_date):
                continue
            result[trade_date] = SourceBar(
                trade_date=trade_date,
                open=float(open_i) / 100.0,
                high=float(high_i) / 100.0,
                low=float(low_i) / 100.0,
                close=float(close_i) / 100.0,
                volume=float(round(float(volume_i or 0) / 100.0, 0)),
                amount=float(amount_f or 0.0),
            )
    return result


def load_candidates(
    start_date: date,
    end_date: date,
    codes: list[str],
) -> list[StoredBar]:
    code_filter = ""
    if codes:
        quoted = ", ".join(_quote_sql(code) for code in codes)
        code_filter = f"AND code IN ({quoted})"
    frame = clickhouse_query_df(
        f"""
        SELECT code, trade_date, open, high, low, close, volume, amount
        FROM kline_daily FINAL
        WHERE code LIKE '92%.BJ'
          AND trade_date BETWEEN toDate({_quote_sql(start_date)})
                             AND toDate({_quote_sql(end_date)})
          AND volume > 0
          AND close > 0
          AND amount > 0
          AND (
              amount / (volume * close * 100.0) > 1.2
              OR amount / (volume * close * 100.0) < 0.8
          )
          {code_filter}
        ORDER BY code, trade_date
        """
    )
    if frame.empty:
        return []
    result: list[StoredBar] = []
    for row in frame.to_dict(orient="records"):
        result.append(
            StoredBar(
                code=str(row["code"]).upper(),
                trade_date=pd.to_datetime(row["trade_date"]).date(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                amount=float(row["amount"]),
            )
        )
    return result


def validate_source_bar(stored: StoredBar, source: SourceBar) -> tuple[bool, str]:
    """Require OHLC and amount agreement before allowing a volume update."""
    for field in ("open", "high", "low", "close"):
        if abs(getattr(stored, field) - getattr(source, field)) > PRICE_TOLERANCE:
            return False, f"{field}_mismatch"
    amount_tolerance = max(
        AMOUNT_ABSOLUTE_TOLERANCE,
        abs(source.amount) * AMOUNT_RELATIVE_TOLERANCE,
    )
    if abs(stored.amount - source.amount) > amount_tolerance:
        return False, "amount_mismatch"
    if source.volume <= 0:
        return False, "source_volume_not_positive"
    if abs(source.volume - stored.volume) <= 0.01:
        return False, "volume_already_matches"
    return True, "validated"


def _tdx_file_path(tdx_root: Path, code: str) -> Path:
    return tdx_root / "bj" / "lday" / f"bj{code.split('.', 1)[0]}.day"


def build_updates(
    candidates: list[StoredBar],
    tdx_root: Path,
    start_date: date,
    end_date: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_cache: dict[str, dict[date, SourceBar]] = {}
    updates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for stored in candidates:
        if stored.code not in source_cache:
            path = _tdx_file_path(tdx_root, stored.code)
            if not path.exists():
                source_cache[stored.code] = {}
                rejected.append(
                    {
                        "code": stored.code,
                        "trade_date": stored.trade_date.isoformat(),
                        "reason": "tdx_file_missing",
                        "path": str(path),
                    }
                )
                continue
            source_cache[stored.code] = read_tdx_day_file(path, start_date, end_date)
        source = source_cache[stored.code].get(stored.trade_date)
        if source is None:
            rejected.append(
                {
                    "code": stored.code,
                    "trade_date": stored.trade_date.isoformat(),
                    "reason": "tdx_bar_missing",
                }
            )
            continue
        valid, reason = validate_source_bar(stored, source)
        if not valid:
            rejected.append(
                {
                    "code": stored.code,
                    "trade_date": stored.trade_date.isoformat(),
                    "reason": reason,
                }
            )
            continue
        updates.append(
            {
                "code": stored.code,
                "trade_date": stored.trade_date.isoformat(),
                "old_volume": stored.volume,
                "new_volume": source.volume,
                "source": "local_tdx",
                "source_path": str(_tdx_file_path(tdx_root, stored.code)),
            }
        )
    return updates, rejected


def apply_updates(updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    client = clickhouse_client()
    for item in updates:
        client.command(
            "ALTER TABLE kline_daily "
            f"UPDATE volume = {float(item['new_volume']):.1f} "
            f"WHERE code = {_quote_sql(item['code'])} "
            f"AND trade_date = toDate({_quote_sql(item['trade_date'])}) "
            "SETTINGS mutations_sync = 1"
        )


def apply_updates_batched(updates: list[dict[str, Any]], batch_size: int) -> None:
    if not updates:
        return
    size = max(1, int(batch_size or 1))
    client = clickhouse_client()
    for start in range(0, len(updates), size):
        batch = updates[start : start + size]
        branches: list[str] = []
        keys: list[str] = []
        for item in batch:
            condition = (
                f"code = {_quote_sql(item['code'])} "
                f"AND trade_date = toDate({_quote_sql(item['trade_date'])})"
            )
            branches.extend([condition, f"{float(item['new_volume']):.1f}"])
            keys.append(
                f"({_quote_sql(item['code'])}, toDate({_quote_sql(item['trade_date'])}))"
            )
        update_expression = "multiIf(" + ", ".join(branches + ["volume"]) + ")"
        client.command(
            "ALTER TABLE kline_daily "
            f"UPDATE volume = {update_expression} "
            f"WHERE (code, trade_date) IN ({', '.join(keys)}) "
            "SETTINGS mutations_sync = 1"
        )


def write_report(report: dict[str, Any], report_value: str) -> str:
    target = Path(report_value) if report_value else Path(
        report_path(
            "repair_bj_historical_volume",
            f"bj_volume_{datetime.now():%Y%m%d_%H%M%S}.json",
        )
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def run(args: argparse.Namespace) -> dict[str, Any]:
    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    if start_date > end_date:
        raise ValueError("start-date must not be later than end-date")
    codes = [_canonical_code(item) for item in args.codes.split(",") if item.strip()]
    if any(not code.startswith("92") or not code.endswith(".BJ") for code in codes):
        raise ValueError("codes must be 92xxxxx.BJ BSE codes")

    candidates = load_candidates(start_date, end_date, codes)
    updates, rejected = build_updates(
        candidates,
        Path(args.tdx_root),
        start_date,
        end_date,
    )
    report: dict[str, Any] = {
        "ok": True,
        "applied": False,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "tdx_root": args.tdx_root,
        "candidate_rows": len(candidates),
        "validated_updates": len(updates),
        "rejected_rows": len(rejected),
        "updates": updates,
        "rejected": rejected,
    }
    if args.apply:
        apply_updates_batched(updates, args.batch_size)
        report["applied"] = True
    report["report_path"] = write_report(report, args.report_path)
    return report


def main() -> int:
    args = parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
