from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.gen3_build_four_path_candidates import (  # noqa: E402
    _add_index_features,
    _add_stock_features,
    _build_market_context,
    _json_default,
    _load_index_daily,
    _load_stock_daily,
    _load_trade_dates,
    _with_entry_date,
)


DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen3_panic_v2_research"


def _rank_daily(d: pd.DataFrame, variant: str, top_n: int) -> pd.DataFrame:
    if d.empty:
        return d
    out = d.copy()
    out["panic_variant"] = variant
    out["g3_chain"] = variant
    out["entry_weight_hint"] = 0.20
    out["candidate_score"] = (
        0.30 * (-out["drawdown10"]).rank(pct=True)
        + 0.20 * (-out["drawdown20"]).rank(pct=True)
        + 0.20 * out["lower_shadow_ratio"].rank(pct=True)
        + 0.15 * out["close_position"].rank(pct=True)
        + 0.15 * out["amount_ratio20"].clip(0, 5).rank(pct=True)
    )
    out = out.sort_values(["trade_date", "candidate_score", "amount20"], ascending=[True, False, False])
    out["chain_rank"] = out.groupby("trade_date").cumcount() + 1
    return out[out["chain_rank"] <= int(top_n)].copy()


def _select_variants(features: pd.DataFrame, market_context: pd.DataFrame, top_n: int, min_amount20: float) -> pd.DataFrame:
    ctx_cols = [
        "trade_date",
        "market_style",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "breadth_ma20",
        "breadth_ma60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "adx20",
        "plus_di20",
        "minus_di20",
        "mom20",
    ]
    d = features.merge(market_context[ctx_cols].rename(columns={"mom20": "index_mom20"}), on="trade_date", how="left")
    d = d[(d["amount20"] >= float(min_amount20)) & (d["close"] > 0)].copy()

    panic_context = (
        d["market_style"].isin(["standard_downtrend", "standard_range"])
        & ((d["up_rate"] <= 0.35) | (d["big_down_rate"] >= 0.12) | (d["limit_down_proxy_rate"] >= 0.01))
    )
    tradable = (d["ret1"] > -0.095) & (d["close"] > 2.0) & (d["amount_ratio20"].between(0.7, 5.0))

    v1_reference = d[
        (d["market_style"] == "standard_downtrend")
        & ((d["up_rate"] <= 0.25) | (d["big_down_rate"] >= 0.18) | (d["limit_down_proxy_rate"] >= 0.015))
        & ((d["drawdown10"] <= -0.12) | (d["drawdown20"] <= -0.20))
        & (d["close_position"] >= 0.35)
        & (d["lower_shadow_ratio"] >= 0.25)
        & tradable
        & (d["range_pos60"] <= 0.45)
    ].copy()

    icepoint_reclaim = d[
        panic_context
        & (d["up_rate"] <= 0.25)
        & ((d["drawdown10"] <= -0.10) | (d["drawdown20"] <= -0.18))
        & (d["close_position"] >= 0.45)
        & (d["lower_shadow_ratio"] >= 0.25)
        & tradable
        & (d["range_pos60"] <= 0.55)
    ].copy()

    deep_wash_repair = d[
        panic_context
        & (d["big_down_rate"] >= 0.10)
        & (d["drawdown20"] <= -0.20)
        & (d["close_position"] >= 0.55)
        & (d["amount_ratio20"].between(1.0, 5.0))
        & (d["ret1"].between(-0.09, 0.05))
        & (d["range_pos60"] <= 0.50)
    ].copy()

    no_limit_capitulation = d[
        (d["market_style"] == "standard_downtrend")
        & ((d["up_rate"] <= 0.30) | (d["big_down_rate"] >= 0.15))
        & (d["drawdown10"] <= -0.12)
        & (d["gap_open"] >= -0.06)
        & (d["lower_shadow_ratio"] >= 0.35)
        & (d["close_position"] >= 0.50)
        & tradable
        & (d["range_pos60"] <= 0.50)
    ].copy()

    broad_panic_probe = d[
        panic_context
        & ((d["drawdown5"] <= -0.07) | (d["drawdown10"] <= -0.12) | (d["drawdown20"] <= -0.18))
        & (d["lower_shadow_ratio"] >= 0.20)
        & (d["close_position"] >= 0.35)
        & tradable
        & (d["range_pos60"] <= 0.60)
    ].copy()

    parts = [
        _rank_daily(v1_reference, "panic_v1_reference", top_n),
        _rank_daily(icepoint_reclaim, "panic_v2_icepoint_reclaim", top_n),
        _rank_daily(deep_wash_repair, "panic_v2_deep_wash_repair", top_n),
        _rank_daily(no_limit_capitulation, "panic_v2_no_limit_capitulation", top_n),
        _rank_daily(broad_panic_probe, "panic_v2_broad_probe", top_n),
    ]
    return pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(not p.empty for p in parts) else pd.DataFrame()


