from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_rebuild_range_box_source_map_v1 import (  # noqa: E402
    PROFILES,
    exit_dates as daily_exit_dates,
    load_base,
    md_table,
    simulate as simulate_daily,
    summarize as summarize_daily,
    window_metrics as window_metrics_daily,
)
from scripts.gen3_test_range_30m_volume_acceptance_v1 import (  # noqa: E402
    build_30m_acceptance,
    standardize,
    simulate as simulate_30m,
    summarize as summarize_30m,
    window_metrics as window_metrics_30m,
)
from scripts.gen3_test_range_box_stress_d1d2_weak_exit_v1 import (  # noqa: E402
    apply_exit_policy as apply_30m_exit_policy,
    load_daily_opens,
    summarize as summarize_exit,
)
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


OUT_DIR = ROOT / "reports" / "gen3_strong_panic_30m_d1d2_exit_v1"

D1D2_SPECS = [
    {"suffix": "hold5", "desc": "固定持有5日", "d1_exit": False, "d2_exit": False},
    {"suffix": "d1_exit", "desc": "D1弱确认后D2开盘退出", "d1_exit": True, "d2_exit": False},
    {"suffix": "d2_exit", "desc": "D2仍弱后D3开盘退出", "d1_exit": False, "d2_exit": True},
    {"suffix": "d1_or_d2_exit", "desc": "D1弱或D2仍弱则下一交易日开盘退出", "d1_exit": True, "d2_exit": True},
]


def strong_panic_mask(df: pd.DataFrame) -> pd.Series:
    floor10 = df["range_pos60"].le(0.10)
    very_deep_near_drop = df["drawdown10"].le(-0.12) | df["drawdown5"].le(-0.08)
    strong_reclaim = df["close_position"].ge(0.68)
    no_below_low = df["runup_from_60d_low"].ge(0)
    no_extreme_amount = df["amount_ratio20"].le(2.5)
    return floor10 & very_deep_near_drop & strong_reclaim & no_below_low & no_extreme_amount


def prepare_daily_source(base: pd.DataFrame) -> pd.DataFrame:
    d = base[strong_panic_mask(base)].copy()
    d["variant"] = "strong_panic_filtered_daily_source"
    d["desc"] = "强修复恐慌过滤源：箱体极低、近端深跌、强修复，排除继续创新低和极端放量"
    d["family"] = "strong_panic_filtered"
    d["hold_days"] = 5
    return d


