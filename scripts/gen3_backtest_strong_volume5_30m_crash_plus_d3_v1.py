from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_30m_repair_exit_v1 import (
    _annual,
    _metrics,
    _policy_diagnostics,
    _prepare_candidate_bars,
)
from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _apply_policy, _prepare_base
from scripts.gen3_backtest_strong_volume5_exit_proxy_v1 import _simulate
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_30m_crash_plus_d3_v1"


def _apply_combo(base: pd.DataFrame, bars_map: dict[int, dict], policy: str, cost_bps: float) -> pd.DataFrame:
    if policy in {"fixed_h5", "confirm_d3_le0"}:
        return _apply_policy(base, policy, cost_bps)
    if policy not in {"d3_plus_m30_low_m12", "d3_plus_m30_close_m8"}:
        raise ValueError(policy)

    d = _apply_policy(base, "confirm_d3_le0", cost_bps)
    d["m30_exit_datetime"] = pd.NaT
    d["m30_status"] = "not_scanned"
    d["m30_trigger_ret"] = pd.NA
    for idx, row in d.iterrows():
        info = bars_map.get(idx, {"status": "missing_30m", "bars": pd.DataFrame()})
        bars = info["bars"]
        if info["status"] != "ok" or bars.empty:
            d.at[idx, "m30_status"] = info["status"]
            continue
        current_exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
        bars = bars[bars["datetime"].dt.normalize() <= current_exit_date].copy()
        if bars.empty:
            d.at[idx, "m30_status"] = "no_bar_before_exit"
            continue
        if policy == "d3_plus_m30_low_m12":
            hit = bars[bars["low_ret"] <= -0.12]
            trigger_col = "low_ret"
        else:
            hit = bars[bars["close_ret"] <= -0.08]
            trigger_col = "close_ret"
        if hit.empty:
            d.at[idx, "m30_status"] = "no_crash_before_d3_or_h5"
            continue
        exit_bar = hit.iloc[0]
        d.at[idx, "policy_exit_date"] = pd.Timestamp(exit_bar["datetime"]).normalize()
        d.at[idx, "m30_exit_datetime"] = pd.Timestamp(exit_bar["datetime"])
        d.at[idx, "net_ret"] = float(exit_bar["close_ret"]) - cost_bps / 10000.0
        d.at[idx, "exit_reason_proxy"] = policy
        d.at[idx, "m30_status"] = "crash_before_d3_exit"
        d.at[idx, "m30_trigger_ret"] = float(exit_bar[trigger_col])

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    return d.dropna(subset=["policy_exit_date", "net_ret"]).sort_values(
        ["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"],
        ascending=[True, True, False, True, True],
    )


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame, diagnostics: pd.DataFrame) -> None:
    pct_cols = {
        "total_ret",
        "max_drawdown",
        "win_rate",
        "mean_trade_ret",
        "worst_trade",
        "bad10_rate",
        "return",
        "early_mean_ret",
        "early_worst_ret",
        "early_bad10_rate",
    }
    full = summary[summary["window"].eq("full")].sort_values(["book", "policy"])
    slot5_annual = annual[annual["book"].eq("slot5_20pct_daily2")].sort_values(["policy", "year"])
    lines = [
        "# G3 强势链路 Volume5 D3 + 30m 极端保护 V1",
        "",
        "## 口径",
        "",
        "- 主规则保留 `confirm_d3_le0`：触发 stop 后第3日仍未转正才退出。",
        "- 叠加极端保护：在原退出日前，若 30m 低点跌破 -12% 或 30m 收盘跌破 -8%，则提前按该 bar 收盘退出。",
        "- 目标：保留 D3 的时间确认优势，同时减少等待期间的极端盘中风险。",
        "- 当前仍未纳入跌停不可卖、排队成交和滑点。",
        "",
        "## Full 窗口",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 规则诊断",
        "",
        _md_table(diagnostics, pct_cols=pct_cols),
        "",
        "## Slot5 年度",
        "",
        _md_table(slot5_annual, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 若组合规则相比 D3 代理收益相近、回撤更小或等待风险更低，则可进入执行压力测试。",
        "- 若组合规则收益明显下降，则说明极端保护触发仍过早，需要先做执行风险标注而非正式退出。",
        "",
    ]
    (OUT_DIR / "m30_crash_plus_d3_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    base = _prepare_base(cost_bps)
    bars_map = _prepare_candidate_bars(base)
    policies = ["fixed_h5", "confirm_d3_le0", "d3_plus_m30_low_m12", "d3_plus_m30_close_m8"]
    books = [
        ("slot2_50pct_daily1", 2, 0.50, 1),
        ("slot5_20pct_daily2", 5, 0.20, 2),
    ]
    summaries: list[dict] = []
    annual_parts: list[pd.DataFrame] = []
    diag_rows: list[dict] = []
    for policy in policies:
        candidates = _apply_combo(base, bars_map, policy, cost_bps)
        candidates.to_csv(OUT_DIR / f"{policy}_candidates.csv", index=False, encoding="utf-8-sig")
        diag_rows.append(_policy_diagnostics(candidates, policy))
        for book, slots, slot_pct, daily_open_limit in books:
            closed, curve = _simulate(candidates, policy, slots, slot_pct, daily_open_limit)
            stem = f"{policy}_{book}"
            closed.to_csv(OUT_DIR / f"{stem}_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(OUT_DIR / f"{stem}_curve.csv", index=False, encoding="utf-8-sig")
            annual_parts.append(_annual(policy, book, closed, curve))
            for window in ["train_2020_2023", "valid_2024_2025", "blind_2026ytd", "full"]:
                summaries.append(_metrics(closed, curve, window, policy, book))

    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_parts, ignore_index=True) if annual_parts else pd.DataFrame()
    diagnostics = pd.DataFrame(diag_rows)
    summary.to_csv(OUT_DIR / "m30_crash_plus_d3_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "m30_crash_plus_d3_annual_raw.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(OUT_DIR / "m30_crash_plus_d3_policy_diagnostics.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual, diagnostics)
    full = summary[summary["window"].eq("full") & summary["book"].eq("slot5_20pct_daily2")].sort_values("policy")
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "source_rows": int(len(base)),
                "slot5_full": full[
                    ["policy", "closed", "early_exits", "total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "bad10_rate"]
                ].to_dict(orient="records"),
                "diagnostics": diag_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
