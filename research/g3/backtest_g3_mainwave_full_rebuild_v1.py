"""Raw-data rebuild of the current G3 institutional-mainwave contract."""
from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse, json, sys
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from scripts.backtest_g3_five_strategies_from_scratch_v1 import (  # noqa
    _add_index_features, _add_stock_features, _build_market_context, _build_sector_proxy,
    _load_index_daily, _load_stock_daily, _load_trade_dates, _score_wave_style,
)
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path

OUT = report_path("g3_mainwave_full_rebuild_v1")
CAPITAL, FEE = 1_000_000.0, .003
INDEX_CODE = "000001.SH"  # QMT canonical SSE Composite; never use the TDX alias in strategy logic.

def _q(s: str) -> str: return "'" + str(s).replace("'", "''") + "'"
def _num(x) -> float:
    try: return float(x) if np.isfinite(float(x)) else 0.
    except Exception: return 0.

def _load_trade_dates(start: str, end: str) -> list[str]:
    """Trading calendar comes from the QMT-canonical SSE Composite daily series."""
    d = clickhouse_query_df(
        """
        SELECT trade_date FROM kline_daily
        WHERE code = %(code)s
          AND trade_date BETWEEN subtractDays(toDate(%(start)s), 260) AND toDate(%(end)s)
        ORDER BY trade_date
        """,
        {"code": INDEX_CODE, "start": start, "end": end},
    )
    if d.empty:
        raise RuntimeError(f"no daily trading dates for canonical index {INDEX_CODE}")
    return pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna().tolist()

def _load_index_daily(start: str, end: str) -> pd.DataFrame:
    d = clickhouse_query_df(
        """
        SELECT trade_date, open, high, low, close, volume, amount, turnover_rate
        FROM kline_daily
        WHERE code = %(code)s
          AND trade_date BETWEEN subtractDays(toDate(%(start)s), 260) AND toDate(%(end)s)
        ORDER BY trade_date
        """,
        {"code": INDEX_CODE, "start": start, "end": end},
    )
    if d.empty:
        raise RuntimeError(f"no daily bars for canonical index {INDEX_CODE}")
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["trade_date", "close"]).reset_index(drop=True)

def _daily_candidates(start: str, end: str, amount: float) -> tuple[pd.DataFrame, pd.DataFrame, list[pd.Timestamp]]:
    dates = [pd.Timestamp(x).normalize() for x in _load_trade_dates(start, end)]
    stocks = _add_stock_features(_load_stock_daily(start, end))
    index = _add_index_features(_load_index_daily(start, end))
    ctx = _build_market_context(stocks, index, amount)
    d = _build_sector_proxy(stocks).merge(ctx[["trade_date", "mom60"]].rename(columns={"mom60":"index_mom60"}), on="trade_date", how="left")
    d["score"] = _score_wave_style(d)
    mask = (
        d["amount20"].ge(amount) & d["close"].gt(d["ma20"]) & d["ma20"].gt(d["ma60"])
        & d["mom20"].ge(.08) & d["range_pos60"].ge(.55) & d["amount_ratio20"].between(.9, 3.5)
        & d["score"].ge(120) & d["sector_diffusion_score"].ge(65) & d["index_mom60"].le(.05)
    )
    out = d[mask].copy()
    out["decision_date"] = pd.to_datetime(out["trade_date"]).dt.normalize()
    next_map = {dates[i]: dates[i+1] for i in range(len(dates)-1)}
    out["entry_date"] = out["decision_date"].map(next_map)
    out = out.dropna(subset=["entry_date"]).sort_values(["entry_date","score","amount20"], ascending=[True,False,False])
    out["daily_rank"] = out.groupby("entry_date").cumcount()+1
    return out[out["daily_rank"]<=2].copy(), stocks, dates

