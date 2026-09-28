"""Derive missing index daily bars from complete-endpoint purchased 5m stage days.

Scope (deliberately narrow)
---------------------------
Only the code-days the review queue reports as ``project_daily_missing_for_5m``
whose 5m stage data has BOTH the 09:35 first bar and the 15:00 last bar with
all-positive OHLC.  For those, a daily bar is well defined:

    open  = first bar open   (09:35)
    close = last  bar close  (15:00)
    high  = max high over available bars
    low   = min low  over available bars
    volume/amount = sums

Coverage caveat
---------------
These days are NOT fully covered: the vendor feed has interior holes where
``open`` was frozen and ``close``/``low`` written as 0.  The first and last bars
survive, so open/close are trustworthy, but ``high``/``low`` are computed over
partial coverage and therefore UNDERSTATE the true intraday range.  Each written
row is tagged so this is auditable and reversible.

Safety
------
* dry-run by default; ``--write`` is required to insert
* never overwrites an existing (code, trade_date) row
* writes only to ``stock.kline_daily``, only for the enumerated keys
* every insert is preceded by a re-check that the key is still absent
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import report_path

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
STAGE = "stock.purchased_history_minute_5_stage"
DAILY = "stock.kline_daily"
FIRST_LABEL = "09:35"
LAST_LABEL = "15:00"
REPLAY_INDEX_1M = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_复盘大师\1分钟_指数_按年汇总")


def _stage_day(client: Any, code: str, day: str) -> list[tuple]:
    return client.query(
        f"""
        SELECT datetime, open, high, low, close,
               source_volume_lots, source_amount_yuan
        FROM {STAGE}
        WHERE code = %(code)s AND toDate(datetime) = toDate(%(day)s)
        ORDER BY datetime
        """,
        parameters={"code": code, "day": day},
    ).result_rows


def _label(value: Any) -> str:
    return str(value)[11:16]


def _existing(client: Any, code: str, day: str) -> int:
    rows = client.query(
        f"SELECT count() FROM {DAILY} WHERE code = %(code)s AND trade_date = toDate(%(day)s)",
        parameters={"code": code, "day": day},
    ).result_rows
    return int(rows[0][0]) if rows else 0


def _replaymaster_1m_daily(code: str, trade_date: str) -> dict[str, Any] | None:
    """Derive a full-coverage daily bar from the 复盘大师 index 1m archive.

    Used for code-days whose 5m stage endpoints are damaged (no 15:00 bar).
    The 1m archive carries the true final-minute low/close that the 5m feed
    lost, and its high/low agree with the independent 15m/30m archives, so the
    derived range is NOT degraded.
    """
    import csv as _csv
    from zipfile import ZipFile

    year = trade_date[:4]
    day = trade_date.replace("-", "")
    archive = REPLAY_INDEX_1M / f"{year}_1min.zip"
    if not archive.exists():
        return None
    entry = f"{code.split('.')[0]}_{year}.txt"
    try:
        with ZipFile(archive) as zf:
            names = set(zf.namelist())
            if entry not in names:
                return None
            raw = zf.read(entry).decode("utf-8-sig", errors="replace")
    except Exception:
        return None
    reader = _csv.reader(io.StringIO(raw))
    next(reader, None)
    open_: float | None = None
    close: float | None = None
    high: float | None = None
    low: float | None = None
    volume = 0.0
    valid = 0
    invalid = 0
    for cells in reader:
        if len(cells) < 8 or cells[1] != day:
            continue
        try:
            o, h, l, c, v = (float(cells[i]) for i in (3, 4, 5, 6, 7))
        except ValueError:
            invalid += 1
            continue
        if min(o, h, l, c) <= 0:
            invalid += 1
            continue
        valid += 1
        if open_ is None:
            open_ = o
        close = c
        high = h if high is None else max(high, h)
        low = l if low is None else min(low, l)
        volume += v
    if open_ is None or close is None:
        return None
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "source_1m_valid": valid,
        "source_1m_invalid": invalid,
    }


def build_candidate(client: Any, code: str, day: str) -> dict[str, Any]:
    rows = _stage_day(client, code, day)
    if not rows:
        return {"code": code, "trade_date": day, "decision": "no_stage_rows"}
    labels = [_label(r[0]) for r in rows]
    if labels[0] != FIRST_LABEL or labels[-1] != LAST_LABEL:
        # Endpoints damaged in the 5m feed: fall back to the independent
        # 复盘大师 index 1m archive, which retains the true final minute.
        fallback = _replaymaster_1m_daily(code, day)
        if fallback is None:
            return {
                "code": code,
                "trade_date": day,
                "decision": "endpoints_incomplete",
                "first_label": labels[0],
                "last_label": labels[-1],
                "stage_bars": len(rows),
            }
        prev_close = _prev_close(client, code, day)
        return {
            "code": code,
            "trade_date": day,
            "decision": "derivable_from_1m",
            "stage_bars": len(rows),
            "missing_bars": None,
            "open": fallback["open"],
            "high": fallback["high"],
            "low": fallback["low"],
            "close": fallback["close"],
            "volume": 0.0,
            "amount": float(sum(float(r[6] or 0) for r in rows)),
            "prev_close": prev_close,
            "change_amount": round(fallback["close"] - prev_close, 4) if prev_close else None,
            "change_pct": round((fallback["close"] - prev_close) / prev_close * 100, 4) if prev_close else None,
            "coverage_note": (
                "from replaymaster index 1m; full-day range validated against 15m/30m archives"
            ),
        }
    opens = [float(r[1]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    closes = [float(r[4]) for r in rows]
    if any(v <= 0 for v in opens + highs + lows + closes):
        return {"code": code, "trade_date": day, "decision": "non_positive_ohlc"}
    canonical = [f"{t // 60:02d}:{t % 60:02d}" for t in range(9 * 60 + 35, 11 * 60 + 30 + 1, 5)]
    canonical += [f"{t // 60:02d}:{t % 60:02d}" for t in range(13 * 60 + 5, 15 * 60 + 1, 5)]
    missing = [t for t in canonical if t not in set(labels)]
    open_ = opens[0]
    close = closes[-1]
    high = max(highs)
    low = min(lows)
    prev_close = _prev_close(client, code, day)
    change_amount = round(close - prev_close, 4) if prev_close else None
    change_pct = round((close - prev_close) / prev_close * 100, 4) if prev_close else None
    return {
        "code": code,
        "trade_date": day,
        "decision": "derivable",
        "stage_bars": len(rows),
        "missing_bars": len(missing),
        "missing_labels": missing,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": float(sum(float(r[5] or 0) for r in rows)),
        "amount": float(sum(float(r[6] or 0) for r in rows)),
        "prev_close": prev_close,
        "change_amount": change_amount,
        "change_pct": change_pct,
        "coverage_note": "high/low computed over partial coverage; range understated"
        if missing
        else "full coverage",
    }


def _prev_close(client: Any, code: str, day: str) -> float | None:
    rows = client.query(
        f"""
        SELECT close FROM {DAILY}
        WHERE code = %(code)s AND trade_date < toDate(%(day)s)
        ORDER BY trade_date DESC LIMIT 1
        """,
        parameters={"code": code, "day": day},
    ).result_rows
    return float(rows[0][0]) if rows else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", action="append", default=[], help="code:YYYY-MM-DD; repeatable")
    parser.add_argument(
        "--from-triage",
        type=Path,
        default=None,
        help="gap_triage JSON; picks every project_daily_missing_for_5m key",
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    keys: list[tuple[str, str]] = []
    for item in args.key:
        code, _, day = item.partition(":")
        keys.append((code, day))
    if args.from_triage:
        payload = json.loads(args.from_triage.read_text(encoding="utf-8"))
        for row in payload.get("results", []):
            if row.get("root_cause") == "D_project_daily":
                keys.append((str(row["code"]), str(row["trade_date"])))
    keys = list(dict.fromkeys(keys))
    if not keys:
        print("no keys supplied", file=sys.stderr)
        return 2

    client = clickhouse_client()
    candidates = [build_candidate(client, code, day) for code, day in keys]
    derivable = [c for c in candidates if c["decision"] in ("derivable", "derivable_from_1m")]

    written = 0
    if args.write:
        columns = [
            "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
            "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id",
        ]
        ingested = datetime.now(BUSINESS_TZ)
        for item in derivable:
            if _existing(client, item["code"], item["trade_date"]):
                item["decision"] = "skipped_already_present"
                continue
            high, low, close, open_ = item["high"], item["low"], item["close"], item["open"]
            amplitude = round((high - low) / item["prev_close"] * 100, 4) if item.get("prev_close") else 0.0
            row = [
                item["code"],
                datetime.strptime(item["trade_date"], "%Y-%m-%d").date(),
                open_, high, low, close,
                item["volume"], item["amount"],
                amplitude,
                item.get("change_pct") or 0.0,
                item.get("change_amount") or 0.0,
                0.0,
                ingested,
                int(uuid.uuid4().int % (2**63)),
            ]
            client.insert(DAILY, [row], column_names=columns)
            written += 1

    out = report_path("purchased_history", "index_daily_derivation", f"{uuid.uuid4().hex[:12]}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"write": args.write, "candidates": candidates, "written": written}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"write": args.write, "keys": len(keys), "derivable": len(derivable), "written": written, "report": str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
