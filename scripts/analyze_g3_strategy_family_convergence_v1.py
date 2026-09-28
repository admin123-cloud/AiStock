from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api import gen3_state_alpha as g3
from utils.paths import report_path


OUT_DIR = report_path("g3_strategy_family_convergence_v1")


SUPER_STRATEGY_LABELS = {
    "trend_offense": "趋势进攻统一策略",
    "repair_reversal": "修复反转统一策略",
}

FAMILY_TO_SUPER = {
    "mainwave_breakout_offense": "trend_offense",
    "volume_runup_supplement": "trend_offense",
    "repair_range_weak": "repair_reversal",
    "panic_capitulation_repair": "repair_reversal",
}


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df.get(col), errors="coerce")


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    ret = _num(df, "net_ret").dropna()
    pnl = _num(df, "realized_pnl")
    return {
        "trade_count": int(len(df)),
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_ret": float(ret.mean()) if len(ret) else None,
        "median_ret": float(ret.median()) if len(ret) else None,
        "worst_ret": float(ret.min()) if len(ret) else None,
        "best_ret": float(ret.max()) if len(ret) else None,
        "loss_rate_le_5pct": float((ret <= -0.05).mean()) if len(ret) else None,
        "hard_loss_rate_le_10pct": float((ret <= -0.10).mean()) if len(ret) else None,
        "sum_pnl": float(pnl.sum()) if pnl.notna().any() else 0.0,
    }


def _group_metrics(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if df.empty:
        return pd.DataFrame()
    for keys, part in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(group_cols, keys)}
        row.update(_metrics(part))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["trade_count", "sum_pnl"], ascending=[False, False])


def _fmt_pct(value: Any) -> str:
    try:
        if pd.isna(value):
            return "--"
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "--"


def _fmt_num(value: Any, digits: int = 0) -> str:
    try:
        if pd.isna(value):
            return "--"
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "--"


