from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table


OUT_DIR = ROOT / "reports" / "gen3_combo_panic_strong_execution_stress_v1"
PANIC_FINAL = ROOT / "reports" / "gen3_panic_v2_research" / "final_candidate_v1"
PANIC_STRESS = ROOT / "reports" / "gen3_panic_v2_research" / "final_candidate_execution_stress_v1"
STRONG_STRESS = ROOT / "reports" / "gen3_strong_volume5_risk_layer_execution_stress_v1"


PROFILES = [
    {
        "profile": "base_30bps",
        "panic_curve": PANIC_FINAL / "m30_close5_full_nextopen_cost30_mtm_equity_curve.csv",
        "panic_closed": PANIC_FINAL / "m30_close5_full_nextopen_cost30_closed_trades.csv",
        "strong_curve": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_30bps_curve.csv",
        "strong_closed": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_30bps_closed_trades.csv",
        "note": "Panic 30bps + Strong next-open second-leg 30bps",
    },
    {
        "profile": "slippage_50bps",
        "panic_curve": PANIC_FINAL / "m30_close5_full_nextopen_cost50_mtm_equity_curve.csv",
        "panic_closed": PANIC_FINAL / "m30_close5_full_nextopen_cost50_closed_trades.csv",
        "strong_curve": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_50bps_curve.csv",
        "strong_closed": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_50bps_closed_trades.csv",
        "note": "50bps cost pressure, no extra panic delay",
    },
    {
        "profile": "slippage_100bps",
        "panic_curve": PANIC_FINAL / "m30_close5_full_nextopen_cost100_mtm_equity_curve.csv",
        "panic_closed": PANIC_FINAL / "m30_close5_full_nextopen_cost100_closed_trades.csv",
        "strong_curve": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_100bps_curve.csv",
        "strong_closed": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_100bps_closed_trades.csv",
        "note": "100bps cost pressure, no extra panic delay",
    },
    {
        "profile": "panic_delay1_strong_next30",
        "panic_curve": PANIC_STRESS / "m30_close5_full_nextopen_delay1d_mtm_equity_curve.csv",
        "panic_closed": PANIC_STRESS / "m30_close5_full_nextopen_delay1d_closed_trades.csv",
        "strong_curve": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_30bps_curve.csv",
        "strong_closed": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_30bps_closed_trades.csv",
        "note": "Panic exit delayed 1 trading day, Strong next-open second-leg",
    },
    {
        "profile": "panic_delay2_strong_next30",
        "panic_curve": PANIC_STRESS / "m30_close5_full_nextopen_delay2d_mtm_equity_curve.csv",
        "panic_closed": PANIC_STRESS / "m30_close5_full_nextopen_delay2d_closed_trades.csv",
        "strong_curve": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_30bps_curve.csv",
        "strong_closed": STRONG_STRESS / "half_d2_le0_then_d3_next_open_second_leg_30bps_closed_trades.csv",
        "note": "Panic exit delayed 2 trading days, Strong next-open second-leg",
    },
    {
        "profile": "strong_limitdown_30bps",
        "panic_curve": PANIC_FINAL / "m30_close5_full_nextopen_cost30_mtm_equity_curve.csv",
        "panic_closed": PANIC_FINAL / "m30_close5_full_nextopen_cost30_closed_trades.csv",
        "strong_curve": STRONG_STRESS / "half_d2_le0_then_d3_limit_down_delay_second_leg_30bps_curve.csv",
        "strong_closed": STRONG_STRESS / "half_d2_le0_then_d3_limit_down_delay_second_leg_30bps_closed_trades.csv",
        "note": "Strong approximate limit-down delayed, Panic base",
    },
    {
        "profile": "panic_delay1_strong_limitdown30",
        "panic_curve": PANIC_STRESS / "m30_close5_full_nextopen_delay1d_mtm_equity_curve.csv",
        "panic_closed": PANIC_STRESS / "m30_close5_full_nextopen_delay1d_closed_trades.csv",
        "strong_curve": STRONG_STRESS / "half_d2_le0_then_d3_limit_down_delay_second_leg_30bps_curve.csv",
        "strong_closed": STRONG_STRESS / "half_d2_le0_then_d3_limit_down_delay_second_leg_30bps_closed_trades.csv",
        "note": "Panic delay1 + Strong limit-down delayed",
    },
    {
        "profile": "panic_delay2_strong_limitdown30",
        "panic_curve": PANIC_STRESS / "m30_close5_full_nextopen_delay2d_mtm_equity_curve.csv",
        "panic_closed": PANIC_STRESS / "m30_close5_full_nextopen_delay2d_closed_trades.csv",
        "strong_curve": STRONG_STRESS / "half_d2_le0_then_d3_limit_down_delay_second_leg_30bps_curve.csv",
        "strong_closed": STRONG_STRESS / "half_d2_le0_then_d3_limit_down_delay_second_leg_30bps_closed_trades.csv",
        "note": "Panic delay2 + Strong limit-down delayed",
    },
]


