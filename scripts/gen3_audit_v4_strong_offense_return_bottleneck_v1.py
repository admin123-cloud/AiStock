from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_v4_strong_position_scale_v1 import md_table  # noqa: E402
from utils.paths import report_path  # noqa: E402

PKG = report_path("gen3_v4_strong_offense_candidate_v1")
OUT_DIR = report_path("gen3_v4_strong_offense_return_bottleneck_v1")
INITIAL_CAPITAL = 150_000.0


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _cagr(start_equity: float, end_equity: float, start: pd.Timestamp, end: pd.Timestamp) -> float:
    years = max((end - start).days / 365.25, 1e-9)
    return float((end_equity / start_equity) ** (1.0 / years) - 1.0)


def _load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    closed = pd.read_csv(PKG / "g3_v4_strong_offense_closed_trades.csv", low_memory=False)
    curve = pd.read_csv(PKG / "g3_v4_strong_offense_equity_curve.csv", low_memory=False)
    annual = pd.read_csv(PKG / "g3_v4_strong_offense_annual.csv", low_memory=False)
    compare = pd.read_csv(PKG / "g3_v4_strong_offense_compare_summary.csv", low_memory=False)
    closed["entry_date"] = pd.to_datetime(closed["entry_date"], errors="coerce").dt.normalize()
    closed["policy_exit_date"] = pd.to_datetime(closed["policy_exit_date"], errors="coerce").dt.normalize()
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce").dt.normalize()
    return closed, curve, annual, compare


