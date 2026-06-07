from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, standardize, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table, simulate_scaled, summarize  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_range_box_stress_ice_window_source_v1" / "ice_recent3_signals.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_ice_recent3_entry_quality_v1"

VARIANTS = [
    {"variant": "base_ice_recent3", "desc": "冰点后3日窗口原始源"},
    {"variant": "deep_floor", "desc": "更贴近箱体底部：range_pos60 <= 8%"},
    {"variant": "daily_reclaim", "desc": "日线修复更强：close_position >= 60%"},
    {"variant": "gap_down_reclaim", "desc": "低开后修复：gap_open <= 0 且 close_position >= 60%"},
    {"variant": "deep_floor_reclaim", "desc": "箱体底部 + 日线修复"},
    {"variant": "deep_gap_reclaim", "desc": "箱体底部 + 低开后修复"},
    {"variant": "strong_30m_accept", "desc": "30m承接更强：放量>=1.5且bar涨幅>=0.5%"},
    {"variant": "deep_gap_strong_accept", "desc": "箱体底部 + 低开 + 强30m承接"},
]


def load_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in [
        "range_pos60",
        "close_position",
        "gap_open",
        "amount_ratio3",
        "bar_ret",
        "fwd_ret_confirm_to_close_5d",
        "candidate_score",
        "rank_key",
        "amount_ratio20",
        "index_mom20",
    ]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "fwd_ret_confirm_to_close_5d"]).copy()


def apply_quality(base: pd.DataFrame, variant: str) -> pd.DataFrame:
    d = base.copy()
    if variant == "base_ice_recent3":
        mask = pd.Series(True, index=d.index)
    elif variant == "deep_floor":
        mask = d["range_pos60"].le(0.08)
    elif variant == "daily_reclaim":
        mask = d["close_position"].ge(0.60)
    elif variant == "gap_down_reclaim":
        mask = d["gap_open"].le(0.0) & d["close_position"].ge(0.60)
    elif variant == "deep_floor_reclaim":
        mask = d["range_pos60"].le(0.08) & d["close_position"].ge(0.60)
    elif variant == "deep_gap_reclaim":
        mask = d["range_pos60"].le(0.08) & d["gap_open"].le(0.0) & d["close_position"].ge(0.60)
    elif variant == "strong_30m_accept":
        mask = d["amount_ratio3"].ge(1.50) & d["bar_ret"].ge(0.005)
    elif variant == "deep_gap_strong_accept":
        mask = d["range_pos60"].le(0.08) & d["gap_open"].le(0.0) & d["amount_ratio3"].ge(1.50) & d["bar_ret"].ge(0.005)
    else:
        raise ValueError(variant)
    out = d[mask].copy()
    spec = next(v for v in VARIANTS if v["variant"] == variant)
    out["quality_variant"] = spec["variant"]
    out["quality_desc"] = spec["desc"]
    return out


def coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in VARIANTS:
        d = apply_quality(base, str(spec["variant"]))
        ret = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce")
        rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "signal_count": int(len(d)),
                "signal_days": int(d["entry_date"].nunique()) if not d.empty else 0,
                "raw_avg_5d": float(ret.mean()) if len(ret) else 0.0,
                "raw_win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    base.to_csv(OUT_DIR / "ice_recent3_base.csv", index=False, encoding="utf-8-sig")
    cov = coverage(base)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        selected = apply_quality(base, str(spec["variant"]))
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
    pct_cols = {"raw_avg_5d", "raw_win_rate", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    lines = [
        "# G3 ice_recent3 入场质量拆解 v1",
        "",
        "## 策略名解释",
        "",
        "- `ice_recent3`：中文是“冰点后3个交易日窗口”。市场冰点后3日内，只允许横盘箱体底部 + 30m放量承接买入。",
        "- `deep_floor`：更贴近箱体底部，`range_pos60 <= 8%`。",
        "- `daily_reclaim`：日线修复更强，`close_position >= 60%`。",
        "- `gap_down_reclaim`：低开后修复，`gap_open <= 0` 且 `close_position >= 60%`。",
        "- `deep_gap_reclaim`：箱体底部 + 低开后修复。",
        "- `strong_30m_accept`：30m承接更强，30m成交额相对前三根均量 >=1.5 且确认bar涨幅 >=0.5%。",
        "",
        "## 覆盖率和原始5日表现",
        "",
        md_table(cov, pct_cols=pct_cols),
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
        "- 只接受同时改善 30bps、100bps、2%冲击，且 2024-2025 不明显恶化的入场质量条件。",
        "- 若样本低于 40 笔，只能作为观察标签，不能作为主候选源。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
