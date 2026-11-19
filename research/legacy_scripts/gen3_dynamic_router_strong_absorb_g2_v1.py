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


OUT_DIR = ROOT / "reports" / "gen3_dynamic_router_strong_absorb_g2_v1"
G2_ONLY = ROOT / "reports" / "gen3_g2_full_gap_attribution_v1" / "g2_only_trades.csv"
STRONG_MAINUP = (
    ROOT / "reports" / "gen3_strong_v2_independent_source_v1" / "strong_v2_main_up_only_hold5_closed_trades.csv"
)
STRONG_PLUSWEAK = (
    ROOT / "reports" / "gen3_strong_v2_independent_source_v1" / "strong_v2_main_up_plus_weak04_hold5_closed_trades.csv"
)


VARIANTS = [
    {
        "variant": "baseline_mainup_limit1",
        "strong_path": STRONG_MAINUP,
        "route_daily_limit": {"down_panic": 2, "range_gap": 1, "strong_main": 1},
        "note": "current G3 V3 dynamic router strong route",
    },
    {
        "variant": "plusweak_limit1",
        "strong_path": STRONG_PLUSWEAK,
        "route_daily_limit": {"down_panic": 2, "range_gap": 1, "strong_main": 1},
        "note": "expand strong route from main_up_only to main_up_plus_weak04, keep daily strong limit 1",
    },
    {
        "variant": "plusweak_limit2",
        "strong_path": STRONG_PLUSWEAK,
        "route_daily_limit": {"down_panic": 2, "range_gap": 1, "strong_main": 2},
        "note": "same source expansion, allow up to two strong trades when daily account slots permit",
    },
]


def _key(df: pd.DataFrame, date_col: str = "entry_date") -> pd.Series:
    date = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    return df["code"].astype(str) + "|" + date.astype(str)


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _num(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.{digits}f}"


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
                item[col] = _num(value, 4)
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _load_g2_only() -> pd.DataFrame:
    g2 = pd.read_csv(G2_ONLY, low_memory=False)
    g2["entry_date"] = pd.to_datetime(g2["entry_date"], errors="coerce").dt.normalize()
    g2 = g2[g2["entry_date"].ge(pd.Timestamp("2024-06-01"))].copy()
    g2["key"] = _key(g2)
    g2["pnl"] = pd.to_numeric(g2["pnl"], errors="coerce").fillna(0.0)
    g2["net_return"] = pd.to_numeric(g2["net_return"], errors="coerce")
    return g2


def _recent_window(curve: pd.DataFrame, start: str = "2024-06-01") -> dict[str, Any]:
    part = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp(start))].copy()
    if part.empty:
        return {"recent_return": None, "recent_max_drawdown": None}
    start_eq = float(part["equity"].iloc[0])
    end_eq = float(part["equity"].iloc[-1])
    return {
        "recent_return": end_eq / start_eq - 1.0 if start_eq > 0 else None,
        "recent_max_drawdown": _max_drawdown(part["equity"]),
    }


def _coverage(closed: pd.DataFrame, g2_only: pd.DataFrame) -> dict[str, Any]:
    if closed.empty or g2_only.empty:
        return {"g2_only_captured_count": 0, "g2_only_captured_pnl": 0.0, "g2_only_captured_pnl_share": 0.0}
    c = closed.copy()
    c["entry_date"] = pd.to_datetime(c["entry_date"], errors="coerce").dt.normalize()
    c["key"] = _key(c)
    covered = g2_only[g2_only["key"].isin(set(c["key"]))].copy()
    total = float(g2_only["pnl"].sum())
    pnl = float(covered["pnl"].sum())
    return {
        "g2_only_captured_count": int(len(covered)),
        "g2_only_captured_pnl": pnl,
        "g2_only_captured_pnl_share": pnl / total if total else 0.0,
        "g2_only_captured_best_return": float(covered["net_return"].max()) if not covered.empty else None,
    }


def _run_variant(spec: dict[str, Any], g2_only: pd.DataFrame) -> dict[str, Any]:
    router.STRONG_PATH = Path(spec["strong_path"])
    router.ROUTE_DAILY_LIMIT = dict(spec["route_daily_limit"])
    candidates = router.load_candidates()
    curve, closed = router.simulate(candidates, router.SOURCE_COST_BPS)
    summary = router.summarize(curve, closed, router.SOURCE_COST_BPS)
    summary.update(_recent_window(curve))
    summary.update(_coverage(closed, g2_only))
    summary["variant"] = spec["variant"]
    summary["note"] = spec["note"]
    summary["strong_path"] = str(spec["strong_path"])
    summary["route_daily_limit"] = spec["route_daily_limit"]

    variant_dir = OUT_DIR / spec["variant"]
    variant_dir.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(variant_dir / "candidates_standardized.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(variant_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(variant_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
    router.summarize_windows(curve, closed).to_csv(variant_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
    router.summarize_routes(closed).to_csv(variant_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
    return summary


def _write_report(summary_df: pd.DataFrame) -> None:
    lines = [
        "# G3 强势吸收 G2 思路对照实验 v1",
        "",
        "## 实验边界",
        "",
        "- 这是研究对照，不接入实盘，不替换当前 G3 V3 页面默认结果。",
        "- 只测试强势链路的候选覆盖和每日额度，弱势 panic / range 链路保持原样。",
        "- 不按个股大赢家调参；三组参数在运行前固定，用于验证强势链路是否被过度压窄。",
        "",
        "## 核心结果",
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
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "g2_only_captured_count",
                    "g2_only_captured_pnl_share",
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
                "g2_only_captured_pnl_share",
            },
        ),
        "",
        "## 判断",
        "",
        "- 如果 `plusweak_limit1/2` 明显改善收益但回撤同步恶化，需要继续做 strong 专属退出器，而不是只放大仓位。",
        "- 如果能覆盖更多 G2-only 强势交易，但收益仍显著落后 G2 full，说明主要差距在 30m 分批止盈和移动退出，而不是候选源。",
        "- 下一步应该迁移 G2 full 的强势执行引擎，作为 G3 strong route 的独立卖法；弱势和震荡链路继续保持保守卖法。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g2_only = _load_g2_only()
    summaries = [_run_variant(spec, g2_only) for spec in VARIANTS]
    summary_df = pd.DataFrame(summaries)
    summary_df.to_csv(OUT_DIR / "variant_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(
        json.dumps({"status": "completed", "variants": summaries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_report(summary_df)
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
