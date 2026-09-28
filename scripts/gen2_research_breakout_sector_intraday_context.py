from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client  # noqa: E402


COMBO = ROOT / "reports" / "gen2_breakout_buy_point_research" / "breakout_family_intraday_strength_probe" / "combo_policy_probe"
MANUAL = COMBO / "manual_review_2026ytd"
OUT = COMBO / "sector_context_probe"


@lru_cache(maxsize=1)
def _ch():
    return clickhouse_client()


@lru_cache(maxsize=1)
def _members() -> pd.DataFrame:
    q = """
    SELECT ss.stock_code, s.code AS sector_code, s.name AS sector_name, s.level, s.stock_count
    FROM sector_stocks ss
    JOIN sectors s ON ss.sector_code = s.code
    WHERE s.type = 'industry'
    """
    return _ch().query_df(q)


@lru_cache(maxsize=4096)
def _sector_intraday(sector_code: str, trade_date: str, confirm_time: str) -> dict[str, Any]:
    members = _members()
    codes = members.loc[members["sector_code"] == sector_code, "stock_code"].dropna().astype(str).unique().tolist()
    if not codes:
        return {}
    quoted = ", ".join(f"'{code}'" for code in codes)
    dt = f"{trade_date} {confirm_time}:00"
    q = f"""
    SELECT m.code, m.close AS intraday_close, d.close AS prev_close
    FROM
    (
        SELECT code, close
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime = toDateTime('{dt}')
    ) m
    INNER JOIN
    (
        SELECT code, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date = (
              SELECT max(trade_date)
              FROM kline_daily
              WHERE code IN ({quoted}) AND trade_date < toDate('{trade_date}')
          )
    ) d ON m.code = d.code
    """
    df = _ch().query_df(q)
    if df.empty:
        return {}
    df["intraday_close"] = pd.to_numeric(df["intraday_close"], errors="coerce")
    df["prev_close"] = pd.to_numeric(df["prev_close"], errors="coerce")
    df = df[(df["prev_close"] > 0) & df["intraday_close"].notna()].copy()
    if df.empty:
        return {}
    df["rt_ret"] = df["intraday_close"] / df["prev_close"] - 1.0
    return {
        "rt_member_bars": int(len(df)),
        "rt_avg_return": float(df["rt_ret"].mean()),
        "rt_rise_ratio": float((df["rt_ret"] > 0).mean()),
        "rt_strong3_ratio": float((df["rt_ret"] >= 0.03).mean()),
        "rt_strong5_ratio": float((df["rt_ret"] >= 0.05).mean()),
        "rt_top_return": float(df["rt_ret"].max()),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tickets = pd.read_csv(MANUAL / "2026ytd_review_tickets.csv")
    rows = []
    members = _members()
    for _, ticket in tickets.iterrows():
        sectors = members[members["stock_code"] == str(ticket["code"])].sort_values("level")
        for _, sec in sectors.iterrows():
            stats = _sector_intraday(str(sec["sector_code"]), str(ticket["date"]), str(ticket["confirm_time"]))
            row = {
                "review_type": ticket["review_type"],
                "date": ticket["date"],
                "code": ticket["code"],
                "name": ticket["name"],
                "confirm_time": ticket["confirm_time"],
                "return": ticket["return"],
                "sector_code": sec["sector_code"],
                "sector_name": sec["sector_name"],
                "sector_level": int(sec["level"]),
                "sector_stock_count": int(sec["stock_count"]),
            }
            row.update(stats)
            rows.append(row)
    out = pd.DataFrame(rows)
    path = OUT / "2026ytd_review_ticket_sector_intraday_levels.csv"
    out.to_csv(path, index=False, encoding="utf-8-sig")
    print(json.dumps({"path": str(path), "rows": len(out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
