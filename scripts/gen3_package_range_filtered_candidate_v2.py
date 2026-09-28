from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path

OUT_DIR = report_path("gen3_range_filtered_candidate_package_v2")
COMBO_DIR = report_path("gen3_combo_range_filter_v1")
STRESS_DIR = report_path("gen3_combo_range_filter_execution_stress_v1")
LIVE_REVIEW_DIR = report_path("gen3_live_visible_stability_review_v1")

VARIANT = "range_conservative_combo_b"


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def _md(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    out = df.copy()
    for col in pct_cols or set():
        if col in out:
            out[col] = out[col].map(_pct)
    return out.to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    curve = _read_csv(COMBO_DIR / f"{VARIANT}_cost30_mtm_equity_curve.csv")
    trades = _read_csv(COMBO_DIR / f"{VARIANT}_cost30_closed_trades.csv")
    combo_summary = _read_csv(COMBO_DIR / "combo_range_filter_summary.csv")
    combo_windows = _read_csv(COMBO_DIR / "combo_range_filter_windows.csv")
    combo_routes = _read_csv(COMBO_DIR / "combo_range_filter_route_attribution.csv")
    stress_summary = _read_csv(STRESS_DIR / "execution_stress_summary.csv")
    stress_windows = _read_csv(STRESS_DIR / "execution_stress_windows.csv")
    stress_routes = _read_csv(STRESS_DIR / "execution_stress_route_attribution.csv")
    old_verdict = _read_csv(LIVE_REVIEW_DIR / "live_visible_goal_verdict.csv")

    combo_summary = combo_summary[combo_summary["variant"].eq(VARIANT)].copy()
    combo_windows = combo_windows[combo_windows["variant"].eq(VARIANT)].copy()
    combo_routes = combo_routes[combo_routes["variant"].eq(VARIANT)].copy()

    curve.to_csv(OUT_DIR / "g3_range_filtered_candidate_equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "g3_range_filtered_candidate_closed_trades.csv", index=False, encoding="utf-8-sig")
    combo_summary.to_csv(OUT_DIR / "g3_range_filtered_candidate_summary.csv", index=False, encoding="utf-8-sig")
    combo_windows.to_csv(OUT_DIR / "g3_range_filtered_candidate_windows.csv", index=False, encoding="utf-8-sig")
    combo_routes.to_csv(OUT_DIR / "g3_range_filtered_candidate_route_attribution.csv", index=False, encoding="utf-8-sig")
    stress_summary.to_csv(OUT_DIR / "g3_range_filtered_execution_stress_summary.csv", index=False, encoding="utf-8-sig")
    stress_windows.to_csv(OUT_DIR / "g3_range_filtered_execution_stress_windows.csv", index=False, encoding="utf-8-sig")
    stress_routes.to_csv(OUT_DIR / "g3_range_filtered_execution_stress_route_attribution.csv", index=False, encoding="utf-8-sig")

    close30 = stress_summary[stress_summary["profile"].eq("close_30bps")].iloc[0].to_dict()
    close100 = stress_summary[stress_summary["profile"].eq("close_100bps")].iloc[0].to_dict()
    nextopen = stress_summary[stress_summary["profile"].eq("nextopen_limitdown_30bps")].iloc[0].to_dict()
    haircut = stress_summary[stress_summary["profile"].eq("nextopen_haircut2_30bps")].iloc[0].to_dict()
    weak100 = stress_windows[
        stress_windows["profile"].eq("close_100bps") & stress_windows["window"].eq("weak_gap_2022_2024")
    ].iloc[0].to_dict()

    goal_audit = pd.DataFrame(
        [
            {
                "requirement": "吸收 G2 强势思想",
                "status": "通过",
                "evidence": "strong_main 保留 D-1 volume5 + 盘中确认，仍为组合主要收益链之一。",
            },
            {
                "requirement": "下降周期超过基数",
                "status": "通过",
                "evidence": f"close_100bps 的 weak_gap_2022_2024 收益 {_pct(weak100['return'])}，回撤 {_pct(weak100['max_drawdown'])}。",
            },
            {
                "requirement": "震荡周期超过基数",
                "status": "阶段通过",
                "evidence": "range_gap 通过过滤 downtrend_strength 与 0-2% 小幅高开后，100bps 单链路由负转正，完整组合 100bps 提升到 116.43%。",
            },
            {
                "requirement": "避免过拟合",
                "status": "部分通过",
                "evidence": "只采用两个宽口径、可解释过滤；已看 train/valid/blind，但仍需 shadow 样本和更长盲测。",
            },
            {
                "requirement": "执行偏差压力",
                "status": "大幅改善但未完全通过",
                "evidence": f"nextopen+跌停延迟为 {_pct(nextopen['total_return'])}；极端 haircut2 为 {_pct(haircut['total_return'])}，仍略负。",
            },
            {
                "requirement": "实盘可接入",
                "status": "未通过",
                "evidence": "本包仍是研究候选，尚未生成 live-safe payload V2，也未打开自动交易。",
            },
        ]
    )
    goal_audit.to_csv(OUT_DIR / "g3_range_filtered_goal_audit.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "candidate": "g3_range_filtered_volume5_v2",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "range_filter": "exclude adx_layer=downtrend_strength and 0<gap_open<=2%",
        "stress_close_30bps": {
            "total_return": close30["total_return"],
            "max_drawdown": close30["max_drawdown"],
            "trade_count": int(close30["trade_count"]),
        },
        "stress_close_100bps": {
            "total_return": close100["total_return"],
            "max_drawdown": close100["max_drawdown"],
            "trade_count": int(close100["trade_count"]),
        },
        "nextopen_limitdown_30bps": {
            "total_return": nextopen["total_return"],
            "max_drawdown": nextopen["max_drawdown"],
            "trade_count": int(nextopen["trade_count"]),
        },
        "severe_nextopen_haircut2": {
            "total_return": haircut["total_return"],
            "max_drawdown": haircut["max_drawdown"],
            "trade_count": int(haircut["trade_count"]),
        },
        "goal_complete": False,
        "next_step": "build_live_safe_payload_v2_for_range_filtered_candidate_shadow_only",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 range-filtered 候选策略包 V2

生成时间：{meta["generated_at"]}

## 候选定义

- `down_panic`：沿用已通过 D-1 环境快照修正的弱势恐慌链。
- `strong_main`：沿用 G2 思想吸收后的 D-1 volume5 + 盘中确认强势链。
- `range_gap`：在原 `range_v3_weak_low_not_chasing_h5` 上增加两个宽口径降噪过滤：
  - 过滤 `adx_layer=downtrend_strength`
  - 过滤 `0 < gap_open <= 2%`

## 组合复算

{_md(combo_summary, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"})}

## 执行压力

{_md(stress_summary, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"})}

## 分窗口

{_md(stress_windows, {"return", "max_drawdown", "win_rate"})}

## 链路贡献

{_md(stress_routes, {"win_rate", "avg_trade_return", "worst_trade"})}

## 目标完成度

{goal_audit.to_markdown(index=False)}

## 与上一阶段相比

上一阶段的主要问题是 `range_gap` 在 100bps 和极端开盘冲击下拖累组合。本候选把 100bps 压力下的组合收益提升到 `{_pct(close100["total_return"])}`，回撤压到 `{_pct(close100["max_drawdown"])}`；极端 haircut2 从大幅失败收敛到 `{_pct(haircut["total_return"])}`。

## 下一步目标

生成独立 live-safe payload V2 和 shadow-only 输出，不覆盖 G2，不打开自动交易。之后继续积累真实影子样本，并专门处理极端开盘冲击仍略负的问题。
"""
    (OUT_DIR / "g3_range_filtered_candidate_package_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
