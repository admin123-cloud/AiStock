from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_rebuild_range_box_source_map_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    PROFILES,
    max_drawdown,
    md_table,
    simulate,
    summarize,
    window_metrics,
)
from scripts.gen3_test_range_box_structural_entry_v1 import mask_for  # noqa: E402
from scripts.gen3_validate_intraday_confirm import _label_signals  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = _report_path() / "gen3_four_path_independent_candidates" / "validation_v1" / "labeled_candidates.parquet"
OUT_DIR = _report_path() / "gen3_range_box_structural_30m_accept_v1"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
CURVE_END = "2026-06-04"

DAILY_VARIANTS = [
    {
        "variant": "box_first_panic_reclaim",
        "desc_cn": "箱体底部第一次恐慌后修复：箱体低位、近端急跌、收盘修复或下影承接",
    },
    {
        "variant": "box_first_panic_strong_reclaim",
        "desc_cn": "箱体底部强修复恐慌：箱体极低、近端深跌、收盘修复更强",
    },
    {
        "variant": "box_retest_reclaim",
        "desc_cn": "箱体底部二次回踩不破后反抽：低位反弹过、短线回踩、收盘重新修复",
    },
    {
        "variant": "box_retest_low_amount_reclaim",
        "desc_cn": "箱体底部二次回踩缩量修复：二次回踩不破，日线量能不过热",
    },
    {
        "variant": "box_retest_capitulation_reclaim",
        "desc_cn": "箱体底部二次回踩叠加市场出清：二次回踩结构遇到市场恐慌后修复",
    },
]

POLICIES = [
    {"policy": "h5", "desc_cn": "30m确认后固定持有到D5"},
    {"policy": "d3neg", "desc_cn": "30m确认后，若D3仍为负则D3退出，否则D5退出"},
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value) * 100:+.2f}%"


def money(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value):,.0f}"