def _load_curve(path: Path, chain: str) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.normalize()
    if "ret_from_start" in d.columns:
        d["norm_equity"] = 1.0 + pd.to_numeric(d["ret_from_start"], errors="coerce")
    else:
        eq = pd.to_numeric(d["equity"], errors="coerce")
        first = float(eq.dropna().iloc[0])
        d["norm_equity"] = eq / first
    if "worst_open_mtm_ret" not in d.columns:
        d["worst_open_mtm_ret"] = 0.0
    d["worst_open_mtm_ret"] = pd.to_numeric(d["worst_open_mtm_ret"], errors="coerce").fillna(0.0)
    d["open_positions"] = pd.to_numeric(d.get("open_positions", 0), errors="coerce").fillna(0).astype(int)
    d["chain"] = chain
    return d[["date", "norm_equity", "worst_open_mtm_ret", "open_positions", "chain"]].dropna(subset=["date", "norm_equity"])


def _load_closed(path: Path, chain: str) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["chain"] = chain
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d.get("policy_exit_date", d.get("exit_date")), errors="coerce").dt.normalize()
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    return d.dropna(subset=["entry_date", "net_ret"])


def _combine_curve(panic_curve: pd.DataFrame, strong_curve: pd.DataFrame, profile: str) -> pd.DataFrame:
    all_dates = sorted(set(panic_curve["date"]).union(set(strong_curve["date"])))
    out = pd.DataFrame({"date": all_dates})
    parts = []
    for chain, curve in [("panic", panic_curve), ("strong", strong_curve)]:
        c = curve.set_index("date").reindex(all_dates).ffill()
        c["norm_equity"] = c["norm_equity"].fillna(1.0)
        c["worst_open_mtm_ret"] = c["worst_open_mtm_ret"].fillna(0.0)
        c["open_positions"] = c["open_positions"].fillna(0).astype(int)
        parts.append((chain, c))
    out["equity"] = 0.5 * parts[0][1]["norm_equity"].values + 0.5 * parts[1][1]["norm_equity"].values
    out["panic_equity"] = parts[0][1]["norm_equity"].values
    out["strong_equity"] = parts[1][1]["norm_equity"].values
    out["open_positions"] = parts[0][1]["open_positions"].values + parts[1][1]["open_positions"].values
    out["worst_open_mtm_ret"] = pd.concat(
        [parts[0][1]["worst_open_mtm_ret"].reset_index(drop=True), parts[1][1]["worst_open_mtm_ret"].reset_index(drop=True)],
        axis=1,
    ).min(axis=1)
    out["peak"] = out["equity"].cummax()
    out["drawdown"] = out["equity"] / out["peak"] - 1.0
    out["ret_from_start"] = out["equity"] - 1.0
    out["profile"] = profile
    return out


