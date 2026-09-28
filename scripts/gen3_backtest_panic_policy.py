from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df


WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{v * 100:.2f}%"


def _trade_calendar(start: str, end: str) -> list[pd.Timestamp]:
    sql = f"""
    SELECT DISTINCT trade_date
    FROM kline_daily
    WHERE trade_date BETWEEN toDate('{start}') AND toDate('{end}')
    ORDER BY trade_date
    """
    df = clickhouse_query_df(sql)
    return pd.to_datetime(df["trade_date"]).sort_values().drop_duplicates().tolist()


def _exit_date_map(calendar: list[pd.Timestamp], hold_days: int) -> dict[pd.Timestamp, pd.Timestamp]:
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for i, d in enumerate(calendar):
        j = i + hold_days
        if j < len(calendar):
            out[pd.Timestamp(d).normalize()] = pd.Timestamp(calendar[j]).normalize()
    return out


def _policy_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    deep = df["g3_chain"].eq("panic_v2_deep_wash_repair")
    lowpos = df["g3_position_guard"].eq("low_safety_margin")
    avoid_midrisk = ~df["g3_repair_env_label"].eq("mid_panic_failure_risk")
    clearance = df["g3_repair_env_label"].isin(["strong_clearance", "extreme_clearance"])
    return {
        "deep_all": deep,
        "deep_lowpos": deep & lowpos,
        "deep_lowpos_avoid_midrisk": deep & lowpos & avoid_midrisk,
        "deep_lowpos_clearance_only": deep & lowpos & clearance,
    }


def _select_trades(df: pd.DataFrame, policy: str, max_per_day: int | None) -> pd.DataFrame:
    mask = _policy_masks(df)[policy]
    d = df[mask].copy()
    if d.empty:
        return d
    score_cols = [c for c in ["candidate_score", "chain_rank", "amount_ratio3"] if c in d.columns]
    if "candidate_score" in score_cols:
        d["_score_sort"] = pd.to_numeric(d["candidate_score"], errors="coerce").fillna(-1e9)
    else:
        d["_score_sort"] = 0.0
    if "chain_rank" in score_cols:
        d["_rank_sort"] = pd.to_numeric(d["chain_rank"], errors="coerce").fillna(1e9)
    else:
        d["_rank_sort"] = 1e9
    d = d.sort_values(["entry_date", "_score_sort", "_rank_sort"], ascending=[True, False, True])
    if max_per_day is not None:
        d = d.groupby("entry_date", group_keys=False).head(max_per_day)
    return d.drop(columns=[c for c in ["_score_sort", "_rank_sort"] if c in d.columns])


def _basket_curve(trades: pd.DataFrame, hold_days: int, cost_bps: float, exit_map: dict[pd.Timestamp, pd.Timestamp]) -> tuple[pd.DataFrame, pd.DataFrame]:
    ret_col = f"fwd_ret_confirm_to_close_{hold_days}d"
    d = trades.dropna(subset=[ret_col]).copy()
    d["entry_ts"] = pd.to_datetime(d["entry_date"]).dt.normalize()
    d["exit_date"] = d["entry_ts"].map(exit_map)
    d = d.dropna(subset=["exit_date"]).copy()
    d["gross_ret"] = pd.to_numeric(d[ret_col], errors="coerce")
    d["net_ret"] = d["gross_ret"] - cost_bps / 10000.0
    baskets = (
        d.groupby(["entry_ts", "exit_date"], as_index=False)
        .agg(
            signals=("code", "size"),
            unique_codes=("code", "nunique"),
            basket_ret=("net_ret", "mean"),
            gross_basket_ret=("gross_ret", "mean"),
            win_rate=("net_ret", lambda s: float((s > 0).mean())),
        )
        .sort_values("exit_date")
    )
    if baskets.empty:
        baskets["equity"] = []
        baskets["drawdown"] = []
        return d, baskets
    baskets["equity"] = (1.0 + baskets["basket_ret"]).cumprod()
    peak = baskets["equity"].cummax()
    baskets["drawdown"] = baskets["equity"] / peak - 1.0
    return d, baskets


def _metrics(trades: pd.DataFrame, baskets: pd.DataFrame, window_name: str, policy: str, cap_label: str, hold_days: int) -> dict:
    if trades.empty or baskets.empty:
        return {
            "window": window_name,
            "policy": policy,
            "cap": cap_label,
            "hold_days": hold_days,
            "trades": 0,
            "basket_days": 0,
        }
    ret = baskets["basket_ret"].astype(float)
    equity = float(baskets["equity"].iloc[-1])
    return {
        "window": window_name,
        "policy": policy,
        "cap": cap_label,
        "hold_days": hold_days,
        "trades": int(len(trades)),
        "basket_days": int(len(baskets)),
        "unique_codes": int(trades["code"].nunique()),
        "avg_trades_per_basket": float(baskets["signals"].mean()),
        "mean_basket_ret": float(ret.mean()),
        "median_basket_ret": float(ret.median()),
        "basket_win_rate": float((ret > 0).mean()),
        "total_compound_ret": equity - 1.0,
        "max_closed_basket_drawdown": float(baskets["drawdown"].min()),
        "best_basket": float(ret.max()),
        "worst_basket": float(ret.min()),
    }


