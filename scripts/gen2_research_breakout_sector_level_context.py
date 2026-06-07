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
def _sector_daily(sector_code: str, date: str) -> dict[str, Any]:
    codes = _members().loc[_members()["sector_code"] == sector_code, "stock_code"].dropna().astype(str).unique().tolist()
    if not codes:
        return {}
    quoted = ", ".join(f"'{code}'" for code in codes)
    start = (pd.Timestamp(date) - pd.Timedelta(days=160)).strftime("%Y-%m-%d")
    q = f"""
    SELECT trade_date, code, change_pct, amount
    FROM kline_daily
    WHERE code IN ({quoted})
      AND trade_date BETWEEN '{start}' AND '{date}'
    ORDER BY trade_date, code
    """
    df = _ch().query_df(q)
    if df.empty:
        return {}
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce")
    rows = []
    for trade_date, g in df.groupby("trade_date"):
        g = g.dropna(subset=["change_pct"])
        if g.empty:
            continue
        rows.append(
            {
                "trade_date": trade_date,
                "avg_change_pct": float(g["change_pct"].mean()),
                "rise_ratio": float((g["change_pct"] > 0).mean()),
                "strong_ratio": float((g["change_pct"] >= 3).mean()),
                "limit_up_count": int((g["change_pct"] >= 9.9).sum()),
                "member_bars": int(len(g)),
            }
        )
    hist = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    if hist.empty:
        return {}
    hist["idx"] = (1 + hist["avg_change_pct"] / 100).cumprod()
    hist["ma5"] = hist["idx"].rolling(5, min_periods=3).mean()
    hist["ma20"] = hist["idx"].rolling(20, min_periods=10).mean()
    hist["ret5"] = hist["idx"].pct_change(5)
    hist["ret20"] = hist["idx"].pct_change(20)
    hist["prior20_high"] = hist["idx"].rolling(20, min_periods=10).max().shift(1)
    hist["vs20h"] = hist["idx"] / hist["prior20_high"] - 1
    prior = hist[hist["trade_date"] < date].tail(1)
    today = hist[hist["trade_date"] == date].tail(1)
    out: dict[str, Any] = {}
    if not prior.empty:
        p = prior.iloc[0]
        out.update(
            {
                "t1_ret5": p["ret5"],
                "t1_ret20": p["ret20"],
                "t1_ma5_gt_ma20": bool(p["ma5"] > p["ma20"]) if pd.notna(p["ma5"]) and pd.notna(p["ma20"]) else None,
                "t1_vs20h": p["vs20h"],
                "t1_rise_ratio": p["rise_ratio"],
            }
        )
    if not today.empty:
        t = today.iloc[0]
        out.update(
            {
                "eod_avg_change": t["avg_change_pct"],
                "eod_rise_ratio": t["rise_ratio"],
                "eod_strong_ratio": t["strong_ratio"],
                "eod_limit_up_count": t["limit_up_count"],
                "member_bars": t["member_bars"],
            }
        )
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tickets = pd.read_csv(MANUAL / "2026ytd_review_tickets.csv")
    rows = []
    members = _members()
    for _, ticket in tickets.iterrows():
        sectors = members[members["stock_code"] == str(ticket["code"])].sort_values("level")
        for _, sec in sectors.iterrows():
            stats = _sector_daily(str(sec["sector_code"]), str(ticket["date"]))
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
    path = OUT / "2026ytd_review_ticket_sector_levels.csv"
    out.to_csv(path, index=False, encoding="utf-8-sig")
    print(json.dumps({"path": str(path), "rows": len(out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