def _overall(curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    start = pd.Timestamp(curve["date"].min())
    end = pd.Timestamp(curve["date"].max())
    end_equity = float(curve["equity"].iloc[-1])
    invested_ratio = pd.to_numeric(curve["reserved_principal"], errors="coerce").fillna(0.0) / pd.to_numeric(
        curve["equity"], errors="coerce"
    ).replace(0, pd.NA)
    open_positions = pd.to_numeric(curve["open_positions"], errors="coerce").fillna(0)
    rows = [
        {
            "start": start.strftime("%Y-%m-%d"),
            "end": end.strftime("%Y-%m-%d"),
            "years": (end - start).days / 365.25,
            "total_return": end_equity / INITIAL_CAPITAL - 1.0,
            "cagr": _cagr(INITIAL_CAPITAL, end_equity, start, end),
            "max_drawdown": _max_drawdown(pd.to_numeric(curve["equity"], errors="coerce")),
            "trade_count": int(len(closed)),
            "trades_per_year": float(len(closed) / max((end - start).days / 365.25, 1e-9)),
            "avg_invested_ratio": float(invested_ratio.mean()),
            "median_invested_ratio": float(invested_ratio.median()),
            "days_no_position_rate": float(open_positions.eq(0).mean()),
            "days_full_2plus_positions_rate": float(open_positions.ge(2).mean()),
            "avg_open_positions": float(open_positions.mean()),
        }
    ]
    return pd.DataFrame(rows)


def _route_quality(closed: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total_pnl = float(pd.to_numeric(closed["realized_pnl"], errors="coerce").fillna(0).sum())
    for route, g in closed.groupby("route", dropna=False):
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        pnl = pd.to_numeric(g["realized_pnl"], errors="coerce").fillna(0)
        rows.append(
            {
                "route": route,
                "trade_count": int(len(g)),
                "pnl": float(pnl.sum()),
                "pnl_share": float(pnl.sum() / total_pnl) if total_pnl else 0.0,
                "win_rate": float(ret.gt(0).mean()) if len(ret) else 0.0,
                "avg_return": float(ret.mean()) if len(ret) else 0.0,
                "median_return": float(ret.median()) if len(ret) else 0.0,
                "worst_return": float(ret.min()) if len(ret) else 0.0,
                "best_return": float(ret.max()) if len(ret) else 0.0,
                "bad5_rate": float(ret.le(-0.05).mean()) if len(ret) else 0.0,
                "big10_rate": float(ret.ge(0.10).mean()) if len(ret) else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values("pnl", ascending=False)


def _concentration(closed: pd.DataFrame) -> pd.DataFrame:
    d = closed.copy()
    d["realized_pnl"] = pd.to_numeric(d["realized_pnl"], errors="coerce").fillna(0)
    total = float(d["realized_pnl"].sum())
    winners = d.sort_values("realized_pnl", ascending=False).copy()
    losers = d.sort_values("realized_pnl").copy()
    rows = []
    for n in [3, 5, 10, 20]:
        rows.append(
            {
                "bucket": f"top_{n}_winners",
                "rows": n,
                "pnl": float(winners.head(n)["realized_pnl"].sum()),
                "pnl_share": float(winners.head(n)["realized_pnl"].sum() / total) if total else 0.0,
            }
        )
        rows.append(
            {
                "bucket": f"top_{n}_losers",
                "rows": n,
                "pnl": float(losers.head(n)["realized_pnl"].sum()),
                "pnl_share": float(losers.head(n)["realized_pnl"].sum() / total) if total else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _idle_gaps(curve: pd.DataFrame) -> pd.DataFrame:
    d = curve[["date", "open_positions"]].copy()
    d["idle"] = pd.to_numeric(d["open_positions"], errors="coerce").fillna(0).eq(0)
    groups = []
    start = None
    prev = None
    for row in d.itertuples(index=False):
        if row.idle and start is None:
            start = row.date
        if not row.idle and start is not None:
            groups.append({"start": start, "end": prev, "calendar_days": int((prev - start).days + 1)})
            start = None
        prev = row.date
    if start is not None and prev is not None:
        groups.append({"start": start, "end": prev, "calendar_days": int((prev - start).days + 1)})
    out = pd.DataFrame(groups)
    if out.empty:
        return out
    out["start"] = pd.to_datetime(out["start"]).dt.strftime("%Y-%m-%d")
    out["end"] = pd.to_datetime(out["end"]).dt.strftime("%Y-%m-%d")
    return out.sort_values("calendar_days", ascending=False).head(20)


def _annual_bottleneck(annual: pd.DataFrame) -> pd.DataFrame:
    d = annual[annual["profile"].astype(str).eq("cost30")].copy()
    d["return"] = pd.to_numeric(d["return"], errors="coerce")
    d["trade_count"] = pd.to_numeric(d["trade_count"], errors="coerce").fillna(0).astype(int)
    d["strong_trades"] = pd.to_numeric(d["strong_trades"], errors="coerce").fillna(0).astype(int)
    d["return_per_trade"] = d["return"] / d["trade_count"].replace(0, pd.NA)
    d["bottleneck"] = "ok"
    d.loc[d["trade_count"].le(7), "bottleneck"] = "low_trade_count"
    d.loc[d["return"].le(0.05), "bottleneck"] = "low_or_negative_return"
    return d[["year", "return", "max_drawdown", "trade_count", "strong_trades", "win_rate", "avg_trade_return", "return_per_trade", "bottleneck"]]


def _return_targets(overall: pd.DataFrame) -> pd.DataFrame:
    row = overall.iloc[0]
    years = float(row["years"])
    rows = []
    for target_cagr in [0.18, 0.25, 0.30, 0.40]:
        target_total = (1.0 + target_cagr) ** years - 1.0
        rows.append(
            {
                "target_cagr": target_cagr,
                "target_total_return": target_total,
                "current_total_return": float(row["total_return"]),
                "return_gap": target_total - float(row["total_return"]),
                "multiple_vs_current": target_total / float(row["total_return"]) if float(row["total_return"]) else None,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    closed, curve, annual, compare = _load()
    overall = _overall(curve, closed)
    route = _route_quality(closed)
    concentration = _concentration(closed)
    gaps = _idle_gaps(curve)
    annual_bottleneck = _annual_bottleneck(annual)
    targets = _return_targets(overall)

    top_winners = closed.sort_values("realized_pnl", ascending=False).head(30)
    top_losers = closed.sort_values("realized_pnl").head(30)

    overall.to_csv(OUT_DIR / "overall_return_bottleneck.csv", index=False, encoding="utf-8-sig")
    route.to_csv(OUT_DIR / "route_return_quality.csv", index=False, encoding="utf-8-sig")
    concentration.to_csv(OUT_DIR / "pnl_concentration.csv", index=False, encoding="utf-8-sig")
    gaps.to_csv(OUT_DIR / "idle_gaps.csv", index=False, encoding="utf-8-sig")
    annual_bottleneck.to_csv(OUT_DIR / "annual_return_bottleneck.csv", index=False, encoding="utf-8-sig")
    targets.to_csv(OUT_DIR / "return_target_gap.csv", index=False, encoding="utf-8-sig")
    top_winners.to_csv(OUT_DIR / "top_winners.csv", index=False, encoding="utf-8-sig")
    top_losers.to_csv(OUT_DIR / "top_losers.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "total_return",
        "cagr",
        "max_drawdown",
        "avg_invested_ratio",
        "median_invested_ratio",
        "days_no_position_rate",
        "days_full_2plus_positions_rate",
        "pnl_share",
        "win_rate",
        "avg_return",
        "median_return",
        "worst_return",
        "best_return",
        "bad5_rate",
        "big10_rate",
        "return",
        "avg_trade_return",
        "return_per_trade",
        "target_cagr",
        "target_total_return",
        "current_total_return",
        "return_gap",
    }
    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidate": "g3_v4_strong_offense_candidate_v1",
        "audit": "return_bottleneck_v1",
        "total_return": float(overall.iloc[0]["total_return"]),
        "cagr": float(overall.iloc[0]["cagr"]),
        "trade_count": int(overall.iloc[0]["trade_count"]),
        "trades_per_year": float(overall.iloc[0]["trades_per_year"]),
        "avg_invested_ratio": float(overall.iloc[0]["avg_invested_ratio"]),
        "days_no_position_rate": float(overall.iloc[0]["days_no_position_rate"]),
        "primary_bottleneck": "low_trade_frequency_and_low_capital_utilization",
        "secondary_bottleneck": "return_concentrated_in_few_big_winners_and_range_gap_tail_loss",
        "next_step": "test offensive capital utilization variants before further candidate-source engineering.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# G3 V4 强进攻收益瓶颈审计 V1",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        "## 核心判断",
        "",
        "你不满意收益是合理的：当前版本胜率和单笔质量不差，但资金长期闲置、交易频率太低，导致总收益看起来不够有进攻性。",
        "",
        "## 总览",
        "",
        md_table(overall, pct_cols=pct_cols),
        "",
        "## 路线收益质量",
        "",
        md_table(route, pct_cols=pct_cols),
        "",
        "## 年度瓶颈",
        "",
        md_table(annual_bottleneck, pct_cols=pct_cols),
        "",
        "## 收益集中度",
        "",
        md_table(concentration, pct_cols=pct_cols),
        "",
        "## 空仓最长区间",
        "",
        md_table(gaps.head(10), pct_cols=pct_cols),
        "",
        "## 目标收益差距",
        "",
        md_table(targets, pct_cols=pct_cols),
        "",
        "## 结论",
        "",
        "- 当前不是“完全没有赚钱能力”，而是进攻火力没有打满：年化、交易频率、资金占用都偏保守。",
        "- `range_gap` 单笔弹性最高，但尾部也最大；`strong_main` 质量最好，是提升收益最值得加码的路线；`down_panic` 笔数多但平均收益低，承担了太多低效交易。",
        "- 下一步应该先测试仓位/开仓限制/strong 权重的进攻版，而不是继续把精力放在候选源是否新鲜上。",
    ]
    (OUT_DIR / "g3_v4_strong_offense_return_bottleneck_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(overall.to_string(index=False))
    print(route.to_string(index=False))


if __name__ == "__main__":
    main()
