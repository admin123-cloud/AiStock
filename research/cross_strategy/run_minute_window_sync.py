"""
按日期窗口执行分钟K线同步（基于 sync_all_klines.KlineSyncer）。

默认用途：
- 股票 + 指数
- 5m + 15m
- 最近 N 个交易日（默认 120）
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
import sys
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sync_all_klines import KlineSyncer
from utils.market_warehouse import clickhouse_query_df


def _resolve_trade_window(end_date: str, days: int, market: str = "SH") -> tuple[str, str, int]:
    df = clickhouse_query_df(
        """
        SELECT trade_date
        FROM trade_calendar
        WHERE market = ?
          AND is_trading = 1
          AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        [market, end_date, int(days)],
    )
    if df.empty:
        raise RuntimeError("trade_calendar 无可用交易日")
    ds = sorted([str(x)[:10] for x in df["trade_date"].tolist()])
    return ds[0], ds[-1], len(ds)


def _run_period(syncer: KlineSyncer, period: str, symbols: List[Dict[str, str]], start_date: str, end_date: str, workers: int) -> Dict[str, int]:
    total = len(symbols)
    ok = 0
    fail = 0
    done = 0
    print(f"\n[START] period={period}, symbols={total}, window={start_date}~{end_date}, workers={workers}")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        fut_map = {
            ex.submit(syncer.sync_kline_for_stock, s, period, start_date, end_date): s
            for s in symbols
        }
        for fut in as_completed(fut_map):
            s = fut_map[fut]
            code = s.get("code", "?")
            try:
                if fut.result():
                    ok += 1
                else:
                    fail += 1
            except Exception as exc:
                fail += 1
                print(f"[FAIL] period={period} code={code} err={type(exc).__name__}: {exc}")
            done += 1
            if done % 50 == 0 or done == total:
                print(f"[PROGRESS] period={period} {done}/{total} ok={ok} fail={fail}")
    print(f"[DONE] period={period} ok={ok} fail={fail} total={total}")
    return {"ok": ok, "fail": fail, "total": total}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="按交易日窗口同步 5m/15m 分钟K线")
    p.add_argument("--end-date", default=str(date.today()), help="结束日期 YYYY-MM-DD")
    p.add_argument("--days", type=int, default=120, help="向前交易日数量")
    p.add_argument("--workers", type=int, default=3, help="并发数")
    p.add_argument("--types", default="stock,index", help="逗号分隔：stock,index")
    p.add_argument("--periods", default="5m,15m", help="逗号分隔：5m,15m")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    start_date, end_date, trading_days = _resolve_trade_window(args.end_date, args.days, market="SH")
    syncer = KlineSyncer()

    types = [x.strip() for x in args.types.split(",") if x.strip()]
    periods = [x.strip() for x in args.periods.split(",") if x.strip()]

    symbols: List[Dict[str, str]] = []
    if "stock" in types:
        s = syncer.get_all_stocks()
        print(f"[LOAD] stock={len(s)}")
        symbols.extend(s)
    if "index" in types:
        i = syncer.get_all_indices()
        print(f"[LOAD] index={len(i)}")
        symbols.extend(i)

    # 去重
    uniq = {}
    for item in symbols:
        code = str(item.get("code", "")).strip().upper()
        if code:
            uniq[code] = item
    symbols = list(uniq.values())
    print(f"[WINDOW] {start_date}~{end_date}, trading_days={trading_days}, symbols={len(symbols)}, periods={periods}")

    final = {}
    for p in periods:
        final[p] = _run_period(syncer, p, symbols, start_date, end_date, args.workers)

    print(f"\n[SUMMARY] {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

