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

import numpy as np
import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


OUT = _report_path() / "mainline_overlay_g2_g3_validation_v1"
G2_SOURCE = _report_path() / "gen2_v2_complete_strategy" / "sources" / "g2_v2_complete.parquet"
G2_TRADES = _report_path() / "gen2_v2_complete_strategy" / "runs" / "official" / "full" / "trades.csv"
G2_SUMMARY = _report_path() / "gen2_v2_complete_strategy" / "summary.json"
G2_SECTOR_MATRIX = _report_path() / "g2_sector_integration_probe" / "sector_integration_summary.csv"
G2_BREAKOUT_MATRIX = (
    _report_path()
    / "gen2_breakout_buy_point_research"
    / "breakout_family_intraday_strength_probe"
    / "combo_policy_probe"
    / "sector_context_probe"
    / "sector_filter_matrix"
    / "sector_filter_summary.csv"
)
G3_EVAL = _report_path() / "gen3_strong_v2_independent_source_v1" / "strong_v2_eval_summary.csv"
G3_WINDOW = _report_path() / "gen3_strong_v2_independent_source_v1" / "strong_v2_window_summary.csv"


WINDOWS = {
    "train": ("2024-07-09", "2025-03-31"),
    "valid": ("2025-04-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2024-07-09", "2026-05-29"),
}


from research.common.reporting import timestamp_json_default as _json_default


def _pct(v: Any) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _num(v: Any, digits: int = 4) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.{digits}f}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = _num(value)
            else:
                item[col] = "" if pd.isna(value) else value
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _window_of(date: pd.Timestamp) -> str | None:
    for name, (start, end) in WINDOWS.items():
        if name == "full":
            continue
        if pd.Timestamp(start) <= date <= pd.Timestamp(end):
            return name
    return None


