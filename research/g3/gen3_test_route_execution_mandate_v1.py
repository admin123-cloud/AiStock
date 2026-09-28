from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_audit_guarded_execution_stress_v1 import (  # noqa: E402
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
from utils.paths import report_path  # noqa: E402

OUT_DIR = report_path("gen3_route_execution_mandate_v1")
CANDIDATES = report_path("gen3_combo_range_filter_v1", "range_conservative_combo_b_candidates.csv")
VARIANT = "range_conservative_combo_b"

SPEC_CLOSE_30 = {"profile": "close_30bps", "cost_bps": 30.0, "exit_mode": "close", "limit_delay": True, "open_haircut": 0.0}
SPEC_CLOSE_50 = {"profile": "close_50bps", "cost_bps": 50.0, "exit_mode": "close", "limit_delay": True, "open_haircut": 0.0}
SPEC_CLOSE_100 = {"profile": "close_100bps", "cost_bps": 100.0, "exit_mode": "close", "limit_delay": True, "open_haircut": 0.0}
SPEC_NEXTOPEN_HAIRCUT2 = {
    "profile": "nextopen_haircut2_30bps",
    "cost_bps": 30.0,
    "exit_mode": "next_open",
    "limit_delay": True,
    "open_haircut": 0.02,
}

POLICIES = [
    {
        "profile": "all_nextopen_haircut2_baseline",
        "desc": "全部链路按次日开盘+跌停延迟+2% haircut退出。",
        "route_specs": {
            "down_panic": SPEC_NEXTOPEN_HAIRCUT2,
            "range_gap": SPEC_NEXTOPEN_HAIRCUT2,
            "strong_main": SPEC_NEXTOPEN_HAIRCUT2,
        },
    },
    {
        "profile": "down_range_same_day_close30_strong_haircut2",
        "desc": "down_panic/range_gap 强制同日收盘可成交退出，strong_main 保留次日开盘 haircut2 压力。",
        "route_specs": {
            "down_panic": SPEC_CLOSE_30,
            "range_gap": SPEC_CLOSE_30,
            "strong_main": SPEC_NEXTOPEN_HAIRCUT2,
        },
    },
    {
        "profile": "down_range_same_day_close50_strong_haircut2",
        "desc": "down_panic/range_gap 同日退出并承受50bps成本，strong_main 保留次日开盘 haircut2 压力。",
        "route_specs": {
            "down_panic": SPEC_CLOSE_50,
            "range_gap": SPEC_CLOSE_50,
            "strong_main": SPEC_NEXTOPEN_HAIRCUT2,
        },
    },
    {
        "profile": "down_range_same_day_close100_strong_haircut2",
        "desc": "down_panic/range_gap 同日退出并承受100bps成本，strong_main 保留次日开盘 haircut2 压力。",
        "route_specs": {
            "down_panic": SPEC_CLOSE_100,
            "range_gap": SPEC_CLOSE_100,
            "strong_main": SPEC_NEXTOPEN_HAIRCUT2,
        },
    },
    {
        "profile": "range_same_day_only_down_haircut2",
        "desc": "仅 range_gap 强制同日退出，down_panic/strong_main 仍承受次日开盘 haircut2。",
        "route_specs": {
            "down_panic": SPEC_NEXTOPEN_HAIRCUT2,
            "range_gap": SPEC_CLOSE_30,
            "strong_main": SPEC_NEXTOPEN_HAIRCUT2,
        },
    },
    {
        "profile": "down_same_day_only_range_haircut2",
        "desc": "仅 down_panic 强制同日退出，range_gap/strong_main 仍承受次日开盘 haircut2。",
        "route_specs": {
            "down_panic": SPEC_CLOSE_30,
            "range_gap": SPEC_NEXTOPEN_HAIRCUT2,
            "strong_main": SPEC_NEXTOPEN_HAIRCUT2,
        },
    },
]


def _stress_by_route(candidates: pd.DataFrame, policy: dict, calendar: list[pd.Timestamp], daily_map: dict) -> pd.DataFrame:
    parts = []
    for route, group in candidates.groupby("route", dropna=False):
        spec = dict(policy["route_specs"].get(str(route), SPEC_NEXTOPEN_HAIRCUT2))
        stressed = apply_stress(group, spec, calendar, daily_map)
        stressed["route_execution_profile"] = spec["profile"]
        stressed["mixed_profile"] = policy["profile"]
        parts.append(stressed)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


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
    policy_desc_rows = []

    for policy in POLICIES:
        profile = policy["profile"]
        stressed = _stress_by_route(candidates, policy, calendar, daily_map)
        curve, closed = simulate(stressed, calendar, close_map)
        stressed.to_csv(OUT_DIR / f"{profile}_stressed_candidates.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{profile}_closed_trades.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{profile}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")

        row = summarize(curve, closed, profile)
        row["variant"] = VARIANT
        summary_rows.append(row)
        window_rows.extend([{**w, "variant": VARIANT} for w in summarize_windows(curve, closed, profile)])
        route = route_attribution(closed, profile)
        route.insert(0, "variant", VARIANT)
        route_parts.append(route)
        policy_desc_rows.append(
            {
                "profile": profile,
                "desc": policy["desc"],
                "down_panic": policy["route_specs"]["down_panic"]["profile"],
                "range_gap": policy["route_specs"]["range_gap"]["profile"],
                "strong_main": policy["route_specs"]["strong_main"]["profile"],
            }
        )

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    routes = pd.concat(route_parts, ignore_index=True)
    policies = pd.DataFrame(policy_desc_rows)

    summary.to_csv(OUT_DIR / "route_execution_mandate_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "route_execution_mandate_windows.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "route_execution_mandate_route_attribution.csv", index=False, encoding="utf-8-sig")
    policies.to_csv(OUT_DIR / "route_execution_mandate_policy_defs.csv", index=False, encoding="utf-8-sig")

    best = summary.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).iloc[0].to_dict()
    close100_policy = summary[summary["profile"].eq("down_range_same_day_close100_strong_haircut2")].iloc[0].to_dict()
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "variant": VARIANT,
        "best_profile": best["profile"],
        "best_total_return": best["total_return"],
        "best_max_drawdown": best["max_drawdown"],
        "close100_profile_total_return": close100_policy["total_return"],
        "close100_profile_max_drawdown": close100_policy["max_drawdown"],
        "next_step": "if same-day route execution is stable, audit whether policy_exit_date is intraday-visible for down/range routes",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 路由级执行约束测试 V1

