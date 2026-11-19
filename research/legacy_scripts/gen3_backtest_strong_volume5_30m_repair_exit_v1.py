from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_audit_strong_volume5_confirm_d3_30m_replay_v1 import _load_bars, _scale_bars
from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _apply_policy, _prepare_base
from scripts.gen3_backtest_strong_volume5_exit_proxy_v1 import _simulate
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import WINDOWS, _max_drawdown, _md_table


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_30m_repair_exit_v1"


def _prepare_candidate_bars(base: pd.DataFrame) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for idx, row in base.iterrows():
        code = str(row["code"])
        entry_date = pd.Timestamp(row["entry_date"]).normalize()
        exit_date = pd.Timestamp(row["exit_date_h5"]).normalize()
        confirm_dt = pd.Timestamp(row.get("confirm_datetime")) if pd.notna(row.get("confirm_datetime")) else entry_date
        entry_price = float(row.get("entry_price") or 0.0)
        bars = _load_bars(code, entry_date, exit_date)
        if bars.empty or entry_price <= 0:
            out[idx] = {"status": "missing_30m", "bars": pd.DataFrame()}
            continue
        bars, scale, scale_source = _scale_bars(bars, entry_price, confirm_dt)
        bars = bars[bars["datetime"] >= confirm_dt].copy()
        if bars.empty:
            out[idx] = {"status": "no_bar_after_confirm", "bars": pd.DataFrame()}
            continue
        bars["low_ret"] = bars["adj_low"] / entry_price - 1.0
        bars["close_ret"] = bars["adj_close"] / entry_price - 1.0
        bars["high_ret"] = bars["adj_high"] / entry_price - 1.0
        out[idx] = {
            "status": "ok",
            "bars": bars,
            "scale": scale,
            "scale_source": scale_source,
        }
    return out


def _apply_30m_policy(base: pd.DataFrame, bars_map: dict[int, dict], policy: str, cost_bps: float) -> pd.DataFrame:
    if policy in {"fixed_h5", "confirm_d3_le0"}:
        return _apply_policy(base, policy, cost_bps)

    wait_bars = {
        "m30_stop_no_reclaim_2bar": 2,
        "m30_stop_no_reclaim_4bar": 4,
    }.get(policy)
    if wait_bars is None:
        raise ValueError(policy)

    d = base.copy()
    d["exit_reason_proxy"] = "fixed_h5"
    d["policy_exit_date"] = d["exit_date_h5"]
    d["net_ret"] = d["fixed_net_ret"]
    d["m30_exit_datetime"] = pd.NaT
    d["m30_min_low_ret_to_exit"] = pd.NA
    d["m30_bars_after_stop"] = 0
    d["m30_reclaimed_before_exit"] = False
    d["m30_status"] = "not_scanned"

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
        stop_pos = int(stop_hit.index[0])
        loc = bars.index.get_loc(stop_pos)
        after = bars.iloc[loc + 1 : loc + 1 + wait_bars].copy()
        if after.empty:
            d.at[idx, "m30_status"] = "stop_no_follow_bars"
            continue
        reclaimed = bool((after["close_ret"] >= 0.0).any())
        d.at[idx, "m30_reclaimed_before_exit"] = reclaimed
        d.at[idx, "m30_bars_after_stop"] = int(len(after))
        d.at[idx, "m30_min_low_ret_to_exit"] = float(after["low_ret"].min())
        if reclaimed:
            d.at[idx, "m30_status"] = "stop_reclaimed"
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


def _metrics(closed: pd.DataFrame, curve: pd.DataFrame, window: str, policy: str, book: str) -> dict:
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "policy": policy, "book": book, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    early = tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5" if not tw.empty else pd.Series(dtype=bool)
    return {
        "window": window,
        "policy": policy,
        "book": book,
        "closed": int(len(tw)),
        "early_exits": int(early.sum()) if len(early) else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
        "bad10_rate": float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0,
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual(policy: str, book: str, closed: pd.DataFrame, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        tw = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "policy": policy,
                "book": book,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(part["equity"]),
                "closed": int(len(tw)),
                "early_exits": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5").sum()) if not tw.empty else 0,
                "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
                "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
                "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
                "bad10_rate": float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _policy_diagnostics(candidates: pd.DataFrame, policy: str) -> dict:
    status_counts = candidates.get("m30_status", pd.Series(dtype=str)).astype(str).value_counts(dropna=False).to_dict()
    early = candidates["exit_reason_proxy"].astype(str) != "fixed_h5"
    return {
        "policy": policy,
        "rows": int(len(candidates)),
        "early_exit_rows": int(early.sum()),
        "status_counts": {str(k): int(v) for k, v in status_counts.items()},
        "early_mean_ret": float(candidates.loc[early, "net_ret"].mean()) if early.any() else None,
        "early_worst_ret": float(candidates.loc[early, "net_ret"].min()) if early.any() else None,
        "early_bad10_rate": float((candidates.loc[early, "net_ret"] <= -0.10).mean()) if early.any() else None,
    }


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
        "# G3 强势链路 Volume5 30m 修复失败退出 V1",
        "",
        "## 口径",
        "",
        "- 样本：`core_recovery_volume5 = main_up + weak_recovery`。",
        "- 规则固定：触发 30m 盘中 -5% 后，若后续 2 根或 4 根 30m 收盘都不能重新站回入场价，则在等待窗口末尾按 30m 收盘退出。",
        "- 参照：`fixed_h5` 与 `confirm_d3_le0`；后者仍是 D3 收盘代理。",
        "- 价格尺度：用源 `entry_price` 与确认 bar close 做比例校正后计算 30m 收益。",
        "- 这仍是代理复算：未处理跌停不可卖、真实滑点、排队成交和尾盘次日开盘成交。",
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
        "- 若 30m 修复失败规则能接近 `confirm_d3_le0` 的收益/回撤，同时显著降低最差交易和大亏率，才值得进入正式明细重放。",
        "- 若收益大幅塌陷，则说明强势股需要更宽的修复窗口，不能简单从 D3 代理前移。",
        "- 下一步必须补执行压力：触发 bar 成交、跌停不可卖、30/50/100bps 滑点、尾盘次日开盘。",
        "",
    ]
    (OUT_DIR / "m30_repair_exit_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    base = _prepare_base(cost_bps)
    bars_map = _prepare_candidate_bars(base)
    policies = ["fixed_h5", "confirm_d3_le0", "m30_stop_no_reclaim_2bar", "m30_stop_no_reclaim_4bar"]
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
            for window in WINDOWS:
                summaries.append(_metrics(closed, curve, window, policy, book))

    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_parts, ignore_index=True) if annual_parts else pd.DataFrame()
    diagnostics = pd.DataFrame(diag_rows)
    summary.to_csv(OUT_DIR / "m30_repair_exit_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "m30_repair_exit_annual_raw.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(OUT_DIR / "m30_repair_exit_policy_diagnostics.csv", index=False, encoding="utf-8-sig")
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
