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
from scripts.gen3_build_dynamic_router_combo_v1 import md_table, pct  # noqa: E402


OUT_DIR = ROOT / "reports" / "gen3_combo_range_filter_execution_stress_v1"
CANDIDATES = ROOT / "reports" / "gen3_combo_range_filter_v1" / "range_conservative_combo_b_candidates.csv"
VARIANT = "range_conservative_combo_b"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_csv(CANDIDATES, low_memory=False)
    candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce").dt.normalize()
    candidates["policy_exit_date"] = pd.to_datetime(candidates["policy_exit_date"], errors="coerce").dt.normalize()
    daily = load_daily_ohlc(candidates)
    daily_map = prepare_daily_maps(daily, candidates)
    calendar = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max() + pd.Timedelta(days=20))
    close_map = {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in daily.itertuples(index=False)}

    summary_rows = []
    window_rows = []
    route_parts = []
    for spec in STRESS_SPECS:
        stressed = apply_stress(candidates, spec, calendar, daily_map)
        curve, closed = simulate(stressed, calendar, close_map)
        profile = spec["profile"]
        stressed.to_csv(OUT_DIR / f"{profile}_stressed_candidates.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{profile}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{profile}_closed_trades.csv", index=False, encoding="utf-8-sig")
        row = summarize(curve, closed, profile)
        row["variant"] = VARIANT
        summary_rows.append(row)
        window_rows.extend([{**w, "variant": VARIANT} for w in summarize_windows(curve, closed, profile)])
        route = route_attribution(closed, profile)
        route.insert(0, "variant", VARIANT)
        route_parts.append(route)

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    routes = pd.concat(route_parts, ignore_index=True)
    summary.to_csv(OUT_DIR / "execution_stress_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "execution_stress_windows.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "execution_stress_route_attribution.csv", index=False, encoding="utf-8-sig")

    close100 = summary[summary["profile"].eq("close_100bps")].iloc[0].to_dict()
    nextopen = summary[summary["profile"].eq("nextopen_limitdown_30bps")].iloc[0].to_dict()
    haircut = summary[summary["profile"].eq("nextopen_haircut2_30bps")].iloc[0].to_dict()
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "variant": VARIANT,
        "close_100bps_total_return": close100["total_return"],
        "close_100bps_max_drawdown": close100["max_drawdown"],
        "nextopen_limitdown_30bps_total_return": nextopen["total_return"],
        "nextopen_limitdown_30bps_max_drawdown": nextopen["max_drawdown"],
        "haircut2_total_return": haircut["total_return"],
        "haircut2_max_drawdown": haircut["max_drawdown"],
        "next_step": "compare_filtered_vs_base_and_decide_candidate_upgrade",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 range_conservative_combo_b 执行压力测试 V1

生成时间：{meta["generated_at"]}

## 目的

对回灌组合后的 `range_conservative_combo_b` 重新做真实执行压力测试，确认它不是只在固定 close 口径下改善。

## 执行压力

{md_table(summary, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"})}

## 分窗口

{md_table(windows, {"return", "max_drawdown", "win_rate"})}

## 链路贡献

{md_table(routes, {"win_rate", "avg_trade_return", "worst_trade"})}

## 阶段判断

- 100bps：收益 `{pct(meta["close_100bps_total_return"])}`，回撤 `{pct(meta["close_100bps_max_drawdown"])}`。
- 次日开盘 + 跌停延迟：收益 `{pct(meta["nextopen_limitdown_30bps_total_return"])}`，回撤 `{pct(meta["nextopen_limitdown_30bps_max_drawdown"])}`。
- 极端 nextopen haircut2：收益 `{pct(meta["haircut2_total_return"])}`，回撤 `{pct(meta["haircut2_max_drawdown"])}`。

下一步把该结果与 base guarded 候选并排比较，决定是否升级为新的 G3 候选版本。
"""
    (OUT_DIR / "execution_stress_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
