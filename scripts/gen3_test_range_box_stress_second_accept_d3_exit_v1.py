from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    PROFILES,
    exit_dates,
    max_drawdown,
    simulate,
    summarize,
    window_metrics,
)
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_range_30m_volume_acceptance_v1" / "box_stress_accept_h5_signals.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_box_stress_second_accept_d3_exit_v1"

SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
CURVE_END = "2026-06-04"

VARIANTS = [
    {
        "variant": "box_stress_base_h5",
        "desc_cn": "箱体底部压力源，首个30m放量承接后固定持有到D5",
        "mode": "base",
        "policy": "hold5",
    },
    {
        "variant": "box_stress_second_accept_h5",
        "desc_cn": "箱体底部压力源，首个30m承接后，D0后半日或D1还要出现二次30m承接，固定持有到D5",
        "mode": "second_any",
        "policy": "hold5",
    },
    {
        "variant": "box_stress_reclaim60_second_accept_h5",
        "desc_cn": "箱体底部压力源，日线收盘修复>=60%，且D0后半日或D1二次30m承接，固定持有到D5",
        "mode": "reclaim60_second_any",
        "policy": "hold5",
    },
    {
        "variant": "box_stress_reclaim60_second_accept_d3neg",
        "desc_cn": "箱体底部压力源，日线收盘修复>=60%，且D0后半日或D1二次30m承接；若D3仍为负则D3退出，否则D5退出",
        "mode": "reclaim60_second_any",
        "policy": "d3_negative_exit",
    },
    {
        "variant": "box_stress_floor12_reclaim60_second_accept_d3neg",
        "desc_cn": "更深箱体底部，range_pos60<=12%，日线收盘修复>=60%，D0/D1二次30m承接；若D3仍为负则D3退出，否则D5退出",
        "mode": "floor12_reclaim60_second_any",
        "policy": "d3_negative_exit",
    },
    {
        "variant": "box_stress_floor20_reclaim55_second_accept_d3neg",
        "desc_cn": "箱体底部range_pos60<=20%，日线收盘修复>=55%，D0/D1二次30m承接；若D3仍为负则D3退出，否则D5退出",
        "mode": "floor20_reclaim55_second_any",
        "policy": "d3_negative_exit",
    },
]


def pct(value: Any, signed: bool = True) -> str:
    if value is None or pd.isna(value):
        return "--"
    sign = "+" if signed else ""
    return f"{float(value) * 100:{sign}.2f}%"


def money(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value):,.0f}"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None) -> str:
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


