from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _load_daily_prices
from scripts.gen3_backtest_strong_volume5_exit_proxy_v1 import _simulate
from scripts.gen3_backtest_strong_volume5_risk_layer_execution_stress_v1 import _apply_execution_variant
from scripts.gen3_backtest_strong_volume5_risk_layer_v1 import _apply_layer_policy, _prepare_base
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import WINDOWS, _max_drawdown, _md_table


OUT_DIR = _report_path() / "gen3_strong_volume5_risk_layer_guard_probe_v1"


def _apply_guard(df: pd.DataFrame, guard: str) -> pd.DataFrame:
    d = df.copy()
    d["guard"] = guard
    d["guard_scale"] = 1.0
    d["guard_note"] = "full"
    if guard == "base":
        return d
    if guard == "weak_recovery_half":
        mask = d["g3_market_style"].astype(str).eq("weak_recovery")
        d.loc[mask, "guard_scale"] = 0.5
        d.loc[mask, "guard_note"] = "weak_recovery_half"
    elif guard == "sector_not_strong_half":
        mask = ~d["sector_strong"].fillna(False).astype(bool)
        d.loc[mask, "guard_scale"] = 0.5
        d.loc[mask, "guard_note"] = "sector_not_strong_half"
    elif guard == "hot_30m_half":
        mask = pd.to_numeric(d["rt_30m_amount_ratio"], errors="coerce").ge(5.0)
        d.loc[mask, "guard_scale"] = 0.5
        d.loc[mask, "guard_note"] = "hot_30m_half"
    elif guard == "rank2_half":
        mask = pd.to_numeric(d["v4_rank"], errors="coerce").ge(2)
        d.loc[mask, "guard_scale"] = 0.5
        d.loc[mask, "guard_note"] = "rank2_half"
    elif guard == "weak_or_sector_half":
        mask = d["g3_market_style"].astype(str).eq("weak_recovery") | ~d["sector_strong"].fillna(False).astype(bool)
        d.loc[mask, "guard_scale"] = 0.5
        d.loc[mask, "guard_note"] = "weak_or_sector_half"
    else:
        raise ValueError(guard)
    d["raw_net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    d["net_ret"] = d["raw_net_ret"] * d["guard_scale"]
    return d


def _metrics(closed: pd.DataFrame, curve: pd.DataFrame, window: str, guard: str) -> dict:
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "guard": guard, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    return {
        "window": window,
        "guard": guard,
        "closed": int(len(tw)),
        "scaled_trades": int((tw.get("guard_scale", pd.Series(dtype=float)).fillna(1.0).astype(float) < 1.0).sum()) if len(tw) else 0,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
        "bad10_rate": float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0,
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual(guard: str, closed: pd.DataFrame, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        tw = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "guard": guard,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(part["equity"]),
                "closed": int(len(tw)),
                "scaled_trades": int((tw.get("guard_scale", pd.Series(dtype=float)).fillna(1.0).astype(float) < 1.0).sum()) if len(tw) else 0,
                "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
                "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
                "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
                "bad10_rate": float((tw["net_ret"] <= -0.10).mean()) if len(tw) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _diagnostics(candidates: pd.DataFrame, guard: str) -> dict:
    return {
        "guard": guard,
        "rows": int(len(candidates)),
        "scaled_rows": int((candidates["guard_scale"].astype(float) < 1.0).sum()),
        "mean_ret": float(candidates["net_ret"].mean()),
        "worst_ret": float(candidates["net_ret"].min()),
        "bad10_rate": float((candidates["net_ret"] <= -0.10).mean()),
        "note_counts": json.dumps({str(k): int(v) for k, v in candidates["guard_note"].astype(str).value_counts().to_dict().items()}, ensure_ascii=False),
    }


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame, diagnostics: pd.DataFrame) -> None:
    pct_cols = {"total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "bad10_rate", "return", "mean_ret", "worst_ret"}
    full = summary[summary["window"].eq("full")].sort_values(["max_drawdown", "total_ret"], ascending=[False, False])
    annual_focus = annual[annual["guard"].isin(["base", "weak_recovery_half", "sector_not_strong_half", "hot_30m_half"])].sort_values(["guard", "year"])
    lines = [
        "# G3 强势链路粗护栏回放 V1",
        "",
        "## 口径",
        "",
        "- 基准：`half_d2_le0_then_d3 + next_open_second_leg + 30bps`。",
        "- 本轮只测少数可解释粗护栏，不做参数搜索。",
        "- 触发护栏时按半仓折算收益/亏损，但仍占用原 slot，不奖励释放资金。",
        "",
        "## Full 窗口",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 年度重点",
        "",
        _md_table(annual_focus, pct_cols=pct_cols),
        "",
        "## 诊断",
        "",
        _md_table(diagnostics.sort_values("guard"), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 护栏只有在降低尾部或回撤的同时不破坏 2020-2024 稳定性，才值得进入下一轮。",
        "- 若只是为了修复单笔最坏交易而明显牺牲总收益或盲测收益，应保留为观察项，不进入候选。",
        "",
    ]
    (OUT_DIR / "guard_probe_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    raw = _prepare_base(cost_bps)
    layered = _apply_layer_policy(raw, "half_d2_le0_then_d3", cost_bps)
    daily = _load_daily_prices(layered)
    base = _apply_execution_variant(layered, daily, "next_open_second_leg", cost_bps)

    guards = [
        "base",
        "weak_recovery_half",
        "sector_not_strong_half",
        "hot_30m_half",
        "rank2_half",
        "weak_or_sector_half",
    ]
    summaries: list[dict] = []
    annual_parts: list[pd.DataFrame] = []
    diagnostics: list[dict] = []
    for guard in guards:
        candidates = _apply_guard(base, guard)
        candidates.to_csv(OUT_DIR / f"{guard}_candidates.csv", index=False, encoding="utf-8-sig")
        closed, curve = _simulate(candidates, guard, slots=5, slot_pct=0.20, daily_open_limit=2)
        closed.to_csv(OUT_DIR / f"{guard}_closed_trades.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{guard}_curve.csv", index=False, encoding="utf-8-sig")
        diagnostics.append(_diagnostics(candidates, guard))
        annual_parts.append(_annual(guard, closed, curve))
        for window in ["train_2020_2023", "valid_2024_2025", "blind_2026ytd", "full"]:
            summaries.append(_metrics(closed, curve, window, guard))

    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_parts, ignore_index=True)
    diag = pd.DataFrame(diagnostics)
    summary.to_csv(OUT_DIR / "guard_probe_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "guard_probe_annual_raw.csv", index=False, encoding="utf-8-sig")
    diag.to_csv(OUT_DIR / "guard_probe_diagnostics.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual, diag)
    full = summary[summary["window"].eq("full")].sort_values(["max_drawdown", "total_ret"], ascending=[False, False])
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "full": full[
                    [
                        "guard",
                        "closed",
                        "scaled_trades",
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
