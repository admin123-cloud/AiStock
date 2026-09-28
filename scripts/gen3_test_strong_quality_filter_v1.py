from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.gen3_build_dynamic_router_combo_v1 as router
from utils.paths import report_path


OUT_DIR = report_path("gen3_strong_quality_filter_v1")
STRONG_PLUSWEAK = report_path("gen3_strong_v2_independent_source_v1", "strong_v2_main_up_plus_weak04_hold5_closed_trades.csv")

INITIAL_CAPITAL = 150_000.0
ROUTE_DAILY_LIMIT = {"down_panic": 2, "range_gap": 1, "strong_main": 2}


VARIANTS = [
    {"variant": "base_plusweak", "filter": "none"},
    {"variant": "veto_l3_s3_ge50", "filter": "l3_s3_lt50_or_missing"},
    {"variant": "veto_l3_s3_ge50_and_runup_le30", "filter": "l3_s3_lt50_and_runup_gt30_or_missing"},
]


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _standardize_strong(filter_name: str) -> pd.DataFrame:
    d = pd.read_csv(STRONG_PLUSWEAK, low_memory=False)
    d["l3_s3"] = pd.to_numeric(d.get("l3_s3"), errors="coerce")
    d["runup_from_60d_low"] = pd.to_numeric(d.get("runup_from_60d_low"), errors="coerce")
    if filter_name == "l3_s3_lt50_or_missing":
        d = d[d["l3_s3"].lt(0.50) | d["l3_s3"].isna()].copy()
    elif filter_name == "l3_s3_lt50_and_runup_gt30_or_missing":
        d = d[(d["l3_s3"].lt(0.50) | d["l3_s3"].isna()) & (d["runup_from_60d_low"].gt(0.30) | d["runup_from_60d_low"].isna())].copy()
    elif filter_name != "none":
        raise ValueError(filter_name)

    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "strong_main"
    out["route_source"] = f"strong_plusweak_hold5_{filter_name}"
    out["route_priority"] = router.ROUTE_PRIORITY["strong_main"]
    out["score"] = pd.to_numeric(d.get("g3_strong_score", d.get("score_volume5", d.get("v4_score", 0.0))), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_price"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("net_ret"), errors="coerce")
    passthrough_cols = [
        "market_breadth",
        "g3_strong_score",
        "score_volume5",
        "runup_from_60d_low",
        "l3_s3",
        "l2_s3",
        "l3_rise",
        "l2_rt_rise_ratio",
        "v4_rank",
        "v4_score",
        "confirm_datetime",
        "source_family",
        "l2_sector_name",
        "l3_sector_name",
    ]
    for col in passthrough_cols:
        if col in d.columns:
            out[col] = d[col]
    return out


def _load_candidates(filter_name: str) -> pd.DataFrame:
    d = pd.concat([router.standardize_panic(), router.standardize_range(), _standardize_strong(filter_name)], ignore_index=True)
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"]).copy()
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


def _simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    old_limits = dict(router.ROUTE_DAILY_LIMIT)
    try:
        router.ROUTE_DAILY_LIMIT = dict(ROUTE_DAILY_LIMIT)
        return router.simulate(candidates, router.SOURCE_COST_BPS)
    finally:
        router.ROUTE_DAILY_LIMIT = old_limits


def _summary(curve: pd.DataFrame, closed: pd.DataFrame) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    recent = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp("2024-06-01"))].copy()
    return {
        "trade_count": int(len(closed)),
        "strong_trade_count": int((closed.get("route", pd.Series(dtype=str)).astype(str) == "strong_main").sum()) if not closed.empty else 0,
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": _max_drawdown(curve["equity"]),
        "recent_return": float(recent["equity"].iloc[-1] / recent["equity"].iloc[0] - 1.0) if not recent.empty else None,
        "recent_max_drawdown": _max_drawdown(recent["equity"]) if not recent.empty else None,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
    }


def _windows(curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    return router.summarize_windows(curve, closed)


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _write_report(summary_df: pd.DataFrame) -> None:
    lines = [
        "# G3 strong route 质量过滤对照 v1",
        "",
        "## 边界",
        "",
        "- 这是固定过滤验证，不接实盘。",
        "- 过滤来自上一轮分层审计的风险层，但必须同时看 train/valid/blind。",
        "- 没有按单只股票或单一年份调参。",
        "",
        "## 结果",
        "",
        _md_table(
            summary_df[
                [
                    "variant",
                    "total_return",
                    "max_drawdown",
                    "recent_return",
                    "recent_max_drawdown",
                    "trade_count",
                    "strong_trade_count",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "worst_open_mtm_ret",
                ]
            ],
            pct_cols={
                "total_return",
                "max_drawdown",
                "recent_return",
                "recent_max_drawdown",
                "win_rate",
                "avg_trade_return",
                "worst_trade",
                "worst_open_mtm_ret",
            },
        ),
        "",
        "## 判断",
        "",
        "- 如果剔除过热 L3 层改善 valid/recent 但损害 train/blind，需要继续找交叉条件。",
        "- 如果回撤下降且 blind 不受损，可以进入下一轮执行压力测试。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for spec in VARIANTS:
        candidates = _load_candidates(spec["filter"])
        curve, closed = _simulate(candidates)
        variant_dir = OUT_DIR / spec["variant"]
        variant_dir.mkdir(parents=True, exist_ok=True)
        candidates.to_csv(variant_dir / "candidates_standardized.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(variant_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(variant_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        _windows(curve, closed).to_csv(variant_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
        router.summarize_routes(closed).to_csv(variant_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
        row = _summary(curve, closed)
        row.update(spec)
        rows.append(row)
    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(OUT_DIR / "variant_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps({"status": "completed", "variants": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary_df)
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
