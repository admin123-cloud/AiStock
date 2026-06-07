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


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_30m_repair_exit_v2"


def _apply_30m_policy(base: pd.DataFrame, bars_map: dict[int, dict], policy: str, cost_bps: float) -> pd.DataFrame:
    if policy in {"fixed_h5", "confirm_d3_le0"}:
        return _apply_policy(base, policy, cost_bps)

    cfg = {
        "m30_stop_no_m3_reclaim_2bar": (2, -0.03),
        "m30_stop_no_m3_reclaim_4bar": (4, -0.03),
    }.get(policy)
    if cfg is None:
        raise ValueError(policy)
    wait_bars, reclaim_level = cfg

    d = base.copy()
    d["exit_reason_proxy"] = "fixed_h5"
    d["policy_exit_date"] = d["exit_date_h5"]
    d["net_ret"] = d["fixed_net_ret"]
    d["m30_exit_datetime"] = pd.NaT
    d["m30_min_low_ret_to_exit"] = pd.NA
    d["m30_bars_after_stop"] = 0
    d["m30_reclaimed_before_exit"] = False
    d["m30_status"] = "not_scanned"
    d["m30_reclaim_level"] = reclaim_level

    for idx, row in d.iterrows():
        info = bars_map.get(idx, {"status": "missing_30m", "bars": pd.DataFrame()})
        bars = info["bars"]
        if info["status"] != "ok" or bars.empty:
            d.at[idx, "m30_status"] = info["status"]
            continue
        stop_hit = bars[bars["low_ret"] <= -0.05]
        if stop_hit.empty:
            d.at[idx, "m30_status"] = "no_stop"
            continue
        stop_idx = int(stop_hit.index[0])
        loc = bars.index.get_loc(stop_idx)
        after = bars.iloc[loc + 1 : loc + 1 + wait_bars].copy()
        if after.empty:
            d.at[idx, "m30_status"] = "stop_no_follow_bars"
            continue
        reclaimed = bool((after["close_ret"] >= reclaim_level).any())
        d.at[idx, "m30_reclaimed_before_exit"] = reclaimed
        d.at[idx, "m30_bars_after_stop"] = int(len(after))
        d.at[idx, "m30_min_low_ret_to_exit"] = float(after["low_ret"].min())
        if reclaimed:
            d.at[idx, "m30_status"] = "stop_reclaimed_to_m3"
            continue
        exit_bar = after.iloc[-1]
        d.at[idx, "policy_exit_date"] = pd.Timestamp(exit_bar["datetime"]).normalize()
        d.at[idx, "m30_exit_datetime"] = pd.Timestamp(exit_bar["datetime"])
        d.at[idx, "net_ret"] = float(exit_bar["close_ret"]) - cost_bps / 10000.0
        d.at[idx, "exit_reason_proxy"] = policy
        d.at[idx, "m30_status"] = "repair_failed_exit"

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
        "# G3 强势链路 Volume5 30m 修复失败退出 V2",
        "",
        "## 口径",
        "",
        "- V1 要求 stop 后重新站回入场价，过于苛刻。",
        "- V2 改成：触发 30m -5% 后，若后续 2 根或 4 根 30m 收盘仍不能修复到 -3% 以内，则按等待窗口末尾 30m 收盘退出。",
        "- 固定测试两个版本：`m30_stop_no_m3_reclaim_2bar`、`m30_stop_no_m3_reclaim_4bar`。",
        "- 参照：`fixed_h5` 和 `confirm_d3_le0`。",
        "- 这仍未纳入跌停不可卖、排队、滑点、尾盘次日开盘。",
        "",
        "## Full 窗口",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 30m 规则诊断",
        "",
        _md_table(diagnostics, pct_cols=pct_cols),
        "",
        "## Slot5 年度",
        "",
        _md_table(slot5_annual, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 若 V2 仍收益塌陷，说明 30m 提前退出需要更复杂的失败确认，而不是单一修复阈值。",
        "- 若 V2 接近 D3 代理，则下一步进入执行压力测试。",
        "",
    ]
    (OUT_DIR / "m30_repair_exit_v2_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    base = _prepare_base(cost_bps)
    bars_map = _prepare_candidate_bars(base)
    policies = ["fixed_h5", "confirm_d3_le0", "m30_stop_no_m3_reclaim_2bar", "m30_stop_no_m3_reclaim_4bar"]
    books = [
        ("slot2_50pct_daily1", 2, 0.50, 1),
        ("slot5_20pct_daily2", 5, 0.20, 2),
    ]
    summaries: list[dict] = []
    annual_parts: list[pd.DataFrame] = []
    diag_rows: list[dict] = []
    for policy in policies:
        candidates = _apply_30m_policy(base, bars_map, policy, cost_bps)
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
    summary.to_csv(OUT_DIR / "m30_repair_exit_v2_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "m30_repair_exit_v2_annual_raw.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(OUT_DIR / "m30_repair_exit_v2_policy_diagnostics.csv", index=False, encoding="utf-8-sig")
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
