from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, standardize, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table, simulate_scaled, summarize  # noqa: E402


SOURCE = _report_path() / "gen3_range_ice_recent3_entry_quality_v1" / "ice_recent3_base.csv"
OUT_DIR = _report_path() / "gen3_range_ice_recent3_quality_quadrants_v1"

VARIANTS = [
    {"variant": "base_ice_recent3", "desc": "冰点后3日窗口原始源"},
    {"variant": "deep_or_reclaim", "desc": "箱体底部或日线修复，二者满足其一"},
    {"variant": "deep_and_reclaim", "desc": "箱体底部且日线修复，二者同时满足"},
    {"variant": "deep_only", "desc": "只有箱体底部，不满足日线修复"},
    {"variant": "reclaim_only", "desc": "只有日线修复，不够贴近箱体底部"},
    {"variant": "neither_deep_nor_reclaim", "desc": "既不贴近箱体底部，也没有强日线修复"},
]


def load_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in [
        "range_pos60",
        "close_position",
        "fwd_ret_confirm_to_close_5d",
        "candidate_score",
        "rank_key",
        "amount_ratio3",
    ]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["is_deep_floor"] = d["range_pos60"].le(0.08)
    d["is_daily_reclaim"] = d["close_position"].ge(0.60)
    return d.dropna(subset=["entry_date", "code", "fwd_ret_confirm_to_close_5d"]).copy()


def select_variant(base: pd.DataFrame, variant: str) -> pd.DataFrame:
    deep = base["is_deep_floor"]
    reclaim = base["is_daily_reclaim"]
    if variant == "base_ice_recent3":
        mask = pd.Series(True, index=base.index)
    elif variant == "deep_or_reclaim":
        mask = deep | reclaim
    elif variant == "deep_and_reclaim":
        mask = deep & reclaim
    elif variant == "deep_only":
        mask = deep & ~reclaim
    elif variant == "reclaim_only":
        mask = ~deep & reclaim
    elif variant == "neither_deep_nor_reclaim":
        mask = ~deep & ~reclaim
    else:
        raise ValueError(variant)
    out = base[mask].copy()
    spec = next(v for v in VARIANTS if v["variant"] == variant)
    out["quadrant_variant"] = spec["variant"]
    out["quadrant_desc"] = spec["desc"]
    return out


def coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        d = select_variant(base, str(spec["variant"]))
        ret = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce")
        rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "signal_count": int(len(d)),
                "raw_avg_5d": float(ret.mean()) if len(ret) else 0.0,
                "raw_win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    base.to_csv(OUT_DIR / "ice_recent3_with_quality_flags.csv", index=False, encoding="utf-8-sig")
    cov = coverage(base)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        selected = select_variant(base, str(spec["variant"]))
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
        "# G3 ice_recent3 入场质量四象限复验 v1",
        "",
        "## 策略名解释",
        "",
        "- `ice_recent3`：中文是“冰点后3个交易日窗口”。",
        "- `deep_or_reclaim`：中文是“箱体底部或日线修复，二者满足其一”。它测试样本能否扩到接近原始频率。",
        "- `deep_and_reclaim`：中文是“箱体底部且日线修复，二者同时满足”。它是上一轮最强质量标签。",
        "- `deep_only`：中文是“只有箱体底部，不满足日线修复”。",
        "- `reclaim_only`：中文是“只有日线修复，不够贴近箱体底部”。",
        "- `neither_deep_nor_reclaim`：中文是“既不贴近箱体底部，也没有强日线修复”。",
        "",
        "## 覆盖率",
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
        "- 若 `deep_or_reclaim` 能保留大部分收益并改善压力口径，可作为主候选源。",
        "- 若只有 `deep_and_reclaim` 稳定，则说明必须牺牲频率换质量。",
        "- 若 `deep_only` 和 `reclaim_only` 差异明显，下一步按更强的一侧继续扩样本。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
