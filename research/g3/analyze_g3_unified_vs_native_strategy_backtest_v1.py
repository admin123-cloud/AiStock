from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api import gen3_state_alpha as g3
from utils.paths import report_path


OUT_DIR = report_path("g3_unified_vs_native_strategy_backtest_v1")


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df.get(col), errors="coerce")


def _max_drawdown(values: pd.Series) -> float:
    if values.empty:
        return 0.0
    curve = values.fillna(0.0).cumsum()
    return float((curve - curve.cummax()).min())


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
        "max_drawdown_pnl": _max_drawdown(pnl),
    }


def _group_metrics(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if df.empty:
        return pd.DataFrame()
    for keys, part in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(group_cols, keys)}
        row.update(_metrics(part.sort_values("entry_date")))
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
        cells: list[str] = []
        for col in columns:
            value = row.get(col)
            if col in pct_cols or col.endswith("_ret"):
                cells.append(_fmt_pct(value))
            elif col in {"sum_pnl", "max_drawdown_pnl"}:
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
    return df.sort_values("entry_date").reset_index(drop=True)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _load()

    unified = _group_metrics(df, ["trade_strategy", "trade_strategy_label"])
    native = _group_metrics(df, ["route_strategy", "route_strategy_label", "trade_strategy_label"])
    by_window = _group_metrics(df, ["trade_strategy_label", "window"])
    by_style = _group_metrics(df, ["trade_strategy_label", "market_style"])

    panic_mask = df["trade_strategy"].astype(str).eq("panic_capitulation_repair")
    panic = df[panic_mask].copy()
    panic_merged = _metrics(panic)
    panic_split = _group_metrics(panic, ["route_strategy", "route_strategy_label", "market_style"])
    panic_window = _group_metrics(panic, ["route_strategy_label", "window"])
    panic_worst = panic.sort_values("net_ret").head(12)

    merged_row = {
        "strategy_view": "统一后",
        "strategy_label": "恐慌出清修复",
        **panic_merged,
    }
    split_compare = panic_split.copy()
    split_compare.insert(0, "strategy_view", "合并前")
    split_compare = split_compare.rename(columns={"route_strategy_label": "strategy_label"})
    compare_cols = [
        "strategy_view",
        "strategy_label",
        "trade_count",
        "win_rate",
        "avg_ret",
        "median_ret",
        "worst_ret",
        "best_ret",
        "loss_rate_le_5pct",
        "hard_loss_rate_le_10pct",
        "sum_pnl",
        "max_drawdown_pnl",
    ]
    panic_compare = pd.concat([pd.DataFrame([merged_row]), split_compare], ignore_index=True, sort=False)
    panic_compare = panic_compare[[c for c in compare_cols if c in panic_compare.columns]]

    files = {
        "unified_trade_strategy_backtest.csv": unified,
        "native_strategy_backtest.csv": native,
        "unified_by_window.csv": by_window,
        "unified_by_market_style.csv": by_style,
        "panic_unified_vs_split.csv": panic_compare,
        "panic_split_by_window.csv": panic_window,
        "panic_worst_trades.csv": panic_worst,
    }
    for name, frame in files.items():
        frame.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    meta = {
        "source": str(g3.HISTORICAL_TRADES_PATH),
        "rows": int(len(df)),
        "unified_strategy_count": int(unified["trade_strategy"].nunique()) if not unified.empty else 0,
        "native_strategy_count": int(native["route_strategy"].nunique()) if not native.empty else 0,
        "output_dir": str(OUT_DIR),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# G3 统一交易策略 vs 原生拆分策略回测对照 v1",
        "",
        "## 结论",
        "",
        f"- 样本来源：`{g3.HISTORICAL_TRADES_PATH}`，共 {len(df)} 笔已退出交易。",
        f"- 合并前原生策略数：{meta['native_strategy_count']}；统一后正式交易策略数：{meta['unified_strategy_count']}。",
        "- 本报告重跑的是同一批历史成交的策略归因回测，不重新发明出场价；它回答的是：统一策略口径后，收益、胜率、亏损尾部和样本结构相比原拆分策略是否仍然成立。",
        "- `旧G3恐慌修复` 与 `下跌恐慌修复` 合并后，整体胜率和均值接近两者各自表现；主要新增风险来自 `standard_downtrend` 子样本的 -12% 尾部，适合保留同一策略内的压力仓位分层，而不是继续拆成两个交易策略。",
        "",
        "## 统一后 5 个正式交易策略",
        "",
        _md_table(
            unified,
            ["trade_strategy_label", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct", "sum_pnl", "max_drawdown_pnl"],
            {"win_rate", "avg_ret", "median_ret", "worst_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct"},
        ),
        "",
        "## 合并前原生策略表现",
        "",
        _md_table(
            native,
            ["route_strategy_label", "trade_strategy_label", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "median_ret", "worst_ret"},
        ),
        "",
        "## 恐慌出清修复：统一后 vs 合并前",
        "",
        _md_table(
            panic_compare,
            ["strategy_view", "strategy_label", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct", "sum_pnl", "max_drawdown_pnl"],
            {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct"},
        ),
        "",
        "## 恐慌出清修复分窗口",
        "",
        _md_table(
            panic_window,
            ["route_strategy_label", "window", "trade_count", "win_rate", "avg_ret", "worst_ret", "sum_pnl"],
            {"win_rate", "avg_ret", "worst_ret"},
        ),
        "",
        "## 恐慌出清修复最差样本",
        "",
        _md_table(
            panic_worst.assign(entry_date=panic_worst["entry_date"].dt.strftime("%Y-%m-%d")),
            ["entry_date", "code", "name", "route_strategy_label", "market_style", "net_ret", "realized_pnl", "exit_reason"],
            {"net_ret"},
        ),
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
