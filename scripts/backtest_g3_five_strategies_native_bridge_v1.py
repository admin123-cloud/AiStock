from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.analyze_g3_native_source_bridge_recall_v1 import _build_native_bridge_candidates  # noqa: E402
from scripts.backtest_g3_five_strategies_from_scratch_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    STRATEGY_PRIORITY,
    _group_metrics,
    _md_table,
    _money,
    _pct,
    _pick_daily,
    _simulate_portfolio,
    _simulate_trade,
)
from scripts.gen3_build_four_path_candidates import (  # noqa: E402
    _add_index_features,
    _add_stock_features,
    _build_market_context,
    _load_index_daily,
    _load_stock_daily,
    _load_trade_dates,
)
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_five_strategies_native_bridge_v1")


def _sector_proxy(code: Any) -> str:
    text = str(code or "")
    if text.startswith("688"):
        return "科创板"
    if text.startswith("300"):
        return "创业板"
    if text.startswith("60"):
        return "沪市主板"
    if text.startswith("00"):
        return "深市主板"
    return text[:3]


def _enrich_candidates(candidates: pd.DataFrame, features: pd.DataFrame, ctx: pd.DataFrame, min_amount20: float) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    out = candidates.copy()
    out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    feature_cols = [
        "code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "amount",
        "amount20",
        "amount_ratio20",
        "mom5",
        "mom10",
        "mom20",
        "range_pos60",
        "drawdown20",
        "close_position",
        "lower_shadow_ratio",
    ]
    available_feature_cols = [c for c in feature_cols if c in features.columns]
    enriched = out.merge(
        features[available_feature_cols].rename(columns={"trade_date": "entry_date"}),
        on=["code", "entry_date"],
        how="left",
    )
    ctx_cols = [
        "trade_date",
        "market_style",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "breadth_ma20",
        "breadth_ma60",
        "mom20",
        "mom60",
        "market_amount_ratio20",
    ]
    available_ctx_cols = [c for c in ctx_cols if c in ctx.columns]
    enriched = enriched.merge(
        ctx[available_ctx_cols].rename(columns={"trade_date": "entry_date", "mom20": "index_mom20", "mom60": "index_mom60"}),
        on="entry_date",
        how="left",
    )
    enriched["sector_proxy"] = enriched["code"].map(_sector_proxy)
    enriched["amount20"] = pd.to_numeric(enriched.get("amount20"), errors="coerce").fillna(0.0)
    enriched["strategy_score"] = pd.to_numeric(enriched.get("strategy_score"), errors="coerce").fillna(0.0)
    enriched["strategy_priority"] = enriched["trade_strategy"].map(STRATEGY_PRIORITY).fillna(99).astype(int)
    enriched["strategy_rank"] = (
        enriched.sort_values(["entry_date", "trade_strategy", "strategy_score"], ascending=[True, True, False])
        .groupby(["entry_date", "trade_strategy"])
        .cumcount()
        + 1
    )
    enriched = enriched[~enriched["code"].astype(str).str.endswith(".BJ")].copy()
    enriched = enriched[pd.to_numeric(enriched["amount20"], errors="coerce").fillna(0.0) >= float(min_amount20)].copy()
    return enriched.sort_values(["entry_date", "strategy_priority", "strategy_score"], ascending=[True, True, False]).reset_index(drop=True)


def _metrics(df: pd.DataFrame, curve: pd.DataFrame) -> dict[str, Any]:
    final_equity = float(curve["equity"].iloc[-1]) if not curve.empty else INITIAL_CAPITAL
    max_dd = float(curve["drawdown"].min()) if not curve.empty else 0.0
    ret = pd.to_numeric(df.get("net_ret"), errors="coerce") if not df.empty else pd.Series(dtype="float64")
    return {
        "closed_trades": int(len(df)),
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_ret": float(ret.mean()) if len(ret) else None,
        "final_equity": final_equity,
        "total_return": final_equity / INITIAL_CAPITAL - 1.0,
        "max_drawdown": max_dd,
        "sum_pnl": float(pd.to_numeric(df.get("realized_pnl"), errors="coerce").fillna(0).sum()) if not df.empty else 0.0,
    }