def _metrics(profile: str, note: str, curve: pd.DataFrame, closed: pd.DataFrame) -> dict:
    return {
        "profile": profile,
        "note": note,
        "start": pd.Timestamp(curve["date"].min()).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(curve["date"].max()).strftime("%Y-%m-%d"),
        "total_ret": float(curve["equity"].iloc[-1] - 1.0),
        "max_drawdown": float(curve["drawdown"].min()),
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        "max_open_positions": int(curve["open_positions"].max()),
        "closed": int(len(closed)),
        "win_rate": float((closed["net_ret"] > 0).mean()) if len(closed) else 0.0,
        "mean_trade_ret": float(closed["net_ret"].mean()) if len(closed) else 0.0,
        "worst_trade": float(closed["net_ret"].min()) if len(closed) else 0.0,
        "bad10_rate": float((closed["net_ret"] <= -0.10).mean()) if len(closed) else 0.0,
    }


def _annual(profile: str, curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        trades = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "profile": profile,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": float(part["equity"].div(part["equity"].cummax()).sub(1.0).min()),
                "worst_open_mtm_ret": float(part["worst_open_mtm_ret"].min()),
                "closed": int(len(trades)),
                "win_rate": float((trades["net_ret"] > 0).mean()) if len(trades) else 0.0,
                "mean_trade_ret": float(trades["net_ret"].mean()) if len(trades) else 0.0,
                "worst_trade": float(trades["net_ret"].min()) if len(trades) else 0.0,
                "bad10_rate": float((trades["net_ret"] <= -0.10).mean()) if len(trades) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame) -> None:
    pct_cols = {"total_ret", "max_drawdown", "worst_open_mtm_ret", "win_rate", "mean_trade_ret", "worst_trade", "bad10_rate", "return"}
    focus = annual[annual["profile"].isin(["base_30bps", "slippage_100bps", "panic_delay2_strong_limitdown30"])].sort_values(["profile", "year"])
    lines = [
        "# G3 50/50 组合执行压力 V1",
        "",
        "## 口径",
        "",
        "- 固定组合：Panic 50% + Strong 50%。",
        "- 不做权重搜索，只做压力测试。",
        "- Panic 成本压力使用 final_candidate_v1 的 30/50/100bps；Panic 延迟压力使用 execution_stress_v1 的 delay0/1/2。",
        "- Strong 使用 `half_d2_le0_then_d3` 的 next-open second-leg 与 limit-down delay second-leg。",
        "- 组合曲线按两条袖珍账本净值 50/50 合成。",
        "",
        "## Full 窗口",
        "",
        _md_table(summary.sort_values(["max_drawdown", "total_ret"], ascending=[False, False]), pct_cols=pct_cols),
        "",
        "## 年度重点",
        "",
        _md_table(focus, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 50/50 组合若在 100bps 或双延迟压力下仍能保持 2022-2024 不大幅亏损，才具备 shadow 组合价值。",
        "- 如果压力后收益仍主要来自 2025/2026，说明多环境打法还只是降低回撤，不是完整收益来源多元化。",
        "",
    ]
    (OUT_DIR / "combo_execution_stress_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summaries = []
    annual_parts = []
    for spec in PROFILES:
        profile = spec["profile"]
        panic_curve = _load_curve(spec["panic_curve"], "panic")
        strong_curve = _load_curve(spec["strong_curve"], "strong")
        curve = _combine_curve(panic_curve, strong_curve, profile)
        panic_closed = _load_closed(spec["panic_closed"], "panic")
        strong_closed = _load_closed(spec["strong_closed"], "strong")
        closed = pd.concat([panic_closed, strong_closed], ignore_index=True)
        curve.to_csv(OUT_DIR / f"{profile}_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{profile}_closed_trades.csv", index=False, encoding="utf-8-sig")
        summaries.append(_metrics(profile, spec["note"], curve, closed))
        annual_parts.append(_annual(profile, curve, closed))

    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_parts, ignore_index=True)
    summary.to_csv(OUT_DIR / "combo_execution_stress_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "combo_execution_stress_annual_raw.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual)
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "summary": summary[["profile", "total_ret", "max_drawdown", "worst_open_mtm_ret", "closed", "worst_trade", "bad10_rate"]].to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
