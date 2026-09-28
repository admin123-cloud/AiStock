from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_score120_exit_payoff_contracts_v1 import _load_signals, _net  # noqa: E402
from scripts.audit_score120_mainwave_risk_contract_variants_v1 import _metrics, _simulate  # noqa: E402
from scripts.audit_score120_minute_exit_contracts_v1 import (  # noqa: E402
    _adjust_minute_to_daily_close,
    _load_daily_closes,
)
from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table  # noqa: E402
from scripts.backtest_wave_style_template_strategy_v1 import _load_index, _trade_calendar  # noqa: E402
from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("score120_hybrid_exit_contracts_v1")


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
        return value if math.isfinite(value) else None
    if pd.isna(value):
        return None
    return str(value)


def _load_minute_bars(signals: pd.DataFrame, period: int) -> pd.DataFrame:
    table = f"kline_minute_{period}"
    if signals.empty:
        return pd.DataFrame()
    codes = sorted(signals["code_raw"].astype(str).unique())
    start = (signals["entry_date"].min() - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
    end = signals["policy_exit_date"].max().strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for ci in range(0, len(codes), 250):
        quoted = ",".join(_sql_literal(code) for code in codes[ci : ci + 250])
        sql = f"""
        SELECT code, datetime, open, high, low, close
        FROM {table}
        WHERE code IN ({quoted})
          AND toDate(datetime) >= toDate({_sql_literal(start)})
          AND toDate(datetime) <= toDate({_sql_literal(end)})
        ORDER BY code, datetime
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trade_date"] = bars["datetime"].dt.normalize()
    for col in ["open", "high", "low", "close"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["code", "datetime", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "datetime"])


def _with_ma20(bars30: pd.DataFrame) -> pd.DataFrame:
    if bars30.empty:
        return bars30
    out = bars30.sort_values(["code", "datetime"]).copy()
    out["ma20"] = out.groupby("code")["close"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    return out


def _contract_specs() -> list[dict[str, Any]]:
    return [
        {
            "contract": "hybrid_hard12_soft10_tp12_half_ma20",
            "hard_stop": 0.12,
            "soft_stop": 0.10,
            "take_profit": 0.12,
            "sell_ratio": 0.50,
            "after_tp": "ma20",
        },
        {
            "contract": "hybrid_hard12_soft8_tp12_half_ma20",
            "hard_stop": 0.12,
            "soft_stop": 0.08,
            "take_profit": 0.12,
            "sell_ratio": 0.50,
            "after_tp": "ma20",
        },
        {
            "contract": "hybrid_hard10_soft8_tp12_half_ma20",
            "hard_stop": 0.10,
            "soft_stop": 0.08,
            "take_profit": 0.12,
            "sell_ratio": 0.50,
            "after_tp": "ma20",
        },
        {
            "contract": "hybrid_hard12_soft10_tp10_half_ma20",
            "hard_stop": 0.12,
            "soft_stop": 0.10,
            "take_profit": 0.10,
            "sell_ratio": 0.50,
            "after_tp": "ma20",
        },
        {
            "contract": "hybrid_hard12_soft10_tp12_half_be_or_ma20",
            "hard_stop": 0.12,
            "soft_stop": 0.10,
            "take_profit": 0.12,
            "sell_ratio": 0.50,
            "after_tp": "be_or_ma20",
        },
        {
            "contract": "hybrid_hard12_soft10_tp12_third2_ma20",
            "hard_stop": 0.12,
            "soft_stop": 0.10,
            "take_profit": 0.12,
            "sell_ratio": 2.0 / 3.0,
            "after_tp": "ma20",
        },
    ]


def _simulate_trade(row: pd.Series, bars5: pd.DataFrame, bars30: pd.DataFrame, spec: dict[str, Any]) -> dict[str, Any]:
    entry = float(row["entry_price"])
    entry_date = pd.Timestamp(row["entry_date"]).normalize()
    policy_exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
    hard_price = entry * (1.0 - float(spec["hard_stop"]))
    soft_price = entry * (1.0 - float(spec["soft_stop"]))
    tp_price = entry * (1.0 + float(spec["take_profit"]))
    sell_ratio = float(spec["sell_ratio"])
    remaining_ratio = 1.0 - sell_ratio
    after_tp = str(spec["after_tp"])

    events: list[dict[str, Any]] = []
    if not bars5.empty:
        g5 = bars5[(bars5["trade_date"] >= entry_date) & (bars5["trade_date"] <= policy_exit_date)].copy()
        for bar in g5.itertuples(index=False):
            events.append(
                {
                    "datetime": pd.Timestamp(bar.datetime),
                    "kind": "hard5",
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "ma20": math.nan,
                }
            )
    if not bars30.empty:
        g30 = bars30[(bars30["trade_date"] >= entry_date) & (bars30["trade_date"] <= policy_exit_date)].copy()
        for bar in g30.itertuples(index=False):
            events.append(
                {
                    "datetime": pd.Timestamp(bar.datetime),
                    "kind": "signal30",
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "ma20": float(bar.ma20) if pd.notna(bar.ma20) else math.nan,
                }
            )
    if not events:
        return {
            **row.to_dict(),
            **spec,
            "contract_net_ret": float(row["net_ret"]),
            "exit_reason": "missing_minute_policy_exit",
            "touched_take_profit": False,
            "final_exit_datetime": policy_exit_date,
        }

    touched_tp = False
    first_exit_dt = pd.NaT
    final_ret = float(row["net_ret"])
    final_exit_dt = policy_exit_date
    reason = "policy_exit"
    last_signal_close = None

    for ev in sorted(events, key=lambda item: (item["datetime"], 0 if item["kind"] == "hard5" else 1)):
        dt = ev["datetime"]
        close = float(ev["close"])
        if ev["kind"] == "hard5" and float(ev["low"]) <= hard_price:
            if not touched_tp:
                final_ret = _net(hard_price / entry - 1.0)
            else:
                final_ret = sell_ratio * _net(tp_price / entry - 1.0) + remaining_ratio * _net(hard_price / entry - 1.0)
            final_exit_dt = dt
            reason = "hard_5m_intrabar_stop"
            break
        if ev["kind"] != "signal30":
            continue
        last_signal_close = close
        if not touched_tp:
            if close <= soft_price:
                final_ret = _net(close / entry - 1.0)
                final_exit_dt = dt
                reason = "soft_30m_close_stop"
                break
            if close >= tp_price:
                touched_tp = True
                first_exit_dt = dt
                reason = "take_profit_then_policy"
                continue
        else:
            ma20 = float(ev["ma20"]) if pd.notna(ev["ma20"]) else math.nan
            ma20_break = math.isfinite(ma20) and close < ma20
            be_break = close <= entry
            if ma20_break or (after_tp == "be_or_ma20" and be_break):
                final_ret = sell_ratio * _net(tp_price / entry - 1.0) + remaining_ratio * _net(close / entry - 1.0)
                final_exit_dt = dt
                reason = "after_tp_30m_ma20_or_be_break" if after_tp == "be_or_ma20" else "after_tp_30m_ma20_break"
                break

    if final_exit_dt == policy_exit_date and reason in {"policy_exit", "take_profit_then_policy"}:
        if touched_tp and last_signal_close is not None:
            final_ret = sell_ratio * _net(tp_price / entry - 1.0) + remaining_ratio * _net(float(last_signal_close) / entry - 1.0)
        elif last_signal_close is not None:
            final_ret = _net(float(last_signal_close) / entry - 1.0)

    return {
        **row.to_dict(),
        **spec,
        "contract_net_ret": final_ret,
        "exit_reason": reason,
        "touched_take_profit": bool(touched_tp),
        "first_exit_datetime": first_exit_dt,
        "final_exit_datetime": final_exit_dt,
    }


def _trade_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for name, g in trades.groupby("contract"):
        ret = pd.to_numeric(g["contract_net_ret"], errors="coerce")
        wins = ret[ret > 0]
        losses = ret[ret <= 0]
        rows.append(
            {
                "contract": name,
                "trades": int(len(g)),
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
                "mean_ret": float(ret.mean()) if len(ret) else 0.0,
                "median_ret": float(ret.median()) if len(ret) else 0.0,
                "worst_ret": float(ret.min()) if len(ret) else 0.0,
                "avg_win": float(wins.mean()) if len(wins) else 0.0,
                "avg_loss": float(losses.mean()) if len(losses) else 0.0,
                "payoff_ratio": float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) and losses.mean() != 0 else math.nan,
                "breakeven_win_rate": float(abs(losses.mean()) / (wins.mean() + abs(losses.mean())))
                if len(wins) and len(losses) and wins.mean() > 0
                else math.nan,
                "tp_touch_rate": float(g["touched_take_profit"].mean()) if len(g) else 0.0,
                "hard_stop_rate": float(g["exit_reason"].astype(str).eq("hard_5m_intrabar_stop").mean()) if len(g) else 0.0,
                "soft_stop_rate": float(g["exit_reason"].astype(str).eq("soft_30m_close_stop").mean()) if len(g) else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(["mean_ret", "worst_ret"], ascending=[False, False])


def _portfolio_backtest(trades: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    calendar = _trade_calendar(pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=60))
    index_df = _load_index(start, end)
    windows = {
        "full": (start, end),
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "post_2024_09": ("2024-09-24", end),
        "blind_2026ytd": ("2026-01-01", end),
    }
    rows: list[dict[str, Any]] = []
    for contract in sorted(trades["contract"].dropna().unique()):
        sig = trades[trades["contract"].eq(contract)].copy()
        sig["final_exit_datetime"] = pd.to_datetime(sig["final_exit_datetime"], errors="coerce")
        sig["policy_exit_date"] = sig["final_exit_datetime"].dt.normalize()
        sig["net_ret"] = pd.to_numeric(sig["contract_net_ret"], errors="coerce")
        sig["original_net_ret"] = pd.to_numeric(sig.get("net_ret"), errors="coerce")
        sig["risk_exit_note"] = sig["exit_reason"]
        sig = sig.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "net_ret"])
        variant = {
            "variant": contract,
            "slot_pct": 0.50,
            "cooldown_after_losses": 2,
            "cooldown_mode": "dynamic_recovery",
            "min_cooldown_days": 3,
            "max_cooldown_days": 15,
        }
        curve, closed = _simulate(sig, calendar, variant, slots=2, daily_open_limit=2)
        run_dir = OUT_DIR / contract
        run_dir.mkdir(parents=True, exist_ok=True)
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        for window, (ws, we) in windows.items():
            rows.append(_metrics(curve, closed, index_df, contract, window, ws, we))
    return pd.DataFrame(rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(args.start_date, args.end_date)
    daily_closes = _load_daily_closes(signals)
    bars5 = _adjust_minute_to_daily_close(_load_minute_bars(signals, 5), daily_closes)
    bars30 = _with_ma20(_adjust_minute_to_daily_close(_load_minute_bars(signals, 30), daily_closes))
    by5 = {code: g.copy() for code, g in bars5.groupby("code")} if not bars5.empty else {}
    by30 = {code: g.copy() for code, g in bars30.groupby("code")} if not bars30.empty else {}
    rows: list[dict[str, Any]] = []
    for spec in _contract_specs():
        for _, row in signals.iterrows():
            code = str(row["code_raw"])
            rows.append(_simulate_trade(row, by5.get(code, pd.DataFrame()), by30.get(code, pd.DataFrame()), spec))
    trades = pd.DataFrame(rows)
    trade_summary = _trade_summary(trades)
    portfolio = _portfolio_backtest(trades, args.start_date, args.end_date)

    signals.to_csv(OUT_DIR / "source_signals.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "hybrid_contract_trade_results.csv", index=False, encoding="utf-8-sig")
    trade_summary.to_csv(OUT_DIR / "hybrid_contract_trade_summary.csv", index=False, encoding="utf-8-sig")
    portfolio.to_csv(OUT_DIR / "hybrid_contract_portfolio_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "strategy_ret",
        "index_ret",
        "excess_ret",
        "max_drawdown",
        "win_rate",
        "mean_trade_ret",
        "worst_trade",
        "mean_ret",
        "median_ret",
        "avg_win",
        "avg_loss",
        "payoff_ratio",
        "breakeven_win_rate",
        "tp_touch_rate",
        "hard_stop_rate",
        "soft_stop_rate",
    }
    full = portfolio[portfolio["window"].eq("full")].sort_values(["strategy_ret", "max_drawdown"], ascending=[False, False])
    lines = [
        "# Score120 双层分钟退出合同审计 v1",
        "",
        "## 口径",
        "",
        "- 入场信号不变：score>=120 + sector_diffusion>=65 + 30m close>=MA20 + index_mom60<=5%。",
        "- 5m intrabar 只做硬止损兜底；30m close 做软止损、止盈确认和止盈后 MA20 趋势保护。",
        "- 分钟线先按当日日线 close 做复权校准，避免分钟/日线价格口径错配。",
        "- 组合规则：2槽、单槽50%、每日最多2票；连续2笔已平仓亏损后动态冷却。",
        f"- 样本数：{len(signals)}。",
        "",
        "## 单笔表现",
        "",
        _md_table(trade_summary, pct_cols=pct_cols),
        "",
        "## 组合表现（全周期排序）",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "signals": int(len(signals)),
        "contracts": int(trade_summary["contract"].nunique()) if not trade_summary.empty else 0,
        "best_portfolio_full": full.iloc[0].to_dict() if not full.empty else {},
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Score120 hybrid minute exit contracts.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