def _write_report(
    meta: dict[str, Any],
    strategy_metrics: pd.DataFrame,
    source_metrics: pd.DataFrame,
    exit_metrics: pd.DataFrame,
    scenario_matrix: pd.DataFrame,
) -> None:
    report = [
        "# G3 五策略原生源桥接统一回测 v1",
        "",
        "## 口径",
        "",
        "- 策略主体仍然只有 5 个：机构主升Score120、强势突破、恐慌出清修复、震荡弱势修复、量能续强补位。",
        "- 候选买点来自现存原生信号源桥接表；只使用 code、entry_date、strategy_score、source_strategy_label 等买点字段。",
        "- 原生源文件中的 policy_exit_date、net_ret、realized_pnl 不作为本次回测收益输入；本次统一使用五策略卖出合同重新模拟。",
        "- 默认排除 `Range V3弱势低吸原生源`，因为它不是 G3 最终版原盈利主体，且在统一卖出合同下形成主要回撤；可用 `--include-range-v3` 打开研究。",
        "- 当前仍是日线 OHLC 执行代理，尚未恢复全量 30m 盘中执行顺序，因此这是收益还原的桥接验证，不是最终实盘合同验收。",
        "",
        "## 总览",
        "",
        f"- 原生桥接候选：{meta['raw_bridge_candidates']}",
        f"- 默认准入候选：{meta['admitted_bridge_candidates']}",
        f"- 补充行情后候选：{meta['candidate_rows']}",
        f"- 每日二槽选票：{meta['selected_rows']}",
        f"- 已关闭交易：{meta['closed_trades']}",
        f"- 总收益：{_pct(meta['total_return'])}",
        f"- 最大回撤：{_pct(meta['max_drawdown'])}",
        f"- 最终权益：{_money(meta['final_equity'])}",
        "",
        "## 策略表现",
        "",
        _md_table(
            strategy_metrics,
            {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct"},
            {"sum_pnl"},
        ),
        "",
        "## 原生来源表现",
        "",
        _md_table(
            source_metrics,
            {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct"},
            {"sum_pnl"},
        ),
        "",
        "## 退出原因",
        "",
        _md_table(
            exit_metrics,
            {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct"},
            {"sum_pnl"},
        ),
        "",
        "## 场景矩阵",
        "",
        _md_table(
            scenario_matrix,
            {"win_rate", "avg_ret", "total_return", "max_drawdown"},
            {"sum_pnl"},
        ),
        "",
        "## 判断",
        "",
        "原生源桥接的目标不是美化历史结果，而是验证：在策略名称减少为 5 个之后，原来赚钱的买点能否继续进入统一候选和统一路由。若本回测明显优于日线代理五策略，说明收益还原方向应是恢复原生买点生成器，而不是继续放宽日线代理规则。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")


def _simulate_candidate_set(name: str, candidates: pd.DataFrame, daily_by_code: dict[str, pd.DataFrame], calendar: list[pd.Timestamp], slots: int, slot_pct: float) -> dict[str, Any]:
    selected = _pick_daily(candidates, slots=slots) if not candidates.empty else pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for row in selected.to_dict("records"):
        item = _simulate_trade(pd.Series(row), daily_by_code, calendar)
        if item is not None:
            rows.append(item)
    events = pd.DataFrame(rows)
    curve, closed = _simulate_portfolio(events, calendar, slots=slots, slot_pct=slot_pct) if not events.empty else (pd.DataFrame(), pd.DataFrame())
    ret = pd.to_numeric(closed.get("net_ret"), errors="coerce") if not closed.empty else pd.Series(dtype="float64")
    final_equity = float(curve["equity"].iloc[-1]) if not curve.empty else INITIAL_CAPITAL
    max_dd = float(curve["drawdown"].min()) if not curve.empty else 0.0
    return {
        "scenario": name,
        "candidates": int(len(candidates)),
        "selected": int(len(selected)),
        "closed": int(len(closed)),
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_ret": float(ret.mean()) if len(ret) else None,
        "total_return": final_equity / INITIAL_CAPITAL - 1.0,
        "max_drawdown": max_dd,
        "sum_pnl": float(pd.to_numeric(closed.get("realized_pnl"), errors="coerce").fillna(0).sum()) if not closed.empty else 0.0,
    }


def _build_scenario_matrix(candidates_all: pd.DataFrame, daily_by_code: dict[str, pd.DataFrame], calendar: list[pd.Timestamp], slots: int, slot_pct: float) -> pd.DataFrame:
    if candidates_all.empty:
        return pd.DataFrame()
    labels = candidates_all.get("source_strategy_label", pd.Series("", index=candidates_all.index)).fillna("").astype(str)
    scenarios = [
        ("all_native_sources", candidates_all),
        ("default_exclude_range_v3", candidates_all[~labels.str.contains("Range V3", na=False, regex=False)]),
        ("exclude_range_weak_strategy", candidates_all[candidates_all["trade_strategy"] != "range_weak_repair"]),
        ("offense_plus_panic_no_range", candidates_all[candidates_all["trade_strategy"].isin(["institutional_score120_mainwave", "old_g3_strong_breakout", "volume_runup_supplement", "panic_capitulation_repair"])]),
        ("exclude_all_repair", candidates_all[~candidates_all["trade_strategy"].isin(["range_weak_repair", "panic_capitulation_repair"])]),
    ]
    rows = [_simulate_candidate_set(name, df.copy(), daily_by_code, calendar, slots, slot_pct) for name, df in scenarios]
    return pd.DataFrame(rows).sort_values("total_return", ascending=False).reset_index(drop=True)


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trade_dates = _load_trade_dates(args.start_date, args.end_date)
    stocks = _load_stock_daily(args.start_date, args.end_date, max_codes=int(args.max_codes or 0))
    index = _load_index_daily(args.start_date, args.end_date)
    features = _add_stock_features(stocks)
    index_features = _add_index_features(index)
    ctx = _build_market_context(features, index_features, min_amount20=float(args.min_amount20))

    raw_candidates_all = _build_native_bridge_candidates()
    raw_candidates_all = raw_candidates_all[
        pd.to_datetime(raw_candidates_all["entry_date"], errors="coerce").between(pd.Timestamp(args.start_date), pd.Timestamp(args.end_date))
    ].copy()
    raw_candidates = raw_candidates_all.copy()
    if not bool(args.include_range_v3):
        source_labels = raw_candidates.get("source_strategy_label", pd.Series("", index=raw_candidates.index)).fillna("").astype(str)
        raw_candidates = raw_candidates[~source_labels.str.contains("Range V3", na=False, regex=False)].copy()
    candidates = _enrich_candidates(raw_candidates, features, ctx, min_amount20=float(args.min_amount20))
    if int(args.top_n or 0) > 0 and not candidates.empty:
        candidates = candidates[candidates["strategy_rank"] <= int(args.top_n)].copy()
    selected = _pick_daily(candidates, slots=int(args.slots)) if not candidates.empty else pd.DataFrame()

    daily_for_exit = stocks.copy()
    daily_for_exit["trade_date_ts"] = pd.to_datetime(daily_for_exit["trade_date"], errors="coerce").dt.normalize()
    daily_by_code = {code: g.sort_values("trade_date_ts").copy() for code, g in daily_for_exit.groupby("code", sort=False)}
    calendar = [pd.Timestamp(x).normalize() for x in trade_dates if pd.Timestamp(args.start_date) <= pd.Timestamp(x) <= pd.Timestamp(args.end_date)]
    sim_rows: list[dict[str, Any]] = []
    for row in selected.to_dict("records"):
        item = _simulate_trade(pd.Series(row), daily_by_code, calendar)
        if item is not None:
            sim_rows.append(item)
    trade_events = pd.DataFrame(sim_rows)
    curve, closed = _simulate_portfolio(trade_events, calendar, slots=int(args.slots), slot_pct=float(args.slot_pct)) if not trade_events.empty else (pd.DataFrame(), pd.DataFrame())

    candidates.to_csv(OUT_DIR / "all_strategy_candidates.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(OUT_DIR / "selected_daily_candidates.csv", index=False, encoding="utf-8-sig")
    trade_events.to_csv(OUT_DIR / "simulated_trade_events.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(OUT_DIR / "closed_trades.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(OUT_DIR / "equity_curve.csv", index=False, encoding="utf-8-sig")
    ctx.to_csv(OUT_DIR / "market_context.csv", index=False, encoding="utf-8-sig")

    strategy_metrics = _group_metrics(closed, ["trade_strategy", "trade_strategy_label"]) if not closed.empty else pd.DataFrame()
    source_metrics = _group_metrics(closed, ["trade_strategy_label", "source_strategy_label"]) if not closed.empty else pd.DataFrame()
    exit_metrics = _group_metrics(closed, ["trade_strategy_label", "exit_reason"]) if not closed.empty else pd.DataFrame()
    candidates_all_for_scenario = _enrich_candidates(raw_candidates_all, features, ctx, min_amount20=float(args.min_amount20))
    if int(args.top_n or 0) > 0 and not candidates_all_for_scenario.empty:
        candidates_all_for_scenario = candidates_all_for_scenario[candidates_all_for_scenario["strategy_rank"] <= int(args.top_n)].copy()
    scenario_matrix = _build_scenario_matrix(candidates_all_for_scenario, daily_by_code, calendar, slots=int(args.slots), slot_pct=float(args.slot_pct))
    strategy_metrics.to_csv(OUT_DIR / "strategy_metrics.csv", index=False, encoding="utf-8-sig")
    source_metrics.to_csv(OUT_DIR / "source_metrics.csv", index=False, encoding="utf-8-sig")
    exit_metrics.to_csv(OUT_DIR / "exit_metrics.csv", index=False, encoding="utf-8-sig")
    scenario_matrix.to_csv(OUT_DIR / "scenario_matrix.csv", index=False, encoding="utf-8-sig")

    meta = {
        "version": "g3_five_strategies_native_bridge_v1",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "source": "native signal-source bridge candidates + clickhouse daily OHLC unified exit simulation",
        "raw_bridge_candidates": int(len(raw_candidates_all)),
        "admitted_bridge_candidates": int(len(raw_candidates)),
        "include_range_v3": bool(args.include_range_v3),
        "candidate_rows": int(len(candidates)),
        "selected_rows": int(len(selected)),
        **_metrics(closed, curve),
        "output_dir": str(OUT_DIR),
        "note": "Uses native source entry signals but ignores native closed-trade returns; exits are recomputed by the unified five-strategy daily-OHLC proxy contract.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(meta, strategy_metrics, source_metrics, exit_metrics, scenario_matrix)
    return meta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest G3 five strategies with native source bridge candidates.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-19")
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--min-amount20", type=float, default=30000.0)
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--slots", type=int, default=2)
    parser.add_argument("--slot-pct", type=float, default=0.50)
    parser.add_argument("--include-range-v3", action="store_true", help="Include Range V3 weak-low native source for research; excluded by default.")
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))
