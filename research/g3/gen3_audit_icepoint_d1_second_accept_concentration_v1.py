from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table  # noqa: E402


SOURCE_DIR = _report_path() / "gen3_range_icepoint_d1_second_accept_overlay_v1"
CLOSED = SOURCE_DIR / "combo_e_plus_icepoint_d1_closed_trades.csv"
CANDIDATES = SOURCE_DIR / "combo_e_plus_icepoint_d1_candidates.csv"
OUT_DIR = _report_path() / "gen3_icepoint_d1_second_accept_concentration_audit_v1"

BACKTEST_START = "2020-01-01"
BACKTEST_END = "2026-05-29"
CURVE_END = "2026-06-04"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_overlay_trades() -> pd.DataFrame:
    closed = pd.read_csv(CLOSED, low_memory=False)
    candidates = pd.read_csv(CANDIDATES, low_memory=False)
    for d in [closed, candidates]:
        d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
        d["code"] = d["code"].astype(str)
    closed = closed[closed.get("source_group", "").astype(str).eq("icepoint_d1_overlay")].copy()
    keep_cols = [
        "entry_date",
        "code",
        "signal_date",
        "confirm_datetime",
        "d1_second_accept_datetime",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "gap_open",
        "index_mom20",
        "runup_from_60d_low",
        "ice_score",
        "limit_up_count",
        "limit_down_count",
        "source_desc",
    ]
    ctx = candidates[candidates.get("source_group", "").astype(str).eq("icepoint_d1_overlay")][
        [c for c in keep_cols if c in candidates.columns]
    ].copy()
    d = closed.merge(ctx, on=["entry_date", "code"], how="left", suffixes=("", "_ctx"))
    for col in [
        "policy_net_ret",
        "realized_pnl",
        "stake",
        "entry_price",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "gap_open",
        "index_mom20",
        "runup_from_60d_low",
        "ice_score",
        "limit_up_count",
        "limit_down_count",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    d["win"] = d["policy_net_ret"].gt(0)
    d["quality_note"] = d.apply(quality_note, axis=1)
    return d.sort_values(["entry_date", "code"]).reset_index(drop=True)


def quality_note(row: pd.Series) -> str:
    notes: list[str] = []
    if pd.notna(row.get("range_pos60")) and float(row["range_pos60"]) <= 0.03:
        notes.append("贴近60日箱体底部")
    if pd.notna(row.get("close_position")) and float(row["close_position"]) >= 0.65:
        notes.append("日线收盘修复较强")
    if pd.notna(row.get("amount_ratio3")) and float(row["amount_ratio3"]) >= 1.5:
        notes.append("30m承接量明显")
    if pd.notna(row.get("gap_open")) and float(row["gap_open"]) < -0.03:
        notes.append("入场日低开压力")
    if pd.notna(row.get("index_mom20")) and float(row["index_mom20"]) > 0:
        notes.append("指数20日动量已转正")
    return "；".join(notes) if notes else "无明显额外标签"


def exclusion_summary(d: pd.DataFrame) -> pd.DataFrame:
    total_pnl = float(d["realized_pnl"].sum())
    total_ret = float(d["policy_net_ret"].sum())
    rows = []
    ranked = d.sort_values("realized_pnl", ascending=False).reset_index(drop=True)
    for n in [0, 1, 2, 3, 5]:
        part = ranked.iloc[n:].copy()
        rows.append(
            {
                "exclude_top_n": n,
                "remaining_trades": int(len(part)),
                "remaining_pnl": float(part["realized_pnl"].sum()),
                "remaining_sum_trade_ret": float(part["policy_net_ret"].sum()),
                "remaining_avg_trade_ret": float(part["policy_net_ret"].mean()) if len(part) else 0.0,
                "remaining_win_rate": float(part["win"].mean()) if len(part) else 0.0,
                "removed_pnl_share": float((total_pnl - part["realized_pnl"].sum()) / total_pnl) if total_pnl else 0.0,
                "total_pnl": total_pnl,
                "total_sum_trade_ret": total_ret,
            }
        )
    return pd.DataFrame(rows)


def annual_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, g in d.groupby("year"):
        rows.append(
            {
                "year": int(year),
                "trade_count": int(len(g)),
                "pnl": float(g["realized_pnl"].sum()),
                "sum_trade_ret": float(g["policy_net_ret"].sum()),
                "avg_trade_ret": float(g["policy_net_ret"].mean()),
                "win_rate": float(g["win"].mean()),
                "best_trade": float(g["policy_net_ret"].max()),
                "worst_trade": float(g["policy_net_ret"].min()),
            }
        )
    return pd.DataFrame(rows).sort_values("year")


def trade_table(d: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "realized_pnl",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "gap_open",
        "index_mom20",
        "quality_note",
    ]
    return d[[c for c in cols if c in d.columns]].sort_values("realized_pnl", ascending=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = load_overlay_trades()
    excl = exclusion_summary(d)
    annual = annual_summary(d)
    trades = trade_table(d)
    d.to_csv(OUT_DIR / "icepoint_d1_overlay_trades_enriched.csv", index=False, encoding="utf-8-sig")
    excl.to_csv(OUT_DIR / "top_exclusion_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "trade_table.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "remaining_sum_trade_ret",
        "remaining_avg_trade_ret",
        "remaining_win_rate",
        "removed_pnl_share",
        "total_sum_trade_ret",
        "sum_trade_ret",
        "avg_trade_ret",
        "win_rate",
        "best_trade",
        "worst_trade",
        "policy_net_ret",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "gap_open",
        "index_mom20",
    }
    top3_removed = excl[excl["exclude_top_n"].eq(3)].iloc[0].to_dict()
    report = "\n".join(
        [
            "# G3 冰点+D1二次承接集中度审计 v1",
            "",
            "## 回测范围",
            f"- 候选信号/入场窗口：{BACKTEST_START} 至 {BACKTEST_END}。",
            f"- 资金曲线结算至：{CURVE_END}。",
            "- 审计对象：`icepoint_d1_second_accept`，中文意思是“冰点环境下，箱体底部日线修复后，D1再次出现30m放量承接”。",
            "- 这是 combo_e 叠加后的增量交易审计，只看 `source_group = icepoint_d1_overlay` 的 11 笔，不含原 combo_e 交易。",
            "",
            "## Top 剔除检验",
            md_table(excl, pct_cols=pct_cols),
            "",
            "## 年度贡献",
            md_table(annual, pct_cols=pct_cols),
            "",
            "## 逐笔交易",
            md_table(trades, pct_cols=pct_cols),
            "",
            "## 阶段判断",
            f"- 剔除收益最高的3笔后，剩余 {int(top3_removed['remaining_trades'])} 笔，剩余Pnl {top3_removed['remaining_pnl']:.2f}，胜率 {pct(top3_removed['remaining_win_rate'])}。",
            "- 如果 top3 剔除后仍为正，说明方向不是完全依赖单点；但如果剩余收益很薄，则只能作为研究标签。",
            "- 本轮不建议升级为正式买点；下一步应审计这些成功样本是否来自同一种可见结构，再决定是否加入页面展示。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text(report + "\n", encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