生成时间：{meta["generated_at"]}

## 目的

V2 的选股过滤已经把 close/100bps 与 nextopen+跌停延迟压住，但极端 `nextopen_haircut2` 仍为负。本测试不再改选股参数，而是验证一个更低过拟合的执行假设：弱势恐慌与震荡缺口不是趋势持有，退出必须尽量在同日完成；强势主升链路保留更强的隔夜承受能力。

## 策略定义

{md_table(policies, set())}

## 组合结果

{md_table(summary, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"})}

## 分窗口结果

{md_table(windows, {"return", "max_drawdown", "win_rate"})}

## 链路归因

{md_table(routes, {"win_rate", "avg_trade_return", "worst_trade"})}

## 当前判断

最优测试口径为 `{best["profile"]}`，收益 `{pct(best["total_return"])}`，回撤 `{pct(best["max_drawdown"])}`。

更保守的 down/range 同日退出且按 100bps 成本计，收益 `{pct(close100_policy["total_return"])}`，回撤 `{pct(close100_policy["max_drawdown"])}`。

这说明当前最大的缺口更像执行路径错配：`down_panic/range_gap` 如果被强行拖到次日开盘再承受额外 2% 冲击，会被打穿；但它们在同日退出路径下仍有正收益。下一步不能直接宣布可实盘，而要审计这些同日退出是否真的来自盘中可见 30m 触发，而不是收盘后回看。"""
    (OUT_DIR / "route_execution_mandate_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
