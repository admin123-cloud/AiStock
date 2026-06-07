from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_range_ice_recent3_entry_quality_v1" / "ice_recent3_base.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_reversal_gate_d1d2_exit_v1"
INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 1
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}

SOURCE_VARIANTS = [
    {
        "variant": "all_reversal_pool",
        "desc": "不做入场质量过滤（原始样本）",
        "warning_filter": "none",
    },
    {
        "variant": "reversal_only",
        "desc": "只保留入场日冲高回落/突破失败候选",
        "warning_filter": "only_warning",
    },
    {
        "variant": "reversal_filtered",
        "desc": "剔除入场日冲高回落/突破失败候选",
        "warning_filter": "drop_warning",
    },
]

POLICIES = [
    {
        "policy": "hold5_baseline",
        "desc": "入场后默认持有5日收盘",
        "skip_warning": False,
        "warn_d1exit": False,
        "warn_d2exit": False,
        "warn_exit_if_weak": False,
        "weak_d1d2_exit": False,
    },
    {
        "policy": "warn_fast_d1",
        "desc": "警戒信号启用：D1收盘触发快速退出",
        "skip_warning": False,
        "warn_d1exit": True,
        "warn_d2exit": False,
        "warn_exit_if_weak": False,
        "weak_d1d2_exit": False,
    },
    {
        "policy": "warn_fast_d1d2",
        "desc": "警戒信号启用：D1收盘弱确认，D2再弱则继续等待D2收盘退出",
        "skip_warning": False,
        "warn_d1exit": True,
        "warn_d2exit": True,
        "warn_exit_if_weak": False,
        "weak_d1d2_exit": False,
    },
    {
        "policy": "weak_d1d2_exit",
        "desc": "不看警戒分类，所有样本执行D1/D2早期弱确认快速退出",
        "skip_warning": False,
        "warn_d1exit": False,
        "warn_d2exit": False,
        "warn_exit_if_weak": True,
        "weak_d1d2_exit": True,
    },
    {
        "policy": "drop_warning",
        "desc": "直接剔除警戒样本，减少入场失败",
        "skip_warning": True,
        "warn_d1exit": False,
        "warn_d2exit": False,
        "warn_exit_if_weak": False,
        "weak_d1d2_exit": False,
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


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def load_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    for col in [
        "entry_price",
        "confirm_open",
        "confirm_high",
        "confirm_low",
        "prev_bar_high",
        "prev_bar_low",
        "intraday_high_so_far",
        "intraday_low_so_far",
        "open_range_high",
        "bar_close_pos",
        "bar_ret",
        "amount_ratio3",
        "entry_open",
        "close_position",
        "amount_ratio20",
        "gap_open",
        "index_mom20",
        "candidate_score",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "entry_price_adjusted",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    for col in [
        "exit_close_1d",
        "exit_close_2d",
        "exit_close_3d",
        "exit_close_5d",
        "confirm_amount",
        "amount",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    d["entry_price_adjusted"] = d["entry_price_adjusted"].fillna(d["entry_price"])
    d = d.dropna(subset=["entry_date", "code", "entry_price_adjusted", "entry_price"]).copy()
    d["g3_chain"] = d["g3_chain"].fillna("")
    d["variant"] = d["variant"].fillna("")

    d["d0_break_attempt"] = d["confirm_high"] > d["prev_bar_high"]
    d["d0_retracement"] = d["entry_price"] <= (d["confirm_high"] * 0.995)
    d["d0_high_pullback"] = (d["bar_ret"] > 0.02) & (d["bar_close_pos"] <= 0.55)
    d["entry_reversal_warning"] = d["d0_break_attempt"] & (d["d0_retracement"] | d["d0_high_pullback"])

    d["d1_weak_close"] = d["fwd_ret_confirm_to_close_1d"] <= -0.03
    d["d2_weak_close"] = d["fwd_ret_confirm_to_close_2d"] <= -0.03

    return d


def select_by_warning(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    if mode == "none":
        return df
    if mode == "only_warning":
        return df[df["entry_reversal_warning"]].copy()
    if mode == "drop_warning":
        return df[~df["entry_reversal_warning"]].copy()
    raise ValueError(mode)


def exit_dates(entry_dates: pd.Series, hold_days: int) -> dict[pd.Timestamp, pd.Timestamp]:
    entry_dates = pd.to_datetime(entry_dates).dropna().drop_duplicates().sort_values()
    cal = _trade_calendar(entry_dates.min(), entry_dates.max() + pd.Timedelta(days=40))
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    pos = {pd.Timestamp(day).normalize(): idx for idx, day in enumerate(cal)}
    for day in entry_dates.dt.normalize():
        idx = pos.get(day)
        if idx is None:
            continue
        j = idx + hold_days - 1
        if j < len(cal):
            out[pd.Timestamp(day).normalize()] = pd.Timestamp(cal[j]).normalize()
    return out


def apply_policy(base: pd.DataFrame, policy: dict[str, Any], profile: dict[str, Any]) -> pd.DataFrame:
    d = base.copy()
    raw = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce").copy()
    reason = pd.Series("hold5", index=d.index)

    warn = d["entry_reversal_warning"].fillna(False)
    hold5_exit = exit_dates(d["entry_date"], 5)
    d1_exit = exit_dates(d["entry_date"], 2)
    d2_exit = exit_dates(d["entry_date"], 3)

    if bool(policy["warn_d1exit"]):
        mask = warn & d["d1_weak_close"] & d["d1_weak_close"].notna()
        raw.loc[mask] = pd.to_numeric(d.loc[mask, "fwd_ret_confirm_to_close_1d"], errors="coerce")
        d.loc[mask, "policy_exit_date"] = d.loc[mask, "entry_date"].map(d1_exit)
        reason.loc[mask] = "d1_weak_exit"

    if bool(policy["warn_d1exit"]) and bool(policy["warn_d2exit"]):
        mask2 = (
            warn
            & d["d1_weak_close"].fillna(False)
            & d["d2_weak_close"]
            & d["d2_weak_close"].notna()
        )
        raw.loc[mask2] = pd.to_numeric(d.loc[mask2, "fwd_ret_confirm_to_close_2d"], errors="coerce")
        d.loc[mask2, "policy_exit_date"] = d.loc[mask2, "entry_date"].map(d2_exit)
        reason.loc[mask2] = "d1_d2_weak_exit"

    if bool(policy["weak_d1d2_exit"]):
        mask = d["d1_weak_close"].fillna(False) & d["d1_weak_close"].notna()
        raw.loc[mask] = pd.to_numeric(d.loc[mask, "fwd_ret_confirm_to_close_1d"], errors="coerce")
        d.loc[mask, "policy_exit_date"] = d.loc[mask, "entry_date"].map(d1_exit)
        reason.loc[mask] = "d1_weak_exit"

        mask2 = (~d["d1_weak_close"]) & d["d2_weak_close"].fillna(False)
        raw.loc[mask2] = pd.to_numeric(d.loc[mask2, "fwd_ret_confirm_to_close_2d"], errors="coerce")
        d.loc[mask2, "policy_exit_date"] = d.loc[mask2, "entry_date"].map(d2_exit)
        reason.loc[mask2] = "d2_weak_exit"

    d["policy_exit_date"] = d.get("policy_exit_date", pd.Series(pd.NaT, index=d.index)).fillna(
        d["entry_date"].map(hold5_exit)
    )
    d.loc[d["policy_exit_date"].isna(), "policy_exit_date"] = d["entry_date"].map(hold5_exit)

    cost = float(profile["cost_bps"]) / 10000.0
    d["policy_net_ret"] = raw - cost - float(profile["shock"])
    d["entry_adjust_reason"] = reason.fillna("hold5")
    return d.dropna(subset=["policy_exit_date", "policy_net_ret"]).copy()


def simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()

    d = candidates.sort_values(["entry_date", "confirm_datetime", "code"], ascending=[True, True, True]).copy()
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()

    first = d["entry_date_ts"].min()
    last = d["policy_exit_date"].max()
    cal = _trade_calendar(first, last)
    by_day = {day: g.copy() for day, g in d.groupby("entry_date_ts")}

    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []

    for day in cal:
        day = pd.Timestamp(day).normalize()
        still: list[dict[str, Any]] = []
        realized = 0.0
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                realized += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
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
            }
        )

    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def summarize(
    curve: pd.DataFrame,
    closed: pd.DataFrame,
    variant: str,
    policy: str,
    profile: str,
    source_desc: str,
    policy_desc: str,
) -> dict[str, Any]:
    if curve.empty:
        return {
            "variant": variant,
            "policy": policy,
            "profile": profile,
            "source_desc": source_desc,
            "policy_desc": policy_desc,
            "trade_count": 0,
        }
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "policy": policy,
        "profile": profile,
        "source_desc": source_desc,
        "policy_desc": policy_desc,
        "trade_count": int(len(closed)),
        "warning_trades": int(closed.get("entry_reversal_warning", pd.Series(dtype=bool)).fillna(False).sum()),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": max_drawdown(curve["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "d1_weak_exit_count": int((closed.get("entry_adjust_reason", pd.Series(dtype=str)) == "d1_weak_exit").sum()),
        "d2_weak_exit_count": int((closed.get("entry_adjust_reason", pd.Series(dtype=str)) == "d2_weak_exit").sum()),
        "hold5_count": int((closed.get("entry_adjust_reason", pd.Series(dtype=str)) == "hold5").sum()),
    }


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, policy: str, profile: str) -> list[dict[str, Any]]:
    rows = []
    for name, (start, end) in WINDOWS.items():
        part = curve[pd.to_datetime(curve["date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        c = closed[pd.to_datetime(closed["entry_date_ts"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        net = pd.to_numeric(c.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": variant,
                "policy": policy,
                "profile": profile,
                "window": name,
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0) if len(part) else 0.0,
                "max_drawdown": max_drawdown(part["equity"]) if len(part) else 0.0,
                "trade_count": int(len(c)),
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
            }
        )
    return rows


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无样本"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for c in df.columns:
            v = row[c]
            if c in pct_cols:
                item[c] = pct(v)
            elif isinstance(v, float):
                item[c] = f"{v:.4f}"
            else:
                item[c] = "" if pd.isna(v) else str(v)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()

    out_summary: list[dict[str, Any]] = []
    out_windows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []

    for source in SOURCE_VARIANTS:
        selected = select_by_warning(base, source["warning_filter"]).copy()
        if selected.empty:
            continue

        selected["source_variant"] = source["variant"]
        selected["source_desc"] = source["desc"]
        selected.to_csv(OUT_DIR / f"{source['variant']}_signals_noexit.csv", index=False, encoding="utf-8-sig")

        coverage_rows.append(
            {
                "source_variant": source["variant"],
                "source_desc": source["desc"],
                "raw_signals": int(len(selected)),
                "warning_signals": int(selected["entry_reversal_warning"].sum()),
                "warning_ratio": float(selected["entry_reversal_warning"].mean()) if len(selected) else 0.0,
            }
        )

        for policy in POLICIES:
            for profile in PROFILES:
                if bool(policy["skip_warning"]) and source["warning_filter"] == "only_warning":
                    continue

                run = apply_policy(selected, policy, profile)
                curve, closed = simulate(run)
                run_dir = OUT_DIR / f"{source['variant']}__{policy['policy']}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                run.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")

                out_summary.append(
                    summarize(
                        curve,
                        closed,
                        source["variant"],
                        policy["policy"],
                        profile["profile"],
                        source["desc"],
                        policy["desc"],
                    )
                )
                out_windows.extend(
                    window_metrics(
                        curve,
                        closed,
                        source["variant"],
                        policy["policy"],
                        profile["profile"],
                    )
                )

    summary = pd.DataFrame(out_summary)
    windows = pd.DataFrame(out_windows)
    coverage = pd.DataFrame(coverage_rows)

    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8")
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8")

    if not summary.empty:
        rank = summary.sort_values(["policy", "profile", "total_return"], ascending=[True, True, False]).copy()
        best_cost30 = summary[summary["profile"].eq("cost30")].sort_values(
            ["total_return", "trade_count"], ascending=[False, False]
        )
    else:
        rank = pd.DataFrame()
        best_cost30 = pd.DataFrame()

    report = [
        "# G3 range 撤退警戒 + D1/D2 早期弱确认 v1",
        "",
        "## 实验目的",
        "",
        "- 用入场日 `30m` 行为构造质量警戒（冲高回落/突破失败）",
        "- 在警戒样本触发时，测试 D1/D2 弱确认的快速退出",
        "- 不再使用 score/rank 做硬过滤，不在同日做 topN 排序",
        "",
        "## 警戒定义",
        "",
        "- d0_break_attempt：`confirm_high > prev_bar_high`",
        "- d0_retracement：`entry_price <= confirm_high * 99.5%`",
        "- d0_high_pullback：`bar_ret > 2%` 且 `bar_close_pos <= 55%`",
        "- entry_reversal_warning：`d0_break_attempt` 且 (`d0_retracement` 或 `d0_high_pullback`)",
        "- `d1_weak_close`：`fwd_ret_confirm_to_close_1d <= -3%`",
        "- `d2_weak_close`：`fwd_ret_confirm_to_close_2d <= -3%`",
        "",
        "## 覆盖率",
        "",
        md_table(coverage, pct_cols={"warning_ratio"}),
        "",
        "## 样本池+策略汇总",
        "",
        md_table(
            summary,
            pct_cols={
                "total_return",
                "max_drawdown",
                "win_rate",
                "avg_trade_return",
                "worst_trade",
                "warning_trades",
                "d1_weak_exit_count",
                "d2_weak_exit_count",
                "hold5_count",
            },
        ),
        "",
        "## 时间窗表现",
        "",
        md_table(windows, pct_cols={"return", "max_drawdown", "win_rate"}),
        "",
        "## 最佳 (cost30)",
        "",
        md_table(best_cost30.head(20), pct_cols={"total_return", "max_drawdown", "avg_trade_return", "worst_trade", "win_rate"}),
        "",
        "## 说明",
        "",
        "- `all_reversal_pool` 对应原始 `ice_recent3_base` 样本（99行左右，基于离线确认样本）。",
        "- `reversal_only` 只看入场日冲高回落/突破失败样本。",
        "- `reversal_filtered` 直接剔除上述警戒样本，观察是否减少失效交易。",
        "- 策略 `warn_fast_d1` / `warn_fast_d1d2` 仅对警戒信号样本应用 D1/D2 快速退出。",
        "- 策略 `weak_d1d2_exit` 在所有样本上做 D1/D2 弱确认快速退出对照。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\\n".join(report), encoding="utf-8")

    if rank.empty:
        print(f"written: {OUT_DIR}")
    else:
        print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