def _load_g2_lots() -> pd.DataFrame:
    src = pd.read_parquet(G2_SOURCE).copy()
    trades = pd.read_csv(G2_TRADES).copy()
    src["entry_date"] = pd.to_datetime(src["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["buy_date"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    lot = (
        trades.groupby(["buy_date", "code", "name"], dropna=False)
        .agg(
            sell_date=("sell_date", "max"),
            pnl=("pnl", "sum"),
            lot_return=("return", "sum"),
            capital=("capital", "first"),
            exit_rows=("exit_reason", "count"),
            exit_reasons=("exit_reason", lambda s: "|".join(map(str, s))),
            v4_rank=("v4_rank", "first"),
            v4_score=("v4_score", "first"),
        )
        .reset_index()
    )
    keep_cols = [
        "entry_date",
        "code",
        "signal_family",
        "source_family",
        "g2_v2_buy_logic",
        "sector_strong",
        "l3_s3",
        "l2_s3",
        "l3_rise",
        "l3_sector_name",
        "l2_sector_name",
        "confirm_datetime",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "v4_rank",
        "v4_score",
    ]
    ctx = src[[c for c in keep_cols if c in src.columns]].copy()
    out = lot.merge(ctx, left_on=["buy_date", "code"], right_on=["entry_date", "code"], how="left", suffixes=("", "_signal"))
    out["buy_ts"] = pd.to_datetime(out["buy_date"], errors="coerce")
    out["window"] = out["buy_ts"].map(_window_of)
    out["sector_strong"] = out["sector_strong"].fillna(False).astype(bool)
    out["l3_s3"] = pd.to_numeric(out["l3_s3"], errors="coerce")
    out["l2_s3"] = pd.to_numeric(out["l2_s3"], errors="coerce")
    out["lot_return"] = pd.to_numeric(out["lot_return"], errors="coerce")
    out["pnl"] = pd.to_numeric(out["pnl"], errors="coerce")
    out["l3_s3_bucket"] = pd.cut(
        out["l3_s3"],
        bins=[-np.inf, 0.05, 0.10, 0.30, np.inf],
        labels=["weak_<5%", "warm_5-10%", "strong_10-30%", "hot_>=30%"],
        right=False,
    )
    return out


def _lot_summary(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows = []
    for keys, part in df.groupby(group_cols, dropna=False, observed=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = dict(zip(group_cols, keys))
        row.update(
            {
                "lots": int(len(part)),
                "win_rate": float((part["lot_return"] > 0).mean()),
                "avg_lot_return": float(part["lot_return"].mean()),
                "median_lot_return": float(part["lot_return"].median()),
                "sum_pnl": float(part["pnl"].sum()),
                "worst_lot": float(part["lot_return"].min()),
                "best_lot": float(part["lot_return"].max()),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _compact_matrix(path: Path, variants: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df = df[df["variant"].isin(variants)].copy()
    cols = [
        "variant",
        "window",
        "signals",
        "trades",
        "total_return",
        "excess_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
    ]
    return df[[c for c in cols if c in df.columns]].copy()


def _g2_official_summary() -> pd.DataFrame:
    data = json.loads(G2_SUMMARY.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    df = pd.DataFrame(rows)
    cols = ["window", "signal_count", "trade_count", "total_return", "excess_return", "max_drawdown", "win_rate", "avg_trade_return"]
    return df[[c for c in cols if c in df.columns]].copy()


def _g3_compact() -> tuple[pd.DataFrame, pd.DataFrame]:
    eval_df = pd.read_csv(G3_EVAL) if G3_EVAL.exists() else pd.DataFrame()
    win_df = pd.read_csv(G3_WINDOW) if G3_WINDOW.exists() else pd.DataFrame()
    if not eval_df.empty:
        eval_df = eval_df[
            eval_df["variant"].isin(["strong_v2_main_up_plus_weak04", "strong_v2_sector_confirmed", "strong_v2_main_up_only"])
        ].copy()
        eval_df = eval_df.sort_values(["hold_days", "total_return"], ascending=[True, False])
    if not win_df.empty:
        win_df = win_df[
            win_df["variant"].isin(["strong_v2_main_up_plus_weak04", "strong_v2_sector_confirmed", "strong_v2_main_up_only"])
            & win_df["hold_days"].isin([5, 10, 20])
        ].copy()
        win_df = win_df.sort_values(["variant", "hold_days", "window"])
    return eval_df, win_df


def run() -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)

    lots = _load_g2_lots()
    lots.to_csv(OUT / "g2_current_official_lot_context.csv", index=False, encoding="utf-8-sig")
    by_bucket = _lot_summary(lots, ["window", "l3_s3_bucket"]).sort_values(["window", "l3_s3_bucket"])
    by_family = _lot_summary(lots, ["window", "signal_family", "sector_strong"]).sort_values(
        ["window", "signal_family", "sector_strong"]
    )
    by_sector = (
        _lot_summary(lots, ["l3_sector_name", "sector_strong"])
        .sort_values(["sum_pnl"], ascending=False)
        .head(20)
    )
    by_bucket.to_csv(OUT / "g2_current_by_l3_s3_bucket.csv", index=False, encoding="utf-8-sig")
    by_family.to_csv(OUT / "g2_current_by_family_sector.csv", index=False, encoding="utf-8-sig")
    by_sector.to_csv(OUT / "g2_current_top_l3_sector_lots.csv", index=False, encoding="utf-8-sig")

    official = _g2_official_summary()
    sector_matrix = _compact_matrix(G2_SECTOR_MATRIX, ["base", "gate_all_05", "gate_all_10", "gate_all_03", "gate_all_06", "gate_l2or3"])
    breakout_matrix = _compact_matrix(G2_BREAKOUT_MATRIX, ["base_combo_or", "l3_s3_ge_05", "l3_s3_ge_10", "l3_rise55_s3_ge_05"])
    g3_eval, g3_window = _g3_compact()

    official.to_csv(OUT / "g2_official_summary_compact.csv", index=False, encoding="utf-8-sig")
    sector_matrix.to_csv(OUT / "g2_sector_integration_compact.csv", index=False, encoding="utf-8-sig")
    breakout_matrix.to_csv(OUT / "g2_breakout_sector_filter_compact.csv", index=False, encoding="utf-8-sig")
    g3_eval.to_csv(OUT / "g3_strong_v2_eval_compact.csv", index=False, encoding="utf-8-sig")
    g3_window.to_csv(OUT / "g3_strong_v2_window_compact.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "total_return",
        "excess_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "avg_lot_return",
        "median_lot_return",
        "worst_lot",
        "best_lot",
        "worst_trade",
        "return",
        "bad10_rate",
    }

    lines = [
        "# 主线/板块方向叠加对 G2/G3 的回测验证 V1",
        "",
        "## 验证问题",
        "",
        "不复现短视频收益数字，只验证一个可落地命题：在我们的 G2/G3 候选与组合里，加入“市场环境 -> 主线板块强度 -> 个股”的方向层，是否能提高收益或改善风险收益比。",
        "",
        "## 数据与防偏差口径",
        "",
        "- G2 当前正式口径：`reports/gen2_v2_complete_strategy`，窗口到 `2026-05-29`。",
        "- G2 板块强度字段来自信号确认时刻的 30m 行业内成员扩散度，例如 `l3_s3 = 三级行业中涨幅>=3%的成员占比`。",
        "- 本报告的逐笔归因按 `buy_date + code` 合并半仓/剩余仓退出行，避免把同一笔交易重复计数。",
        "- 组合增益引用已跑出的 sector matrix；它用相同回测引擎重跑不同板块过滤，不是只做事后逐笔分组。",
        "- G3 部分引用现有 strong_v2 迁移测试；其中固定持有代理不是实盘撮合，只用于验证强势候选源是否有进攻性。",
        "",
        "## G2 当前正式组合",
        "",
        _md_table(official, pct_cols=pct_cols),
        "",
        "## G2 当前成交逐笔归因：按三级板块强度分桶",
        "",
        _md_table(by_bucket, pct_cols=pct_cols),
        "",
        "## G2 当前成交逐笔归因：按买点族和板块强弱",
        "",
        _md_table(by_family, pct_cols=pct_cols),
        "",
        "## G2 组合级重跑：板块层是否带来增益",
        "",
        _md_table(sector_matrix, pct_cols=pct_cols),
        "",
        "## G2 突破族独立矩阵：阈值稳健性",
        "",
        _md_table(breakout_matrix, pct_cols=pct_cols),
        "",
        "## G3 strong_v2 迁移测试摘要",
        "",
        _md_table(g3_eval, pct_cols=pct_cols),
        "",
        "## G3 strong_v2 分窗口摘要",
        "",
        _md_table(g3_window, pct_cols=pct_cols),
        "",
        "## 结论",
        "",
        "1. 这个思路对 G2 是有收益贡献的，但不是“越强越好”。`l3_s3 >= 0.05` 这种温和扩散确认，在组合级矩阵里提升了全样本和 2026 盲测；提高到 `0.10` 后收益明显塌陷，说明它适合作为方向确认/排序加分，不适合机械追最热板块。",
        "2. 当前正式 G2 已经吸收了这条原则：突破族要求板块强，volume5 给板块强度排序加分。当前正式组合 `full` 收益 331.32%、最大回撤 -12.94%，`blind_2026ytd` 收益 72.51%、最大回撤 -4.84%。",
        "3. G3 strong_v2 的结果说明“主升/弱修复路由 + 强势候选”能带来进攻性，但单独要求 `sector_confirmed` 并不总是更好。它更像候选源质量层，而不是独立万能过滤器。",
        "4. 可迁移原则应是：先由市场环境决定是否开强势仓，再在同一环境内优先选择有板块扩散确认的个股；弱势/震荡环境不能照搬强者恒强，需要 range/panic 另一路处理。",
        "",
        "## 建议落地方式",
        "",
        "- G2：保留当前 `sector_strong_threshold=0.05`，不要上调到 0.10；后续只做影子验证 `l3_s3` 连续加权排序，不做硬过滤扩张。",
        "- G3：把板块方向作为 strong route 的确认层和仓位层，而不是全局买入 gate；主升给正常仓，弱修复给小权重，防守/失败环境空仓或交给其他路线。",
        "- 下一轮验证应补一个 walk-forward：训练段只选阈值和用法，验证/盲测固定不动；同时加入滑点、涨跌停、容量和确认 bar 可见性审计。",
        "",
    ]
    (OUT / "mainline_overlay_validation_report_cn.md").write_text("\n".join(lines), encoding="utf-8")

    payload = {
        "out": str(OUT),
        "g2_lots": int(len(lots)),
        "files": {
            "report": str(OUT / "mainline_overlay_validation_report_cn.md"),
            "g2_lots": str(OUT / "g2_current_official_lot_context.csv"),
            "g2_sector_matrix": str(OUT / "g2_sector_integration_compact.csv"),
            "g3_eval": str(OUT / "g3_strong_v2_eval_compact.csv"),
        },
    }
    (OUT / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return payload


if __name__ == "__main__":
    run()
