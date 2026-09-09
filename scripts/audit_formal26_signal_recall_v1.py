"""Recall audit for a signal-date-only institutional-mainwave candidate engine.

The old formal 26 trades are a recall reference, never a training label.  This
script deliberately excludes learned-sector and historical scheduler fields.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.backtest_wave_style_template_strategy_v1 as base
from utils.paths import report_path


FORMAL = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OUT = report_path("formal26_signal_recall_v1")


def _key(frame: pd.DataFrame, code_col: str) -> pd.Series:
    return pd.to_datetime(frame["entry_date"]).dt.strftime("%Y-%m-%d") + "|" + frame[code_col].astype(str)


def main() -> int:
    parser = argparse.ArgumentParser(description="Signal-only candidate recall audit; creates no trades or orders.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-30")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    base.INDEX_CODE = "000001.SH"
    base.INDEX_FALLBACK_CODES = []
    ns = argparse.Namespace(start_date=args.start_date, end_date=args.end_date, min_score=88.0, strict_min_score=100.0, cost_bps=30.0)
    load_start = (pd.Timestamp(args.start_date) - pd.Timedelta(days=300)).strftime("%Y-%m-%d")
    load_end = (pd.Timestamp(args.end_date) + pd.Timedelta(days=90)).strftime("%Y-%m-%d")
    stocks = base._load_stocks()
    raw = base._load_daily(load_start, load_end).merge(stocks[["code_key", "stock_name", "industry"]], on="code_key", how="inner")
    features = base._attach_sector(base._add_features(raw, max_hold=20))
    index = base._load_index_features(load_start, load_end)
    if index.empty or set(index.get("index_code", pd.Series(dtype=str)).dropna()) - {"000001.SH"}:
        raise RuntimeError("canonical 000001.SH index features unavailable")
    features = features.merge(index, on="trade_date", how="left")
    formal = pd.read_csv(FORMAL, encoding="utf-8-sig")
    formal["trade_key"] = _key(formal, "code")
    formal_keys = set(formal["trade_key"])
    # Keep a diagnostic snapshot at every formal entry key.  It is not a
    # training table; it tells us which observable condition blocks recall.
    scored = base._score_candidates(features, use_learned_sector=False).copy()
    scored["entry_date"] = pd.to_datetime(scored["next_entry_date"], errors="coerce").dt.normalize()
    scored["trade_key"] = _key(scored.dropna(subset=["entry_date"]), "code_raw")
    diagnostic = formal[["trade_key", "entry_date", "code", "name", "wave_style_score", "sector_diffusion_score"]].merge(
        scored,
        on="trade_key",
        how="left",
        suffixes=("_formal", "_rebuilt"),
    )
    diagnostic["index_gate_pass"] = pd.to_numeric(diagnostic.get("index_mom60"), errors="coerce").le(0.05)
    diagnostic["shape88_score_pass"] = pd.to_numeric(diagnostic.get("wave_style_score_rebuilt"), errors="coerce").ge(88.0)
    diagnostic["shape100_score_pass"] = pd.to_numeric(diagnostic.get("wave_style_score_rebuilt"), errors="coerce").ge(100.0)
    diagnostic["amount_pass"] = pd.to_numeric(diagnostic.get("amount_rank"), errors="coerce").ge(0.70)
    diagnostic["label_pass"] = diagnostic.get("template_label", pd.Series(index=diagnostic.index, dtype=str)).isin(
        ["institution_trend_setup", "breakout_acceleration", "capacity_theme_mainwave"]
    )
    variants = {
        "shape88": {"strict": False, "template": None, "min_wave_score": 88.0},
        "shape95_score_only": {"strict": False, "template": None, "min_wave_score": 95.0},
        "shape100_score_only": {"strict": False, "template": None, "min_wave_score": 100.0},
        "shape103_score_only": {"strict": False, "template": None, "min_wave_score": 103.0},
        "shape100": {"strict": True, "template": None},
        "shape88_breakout": {"strict": False, "template": "breakout_acceleration"},
        "shape88_capacity": {"strict": False, "template": "capacity_theme_mainwave"},
    }
    summary_rows: list[dict] = []
    detail_parts: list[pd.DataFrame] = []
    for name, spec in variants.items():
        candidates = base._build_signal_candidates(features, ns, 20, use_learned_sector=False, strict=bool(spec["strict"]), market_gate="none")
        candidates = candidates[pd.to_numeric(candidates["index_mom60"], errors="coerce").le(0.05)].copy()
        candidates = candidates[pd.to_numeric(candidates["wave_style_score"], errors="coerce").ge(float(spec.get("min_wave_score", 88.0)))].copy()
        if spec["template"]:
            candidates = candidates[candidates["template_label"].eq(spec["template"])].copy()
        # Contemporaneous sector diffusion: number of stocks that pass the
        # same observable mainwave skeleton in the same industry on the same
        # signal day.  It contains no forward return or scheduler outcome.
        sector_keys = ["entry_date", "l2_sector_code"]
        valid_sector = candidates["l2_sector_code"].fillna("").astype(str).str.len().gt(0)
        diffusion = (
            candidates.loc[valid_sector]
            .groupby(sector_keys)["code_key"]
            .nunique()
            .rename("sector_signal_count")
            .reset_index()
        )
        candidates = candidates.merge(diffusion, on=sector_keys, how="left")
        candidates["sector_signal_count"] = candidates["sector_signal_count"].fillna(0).astype(int)
        candidates = candidates.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False])
        candidates["daily_rank"] = candidates.groupby("entry_date").cumcount() + 1
        candidates["trade_key"] = _key(candidates, "code_raw")
        candidates["formal_recalled"] = candidates["trade_key"].isin(formal_keys)
        recalled = candidates[candidates["formal_recalled"]].drop_duplicates("trade_key")
        summary_rows.append({
            "variant": name,
            "candidate_rows": int(len(candidates)),
            "candidate_days": int(candidates["entry_date"].nunique()) if not candidates.empty else 0,
            "formal_recall": int(len(recalled)),
            "formal_recall_rate": float(len(recalled) / len(formal)) if len(formal) else None,
            "formal_recall_top2": int(recalled["daily_rank"].le(2).sum()),
            "formal_recall_top5": int(recalled["daily_rank"].le(5).sum()),
            "formal_recall_top10": int(recalled["daily_rank"].le(10).sum()),
            "formal_recall_sector2": int(recalled["sector_signal_count"].ge(2).sum()),
            "formal_recall_sector3": int(recalled["sector_signal_count"].ge(3).sum()),
            "formal_sector_signal_count_avg": float(recalled["sector_signal_count"].mean()) if not recalled.empty else None,
            "all_sector_signal_count_avg": float(candidates["sector_signal_count"].mean()) if not candidates.empty else None,
            "min_wave_score": float(spec.get("min_wave_score", 88.0)),
            "uses_learned_sector": False,
            "index_gate": "000001.SH index_mom60 <= 5%",
        })
        detail_parts.append(candidates[candidates["formal_recalled"]].assign(variant=name))
    summary = pd.DataFrame(summary_rows)
    details = pd.concat(detail_parts, ignore_index=True) if detail_parts else pd.DataFrame()
    summary.to_csv(OUT / "recall_summary.csv", index=False, encoding="utf-8-sig")
    details.to_csv(OUT / "recalled_formal26_details.csv", index=False, encoding="utf-8-sig")
    diagnostic.to_csv(OUT / "formal26_signal_diagnostics.csv", index=False, encoding="utf-8-sig")
    payload = {"status": "completed", "index_code": "000001.SH", "formal_reference_rows": int(len(formal)), "variants": summary_rows,
               "scope": "signal-date-only recall audit; no learned sector, scheduler score, future return, portfolio simulation, or order generation"}
    (OUT / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 正式26笔：信号日候选召回审计", "", "- 仅用于检查新候选生成器能否覆盖旧正式样本。", "- 不使用旧收益、调度分或学习板块标签；召回率不是收益证明。", "", "| 版本 | 候选数 | 候选日 | 召回正式26笔 | 召回率 |", "|---|---:|---:|---:|---:|"]
    for row in summary_rows:
        lines.append(f"| {row['variant']} | {row['candidate_rows']} | {row['candidate_days']} | {row['formal_recall']} | {row['formal_recall_rate']:.1%} |")
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
