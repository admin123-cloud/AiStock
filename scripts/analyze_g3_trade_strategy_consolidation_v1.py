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


OUT_DIR = report_path("g3_trade_strategy_consolidation_v1")


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
            if col in pct_cols or col in {"avg_ret", "median_ret", "worst_ret", "best_ret"}:
                cells.append(_fmt_pct(value))
            elif col == "sum_pnl":
                cells.append(_fmt_num(value, 0))
            else:
                cells.append(str(value) if not pd.isna(value) else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _load() -> pd.DataFrame:
    df = pd.read_csv(g3.HISTORICAL_TRADES_PATH, low_memory=False)
    df = g3._normalize_latest_g3_closed_trades(df)
    df = g3._with_route_strategy_fields(df)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["window"] = "pre_2024_10"
    df.loc[df["entry_date"] >= pd.Timestamp("2024-10-01"), "window"] = "post_2024_10"
    df.loc[df["entry_date"] >= pd.Timestamp("2026-01-01"), "window"] = "2026_ytd"
    return df


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _load()

    trade_strategy = _group_metrics(df, ["trade_strategy", "trade_strategy_label"])
    native_strategy = _group_metrics(df, ["trade_strategy_label", "route_strategy", "route_strategy_label"])
    by_window = _group_metrics(df, ["trade_strategy_label", "window"])
    by_style = _group_metrics(df, ["trade_strategy_label", "market_style"])
    by_route = _group_metrics(df, ["trade_strategy_label", "route_parent_label"])
    worst = (
        df.sort_values("net_ret")
        .loc[
            :,
            [
                "entry_date",
                "code",
                "name",
                "trade_strategy_label",
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
        "trade_strategy_summary.csv": trade_strategy,
        "native_strategy_mapping.csv": native_strategy,
        "trade_strategy_by_window.csv": by_window,
        "trade_strategy_by_market_style.csv": by_style,
        "trade_strategy_by_route.csv": by_route,
        "worst_trades.csv": worst,
    }.items():
        frame.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    meta = {
        "source": str(g3.HISTORICAL_TRADES_PATH),
        "rows": int(len(df)),
        "output_dir": str(OUT_DIR),
        "trade_strategy_count": int(trade_strategy["trade_strategy"].nunique()) if not trade_strategy.empty else 0,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# G3 正式交易策略收敛审计 v1",
        "",
        "## 结论先行",
        "",
        "本轮不再把策略强压成 2 个大类，也不把“同属进攻方向”误认为“同一交易策略”。只合并交易逻辑基本重复的来源，保留 5 个正式交易策略。",
        "",
        "1. **机构主升Score120**：保留机构主升浪 Score120 核心。它依赖机构主线扩散、Score120 强确认和 30m 确认。",
        "2. **旧G3强势突破**：保留旧G3跨周期强势突破。它和机构主升同属进攻方向，但入场链路、评分口径和市场适配不同。",
        "3. **量能续强补位**：合并 G2 量能续强与 G2 量能续强+板块加分。它仍是补位策略，不与机构主升或旧G3突破完全等同。",
        "4. **震荡弱势修复**：合并旧G3弱势/震荡修复与震荡恐慌修复。买的是非主升环境里的确认修复。",
        "5. **恐慌出清修复**：合并旧G3恐慌修复与下跌恐慌修复。它和震荡修复同属修复系，但压力等级更高，应保留独立风控。",
        "",
        "这样可以把原先多个命名来源收敛为 5 个真实策略，同时保留来源路由与原生策略字段用于复盘追溯。",
        "",
        "## 正式交易策略表现",
        "",
        _md_table(
            trade_strategy,
            ["trade_strategy_label", "trade_count", "win_rate", "avg_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "worst_ret"},
        ),
        "",
        "## 原生策略映射",
        "",
        _md_table(
            native_strategy,
            ["trade_strategy_label", "route_strategy_label", "trade_count", "win_rate", "avg_ret", "sum_pnl"],
            {"win_rate", "avg_ret"},
        ),
        "",
        "## 分窗口审计",
        "",
        _md_table(
            by_window,
            ["trade_strategy_label", "window", "trade_count", "win_rate", "avg_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "worst_ret"},
        ),
        "",
        "## 市场风格审计",
        "",
        _md_table(
            by_style,
            ["trade_strategy_label", "market_style", "trade_count", "win_rate", "avg_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "worst_ret"},
        ),
        "",
        "## 建议运行口径",
        "",
        "- 路由不再等同交易策略：`institutional_mainwave`、`panic_repair`、`old_g3_route_v3`、`g2_gap_supplement` 只表示来源通道。",
        "- 正式策略用 `trade_strategy`：今日候选、历史成交、逐日复盘、归因行统一使用这一个字段。",
        "- 原生策略用 `route_strategy` 保留：用于定位旧G3链路、G2逻辑、panic_repair 子来源，不再作为并列交易策略。",
        "- `旧G3恐慌修复` 与 `下跌恐慌修复` 合并为 `恐慌出清修复`：日线恐慌出清/压力释放为共同入场主体，盘中优先要求 30m 修复确认，退出统一为 -5% 结构止损、-8% 硬止损、+8% 先减半、剩余仓以前低跌破或 30m 转弱退出。",
        "- 允许在同一 `恐慌出清修复` 内保留压力分层：`standard_downtrend` 首仓更小，但这只是仓位风控状态，不再拆成新的交易策略。",
        "- 后续允许保留 4-6 个正式策略；新增策略必须先证明它和现有正式策略的买点假设或风控合同不同。",
        "",
        "## 最差样本",
        "",
        _md_table(
            worst.assign(entry_date=worst["entry_date"].dt.strftime("%Y-%m-%d")),
            [
                "entry_date",
                "code",
                "name",
                "trade_strategy_label",
                "route_strategy_label",
                "market_style",
                "net_ret",
                "realized_pnl",
            ],
            {"net_ret"},
        ),
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
