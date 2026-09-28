"""Build the historical review evidence set for the current two-mode G3 contract."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path


MAINWAVE_SOURCE = report_path("score120_tight_stop_payoff_v1", "contract_trades.csv")
BREAKOUT_SOURCE = report_path("breakout_1030_stop_sensitivity_v1", "closed_trades.csv")
OUT_DIR = report_path("g3_mainwave_breakout_historical_review_v1")


def _read(path):
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _mainwave(df: pd.DataFrame) -> pd.DataFrame:
    data = df[(df["scope"].eq("full")) & (df["contract"].eq("hs10_tp10_half_be"))].copy()
    data["route"] = "institutional_mainwave"
    data["trade_strategy"] = "institutional_score120_mainwave"
    data["trade_strategy_label"] = "机构主升延续"
    data["entry_mode"] = "mainwave_continuation"
    data["net_ret"] = pd.to_numeric(data["contract_net_ret"], errors="coerce")
    data["hard_stop_pct"] = 0.10
    data["take_profit_1_pct"] = 0.10
    data["take_profit_1_sell_ratio"] = 0.50
    data["review_contract"] = "-10%硬止损；+10%卖半；剩余仓结构保护"
    data["evidence_level"] = "native_current_source_replay"
    return data


def _breakout(df: pd.DataFrame) -> pd.DataFrame:
    data = df[pd.to_numeric(df["stop_pct"], errors="coerce").eq(0.04)].copy()
    data["route"] = "institutional_mainwave"
    data["trade_strategy"] = "mainwave_breakout_initiation"
    data["trade_strategy_label"] = "前高突破启动"
    data["entry_mode"] = "breakout_initiation"
    data["hard_stop_pct"] = 0.04
    data["take_profit_1_pct"] = 0.10
    data["take_profit_1_sell_ratio"] = 0.50
    data["review_contract"] = "前高突破后首根合格30m承接；-4%硬止损；+10%卖半；剩余仓结构保护"
    data["evidence_level"] = "30m_execution_proxy_replay"
    return data


def _metrics(frame: pd.DataFrame) -> dict:
    returns = pd.to_numeric(frame.get("net_ret"), errors="coerce").dropna()
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    return {
        "trade_count": int(len(returns)),
        "win_rate": float((returns > 0).mean()) if len(returns) else None,
        "avg_trade_return": float(returns.mean()) if len(returns) else None,
        "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(losses.mean()) if len(losses) else None,
        "payoff_ratio": float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else None,
        "worst_trade": float(returns.min()) if len(returns) else None,
    }


def main() -> int:
    mainwave = _mainwave(_read(MAINWAVE_SOURCE))
    breakout = _breakout(_read(BREAKOUT_SOURCE))
    trades = pd.concat([mainwave, breakout], ignore_index=True, sort=False)
    if trades.empty:
        raise RuntimeError("current two-mode evidence sources are missing or empty")
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce")
    trades["policy_exit_date"] = pd.to_datetime(trades.get("policy_exit_date"), errors="coerce")
    trades["position_pct"] = 0.50
    trades["slot_pct"] = 0.50
    trades["position_slots"] = 2
    # Source files retain the PnL of their earlier exit variants.  The review
    # page must calculate money from this review contract, not reuse it.
    trades["stake"] = 500_000.0
    trades["realized_pnl"] = pd.to_numeric(trades["net_ret"], errors="coerce").fillna(0.0) * trades["stake"]
    trades["exit_value"] = trades["stake"] + trades["realized_pnl"]
    trades["source_type"] = "current_g3_contract_replay"
    trades["historical_status"] = "research_evidence_not_live_track_record"
    trades["trade_key"] = trades.apply(lambda row: f"{row['trade_strategy']}|{row['entry_date']:%Y-%m-%d}|{row['code']}", axis=1)
    trades = trades.sort_values(["entry_date", "trade_strategy", "code"]).reset_index(drop=True)

    # Two-slot realised-return proxy: each entry consumes one 50% slot.  This
    # intentionally is not marked-to-market and never claims intraday fill parity.
    daily = trades.groupby("entry_date", as_index=False).agg(
        slot_count=("code", "count"),
        account_ret_proxy=("net_ret", lambda values: float(pd.to_numeric(values, errors="coerce").fillna(0).head(2).sum() * 0.5)),
    )
    daily["slot_count"] = daily["slot_count"].clip(upper=2)
    daily["equity_proxy"] = (1.0 + daily["account_ret_proxy"]).cumprod()
    daily["drawdown_proxy"] = daily["equity_proxy"] / daily["equity_proxy"].cummax() - 1.0
    curve = daily.rename(columns={"entry_date": "date", "equity_proxy": "equity", "drawdown_proxy": "drawdown"}).copy()
    curve["ret_from_start"] = curve["equity"] - 1.0
    curve["realized_pnl"] = curve["account_ret_proxy"] * 1_000_000
    curve["open_positions"] = 0
    curve["reserved_principal"] = 0.0
    curve["cash"] = curve["equity"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades.to_csv(OUT_DIR / "historical_trades.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(OUT_DIR / "two_slot_realised_proxy_curve.csv", index=False, encoding="utf-8-sig")
    by_strategy = [{"trade_strategy": name, **_metrics(group)} for name, group in trades.groupby("trade_strategy")]
    windows = {"full": trades, "recent_12m": trades[trades["entry_date"] >= (trades["entry_date"].max() - pd.Timedelta(days=365))]}
    window_rows = []
    for name, frame in windows.items():
        metrics = _metrics(frame)
        window_rows.append({"model": "g3_mainwave_continuation_breakout_cash_v1", "window": name, "trades": metrics["trade_count"], "return": metrics["avg_trade_return"], "max_drawdown": float(curve["drawdown"].min()), "win_rate": metrics["win_rate"], "sum_pnl": float(pd.to_numeric(frame["realized_pnl"], errors="coerce").fillna(0).sum())})
    pd.DataFrame(window_rows).to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "strategy_id": "g3_mainwave_continuation_breakout_cash_v1",
        "status": "research_evidence_only",
        "trade_count": int(len(trades)),
        "by_strategy": by_strategy,
        "combined": _metrics(trades),
        "two_slot_realised_proxy": {
            "final_equity": float(daily["equity_proxy"].iloc[-1]),
            "max_drawdown": float(daily["drawdown_proxy"].min()),
            "warning": "按入场日两槽50%已实现收益代理；不等同于逐日盯市、真实成交或两模式并行组合回测。",
        },
        "sources": {"mainwave": str(MAINWAVE_SOURCE), "breakout": str(BREAKOUT_SOURCE)},
        "contract": {"mainwave": "-10% / +10% half", "breakout": "-4% / +10% half"},
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
