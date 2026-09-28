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

from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table  # noqa: E402
from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE = report_path("score120_mom60_gate_revalidation_v1", "score120_diff65_m30_ma20_source_signals.csv")
OUT_DIR = report_path("score120_exit_payoff_contracts_v1")
COST_BPS = 30.0


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
        return value if math.isfinite(value) else None
    if pd.isna(value):
        return None
    return str(value)


def _load_signals(start: str, end: str) -> pd.DataFrame:
    if not SOURCE.exists():
        raise FileNotFoundError(f"missing source: {SOURCE}")
    d = pd.read_csv(SOURCE, encoding="utf-8-sig")
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    for col in [
        "entry_price",
        "exit_price",
        "net_ret",
        "rank_key",
        "amount_rank",
        "sector_diffusion_score",
        "index_mom60",
        "wave_style_score",
    ]:
        d[col] = pd.to_numeric(d.get(col), errors="coerce")
    d["code_raw"] = d.get("code_raw", d.get("code")).astype(str)
    d = d[(d["entry_date"] >= pd.Timestamp(start)) & (d["entry_date"] <= pd.Timestamp(end))].copy()
    d = d[d["index_mom60"] <= 0.05].copy()
    d = d.dropna(subset=["code_raw", "entry_date", "policy_exit_date", "entry_price", "exit_price", "net_ret"])
    return d.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False]).reset_index(drop=True)