def md_table2(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    money_cols = money_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif col in money_cols:
                item[col] = money(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_base() -> pd.DataFrame:
    d = pd.read_parquet(SOURCE).copy()
    d = d[d["g3_chain"].eq("range_box_bottom")].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    numeric_cols = [
        "range_pos60",
        "drawdown5",
        "drawdown10",
        "drawdown20",
        "runup_from_60d_low",
        "mom10",
        "big_down_rate",
        "amount_ratio20",
        "lower_shadow_ratio",
        "close_position",
        "entry_open",
        "candidate_score",
        "up_rate",
        "breadth_ma20",
    ]
    for col in numeric_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["entry_date", "code", "entry_open"]).copy()
    d["entry_date"] = d["entry_date"].dt.strftime("%Y-%m-%d")
    d["struct_tiebreak"] = (
        (1.0 - d["range_pos60"].clip(0, 1)).fillna(0.0) * 10.0
        + d["close_position"].clip(0, 1).fillna(0.0)
        - d["amount_ratio20"].clip(0, 3).fillna(0.0) * 0.02
    )
    return d.sort_values(["entry_date", "struct_tiebreak", "code"], ascending=[True, False, True])


def sql_quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_pair_30m_bars(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    pairs = (
        daily[["code", "entry_date"]]
        .dropna()
        .astype({"code": str, "entry_date": str})
        .drop_duplicates()
        .sort_values(["entry_date", "code"])
        .values.tolist()
    )
    parts: list[pd.DataFrame] = []
    chunk_size = 800
    for i in range(0, len(pairs), chunk_size):
        chunk = pairs[i : i + chunk_size]
        tuple_list = ", ".join(f"({sql_quote(code)}, toDate({sql_quote(date)}))" for code, date in chunk)
        part = clickhouse_query_df(
            f"""
            SELECT code, datetime, open, high, low, close, volume, amount
            FROM kline_minute_30
            WHERE (code, toDate(datetime)) IN ({tuple_list})
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
    rng = (bars["high"] - bars["low"]).replace(0, pd.NA)
    bars["bar_close_pos"] = (bars["close"] - bars["low"]) / rng
    bars["bar_ret"] = bars["close"] / bars["open"].replace(0, pd.NA) - 1.0
    bars["amount_ratio3"] = bars["amount"] / bars["amount_ma3_prev"].replace(0, pd.NA)
    return bars


def build_30m_acceptance(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    bars = load_pair_30m_bars(daily)
    if bars.empty:
        return bars
    ctx_cols = [
        "entry_date",
        "code",
        "name",
        "g3_chain",
        "range_pos60",
        "drawdown5",
        "drawdown10",
        "drawdown20",
        "runup_from_60d_low",
        "mom10",
        "big_down_rate",
        "amount_ratio20",
        "lower_shadow_ratio",
        "close_position",
        "entry_open",
        "candidate_score",
        "up_rate",
        "breadth_ma20",
        "struct_tiebreak",
    ]
    ctx = daily[[c for c in ctx_cols if c in daily.columns]].copy()
    joined = bars.merge(ctx, on=["entry_date", "code"], how="inner")
    joined = joined[joined["bar_time"].ge("10:00:00")].copy()
    if joined.empty:
        return joined
    prior_low = joined.groupby(["code", "entry_date"])["intraday_low_so_far"].shift(1)
    no_new_low = joined["low"] >= prior_low.fillna(joined["low"])
    break_prev = joined["close"] > joined["prev_bar_high"]
    true_accept = (
        (joined["close"] > joined["open"])
        & joined["bar_close_pos"].ge(0.70)
        & joined["amount_ratio3"].fillna(0.0).ge(1.30)
        & (break_prev | no_new_low)
    )
    out = joined[true_accept].copy()
    if out.empty:
        return out
    out = out.sort_values(["entry_date", "code", "datetime"]).groupby(["entry_date", "code"], as_index=False).first()
    out = out.rename(
        columns={
            "datetime": "confirm_datetime",
            "close": "entry_price",
            "open": "confirm_open",
            "high": "confirm_high",
            "low": "confirm_low",
            "amount": "confirm_amount",
        }
    )
    out["confirm_rule"] = "30m_true_volume_acceptance"
    return _label_signals(out)


def daily_source_coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in DAILY_VARIANTS:
        d = base[mask_for(base, spec["variant"])].copy()
        rows.append(
            {
                "daily_variant": spec["variant"],
                "desc": spec["desc_cn"],
                "daily_rows": int(len(d)),
                "daily_days": int(d["entry_date"].nunique()) if len(d) else 0,
                "first_date": d["entry_date"].min() if len(d) else "",
                "last_date": d["entry_date"].max() if len(d) else "",
            }
        )
    return pd.DataFrame(rows)


def select_daily(base: pd.DataFrame, spec: dict[str, str]) -> pd.DataFrame:
    d = base[mask_for(base, spec["variant"])].copy()
    if d.empty:
        return d
    d["daily_variant"] = spec["variant"]
    d["daily_desc"] = spec["desc_cn"]
    return d


def select_one_per_day(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return signals
    d = signals.copy()
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["struct_tiebreak"] = pd.to_numeric(d.get("struct_tiebreak", 0.0), errors="coerce").fillna(0.0)
    return (
        d.sort_values(["entry_date", "confirm_datetime", "struct_tiebreak", "code"], ascending=[True, True, False, True])
        .groupby("entry_date", as_index=False)
        .first()
    )


def prepare_policy(signals: pd.DataFrame, profile: dict[str, Any], policy: str, variant: str, desc: str) -> pd.DataFrame:
    if signals.empty:
        return signals.copy()
    d = signals.copy()
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    ret3 = pd.to_numeric(d["fwd_ret_confirm_to_close_3d"], errors="coerce")
    ret5 = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce")
    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    if policy == "h5":
        d["policy_net_ret"] = ret5 - cost - shock
        d["exit_reason"] = "hold_d5"
    elif policy == "d3neg":
        early = ret3.lt(0.0)
        d["policy_net_ret"] = ret5.where(~early, ret3) - cost - shock
        d["exit_reason"] = early.map({True: "d3_negative_exit", False: "hold_d5"})
    else:
        raise ValueError(policy)
    d["variant"] = variant
    d["desc"] = desc
    d["policy_exit_date"] = pd.to_datetime(d["exit_close_5d"].notna().map(lambda _: pd.NaT))
    # Reuse the existing simulator's required fields and 5-day exit map from daily close labels.
    from scripts.gen3_rebuild_range_box_source_map_v1 import exit_dates as exit_date_map  # noqa: PLC0415

    d["policy_exit_date"] = d["entry_date_ts"].map(exit_date_map(d["entry_date_ts"]))
    d["entry_price_used"] = pd.to_numeric(d["entry_price_adjusted"], errors="coerce")
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret", "entry_price_used"]).copy()


def concentration(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values("realized_pnl", ascending=False).copy()
    total = float(pd.to_numeric(d["realized_pnl"], errors="coerce").sum())
    rows = []
    for n in [0, 1, 3, 5, 10]:
        rest = d.iloc[n:].copy()
        rets = pd.to_numeric(rest["policy_net_ret"], errors="coerce")
        pnl = float(pd.to_numeric(rest.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum())
        rows.append(
            {
                "exclude_top_n": n,
                "remaining_trades": int(len(rest)),
                "remaining_pnl": pnl,
                "remaining_pnl_share": pnl / total if total else 0.0,
                "remaining_avg_return": float(rets.mean()) if len(rets) else 0.0,
                "remaining_win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def group_by_year(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    rows = []
    for year, g in d.groupby("year"):
        rets = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "year": int(year),
                "trade_count": int(len(g)),
                "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
                "avg_return": float(rets.mean()) if len(rets) else 0.0,
                "worst_return": float(rets.min()) if len(rets) else 0.0,
                "best_return": float(rets.max()) if len(rets) else 0.0,
                "pnl": float(pd.to_numeric(g.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows)


def write_report(
    daily_cov: pd.DataFrame,
    confirm_cov: pd.DataFrame,
    summary: pd.DataFrame,
    windows: pd.DataFrame,
    best_variant: str,
    best_desc: str,
    yearly: pd.DataFrame,
    conc: pd.DataFrame,
) -> None:
    pct_cols = {
        "confirm_rate",
        "raw_avg_5d",
        "raw_win_5d",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "avg_return",
        "worst_return",
        "best_return",
        "remaining_pnl_share",
        "remaining_avg_return",
        "remaining_win_rate",
    }
    best_summary = summary[summary["variant"].eq(best_variant)].copy()
    best_windows = windows[windows["variant"].eq(best_variant)].copy()
    lines = [
        "# G3 v4 横盘/箱体底部结构源 + 30m真实承接复验",
        "",
        "## 回测范围",
        f"- 信号入场窗口：{SIGNAL_START} 至 {SIGNAL_END}。",
        f"- 曲线结算窗口：{SIGNAL_START} 至 {CURVE_END}。",
        "- 初始资金：150000；5个槽位；单槽20%；同日最多开1笔。",
        "- 本轮不使用新的 score/rank 过滤；同日多信号只按最早30m确认时间择一，确认时间相同时用更靠近箱体底部的结构优先。",
        "",
        "## 英文策略名解释",
        "- `box_first_panic_reclaim`：箱体底部第一次恐慌后修复。股价在60日箱体低位，短线急跌后收盘修复或下影承接。",
        "- `box_first_panic_strong_reclaim`：第一次恐慌强修复版。要求更深下跌和更强收盘修复。",
        "- `box_retest_reclaim`：箱体底部二次回踩不破后反抽。前面从低位反弹过，短线再次回踩但没有破坏箱体底部，收盘重新修复。",
        "- `low_amount`：缩量修复。表示回踩时日线量能不过热，避免追放量脉冲。",
        "- `capitulation`：市场出清。表示个股结构叠加市场层面的恐慌/大跌日。",
        "- `30m_accept`：30m真实承接。10:00后30m K线非阴、收在区间70%以上、相对前三根30m放量>=1.30，且不再创新低或突破前一根高点。",
        "- `d3neg`：D3弱退出。30m确认后到第3个交易日仍为负，就D3退出；否则D5退出。",
        "",
        "## 日线候选覆盖",
        md_table2(daily_cov),
        "",
        "## 30m确认覆盖",
        md_table2(confirm_cov, pct_cols=pct_cols),
        "",
        "## slot复算结果",
        md_table2(summary, pct_cols=pct_cols),
        "",
        f"## 当前相对最好方案：`{best_variant}`",
        f"- 中文含义：{best_desc}",
        md_table2(best_summary, pct_cols=pct_cols),
        "",
        "## 最好方案分窗口",
        md_table2(best_windows, pct_cols=pct_cols),
        "",
        "## 最好方案年度拆分（cost30）",
        md_table2(yearly, pct_cols=pct_cols, money_cols={"pnl"}),
        "",
        "## 最好方案集中度（cost30）",
        md_table2(conc, pct_cols=pct_cols, money_cols={"remaining_pnl"}),
        "",
        "## 判断",
        "- 如果30m确认后验证段仍不能为正，说明横盘/箱体底部的日线结构本身还不够，需要继续换候选源而不是加退出条件。",
        "- 如果只有训练段或少数大票贡献收益，则保持研究候选，不进入G3实盘。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    daily_cov = daily_source_coverage(base)
    daily_cov.to_csv(OUT_DIR / "daily_source_coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    confirm_rows: list[dict[str, Any]] = []
    closed_map: dict[tuple[str, str], pd.DataFrame] = {}
    best_variant = ""
    best_desc = ""
    best_key: tuple[float, float, int] | None = None

    for daily_spec in DAILY_VARIANTS:
        daily = select_daily(base, daily_spec)
        daily.to_csv(OUT_DIR / f"{daily_spec['variant']}_daily_candidates.csv", index=False, encoding="utf-8-sig")
        confirmed_all = build_30m_acceptance(daily)
        confirmed_all.to_csv(OUT_DIR / f"{daily_spec['variant']}_30m_confirmed_all.csv", index=False, encoding="utf-8-sig")
        confirmed = select_one_per_day(confirmed_all)
        confirmed.to_csv(OUT_DIR / f"{daily_spec['variant']}_30m_confirmed_one_per_day.csv", index=False, encoding="utf-8-sig")
        raw_ret = pd.to_numeric(confirmed.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce")
        confirm_rows.append(
            {
                "daily_variant": daily_spec["variant"],
                "desc": daily_spec["desc_cn"],
                "daily_rows": int(len(daily)),
                "daily_days": int(daily["entry_date"].nunique()) if len(daily) else 0,
                "confirmed_rows": int(len(confirmed_all)),
                "confirmed_trade_days": int(confirmed["entry_date"].nunique()) if len(confirmed) else 0,
                "confirm_rate": float(len(confirmed) / daily["entry_date"].nunique()) if len(daily) else 0.0,
                "raw_avg_5d": float(raw_ret.mean()) if len(raw_ret) else 0.0,
                "raw_win_5d": float((raw_ret > 0).mean()) if len(raw_ret) else 0.0,
            }
        )
        for policy in POLICIES:
            variant = f"{daily_spec['variant']}_30m_accept_{policy['policy']}"
            desc = f"{daily_spec['desc_cn']}；{policy['desc_cn']}"
            for profile in PROFILES:
                candidates = prepare_policy(confirmed, profile, policy["policy"], variant, desc)
                curve, closed = simulate(candidates)
                run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
                closed_map[(variant, str(profile["profile"]))] = closed
                spec = {"variant": variant, "desc": desc}
                summary_rows.append(summarize(curve, closed, spec, str(profile["profile"])))
                window_rows.extend(window_metrics(curve, closed, variant, str(profile["profile"])))
            cost30 = [r for r in summary_rows if r.get("variant") == variant and r.get("profile") == "cost30"][-1]
            if int(cost30.get("trade_count", 0)) >= 30:
                key = (
                    float(cost30.get("total_return", 0.0)),
                    -abs(float(cost30.get("max_drawdown", 0.0))),
                    int(cost30.get("trade_count", 0)),
                )
                if best_key is None or key > best_key:
                    best_key = key
                    best_variant = variant
                    best_desc = desc

    confirm_cov = pd.DataFrame(confirm_rows)
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    confirm_cov.to_csv(OUT_DIR / "confirm_coverage.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    if not best_variant and len(summary):
        best_row = summary[summary["profile"].eq("cost30")].sort_values("total_return", ascending=False).head(1).iloc[0]
        best_variant = str(best_row["variant"])
        best_desc = str(best_row["desc"])

    best_closed = closed_map.get((best_variant, "cost30"), pd.DataFrame())
    yearly = group_by_year(best_closed)
    conc = concentration(best_closed)
    yearly.to_csv(OUT_DIR / "best_yearly_cost30.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "best_concentration_cost30.csv", index=False, encoding="utf-8-sig")

    keep_cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "exit_reason",
        "policy_net_ret",
        "realized_pnl",
        "range_pos60",
        "drawdown5",
        "drawdown10",
        "close_position",
        "amount_ratio20",
        "bar_time",
        "bar_close_pos",
        "bar_ret",
        "amount_ratio3",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
    ]
    keep_cols = [c for c in keep_cols if c in best_closed.columns]
    best_closed.sort_values("realized_pnl", ascending=False).head(20)[keep_cols].to_csv(
        OUT_DIR / "best_top_trades_cost30.csv", index=False, encoding="utf-8-sig"
    )
    best_closed.sort_values("policy_net_ret").head(20)[keep_cols].to_csv(
        OUT_DIR / "best_worst_trades_cost30.csv", index=False, encoding="utf-8-sig"
    )
    valid = best_closed[
        pd.to_datetime(best_closed.get("entry_date", pd.Series(dtype=str)), errors="coerce").between(
            pd.Timestamp("2024-01-01"), pd.Timestamp("2025-12-31")
        )
    ].copy()
    valid.sort_values("entry_date")[keep_cols].to_csv(OUT_DIR / "best_valid_2024_2025_trades_cost30.csv", index=False, encoding="utf-8-sig")

    write_report(daily_cov, confirm_cov, summary, windows, best_variant, best_desc, yearly, conc)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
