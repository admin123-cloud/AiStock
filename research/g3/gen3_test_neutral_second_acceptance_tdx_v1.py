from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_tdx_minute_periods import _aggregate, _find_files, _iter_lc5, _parse_day  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_range_floor10_reclaim60_emotion_split_v1" / "neutral_only__cost30" / "closed_trades.csv"
OUT_DIR = ROOT / "reports" / "gen3_neutral_second_acceptance_tdx_v1"
TDX_ROOT = Path(r"D:\TDX\vipdoc")

INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 1
PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "shock": 0.0},
    {"profile": "shock2_cost30", "cost_bps": 30.0, "shock": 0.02},
]
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}
VARIANTS = [
    ("neutral_baseline", "neutral_only原始18笔", lambda d: pd.Series(True, index=d.index)),
    ("second_accept_any", "D0后半日或D1出现二次30m承接", lambda d: d["second_accept_any"].fillna(False)),
    ("d0_tail_second_accept", "D0确认后半日出现二次30m承接", lambda d: d["d0_tail_second_accept"].fillna(False)),
    ("d1_second_accept", "D1出现二次30m承接", lambda d: d["d1_second_accept"].fillna(False)),
    ("no_second_accept", "没有二次30m承接，用作反面对照", lambda d: ~d["second_accept_any"].fillna(False)),
]


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


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def load_trades() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["d1_date"] = pd.to_datetime(d["d1_date"], errors="coerce")
    d["entry_date_ts"] = d["entry_date"].dt.normalize()
    d["policy_exit_date"] = d["entry_date_ts"] + pd.Timedelta(days=8)
    for col in [
        "entry_price",
        "entry_price_adjusted",
        "fwd_ret_confirm_to_close_5d",
        "rank_key",
        "candidate_score",
        "amount_ratio3",
        "amount_ratio20",
        "range_pos60",
        "close_position",
        "policy_net_ret",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    if "rank_key" not in d.columns:
        d["rank_key"] = d["candidate_score"].fillna(0.0) + d["amount_ratio3"].fillna(0.0) * 0.02
    d["entry_price_used"] = d["entry_price_adjusted"]
    return d.dropna(subset=["code", "entry_date", "confirm_datetime", "fwd_ret_confirm_to_close_5d"]).copy()


def load_local_30m_bars(trades: pd.DataFrame) -> pd.DataFrame:
    if not TDX_ROOT.exists():
        return pd.DataFrame()
    codes = set(trades["code"].dropna().astype(str).str.upper())
    if not codes:
        return pd.DataFrame()
    start = pd.to_datetime(trades["entry_date"]).min().strftime("%Y-%m-%d")
    end = pd.to_datetime(trades["d1_date"].fillna(trades["entry_date"])).max().strftime("%Y-%m-%d")
    files = _find_files(TDX_ROOT, {"sh", "sz", "bj"}, codes, limit_files=0)
    rows = []
    start_dt = _parse_day(start)
    end_dt = _parse_day(end, end=True)
    for path in files:
        rows.extend(list(_iter_lc5(path, start_dt, end_dt)))
    if not rows:
        return pd.DataFrame()
    bars30 = _aggregate(rows, "30m")
    data = [
        {
            "code": bar.code,
            "datetime": pd.Timestamp(bar.dt),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "amount": bar.amount,
        }
        for bar in bars30
    ]
    bars = pd.DataFrame(data)
    if bars.empty:
        return bars
    bars["entry_date"] = bars["datetime"].dt.normalize()
    bars["bar_time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    bars = bars.sort_values(["code", "entry_date", "datetime"]).reset_index(drop=True)
    g = bars.groupby(["code", "entry_date"], sort=False)
    bars["prev_bar_high"] = g["high"].shift(1)
    bars["amount_ma3_prev"] = g["amount"].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    bar_range = bars["high"] - bars["low"]
    bars["bar_close_pos"] = np.where(bar_range > 0, (bars["close"] - bars["low"]) / bar_range, np.nan)
    bars["bar_ret"] = bars["close"] / bars["open"] - 1.0
    bars["amount_ratio3"] = bars["amount"] / bars["amount_ma3_prev"]
    return bars.replace([np.inf, -np.inf], np.nan)


def accept_mask(bars: pd.DataFrame) -> pd.Series:
    return (
        bars["close"].gt(bars["open"])
        & bars["bar_close_pos"].ge(0.65)
        & bars["amount_ratio3"].fillna(0.0).ge(1.2)
        & bars["close"].ge(bars["prev_bar_high"].fillna(bars["close"]))
    )


def enrich_second_acceptance(trades: pd.DataFrame, bars: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if bars.empty:
        out = trades.copy()
        for col in ["d0_tail_second_accept", "d1_second_accept", "second_accept_any"]:
            out[col] = False
        out["tdx_bars_loaded"] = False
        return out, pd.DataFrame()

    accept_events: list[dict[str, Any]] = []
    rows: list[pd.Series] = []
    for row in trades.itertuples(index=False):
        entry_day = pd.Timestamp(row.entry_date).normalize()
        d1_day = pd.Timestamp(row.d1_date).normalize() if not pd.isna(row.d1_date) else None
        code = str(row.code)
        confirm_dt = pd.Timestamp(row.confirm_datetime)
        entry_price = float(getattr(row, "entry_price", np.nan))
        sub = bars[bars["code"].eq(code)].copy()
        d0_later = sub[(sub["entry_date"].eq(entry_day)) & (sub["datetime"].gt(confirm_dt))].copy()
        d1 = sub[sub["entry_date"].eq(d1_day)].copy() if d1_day is not None else pd.DataFrame()
        d1 = d1[d1["bar_time"].ge("10:00:00")].copy() if not d1.empty else d1
        d0_mask = accept_mask(d0_later) & d0_later["close"].ge(entry_price)
        d1_mask = accept_mask(d1) & d1["close"].ge(entry_price)
        item = pd.Series(row._asdict())
        item["d0_tail_second_accept"] = bool(d0_mask.any())
        item["d1_second_accept"] = bool(d1_mask.any())
        item["second_accept_any"] = bool(item["d0_tail_second_accept"] or item["d1_second_accept"])
        item["tdx_bars_loaded"] = bool(len(d0_later) or len(d1))
        item["d0_tail_accept_count"] = int(d0_mask.sum()) if len(d0_later) else 0
        item["d1_accept_count"] = int(d1_mask.sum()) if len(d1) else 0
        rows.append(item)
        for scope, part, mask in [("d0_tail", d0_later, d0_mask), ("d1", d1, d1_mask)]:
            hits = part[mask].copy() if len(part) else pd.DataFrame()
            for hit in hits.itertuples(index=False):
                accept_events.append(
                    {
                        "entry_date": entry_day.strftime("%Y-%m-%d"),
                        "code": code,
                        "name": getattr(row, "name", ""),
                        "scope": scope,
                        "accept_datetime": hit.datetime,
                        "close": hit.close,
                        "bar_ret": hit.bar_ret,
                        "bar_close_pos": hit.bar_close_pos,
                        "amount_ratio3": hit.amount_ratio3,
                        "policy_net_ret": getattr(row, "policy_net_ret", np.nan),
                    }
                )
    enriched = pd.DataFrame(rows)
    events = pd.DataFrame(accept_events)
    return enriched, events


def local_calendar(start: pd.Timestamp, end: pd.Timestamp, extra: pd.Series) -> list[pd.Timestamp]:
    days = set(pd.bdate_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()).tolist())
    for day in pd.to_datetime(extra, errors="coerce").dropna():
        days.add(pd.Timestamp(day).normalize())
    return sorted(days)


def simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.sort_values(["entry_date_ts", "rank_key"], ascending=[True, False]).copy()
    cal = local_calendar(d["entry_date_ts"].min(), d["policy_exit_date"].max(), pd.concat([d["entry_date_ts"], d["policy_exit_date"]]))
    by_day = {pd.Timestamp(day).normalize(): g.copy() for day, g in d.groupby("entry_date_ts")}
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
        curve_rows.append({"date": day, "equity": equity, "cash": cash, "reserved_principal": sum(float(p["stake"]) for p in open_pos), "open_positions": len(open_pos), "opened": opened, "realized_pnl": realized})
    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def standardize(signals: pd.DataFrame, profile: dict[str, Any], variant: str, desc: str) -> pd.DataFrame:
    d = signals.copy()
    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    d["policy_net_ret"] = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce") - cost - shock
    d["variant"] = variant
    d["desc"] = desc
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret", "entry_price_used"]).copy()


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str, desc: str) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "desc": desc,
        "profile": profile,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0,
        "max_drawdown": max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
    }


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> list[dict[str, Any]]:
    rows = []
    for name, (start, end) in WINDOWS.items():
        part = curve[pd.to_datetime(curve["date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not curve.empty else pd.DataFrame()
        c = closed[pd.to_datetime(closed["entry_date_ts"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
        net = pd.to_numeric(c.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "window": name,
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0) if len(part) else 0.0,
                "max_drawdown": max_drawdown(part["equity"]) if len(part) else 0.0,
                "trade_count": int(len(c)),
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
            }
        )
    return rows


def build_diagnostic(enriched: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for flag in ["d0_tail_second_accept", "d1_second_accept", "second_accept_any"]:
        hit = enriched[enriched[flag].fillna(False)]
        miss = enriched[~enriched[flag].fillna(False)]
        rows.append(
            {
                "flag": flag,
                "中文解释": {
                    "d0_tail_second_accept": "D0确认后半日再次出现30m放量承接",
                    "d1_second_accept": "D1出现30m放量承接",
                    "second_accept_any": "D0后半日或D1任一二次承接",
                }[flag],
                "hit_count": int(len(hit)),
                "hit_sum_ret_cost30": float(hit["policy_net_ret"].sum()) if len(hit) else 0.0,
                "hit_avg_ret_cost30": float(hit["policy_net_ret"].mean()) if len(hit) else 0.0,
                "hit_win_rate": float((hit["policy_net_ret"] > 0).mean()) if len(hit) else 0.0,
                "miss_count": int(len(miss)),
                "miss_sum_ret_cost30": float(miss["policy_net_ret"].sum()) if len(miss) else 0.0,
                "miss_avg_ret_cost30": float(miss["policy_net_ret"].mean()) if len(miss) else 0.0,
                "miss_win_rate": float((miss["policy_net_ret"] > 0).mean()) if len(miss) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def write_report(enriched: pd.DataFrame, events: pd.DataFrame, diagnostic: pd.DataFrame, rank: pd.DataFrame, summary: pd.DataFrame, windows: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "cost30_return",
        "cost30_max_dd",
        "cost30_win_rate",
        "cost30_worst_trade",
        "cost100_return",
        "cost100_max_dd",
        "shock2_return",
        "shock2_max_dd",
        "hit_sum_ret_cost30",
        "hit_avg_ret_cost30",
        "hit_win_rate",
        "miss_sum_ret_cost30",
        "miss_avg_ret_cost30",
        "miss_win_rate",
        "policy_net_ret",
        "bar_ret",
        "bar_close_pos",
        "amount_ratio3",
    }
    lines = [
        "# G3 neutral_second_acceptance 本地TDX分钟复验 v1",
        "",
        "## 策略名解释",
        "",
        "- `neutral_second_acceptance`：中文是“中性情绪下的二次30m承接”。先有 `neutral_only` 的入场确认，再要求 D0确认后半日或 D1 出现第二次可见放量承接。",
        "- `d0_tail_second_accept`：D0买入确认之后，剩余交易时段再次出现30m放量承接。",
        "- `d1_second_accept`：下一交易日 D1 出现30m放量承接。",
        "- `second_accept_any`：上述二者任一触发。",
        "- `no_second_accept`：没有二次承接，用作反面对照。",
        "",
        "## 数据口径",
        "",
        "- ClickHouse 当前不可用，本轮使用本地 TDX `lc5` 文件读取 5m，并在脚本内聚合为30m。",
        "- 这是真实分钟数据的小样本复验，但没有做前复权精确校准；判断使用分钟原始价与原始确认价比较。",
        "- 本轮只验证方向，不作为正式策略参数。",
        "",
        "## 诊断结果",
        "",
        md_table(diagnostic, pct_cols=pct_cols),
        "",
        "## slot复算排名",
        "",
        md_table(rank, pct_cols=pct_cols),
        "",
        "## 二次承接事件",
        "",
        md_table(events, pct_cols=pct_cols),
        "",
        "## 完整结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 下一步判断",
        "",
        "- 如果 `second_accept_any` 明显优于 `neutral_baseline`，说明 neutral 可以继续按二次承接方向研究。",
        "- 如果 `second_accept_any` 过滤掉大赢家或冲击口径更差，说明二次承接不是答案，应停止在 neutral 上加过滤。",
        "- 若结果方向正向，下一步再恢复 ClickHouse 后做全量精确分钟复算。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    bars = load_local_30m_bars(trades)
    enriched, events = enrich_second_acceptance(trades, bars)
    enriched.to_csv(OUT_DIR / "neutral_enriched.csv", index=False, encoding="utf-8-sig")
    bars.to_csv(OUT_DIR / "local_tdx_30m_bars.csv", index=False, encoding="utf-8-sig")
    events.to_csv(OUT_DIR / "second_accept_events.csv", index=False, encoding="utf-8-sig")
    diagnostic = build_diagnostic(enriched)
    diagnostic.to_csv(OUT_DIR / "diagnostic.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    for variant, desc, mask_fn in VARIANTS:
        selected = enriched[mask_fn(enriched)].copy()
        selected.to_csv(OUT_DIR / f"{variant}_signals.csv", index=False, encoding="utf-8-sig")
        coverage_rows.append(
            {
                "variant": variant,
                "desc": desc,
                "signal_count": int(len(selected)),
                "signal_2026": int(selected["entry_date_ts"].dt.year.eq(2026).sum()) if len(selected) else 0,
            }
        )
        for profile in PROFILES:
            candidates = standardize(selected, profile, variant, desc)
            curve, closed = simulate(candidates)
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, variant, str(profile["profile"]), desc))
            window_rows.extend(window_metrics(curve, closed, variant, str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    coverage = pd.DataFrame(coverage_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    cost30 = summary[summary["profile"].eq("cost30")].copy()
    cost100 = summary[summary["profile"].eq("cost100")].copy()
    shock = summary[summary["profile"].eq("shock2_cost30")].copy()
    rank = (
        cost30[["variant", "desc", "trade_count", "total_return", "max_drawdown", "win_rate", "worst_trade"]]
        .rename(columns={"total_return": "cost30_return", "max_drawdown": "cost30_max_dd", "win_rate": "cost30_win_rate", "worst_trade": "cost30_worst_trade"})
        .merge(cost100[["variant", "total_return", "max_drawdown"]].rename(columns={"total_return": "cost100_return", "max_drawdown": "cost100_max_dd"}), on="variant")
        .merge(shock[["variant", "total_return", "max_drawdown"]].rename(columns={"total_return": "shock2_return", "max_drawdown": "shock2_max_dd"}), on="variant")
        .merge(coverage[["variant", "signal_2026"]], on="variant")
        .sort_values(["shock2_return", "cost100_return", "cost30_return"], ascending=[False, False, False])
    )
    rank.to_csv(OUT_DIR / "second_acceptance_rank.csv", index=False, encoding="utf-8-sig")
    write_report(enriched, events, diagnostic, rank, summary, windows)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
