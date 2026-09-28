from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_validate_intraday_confirm import _label_signals, _load_minute_bars  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_four_path_independent_candidates" / "validation_v1" / "labeled_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_30m_volume_acceptance_v1"
INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 1
BASE_COST_BPS = 30.0

WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}

VARIANTS = [
    {
        "variant": "box_stress_accept_h5",
        "desc": "横盘箱体底部放量承接，持有5日",
        "family": "true_range_acceptance",
        "source": "box_stress",
        "hold_days": 5,
    },
    {
        "variant": "weak_low_accept_h5",
        "desc": "弱反弹低位不追高放量承接，持有5日",
        "family": "weak_rebound_acceptance",
        "source": "weak_low",
        "hold_days": 5,
    },
    {
        "variant": "weak_low_accept_h3",
        "desc": "弱反弹低位不追高放量承接，持有3日",
        "family": "weak_rebound_acceptance",
        "source": "weak_low",
        "hold_days": 3,
    },
]

PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "shock": 0.0},
    {"profile": "shock2_cost30", "cost_bps": 30.0, "shock": 0.02},
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
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


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def load_base() -> pd.DataFrame:
    d = pd.read_parquet(SOURCE).copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    numeric_cols = [
        "range_pos60",
        "up_rate",
        "breadth_ma20",
        "big_down_rate",
        "drawdown20",
        "runup_from_60d_low",
        "close_position",
        "lower_shadow_ratio",
        "amount_ratio20",
        "gap_open",
        "index_mom20",
        "mom10",
        "candidate_score",
        "entry_open",
        "fwd_ret_open_to_close_3d",
        "fwd_ret_open_to_close_5d",
    ]
    for col in numeric_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "g3_chain", "entry_open"]).copy()


def daily_source_mask(d: pd.DataFrame, source: str) -> pd.Series:
    reclaim = d["close_position"].ge(0.55) | d["lower_shadow_ratio"].ge(0.25)
    if source == "box_stress":
        return (
            d["g3_chain"].eq("range_box_bottom")
            & d["range_pos60"].le(0.20)
            & d["big_down_rate"].between(0.10, 0.35, inclusive="left")
            & reclaim
        )
    if source == "weak_low":
        return (
            d["g3_chain"].eq("weak_rebound_repair")
            & d["range_pos60"].between(0.10, 0.45, inclusive="both")
            & d["runup_from_60d_low"].le(0.30)
            & d["mom10"].le(0.10)
            & d["close_position"].ge(0.55)
            & d["index_mom20"].ge(-0.08)
        )
    raise ValueError(source)


def build_30m_acceptance(daily: pd.DataFrame, bars: pd.DataFrame | None = None) -> pd.DataFrame:
    if bars is None:
        bars = _load_minute_bars(daily, period=30)
    if bars.empty:
        return pd.DataFrame()
    ctx_cols = [
        "entry_date",
        "code",
        "name",
        "g3_chain",
        "candidate_score",
        "entry_open",
        "range_pos60",
        "runup_from_60d_low",
        "close_position",
        "amount_ratio20",
        "gap_open",
        "index_mom20",
    ]
    ctx = daily[[c for c in ctx_cols if c in daily.columns]].copy()
    joined = bars.merge(ctx, on=["entry_date", "code"], how="inner")
    joined = joined[joined["bar_time"].ge("10:00:00")].copy()
    if joined.empty:
        return joined

    prior_low = joined.groupby(["code", "entry_date"])["intraday_low_so_far"].shift(1)
    no_new_low = joined["low"] >= prior_low.fillna(joined["low"])
    break_prev = joined["close"] > joined["prev_bar_high"]
    reclaim = (
        (joined["close"] > joined["open"])
        & joined["bar_close_pos"].ge(0.65)
        & joined["amount_ratio3"].fillna(0.0).ge(1.20)
        & (break_prev | no_new_low)
    )
    out = joined[reclaim].copy()
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
    out["confirm_rule"] = "30m_volume_acceptance"
    return _label_signals(out)


def select_signals(base: pd.DataFrame, spec: dict[str, Any], bars: pd.DataFrame | None = None) -> pd.DataFrame:
    daily = base[daily_source_mask(base, str(spec["source"]))].copy()
    if daily.empty:
        return daily
    signals = build_30m_acceptance(daily, bars=bars)
    if signals.empty:
        return signals
    signals["variant"] = spec["variant"]
    signals["desc"] = spec["desc"]
    signals["family"] = spec["family"]
    signals["hold_days"] = int(spec["hold_days"])
    signals["rank_key"] = pd.to_numeric(signals.get("candidate_score", 0.0), errors="coerce").fillna(0.0) + pd.to_numeric(
        signals.get("amount_ratio3", 0.0), errors="coerce"
    ).fillna(0.0) * 0.02
    signals["rank_in_day"] = signals.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
    return signals[signals["rank_in_day"].le(DAILY_OPEN_LIMIT)].copy()