def sql_list(values: list[str]) -> str:
    return ",".join("'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)


def load_source() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["code"] = d["code"].astype(str)
    numeric_cols = [
        "close_position",
        "range_pos60",
        "amount_ratio3",
        "bar_close_pos",
        "bar_ret",
        "candidate_score",
        "rank_key",
        "entry_price_adjusted",
        "entry_price",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
    ]
    for col in numeric_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(
        subset=[
            "entry_date",
            "code",
            "confirm_datetime",
            "entry_price_adjusted",
            "fwd_ret_confirm_to_close_3d",
            "fwd_ret_confirm_to_close_5d",
        ]
    ).copy()


def load_following_30m(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame()
    codes = sorted(signals["code"].astype(str).unique())
    start = signals["entry_date"].min().strftime("%Y-%m-%d")
    end = (signals["entry_date"].max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    sql = f"""
    SELECT code, datetime, open, high, low, close, amount
    FROM kline_minute_30
    WHERE code IN ({sql_list(codes)})
      AND datetime >= toDateTime('{start} 09:30:00')
      AND datetime <= toDateTime('{end} 15:00:00')
    ORDER BY code, datetime
    """
    bars = clickhouse_query_df(sql)
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["bar_date"] = bars["datetime"].dt.normalize()
    for col in ["open", "high", "low", "close", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars["amount_ma3_prev"] = bars.groupby("code")["amount"].transform(lambda s: s.rolling(3, min_periods=3).mean().shift(1))
    bars["amount_ratio3_follow"] = bars["amount"] / bars["amount_ma3_prev"].replace(0, pd.NA)
    rng = (bars["high"] - bars["low"]).replace(0, pd.NA)
    bars["bar_close_pos_follow"] = (bars["close"] - bars["low"]) / rng
    bars["bar_ret_follow"] = bars["close"] / bars["open"].replace(0, pd.NA) - 1.0
    return bars


def attach_second_acceptance(signals: pd.DataFrame) -> pd.DataFrame:
    bars = load_following_30m(signals)
    out = signals.copy()
    out["d0_second_accept"] = False
    out["d1_second_accept"] = False
    out["d0_second_accept_datetime"] = pd.NaT
    out["d1_second_accept_datetime"] = pd.NaT
    if bars.empty:
        return out

    by_code = {code: g.copy() for code, g in bars.groupby("code")}
    rows: list[tuple[bool, bool, Any, Any]] = []
    for row in out.itertuples(index=False):
        code = str(getattr(row, "code"))
        entry_date = pd.Timestamp(getattr(row, "entry_date")).normalize()
        confirm_dt = pd.Timestamp(getattr(row, "confirm_datetime"))
        b = by_code.get(code, pd.DataFrame())
        if b.empty:
            rows.append((False, False, pd.NaT, pd.NaT))
            continue
        d0 = b[(b["bar_date"].eq(entry_date)) & (b["datetime"] > confirm_dt)].copy()
        future_dates = sorted(pd.Timestamp(x).normalize() for x in b.loc[b["bar_date"].gt(entry_date), "bar_date"].dropna().unique())
        d1 = b[b["bar_date"].eq(future_dates[0])].copy() if future_dates else pd.DataFrame()

        def first_accept(df: pd.DataFrame) -> pd.Timestamp | pd.NaT:
            if df.empty:
                return pd.NaT
            mask = (
                df["bar_ret_follow"].ge(0.0).fillna(False)
                & df["bar_close_pos_follow"].ge(0.70).fillna(False)
                & df["amount_ratio3_follow"].ge(1.30).fillna(False)
            )
            hit = df[mask].sort_values("datetime").head(1)
            return pd.NaT if hit.empty else pd.Timestamp(hit["datetime"].iloc[0])

        d0_hit = first_accept(d0)
        d1_hit = first_accept(d1)
        rows.append((pd.notna(d0_hit), pd.notna(d1_hit), d0_hit, d1_hit))

    flags = pd.DataFrame(rows, columns=["d0_second_accept", "d1_second_accept", "d0_second_accept_datetime", "d1_second_accept_datetime"])
    for col in flags.columns:
        out[col] = flags[col].values
    out["second_accept_any"] = out["d0_second_accept"] | out["d1_second_accept"]
    return out


def select_variant(base: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    mode = str(spec["mode"])
    if mode == "base":
        mask = pd.Series(True, index=base.index)
    elif mode == "second_any":
        mask = base["second_accept_any"]
    elif mode == "reclaim60_second_any":
        mask = base["close_position"].ge(0.60) & base["second_accept_any"]
    elif mode == "floor12_reclaim60_second_any":
        mask = base["range_pos60"].le(0.12) & base["close_position"].ge(0.60) & base["second_accept_any"]
    elif mode == "floor20_reclaim55_second_any":
        mask = base["range_pos60"].le(0.20) & base["close_position"].ge(0.55) & base["second_accept_any"]
    else:
        raise ValueError(mode)
    d = base[mask].copy()
    d["variant"] = spec["variant"]
    d["desc"] = spec["desc_cn"]
    d["family"] = "range_box_stress_second_accept"
    d["hold_days"] = 5
    d["rank_key"] = pd.to_numeric(d.get("rank_key", d.get("candidate_score", 0.0)), errors="coerce").fillna(0.0)
    d["rank_in_day"] = d.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
    return d.copy()


def standardize_policy(signals: pd.DataFrame, profile: dict[str, Any], policy: str) -> pd.DataFrame:
    if signals.empty:
        return signals.copy()
    d = signals.copy()
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    ret3 = pd.to_numeric(d["fwd_ret_confirm_to_close_3d"], errors="coerce")
    ret5 = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce")
    if policy == "hold5":
        d["policy_hold_days"] = 5
        d["policy_net_ret"] = ret5 - cost - shock
        d["exit_reason"] = "hold_d5"
    elif policy == "d3_negative_exit":
        early = ret3.lt(0.0)
        d["policy_hold_days"] = early.map({True: 3, False: 5}).astype(int)
        d["policy_net_ret"] = ret5.where(~early, ret3) - cost - shock
        d["exit_reason"] = early.map({True: "d3_negative_exit", False: "hold_d5"})
    else:
        raise ValueError(policy)
    maps: dict[int, dict[pd.Timestamp, pd.Timestamp]] = {
        3: exit_dates(d["entry_date_ts"], 3),
        5: exit_dates(d["entry_date_ts"], 5),
    }
    d["policy_exit_date"] = [maps[int(h)].get(pd.Timestamp(day).normalize()) for day, h in zip(d["entry_date_ts"], d["policy_hold_days"])]
    d["entry_price_used"] = pd.to_numeric(d.get("entry_price_adjusted", d.get("entry_price")), errors="coerce")
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret", "entry_price_used"]).copy()


def coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        d = select_variant(base, spec)
        rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc_cn"],
                "signal_count": int(len(d)),
                "signal_days": int(d["entry_date"].nunique()) if len(d) else 0,
                "d0_second": int(d["d0_second_accept"].sum()) if "d0_second_accept" in d else 0,
                "d1_second": int(d["d1_second_accept"].sum()) if "d1_second_accept" in d else 0,
                "raw_avg_3d": float(pd.to_numeric(d.get("fwd_ret_confirm_to_close_3d", pd.Series(dtype=float)), errors="coerce").mean()) if len(d) else 0.0,
                "raw_avg_5d": float(pd.to_numeric(d.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce").mean()) if len(d) else 0.0,
                "raw_win_5d": float((pd.to_numeric(d.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce") > 0).mean()) if len(d) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def add_window(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["entry_dt"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["year"] = d["entry_dt"].dt.year
    d["window"] = "train_2020_2023"
    d.loc[d["entry_dt"].between(pd.Timestamp("2024-01-01"), pd.Timestamp("2025-12-31")), "window"] = "valid_2024_2025"
    d.loc[d["entry_dt"].ge(pd.Timestamp("2026-01-01")), "window"] = "blind_2026ytd"
    return d


def group_returns(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, g in add_window(df).groupby(by, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        rets = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        row = {col: val for col, val in zip(by, key)}
        row.update(
            {
                "trade_count": int(len(g)),
                "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
                "avg_return": float(rets.mean()) if len(rets) else 0.0,
                "worst_return": float(rets.min()) if len(rets) else 0.0,
                "best_return": float(rets.max()) if len(rets) else 0.0,
                "pnl": float(pd.to_numeric(g.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def concentration(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values("realized_pnl", ascending=False).copy()
    total = float(pd.to_numeric(d["realized_pnl"], errors="coerce").sum())
    rows = []
    for n in [0, 1, 3, 5, 10]:
        rest = d.iloc[n:].copy()
        rets = pd.to_numeric(rest["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "exclude_top_n": n,
                "remaining_trades": int(len(rest)),
                "remaining_pnl": float(pd.to_numeric(rest.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()),
                "remaining_pnl_share": float(pd.to_numeric(rest.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum() / total) if total else 0.0,
                "remaining_avg_return": float(rets.mean()) if len(rets) else 0.0,
                "remaining_win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, cov: pd.DataFrame, best: str, best_cn: str, conc: pd.DataFrame, yearly: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "raw_avg_3d",
        "raw_avg_5d",
        "raw_win_5d",
        "avg_return",
        "worst_return",
        "best_return",
        "remaining_pnl_share",
        "remaining_avg_return",
        "remaining_win_rate",
    }
    best_summary = summary[summary["variant"].eq(best)].copy()
    best_windows = windows[windows["variant"].eq(best)].copy()
    lines = [
        "# G3 v4 横盘/箱体压力源二次承接与D3弱退出复验",
        "",
        "## 回测范围",
        f"- 信号入场窗口：{SIGNAL_START} 至 {SIGNAL_END}。",
        f"- 曲线结算窗口：{SIGNAL_START} 至 {CURVE_END}。",
        "- 初始资金：150000；5个槽位；单槽20%；同日最多开1笔。",
        "- 本轮不使用新的 score/rank 过滤，只复验结构条件：二次30m承接、日线修复、D3早期弱确认退出。",
        "",
        "## 英文策略名解释",
        "- `box_stress_base_h5`：箱体底部压力源基础版。意思是先在横盘/箱体底部遇到压力释放，再等首个30m放量承接，买入后固定持有到D5。",
        "- `second_accept`：二次承接。意思是首个30m承接后，入场日后半天或次日还要再出现一根放量、收在高位、非阴线的30m K线，用来确认不是一根脉冲。",
        "- `reclaim60`：日线修复到60%以上。意思是当天收盘位于日内区间较高位置，恐慌后已经有明显修复。",
        "- `floor12` / `floor20`：60日箱体位置不高于12%或20%，越低表示越接近箱体底部。",
        "- `d3neg`：D3弱退出。意思是买入后到第3个交易日仍为负收益，就在D3退出；否则继续持有到D5。",
        "- `cost30`：按30bps交易摩擦；`cost100`：按100bps高摩擦；`shock2_cost30`：在30bps基础上额外扣2%低开/冲击压力。",
        "",
        "## 覆盖率",
        md_table(cov, pct_cols=pct_cols),
        "",
        "## 全部方案slot复算",
        md_table(summary, pct_cols=pct_cols),
        "",
        f"## 当前相对最好方案：`{best}`",
        f"- 中文含义：{best_cn}",
        md_table(best_summary, pct_cols=pct_cols),
        "",
        "## 最好方案分窗口",
        md_table(best_windows, pct_cols=pct_cols),
        "",
        "## 最好方案年度拆分（cost30）",
        md_table(yearly, pct_cols=pct_cols, money_cols={"pnl"}),
        "",
        "## 最好方案集中度（cost30）",
        md_table(conc, pct_cols=pct_cols, money_cols={"remaining_pnl"}),
        "",
        "## 初步判断",
        "- 这轮检验的重点不是调止盈止损，而是验证“更宽箱体源能不能用二次承接和D3弱退出变成可用策略”。",
        "- 若验证段和压力口径仍不能站住，则说明横盘/箱体源需要重建更独立的候选定义，而不是继续在这137笔旧源上加过滤。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = attach_second_acceptance(load_source())
    base.to_csv(OUT_DIR / "base_with_second_acceptance_flags.csv", index=False, encoding="utf-8-sig")
    cov = coverage(base)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    best_key: tuple[float, float, int] | None = None
    best_variant = ""
    best_cn = ""
    closed_by_variant_profile: dict[tuple[str, str], pd.DataFrame] = {}
    for spec in VARIANTS:
        selected = select_variant(base, spec)
        selected.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            candidates = standardize_policy(selected, profile, str(spec["policy"]))
            curve, closed = simulate(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            closed_by_variant_profile[(str(spec["variant"]), str(profile["profile"]))] = closed
            summary_rows.append(summarize(curve, closed, str(spec["variant"]), str(profile["profile"]), str(spec["desc_cn"])))
            window_rows.extend(window_metrics(curve, closed, str(spec["variant"]), str(profile["profile"])))
        cost30_closed = closed_by_variant_profile[(str(spec["variant"]), "cost30")]
        cost30_summary = [r for r in summary_rows if r.get("variant") == spec["variant"] and r.get("profile") == "cost30"][-1]
        if int(cost30_summary.get("trade_count", 0)) >= 20:
            key = (
                float(cost30_summary.get("total_return", 0.0)),
                -abs(float(cost30_summary.get("max_drawdown", 0.0))),
                int(len(cost30_closed)),
            )
            if best_key is None or key > best_key:
                best_key = key
                best_variant = str(spec["variant"])
                best_cn = str(spec["desc_cn"])

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    if not best_variant:
        best_variant = "box_stress_base_h5"
        best_cn = VARIANTS[0]["desc_cn"]
    best_closed = closed_by_variant_profile[(best_variant, "cost30")]
    yearly = group_returns(best_closed, ["year"])
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
        "close_position",
        "amount_ratio3",
        "bar_close_pos",
        "bar_ret",
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
    add_window(best_closed).query("window == 'valid_2024_2025'").sort_values("entry_date")[keep_cols].to_csv(
        OUT_DIR / "best_valid_2024_2025_trades_cost30.csv", index=False, encoding="utf-8-sig"
    )
    result = {
        "signal_window": [SIGNAL_START, SIGNAL_END],
        "curve_window": [SIGNAL_START, CURVE_END],
        "initial_capital": INITIAL_CAPITAL,
        "best_variant": best_variant,
        "best_desc_cn": best_cn,
    }
    (OUT_DIR / "result.json").write_text(pd.Series(result).to_json(force_ascii=False, indent=2), encoding="utf-8")
    write_report(summary, windows, cov, best_variant, best_cn, conc, yearly)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
