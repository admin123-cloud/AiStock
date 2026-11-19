from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import (  # noqa: E402
    _load_daily_prices,
    _next_trade_date_map,
    _price_maps,
    _ret_from_price,
)
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    VARIANT_ROUTE_LIMITS,
    max_drawdown,
    md_table,
    simulate_scaled,
    summarize,
)
from utils.paths import report_path  # noqa: E402


SRC_DIR = report_path("gen3_v4_strong_position_scale_probe_v1")
OUT_DIR = report_path("gen3_v4_strong_second_093_execution_stress_v1")
VARIANT = "strong_second_score_ge_093"

WINDOWS = {
    "weak_gap_2022_2024": ("2022-01-01", "2024-12-31"),
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2020-01-01", "2026-05-29"),
}

PROFILES = [
    {"profile": "close_30bps", "mode": "close", "cost_bps": 30.0, "shock": 0.0},
    {"profile": "close_100bps", "mode": "close", "cost_bps": 100.0, "shock": 0.0},
    {"profile": "close_all_shock2", "mode": "close", "cost_bps": 30.0, "shock": 0.02},
    {"profile": "nextopen_30bps", "mode": "nextopen", "cost_bps": 30.0, "shock": 0.0},
    {"profile": "nextopen_100bps", "mode": "nextopen", "cost_bps": 100.0, "shock": 0.0},
    {"profile": "nextopen_haircut2_30bps", "mode": "nextopen", "cost_bps": 30.0, "shock": 0.02},
    {"profile": "nextopen_limitdown_30bps", "mode": "nextopen_limitdown", "cost_bps": 30.0, "shock": 0.0},
]


def load_candidates() -> pd.DataFrame:
    path = SRC_DIR / f"{VARIANT}_candidates.csv"
    d = pd.read_csv(path, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["score", "entry_price", "policy_net_ret", "position_scale", "route_priority"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False]).reset_index(drop=True)


def apply_execution_profile(candidates: pd.DataFrame, daily: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    mode = str(profile["mode"])
    cost_bps = float(profile["cost_bps"])
    shock = float(profile.get("shock", 0.0))
    d["execution_profile"] = str(profile["profile"])
    d["execution_note"] = "close_keep"
    d["base_policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")

    if mode == "close":
        extra_cost = (cost_bps - 30.0) / 10000.0
        d["policy_net_ret"] = d["base_policy_net_ret"] - extra_cost - shock
        if shock > 0:
            d["execution_note"] = "close_extra_shock"
        elif cost_bps > 30.0:
            d["execution_note"] = "close_extra_cost"
        return d.dropna(subset=["policy_net_ret"])

    maps = _price_maps(daily)
    calendar = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=30))
    next_map = _next_trade_date_map(calendar)
    missing = 0
    delayed = 0
    max_delay_days = 10

    for idx, row in d.iterrows():
        code = str(row["code"])
        entry_price = float(row["entry_price"])
        exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
        target_date = next_map.get(exit_date)
        if target_date is None:
            missing += 1
            d.at[idx, "execution_note"] = "missing_next_trade_keep_close"
            d.at[idx, "policy_net_ret"] = float(row["policy_net_ret"]) - shock
            continue

        note = "nextopen"
        if mode == "nextopen_limitdown":
            checks = 0
            while bool(maps["limit_down"].get((code, target_date), False)) and checks < max_delay_days:
                delayed += 1
                note = "limitdown_delayed_nextopen"
                next_date = next_map.get(target_date)
                if next_date is None:
                    break
                target_date = next_date
                checks += 1

        price = maps["open"].get((code, target_date))
        ret = _ret_from_price(price, entry_price, cost_bps)
        if ret is None:
            missing += 1
            d.at[idx, "execution_note"] = "missing_nextopen_keep_close"
            d.at[idx, "policy_net_ret"] = float(row["policy_net_ret"]) - shock
            continue

        d.at[idx, "policy_exit_date"] = target_date
        d.at[idx, "policy_net_ret"] = float(ret) - shock
        d.at[idx, "execution_note"] = note

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["missing_nextopen_count"] = missing
    d["limitdown_delay_count_total"] = delayed
    return d.dropna(subset=["policy_exit_date", "policy_net_ret"]).sort_values(
        ["entry_date", "route_priority", "score"], ascending=[True, False, False]
    )


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, profile: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window, (start, end) in WINDOWS.items():
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        cw = curve[(curve["date"] >= start_ts) & (curve["date"] <= end_ts)].copy()
        tw = closed[(closed["entry_date"] >= start_ts) & (closed["entry_date"] <= end_ts)].copy()
        if cw.empty:
            continue
        start_eq = float(cw["equity"].iloc[0])
        end_eq = float(cw["equity"].iloc[-1])
        net = pd.to_numeric(tw.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "profile": profile,
                "window": window,
                "return": end_eq / start_eq - 1.0,
                "max_drawdown": max_drawdown(cw["equity"] / start_eq),
                "trade_count": int(len(tw)),
                "strong_trades": int(tw["route"].astype(str).eq("strong_main").sum()) if not tw.empty else 0,
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
                "avg_trade_return": float(net.mean()) if len(net) else 0.0,
                "worst_trade": float(net.min()) if len(net) else 0.0,
                "worst_open_mtm_ret": float(cw["worst_open_mtm_ret"].min()),
            }
        )
    return rows