def load_minute_bars_exact(candidates: pd.DataFrame) -> pd.DataFrame:
    pairs = candidates[["entry_date", "code"]].dropna().copy()
    pairs["entry_date"] = pd.to_datetime(pairs["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    pairs["code"] = pairs["code"].astype(str)
    pairs = pairs.dropna().drop_duplicates()
    if pairs.empty:
        return pd.DataFrame()

    by_date = pairs.groupby("entry_date")["code"].apply(lambda s: sorted(set(s))).to_dict()
    dates = sorted(by_date.keys())
    parts: list[pd.DataFrame] = []
    chunk_size = 30
    for i in range(0, len(dates), chunk_size):
        chunk = dates[i : i + chunk_size]
        clauses = []
        for day in chunk:
            codes = by_date[day]
            quoted = ", ".join(f"'{code}'" for code in codes)
            clauses.append(f"(toDate(datetime) = toDate('{day}') AND code IN ({quoted}))")
        where = " OR ".join(clauses)
        part = clickhouse_query_df(
            f"""
            SELECT code, datetime, open, high, low, close, volume, amount
            FROM kline_minute_30
            WHERE {where}
            ORDER BY code, datetime
            """
        )
        if not part.empty:
            parts.append(part)

    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["entry_date"] = bars["datetime"].dt.strftime("%Y-%m-%d")
    bars["bar_time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["code", "datetime", "entry_date", "open", "high", "low", "close"])
    bars = bars.sort_values(["code", "entry_date", "datetime"]).reset_index(drop=True)
    g = bars.groupby(["code", "entry_date"], sort=False)
    bars["prev_bar_high"] = g["high"].shift(1)
    bars["prev_bar_low"] = g["low"].shift(1)
    bars["intraday_high_so_far"] = g["high"].cummax()
    bars["intraday_low_so_far"] = g["low"].cummin()
    bars["amount_ma3_prev"] = g["amount"].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    bars["minute_day_close"] = g["close"].transform("last")
    bar_range = bars["high"] - bars["low"]
    bars["bar_close_pos"] = np.where(bar_range > 0, (bars["close"] - bars["low"]) / bar_range, np.nan)
    bars["bar_ret"] = bars["close"] / bars["open"] - 1.0
    bars["amount_ratio3"] = bars["amount"] / bars["amount_ma3_prev"]
    return bars.replace([np.inf, -np.inf], np.nan)


def prepare_daily_candidates(daily: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = daily.copy()
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = d["entry_date_ts"].map(daily_exit_dates(d["entry_date_ts"]))
    d["policy_net_ret"] = (
        pd.to_numeric(d["fwd_ret_open_to_close_5d"], errors="coerce")
        - float(profile["cost_bps"]) / 10000.0
        - float(profile["shock"])
    )
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret"]).copy()


def add_daily_exit_opens(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    opens = load_daily_opens(d)
    d = d.merge(opens, on=["code", "entry_date"], how="left")
    return d


def apply_daily_exit_policy(daily: pd.DataFrame, spec: dict[str, Any], profile: dict[str, Any]) -> pd.DataFrame:
    d = add_daily_exit_opens(daily)
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = d["entry_date_ts"].map(daily_exit_dates(d["entry_date_ts"]))
    for col in [
        "entry_open",
        "fwd_ret_open_to_close_2d",
        "fwd_ret_open_to_close_3d",
        "fwd_ret_open_to_close_5d",
        "d2_open",
        "d3_open",
    ]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    raw_ret = d["fwd_ret_open_to_close_5d"].copy()
    d["d1_weak_trigger"] = d["fwd_ret_open_to_close_2d"].le(-0.03)
    d["d2_weak_trigger"] = d["fwd_ret_open_to_close_3d"].le(-0.03)
    d["exit_reason"] = "hold5"
    if bool(spec["d1_exit"]):
        mask = d["d1_weak_trigger"] & d["d2_open"].notna()
        raw_ret.loc[mask] = d.loc[mask, "d2_open"] / d.loc[mask, "entry_open"] - 1.0
        d.loc[mask, "exit_reason"] = "d1_weak_d2open"
    if bool(spec["d2_exit"]):
        mask = d["exit_reason"].eq("hold5") & d["d2_weak_trigger"] & d["d3_open"].notna()
        raw_ret.loc[mask] = d.loc[mask, "d3_open"] / d.loc[mask, "entry_open"] - 1.0
        d.loc[mask, "exit_reason"] = "d2_weak_d3open"
    d["policy_net_ret"] = raw_ret - float(profile["cost_bps"]) / 10000.0 - float(profile["shock"])
    d["variant"] = f"strong_panic_daily_{spec['suffix']}"
    d["desc"] = f"日线强修复恐慌过滤源，{spec['desc']}"
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret"]).copy()


def build_30m_signals(daily: pd.DataFrame) -> pd.DataFrame:
    source = daily.copy()
    source["entry_date"] = pd.to_datetime(source["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    bars = load_minute_bars_exact(source)
    m30 = build_30m_acceptance(source, bars=bars)
    if m30.empty:
        return m30
    m30["variant"] = "strong_panic_30m_accept_hold5"
    m30["desc"] = "强修复恐慌过滤源 + 30m真实放量承接确认"
    m30["family"] = "strong_panic_30m_accept"
    m30["hold_days"] = 5
    m30["rank_key"] = pd.to_numeric(m30.get("amount_ratio3", 0.0), errors="coerce").fillna(0.0)
    m30["rank_in_day"] = m30.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
    return m30[m30["rank_in_day"].le(1)].copy()


def summarize_with_exit(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str, desc: str) -> dict[str, Any]:
    out = summarize_exit(curve, closed, variant, profile, desc)
    return out


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-30m", action="store_true", help="Only run daily source and D1/D2 exit tests.")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    daily = prepare_daily_source(base)
    daily.to_csv(OUT_DIR / "daily_source_signals.csv", index=False, encoding="utf-8-sig")
    m30 = pd.DataFrame() if args.skip_30m else build_30m_signals(daily)
    m30.to_csv(OUT_DIR / "m30_accept_signals.csv", index=False, encoding="utf-8-sig")

    coverage = pd.DataFrame(
        [
            {
                "variant": "strong_panic_filtered_daily_source",
                "desc": "日线强修复恐慌过滤源",
                "raw_signals": int(len(daily)),
                "signal_days": int(daily["entry_date"].nunique()) if len(daily) else 0,
            },
            {
                "variant": "strong_panic_30m_accept",
                "desc": "日线源叠加30m真实承接",
                "raw_signals": int(len(m30)),
                "signal_days": int(m30["entry_date"].nunique()) if len(m30) else 0,
            },
        ]
    )
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []

    for profile in PROFILES:
        daily_candidates = prepare_daily_candidates(daily, profile)
        curve, closed = simulate_daily(daily_candidates)
        run_dir = OUT_DIR / f"strong_panic_daily_hold5__{profile['profile']}"
        run_dir.mkdir(parents=True, exist_ok=True)
        daily_candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        spec = {"variant": "strong_panic_daily_hold5", "desc": "日线强修复恐慌过滤源，固定持有5日"}
        summary_rows.append(summarize_daily(curve, closed, spec, str(profile["profile"])))
        window_rows.extend(window_metrics_daily(curve, closed, spec["variant"], str(profile["profile"])))

        for spec_exit in D1D2_SPECS[1:]:
            candidates = apply_daily_exit_policy(daily, spec_exit, profile)
            curve, closed = simulate_daily(candidates)
            variant = f"strong_panic_daily_{spec_exit['suffix']}"
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize_with_exit(curve, closed, variant, str(profile["profile"]), f"日线强修复恐慌过滤源，{spec_exit['desc']}"))
            window_rows.extend(window_metrics_daily(curve, closed, variant, str(profile["profile"])))

        if not m30.empty:
            m30_hold = standardize(m30, profile)
            curve, closed = simulate_30m(m30_hold)
            run_dir = OUT_DIR / f"strong_panic_30m_accept_hold5__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            m30_hold.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize_30m(curve, closed, "strong_panic_30m_accept_hold5", str(profile["profile"]), "30m真实承接确认，固定持有5日"))
            window_rows.extend(window_metrics_30m(curve, closed, "strong_panic_30m_accept_hold5", str(profile["profile"])))

            base_m30_exit = m30.copy()
            base_m30_exit["entry_date"] = pd.to_datetime(base_m30_exit["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            opens = load_daily_opens(base_m30_exit)
            base_m30_exit = base_m30_exit.merge(opens, on=["code", "entry_date"], how="left")
            base_m30_exit["entry_date_ts"] = pd.to_datetime(base_m30_exit["entry_date"], errors="coerce").dt.normalize()
            base_m30_exit["policy_exit_date"] = base_m30_exit["entry_date_ts"].map(lambda x: pd.NaT)
            base_m30_exit["policy_exit_date"] = base_m30_exit["entry_date_ts"].map(lambda x: x + pd.Timedelta(days=8))
            for col in ["entry_price_adjusted", "fwd_ret_confirm_to_close_2d", "fwd_ret_confirm_to_close_3d", "fwd_ret_confirm_to_close_5d", "d2_open", "d3_open"]:
                base_m30_exit[col] = pd.to_numeric(base_m30_exit[col], errors="coerce")
            base_m30_exit["entry_price_used"] = base_m30_exit["entry_price_adjusted"]

            for spec_exit in D1D2_SPECS[1:]:
                variant = f"strong_panic_30m_accept_{spec_exit['suffix']}"
                policy_spec = {
                    "variant": variant,
                    "desc": f"30m真实承接确认，{spec_exit['desc']}",
                    "d1_exit": spec_exit["d1_exit"],
                    "d2_exit": spec_exit["d2_exit"],
                }
                candidates = apply_30m_exit_policy(base_m30_exit, policy_spec, profile)
                candidates["variant"] = variant
                candidates["desc"] = policy_spec["desc"]
                curve, closed = simulate_30m(candidates)
                run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
                summary_rows.append(summarize_with_exit(curve, closed, variant, str(profile["profile"]), f"30m真实承接确认，{spec_exit['desc']}"))
                window_rows.extend(window_metrics_30m(curve, closed, variant, str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    report = [
        "# G3 强修复恐慌过滤源 30m承接与D1/D2快退复验 v1",
        "",
        "## 英文名解释",
        "",
        "- `strong_panic_daily_hold5`：日线强修复恐慌过滤源，固定持有5日。",
        "- `strong_panic_daily_d1_exit`：日线源，D1弱确认后D2开盘退出。",
        "- `strong_panic_daily_d2_exit`：日线源，D2仍弱后D3开盘退出。",
        "- `strong_panic_daily_d1_or_d2_exit`：日线源，D1弱或D2仍弱则下一交易日开盘退出。",
        "- `strong_panic_30m_accept_hold5`：日线源叠加30m真实放量承接确认，固定持有5日。",
        "- `strong_panic_30m_accept_d1_exit/d2_exit/d1_or_d2_exit`：30m承接确认后，再做D1/D2早期弱确认退出。",
        "",
        "## 覆盖率",
        "",
        md_table(coverage),
        "",
        "## slot复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口稳定性",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "",
        "- 若30m承接显著降低回撤但收益被打掉，说明它更像风控确认，不是收益增强。",
        "- 若D1/D2退出改善100bps和shock2，同时不明显伤害2024-2025/2026YTD，才值得保留。",
        "- 若只提升full但验证窗口恶化，视为过拟合或个案修复。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
