from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _prepare_base
from scripts.gen3_backtest_strong_volume5_exit_proxy_v1 import _simulate
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import WINDOWS, _max_drawdown, _md_table


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_risk_layer_v1"


def _blend_ret(first_ret: pd.Series, second_ret: pd.Series, cost_bps: float) -> pd.Series:
    # Charge one round-trip cost on the blended position, same convention as prior proxy tests.
    return 0.5 * first_ret + 0.5 * second_ret - cost_bps / 10000.0


def _apply_layer_policy(base: pd.DataFrame, policy: str, cost_bps: float) -> pd.DataFrame:
    d = base.copy()
    d["layer_policy"] = policy
    d["exit_reason_proxy"] = "fixed_h5"
    d["policy_exit_date"] = d["exit_date_h5"]
    d["net_ret"] = d["fixed_net_ret"]
    d["first_leg_weight"] = 0.0
    d["first_leg_date"] = pd.NaT
    d["first_leg_ret"] = 0.0
    d["second_leg_weight"] = 1.0
    d["second_leg_date"] = d["exit_date_h5"]
    d["second_leg_ret"] = d["h5"]
    stop = d["stop5_touch_30m"].astype(bool)

    if policy == "fixed_h5":
        pass
    elif policy == "confirm_d3_full":
        fail = stop & d["fwd_ret_3d"].le(0.0)
        d.loc[fail, "policy_exit_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[fail, "second_leg_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[fail, "second_leg_ret"] = d.loc[fail, "fwd_ret_3d"]
        d.loc[fail, "net_ret"] = d.loc[fail, "fwd_ret_3d"] - cost_bps / 10000.0
        d.loc[fail, "exit_reason_proxy"] = "confirm_d3_full"
    elif policy == "half_d1_le_m3_then_d3":
        half = stop & d["fwd_ret_1d"].le(-0.03)
        fail = half & d["fwd_ret_3d"].le(0.0)
        recover = half & ~fail
        d.loc[fail, "policy_exit_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[half, "first_leg_weight"] = 0.5
        d.loc[half, "first_leg_date"] = d.loc[half, "exit_date_h1"]
        d.loc[half, "first_leg_ret"] = d.loc[half, "fwd_ret_1d"]
        d.loc[half, "second_leg_weight"] = 0.5
        d.loc[fail, "second_leg_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[fail, "second_leg_ret"] = d.loc[fail, "fwd_ret_3d"]
        d.loc[recover, "second_leg_date"] = d.loc[recover, "exit_date_h5"]
        d.loc[recover, "second_leg_ret"] = d.loc[recover, "h5"]
        d.loc[fail, "net_ret"] = _blend_ret(d.loc[fail, "fwd_ret_1d"], d.loc[fail, "fwd_ret_3d"], cost_bps)
        d.loc[fail, "exit_reason_proxy"] = "half_d1_le_m3_d3_fail"
        d.loc[recover, "net_ret"] = _blend_ret(d.loc[recover, "fwd_ret_1d"], d.loc[recover, "h5"], cost_bps)
        d.loc[recover, "exit_reason_proxy"] = "half_d1_le_m3_recover_h5"
    elif policy == "half_d1_le_m5_then_d3":
        half = stop & d["fwd_ret_1d"].le(-0.05)
        fail = half & d["fwd_ret_3d"].le(0.0)
        recover = half & ~fail
        d.loc[fail, "policy_exit_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[half, "first_leg_weight"] = 0.5
        d.loc[half, "first_leg_date"] = d.loc[half, "exit_date_h1"]
        d.loc[half, "first_leg_ret"] = d.loc[half, "fwd_ret_1d"]
        d.loc[half, "second_leg_weight"] = 0.5
        d.loc[fail, "second_leg_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[fail, "second_leg_ret"] = d.loc[fail, "fwd_ret_3d"]
        d.loc[recover, "second_leg_date"] = d.loc[recover, "exit_date_h5"]
        d.loc[recover, "second_leg_ret"] = d.loc[recover, "h5"]
        d.loc[fail, "net_ret"] = _blend_ret(d.loc[fail, "fwd_ret_1d"], d.loc[fail, "fwd_ret_3d"], cost_bps)
        d.loc[fail, "exit_reason_proxy"] = "half_d1_le_m5_d3_fail"
        d.loc[recover, "net_ret"] = _blend_ret(d.loc[recover, "fwd_ret_1d"], d.loc[recover, "h5"], cost_bps)
        d.loc[recover, "exit_reason_proxy"] = "half_d1_le_m5_recover_h5"
    elif policy == "half_d2_le0_then_d3":
        half = stop & d["fwd_ret_2d"].le(0.0)
        fail = half & d["fwd_ret_3d"].le(0.0)
        recover = half & ~fail
        d.loc[fail, "policy_exit_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[half, "first_leg_weight"] = 0.5
        d.loc[half, "first_leg_date"] = d.loc[half, "exit_date_h2"]
        d.loc[half, "first_leg_ret"] = d.loc[half, "fwd_ret_2d"]
        d.loc[half, "second_leg_weight"] = 0.5
        d.loc[fail, "second_leg_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[fail, "second_leg_ret"] = d.loc[fail, "fwd_ret_3d"]
        d.loc[recover, "second_leg_date"] = d.loc[recover, "exit_date_h5"]
        d.loc[recover, "second_leg_ret"] = d.loc[recover, "h5"]
        d.loc[fail, "net_ret"] = _blend_ret(d.loc[fail, "fwd_ret_2d"], d.loc[fail, "fwd_ret_3d"], cost_bps)
        d.loc[fail, "exit_reason_proxy"] = "half_d2_le0_d3_fail"
        d.loc[recover, "net_ret"] = _blend_ret(d.loc[recover, "fwd_ret_2d"], d.loc[recover, "h5"], cost_bps)
        d.loc[recover, "exit_reason_proxy"] = "half_d2_le0_recover_h5"
    elif policy == "half_d2_le_m3_then_d3":
        half = stop & d["fwd_ret_2d"].le(-0.03)
        fail = half & d["fwd_ret_3d"].le(0.0)
        recover = half & ~fail
        d.loc[fail, "policy_exit_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[half, "first_leg_weight"] = 0.5
        d.loc[half, "first_leg_date"] = d.loc[half, "exit_date_h2"]
        d.loc[half, "first_leg_ret"] = d.loc[half, "fwd_ret_2d"]
        d.loc[half, "second_leg_weight"] = 0.5
        d.loc[fail, "second_leg_date"] = d.loc[fail, "exit_date_h3"]
        d.loc[fail, "second_leg_ret"] = d.loc[fail, "fwd_ret_3d"]
        d.loc[recover, "second_leg_date"] = d.loc[recover, "exit_date_h5"]
        d.loc[recover, "second_leg_ret"] = d.loc[recover, "h5"]
        d.loc[fail, "net_ret"] = _blend_ret(d.loc[fail, "fwd_ret_2d"], d.loc[fail, "fwd_ret_3d"], cost_bps)
        d.loc[fail, "exit_reason_proxy"] = "half_d2_le_m3_d3_fail"
        d.loc[recover, "net_ret"] = _blend_ret(d.loc[recover, "fwd_ret_2d"], d.loc[recover, "h5"], cost_bps)
        d.loc[recover, "exit_reason_proxy"] = "half_d2_le_m3_recover_h5"
    else:
        raise ValueError(policy)

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    return d.dropna(subset=["policy_exit_date", "net_ret"]).sort_values(
        ["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"],
        ascending=[True, True, False, True, True],
    )


def _metrics(closed: pd.DataFrame, curve: pd.DataFrame, window: str, policy: str) -> dict:
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "policy": policy, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    return {
        "window": window,
        "policy": policy,
        "closed": int(len(tw)),
        "layered_trades": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5").sum()) if not tw.empty else 0,
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


def _annual(policy: str, closed: pd.DataFrame, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        tw = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "policy": policy,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(part["equity"]),
                "closed": int(len(tw)),
                "layered_trades": int((tw.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str) != "fixed_h5").sum()) if len(tw) else 0,
                "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
                "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
                "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
                "bad10_rate": float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame, diagnostics: pd.DataFrame) -> None:
    pct_cols = {
        "total_ret",
        "max_drawdown",
        "win_rate",
        "mean_trade_ret",
        "worst_trade",
        "bad10_rate",
        "return",
        "mean_candidate_ret",
        "worst_candidate_ret",
    }
    full = summary[summary["window"].eq("full")].sort_values(["policy"])
    lines = [
        "# G3 强势链路 Volume5 风险分层 V1",
        "",
        "## 口径",
        "",
        "- 样本：core_recovery_volume5 = main_up + weak_recovery。",
        "- 账本：slot5_20pct_daily2，daily_open_limit=2，成本 30bps。",
        "- 目标：验证 D1/D2 等待确认期的半仓降风险，不做权重调参。",
        "- 保守记账：触发半仓后，slot 模拟中不额外奖励提前释放资金。",
        "",
        "## Full 窗口",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 年度拆分",
        "",
        _md_table(annual.sort_values(["policy", "year"]), pct_cols=pct_cols),
        "",
        "## 诊断",
        "",
        _md_table(diagnostics.sort_values("policy"), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 风险分层只有在不破坏 2020-2024 稳定性的前提下改善回撤或坏交易，才值得继续。",
        "- 如果半仓规则只是砍收益、没有压住尾部风险，强势链路仍应只保留 shadow。",
        "",
    ]
    (OUT_DIR / "risk_layer_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    base = _prepare_base(cost_bps)
    policies = [
        "fixed_h5",
        "confirm_d3_full",
        "half_d1_le_m3_then_d3",
        "half_d1_le_m5_then_d3",
        "half_d2_le0_then_d3",
        "half_d2_le_m3_then_d3",
    ]
    summaries: list[dict] = []
    annual_parts: list[pd.DataFrame] = []
    diag_rows: list[dict] = []
    for policy in policies:
        candidates = _apply_layer_policy(base, policy, cost_bps)
        candidates.to_csv(OUT_DIR / f"{policy}_candidates.csv", index=False, encoding="utf-8-sig")
        closed, curve = _simulate(candidates, policy, slots=5, slot_pct=0.20, daily_open_limit=2)
        closed.to_csv(OUT_DIR / f"{policy}_closed_trades.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{policy}_curve.csv", index=False, encoding="utf-8-sig")
        annual_parts.append(_annual(policy, closed, curve))
        for window in ["train_2020_2023", "valid_2024_2025", "blind_2026ytd", "full"]:
            summaries.append(_metrics(closed, curve, window, policy))
        diag_rows.append(
            {
                "policy": policy,
                "rows": int(len(candidates)),
                "layered_rows": int((candidates["exit_reason_proxy"].astype(str) != "fixed_h5").sum()),
                "reason_counts": json.dumps(
                    {str(k): int(v) for k, v in candidates["exit_reason_proxy"].astype(str).value_counts().to_dict().items()},
                    ensure_ascii=False,
                ),
                "mean_candidate_ret": float(candidates["net_ret"].mean()),
                "worst_candidate_ret": float(candidates["net_ret"].min()),
                "bad10_rate": float((candidates["net_ret"] <= -0.10).mean()),
            }
        )

    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_parts, ignore_index=True)
    diagnostics = pd.DataFrame(diag_rows)
    summary.to_csv(OUT_DIR / "risk_layer_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "risk_layer_annual_raw.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(OUT_DIR / "risk_layer_diagnostics.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual, diagnostics)
    full = summary[summary["window"].eq("full")].sort_values("policy")
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "source_rows": int(len(base)),
                "full": full[
                    [
                        "policy",
                        "closed",
                        "layered_trades",
                        "total_ret",
                        "max_drawdown",
                        "win_rate",
                        "mean_trade_ret",
                        "worst_trade",
                        "bad10_rate",
                    ]
                ].to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
