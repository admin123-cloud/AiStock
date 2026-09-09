"""Build a QMT-canonical, pre-breakout box-end candidate source from full A shares."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client  # noqa: E402
from utils.paths import report_path  # noqa: E402

OUT = report_path("qmt_independent_box_end_candidates_v1")
BREADTH = report_path("qmt_new_high_breadth_v1", "new_high_breadth_daily.parquet")


def build(start: str, end: str, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    ch = clickhouse_client()
    # All structural fields use only the signal bar and earlier completed bars.
    # The next-open/20-session-close fields are outcome columns, never filters.
    raw = ch.query_df(f"""
    WITH bars AS (
        SELECT d.code, d.trade_date, d.open, d.high, d.low, d.close, d.amount,
            max(d.high) OVER (PARTITION BY d.code ORDER BY d.trade_date ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING) AS prior_high10,
            min(d.low) OVER (PARTITION BY d.code ORDER BY d.trade_date ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING) AS prior_low10,
            avg(d.close) OVER (PARTITION BY d.code ORDER BY d.trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ma20_prior,
            avg(d.close) OVER (PARTITION BY d.code ORDER BY d.trade_date ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS ma60_prior,
            leadInFrame(d.open, 1, 0.) OVER (PARTITION BY d.code ORDER BY d.trade_date ROWS BETWEEN CURRENT ROW AND 21 FOLLOWING) AS next_open,
            leadInFrame(d.close, 21, 0.) OVER (PARTITION BY d.code ORDER BY d.trade_date ROWS BETWEEN CURRENT ROW AND 21 FOLLOWING) AS close_h20
        FROM kline_daily d
        INNER JOIN stocks s ON d.code = s.code
        WHERE s.type = 'stock'
          AND (s.list_date IS NULL OR d.trade_date >= s.list_date)
          AND (s.delist_date IS NULL OR d.trade_date <= s.delist_date)
          AND d.trade_date BETWEEN subtractDays(toDate('{start}'), 120) AND addDays(toDate('{end}'), 45)
    )
    SELECT *,
        prior_high10 / prior_low10 - 1 AS box_width10,
        close / prior_high10 - 1 AS close_to_box_top,
        (close - prior_low10) / (prior_high10 - prior_low10) AS box_close_pos,
        row_number() OVER (PARTITION BY trade_date ORDER BY amount DESC) AS amount_rank_daily
    FROM bars
    WHERE trade_date BETWEEN toDate('{start}') AND toDate('{end}')
      AND prior_high10 > prior_low10 AND ma20_prior > ma60_prior
      AND close >= ma20_prior
      AND prior_high10 / prior_low10 - 1 <= 0.12
      AND close / prior_high10 - 1 BETWEEN -0.04 AND -0.000001
      AND (close - prior_low10) / (prior_high10 - prior_low10) >= 0.70
      AND next_open > 0 AND close_h20 > 0
    ORDER BY trade_date, amount_rank_daily
    """)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.normalize()
    breadth = pd.read_parquet(BREADTH)
    breadth["trade_date"] = pd.to_datetime(breadth["trade_date"]).dt.normalize()
    raw = raw.merge(breadth[["trade_date", "breakout_environment", "nh100_breadth", "nhall_breadth"]], on="trade_date", how="left")
    raw["breakout_environment"] = raw["breakout_environment"].fillna(False).astype(bool)
    # Capacity is intentionally capped before outcome review, not chosen by return.
    raw = raw[raw["amount_rank_daily"].le(20)].copy()
    raw["nextopen_h20_net_proxy"] = raw["close_h20"] / raw["next_open"] - 1.003
    gated = raw[raw["breakout_environment"]].copy()
    for frame, name in [(raw, "all_box_end_candidates.csv"), (gated, "nh_environment_box_end_candidates.csv")]:
        frame.to_csv(out / name, index=False, encoding="utf-8-sig")
    def stat(frame: pd.DataFrame) -> dict:
        ret = pd.to_numeric(frame["nextopen_h20_net_proxy"], errors="coerce").dropna()
        return {"n": int(len(ret)), "win_rate": float((ret > 0).mean()) if len(ret) else None,
                "avg_ret": float(ret.mean()) if len(ret) else None, "median_ret": float(ret.median()) if len(ret) else None,
                "worst_ret": float(ret.min()) if len(ret) else None}
    payload = {
        "research_only": True,
        "definition": "QMT full A: prior 10 completed sessions box <=12%; signal close 0%-4% below prior box top and in top 30% of box; prior MA20>MA60 and close>=prior MA20; daily amount top20; entry next session open.",
        "visibility": "Candidate fields and NH environment are known after signal-date close; outcome columns are not filters.",
        "all_box_end": stat(raw), "nh_environment_box_end": stat(gated),
        "nh_environment_retention": float(len(gated) / len(raw)) if len(raw) else None,
        "promotion": "no_promote: fixed-20-day proxy only; requires an unchanged-exit and two-slot staged replay",
    }
    (out / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return payload


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start-date", default="2020-01-01")
    p.add_argument("--end-date", default="2026-06-30")
    p.add_argument("--output-dir", default=str(OUT))
    a = p.parse_args()
    build(a.start_date, a.end_date, Path(a.output_dir))