def _write_report(output_dir: Path, summary: dict[str, Any], candidates: pd.DataFrame) -> None:
    lines = [
        "# G3 Panic V2 候选源研究",
        "",
        "## 定位",
        "",
        "- 仅研究弱势/下跌环境里的恐慌出清，不接入 G2/G3 正式入口。",
        "- V2 不是收益调参，而是从市场冰点、深洗、非跌停可交易、下影修复几个可解释方向扩展样本。",
        "- 输出仍需后续前瞻标签、30m 口径校准验证和组合回测。",
        "",
        "## 候选数量",
        "",
        "| 变体 | 候选数 | 覆盖交易日 | 平均每日候选 |",
        "| --- | ---: | ---: | ---: |",
    ]
    if candidates.empty:
        lines.append("| 空 | 0 | 0 | 0 |")
    else:
        for variant, group in candidates.groupby("panic_variant", sort=True):
            days = int(group["trade_date"].nunique())
            rows = int(len(group))
            lines.append(f"| {variant} | {rows} | {days} | {rows / days if days else 0:.2f} |")
    lines.extend(
        [
            "",
            "## 防过拟合约束",
            "",
            "- 先看 train/valid/blind/full 的方向一致性，不按单段收益继续细化参数。",
            "- 样本少于 100 条的变体只能当观察，不作为正式候选。",
            "- 后续分钟验证必须校准日线/分钟价格口径。",
            "",
        ]
    )
    output_dir.joinpath("README_CN.md").write_text("\n".join(lines), encoding="utf-8")


def build(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trade_dates = _load_trade_dates(args.start_date, args.end_date)
    stocks = _load_stock_daily(args.start_date, args.end_date, max_codes=int(args.max_codes or 0))
    features = _add_stock_features(stocks)
    index = _load_index_daily(args.start_date, args.end_date)
    market_context = _build_market_context(features, _add_index_features(index), min_amount20=float(args.min_amount20))
    candidates = _select_variants(features, market_context, top_n=int(args.top_n), min_amount20=float(args.min_amount20))
    candidates = _with_entry_date(candidates, trade_dates)
    candidates = candidates[(candidates["entry_date"] >= args.start_date) & (candidates["entry_date"] <= args.end_date)].copy()
    keep_cols = [
        "trade_date",
        "entry_date",
        "code",
        "name",
        "g3_chain",
        "panic_variant",
        "market_style",
        "chain_rank",
        "candidate_score",
        "entry_weight_hint",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "breadth_ma20",
        "breadth_ma60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "adx20",
        "plus_di20",
        "minus_di20",
        "index_mom20",
        "open",
        "high",
        "low",
        "close",
        "ret1",
        "mom5",
        "mom10",
        "mom20",
        "drawdown5",
        "drawdown10",
        "drawdown20",
        "runup_from_60d_low",
        "range_pos20",
        "range_pos60",
        "amount",
        "amount20",
        "amount_ratio5",
        "amount_ratio20",
        "turnover_rate",
        "lower_shadow_ratio",
        "close_position",
        "gap_open",
    ]
    candidates = candidates[[c for c in keep_cols if c in candidates.columns]].sort_values(
        ["entry_date", "panic_variant", "chain_rank", "code"]
    )
    candidates.to_parquet(output_dir / "panic_v2_candidates.parquet", index=False)
    candidates.to_csv(output_dir / "panic_v2_candidates.csv", index=False, encoding="utf-8-sig")
    market_context.to_csv(output_dir / "market_context.csv", index=False, encoding="utf-8-sig")
    summary = {
        "version": "gen3_panic_v2_research",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "output_dir": str(output_dir),
        "candidates_path": str(output_dir / "panic_v2_candidates.parquet"),
        "stock_rows": int(len(stocks)),
        "stock_count": int(stocks["code"].nunique()),
        "candidate_rows": int(len(candidates)),
        "top_n_per_variant_per_day": int(args.top_n),
        "min_amount20": float(args.min_amount20),
        "max_codes": int(args.max_codes or 0),
        "g2_runtime_touched": False,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, summary, candidates)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Research G3 panic V2 candidate variants.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-05-29")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--min-amount20", type=float, default=30000.0)
    parser.add_argument("--max-codes", type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(build(args), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