def _m30_for(candidates: pd.DataFrame) -> dict[str,pd.DataFrame]:
    # Fetch in bounded code batches.  Per-code HTTP queries turn a full-universe
    # replay into thousands of round trips and can time out before trading logic
    # starts.  The global window remains restricted to actual candidate dates.
    parts=[]
    code_dates = candidates.groupby("code")["entry_date"].agg(["min", "max"])
    codes = code_dates.index.astype(str).tolist()
    for offset in range(0, len(codes), 120):
        batch = codes[offset:offset + 120]
        window = code_dates.loc[batch]
        start=(pd.to_datetime(window["min"]).min()-pd.Timedelta(days=7)).strftime("%Y-%m-%d")
        end=pd.to_datetime(window["max"]).max().strftime("%Y-%m-%d")
        code_sql=",".join(_q(code) for code in batch)
        x=clickhouse_query_df(
            f"SELECT code,datetime,open,high,low,close FROM kline_minute_30 "
            f"WHERE code IN ({code_sql}) AND toDate(datetime) BETWEEN toDate({_q(start)}) AND toDate({_q(end)}) "
            "ORDER BY code, datetime"
        )
        if not x.empty: parts.append(x)
    d=pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()
    if d.empty:return {}
    d["datetime"]=pd.to_datetime(d.datetime); d["date"]=d.datetime.dt.normalize(); d["time"]=d.datetime.dt.strftime("%H:%M:%S")
    for c in ["open","high","low","close"]:d[c]=pd.to_numeric(d[c],errors="coerce")
    return {str(k):v.sort_values("datetime").reset_index(drop=True) for k,v in d.groupby("code")}

def _trade(row: dict, m30: pd.DataFrame, daily: pd.DataFrame) -> dict | None:
    day=pd.Timestamp(row["entry_date"]).normalize(); hist=m30[m30.date.le(day)].copy()
    session=hist[(hist.date.eq(day)) & (hist.time>"09:30:00")]
    entry=None; entry_ix=None
    for ix,b in session.iterrows():
        prior=hist[hist.datetime.le(b.datetime)].tail(20)
        if len(prior)>=20 and _num(b.close)>=_num(prior.close.mean()): entry,entry_ix=_num(b.close),ix; break
    if not entry:return None
    stop,tp=entry*.90,entry*1.10; remaining=1.; realised=0.; took=False; reason="hold20"; exit_day=day
    same=m30[(m30.date.eq(day)) & (m30.datetime>m30.loc[entry_ix,"datetime"])].reset_index(drop=True)
    def apply(low,high,close,date,prev_low=0):
        nonlocal remaining,realised,took,reason,exit_day
        if low<=stop: realised+=remaining*(stop/entry-1); remaining=0; reason="hard_stop"; exit_day=date; return True
        if not took and high>=tp: realised+=.5*(tp/entry-1); remaining=.5; took=True
        if took and prev_low and close<prev_low: realised+=remaining*(close/entry-1); remaining=0; reason="runner_prev_day_low_break"; exit_day=date; return True
        return False
    for i,b in same.iterrows():
        if apply(_num(b.low),_num(b.high),_num(b.close),day,_num(same.iloc[i-1].low) if took and i else 0):break
    future=daily[daily.trade_date_ts.gt(day)].head(20).reset_index(drop=True)
    for i,b in future.iterrows() if remaining else []:
        date=pd.Timestamp(b.trade_date_ts).normalize(); prev=_num(future.iloc[i-1].low) if took and i else 0
        if apply(_num(b.low),_num(b.high),_num(b.close),date,prev):break
        if i==len(future)-1: realised+=remaining*(_num(b.close)/entry-1);remaining=0;exit_day=date
    if remaining: realised+=remaining*0;reason="insufficient_future_bars"
    return {**row,"entry_price":entry,"exit_date":exit_day.strftime("%Y-%m-%d"),"net_ret":realised-FEE,"gross_ret":realised,"exit_reason":reason,"took_profit":took,"hard_stop":stop,"take_profit_1":tp,"entry_mode":"first_completed_30m_ma20_acceptance"}

