from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Callable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, standardize, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table, simulate_scaled, summarize  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_range_second_acceptance_v1" / "base_with_second_acceptance_flags.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_ice_recent3_second_accept_expanded_source_v1"

BACKTEST_START = "2020-01-01"
BACKTEST_END = "2026-05-29"
CURVE_END = "2026-06-04"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in [
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "candidate_score",
        "rank_key",
        "fwd_ret_confirm_to_close_5d",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    for col in ["d0_second_accept", "d1_second_accept"]:
        d[col] = d.get(col, False).astype(str).str.lower().isin(["true", "1"])
    d["second_accept_any"] = d["d0_second_accept"] | d["d1_second_accept"]
    d["emotion_signal"] = d.get("emotion_signal", "").astype(str)
    d["rank_key"] = pd.to_numeric(d.get("rank_key", d.get("candidate_score", 0.0)), errors="coerce").fillna(0.0)
    return d.dropna(subset=["entry_date", "code", "entry_price_adjusted", "fwd_ret_confirm_to_close_5d"]).copy()


def variant_specs() -> list[dict[str, Any]]:
    return [
        {
            "variant": "base_ice_recent3_second_accept",
            "desc": "冰点后3日窗口，D0后半日或D1出现二次30m承接",
            "fn": lambda d: d["second_accept_any"],
        },
        {
            "variant": "floor8_second_accept",
            "desc": "箱体位置<=8%，并出现D0/D1二次30m承接",
            "fn": lambda d: d["range_pos60"].le(0.08) & d["second_accept_any"],
        },
        {
            "variant": "floor12_second_accept",
            "desc": "箱体位置<=12%，并出现D0/D1二次30m承接",
            "fn": lambda d: d["range_pos60"].le(0.12) & d["second_accept_any"],
        },
        {
            "variant": "reclaim60_second_accept",
            "desc": "日线收盘修复>=60%，并出现D0/D1二次30m承接",
            "fn": lambda d: d["close_position"].ge(0.60) & d["second_accept_any"],
        },
        {
            "variant": "reclaim70_second_accept",
            "desc": "日线收盘修复>=70%，并出现D0/D1二次30m承接",
            "fn": lambda d: d["close_position"].ge(0.70) & d["second_accept_any"],
        },
        {
            "variant": "floor20_reclaim55_second_accept",
            "desc": "箱体位置<=20%且日线修复>=55%，并出现D0/D1二次30m承接",
            "fn": lambda d: d["range_pos60"].le(0.20) & d["close_position"].ge(0.55) & d["second_accept_any"],
        },
        {
            "variant": "icepoint_only_second_accept",
            "desc": "只保留情绪冰点样本，并出现D0/D1二次30m承接",
            "fn": lambda d: d["emotion_signal"].eq("icepoint") & d["second_accept_any"],
        },
        {
            "variant": "neutral_only_second_accept",
            "desc": "只保留中性情绪样本，并出现D0/D1二次30m承接",
            "fn": lambda d: d["emotion_signal"].eq("neutral") & d["second_accept_any"],
        },
    ]


def coverage(base: pd.DataFrame, specs: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for spec in specs:
        selected = base[spec["fn"](base).fillna(False)].copy()
        ret = pd.to_numeric(selected.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "base_count": int(len(base)),
                "signal_count": int(len(selected)),
                "signal_days": int(selected["entry_date"].nunique()) if len(selected) else 0,
                "icepoint_count": int(selected["emotion_signal"].eq("icepoint").sum()) if len(selected) else 0,
                "neutral_count": int(selected["emotion_signal"].eq("neutral").sum()) if len(selected) else 0,
                "raw_avg_5d": float(ret.mean()) if len(ret) else 0.0,
                "raw_win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def concentration(closed: pd.DataFrame, variant: str) -> dict[str, Any]:
    if closed.empty:
        return {"variant": variant, "trade_count": 0}
    d = closed.sort_values("realized_pnl", ascending=False).copy()
    total_pnl = float(pd.to_numeric(d["realized_pnl"], errors="coerce").sum())
    net = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    out: dict[str, Any] = {
        "variant": variant,
        "trade_count": int(len(d)),
        "total_pnl": total_pnl,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
    }
    for n in [1, 3, 5]:
        rest = d.iloc[n:].copy()
        rest_net = pd.to_numeric(rest.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        out[f"top{n}_removed_trades"] = int(len(rest))
        out[f"top{n}_removed_pnl"] = float(pd.to_numeric(rest.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()) if len(rest) else 0.0
        out[f"top{n}_removed_avg_ret"] = float(rest_net.mean()) if len(rest_net) else 0.0
        out[f"top{n}_removed_win_rate"] = float((rest_net > 0).mean()) if len(rest_net) else 0.0
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    specs = variant_specs()
    cov = coverage(base, specs)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    concentration_rows: list[dict[str, Any]] = []
    for spec in specs:
        selected = base[spec["fn"](base).fillna(False)].copy()
        selected["variant"] = spec["variant"]
        selected["desc"] = spec["desc"]
        selected["family"] = "range_ice_recent3_second_accept_expanded"
        selected["hold_days"] = 5
        selected["rank_in_day"] = selected.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
        selected.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            candidates = standardize(selected, profile)
            curve, closed = simulate_scaled(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, spec["variant"], str(profile["profile"]), spec["desc"]))
            window_rows.extend(window_metrics(curve, closed, spec["variant"], str(profile["profile"])))
            if profile["profile"] == "cost30":
                concentration_rows.append(concentration(closed, spec["variant"]))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    conc = pd.DataFrame(concentration_rows)
    summary.insert(0, "backtest_start", BACKTEST_START)
    summary.insert(1, "backtest_end", BACKTEST_END)
    summary.insert(2, "curve_end", CURVE_END)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "concentration.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "raw_avg_5d",
        "raw_win_rate",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "avg_trade_return",
        "top1_removed_avg_ret",
        "top1_removed_win_rate",
        "top3_removed_avg_ret",
        "top3_removed_win_rate",
        "top5_removed_avg_ret",
        "top5_removed_win_rate",
    }
    top_cost30 = summary[summary["profile"].eq("cost30")].sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    report = [
        "# G3 横盘/箱体底部二次30m承接扩源复验 v1",
        "",
        "## 回测范围",
        f"- 候选信号入场窗口：{BACKTEST_START} 至 {BACKTEST_END}。",
        f"- 资金曲线结算至：{CURVE_END}。",
        "- 底层候选源：`ice_recent3`，中文是“冰点后3日窗口内的横盘/箱体底部候选”。本轮从 99 笔底层样本出发。",
        "- 本轮不使用新的 score/rank 过滤，只用固定结构条件和 D0/D1 二次30m承接。",
        "",
        "## 策略名解释",
        "- `base_ice_recent3_second_accept`：冰点后3日窗口内，只要求 D0 后半日或 D1 出现二次30m承接。",
        "- `floor8_second_accept`：箱体位置<=8%，并出现二次30m承接。",
        "- `floor12_second_accept`：箱体位置<=12%，并出现二次30m承接。",
        "- `reclaim60_second_accept`：日线收盘修复>=60%，并出现二次30m承接。",
        "- `floor20_reclaim55_second_accept`：箱体位置<=20%且日线修复>=55%，并出现二次30m承接。",
        "- `icepoint_only_second_accept`：只保留情绪冰点样本，并出现二次30m承接。",
        "- `neutral_only_second_accept`：只保留中性情绪样本，并出现二次30m承接。",
        "",
        "## 覆盖率",
        md_table(cov, pct_cols=pct_cols),
        "",
        "## 30bps完整slot复算",
        md_table(top_cost30, pct_cols=pct_cols),
        "",
        "## 全部压力口径",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 收益集中度",
        md_table(conc, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "- 如果扩源后交易数仍低于 50 笔，只能作为研究候选，不足以升正式策略。",
        "- 如果 100bps 或 2%冲击口径明显恶化，说明真实成交质量不足。",
        "- 如果 2024-2025 验证段为负，不能用 2026 或少数大赢家覆盖问题。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    result = {
        "backtest_start": BACKTEST_START,
        "backtest_end": BACKTEST_END,
        "curve_end": CURVE_END,
        "best_cost30": top_cost30.head(5).to_dict(orient="records"),
    }
    (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
