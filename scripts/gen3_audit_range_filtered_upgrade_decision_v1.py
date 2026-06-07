from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_range_filtered_upgrade_decision_v1"

GUARDED_SUMMARY = ROOT / "reports" / "gen3_guarded_candidate_package_v1" / "g3_guarded_candidate_summary.csv"
GUARDED_STRESS = ROOT / "reports" / "gen3_guarded_candidate_package_v1" / "g3_guarded_execution_stress_summary.csv"
RANGE_SUMMARY = ROOT / "reports" / "gen3_combo_range_filter_v1" / "combo_range_filter_summary.csv"
RANGE_STRESS = ROOT / "reports" / "gen3_combo_range_filter_execution_stress_v1" / "execution_stress_summary.csv"
RANGE_ROUTES = ROOT / "reports" / "gen3_combo_range_filter_execution_stress_v1" / "execution_stress_route_attribution.csv"
RANGE_WINDOWS = ROOT / "reports" / "gen3_combo_range_filter_execution_stress_v1" / "execution_stress_windows.csv"


def pct(v: object) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def pick(df: pd.DataFrame, **conds: str) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for col, value in conds.items():
        mask &= df[col].astype(str).eq(str(value))
    matched = df[mask]
    if matched.empty:
        raise ValueError(f"missing row: {conds}")
    return matched.iloc[0]


def row_to_metrics(label: str, source: str, row: pd.Series) -> dict[str, object]:
    return {
        "candidate": label,
        "source": source,
        "trade_count": int(row["trade_count"]),
        "total_return": float(row["total_return"]),
        "max_drawdown": float(row["max_drawdown"]),
        "win_rate": float(row["win_rate"]),
        "avg_trade_return": float(row["avg_trade_return"]),
        "worst_trade": float(row["worst_trade"]),
    }


