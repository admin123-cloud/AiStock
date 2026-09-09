from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import (  # noqa: E402
    PROFILES,
    md_table,
    simulate,
    standardize,
    summarize,
    window_metrics,
)


SOURCE = (
    _report_path()
    / "gen3_range_30m_true_acceptance_structure_v1"
    / "box_stress_accept_h5__no_gapup_strong_bar_signals.csv"
)
OUT_DIR = _report_path() / "gen3_range_30m_no_gapup_fixed_structure_candidates_v1"

BASE_VARIANT = "box_stress_accept_h5__no_gapup_strong_bar"
BASE_CN = "横盘箱体底部不追高30m强承接"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value) * 100:+.2f}%"


def load_signals() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    cols = [
        "range_pos60",
        "close_position",
        "gap_open",
        "index_mom20",
        "amount_ratio3",
        "bar_close_pos",
        "bar_ret",
        "confirm_high",
        "confirm_low",
        "entry_price",
        "minute_day_close",
        "open_range_high",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "rank_key",
    ]
    for col in cols:
        if col in d:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["entry_bar_upper_shadow"] = (d["confirm_high"] - d["entry_price"]) / (d["confirm_high"] - d["confirm_low"]).replace(0, pd.NA)
    d["close_vs_open_range_high"] = d["minute_day_close"] / d["open_range_high"].replace(0, pd.NA) - 1.0
    return d


def masks(d: pd.DataFrame) -> dict[str, tuple[str, pd.Series]]:
    return {
        "base_no_gapup_strong_bar": ("基准：横盘箱体底部不追高30m强承接", pd.Series(True, index=d.index)),
        "floor8_reclaim70": ("箱体位置<=8%且日线修复>=70%", d["range_pos60"].le(0.08) & d["close_position"].ge(0.70)),
        "floor8": ("更贴近箱体底部：箱体位置<=8%", d["range_pos60"].le(0.08)),
        "reclaim70": ("日线修复更强：日线收盘修复>=70%", d["close_position"].ge(0.70)),
        "no_positive_gap": ("完全不跳空：入场日跳空<=0%", d["gap_open"].le(0.0)),
        "floor8_no_failed_open_range": (
            "贴箱体底且未明显跌回早盘高点下方：箱体位置<=8%，收盘相对早盘高点>=-0.5%",
            d["range_pos60"].le(0.08) & d["close_vs_open_range_high"].ge(-0.005),
        ),
        "reclaim70_no_failed_open_range": (
            "日线强修复且未明显跌回早盘高点下方：日线修复>=70%，收盘相对早盘高点>=-0.5%",
            d["close_position"].ge(0.70) & d["close_vs_open_range_high"].ge(-0.005),
        ),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_signals()
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []

    for suffix, (cn, mask) in masks(base).items():
        selected = base[mask.fillna(False)].copy()
        selected["variant"] = f"{BASE_VARIANT}__{suffix}"
        selected["desc"] = f"{BASE_CN} + {cn}"
        selected["filter"] = suffix
        selected["filter_cn"] = cn
        selected.to_csv(OUT_DIR / f"{suffix}_signals.csv", index=False, encoding="utf-8-sig")
        coverage_rows.append(
            {
                "variant": selected["variant"].iloc[0] if len(selected) else f"{BASE_VARIANT}__{suffix}",
                "filter": suffix,
                "filter_cn": cn,
                "base_signal_count": int(len(base)),
                "selected_count": int(len(selected)),
                "selected_days": int(selected["entry_date"].nunique()) if len(selected) else 0,
                "select_rate": float(len(selected) / len(base)) if len(base) else 0.0,
                "raw_avg_5d": float(pd.to_numeric(selected.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce").mean()) if len(selected) else None,
                "raw_win_rate": float((pd.to_numeric(selected.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce") > 0).mean()) if len(selected) else None,
            }
        )
        for profile in PROFILES:
            candidates = standardize(selected, profile)
            curve, closed = simulate(candidates)
            variant = f"{BASE_VARIANT}__{suffix}"
            run_dir = OUT_DIR / f"{suffix}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            row = summarize(curve, closed, variant, str(profile["profile"]), cn)
            row["filter"] = suffix
            row["filter_cn"] = cn
            summary_rows.append(row)
            window_rows.extend(window_metrics(curve, closed, variant, str(profile["profile"])))

    coverage = pd.DataFrame(coverage_rows)
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    primary = summary[summary["profile"].eq("cost30")].sort_values(["total_return", "max_drawdown"], ascending=[False, False]).copy()
    pressure = summary[summary["profile"].isin(["cost100", "shock2_cost30"])].copy()
    pct_cols = {"select_rate", "raw_avg_5d", "raw_win_rate", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    result = {
        "base_variant": BASE_VARIANT,
        "base_cn": BASE_CN,
        "signal_backtest_start": SIGNAL_START,
        "signal_backtest_end": SIGNAL_END,
        "best_cost30": primary.head(5).to_dict(orient="records"),
    }
    (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# G3 横盘30m不追高强承接 固定结构候选复算 v1",
        "",
        "## 对象",
        f"- 英文名：`{BASE_VARIANT}`。",
        f"- 中文解释：{BASE_CN}。它是横盘/箱体底部候选里，只保留不追高并出现30m放量强承接的买法。",
        f"- 回测范围：信号 {SIGNAL_START} 至 {SIGNAL_END}；按完整 slot 复算；交易成本口径含 30bps、100bps、30bps+2%冲击。",
        "",
        "## 覆盖率",
        md_table(coverage, pct_cols=pct_cols),
        "",
        "## 30bps完整复算",
        md_table(primary[["variant", "filter_cn", "profile", "trade_count", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"]], pct_cols=pct_cols),
        "",
        "## 压力口径",
        md_table(pressure[["variant", "filter_cn", "profile", "trade_count", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"]], pct_cols=pct_cols),
        "",
        "## 分窗口",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 结论",
        "- 本轮仍然不使用 score/rank 二次过滤，只验证固定结构。",
        "- 任何候选如果 2024-2025 验证段仍为负，不能因为 2026 盲测收益好就升级。",
        "- 下一步应把有效性不足的结论反馈到候选源层：继续寻找新的横盘/箱体底部源，或改做 30m 真实放量承接的独立源，而不是继续调止盈止损阈值。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
