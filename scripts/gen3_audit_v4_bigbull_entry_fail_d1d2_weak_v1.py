from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_v4_bigbull_structural_context_v1" / "bigbull_context_enriched.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_bigbull_entry_fail_d1d2_weak_v1"
COST_BPS = 30.0
WEAK_1030_RET = -0.03


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_struct_bigbull() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in [
        "entry_price",
        "calc_ret_5d",
        "calc_ret_10d",
        "calc_mfe_5d",
        "calc_mae_5d",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "l3_rt_strong3_ratio",
        "breadth_ma20",
        "box_top",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    gate = (
        d["index_ge_ma20"].fillna(False).astype(bool)
        & d["breadth_ma20"].ge(0.50)
        & d["l3_rt_strong3_ratio"].ge(0.10)
        & d["rt_return_from_d1_close"].ge(0.06)
        & d["rt_breakout_vs_box_top"].ge(0.025)
        & d["rt_breakout_vs_box_top"].le(0.08)
    )
    return d[gate].dropna(subset=["entry_date", "code", "entry_price", "calc_ret_5d"]).copy()


def add_d1_d2_dates(d: pd.DataFrame) -> pd.DataFrame:
    calendar = _trade_calendar(d["entry_date"].min(), d["entry_date"].max() + pd.Timedelta(days=20))
    next_map = {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}
    out = d.copy()
    out["d1_date"] = out["entry_date"].map(next_map)
    out["d2_date"] = out["d1_date"].map(next_map)
    return out


def load_daily(candidates: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(candidates["code"].dropna().astype(str).unique().tolist())
    dates = set()
    for col in ["entry_date", "d1_date", "d2_date"]:
        dates.update(pd.to_datetime(candidates[col], errors="coerce").dropna().dt.strftime("%Y-%m-%d").tolist())
    dates_sorted = sorted(dates)
    parts: list[pd.DataFrame] = []
    for di in range(0, len(dates_sorted), 120):
        date_list = ",".join(f"toDate({_sql_literal(x)})" for x in dates_sorted[di : di + 120])
        for ci in range(0, len(codes), 250):
            quoted = ",".join(_sql_literal(x) for x in codes[ci : ci + 250])
            sql = f"""
            SELECT code, trade_date, open, high, low, close
            FROM kline_daily
            WHERE code IN ({quoted})
              AND trade_date IN ({date_list})
            ORDER BY code, trade_date
            """
            part = clickhouse_query_df(sql)
            if not part.empty:
                parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        return daily
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    return daily.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).copy()


def load_30m(candidates: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(candidates["code"].dropna().astype(str).unique().tolist())
    dates = set()
    for col in ["d1_date", "d2_date"]:
        dates.update(pd.to_datetime(candidates[col], errors="coerce").dropna().dt.strftime("%Y-%m-%d").tolist())
    dates_sorted = sorted(dates)
    parts: list[pd.DataFrame] = []
    for di in range(0, len(dates_sorted), 120):
        date_list = ",".join(f"toDate({_sql_literal(x)})" for x in dates_sorted[di : di + 120])
        for ci in range(0, len(codes), 250):
            quoted = ",".join(_sql_literal(x) for x in codes[ci : ci + 250])
            sql = f"""
            SELECT code, datetime, open, high, low, close
            FROM kline_minute_30
            WHERE code IN ({quoted})
              AND toDate(datetime) IN ({date_list})
            ORDER BY code, datetime
            """
            part = clickhouse_query_df(sql)
            if not part.empty:
                parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trade_date"] = bars["datetime"].dt.normalize()
    bars["time_text"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["code", "datetime", "trade_date", "open", "high", "low", "close"]).copy()


def attach_daily_features(candidates: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    out = candidates.copy()
    for label, date_col in [("entry", "entry_date"), ("d1", "d1_date"), ("d2", "d2_date")]:
        part = daily.rename(columns={"trade_date": date_col}).copy()
        keep = ["code", date_col, "open", "high", "low", "close"]
        part = part[keep].rename(columns={c: f"{label}_{c}" for c in ["open", "high", "low", "close"]})
        out = out.merge(part, on=["code", date_col], how="left")
    ep = pd.to_numeric(out["entry_price"], errors="coerce")
    out["entry_high_ret"] = out["entry_high"] / ep - 1.0
    out["entry_close_ret"] = out["entry_close"] / ep - 1.0
    out["entry_low_ret"] = out["entry_low"] / ep - 1.0
    out["entry_high_to_close_giveback"] = (out["entry_high"] - out["entry_close"]) / ep
    out["entry_close_vs_box_top"] = out["entry_close"] / out["box_top"] - 1.0
    out["d1_close_ret"] = out["d1_close"] / ep - 1.0
    out["d2_close_ret"] = out["d2_close"] / ep - 1.0
    return out


def attach_1030_features(candidates: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    out = candidates.copy()
    if bars.empty:
        out["d1_1030_ret"] = pd.NA
        out["d2_1030_ret"] = pd.NA
        return out
    by_key = {(str(code), pd.Timestamp(day)): g.sort_values("datetime") for (code, day), g in bars.groupby(["code", "trade_date"])}
    d1_vals: list[Any] = []
    d2_vals: list[Any] = []
    for row in out.itertuples(index=False):
        ep = float(getattr(row, "entry_price") or 0.0)
        vals: list[Any] = []
        for col in ["d1_date", "d2_date"]:
            day = getattr(row, col)
            g = by_key.get((str(getattr(row, "code")), pd.Timestamp(day))) if pd.notna(day) else None
            if g is None or g.empty or ep <= 0:
                vals.append(pd.NA)
                continue
            usable = g[g["time_text"] <= "10:30:00"]
            if usable.empty:
                vals.append(pd.NA)
            else:
                vals.append(float(usable.iloc[-1]["close"]) / ep - 1.0)
        d1_vals.append(vals[0])
        d2_vals.append(vals[1])
    out["d1_1030_ret"] = d1_vals
    out["d2_1030_ret"] = d2_vals
    return out


def add_warning_labels(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    out["entry_intraday_giveback_warn"] = out["entry_high_ret"].ge(0.05) & out["entry_high_to_close_giveback"].ge(0.04)
    out["entry_breakout_fail_warn"] = out["entry_close_vs_box_top"].le(0.0) | out["entry_close_ret"].le(0.0)
    out["d1_1030_weak"] = pd.to_numeric(out["d1_1030_ret"], errors="coerce").le(WEAK_1030_RET)
    out["d2_1030_weak"] = pd.to_numeric(out["d2_1030_ret"], errors="coerce").le(WEAK_1030_RET)
    out["d1_close_weak"] = pd.to_numeric(out["d1_close_ret"], errors="coerce").le(0.0)
    out["d2_close_weak"] = pd.to_numeric(out["d2_close_ret"], errors="coerce").le(0.0)
    out["entry_fail_plus_d1_weak"] = (
        (out["entry_intraday_giveback_warn"] | out["entry_breakout_fail_warn"])
        & (out["d1_1030_weak"] | out["d1_close_weak"])
    )
    out["d1d2_persistent_weak"] = (out["d1_1030_weak"] | out["d1_close_weak"]) & (out["d2_1030_weak"] | out["d2_close_weak"])
    out["base_policy_ret"] = pd.to_numeric(out["calc_ret_5d"], errors="coerce") - COST_BPS / 10000.0
    out["haircut2_policy_ret"] = out["base_policy_ret"] - 0.02
    out["d1_1030_exit_ret"] = pd.to_numeric(out["d1_1030_ret"], errors="coerce") - COST_BPS / 10000.0
    out["d2_1030_exit_ret"] = pd.to_numeric(out["d2_1030_ret"], errors="coerce") - COST_BPS / 10000.0
    return out


def summarize_flag(d: pd.DataFrame, flag: str) -> dict[str, Any]:
    g = d[d[flag].fillna(False).astype(bool)].copy()
    base = pd.to_numeric(d["base_policy_ret"], errors="coerce")
    ret = pd.to_numeric(g["base_policy_ret"], errors="coerce")
    return {
        "flag": flag,
        "trigger_count": int(len(g)),
        "coverage": float(len(g) / len(d)) if len(d) else 0.0,
        "base_ret_mean_all": float(base.mean()),
        "trigger_ret_mean": float(ret.mean()) if len(g) else pd.NA,
        "trigger_win_rate": float((ret > 0).mean()) if len(g) else pd.NA,
        "trigger_bad_m5_rate": float((ret <= -0.05).mean()) if len(g) else pd.NA,
        "non_trigger_ret_mean": float(pd.to_numeric(d.loc[~d[flag].fillna(False).astype(bool), "base_policy_ret"], errors="coerce").mean()),
    }


def policy_summary(d: pd.DataFrame) -> pd.DataFrame:
    policies = {
        "base_hold5": pd.to_numeric(d["base_policy_ret"], errors="coerce"),
        "exit_d1_1030_weak": pd.to_numeric(d["base_policy_ret"], errors="coerce").where(~d["d1_1030_weak"], d["d1_1030_exit_ret"]),
        "exit_entry_fail_plus_d1_weak": pd.to_numeric(d["base_policy_ret"], errors="coerce").where(
            ~d["entry_fail_plus_d1_weak"], d["d1_1030_exit_ret"]
        ),
        "exit_d1d2_persistent_weak": pd.to_numeric(d["base_policy_ret"], errors="coerce").where(
            ~d["d1d2_persistent_weak"], d["d2_1030_exit_ret"].combine_first(d["base_policy_ret"])
        ),
    }
    rows: list[dict[str, Any]] = []
    for policy, ret in policies.items():
        rows.append(
            {
                "policy": policy,
                "count": int(ret.notna().sum()),
                "avg_ret": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "bad_m5_rate": float((ret <= -0.05).mean()),
                "worst_ret": float(ret.min()),
                "haircut2_avg_ret": float((ret - 0.02).mean()),
            }
        )
    return pd.DataFrame(rows).sort_values("avg_ret", ascending=False)


def exit_delta_summary(d: pd.DataFrame) -> pd.DataFrame:
    rules = [
        ("d1_1030_weak", "d1_1030_exit_ret"),
        ("entry_fail_plus_d1_weak", "d1_1030_exit_ret"),
        ("d1d2_persistent_weak", "d2_1030_exit_ret"),
    ]
    rows: list[dict[str, Any]] = []
    for flag, exit_col in rules:
        g = d[d[flag].fillna(False).astype(bool)].copy()
        if g.empty:
            rows.append({"rule": flag, "trigger_count": 0})
            continue
        exit_ret = pd.to_numeric(g[exit_col], errors="coerce").fillna(g["base_policy_ret"])
        base_ret = pd.to_numeric(g["base_policy_ret"], errors="coerce")
        delta = exit_ret - base_ret
        rows.append(
            {
                "rule": flag,
                "trigger_count": int(len(g)),
                "base_avg": float(base_ret.mean()),
                "exit_avg": float(exit_ret.mean()),
                "delta_avg": float(delta.mean()),
                "saved_loss_count": int(((base_ret < 0) & (delta > 0)).sum()),
                "harmed_winner_count": int(((base_ret > 0) & (delta < 0)).sum()),
                "deepened_loss_count": int(((base_ret < 0) & (delta < 0)).sum()),
                "improved_count": int((delta > 0).sum()),
                "worsened_count": int((delta < 0).sum()),
            }
        )
    return pd.DataFrame(rows)


def worst_table(d: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "entry_date",
        "code",
        "name",
        "base_policy_ret",
        "calc_ret_10d",
        "entry_high_ret",
        "entry_close_ret",
        "entry_high_to_close_giveback",
        "entry_close_vs_box_top",
        "d1_1030_ret",
        "d1_close_ret",
        "d2_1030_ret",
        "d2_close_ret",
        "entry_intraday_giveback_warn",
        "entry_breakout_fail_warn",
        "entry_fail_plus_d1_weak",
        "d1d2_persistent_weak",
    ]
    out = d.sort_values("base_policy_ret", ascending=True).head(20).copy()
    out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return out[[c for c in cols if c in out.columns]]


def write_report(d: pd.DataFrame, flags: pd.DataFrame, policies: pd.DataFrame, deltas: pd.DataFrame) -> None:
    pct_cols = {
        "coverage",
        "base_ret_mean_all",
        "trigger_ret_mean",
        "trigger_win_rate",
        "trigger_bad_m5_rate",
        "non_trigger_ret_mean",
        "avg_ret",
        "win_rate",
        "bad_m5_rate",
        "worst_ret",
        "haircut2_avg_ret",
        "base_avg",
        "exit_avg",
        "delta_avg",
    }
    lines = [
        "# G3 V4 big_bull 入场失败 + D1/D2 早期弱确认审计 v1",
        "",
        "## 本轮完成",
        "",
        "- 对结构门槛 big_bull 样本做执行质量拆解，不使用 `score/rank` 过滤。",
        "- 回填入场日日线 OHLC、D1/D2 日线 OHLC、D1/D2 10:30 的 30m 可见收盘。",
        "- 目标是判断：入场日冲高回落/突破失败 + D1/D2 早期弱确认，是否能作为减仓或快速退出方向。",
        "",
        "## 触发标签表现",
        "",
        md_table(flags, pct_cols=pct_cols),
        "",
        "## 快速退出代理",
        "",
        md_table(policies, pct_cols=pct_cols),
        "",
        "## 退出救损/误伤拆解",
        "",
        md_table(deltas, pct_cols=pct_cols),
        "",
        "## 最差样本",
        "",
        md_table(worst_table(d)),
        "",
        "## 下一步目标",
        "",
        "- 如果某个弱确认规则能提高均值、降低大亏率，下一步才进入组合级轻仓/退出复算。",
        "- 如果触发组并不是亏损来源，说明 big_bull 的脆弱点不是 D1/D2 走弱，而可能是仓位冲突或市场风格切换。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = add_d1_d2_dates(load_struct_bigbull())
    d = attach_daily_features(d, load_daily(d))
    d = attach_1030_features(d, load_30m(d))
    d = add_warning_labels(d)
    flag_names = [
        "entry_intraday_giveback_warn",
        "entry_breakout_fail_warn",
        "d1_1030_weak",
        "d1_close_weak",
        "d2_1030_weak",
        "d2_close_weak",
        "entry_fail_plus_d1_weak",
        "d1d2_persistent_weak",
    ]
    flags = pd.DataFrame([summarize_flag(d, flag) for flag in flag_names])
    policies = policy_summary(d)
    deltas = exit_delta_summary(d)
    d.to_csv(OUT_DIR / "entry_fail_d1d2_enriched.csv", index=False, encoding="utf-8-sig")
    flags.to_csv(OUT_DIR / "flag_summary.csv", index=False, encoding="utf-8-sig")
    policies.to_csv(OUT_DIR / "policy_summary.csv", index=False, encoding="utf-8-sig")
    deltas.to_csv(OUT_DIR / "exit_delta_summary.csv", index=False, encoding="utf-8-sig")
    write_report(d, flags, policies, deltas)
    print(f"done: {OUT_DIR}")
    print(flags.to_string(index=False))
    print(policies.to_string(index=False))


if __name__ == "__main__":
    main()
