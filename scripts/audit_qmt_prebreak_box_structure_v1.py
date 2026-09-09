"""Test one declared 'box-end' definition before any staged portfolio replay."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client  # noqa: E402
from utils.paths import report_path  # noqa: E402

BASE = report_path("g3_recalled_mainwave_contract_v1", "20200101_20260630_breakout_score88_sector2_stop10%_top10")
BREADTH = report_path("qmt_new_high_breadth_v1", "new_high_breadth_daily.parquet")
OUT = report_path("qmt_prebreak_box_structure_audit_v1")
FEE = 0.003


def _stats(frame: pd.DataFrame, col: str) -> dict:
    x = pd.to_numeric(frame.get(col), errors="coerce").dropna()
    return {"n": int(len(x)), "win_rate": float((x > 0).mean()) if len(x) else None,
            "avg_ret": float(x.mean()) if len(x) else None, "median_ret": float(x.median()) if len(x) else None,
            "worst_ret": float(x.min()) if len(x) else None}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cand = pd.read_csv(BASE / "daily_shortlist_candidates.csv", low_memory=False)
    breadth = pd.read_parquet(BREADTH)
    cand["trade_date"] = pd.to_datetime(cand["trade_date"]).dt.normalize()
    breadth["trade_date"] = pd.to_datetime(breadth["trade_date"]).dt.normalize()
    cand = cand.merge(breadth[["trade_date", "breakout_environment"]], on="trade_date", how="left")
    cand = cand[cand["breakout_environment"].fillna(False)].copy()
    codes = sorted(cand["code_raw"].dropna().astype(str).unique())
    code_sql = ",".join("'" + code.replace("'", "") + "'" for code in codes)
    ch = clickhouse_client()
    daily = ch.query_df(f"""
        SELECT code AS code_raw, trade_date, open, high, low, close
        FROM kline_daily
        WHERE code IN ({code_sql})
          AND trade_date BETWEEN toDate('2019-10-01') AND toDate('2026-07-31')
        ORDER BY code, trade_date
    """)
    daily["trade_date"] = pd.to_datetime(daily["trade_date"]).dt.normalize()
    daily = daily.sort_values(["code_raw", "trade_date"]).copy()
    grouped = daily.groupby("code_raw", group_keys=False)
    # All values below are shifted one day where they define the historical box.
    daily["prior_high10"] = grouped["high"].transform(lambda s: s.shift(1).rolling(10, min_periods=10).max())
    daily["prior_low10"] = grouped["low"].transform(lambda s: s.shift(1).rolling(10, min_periods=10).min())
    daily["box_width10"] = daily["prior_high10"] / daily["prior_low10"] - 1.0
    daily["close_to_box_top"] = daily["close"] / daily["prior_high10"] - 1.0
    daily["box_close_pos"] = (daily["close"] - daily["prior_low10"]) / (daily["prior_high10"] - daily["prior_low10"])
    cols = ["code_raw", "trade_date", "prior_high10", "prior_low10", "box_width10", "close_to_box_top", "box_close_pos"]
    test = cand.merge(daily[cols], on=["code_raw", "trade_date"], how="left")
    # Predeclared, not tuned: a 10-session <=12% box, closing in the upper 30%,
    # within 4% below but not through its visible upper boundary.
    test["declared_box_end"] = (
        test["box_width10"].le(0.12) & test["box_close_pos"].ge(0.70)
        & test["close_to_box_top"].ge(-0.04) & test["close_to_box_top"].lt(0.0)
    )
    test["prebreak_net_ret_h20_proxy"] = pd.to_numeric(test["gross_ret_nextopen_h20"], errors="coerce") - FEE
    selected = test[test["declared_box_end"]].copy()
    test.to_csv(OUT / "all_gated_candidates_with_box_features.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(OUT / "declared_box_end_candidates.csv", index=False, encoding="utf-8-sig")
    payload = {
        "research_only": True,
        "definition": "prior 10 completed sessions form a box <=12%; signal close in top 30% of box; close is 0-4% below (not through) prior box top.",
        "visibility": "Every box field is computed from the signal date and prior completed daily bars. Entry proxy is next-session open.",
        "environment_gated_candidates": int(len(test)), "declared_box_end_candidates": int(len(selected)),
        "retention": float(len(selected) / len(test)) if len(test) else None,
        "all_environment_gated_prebreak_proxy": _stats(test, "prebreak_net_ret_h20_proxy"),
        "declared_box_end_prebreak_proxy": _stats(selected, "prebreak_net_ret_h20_proxy"),
        "promotion": "no_promote: requires a two-slot staged replay and independent window checks even if this fixed-20-day proxy improves",
    }
    (OUT / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