def _load_daily(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame()
    codes = sorted(signals["code_raw"].astype(str).unique())
    start = signals["entry_date"].min().strftime("%Y-%m-%d")
    end = signals["policy_exit_date"].max().strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for ci in range(0, len(codes), 250):
        quoted = ",".join(_sql_literal(code) for code in codes[ci : ci + 250])
        sql = f"""
        SELECT code, trade_date, open, high, low, close
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
    for col in ["open", "high", "low", "close"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    return daily.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "trade_date"])


def _contracts() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for hard_stop in [0.12, 0.10, 0.08]:
        for take_profit in [0.10, 0.12, 0.15]:
            specs.append(
                {
                    "contract": f"hs{int(hard_stop*100)}_tp{int(take_profit*100)}_half_hold_policy",
                    "hard_stop": hard_stop,
                    "take_profit": take_profit,
                    "sell_ratio": 0.50,
                    "after_tp_stop": None,
                }
            )
            specs.append(
                {
                    "contract": f"hs{int(hard_stop*100)}_tp{int(take_profit*100)}_half_be",
                    "hard_stop": hard_stop,
                    "take_profit": take_profit,
                    "sell_ratio": 0.50,
                    "after_tp_stop": 0.00,
                }
            )
            specs.append(
                {
                    "contract": f"hs{int(hard_stop*100)}_tp{int(take_profit*100)}_half_lock4",
                    "hard_stop": hard_stop,
                    "take_profit": take_profit,
                    "sell_ratio": 0.50,
                    "after_tp_stop": 0.04,
                }
            )
            specs.append(
                {
                    "contract": f"hs{int(hard_stop*100)}_tp{int(take_profit*100)}_two_thirds_be",
                    "hard_stop": hard_stop,
                    "take_profit": take_profit,
                    "sell_ratio": 2.0 / 3.0,
                    "after_tp_stop": 0.00,
                }
            )
    return specs


def _net(ret: float) -> float:
    return ret - COST_BPS / 10000.0


def _simulate_trade(row: pd.Series, bars: pd.DataFrame, spec: dict[str, Any]) -> dict[str, Any]:
    entry = float(row["entry_price"])
    old_ret = float(row["net_ret"])
    hard_price = entry * (1.0 - float(spec["hard_stop"]))
    tp_price = entry * (1.0 + float(spec["take_profit"]))
    after_tp_stop = spec.get("after_tp_stop")
    protect_price = entry * (1.0 + float(after_tp_stop)) if after_tp_stop is not None else None
    sell_ratio = float(spec["sell_ratio"])
    remaining_ratio = 1.0 - sell_ratio
    touched_tp = False
    first_exit_date = pd.NaT
    final_exit_date = pd.Timestamp(row["policy_exit_date"])
    final_ret = old_ret
    reason = "policy_exit"

    if bars.empty:
        return {
            **row.to_dict(),
            **spec,
            "contract_net_ret": old_ret,
            "exit_reason": "missing_bars_policy_exit",
            "touched_take_profit": False,
            "first_exit_date": pd.NaT,
            "final_exit_date": final_exit_date,
        }

    for bar in bars.itertuples(index=False):
        day = pd.Timestamp(bar.trade_date)
        low = float(bar.low)
        high = float(bar.high)
        close = float(bar.close)
        if not touched_tp:
            if low <= hard_price:
                final_ret = _net(hard_price / entry - 1.0)
                final_exit_date = day
                reason = "hard_stop"
                break
            if high >= tp_price:
                touched_tp = True
                first_exit_date = day
                reason = "take_profit_then_policy_exit" if protect_price is None else "take_profit_then_protect"
                if protect_price is not None and low <= protect_price:
                    final_ret = sell_ratio * _net(tp_price / entry - 1.0) + remaining_ratio * _net(protect_price / entry - 1.0)
                    final_exit_date = day
                    reason = "take_profit_same_day_protect"
                    break
                continue
        else:
            if protect_price is not None and low <= protect_price:
                final_ret = sell_ratio * _net(tp_price / entry - 1.0) + remaining_ratio * _net(protect_price / entry - 1.0)
                final_exit_date = day
                reason = "take_profit_protect_stop"
                break

        if day >= pd.Timestamp(row["policy_exit_date"]):
            if touched_tp:
                remain_ret = close / entry - 1.0
                final_ret = sell_ratio * _net(tp_price / entry - 1.0) + remaining_ratio * _net(remain_ret)
            else:
                final_ret = old_ret
            final_exit_date = day
            break

    return {
        **row.to_dict(),
        **spec,
        "contract_net_ret": final_ret,
        "exit_reason": reason,
        "touched_take_profit": bool(touched_tp),
        "first_exit_date": first_exit_date,
        "final_exit_date": final_exit_date,
    }


def _simulate_contracts(signals: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    by_code = {code: g.copy() for code, g in daily.groupby("code")}
    rows: list[dict[str, Any]] = []
    for _, row in signals.iterrows():
        bars = by_code.get(str(row["code_raw"]), pd.DataFrame())
        if not bars.empty:
            bars = bars[
                (bars["trade_date"] >= pd.Timestamp(row["entry_date"]))
                & (bars["trade_date"] <= pd.Timestamp(row["policy_exit_date"]))
            ].copy()
        for spec in _contracts():
            rows.append(_simulate_trade(row, bars, spec))
    return pd.DataFrame(rows)


def _summarize(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for name, g in trades.groupby("contract"):
        ret = pd.to_numeric(g["contract_net_ret"], errors="coerce")
        old = pd.to_numeric(g["net_ret"], errors="coerce")
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
                "hard_stop_rate": float(g["exit_reason"].astype(str).eq("hard_stop").mean()) if len(g) else 0.0,
                "mean_improvement": float((ret - old).mean()) if len(ret) else 0.0,
            }
        )
    out = pd.DataFrame(rows)
    return out.sort_values(["mean_ret", "worst_ret", "payoff_ratio"], ascending=[False, False, False])


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(args.start_date, args.end_date)
    daily = _load_daily(signals)
    trades = _simulate_contracts(signals, daily)
    summary = _summarize(trades)
    signals.to_csv(OUT_DIR / "source_signals.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "contract_trade_results.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "contract_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "win_rate",
        "mean_ret",
        "median_ret",
        "worst_ret",
        "avg_win",
        "avg_loss",
        "payoff_ratio",
        "breakeven_win_rate",
        "tp_touch_rate",
        "hard_stop_rate",
        "mean_improvement",
    }
    current = summary[summary["contract"].eq("hs12_tp12_half_hold_policy")]
    candidates = summary[
        summary["contract"].isin(
            [
                "hs10_tp12_half_be",
                "hs10_tp12_half_lock4",
                "hs8_tp10_half_be",
                "hs8_tp10_half_lock4",
                "hs8_tp12_half_be",
                "hs8_tp12_two_thirds_be",
                "hs10_tp10_two_thirds_be",
            ]
        )
    ].sort_values(["worst_ret", "mean_ret"], ascending=[False, False])
    top = summary.head(15)
    lines = [
        "# Score120 退出盈亏结构审计 v1",
        "",
        "## 口径",
        "",
        "- 入场信号不变：`score>=120 + sector_diffusion>=65 + 30m close>=MA20 + index_mom60<=5%`。",
        "- 使用日线 high/low 代理止损止盈触发，执行顺序为先止损、再止盈；这是保守口径。",
        "- 成本按 30bps 从每段收益中扣除。",
        "- `half_hold_policy` 表示当前近似合同：+目标价卖一半，剩余仓走原策略退出；`half_be/half_lock4` 表示止盈后剩余仓保护抬到成本线或 +4%。",
        "",
        f"- 样本数：{len(signals)}",
        "",
        "## 当前合同近似",
        "",
        _md_table(current, pct_cols=pct_cols),
        "",
        "## 重点候选合同",
        "",
        _md_table(candidates, pct_cols=pct_cols),
        "",
        "## 全部合同 Top 15",
        "",
        _md_table(top, pct_cols=pct_cols),
        "",
        "## 初步结论",
        "",
        "1. 单笔风险应从 -12% 收到 -8%~-10%，否则半仓止盈无法自然覆盖一次满仓止损。",
        "2. +12% 半仓止盈后必须抬保护，至少抬到成本线；若波动过大，可用 +4% 浮盈保护替代机械前低。",
        "3. 若目标是降低胜率要求，优先测试 `-8%硬止损 +10%先卖2/3 + 剩余成本保护`，它让落袋收益更接近硬止损幅度。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "signals": int(len(signals)),
        "contracts": int(summary["contract"].nunique()) if not summary.empty else 0,
        "best_contract": summary.iloc[0].to_dict() if not summary.empty else {},
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Score120 exit payoff contracts.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
