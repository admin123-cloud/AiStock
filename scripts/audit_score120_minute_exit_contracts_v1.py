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
from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table  # noqa: E402
from scripts.audit_score120_mainwave_risk_contract_variants_v1 import _metrics, _simulate  # noqa: E402
from scripts.backtest_wave_style_template_strategy_v1 import _load_index, _trade_calendar  # noqa: E402
from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("score120_minute_exit_contracts_v1")


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
    start = signals["entry_date"].min().strftime("%Y-%m-%d")
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


def _load_daily_closes(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame()
    codes = sorted(signals["code_raw"].astype(str).unique())
    start = signals["entry_date"].min().strftime("%Y-%m-%d")
    end = signals["policy_exit_date"].max().strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for ci in range(0, len(codes), 250):
        quoted = ",".join(_sql_literal(code) for code in codes[ci : ci + 250])
        sql = f"""
        SELECT code, trade_date, close AS daily_close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date >= toDate({_sql_literal(start)})
          AND trade_date <= toDate({_sql_literal(end)})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        return daily
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
    daily["daily_close"] = pd.to_numeric(daily["daily_close"], errors="coerce")
    return daily.dropna(subset=["code", "trade_date", "daily_close"])


def _adjust_minute_to_daily_close(bars: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    if bars.empty or daily.empty:
        return bars
    last_minute = (
        bars.sort_values("datetime")
        .groupby(["code", "trade_date"], as_index=False)
        .tail(1)[["code", "trade_date", "close"]]
        .rename(columns={"close": "minute_last_close"})
    )
    factors = last_minute.merge(daily, on=["code", "trade_date"], how="left")
    factors["adj_factor"] = pd.to_numeric(factors["daily_close"], errors="coerce") / pd.to_numeric(
        factors["minute_last_close"], errors="coerce"
    )
    factors = factors.replace([np.inf, -np.inf], np.nan).dropna(subset=["adj_factor"])
    out = bars.merge(factors[["code", "trade_date", "adj_factor"]], on=["code", "trade_date"], how="left")
    out["adj_factor"] = pd.to_numeric(out["adj_factor"], errors="coerce").fillna(1.0)
    for col in ["open", "high", "low", "close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce") * out["adj_factor"]
    return out.drop(columns=["adj_factor"])


def _contract_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for period in [15, 30]:
        for hard_stop in [0.12, 0.10, 0.08]:
            for take_profit in [0.10, 0.12, 0.15]:
                specs.append(
                    {
                        "contract": f"m{period}_close_hs{int(hard_stop*100)}_tp{int(take_profit*100)}_half_hold",
                        "period": period,
                        "hard_stop": hard_stop,
                        "take_profit": take_profit,
                        "sell_ratio": 0.50,
                        "after_tp_stop": None,
                    }
                )
                specs.append(
                    {
                        "contract": f"m{period}_close_hs{int(hard_stop*100)}_tp{int(take_profit*100)}_half_be",
                        "period": period,
                        "hard_stop": hard_stop,
                        "take_profit": take_profit,
                        "sell_ratio": 0.50,
                        "after_tp_stop": 0.00,
                    }
                )
                specs.append(
                    {
                        "contract": f"m{period}_close_hs{int(hard_stop*100)}_tp{int(take_profit*100)}_third2_be",
                        "period": period,
                        "hard_stop": hard_stop,
                        "take_profit": take_profit,
                        "sell_ratio": 2.0 / 3.0,
                        "after_tp_stop": 0.00,
                    }
                )
    return specs


def _simulate_trade(row: pd.Series, bars: pd.DataFrame, spec: dict[str, Any]) -> dict[str, Any]:
    entry = float(row["entry_price"])
    hard_price = entry * (1.0 - float(spec["hard_stop"]))
    tp_price = entry * (1.0 + float(spec["take_profit"]))
    after_tp_stop = spec.get("after_tp_stop")
    protect_price = entry * (1.0 + float(after_tp_stop)) if after_tp_stop is not None else None
    sell_ratio = float(spec["sell_ratio"])
    remaining_ratio = 1.0 - sell_ratio
    policy_exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
    old_ret = float(row["net_ret"])
    touched_tp = False
    first_exit_dt = pd.NaT
    final_exit_dt = policy_exit_date
    final_ret = old_ret
    reason = "policy_exit"

    if bars.empty:
        return {
            **row.to_dict(),
            **spec,
            "contract_net_ret": old_ret,
            "exit_reason": "missing_minute_policy_exit",
            "touched_take_profit": False,
            "first_exit_datetime": pd.NaT,
            "final_exit_datetime": final_exit_dt,
        }

    for bar in bars.itertuples(index=False):
        dt = pd.Timestamp(bar.datetime)
        day = pd.Timestamp(bar.trade_date).normalize()
        close = float(bar.close)
        if day < pd.Timestamp(row["entry_date"]).normalize():
            continue
        if day > policy_exit_date:
            break

        if not touched_tp:
            if close <= hard_price:
                final_ret = _net(close / entry - 1.0)
                final_exit_dt = dt
                reason = "minute_close_hard_stop"
                break
            if close >= tp_price:
                touched_tp = True
                first_exit_dt = dt
                reason = "minute_close_take_profit_then_policy"
                if protect_price is not None and close <= protect_price:
                    final_ret = sell_ratio * _net(close / entry - 1.0) + remaining_ratio * _net(close / entry - 1.0)
                    final_exit_dt = dt
                    reason = "minute_close_take_profit_same_bar_protect"
                    break
                continue
        else:
            if protect_price is not None and close <= protect_price:
                final_ret = sell_ratio * _net(tp_price / entry - 1.0) + remaining_ratio * _net(close / entry - 1.0)
                final_exit_dt = dt
                reason = "minute_close_take_profit_protect"
                break

        if day >= policy_exit_date:
            if touched_tp:
                final_ret = sell_ratio * _net(tp_price / entry - 1.0) + remaining_ratio * _net(close / entry - 1.0)
            else:
                final_ret = _net(close / entry - 1.0)
            final_exit_dt = dt
            break

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
                "hard_stop_rate": float(g["exit_reason"].astype(str).eq("minute_close_hard_stop").mean()) if len(g) else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(["mean_ret", "worst_ret"], ascending=[False, False])


def _portfolio_backtest(trades: pd.DataFrame, contracts: list[str], start: str, end: str) -> pd.DataFrame:
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
    for contract in contracts:
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
    bars_by_period = {
        period: _adjust_minute_to_daily_close(_load_minute_bars(signals, period), daily_closes) for period in [15, 30]
    }
    all_rows: list[dict[str, Any]] = []
    for spec in _contract_specs():
        period = int(spec["period"])
        bars = bars_by_period[period]
        by_code = {code: g.copy() for code, g in bars.groupby("code")} if not bars.empty else {}
        for _, row in signals.iterrows():
            g = by_code.get(str(row["code_raw"]), pd.DataFrame())
            if not g.empty:
                g = g[
                    (g["trade_date"] >= pd.Timestamp(row["entry_date"]))
                    & (g["trade_date"] <= pd.Timestamp(row["policy_exit_date"]))
                ]
            all_rows.append(_simulate_trade(row, g, spec))
    trades = pd.DataFrame(all_rows)
    summary = _trade_summary(trades)
    selected = [
        "m30_close_hs12_tp12_half_hold",
        "m30_close_hs10_tp12_half_hold",
        "m30_close_hs10_tp12_half_be",
        "m15_close_hs10_tp12_half_hold",
        "m15_close_hs10_tp12_half_be",
        "m30_close_hs8_tp12_half_be",
    ]
    portfolio = _portfolio_backtest(trades, selected, args.start_date, args.end_date)

    signals.to_csv(OUT_DIR / "source_signals.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "minute_contract_trade_results.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "minute_contract_trade_summary.csv", index=False, encoding="utf-8-sig")
    portfolio.to_csv(OUT_DIR / "minute_contract_portfolio_summary.csv", index=False, encoding="utf-8-sig")

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
    }
    full = portfolio[portfolio["window"].eq("full")].sort_values(["strategy_ret", "max_drawdown"], ascending=[False, False])
    focus_trade = summary[summary["contract"].isin(selected)].sort_values(["mean_ret", "worst_ret"], ascending=[False, False])
    lines = [
        "# Score120 分钟级退出合同审计 v1",
        "",
        "## 口径",
        "",
        "- 入场信号不变：`score>=120 + sector_diffusion>=65 + 30m close>=MA20 + index_mom60<=5%`。",
        "- 止损/止盈改为分钟K线收盘确认：15m 或 30m close 跌破硬止损才退出，close 突破止盈位才触发减仓。",
        "- 组合规则：2槽、单槽50%、每日最多2票；连续2笔已平仓亏损后动态冷却，至少3个交易日，最多15个交易日。",
        "- 成本：30bps。分钟线覆盖截至 2026-06-18。",
        "",
        f"- 样本数：{len(signals)}",
        "",
        "## 重点合同单笔表现",
        "",
        _md_table(focus_trade, pct_cols=pct_cols),
        "",
        "## 重点合同组合表现（全周期）",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "signals": int(len(signals)),
        "contracts": int(summary["contract"].nunique()) if not summary.empty else 0,
        "best_portfolio_full": full.iloc[0].to_dict() if not full.empty else {},
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Score120 minute-level exit contracts.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
