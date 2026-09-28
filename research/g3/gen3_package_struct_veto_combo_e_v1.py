from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_audit_guarded_execution_stress_v1 import (  # noqa: E402
    STRESS_SPECS,
    apply_stress,
    load_daily_ohlc,
    prepare_daily_maps,
    route_attribution,
    simulate,
    summarize,
    summarize_windows,
)
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_range_filtered_structural_veto_v1 import VARIANTS, _candidate_set  # noqa: E402


OUT_DIR = ROOT / "reports" / "gen3_struct_veto_combo_e_candidate_v1"
VARIANT = "struct_veto_combo_e"
BACKTEST_START = "2020-01-01"
BACKTEST_END = "2026-05-29"


def _pct(v: object) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def _md(df: pd.DataFrame, pct_cols: set[str]) -> str:
    if df.empty:
        return "_无数据_"
    out = df.copy()
    for col in pct_cols:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    for col in out.columns:
        if col not in pct_cols and pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].map(lambda v: "" if pd.isna(v) else f"{v:.4f}")
    return out.to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    spec = next(item for item in VARIANTS if item["name"] == VARIANT)
    candidates = _candidate_set(spec)
    candidates.to_csv(OUT_DIR / "struct_veto_combo_e_candidates.csv", index=False, encoding="utf-8-sig")

    daily = load_daily_ohlc(candidates)
    daily_map = prepare_daily_maps(daily, candidates)
    calendar = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max() + pd.Timedelta(days=20))
    close_map = {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in daily.itertuples(index=False)}

    summary_rows: list[dict] = []
    window_rows: list[dict] = []
    route_parts: list[pd.DataFrame] = []
    annual_rows: list[dict] = []
    for stress_spec in STRESS_SPECS:
        profile = stress_spec["profile"]
        stressed = apply_stress(candidates, stress_spec, calendar, daily_map)
        curve, closed = simulate(stressed, calendar, close_map)
        stressed.to_csv(OUT_DIR / f"{profile}_stressed_candidates.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{profile}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{profile}_closed_trades.csv", index=False, encoding="utf-8-sig")

        row = summarize(curve, closed, profile)
        row["variant"] = VARIANT
        row["backtest_start"] = BACKTEST_START
        row["backtest_end"] = BACKTEST_END
        row["curve_first_date"] = str(pd.to_datetime(curve["date"], errors="coerce").min().date()) if not curve.empty else ""
        row["curve_last_date"] = str(pd.to_datetime(curve["date"], errors="coerce").max().date()) if not curve.empty else ""
        row["actual_first_entry_date"] = str(pd.to_datetime(closed["entry_date"], errors="coerce").min().date()) if not closed.empty else ""
        row["actual_last_entry_date"] = str(pd.to_datetime(closed["entry_date"], errors="coerce").max().date()) if not closed.empty else ""
        summary_rows.append(row)

        for w in summarize_windows(curve, closed, profile):
            w["variant"] = VARIANT
            window_rows.append(w)

        route = route_attribution(closed, profile)
        route.insert(0, "variant", VARIANT)
        route_parts.append(route)

        c = curve.copy()
        c["date"] = pd.to_datetime(c["date"], errors="coerce")
        c["year"] = c["date"].dt.year
        for year, g in c.groupby("year"):
            g = g.sort_values("date")
            if g.empty:
                continue
            annual_rows.append(
                {
                    "profile": profile,
                    "year": int(year),
                    "start_date": str(g["date"].iloc[0].date()),
                    "end_date": str(g["date"].iloc[-1].date()),
                    "return": float(g["equity"].iloc[-1] / g["equity"].iloc[0] - 1.0),
                    "max_drawdown": float((g["equity"] / g["equity"].cummax() - 1.0).min()),
                    "trade_count": int((pd.to_datetime(closed["entry_date"], errors="coerce").dt.year == int(year)).sum())
                    if not closed.empty
                    else 0,
                }
            )

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    routes = pd.concat(route_parts, ignore_index=True)
    annual = pd.DataFrame(annual_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "route_attribution.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")

    close30 = summary[summary["profile"].eq("close_30bps")].iloc[0].to_dict()
    close100 = summary[summary["profile"].eq("close_100bps")].iloc[0].to_dict()
    haircut = summary[summary["profile"].eq("nextopen_haircut2_30bps")].iloc[0].to_dict()
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "variant": VARIANT,
        "backtest_start": BACKTEST_START,
        "backtest_end": BACKTEST_END,
        "curve_first_date": close30["curve_first_date"],
        "curve_last_date": close30["curve_last_date"],
        "actual_first_entry_date": close30["actual_first_entry_date"],
        "actual_last_entry_date": close30["actual_last_entry_date"],
        "close_30bps": {
            "trade_count": int(close30["trade_count"]),
            "total_return": float(close30["total_return"]),
            "max_drawdown": float(close30["max_drawdown"]),
            "win_rate": float(close30["win_rate"]),
        },
        "close_100bps": {
            "trade_count": int(close100["trade_count"]),
            "total_return": float(close100["total_return"]),
            "max_drawdown": float(close100["max_drawdown"]),
            "win_rate": float(close100["win_rate"]),
        },
        "nextopen_haircut2_30bps": {
            "trade_count": int(haircut["trade_count"]),
            "total_return": float(haircut["total_return"]),
            "max_drawdown": float(haircut["max_drawdown"]),
            "win_rate": float(haircut["win_rate"]),
        },
        "live_status": "research_candidate_only_not_live",
        "next_step": "audit_live_visibility_and_build_shadow_payload_only_if_passed",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    focus_profiles = ["close_30bps", "close_100bps", "nextopen_limitdown_30bps", "nextopen_haircut2_30bps"]
    report = f"""# G3 struct_veto_combo_e 候选包 V1

生成时间：{meta["generated_at"]}

## 英文名解释

- `struct_veto_combo_e`：结构否决组合。它不是按 `score/rank` 继续调参，而是用可解释结构过滤风险样本。
- `down_panic`：弱势恐慌买法，买非理性出清后的修复。
- `range_gap`：横盘/弱反弹买法，买箱体底部或弱反弹修复。
- `strong_main`：强势主线买法，吸收 G2 的 `volume5` 强势思路。
- `close_30bps`：按计划收盘退出，30bps 成本。
- `close_100bps`：按计划收盘退出，100bps 高成本压力。
- `nextopen_haircut2_30bps`：次日开盘退出并额外扣 2%，模拟低开、滑点和冲击成本。

## 回测范围

- 信号/入场窗口：`{BACKTEST_START}` 到 `{BACKTEST_END}`。
- 实际成交日期：`{meta["actual_first_entry_date"]}` 到 `{meta["actual_last_entry_date"]}`。
- MTM 曲线结算日期：`{meta["curve_first_date"]}` 到 `{meta["curve_last_date"]}`。最后一批入场后的持仓退出可能晚于入场窗口截止日。
- 注意：这是研究候选包，不是实盘买点；尚未完成 live visibility 审计和 shadow-only 接入。

## 核心结果

{_md(summary[summary["profile"].isin(focus_profiles)], {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"})}

## 分窗口结果

{_md(windows[windows["profile"].isin(["close_100bps", "nextopen_haircut2_30bps"])], {"return", "max_drawdown", "win_rate"})}

## 年度结果

{_md(annual[annual["profile"].isin(["close_100bps", "nextopen_haircut2_30bps"])], {"return", "max_drawdown"})}

## 链路归因

{_md(routes[routes["profile"].isin(["close_100bps", "nextopen_haircut2_30bps"])], {"win_rate", "avg_trade_return", "worst_trade"})}

## 当前判断

`struct_veto_combo_e` 比 `struct_veto_combo_c` 更偏稳健：它牺牲了一部分普通 30bps 收益，但显著改善最严苛冲击口径，并把 2022-2024 弱势窗口从负收益修到略正。

下一步不能直接接实盘。应先做 live visibility 审计：确认每个字段在入场时是否可见，再生成 shadow-only 候选源。
"""
    (OUT_DIR / "report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
