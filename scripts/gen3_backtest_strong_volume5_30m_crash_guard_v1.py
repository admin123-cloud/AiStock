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


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_30m_crash_guard_v1"


def _apply_guard(base: pd.DataFrame, bars_map: dict[int, dict], policy: str, cost_bps: float) -> pd.DataFrame:
    if policy in {"fixed_h5", "confirm_d3_le0"}:
        return _apply_policy(base, policy, cost_bps)
    d = base.copy()
    d["exit_reason_proxy"] = "fixed_h5"
    d["policy_exit_date"] = d["exit_date_h5"]
    d["net_ret"] = d["fixed_net_ret"]
    d["m30_exit_datetime"] = pd.NaT
    d["m30_status"] = "not_scanned"
    d["m30_trigger_ret"] = pd.NA

    for idx, row in d.iterrows():
        info = bars_map.get(idx, {"status": "missing_30m", "bars": pd.DataFrame()})
        bars = info["bars"]
        if info["status"] != "ok" or bars.empty:
            d.at[idx, "m30_status"] = info["status"]
            continue
        if policy == "m30_crash_low_m12_exit":
            hit = bars[bars["low_ret"] <= -0.12]
            reason = "m30_crash_low_m12_exit"
            trigger_col = "low_ret"
        elif policy == "m30_crash_close_m8_exit":
            hit = bars[bars["close_ret"] <= -0.08]
            reason = "m30_crash_close_m8_exit"
            trigger_col = "close_ret"
        else:
            raise ValueError(policy)
        if hit.empty:
            d.at[idx, "m30_status"] = "no_crash"
            continue
        exit_bar = hit.iloc[0]
        d.at[idx, "policy_exit_date"] = pd.Timestamp(exit_bar["datetime"]).normalize()
        d.at[idx, "m30_exit_datetime"] = pd.Timestamp(exit_bar["datetime"])
        d.at[idx, "net_ret"] = float(exit_bar["close_ret"]) - cost_bps / 10000.0
        d.at[idx, "exit_reason_proxy"] = reason
        d.at[idx, "m30_status"] = "crash_guard_exit"
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
        "# G3 强势链路 Volume5 30m 极端崩坏保护 V1",
        "",
        "## 口径",
        "",
        "- 样本：`core_recovery_volume5 = main_up + weak_recovery`。",
        "- 规则固定：`m30_crash_low_m12_exit` 为 30m 盘中低点跌破 -12% 后按该 bar 收盘退出；`m30_crash_close_m8_exit` 为 30m 收盘跌破 -8% 后按收盘退出。",
        "- 这是保护型退出，只处理极端尾部，不尝试管理普通 -5% 波动。",
        "- 参照：`fixed_h5` 与 `confirm_d3_le0`。",
        "- 未纳入跌停不可卖、滑点和成交排队，后续必须做执行压力。",
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
        "- 如果保护型退出能保留大部分 fixed_h5 收益，同时降低最差交易和大亏率，它比普遍 stop 修复规则更适合强势链路。",
        "- 如果仍无法接近 D3 代理，说明 D3 的价值不只来自极端保护，还来自趋势衰退后的时间退出。",
        "",
    ]
    (OUT_DIR / "m30_crash_guard_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    base = _prepare_base(cost_bps)
    bars_map = _prepare_candidate_bars(base)
    policies = ["fixed_h5", "confirm_d3_le0", "m30_crash_low_m12_exit", "m30_crash_close_m8_exit"]
    books = [
        ("slot2_50pct_daily1", 2, 0.50, 1),
        ("slot5_20pct_daily2", 5, 0.20, 2),
    ]
    summaries: list[dict] = []
    annual_parts: list[pd.DataFrame] = []
    diag_rows: list[dict] = []
    for policy in policies:
        candidates = _apply_guard(base, bars_map, policy, cost_bps)
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
    summary.to_csv(OUT_DIR / "m30_crash_guard_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "m30_crash_guard_annual_raw.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(OUT_DIR / "m30_crash_guard_policy_diagnostics.csv", index=False, encoding="utf-8-sig")
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
