"""Current shadow scan: sector breadth plus stock relative strength."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


def run(as_of: str | None = None) -> dict:
    latest = clickhouse_query_df("SELECT max(trade_date) AS d FROM kline_daily").iloc[0]["d"]
    asof = pd.Timestamp(as_of or latest).strftime("%Y-%m-%d")
    members = clickhouse_query_df("SELECT sector_code, stock_code, weight, created_at FROM sector_stocks")
    members["stock_code"] = members["stock_code"].astype(str)
    # Use the most recent warehouse membership version as an explicit snapshot.
    snap_at = pd.to_datetime(members["created_at"], errors="coerce").max()
    members = members[members["created_at"].eq(snap_at)].copy()
    codes = sorted(members.stock_code.unique())
    quoted = ",".join("'" + c.replace("'", "\\'") + "'" for c in codes)
    daily = clickhouse_query_df(f"""SELECT code, trade_date, open, close, amount
        FROM kline_daily WHERE code IN ({quoted})
        AND trade_date BETWEEN subtractDays(toDate('{asof}'), 80) AND toDate('{asof}')""")
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce")
    for c in ["open", "close", "amount"]: daily[c] = pd.to_numeric(daily[c], errors="coerce")
    daily = daily.dropna(subset=["code", "trade_date", "close", "open"])
    daily = daily.sort_values(["code", "trade_date"])
    daily["ret15"] = daily.groupby("code")["close"].transform(lambda s: s / s.shift(15) - 1)
    daily["ma20"] = daily.groupby("code")["close"].transform(lambda s: s.rolling(20, min_periods=15).mean())
    daily["amount20"] = daily.groupby("code")["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    today = daily[daily.trade_date.eq(pd.Timestamp(asof))].copy()
    today = today.merge(members[["sector_code", "stock_code"]], left_on="code", right_on="stock_code", how="inner")
    sector = today.groupby("sector_code").agg(
        member_count=("code", "nunique"),
        above_ma20=("ma20", lambda s: int((today.loc[s.index, "close"] >= s).sum())),
        avg_ret15=("ret15", "mean"),
        total_amount20=("amount20", "sum"),
    ).reset_index()
    sector["breadth"] = sector["above_ma20"] / sector["member_count"].clip(lower=1)
    strong = sector[(sector["member_count"] >= 20) & (sector["breadth"] >= 0.55) & (sector["avg_ret15"] > 0)].sort_values(["breadth", "avg_ret15"], ascending=False).head(20)
    leaders = today[today.sector_code.isin(strong.sector_code)].copy()
    leaders = leaders[leaders.ret15.notna() & leaders.ma20.notna() & (leaders.close >= leaders.ma20) & (leaders.amount20 >= 100_000_000)]
    leaders = leaders.sort_values(["sector_code", "ret15", "amount20"], ascending=[True, False, False]).groupby("sector_code").head(3)
    out = {"research_only": True, "as_of": asof, "membership_snapshot_created_at": str(snap_at), "membership_rows": int(len(members)), "sector_candidates": strong.to_dict("records"), "leaders": leaders[["sector_code", "code", "close", "ret15", "amount20"]].to_dict("records")}
    out_dir = report_path("sector_mainwave_leader_rebuild_v2"); out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "current_shadow_scan.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--as-of"); print(json.dumps(run(p.parse_args().as_of), ensure_ascii=False, indent=2, default=str))
