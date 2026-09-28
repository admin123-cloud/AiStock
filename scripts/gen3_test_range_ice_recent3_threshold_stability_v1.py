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


SOURCE = ROOT / "reports" / "gen3_range_ice_recent3_entry_quality_v1" / "ice_recent3_base.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_ice_recent3_threshold_stability_v1"
FLOOR_THRESHOLDS = [0.08, 0.10, 0.12, 0.15]
RECLAIM_THRESHOLDS = [0.55, 0.60, 0.65]


def load_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["range_pos60", "close_position", "fwd_ret_confirm_to_close_5d", "rank_key", "candidate_score", "amount_ratio3"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "range_pos60", "close_position", "fwd_ret_confirm_to_close_5d"]).copy()


def variant_name(floor: float, reclaim: float) -> str:
    return f"floor{int(round(floor * 100)):02d}_reclaim{int(round(reclaim * 100)):02d}"


def select_variant(base: pd.DataFrame, floor: float, reclaim: float) -> pd.DataFrame:
    d = base[base["range_pos60"].le(floor) & base["close_position"].ge(reclaim)].copy()
    d["threshold_variant"] = variant_name(floor, reclaim)
    d["threshold_desc"] = f"箱体位置<={floor:.0%} 且 日线收盘修复>={reclaim:.0%}"
    d["floor_threshold"] = floor
    d["reclaim_threshold"] = reclaim
    return d


def coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for floor in FLOOR_THRESHOLDS:
        for reclaim in RECLAIM_THRESHOLDS:
            d = select_variant(base, floor, reclaim)
            ret = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce")
            rows.append(
                {
                    "variant": variant_name(floor, reclaim),
                    "floor_threshold": floor,
                    "reclaim_threshold": reclaim,
                    "signal_count": int(len(d)),
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
    for floor in FLOOR_THRESHOLDS:
        for reclaim in RECLAIM_THRESHOLDS:
            selected = select_variant(base, floor, reclaim)
            vname = variant_name(floor, reclaim)
            selected.to_csv(OUT_DIR / f"{vname}_signals.csv", index=False, encoding="utf-8-sig")
            for profile in PROFILES:
                candidates = standardize(selected, profile)
                curve, closed = simulate_scaled(candidates)
                run_dir = OUT_DIR / f"{vname}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
                summary_rows.append(summarize(curve, closed, vname, str(profile["profile"]), f"箱体位置<={floor:.0%} 且 日线收盘修复>={reclaim:.0%}"))
                window_rows.extend(window_metrics(curve, closed, vname, str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    cost30 = summary[summary["profile"].eq("cost30")].copy()
    cost100 = summary[summary["profile"].eq("cost100")].copy()
    shock = summary[summary["profile"].eq("shock2_cost30")].copy()
    merged = (
        cost30[["variant", "trade_count", "total_return", "max_drawdown"]]
        .rename(columns={"total_return": "cost30_return", "max_drawdown": "cost30_max_dd"})
        .merge(cost100[["variant", "total_return", "max_drawdown"]].rename(columns={"total_return": "cost100_return", "max_drawdown": "cost100_max_dd"}), on="variant")
        .merge(shock[["variant", "total_return", "max_drawdown"]].rename(columns={"total_return": "shock2_return", "max_drawdown": "shock2_max_dd"}), on="variant")
        .merge(cov[["variant", "floor_threshold", "reclaim_threshold", "raw_avg_5d", "raw_win_rate"]], on="variant")
        .sort_values(["shock2_return", "cost100_return"], ascending=[False, False])
    )
    merged.to_csv(OUT_DIR / "stability_rank.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "floor_threshold",
        "reclaim_threshold",
        "raw_avg_5d",
        "raw_win_rate",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "cost30_return",
        "cost30_max_dd",
        "cost100_return",
        "cost100_max_dd",
        "shock2_return",
        "shock2_max_dd",
    }
    lines = [
        "# G3 ice_recent3 deep_and_reclaim 阈值稳定性矩阵 v1",
        "",
        "## 策略名解释",
        "",
        "- `ice_recent3`：中文是“冰点后3个交易日窗口”。",
        "- `deep_and_reclaim`：中文是“箱体底部且日线修复”。本轮只测试这个结构在相邻阈值下是否稳定。",
        "- `floor08_reclaim60`：中文是“箱体位置<=8% 且 日线收盘修复>=60%”。数字越低代表越贴近箱体底部；日线修复越高代表当日收盘越靠近高位。",
        "",
        "## 稳定性排名",
        "",
        md_table(merged, pct_cols=pct_cols),
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
        "- 如果多个相邻阈值在 30bps、100bps、2%冲击下都为正，说明结构稳定。",
        "- 如果只有一个点为正，说明可能过拟合，不能推进。",
        "- 如果放宽箱体位置后冲击口径明显变差，说明安全边际不能放松。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
