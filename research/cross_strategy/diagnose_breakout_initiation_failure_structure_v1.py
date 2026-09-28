from __future__ import annotations

"""Describe failure structure of executable breakout entries; no threshold optimization."""

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


SOURCE = report_path("breakout_1030_execution_proxy_v1", "closed_trades.csv")
OUT_DIR = report_path("breakout_initiation_failure_structure_v1")
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "validation_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026": ("2026-01-01", "2026-05-15"),
    "full": ("2020-01-01", "2026-05-15"),
}


def _metrics(frame: pd.DataFrame) -> dict[str, float | int]:
    ret = pd.to_numeric(frame["net_ret"], errors="coerce").dropna()
    win, loss = ret[ret > 0], ret[ret <= 0]
    return {
        "trades": int(len(ret)),
        "expectation": float(ret.mean()) if len(ret) else np.nan,
        "win_rate": float((ret > 0).mean()) if len(ret) else np.nan,
        "avg_win": float(win.mean()) if len(win) else np.nan,
        "avg_loss": float(loss.mean()) if len(loss) else np.nan,
        "payoff_ratio": float(win.mean() / abs(loss.mean())) if len(win) and len(loss) else np.nan,
        "stop_rate": float(frame["exit_reason"].eq("breakout_invalidation_stop").mean()) if len(frame) else np.nan,
    }


def _bands(data: pd.DataFrame) -> pd.DataFrame:
    d = data.copy()
    d["breakout_distance"] = d["m30_cutoff_close"] / d["high20_prev"] - 1.0
    specs = {
        "breakout_distance": ([-np.inf, 0.01, 0.03, np.inf], ["<=1%", "1%-3%", ">3%"]),
        "m30_morning_ret": ([-np.inf, 0.0, 0.01, np.inf], ["<=0%", "0%-1%", ">1%"]),
        "m30_morning_drawdown": ([-np.inf, -0.02, 0.0, np.inf], ["<=-2%", "-2%-0%", ">=0%"]),
        "amount_ratio": ([-np.inf, 1.5, 2.0, np.inf], ["<=1.5x", "1.5x-2.0x", ">2.0x"]),
        "base_range10": ([-np.inf, 0.10, 0.20, np.inf], ["<=10%", "10%-20%", ">20%"]),
    }
    rows = []
    for feature, (bins, labels) in specs.items():
        d["band"] = pd.cut(pd.to_numeric(d[feature], errors="coerce"), bins=bins, labels=labels, include_lowest=True)
        for window, (start, end) in WINDOWS.items():
            sample = d[d["entry_date"].between(pd.Timestamp(start), pd.Timestamp(end))]
            for band, group in sample.groupby("band", observed=False):
                if group.empty:
                    continue
                rows.append({"feature": feature, "band": str(band), "window": window, **_metrics(group)})
    return pd.DataFrame(rows)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = pd.read_csv(SOURCE, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    numeric = ["m30_cutoff_close", "high20_prev", "m30_morning_ret", "m30_morning_drawdown", "amount_ratio", "base_range10", "net_ret"]
    for col in numeric:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["entry_date", "net_ret", "m30_cutoff_close", "high20_prev"]).copy()
    bands = _bands(d)
    failures = d[d["exit_reason"].eq("breakout_invalidation_stop")].sort_values("net_ret").copy()
    keep = ["entry_date", "code", "code_raw", "net_ret", "exit_reason", "breakout_distance", "m30_morning_ret", "m30_morning_drawdown", "amount_ratio", "base_range10", "mom20", "mom60"]
    failures["breakout_distance"] = failures["m30_cutoff_close"] / failures["high20_prev"] - 1.0
    failures = failures[[c for c in keep if c in failures.columns]]
    bands.to_csv(OUT_DIR / "feature_band_metrics.csv", index=False, encoding="utf-8-sig")
    failures.to_csv(OUT_DIR / "stop_failure_samples.csv", index=False, encoding="utf-8-sig")
    meta = {
        "status": "completed",
        "research_only": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "entries": int(len(d)),
        "stop_failures": int(len(failures)),
        "method": "predeclared broad descriptive bands; not a parameter search and not a filter recommendation",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        "# 前高突破启动：失败结构归因 v1", "",
        "仅研究。所有分组均为预先声明的宽区间，用于解释失败来源，不构成新的买入过滤条件。", "",
        f"样本：可执行 30m 承接条目 {len(d)} 笔；止损失效 {len(failures)} 笔。", "",
        "## 宽分组结果", "", bands.to_markdown(index=False), "",
        "## 止损失效样本", "", failures.head(40).to_markdown(index=False), "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