def _enforce_two_slots(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep only tickets that could actually enter with two 50% slots.

    An exit on the candidate's entry day is not reusable: the replay has no
    intraday ordering guarantee between the runner/stop and a fresh ticket.
    Requiring ``exit_date < entry_date`` is therefore conservative and avoids
    a same-day capital recycling future leak.
    """
    if trades.empty:
        return trades.copy(), trades.copy()
    all_trades = trades.copy()
    all_trades["entry_date_ts"] = pd.to_datetime(all_trades["entry_date"]).dt.normalize()
    all_trades["exit_date_ts"] = pd.to_datetime(all_trades["exit_date"]).dt.normalize()
    all_trades = all_trades.sort_values(["entry_date_ts", "daily_rank", "score", "amount20"], ascending=[True, True, False, False]).reset_index(drop=True)
    selected: list[dict] = []
    skipped: list[dict] = []
    active: list[dict] = []
    for ticket in all_trades.to_dict("records"):
        entry = ticket["entry_date_ts"]
        active = [item for item in active if item["exit_date_ts"] >= entry]
        if len(active) >= 2:
            ticket["slot_decision"] = "skipped_slots_occupied"
            skipped.append(ticket)
            continue
        ticket["slot_decision"] = "entered_slot"
        ticket["slot_id"] = len(active) + 1
        selected.append(ticket)
        active.append(ticket)
    return pd.DataFrame(selected), pd.DataFrame(skipped)

def _realised_equity_stats(trades: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    if trades.empty:
        return pd.DataFrame(), {"realised_return": None, "realised_max_drawdown": None}
    exits = trades.copy()
    exits["exit_date_ts"] = pd.to_datetime(exits["exit_date"]).dt.normalize()
    # Fixed 50% slots: realised-only series, deliberately not presented as
    # intraday MTM drawdown because daily OHLC cannot reproduce it faithfully.
    curve = exits.groupby("exit_date_ts", as_index=False)["net_ret"].sum().sort_values("exit_date_ts")
    curve["daily_portfolio_ret"] = curve["net_ret"] * 0.5
    curve["equity"] = CAPITAL * (1.0 + curve["daily_portfolio_ret"]).cumprod()
    curve["peak"] = curve["equity"].cummax()
    curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
    return curve, {
        "realised_return": float(curve["equity"].iloc[-1] / CAPITAL - 1.0),
        "realised_max_drawdown": float(curve["drawdown"].min()),
    }

def main() -> None:
    p=argparse.ArgumentParser();p.add_argument("--start-date",default="2020-01-01");p.add_argument("--end-date",default="2026-06-30");p.add_argument("--min-amount20",type=float,default=30000);a=p.parse_args()
    OUT.mkdir(parents=True,exist_ok=True); cand,stocks,dates=_daily_candidates(a.start_date,a.end_date,a.min_amount20); m30=_m30_for(cand)
    by={str(k):v.assign(trade_date_ts=pd.to_datetime(v.trade_date).dt.normalize()).sort_values("trade_date") for k,v in stocks.groupby("code")}
    all_confirmed=pd.DataFrame([x for r in cand.to_dict("records") if (x:=_trade(r,m30.get(str(r["code"]),pd.DataFrame()),by.get(str(r["code"]),pd.DataFrame()))) is not None])
    trades, slot_skipped = _enforce_two_slots(all_confirmed)
    curve, equity_stats = _realised_equity_stats(trades)
    for d,name in [(cand,"daily_candidates.csv"),(all_confirmed,"all_confirmed_tickets.csv"),(trades,"closed_trades.csv"),(slot_skipped,"slot_skipped_tickets.csv"),(curve,"two_slot_realised_proxy_curve.csv")]:d.to_csv(OUT/name,index=False,encoding="utf-8-sig")
    r=pd.to_numeric(trades.get("net_ret"),errors="coerce");w=r[r>0];l=r[r<=0]
    summary={"status":"completed","generated_at":datetime.now().isoformat(timespec="seconds"),"index_code":INDEX_CODE,"contract":"score>=120 + diffusion>=65 + index_mom60<=5% + first completed 30m close>=MA20; -10% stop;+10% half; runner previous-low break","daily_candidates":len(cand),"confirmed_tickets_before_slot_cap":len(all_confirmed),"confirmed_trades_two_slots":len(trades),"slot_skipped":len(slot_skipped),"win_rate":float((r>0).mean()) if len(r) else None,"avg_ret":float(r.mean()) if len(r) else None,"avg_win":float(w.mean()) if len(w) else None,"avg_loss":float(l.mean()) if len(l) else None,"worst":float(r.min()) if len(r) else None,"payoff":float(w.mean()/abs(l.mean())) if len(w) and len(l) else None,**equity_stats,"limitations":"raw daily selection; 30m OHLC stop-first proxy; realised curve is fixed-two-slot exit-date proxy, not intraday MTM; no tick slippage/limit-up fill model"}
    (OUT/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8");print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
