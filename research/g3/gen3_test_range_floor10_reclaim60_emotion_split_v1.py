from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
SOURCE = _report_path() / "gen3_range_floor10_reclaim60_execution_audit_v1" / "audited_trades.csv"
OUT_DIR = _report_path() / "gen3_range_floor10_reclaim60_emotion_split_v1"

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
    {
        "variant": "all_floor10_reclaim60",
        "desc": "全部floor10_reclaim60样本",
        "mask": lambda d: pd.Series(True, index=d.index),
    },
    {
        "variant": "icepoint_only",
        "desc": "只保留冰点后样本",
        "mask": lambda d: d["emotion_signal"].eq("icepoint"),
    },
    {
        "variant": "neutral_only",
        "desc": "只保留中性情绪样本",
        "mask": lambda d: d["emotion_signal"].eq("neutral"),
    },
    {
        "variant": "icepoint_or_strong_amount20",
        "desc": "冰点样本全部保留；中性样本要求日线20日量能>=1.3",
        "mask": lambda d: d["emotion_signal"].eq("icepoint") | (d["emotion_signal"].eq("neutral") & d["amount_ratio20"].ge(1.3)),
    },
    {
        "variant": "icepoint_or_strong_30m",
        "desc": "冰点样本全部保留；中性样本要求30m承接量比>=1.8",
        "mask": lambda d: d["emotion_signal"].eq("icepoint") | (d["emotion_signal"].eq("neutral") & d["amount_ratio3"].ge(1.8)),
    },
    {
        "variant": "icepoint_or_deeper_floor",
        "desc": "冰点样本全部保留；中性样本要求箱体位置<=2%",
        "mask": lambda d: d["emotion_signal"].eq("icepoint") | (d["emotion_signal"].eq("neutral") & d["range_pos60"].le(0.02)),
    },
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


def load_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["policy_exit_date"] = d["entry_date_ts"] + pd.Timedelta(days=8)
    for col in [
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
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "entry_price_used", "fwd_ret_confirm_to_close_5d"]).copy()


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


def standardize(signals: pd.DataFrame, profile: dict[str, Any], variant: str, desc: str) -> pd.DataFrame:
    d = signals.copy()
    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    d["policy_net_ret"] = d["fwd_ret_confirm_to_close_5d"] - cost - shock
    d["variant"] = variant
    d["desc"] = desc
    return d.dropna(subset=["policy_net_ret", "entry_price_used"]).copy()


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
    rows: list[dict[str, Any]] = []
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


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        selected = base[spec["mask"](base)].copy()
        selected.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        coverage_rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "signal_count": int(len(selected)),
                "signal_2026": int(pd.to_datetime(selected["entry_date_ts"]).dt.year.eq(2026).sum()) if len(selected) else 0,
                "icepoint_count": int(selected["emotion_signal"].eq("icepoint").sum()) if len(selected) else 0,
                "neutral_count": int(selected["emotion_signal"].eq("neutral").sum()) if len(selected) else 0,
            }
        )
        for profile in PROFILES:
            candidates = standardize(selected, profile, str(spec["variant"]), str(spec["desc"]))
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

    cost30 = summary[summary["profile"].eq("cost30")].copy()
    cost100 = summary[summary["profile"].eq("cost100")].copy()
    shock = summary[summary["profile"].eq("shock2_cost30")].copy()
    rank = (
        cost30[["variant", "desc", "trade_count", "total_return", "max_drawdown", "win_rate", "worst_trade"]]
        .rename(columns={"total_return": "cost30_return", "max_drawdown": "cost30_max_dd", "win_rate": "cost30_win_rate", "worst_trade": "cost30_worst_trade"})
        .merge(cost100[["variant", "total_return", "max_drawdown"]].rename(columns={"total_return": "cost100_return", "max_drawdown": "cost100_max_dd"}), on="variant")
        .merge(shock[["variant", "total_return", "max_drawdown"]].rename(columns={"total_return": "shock2_return", "max_drawdown": "shock2_max_dd"}), on="variant")
        .merge(coverage[["variant", "signal_2026", "icepoint_count", "neutral_count"]], on="variant")
        .sort_values(["shock2_return", "cost100_return", "cost30_return"], ascending=[False, False, False])
    )
    rank.to_csv(OUT_DIR / "emotion_split_rank.csv", index=False, encoding="utf-8-sig")

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
    }
    lines = [
        "# G3 floor10_reclaim60 情绪拆分复验 v1",
        "",
        "## 策略名解释",
        "",
        "- `floor10_reclaim60`：冰点后3日窗口里，箱体位置不高于10%，且日线收盘修复不低于60%的横盘箱体底部30m承接买法。",
        "- `icepoint_only`：只保留冰点后样本，中文意思是“只在恐慌出清后的修复里买”。",
        "- `neutral_only`：只保留中性情绪样本，中文意思是“没有明显冰点或高潮时，也按箱体底部修复买”。",
        "- `icepoint_or_strong_amount20`：冰点样本全部保留；如果是中性样本，要求日线20日量能更强。",
        "- `icepoint_or_strong_30m`：冰点样本全部保留；如果是中性样本，要求30m承接量比更强。",
        "- `icepoint_or_deeper_floor`：冰点样本全部保留；如果是中性样本，要求更贴近箱体底部。",
        "",
        "## 结论排名",
        "",
        md_table(rank, pct_cols=pct_cols),
        "",
        "## 覆盖率",
        "",
        md_table(coverage, pct_cols=pct_cols),
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
        "- 如果 `icepoint_only` 压低总收益但提高冲击口径，说明它适合作为高确定性子源，但不能单独承担交易频率。",
        "- 如果 `neutral_only` 2026明显拖累，后续要给 neutral 单独开发买法，不能继续和 icepoint 共用同一套买点。",
        "- 如果中性样本加量能或更深箱体后仍不稳定，下一步应转向新的横盘候选源，而不是继续在旧样本上加过滤。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