def md_table(df: pd.DataFrame, pct_cols: set[str]) -> str:
    out = df.copy()
    for col in pct_cols:
        if col in out.columns:
            out[col] = out[col].map(pct)
    for col in out.columns:
        if col not in pct_cols and pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].map(lambda v: "" if pd.isna(v) else f"{v:.4f}")
    return out.to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    guarded_summary = pd.read_csv(GUARDED_SUMMARY, low_memory=False)
    guarded_stress = pd.read_csv(GUARDED_STRESS, low_memory=False)
    range_summary = pd.read_csv(RANGE_SUMMARY, low_memory=False)
    range_stress = pd.read_csv(RANGE_STRESS, low_memory=False)
    range_routes = pd.read_csv(RANGE_ROUTES, low_memory=False)
    range_windows = pd.read_csv(RANGE_WINDOWS, low_memory=False)

    guarded_main = pick(guarded_summary, guard="strong_breadth_score_volume5_guard")
    range_30 = pick(range_summary, variant="range_conservative_combo_b", cost_bps="30.0")
    rows = [
        row_to_metrics("guarded_volume5_30bps", "旧稳健基准：强势 volume5 加防守过滤", guarded_main),
        row_to_metrics("range_conservative_combo_b_30bps", "新保守横盘组合：旧基准叠加横盘过滤", range_30),
        row_to_metrics("guarded_volume5_100bps", "旧基准 100bps 成本压力", pick(guarded_stress, profile="close_100bps")),
        row_to_metrics("range_conservative_combo_b_100bps", "新组合 100bps 成本压力", pick(range_stress, profile="close_100bps")),
        row_to_metrics(
            "guarded_volume5_nextopen_limitdown",
            "旧基准次日开盘加跌停延迟",
            pick(guarded_stress, profile="nextopen_limitdown_30bps"),
        ),
        row_to_metrics(
            "range_conservative_combo_b_nextopen_limitdown",
            "新组合次日开盘加跌停延迟",
            pick(range_stress, profile="nextopen_limitdown_30bps"),
        ),
        row_to_metrics(
            "guarded_volume5_haircut2",
            "旧基准次日开盘再额外扣 2% 冲击",
            pick(guarded_stress, profile="nextopen_haircut2_30bps"),
        ),
        row_to_metrics(
            "range_conservative_combo_b_haircut2",
            "新组合次日开盘再额外扣 2% 冲击",
            pick(range_stress, profile="nextopen_haircut2_30bps"),
        ),
    ]
    matrix = pd.DataFrame(rows)
    matrix.to_csv(OUT_DIR / "upgrade_decision_matrix.csv", index=False, encoding="utf-8-sig")

    route_haircut = range_routes[range_routes["profile"].eq("nextopen_haircut2_30bps")].copy()
    route_haircut.to_csv(OUT_DIR / "range_haircut2_route_attribution.csv", index=False, encoding="utf-8-sig")

    window_haircut = range_windows[range_windows["profile"].eq("nextopen_haircut2_30bps")].copy()
    window_haircut.to_csv(OUT_DIR / "range_haircut2_window_summary.csv", index=False, encoding="utf-8-sig")

    close100 = pick(range_stress, profile="close_100bps")
    nextopen = pick(range_stress, profile="nextopen_limitdown_30bps")
    haircut = pick(range_stress, profile="nextopen_haircut2_30bps")
    verdict = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidate": "range_conservative_combo_b",
        "decision": "research_upgrade_yes_live_no",
        "close_100bps_total_return": float(close100["total_return"]),
        "close_100bps_max_drawdown": float(close100["max_drawdown"]),
        "nextopen_limitdown_total_return": float(nextopen["total_return"]),
        "nextopen_limitdown_max_drawdown": float(nextopen["max_drawdown"]),
        "haircut2_total_return": float(haircut["total_return"]),
        "haircut2_max_drawdown": float(haircut["max_drawdown"]),
        "next_step": "audit_haircut2_failure_then_build_shadow_only_payload_if_needed",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    report = f"""# G3 range-filtered 升级判定 V1

生成时间：{verdict["generated_at"]}

## 英文名解释

- `guarded_volume5`：旧的 G3 稳健基准。核心是吸收 G2 的强势 `volume5` 思路，但加了市场宽度、强势分数等防守过滤。
- `range_conservative_combo_b`：新的保守横盘组合。含义是保留 `down_panic` 弱势恐慌、`strong_main` 强势主线，同时对 `range_gap` 横盘/弱反弹链路过滤掉两类噪声：`adx_layer=downtrend_strength` 明显下跌趋势，以及 `0 < gap_open <= 2%` 小幅高开追价。
- `close_100bps`：按收盘退出，并把单笔交易成本压力提高到 100bps。
- `nextopen_limitdown_30bps`：按次日可交易开盘退出，并模拟跌停不可卖导致的延迟。
- `nextopen_haircut2_30bps`：最严苛压力，次日开盘口径再额外扣 2%，代表低开、滑点、冲击成本一起打到策略上。

## 升级判定

结论：`range_conservative_combo_b` 可以升级为“研究候选”，但不能升级为“实盘买点”。

原因有两层：

1. 正常和高成本压力下，它明显优于旧 `guarded_volume5`。100bps 下新组合收益 {pct(close100["total_return"])}，回撤 {pct(close100["max_drawdown"])}；旧基准 100bps 只有 {pct(pick(guarded_stress, profile="close_100bps")["total_return"])}，回撤 {pct(pick(guarded_stress, profile="close_100bps")["max_drawdown"])}。
2. 真实冲击极端口径仍未过关。`nextopen_haircut2_30bps` 下新组合收益 {pct(haircut["total_return"])}，回撤 {pct(haircut["max_drawdown"])}，说明弱势和横盘链路仍怕买后低开/滑点。

## 对照矩阵

{md_table(matrix, pct_cols)}

## 新组合在极端冲击下的链路归因

{md_table(route_haircut, {"win_rate", "avg_trade_return", "worst_trade"})}

这里可以看到：`strong_main` 仍为正，`down_panic` 和 `range_gap` 被打成负数。也就是说，问题不是强势吸收 G2 思路这条线，而是弱势恐慌和横盘修复在真实执行冲击下的安全垫还不够厚。

## 新组合在极端冲击下的窗口表现

{md_table(window_haircut, {"return", "max_drawdown", "win_rate"})}

## 下一步目标

停止继续调 score/rank。下一步专门审计 `nextopen_haircut2_30bps` 的失败样本：按 `down_panic` 和 `range_gap` 分别找出买后低开/冲击的共同结构，例如入场前是否仍是趋势下跌、箱体底部是否失效、成交量是否是出货而不是承接。只有找到结构性原因，才考虑生成新的 shadow-only payload。
"""
    (OUT_DIR / "upgrade_decision_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(verdict, ensure_ascii=False))


if __name__ == "__main__":
    main()