def exit_dates(entry_dates: pd.Series, hold_days: int) -> dict[pd.Timestamp, pd.Timestamp]:
    cal = _trade_calendar(pd.to_datetime(entry_dates).min(), pd.to_datetime(entry_dates).max() + pd.Timedelta(days=30))
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for idx, day in enumerate(cal):
        j = idx + hold_days - 1
        if j < len(cal):
            out[pd.Timestamp(day).normalize()] = pd.Timestamp(cal[j]).normalize()
    return out


def standardize(signals: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    if signals.empty:
        return signals
    d = signals.copy()
    hold_days = int(d["hold_days"].iloc[0])
    ret_col = f"fwd_ret_confirm_to_close_{hold_days}d"
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = d["entry_date_ts"].map(exit_dates(d["entry_date_ts"], hold_days))
    cost = float(profile["cost_bps"]) / 10000.0
    d["policy_net_ret"] = pd.to_numeric(d[ret_col], errors="coerce") - cost - float(profile["shock"])
    d["entry_price_used"] = pd.to_numeric(d.get("entry_price_adjusted", d.get("entry_price")), errors="coerce")
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret", "entry_price_used"]).copy()


def simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.sort_values(["entry_date_ts", "rank_key"], ascending=[True, False]).copy()
    cal = _trade_calendar(d["entry_date_ts"].min(), d["policy_exit_date"].max())
    by_day = {day: g.copy() for day, g in d.groupby("entry_date_ts")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    for day in cal:
        day = pd.Timestamp(day).normalize()
        still = []
        realized = 0.0
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                pnl = exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                closed.append(out)
                realized += pnl
            else:
                still.append(pos)
        open_pos = still
        todays = by_day.get(day)
        opened = 0
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= DAILY_OPEN_LIMIT or len(open_pos) >= SLOTS:
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * SLOT_PCT
                if stake <= 0 or cash < stake:
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1
        equity = cash + sum(float(p["stake"]) for p in open_pos)
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["stake"]) for p in open_pos),
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized,
            }
        )
    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str, desc: str) -> dict[str, Any]:
    if curve.empty:
        return {"variant": variant, "profile": profile, "desc": desc, "trade_count": 0}
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "desc": desc,
        "profile": profile,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": max_drawdown(curve["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
    }


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> list[dict[str, Any]]:
    rows = []
    for name, (start, end) in WINDOWS.items():
        part = curve[pd.to_datetime(curve["date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not curve.empty else pd.DataFrame()
        c = closed[pd.to_datetime(closed["entry_date_ts"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "window": name,
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0) if len(part) else 0.0,
                "max_drawdown": max_drawdown(part["equity"]) if len(part) else 0.0,
                "trade_count": int(len(c)),
                "win_rate": float((pd.to_numeric(c.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce") > 0).mean()) if len(c) else 0.0,
            }
        )
    return rows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    daily_by_source: dict[str, pd.DataFrame] = {}
    bars_by_source: dict[str, pd.DataFrame] = {}
    for source in sorted({str(spec["source"]) for spec in VARIANTS}):
        daily = base[daily_source_mask(base, source)].copy()
        daily_by_source[source] = daily
        bars_by_source[source] = _load_minute_bars(daily, period=30) if not daily.empty else pd.DataFrame()
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        daily = daily_by_source[str(spec["source"])]
        signals = select_signals(base, spec, bars=bars_by_source[str(spec["source"])])
        signals.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        coverage_rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "daily_candidates": int(len(daily)),
                "confirmed_signals": int(len(signals)),
                "confirm_rate": float(len(signals) / len(daily)) if len(daily) else 0.0,
                "signal_days": int(pd.Series(signals.get("entry_date", [])).nunique()) if not signals.empty else 0,
            }
        )
        for profile in PROFILES:
            candidates = standardize(signals, profile)
            curve, closed = simulate(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, str(spec["variant"]), str(profile["profile"]), str(spec["desc"])))
            window_rows.extend(window_metrics(curve, closed, str(spec["variant"]), str(profile["profile"])))
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    coverage = pd.DataFrame(coverage_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")
    pct_cols = {"confirm_rate", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    lines = [
        "# G3 横盘/弱反弹 30m 真实放量承接源 v1",
        "",
        "## 策略名解释",
        "",
        "- `box_stress_accept_h5`：中文是“横盘箱体底部放量承接，持有5日”。它先找日线处在箱体底部、市场有一定大跌出清、个股当日有反抽迹象的票，再要求盘中 30m K 线放量、收在高位、并突破前高或守住低点后买入。",
        "- `weak_low_accept_h5`：中文是“弱反弹低位不追高放量承接，持有5日”。它先找弱势反弹里的低位修复票，不追高，再要求 30m 真实放量承接。",
        "- `weak_low_accept_h3`：同上，但只持有3日，测试是否更适合短打。",
        "",
        "## 研究边界",
        "",
        "- 不使用 score/rank 做新过滤，只用日线结构生成候选，再用 30m 可见承接确认入场。",
        "- 30m 承接规则固定：10:00 后、阳线、收盘位置 >=65%、30m 成交额相对前三根均量 >=1.2、突破前一根高点或不再创新低。",
        "- 当前只是独立候选源研究，不接入 G2/G3 实盘，不打开自动交易。",
        "",
        "## 覆盖率",
        "",
        md_table(coverage, pct_cols=pct_cols),
        "",
        "## slot 复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口稳定性",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "",
        "- 如果 `cost30`、`cost100` 和 `shock2_cost30` 都为正，才说明 30m 放量承接具备候选源价值。",
        "- 如果正常收益为正但冲击压力为负，它只能作为 shadow 观察，不进入主策略。",
        "- 如果覆盖率很高但收益不改善，说明 30m 条件太宽；如果覆盖率太低，则暂时没有足够交易频率。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    clean_lines = [
        "# G3 横盘/弱反弹 30m 真实放量承接源 v1",
        "",
        "## 本轮结论",
        "",
        "- `box_stress_accept_h5`：中文含义是“横盘箱体底部放量承接，持有5日”。本轮 30bps 收益为正，但 100bps 和 2%低开冲击压力下转负，只能保留为横盘/箱体底部的研究线索，不能接入正式策略。",
        "- `weak_low_accept_h5`：中文含义是“弱反弹低位不追高放量承接，持有5日”。全周期和压力口径均明显为负，当前版本失败。",
        "- `weak_low_accept_h3`：中文含义是“弱反弹低位不追高放量承接，持有3日”。缩短持有期没有解决亏损问题，当前版本失败。",
        "",
        "## 策略名解释",
        "",
        "- `box_stress_accept_h5`：先找日线处在箱体底部、市场有一定大跌出清、个股当日有反抽迹象的票，再要求盘中 30m K 线放量、收在高位，并突破前一根高点或守住低点后买入，持有5个交易日。",
        "- `weak_low_accept_h5`：先找弱势反弹里的低位修复票，避免追高，再要求 30m 真实放量承接，持有5个交易日。",
        "- `weak_low_accept_h3`：同上，但只持有3个交易日，用来测试这类承接是否更适合短打。",
        "- `cost30`：单边约30bps交易成本口径。",
        "- `cost100`：单边约100bps高摩擦压力口径。",
        "- `shock2_cost30`：在30bps成本基础上额外扣除2%冲击，用来模拟次日低开、滑点或成交不利。",
        "",
        "## 研究边界",
        "",
        "- 本轮不使用新的 `score/rank` 过滤；日线只负责生成结构候选，30m 只负责验证真实放量承接。",
        "- 30m 承接规则固定为：10:00后、阳线、收盘位置 >=65%、30m成交额相对前三根均量 >=1.2、突破前一根高点或不再创日内新低。",
        "- 当前只是 G3 独立候选源研究，不接入 G2/G3 实盘，不打开自动交易。",
        "",
        "## 覆盖率",
        "",
        md_table(coverage, pct_cols=pct_cols),
        "",
        "## slot 复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口稳定性",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 下一步判断",
        "",
        "- `box_stress_accept_h5` 有一点可研究性，但必须继续找 2024-2025 失效原因和交易摩擦敏感原因。",
        "- `weak_low_accept_h5/h3` 不应继续微调止盈止损，应该回到候选源定义重建。",
        "- 下一步优先审计 `box_stress_accept_h5` 的失败交易：入场日是否冲高回落、确认bar是否尾盘、是否集中在高位题材退潮股。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(clean_lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
