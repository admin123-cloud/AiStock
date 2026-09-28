from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_behavior_theme_clusters_v1 import (
    _annotate,
    _build_clusters,
    _code6,
    _latest_trade_dates,
    _load_daily,
    _load_names,
    _load_sector_labels,
    _make_stock_features,
    _select_candidates,
)


DEFAULT_OUT_DIR = _report_path() / "g2_behavior_theme_overlay_backtest_v1"
G2_SOURCE = _report_path() / "gen2_v2_complete_strategy" / "sources" / "g2_v2_complete.parquet"
G2_TRADES = _report_path() / "gen2_v2_complete_strategy" / "runs" / "official" / "full" / "trades.csv"

WINDOWS = {
    "train": ("2024-07-09", "2025-03-31"),
    "valid": ("2025-04-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2024-07-09", "2026-05-29"),
}


from research.common.reporting import timestamp_json_default as _json_default


def _window_of(date: pd.Timestamp) -> str | None:
    for name, (start, end) in WINDOWS.items():
        if name == "full":
            continue
        if pd.Timestamp(start) <= date <= pd.Timestamp(end):
            return name
    return None


def _load_g2_lots() -> pd.DataFrame:
    source = pd.read_parquet(G2_SOURCE).copy()
    trades = pd.read_csv(G2_TRADES, encoding="utf-8-sig").copy()
    source["entry_date_text"] = pd.to_datetime(source["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    source["theme_source_date"] = pd.to_datetime(source["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["buy_date_text"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["sell_date_text"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["return", "pnl", "capital"]:
        trades[col] = pd.to_numeric(trades[col], errors="coerce")

    lots = (
        trades.groupby(["buy_date_text", "code", "name"], dropna=False)
        .agg(
            sell_date=("sell_date_text", "max"),
            lot_return=("return", "sum"),
            pnl=("pnl", "sum"),
            capital=("capital", "first"),
            exit_rows=("return", "count"),
        )
        .reset_index()
        .rename(columns={"buy_date_text": "buy_date"})
    )
    context_cols = [
        "entry_date_text",
        "theme_source_date",
        "code",
        "signal_family",
        "source_family",
        "g2_v2_buy_logic",
        "l1_sector_name",
        "l2_sector_name",
        "l3_sector_name",
        "l3_s3",
        "l2_s3",
        "l3_rise",
        "sector_strong",
        "v4_rank",
        "v4_score",
        "confirm_datetime",
    ]
    context = source[[c for c in context_cols if c in source.columns]].copy()
    lots = lots.merge(context, left_on=["buy_date", "code"], right_on=["entry_date_text", "code"], how="left")
    lots["buy_ts"] = pd.to_datetime(lots["buy_date"], errors="coerce")
    lots["theme_ts"] = pd.to_datetime(lots["theme_source_date"], errors="coerce")
    lots["window"] = lots["buy_ts"].map(_window_of)
    lots["code6"] = lots["code"].map(_code6)
    return lots.dropna(subset=["buy_ts", "theme_ts"]).reset_index(drop=True)


def _theme_for_date(
    target_date: str,
    lookback_days: int,
    min_amount: float,
    max_stocks: int,
    min_corr: float,
    min_size: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = _latest_trade_dates(target_date, int(lookback_days) + 5)
    dates = dates[-int(lookback_days) :]
    daily = _load_daily(dates)
    features = _make_stock_features(daily, dates)
    candidates = _select_candidates(features, max(10, int(lookback_days) - 3), int(max_stocks), float(min_amount))
    raw_clusters, raw_members = _build_clusters(daily, candidates, dates, float(min_corr), int(min_size))
    codes6 = sorted(candidates["code6"].astype(str).unique().tolist())
    names = _load_names(codes6)
    sectors = _load_sector_labels(codes6)
    clusters, members = _annotate(raw_clusters, raw_members, features, names, sectors)
    if not clusters.empty:
        clusters["theme_source_date"] = target_date
    if not members.empty:
        members["theme_source_date"] = target_date
    return clusters, members


def _add_theme_flags(lots: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    all_clusters: list[pd.DataFrame] = []
    all_members: list[pd.DataFrame] = []
    cache: dict[str, pd.DataFrame] = {}
    unique_dates = sorted(lots["theme_source_date"].dropna().astype(str).unique().tolist())
    for i, date in enumerate(unique_dates, 1):
        clusters, members = _theme_for_date(
            date,
            args.lookback_days,
            args.min_amount,
            args.max_stocks,
            args.min_corr,
            args.min_size,
        )
        if not clusters.empty:
            all_clusters.append(clusters)
        if not members.empty:
            all_members.append(members)
        cache[date] = members
        print(f"theme_date {i}/{len(unique_dates)} {date} clusters={len(clusters)} members={len(members)}", flush=True)

    out = lots.copy()
    out["behavior_theme_hit"] = False
    out["behavior_theme_label"] = ""
    out["behavior_theme_score"] = np.nan
    out["behavior_cluster_id"] = np.nan
    for idx, row in out.iterrows():
        members = cache.get(str(row["theme_source_date"]), pd.DataFrame())
        if members.empty:
            continue
        hit = members[members["code6"].astype(str).eq(str(row["code6"]))]
        if hit.empty:
            continue
        best = hit.sort_values("theme_score", ascending=False).iloc[0]
        out.at[idx, "behavior_theme_hit"] = True
        out.at[idx, "behavior_theme_label"] = str(best.get("theme_label", ""))
        out.at[idx, "behavior_theme_score"] = float(best.get("theme_score", np.nan))
        out.at[idx, "behavior_cluster_id"] = int(best.get("cluster_id", 0))

    clusters_df = pd.concat(all_clusters, ignore_index=True) if all_clusters else pd.DataFrame()
    members_df = pd.concat(all_members, ignore_index=True) if all_members else pd.DataFrame()
    return out, clusters_df, members_df


def _max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return np.nan
    equity = (1.0 + returns.fillna(0.0)).cumprod()
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def _stats(part: pd.DataFrame) -> dict[str, Any]:
    returns = pd.to_numeric(part["lot_return"], errors="coerce").dropna()
    if returns.empty:
        return {
            "lots": 0,
            "total_return_compound": np.nan,
            "sum_pnl": 0.0,
            "win_rate": np.nan,
            "avg_lot_return": np.nan,
            "median_lot_return": np.nan,
            "max_drawdown_lot_curve": np.nan,
            "best_lot": np.nan,
            "worst_lot": np.nan,
        }
    ordered = part.sort_values(["buy_ts", "code"]).copy()
    ordered_returns = pd.to_numeric(ordered["lot_return"], errors="coerce").fillna(0.0)
    return {
        "lots": int(len(returns)),
        "total_return_compound": float((1.0 + ordered_returns).prod() - 1.0),
        "sum_pnl": float(pd.to_numeric(part["pnl"], errors="coerce").fillna(0.0).sum()),
        "win_rate": float((returns > 0).mean()),
        "avg_lot_return": float(returns.mean()),
        "median_lot_return": float(returns.median()),
        "max_drawdown_lot_curve": _max_drawdown(ordered_returns),
        "best_lot": float(returns.max()),
        "worst_lot": float(returns.min()),
    }


def _summary(lots: pd.DataFrame) -> pd.DataFrame:
    variants = {
        "all_g2": pd.Series(True, index=lots.index),
        "theme_hit": lots["behavior_theme_hit"].astype(bool),
        "theme_miss": ~lots["behavior_theme_hit"].astype(bool),
        "theme_hit_or_l3_s3_ge_05": lots["behavior_theme_hit"].astype(bool)
        | (pd.to_numeric(lots.get("l3_s3"), errors="coerce").fillna(-1) >= 0.05),
    }
    rows = []
    for variant, mask in variants.items():
        for window, (start, end) in WINDOWS.items():
            wmask = (lots["buy_ts"] >= pd.Timestamp(start)) & (lots["buy_ts"] <= pd.Timestamp(end))
            part = lots[mask & wmask].copy()
            row = {"variant": variant, "window": window, "start_date": start, "end_date": end}
            row.update(_stats(part))
            rows.append(row)
    return pd.DataFrame(rows)


def _write_report(
    lots: pd.DataFrame,
    clusters: pd.DataFrame,
    members: pd.DataFrame,
    summary: pd.DataFrame,
    args: argparse.Namespace,
) -> None:
    out_dir = _report_path() / str(args.out_dir_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    lots_path = out_dir / "g2_lots_with_behavior_theme.csv"
    clusters_path = out_dir / "rolling_theme_clusters.csv"
    members_path = out_dir / "rolling_theme_members.csv"
    summary_path = out_dir / "summary.csv"
    report_path = out_dir / "REPORT.md"
    json_path = out_dir / "summary.json"
    lots.to_csv(lots_path, index=False, encoding="utf-8-sig")
    clusters.to_csv(clusters_path, index=False, encoding="utf-8-sig")
    members.to_csv(members_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    payload = {
        "report": str(report_path),
        "summary_csv": str(summary_path),
        "lots_csv": str(lots_path),
        "rolling_theme_clusters_csv": str(clusters_path),
        "rolling_theme_members_csv": str(members_path),
        "g2_lots": int(len(lots)),
        "theme_hit_lots": int(lots["behavior_theme_hit"].sum()),
        "lookback_days": int(args.lookback_days),
        "min_corr": float(args.min_corr),
        "min_size": int(args.min_size),
        "research_only": True,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    def pct(x: Any) -> str:
        if pd.isna(x):
            return ""
        return f"{float(x):.2%}"

    lines = [
        "# G2 + 行为主题簇滚动回测 V1",
        "",
        "## 回测口径",
        "",
        "- 基础策略：`g2_v2_complete` 正式 full 交易记录。",
        "- 主题生成：对每笔 G2 买入，使用其信号源 `trade_date` 往前 20 个交易日生成行为主题簇。",
        "- 未来函数控制：不使用买入日收盘后数据；主题簇只用买入前一交易日及之前的日线。",
        f"- 聚类参数：`lookback_days={args.lookback_days}`，`min_corr={args.min_corr}`，`min_size={args.min_size}`。",
        "- 结果解释：`theme_hit` 表示该 G2 买入股票在买入前已经属于某个行为主题簇。",
        "",
        "## 核心结果",
        "",
        "| variant | window | 日期 | 笔数 | 复合收益 | 最大回撤 | 胜率 | 平均单笔 | 中位单笔 | PnL |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.itertuples(index=False):
        lines.append(
            f"| {row.variant} | {row.window} | {row.start_date}~{row.end_date} | {int(row.lots)} | "
            f"{pct(row.total_return_compound)} | {pct(row.max_drawdown_lot_curve)} | "
            f"{pct(row.win_rate)} | {pct(row.avg_lot_return)} | {pct(row.median_lot_return)} | "
            f"{float(row.sum_pnl):.0f} |"
        )
    hit_examples = lots[lots["behavior_theme_hit"]].sort_values(["buy_ts", "code"]).head(25)
    lines.extend(["", "## 命中样例", ""])
    if hit_examples.empty:
        lines.append("- 无命中样例。")
    else:
        lines.extend(["| 日期 | 股票 | 主题 | 收益 | G2家族 |", "|---|---|---|---:|---|"])
        for row in hit_examples.itertuples(index=False):
            lines.append(
                f"| {row.buy_date} | {row.name} `{row.code}` | {row.behavior_theme_label} | "
                f"{pct(row.lot_return)} | {getattr(row, 'signal_family', '')} |"
            )
    lines.extend(
        [
            "",
            "## 初步结论",
            "",
            "- 如果 `theme_hit` 笔数偏少，说明当前聚类更像捕捉大主线核心票，不适合直接硬过滤 G2。",
            "- 如果 `theme_hit_or_l3_s3_ge_05` 优于全量 G2，说明更合理的接入方式是“行为主题簇 + 行业扩散”联合加分。",
            "- 这仍是研究版，尚未加入完整资金曲线重放、滑点、涨跌停排队和容量约束。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(str(report_path))
    print(str(summary_path))
    print(str(lots_path))
    print(f"g2_lots={len(lots)} theme_hit_lots={int(lots['behavior_theme_hit'].sum())}")
    print(summary.to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest G2 overlay with rolling behavior theme clusters.")
    parser.add_argument("--lookback-days", type=int, default=20)
    parser.add_argument("--min-amount", type=float, default=1_000_000)
    parser.add_argument("--max-stocks", type=int, default=700)
    parser.add_argument("--min-corr", type=float, default=0.58)
    parser.add_argument("--min-size", type=int, default=5)
    parser.add_argument("--out-dir-name", default=DEFAULT_OUT_DIR.name)
    args = parser.parse_args()

    lots = _load_g2_lots()
    lots, clusters, members = _add_theme_flags(lots, args)
    summary = _summary(lots)
    _write_report(lots, clusters, members, summary, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