def _md_table(df: pd.DataFrame, columns: list[str], pct_cols: set[str] | None = None) -> str:
    pct_cols = pct_cols or set()
    if df.empty:
        return "_无数据_"
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for _, row in df[columns].iterrows():
        cells = []
        for col in columns:
            value = row.get(col)
            if col in pct_cols:
                cells.append(_fmt_pct(value))
            elif col in {"sum_pnl"}:
                cells.append(_fmt_num(value, 0))
            elif col in {"avg_ret", "median_ret", "worst_ret", "best_ret"}:
                cells.append(_fmt_pct(value))
            else:
                cells.append(str(value) if not pd.isna(value) else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _load() -> pd.DataFrame:
    df = pd.read_csv(g3.HISTORICAL_TRADES_PATH, low_memory=False)
    df = g3._normalize_latest_g3_closed_trades(df)
    df = g3._with_route_strategy_fields(df)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["year"] = df["entry_date"].dt.year
    df["window"] = "pre_2024_10"
    df.loc[df["entry_date"] >= pd.Timestamp("2024-10-01"), "window"] = "post_2024_10"
    df.loc[df["entry_date"] >= pd.Timestamp("2026-01-01"), "window"] = "2026_ytd"
    df["unified_strategy"] = df["route_strategy_family"].map(FAMILY_TO_SUPER).fillna("other")
    df["unified_strategy_label"] = df["unified_strategy"].map(SUPER_STRATEGY_LABELS).fillna("其他")
    return df


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _load()

    family = _group_metrics(df, ["route_strategy_family", "route_strategy_family_label"])
    strategy = _group_metrics(df, ["route_strategy", "route_strategy_label", "route_strategy_family_label"])
    unified = _group_metrics(df, ["unified_strategy", "unified_strategy_label"])
    unified_window = _group_metrics(df, ["unified_strategy_label", "window"])
    unified_style = _group_metrics(df, ["unified_strategy_label", "market_style"])
    family_window = _group_metrics(df, ["route_strategy_family_label", "window"])
    family_style = _group_metrics(df, ["route_strategy_family_label", "market_style"])

    worst = (
        df.sort_values("net_ret")
        .loc[
            :,
            [
                "entry_date",
                "code",
                "name",
                "route_strategy_family_label",
                "route_strategy_label",
                "route_parent_label",
                "market_style",
                "net_ret",
                "realized_pnl",
                "exit_reason",
            ],
        ]
        .head(20)
    )

    for name, frame in {
        "strategy_family_summary.csv": family,
        "native_strategy_summary.csv": strategy,
        "unified_strategy_summary.csv": unified,
        "unified_strategy_by_window.csv": unified_window,
        "unified_strategy_by_market_style.csv": unified_style,
        "strategy_family_by_window.csv": family_window,
        "strategy_family_by_market_style.csv": family_style,
        "worst_trades.csv": worst,
    }.items():
        frame.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    meta = {
        "source": str(g3.HISTORICAL_TRADES_PATH),
        "rows": int(len(df)),
        "output_dir": str(OUT_DIR),
        "unified_strategy_count": int(unified["unified_strategy"].nunique()) if not unified.empty else 0,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# G3 策略族收敛审计 v1",
        "",
        "## 结论先行",
        "",
        "当前 4 个策略族可以继续收敛成 2 套统一交易策略，但不建议收敛成 1 套。",
        "",
        "1. **趋势进攻统一策略**：合并 `主升/突破进攻` 与 `量能续强补位`。两者都依赖个股强度、量能延续和趋势/主线承接，差异主要是来源路由和优先级。执行上应统一为“趋势进攻候选”，机构主升优先，G2 量能续强只作为补位候选。",
        "2. **修复反转统一策略**：合并 `震荡/弱势修复` 与 `恐慌出清修复`。两者都在非主升环境里买修复，差异是市场压力等级和入场确认强度。执行上应统一为“修复反转候选”，再按市场压力分为震荡修复/恐慌出清两个子状态。",
        "3. 不建议把趋势进攻和修复反转合成一个策略，因为它们的买入假设、容错方式和风险来源不同：趋势进攻怕追高/退潮，修复反转怕弱势延续/二次下杀。",
        "",
        "## 策略族表现",
        "",
        _md_table(
            family,
            ["route_strategy_family_label", "trade_count", "win_rate", "avg_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "worst_ret"},
        ),
        "",
        "## 统一策略表现",
        "",
        _md_table(
            unified,
            ["unified_strategy_label", "trade_count", "win_rate", "avg_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "worst_ret"},
        ),
        "",
        "## 分窗口审计",
        "",
        _md_table(
            unified_window,
            ["unified_strategy_label", "window", "trade_count", "win_rate", "avg_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "worst_ret"},
        ),
        "",
        "## 市场风格审计",
        "",
        _md_table(
            unified_style,
            ["unified_strategy_label", "market_style", "trade_count", "win_rate", "avg_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "worst_ret"},
        ),
        "",
        "## 交易策略一致性判断",
        "",
        "### 可以合并为趋势进攻统一策略",
        "",
        "- 包含：`机构主升浪Score120核心`、`旧G3强势突破`、`G2量能续强+板块加分`、`G2量能续强`。",
        "- 共同交易假设：强票或强量能延续，买的是趋势延伸，不是低位修复。",
        "- 统一入口：趋势强度分 + 量能延续分 + 板块/主线承接分。",
        "- 统一排序：机构主升 > 旧G3强势突破 > G2量能续强带板块加分 > G2量能续强无板块加分。",
        "- 统一风控：高热度降仓、同板块暴露限制、退潮时禁开或只留一槽。",
        "",
        "### 可以合并为修复反转统一策略",
        "",
        "- 包含：`震荡恐慌修复`、`旧G3弱势/震荡修复`、`旧G3恐慌修复`、`下跌恐慌修复`。",
        "- 共同交易假设：市场或个股已有压力释放，买的是修复反弹，不是趋势延伸。",
        "- 统一入口：压力释放分 + 修复确认分 + 流动性/下影/收盘位置分。",
        "- 统一排序：恐慌出清确认 > 震荡弱势修复；若没有 30m 修复确认，只观察不正式买入。",
        "- 统一风控：默认低于趋势进攻仓位，弱市只允许单槽，二次下杀及时退出。",
        "",
        "## 不建议继续合并为一套的原因",
        "",
        "- 趋势进攻的正确买点通常是强者恒强；修复反转的正确买点是过度下跌后的确认修复。",
        "- 趋势进攻可容忍更高位置，但不能容忍主线退潮；修复反转可容忍低位波动，但不能容忍压力继续扩散。",
        "- 若硬合成一个评分，容易把“高分趋势票”和“高分修复票”混排，导致仓位语义不一致。",
        "",
        "## 建议的最终运行形态",
        "",
        "把当前十几个原生策略收敛为两个正式策略引擎：",
        "",
        "1. `trend_offense`：趋势进攻统一策略。",
        "2. `repair_reversal`：修复反转统一策略。",
        "",
        "原有路由只作为候选来源，不再作为交易策略并行运行：",
        "",
        "- `institutional_mainwave`、`old_g3 strong_main`、`g2 volume5` 进入 `trend_offense` 候选池。",
        "- `panic_repair`、`old_g3 range_gap/down_panic` 进入 `repair_reversal` 候选池。",
        "- 页面归因继续展示策略族，成交明细保留原生策略和来源路由，方便追溯。",
        "",
        "## 最差样本",
        "",
        _md_table(
            worst.assign(entry_date=worst["entry_date"].dt.strftime("%Y-%m-%d")),
            ["entry_date", "code", "name", "route_strategy_family_label", "route_strategy_label", "market_style", "net_ret", "realized_pnl"],
            {"net_ret"},
        ),
        "",
        "## 下一步",
        "",
        "建议下一步不再新增策略名，而是实现两个统一策略的候选评分字段：`trend_offense_score` 与 `repair_reversal_score`。历史归因、候选池、今日买入、影子持有应统一使用这两个主分数，原生策略名只作为 `source_strategy` 保留。",
        "",
    ]
    (OUT_DIR / "strategy_family_convergence_report_cn.md").write_text("\n".join(report), encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
