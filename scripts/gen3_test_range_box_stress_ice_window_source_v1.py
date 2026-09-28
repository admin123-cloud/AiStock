from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, standardize, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import (  # noqa: E402
    md_table,
    pct,
    load_base,
    simulate_scaled,
    summarize,
)


OUT_DIR = ROOT / "reports" / "gen3_range_box_stress_ice_window_source_v1"

VARIANTS = [
    {"variant": "base", "desc": "原始横盘箱体底部放量承接", "col": None},
    {"variant": "ice_exact", "desc": "只买冰点后第1个交易日", "col": "ice_recent1"},
    {"variant": "ice_recent2", "desc": "冰点后2个交易日窗口", "col": "ice_recent2"},
    {"variant": "ice_recent3", "desc": "冰点后3个交易日窗口", "col": "ice_recent3"},
    {"variant": "ice_recent5", "desc": "冰点后5个交易日窗口", "col": "ice_recent5"},
    {"variant": "neutral_only", "desc": "只买非冰点窗口", "col": "neutral_only"},
]


def attach_ice_windows(base: pd.DataFrame) -> pd.DataFrame:
    d = base.copy()
    days = (
        d[["entry_date", "emotion_signal"]]
        .drop_duplicates("entry_date")
        .sort_values("entry_date")
        .reset_index(drop=True)
    )
    days["is_ice"] = days["emotion_signal"].astype(str).eq("icepoint")
    for n in [1, 2, 3, 5]:
        days[f"ice_recent{n}"] = days["is_ice"].rolling(n, min_periods=1).max().astype(bool)
    days["neutral_only"] = ~days["ice_recent5"]
    out = d.merge(days[["entry_date", "ice_recent1", "ice_recent2", "ice_recent3", "ice_recent5", "neutral_only"]], on="entry_date", how="left")
    for col in ["ice_recent1", "ice_recent2", "ice_recent3", "ice_recent5", "neutral_only"]:
        out[col] = out[col].fillna(False).astype(bool)
    return out


def select_variant(base: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    col = spec["col"]
    if col is None:
        out = base.copy()
    else:
        out = base[base[str(col)]].copy()
    out["source_variant"] = spec["variant"]
    out["source_desc"] = spec["desc"]
    return out


def count_table(base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in VARIANTS:
        sel = select_variant(base, spec)
        rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "signal_count": int(len(sel)),
                "signal_days": int(pd.Series(sel.get("entry_date", [])).nunique()) if not sel.empty else 0,
                "ice_exact_count": int(sel["emotion_signal"].astype(str).eq("icepoint").sum()) if not sel.empty else 0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = attach_ice_windows(load_base())
    base.to_csv(OUT_DIR / "box_stress_signals_with_ice_windows.csv", index=False, encoding="utf-8-sig")
    counts = count_table(base)
    counts.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        selected = select_variant(base, spec)
        selected.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            candidates = standardize(selected, profile)
            curve, closed = simulate_scaled(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, str(spec["variant"]), str(profile["profile"]), str(spec["desc"])))
            window_rows.extend(window_metrics(curve, closed, str(spec["variant"]), str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    lines = [
        "# G3 box_stress_accept_h5 冰点窗口扩频复验 v1",
        "",
        "## 策略名解释",
        "",
        "- `box_stress_accept_h5`：中文是“横盘箱体底部放量承接，持有5日”。",
        "- `ice_exact`：只买冰点后的第1个交易日，也就是上一轮的 `ice_only`。",
        "- `ice_recent2`：当前交易日或前1个交易日处在冰点后窗口内，等于冰点后2个交易日内允许买。",
        "- `ice_recent3`：冰点后3个交易日窗口。",
        "- `ice_recent5`：冰点后5个交易日窗口。",
        "- `neutral_only`：不在冰点后5日窗口内的交易，用来观察非冰点残余样本。",
        "",
        "## 覆盖率",
        "",
        md_table(counts),
        "",
        "## slot复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "",
        "- 如果窗口放宽后 100bps 或 2%冲击明显恶化，说明冰点修复只适合短窗口，不应扩频。",
        "- 如果 `ice_recent3` 能扩大交易数且压力口径接近 `ice_exact`，才值得继续做独立候选源。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
