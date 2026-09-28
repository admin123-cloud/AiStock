from __future__ import annotations

"""Test wide mainwave templates without narrowing their candidate counts."""

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
OUT_DIR = report_path("mainwave_template_robustness_v1")
WINDOWS = [("train_2020_2023", "2020-01-01", "2023-12-31"), ("validation_2024_2025", "2024-01-01", "2025-12-31"), ("blind_2026", "2026-01-01", "2026-06-30"), ("full", "2020-01-01", "2026-06-30")]


def _metrics(frame: pd.DataFrame) -> dict:
    ret = pd.to_numeric(frame["net_ret"], errors="coerce").dropna()
    win, loss = ret[ret > 0], ret[ret <= 0]
    return {"trades": int(len(ret)), "candidate_days": int(frame["entry_date"].nunique()), "expectation": float(ret.mean()) if len(ret) else np.nan, "win_rate": float((ret > 0).mean()) if len(ret) else np.nan, "avg_win": float(win.mean()) if len(win) else np.nan, "avg_loss": float(loss.mean()) if len(loss) else np.nan, "payoff_ratio": float(win.mean() / abs(loss.mean())) if len(win) and len(loss) else np.nan, "worst_trade": float(ret.min()) if len(ret) else np.nan}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["entry_date", "template_label", "net_ret", "gross_ret", "wave_style_score", "index_mom60"]
    raw = pd.read_csv(SOURCE, usecols=cols, encoding="utf-8-sig", low_memory=False)
    raw["entry_date"] = pd.to_datetime(raw["entry_date"], errors="coerce").dt.normalize()
    for col in ["net_ret", "gross_ret", "wave_style_score", "index_mom60"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    wide = raw[(raw["wave_style_score"] >= 88.0) & (raw["index_mom60"] <= .05)].dropna(subset=["entry_date", "net_ret", "gross_ret"]).copy()
    # Existing research convention: unadjusted corporate-action artifacts are not executable returns.
    pool = wide[wide["gross_ret"].between(-.50, 1.00)].copy()
    rows = []
    for template, group in pool.groupby("template_label"):
        for window, start, end in WINDOWS:
            sample = group[group["entry_date"].between(pd.Timestamp(start), pd.Timestamp(end))]
            rows.append({"template_label": template, "window": window, **_metrics(sample)})
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "template_window_summary.csv", index=False, encoding="utf-8-sig")
    meta = {"status": "completed", "research_only": True, "generated_at": datetime.now().isoformat(timespec="seconds"), "wide_pool_rows_before_return_sanity": int(len(wide)), "rows_after_return_sanity": int(len(pool)), "method": "fixed score>=88 and index_mom60<=5% wide pool; no template-specific filtering; only excludes gross return outside [-50%, +100%] as a non-executable price artifact", "limitations": "5-day next-open research proxy, no 30m confirmation, no portfolio overlap or exit contract"}
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    report = ["# 机构主升宽池：模板稳健性审计 v1", "", "固定宽池，不进行模板内筛选；仅排除不可执行的未复权价格异常。", "", summary.to_markdown(index=False), ""]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
