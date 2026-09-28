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


OUT_DIR = report_path("g3_range_weak_repair_fusion_v1")


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df.get(col), errors="coerce")


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    ret = _num(df, "net_ret").dropna()
    pnl = _num(df, "realized_pnl").fillna(0.0)
    win = ret[ret > 0]
    loss = ret[ret <= 0]
    return {
        "trade_count": int(len(df)),
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_ret": float(ret.mean()) if len(ret) else None,
        "median_ret": float(ret.median()) if len(ret) else None,
        "avg_win_ret": float(win.mean()) if len(win) else None,
        "avg_loss_ret": float(loss.mean()) if len(loss) else None,
        "worst_ret": float(ret.min()) if len(ret) else None,
        "best_ret": float(ret.max()) if len(ret) else None,
        "loss_rate_le_5pct": float((ret <= -0.05).mean()) if len(ret) else None,
        "hard_loss_rate_le_10pct": float((ret <= -0.10).mean()) if len(ret) else None,
        "sum_pnl": float(pnl.sum()),
    }


def _group_metrics(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, part in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(group_cols, keys)}
        row.update(_metrics(part))
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["trade_count", "sum_pnl"], ascending=[False, False])


def _feature_profile(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    feature_cols = ["score", "up_rate", "big_down_rate", "mom20", "breadth_ma20"]
    rows: list[dict[str, Any]] = []
    for keys, part in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(group_cols, keys)}
        row["trade_count"] = int(len(part))
        for col in feature_cols:
            if col in part.columns:
                row[f"avg_{col}"] = float(pd.to_numeric(part[col], errors="coerce").mean())
        rows.append(row)
    return pd.DataFrame(rows)


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
            if col in pct_cols or col.endswith("_ret") or col.startswith("avg_"):
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
    return df[df["trade_strategy"].astype(str).eq("range_weak_repair")].copy()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _load()
    merged = pd.DataFrame([{"strategy_label": "震荡弱势修复", **_metrics(df)}])
    by_source = _group_metrics(df, ["route_strategy", "route_strategy_label"])
    by_style = _group_metrics(df, ["route_strategy_label", "market_style"])
    by_window = _group_metrics(df, ["route_strategy_label", "window"])
    by_exit = _group_metrics(df, ["route_strategy_label", "exit_reason"])
    profile = _feature_profile(df, ["route_strategy_label", "market_style"])
    worst = df.sort_values("net_ret").head(15)

    for name, frame in {
        "merged_summary.csv": merged,
        "source_strategy_compare.csv": by_source,
        "by_market_style.csv": by_style,
        "by_window.csv": by_window,
        "by_exit_reason.csv": by_exit,
        "source_feature_profile.csv": profile,
        "worst_trades.csv": worst,
    }.items():
        frame.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    meta = {
        "source": str(g3.HISTORICAL_TRADES_PATH),
        "rows": int(len(df)),
        "output_dir": str(OUT_DIR),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# G3 震荡弱势修复融合审计 v1",
        "",
        "## 结论",
        "",
        "- `旧G3弱势/震荡修复` 与 `震荡恐慌修复` 可以做成一个正式策略主体：`震荡弱势修复`。",
        "- 共同交易主体不是“恐慌”，而是非主升、非下跌主杀环境中的弱势/震荡修复：市场仍在箱体或弱反弹，个股经历回撤或深洗后出现日线修复，入场前优先要求 30m 确认。",
        "- 二者优点互补：旧G3提供更完整的日线结构底座和更大样本；震荡恐慌修复提供更强压力释放过滤、更高收益弹性和更轻尾部亏损。",
        "- 融合后不建议再拆成两个正式交易策略，但建议保留 `source_strategy_label`：`旧G3弱势/震荡修复` 做结构型子状态，`震荡恐慌修复` 做深洗型子状态。",
        "",
        "## 融合后整体表现",
        "",
        _md_table(
            merged,
            ["strategy_label", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct", "sum_pnl"],
            {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct"},
        ),
        "",
        "## 合并前两个来源表现",
        "",
        _md_table(
            by_source,
            ["route_strategy_label", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct", "sum_pnl"],
            {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct"},
        ),
        "",
        "## 市场风格拆解",
        "",
        _md_table(
            by_style,
            ["route_strategy_label", "market_style", "trade_count", "win_rate", "avg_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "worst_ret"},
        ),
        "",
        "## 环境画像",
        "",
        _md_table(
            profile,
            ["route_strategy_label", "market_style", "trade_count", "avg_up_rate", "avg_big_down_rate", "avg_mom20", "avg_breadth_ma20", "avg_score"],
            {"avg_up_rate", "avg_big_down_rate", "avg_mom20", "avg_breadth_ma20", "avg_score"},
        ),
        "",
        "## 建议融合后的交易合同",
        "",
        "1. 策略主体：震荡/弱反弹环境中的压力释放修复，不追主升，不接下跌主杀。",
        "2. 日线入场底座：`market_style in {standard_range, weak_rebound}`；个股处于中短期回撤后修复；收盘位置偏强或有下影/反包；量能不枯竭也不过热。",
        "3. 旧G3优点：保留 `drawdown20`、`range_pos60`、`close_position`、`amount_ratio20` 的结构过滤，避免只凭恐慌扩散买入。",
        "4. 震荡恐慌优点：保留 `big_down_rate/up_rate` 代表的市场压力释放过滤，并要求 30m 修复确认，减少假修复。",
        "5. 仓位与退出：基础首仓 20%；结构止损 -6%，硬止损 -10%；+8% 先减半，剩余仓跌破前低或 30m 转弱退出。",
        "6. 质量分层：`deep_wash_range` 可正常首仓；`weak_rebound_structure` 若指数热度高或宽度退潮，首仓降到 12.5% 或只观察。",
        "",
        "## 最差样本",
        "",
        _md_table(
            worst.assign(entry_date=worst["entry_date"].dt.strftime("%Y-%m-%d")),
            ["entry_date", "code", "name", "route_strategy_label", "market_style", "net_ret", "realized_pnl", "exit_reason"],
            {"net_ret"},
        ),
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
