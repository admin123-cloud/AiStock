from __future__ import annotations

"""Evaluate observable ranking breadth inside the wide institutional-mainwave pool."""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path


SOURCE = report_path("wave_style_template_strategy_backtest_v1", "shape_only_h5", "candidates.csv")
FORMAL = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OUT_DIR = report_path("mainwave_wide_pool_ranking_v1")
WINDOWS = [("train_2020_2023", "2020-01-01", "2023-12-31"), ("validation_2024_2025", "2024-01-01", "2025-12-31"), ("blind_2026", "2026-01-01", "2026-06-30"), ("full", "2020-01-01", "2026-06-30")]
RANKERS = ["rank_key", "wave_style_score", "amount_rank"]
TOP_NS = [2, 5, 10]


def _key(date: pd.Series, code: pd.Series) -> pd.Series:
    return pd.to_datetime(date, errors="coerce").dt.strftime("%Y-%m-%d").fillna("") + "|" + code.astype(str).str.extract(r"(\d{6})", expand=False).fillna("")


def _metrics(frame: pd.DataFrame) -> dict:
    ret = pd.to_numeric(frame["net_ret"], errors="coerce").dropna()
    win, loss = ret[ret > 0], ret[ret <= 0]
    return {"trades": int(len(ret)), "candidate_days": int(frame["entry_date"].nunique()), "expectation": float(ret.mean()) if len(ret) else np.nan, "win_rate": float((ret > 0).mean()) if len(ret) else np.nan, "avg_win": float(win.mean()) if len(win) else np.nan, "avg_loss": float(loss.mean()) if len(loss) else np.nan, "payoff_ratio": float(win.mean() / abs(loss.mean())) if len(win) and len(loss) else np.nan, "worst_trade": float(ret.min()) if len(ret) else np.nan}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    usecols = ["entry_date", "code_raw", "net_ret", "wave_style_score", "rank_key", "amount_rank", "index_mom60", "template_label"]
    raw = pd.read_csv(SOURCE, usecols=usecols, encoding="utf-8-sig", low_memory=False)
    raw["entry_date"] = pd.to_datetime(raw["entry_date"], errors="coerce").dt.normalize()
    for col in ["net_ret", "wave_style_score", "rank_key", "amount_rank", "index_mom60"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    # This is the widest observable engine that recalled 25/26 formal anchors.
    pool = raw[(raw["wave_style_score"] >= 88.0) & (raw["index_mom60"] <= .05)].dropna(subset=["entry_date", "code_raw", "net_ret"]).copy()
    pool["candidate_key"] = _key(pool["entry_date"], pool["code_raw"])
    formal = pd.read_csv(FORMAL, encoding="utf-8-sig", usecols=["entry_date", "code"])
    formal["candidate_key"] = _key(formal["entry_date"], formal["code"])
    formal_keys = set(formal["candidate_key"])
    rows, selected_parts = [], []
    for ranker in RANKERS:
        ranked = pool.sort_values(["entry_date", ranker, "amount_rank"], ascending=[True, False, False]).copy()
        ranked["daily_rank"] = ranked.groupby("entry_date").cumcount() + 1
        for top_n in TOP_NS:
            selected = ranked[ranked["daily_rank"] <= top_n].copy()
            selected["ranker"] = ranker; selected["top_n"] = top_n
            selected["formal_recalled"] = selected["candidate_key"].isin(formal_keys)
            selected_parts.append(selected)
            for window, start, end in WINDOWS:
                sample = selected[selected["entry_date"].between(pd.Timestamp(start), pd.Timestamp(end))]
                rows.append({"ranker": ranker, "top_n": top_n, "window": window, "formal_recall": int(sample["formal_recalled"].sum()), **_metrics(sample)})
    summary = pd.DataFrame(rows)
    selected_all = pd.concat(selected_parts, ignore_index=True)
    summary.to_csv(OUT_DIR / "ranking_window_summary.csv", index=False, encoding="utf-8-sig")
    selected_all.to_csv(OUT_DIR / "ranked_candidates.csv", index=False, encoding="utf-8-sig")
    meta = {"status": "completed", "research_only": True, "generated_at": datetime.now().isoformat(timespec="seconds"), "pool_rows": int(len(pool)), "pool_days": int(pool["entry_date"].nunique()), "formal_reference_rows": int(len(formal)), "method": "fixed wide score>=88/index<=5% pool; compares only observable same-day rankings and fixed Top2/5/10 breadth; net_ret is a 5-day next-open research proxy, not portfolio or executable return", "limitations": "no 30m confirmation, no slot overlap/capacity, no stop/exit simulation, and no promotion decision"}
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    report = ["# 机构主升宽候选池：排序宽度审计 v1", "", "固定宽候选池，不提高分数门槛；比较同日 Top2/5/10 排序的 5 日研究代理。不是组合回测，也不进入交易合同。", "", summary.to_markdown(index=False), ""]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