def _display(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in [
        "mean_basket_ret",
        "median_basket_ret",
        "basket_win_rate",
        "total_compound_ret",
        "max_closed_basket_drawdown",
        "best_basket",
        "worst_basket",
    ]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest G3 panic policy as event baskets.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/failure_env_audit_v1/failure_env_labeled_signals.parquet")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/policy_backtest_v1")
    parser.add_argument("--hold-days", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=30.0, help="Round-trip cost in basis points.")
    parser.add_argument("--max-per-day", type=int, default=5)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(args.input)
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    calendar = _trade_calendar("2020-01-01", "2026-12-31")
    exit_map = _exit_date_map(calendar, args.hold_days)

    all_metrics: list[dict] = []
    policies = ["deep_all", "deep_lowpos", "deep_lowpos_avoid_midrisk", "deep_lowpos_clearance_only"]
    caps = {"uncapped": None, f"top{args.max_per_day}_per_day": args.max_per_day}
    for policy in policies:
        for cap_label, max_per_day in caps.items():
            selected = _select_trades(df, policy, max_per_day)
            trades, baskets = _basket_curve(selected, args.hold_days, args.cost_bps, exit_map)
            trades.to_csv(out_dir / f"{policy}_{cap_label}_trades.csv", index=False, encoding="utf-8-sig")
            baskets.to_csv(out_dir / f"{policy}_{cap_label}_basket_curve.csv", index=False, encoding="utf-8-sig")
            for window_name, (start, end) in WINDOWS.items():
                start_ts = pd.Timestamp(start)
                end_ts = pd.Timestamp(end)
                tw = trades[(pd.to_datetime(trades["entry_date"]) >= start_ts) & (pd.to_datetime(trades["entry_date"]) <= end_ts)].copy()
                bw = baskets[(pd.to_datetime(baskets["entry_ts"]) >= start_ts) & (pd.to_datetime(baskets["entry_ts"]) <= end_ts)].copy()
                if not bw.empty:
                    bw = bw.copy()
                    bw["equity"] = (1.0 + bw["basket_ret"]).cumprod()
                    bw["drawdown"] = bw["equity"] / bw["equity"].cummax() - 1.0
                all_metrics.append(_metrics(tw, bw, window_name, policy, cap_label, args.hold_days))

    raw = pd.DataFrame(all_metrics)
    raw.to_csv(out_dir / "policy_backtest_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "policy_backtest_summary_display.csv", index=False, encoding="utf-8-sig")

    focus = display[(display["policy"].eq("deep_lowpos_avoid_midrisk")) & (display["cap"].eq(f"top{args.max_per_day}_per_day"))]
    full = display[display["window"].eq("full")]
    lines = [
        "# G3 Panic Policy 组合审计 V1",
        "",
        "## 口径",
        "",
        f"- 输入：`{args.input}`",
        f"- 输出目录：`{out_dir}`",
        f"- 固定持有：`{args.hold_days}` 个交易日。",
        f"- 成本：单笔往返 `{args.cost_bps}` bps，直接从信号收益中扣除。",
        f"- 组合方式：按 entry_date 形成事件篮子，同日信号等权，篮子收益按退出日复利。该曲线是研究用事件曲线，不等同于实盘资金曲线。",
        f"- 固定容量口径：每天最多取前 `{args.max_per_day}` 个信号，按 candidate_score 降序、chain_rank 升序选择；同时保留 uncapped 对照。",
        "",
        "## full 对照",
        "",
        full[
            [
                "policy",
                "cap",
                "trades",
                "basket_days",
                "mean_basket_ret",
                "basket_win_rate",
                "total_compound_ret",
                "max_closed_basket_drawdown",
                "worst_basket",
            ]
        ].to_markdown(index=False),
        "",
        "## 主研究口径分段",
        "",
        focus[
            [
                "window",
                "trades",
                "basket_days",
                "mean_basket_ret",
                "basket_win_rate",
                "total_compound_ret",
                "max_closed_basket_drawdown",
                "worst_basket",
            ]
        ].to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. 这一步仍是研究曲线，不是正式资金曲线；它没有模拟持仓重叠、仓位占用、涨跌停成交失败和盘中止损。",
        "2. 如果主研究口径在 train/valid/blind 都保持正收益和可接受回撤，才值得继续进入更真实的交易级回测。",
        "3. 如果收益主要集中在 valid/blind，而 train 很弱，则仍要按过拟合处理，不能正式化。",
    ]
    (out_dir / "policy_backtest_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": args.input,
        "output_dir": str(out_dir),
        "hold_days": args.hold_days,
        "cost_bps": args.cost_bps,
        "max_per_day": args.max_per_day,
        "rows": int(len(df)),
        "summary_rows": int(len(raw)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