def annual_metrics(curve: pd.DataFrame, closed: pd.DataFrame, profile: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for year, cw in curve.groupby(curve["date"].dt.year):
        cw = cw.sort_values("date")
        tw = closed[closed["entry_date"].dt.year.eq(year)].copy()
        net = pd.to_numeric(tw.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "profile": profile,
                "year": int(year),
                "return": float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0),
                "max_drawdown": max_drawdown(cw["equity"] / float(cw["equity"].iloc[0])),
                "trade_count": int(len(tw)),
                "strong_trades": int(tw["route"].astype(str).eq("strong_main").sum()) if not tw.empty else 0,
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
                "avg_trade_return": float(net.mean()) if len(net) else 0.0,
                "worst_trade": float(net.min()) if len(net) else 0.0,
            }
        )
    return rows


def diagnostics(candidates: pd.DataFrame, profile: str) -> dict[str, Any]:
    net = pd.to_numeric(candidates["policy_net_ret"], errors="coerce")
    notes = candidates["execution_note"].astype(str).value_counts(dropna=False).to_dict()

    def col_max_int(col: str) -> int:
        if col not in candidates.columns:
            return 0
        return int(pd.to_numeric(candidates[col], errors="coerce").fillna(0).max())

    return {
        "profile": profile,
        "candidate_rows": int(len(candidates)),
        "mean_candidate_ret": float(net.mean()),
        "worst_candidate_ret": float(net.min()),
        "bad10_candidate_rate": float((net <= -0.10).mean()),
        "missing_nextopen_count": col_max_int("missing_nextopen_count"),
        "limitdown_delay_count_total": col_max_int("limitdown_delay_count_total"),
        "execution_notes": notes,
    }


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, annual: pd.DataFrame, diagnostics_df: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "strong_main_avg_ret",
        "return",
        "mean_candidate_ret",
        "worst_candidate_ret",
        "bad10_candidate_rate",
    }
    focus = windows[windows["window"].isin(["weak_gap_2022_2024", "train_2020_2023", "valid_2024_2025", "blind_2026ytd", "full"])].copy()
    lines = [
        "# G3 V4 strong_second_score_ge_093 执行压力复核 v1",
        "",
        "## 边界",
        "",
        "- 这是研究压测，不是实盘接入规则。",
        "- 候选源固定为 `strong_second_score_ge_093_candidates.csv`，不重新调参。",
        "- 保留原始 `position_scale` 与 G3 V4 路由日内限制，逐日 MTM 复算。",
        "- `nextopen` 表示所有退出统一延迟到下一交易日开盘成交。",
        "- `nextopen_limitdown` 表示下一交易日若近似跌停不可卖，则继续顺延到可卖开盘；近似口径为 `high <= prev_close * 0.905`。",
        "- `haircut2/all_shock2` 是额外 2% 冲击，不作为优化目标，只用于看脆弱性。",
        "",
        "## Full 汇总",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 窗口复核",
        "",
        md_table(focus, pct_cols=pct_cols),
        "",
        "## 年度复核",
        "",
        md_table(annual, pct_cols=pct_cols),
        "",
        "## 执行诊断",
        "",
        md_table(diagnostics_df, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 若 `nextopen_30bps` 明显低于 `close_30bps`，说明强势链路当前主要吃收盘退出近似，正式化前必须改入场质量或更早可见退出。",
        "- 若 `close_100bps` 仍能保留正收益，但 `nextopen_haircut2_30bps` 崩坏，说明问题更偏执行滑移和隔夜冲击，而不是选股完全失效。",
        "- 这一步只验 0.93 路径能不能承压；下一步再研究如何用可见强度/板块主线救回被 0.93 过滤掉的大赢家。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_candidates()
    daily = _load_daily_prices(base, extra_days=20)
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    diag_rows: list[dict[str, Any]] = []

    for profile in PROFILES:
        name = str(profile["profile"])
        stressed = apply_execution_profile(base, daily, profile)
        run_dir = OUT_DIR / name
        run_dir.mkdir(parents=True, exist_ok=True)
        stressed.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]), VARIANT_ROUTE_LIMITS.get(VARIANT))
        curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(summarize(curve, closed, VARIANT, name))
        window_rows.extend(window_metrics(curve, closed, name))
        annual_rows.extend(annual_metrics(curve, closed, name))
        diag_rows.append(diagnostics(stressed, name))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    annual = pd.DataFrame(annual_rows)
    diagnostics_df = pd.DataFrame(diag_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_metrics.csv", index=False, encoding="utf-8-sig")
    diagnostics_df.to_csv(OUT_DIR / "diagnostics.csv", index=False, encoding="utf-8-sig")
    write_report(summary, windows, annual, diagnostics_df)
    print(f"wrote {OUT_DIR}")
    print(summary[["profile", "trade_count", "total_return", "max_drawdown", "win_rate", "strong_main_pnl"]].to_string(index=False))


if __name__ == "__main__":
    main()
