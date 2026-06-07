from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import (
    _load_daily_prices,
    _next_trade_date_map,
    _price_maps,
    _ret_from_price,
)
from scripts.gen3_backtest_strong_volume5_exit_proxy_v1 import _simulate
from scripts.gen3_backtest_strong_volume5_risk_layer_v1 import _apply_layer_policy, _prepare_base
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import WINDOWS, _max_drawdown, _md_table, _trade_calendar


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_risk_layer_execution_stress_v1"


def _apply_execution_variant(candidates: pd.DataFrame, daily: pd.DataFrame, variant: str, cost_bps: float) -> pd.DataFrame:
    d = candidates.copy()
    d["execution_variant"] = variant
    d["execution_note"] = "same_close"
    if variant == "same_close":
        return d

    maps = _price_maps(daily)
    calendar = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=20))
    next_map = _next_trade_date_map(calendar)

    for idx, row in d.iterrows():
        code = str(row["code"])
        entry_price = float(row.get("entry_price") or 0.0)
        second_date = pd.Timestamp(row["second_leg_date"]).normalize()
        target_date = second_date
        target_field = "close"
        note = variant

        if variant == "next_open_second_leg":
            target_date = next_map.get(second_date, second_date)
            target_field = "open"
        elif variant == "limit_down_delay_second_leg":
            if bool(maps["limit_down"].get((code, second_date), False)):
                target_date = next_map.get(second_date, second_date)
                target_field = "open"
                note = "second_leg_limit_down_delayed"
            else:
                note = "second_leg_no_limit_down"
        else:
            raise ValueError(variant)

        second_price = maps[target_field].get((code, target_date))
        second_ret = _ret_from_price(second_price, entry_price, 0.0)
        if second_ret is None:
            d.at[idx, "execution_note"] = "missing_price_keep_base"
            continue
        first_weight = float(row.get("first_leg_weight") or 0.0)
        second_weight = float(row.get("second_leg_weight") or 1.0)
        first_ret = float(row.get("first_leg_ret") or 0.0)
        net_ret = first_weight * first_ret + second_weight * float(second_ret) - cost_bps / 10000.0
        d.at[idx, "policy_exit_date"] = target_date
        d.at[idx, "second_leg_date"] = target_date
        d.at[idx, "second_leg_ret"] = float(second_ret)
        d.at[idx, "net_ret"] = net_ret
        d.at[idx, "execution_note"] = note

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    return d.dropna(subset=["policy_exit_date", "net_ret"]).sort_values(
        ["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"],
        ascending=[True, True, False, True, True],
    )


def _metrics(closed: pd.DataFrame, curve: pd.DataFrame, window: str, policy: str, variant: str, cost_bps: float) -> dict:
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "policy": policy, "variant": variant, "cost_bps": cost_bps, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    return {
        "window": window,
        "policy": policy,
        "variant": variant,
        "cost_bps": cost_bps,
        "closed": int(len(tw)),
        "layered_trades": int((tw.get("first_leg_weight", pd.Series(dtype=float)).fillna(0.0).astype(float) > 0).sum()) if len(tw) else 0,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
        "bad10_rate": float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0,
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual(policy: str, variant: str, cost_bps: float, closed: pd.DataFrame, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        tw = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "policy": policy,
                "variant": variant,
                "cost_bps": cost_bps,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(part["equity"]),
                "closed": int(len(tw)),
                "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
                "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
                "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
                "bad10_rate": float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _diagnostics(candidates: pd.DataFrame, policy: str, variant: str, cost_bps: float) -> dict:
    return {
        "policy": policy,
        "variant": variant,
        "cost_bps": cost_bps,
        "rows": int(len(candidates)),
        "execution_notes": json.dumps(
            {str(k): int(v) for k, v in candidates.get("execution_note", pd.Series(dtype=str)).astype(str).value_counts().to_dict().items()},
            ensure_ascii=False,
        ),
        "mean_ret": float(candidates["net_ret"].mean()),
        "worst_ret": float(candidates["net_ret"].min()),
        "bad10_rate": float((candidates["net_ret"] <= -0.10).mean()),
    }


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame, diagnostics: pd.DataFrame) -> None:
    pct_cols = {"total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "bad10_rate", "return", "mean_ret", "worst_ret"}
    full = summary[summary["window"].eq("full")].sort_values(["policy", "variant", "cost_bps"])
    annual_focus = annual.sort_values(["policy", "variant", "cost_bps", "year"])
    lines = [
        "# G3 强势链路风险分层执行压力 V1",
        "",
        "## 口径",
        "",
        "- 样本：core_recovery_volume5，slot5_20pct_daily2。",
        "- 比较对象：confirm_d3_full 与 half_d2_le0_then_d3。",
        "- 执行压力：same_close、剩余腿次日开盘、剩余腿近似跌停延迟到次日开盘。",
        "- 半仓第一腿按研究代理原价保留，压力主要作用在最终退出腿。",
        "",
        "## Full 窗口",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 年度拆分",
        "",
        _md_table(annual_focus, pct_cols=pct_cols),
        "",
        "## 诊断",
        "",
        _md_table(diagnostics, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果 half_d2 在次日开盘压力下仍能显著优于 confirm_d3，则它可以作为强势链路下一版候选。",
        "- 如果两者都只靠 2025/2026 或在高滑点下失效，强势链路继续 shadow，不进入正式组合。",
        "",
    ]
    (OUT_DIR / "risk_layer_execution_stress_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    policies = ["confirm_d3_full", "half_d2_le0_then_d3"]
    variants = ["same_close", "next_open_second_leg", "limit_down_delay_second_leg"]
    cost_list = [30.0, 50.0, 100.0]
    summaries: list[dict] = []
    annual_parts: list[pd.DataFrame] = []
    diag_rows: list[dict] = []

    for cost_bps in cost_list:
        base = _prepare_base(cost_bps)
        for policy in policies:
            layered = _apply_layer_policy(base, policy, cost_bps)
            daily = _load_daily_prices(layered)
            for variant in variants:
                candidates = _apply_execution_variant(layered, daily, variant, cost_bps)
                stem = f"{policy}_{variant}_{int(cost_bps)}bps"
                candidates.to_csv(OUT_DIR / f"{stem}_candidates.csv", index=False, encoding="utf-8-sig")
                diag_rows.append(_diagnostics(candidates, policy, variant, cost_bps))
                closed, curve = _simulate(candidates, stem, slots=5, slot_pct=0.20, daily_open_limit=2)
                closed.to_csv(OUT_DIR / f"{stem}_closed_trades.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(OUT_DIR / f"{stem}_curve.csv", index=False, encoding="utf-8-sig")
                annual_parts.append(_annual(policy, variant, cost_bps, closed, curve))
                for window in ["train_2020_2023", "valid_2024_2025", "blind_2026ytd", "full"]:
                    summaries.append(_metrics(closed, curve, window, policy, variant, cost_bps))

    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_parts, ignore_index=True)
    diagnostics = pd.DataFrame(diag_rows)
    summary.to_csv(OUT_DIR / "risk_layer_execution_stress_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "risk_layer_execution_stress_annual_raw.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(OUT_DIR / "risk_layer_execution_stress_diagnostics.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual, diagnostics)
    full = summary[summary["window"].eq("full")].sort_values(["policy", "variant", "cost_bps"])
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "full": full[
                    [
                        "policy",
                        "variant",
                        "cost_bps",
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
