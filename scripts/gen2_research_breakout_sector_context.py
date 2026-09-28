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


BASE = ROOT / "reports" / "gen2_breakout_buy_point_research"
PROBE = BASE / "breakout_family_intraday_strength_probe"
COMBO = PROBE / "combo_policy_probe"
MANUAL = COMBO / "manual_review_2026ytd"
OUT = COMBO / "sector_context_probe"


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if pd.isna(value):
        return None
    return str(value)


@lru_cache(maxsize=1)
def _ch():
    return clickhouse_client()


@lru_cache(maxsize=1)
def _sector_membership() -> pd.DataFrame:
    q = """
    SELECT ss.stock_code, s.code AS sector_code, s.name AS sector_name, s.level, s.stock_count
    FROM sector_stocks ss
    JOIN sectors s ON ss.sector_code = s.code
    WHERE s.type = 'industry'
    """
    return _ch().query_df(q)


@lru_cache(maxsize=2048)
def _sector_daily(sector_code: str, end_date: str, lookback_days: int = 90) -> pd.DataFrame:
    members = _sector_membership()
    codes = members.loc[members["sector_code"] == sector_code, "stock_code"].dropna().astype(str).unique().tolist()
    if not codes:
        return pd.DataFrame()
    quoted = ", ".join(f"'{code}'" for code in codes)
    start = (pd.Timestamp(end_date) - pd.Timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
    q = f"""
    SELECT trade_date, code, change_pct, amount
    FROM kline_daily
    WHERE code IN ({quoted})
      AND trade_date BETWEEN '{start}' AND '{end_date}'
    ORDER BY trade_date, code
    """
    df = _ch().query_df(q)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    rows = []
    for date, g in df.groupby("trade_date"):
        valid = g.dropna(subset=["change_pct"])
        if valid.empty:
            continue
        rows.append(
            {
                "trade_date": date,
                "avg_change_pct": float(valid["change_pct"].mean()),
                "rise_ratio": float((valid["change_pct"] > 0).mean()),
                "strong_ratio": float((valid["change_pct"] >= 3.0).mean()),
                "limit_up_count": int((valid["change_pct"] >= 9.9).sum()),
                "member_bars": int(len(valid)),
                "amount_sum": float(valid["amount"].sum()),
            }
        )
    out = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    if out.empty:
        return out
    out["sector_index"] = (1.0 + out["avg_change_pct"].fillna(0.0) / 100.0).cumprod()
    out["ma5"] = out["sector_index"].rolling(5, min_periods=3).mean()
    out["ma20"] = out["sector_index"].rolling(20, min_periods=10).mean()
    out["ret5"] = out["sector_index"].pct_change(5)
    out["ret20"] = out["sector_index"].pct_change(20)
    out["prior20_high"] = out["sector_index"].rolling(20, min_periods=10).max().shift(1)
    out["close_vs_prior20_high"] = out["sector_index"] / out["prior20_high"] - 1.0
    return out


def _pick_sectors(code: str) -> pd.DataFrame:
    m = _sector_membership()
    s = m[m["stock_code"] == code].copy()
    if s.empty:
        return s
    s = s.sort_values(["level", "stock_count"], ascending=[False, True])
    return s


def _sector_stats_for_row(code: str, date: str) -> dict[str, Any]:
    sectors = _pick_sectors(code)
    result: dict[str, Any] = {}
    if sectors.empty:
        return result
    chosen = None
    for _, row in sectors.iterrows():
        if int(row["level"]) == 3:
            chosen = row
            break
    if chosen is None:
        chosen = sectors.iloc[0]
    sector_code = str(chosen["sector_code"])
    sd = _sector_daily(sector_code, date)
    if sd.empty:
        return result
    prior = sd[sd["trade_date"] < date].tail(1)
    today = sd[sd["trade_date"] == date].tail(1)
    result.update(
        {
            "sector_code": sector_code,
            "sector_name": str(chosen["sector_name"]),
            "sector_level": int(chosen["level"]),
            "sector_stock_count": int(chosen["stock_count"]),
        }
    )
    if not prior.empty:
        p = prior.iloc[0]
        result.update(
            {
                "sector_t1_avg_change": float(p["avg_change_pct"]),
                "sector_t1_rise_ratio": float(p["rise_ratio"]),
                "sector_t1_strong_ratio": float(p["strong_ratio"]),
                "sector_t1_limit_up_count": int(p["limit_up_count"]),
                "sector_t1_ret5": float(p["ret5"]) if pd.notna(p["ret5"]) else None,
                "sector_t1_ret20": float(p["ret20"]) if pd.notna(p["ret20"]) else None,
                "sector_t1_ma5_gt_ma20": bool(p["ma5"] > p["ma20"]) if pd.notna(p["ma5"]) and pd.notna(p["ma20"]) else None,
                "sector_t1_close_vs_20d_high": float(p["close_vs_prior20_high"])
                if pd.notna(p["close_vs_prior20_high"])
                else None,
            }
        )
    if not today.empty:
        t = today.iloc[0]
        result.update(
            {
                "sector_eod_avg_change": float(t["avg_change_pct"]),
                "sector_eod_rise_ratio": float(t["rise_ratio"]),
                "sector_eod_strong_ratio": float(t["strong_ratio"]),
                "sector_eod_limit_up_count": int(t["limit_up_count"]),
            }
        )
    return result


def _load_signals() -> pd.DataFrame:
    sig = pd.read_parquet(COMBO / "sources" / "rtret60_or_breakbox25.parquet")
    sig["entry_date"] = pd.to_datetime(sig["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    sig["confirm_datetime"] = pd.to_datetime(sig["confirm_datetime"], errors="coerce")
    sig["confirm_time"] = sig["confirm_datetime"].dt.strftime("%H:%M")
    sig["source_family"] = sig.get("source_family", "").fillna("")
    return sig


def _visible_cohort_stats(signals: pd.DataFrame, row: pd.Series, sector_code: str) -> dict[str, Any]:
    if not sector_code:
        return {}
    members = set(
        _sector_membership().loc[_sector_membership()["sector_code"] == sector_code, "stock_code"].dropna().astype(str).tolist()
    )
    same_day = signals[signals["entry_date"] == row["date"]].copy()
    same_sector = same_day[same_day["code"].astype(str).isin(members)].copy()
    visible = same_sector[same_sector["confirm_datetime"] <= pd.Timestamp(f"{row['date']} {row['confirm_time']}:00")]
    later = same_sector[same_sector["confirm_datetime"] > pd.Timestamp(f"{row['date']} {row['confirm_time']}:00")]
    return {
        "same_sector_signal_count_day": int(len(same_sector)),
        "same_sector_signal_count_visible": int(len(visible)),
        "same_sector_signal_count_later": int(len(later)),
        "same_sector_visible_rank1_count": int((pd.to_numeric(visible["v4_rank"], errors="coerce") == 1).sum())
        if "v4_rank" in visible.columns
        else 0,
        "same_sector_later_rank1_count": int((pd.to_numeric(later["v4_rank"], errors="coerce") == 1).sum())
        if "v4_rank" in later.columns
        else 0,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tickets = pd.read_csv(MANUAL / "2026ytd_review_tickets.csv")
    signals = _load_signals()
    rows = []
    for _, ticket in tickets.iterrows():
        base = ticket.to_dict()
        sector_stats = _sector_stats_for_row(str(ticket["code"]), str(ticket["date"]))
        cohort = _visible_cohort_stats(signals, ticket, str(sector_stats.get("sector_code", "")))
        base.update(sector_stats)
        base.update(cohort)
        rows.append(base)
    out = pd.DataFrame(rows)
    out_path = OUT / "2026ytd_review_ticket_sector_context.csv"
    out.to_csv(out_path, index=False, encoding="utf-8-sig")

    # Also score all 2026YTD combo signals for broader comparison.
    sig2026 = signals[(signals["entry_date"] >= "2026-01-01") & (signals["entry_date"] <= "2026-05-26")].copy()
    sig_rows = []
    for _, s in sig2026.iterrows():
        item = {
            "date": s["entry_date"],
            "code": s["code"],
            "name": s["name"],
            "confirm_time": s["confirm_time"],
            "source_family": s.get("source_family"),
            "v4_rank": s.get("v4_rank"),
            "v4_score": s.get("v4_score"),
        }
        sector_stats = _sector_stats_for_row(str(s["code"]), str(s["entry_date"]))
        row_for_cohort = pd.Series({"date": s["entry_date"], "confirm_time": s["confirm_time"]})
        item.update(sector_stats)
        item.update(_visible_cohort_stats(signals, row_for_cohort, str(sector_stats.get("sector_code", ""))))
        sig_rows.append(item)
    sig_out = pd.DataFrame(sig_rows)
    sig_path = OUT / "combo_2026ytd_signal_sector_context.csv"
    sig_out.to_csv(sig_path, index=False, encoding="utf-8-sig")

    payload = {"ticket_context": str(out_path), "signal_context": str(sig_path), "ticket_rows": len(out), "signal_rows": len(sig_out)}
    (OUT / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
