from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_four_path_independent_candidates" / "validation_v1" / "labeled_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_box_source_map_v1"
INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 1
HOLD_DAYS = 5
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}
PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "shock": 0.0},
    {"profile": "shock2_cost30", "cost_bps": 30.0, "shock": 0.02},
]


VARIANTS = [
    {
        "variant": "range_box_all",
        "desc": "横盘箱体底部原始全样本",
    },
    {
        "variant": "floor10_reclaim60",
        "desc": "箱体位置低于10%且日线收盘修复高于60%",
    },
    {
        "variant": "floor10_capitulation_reclaim",
        "desc": "箱体位置低于10%、市场有大跌出清且日线修复高于60%",
    },
    {
        "variant": "floor10_ice_reclaim",
        "desc": "箱体位置低于10%、上涨率冰点且日线修复高于60%",
    },
    {
        "variant": "floor10_low_amount_reclaim",
        "desc": "箱体位置低于10%、日线修复高于60%、当日量能没有明显放大",
    },
    {
        "variant": "floor10_strong_amount_reclaim",
        "desc": "箱体位置低于10%、日线修复高于60%、当日量能明显放大",
    },
    {
        "variant": "floor15_gap_reclaim",
        "desc": "箱体位置低于15%、低开后日线修复高于60%",
    },
    {
        "variant": "floor10_deep_gap_reclaim",
        "desc": "箱体位置低于10%、低开后日线修复高于60%",
    },
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无样本_"
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


def load_base() -> pd.DataFrame:
    df = pd.read_parquet(SOURCE).copy()
    df = df[df["g3_chain"].eq("range_box_bottom")].copy()
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    for col in [
        "range_pos60",
        "up_rate",
        "big_down_rate",
        "drawdown20",
        "runup_from_60d_low",
        "amount_ratio20",
        "amount_ratio5",
        "lower_shadow_ratio",
        "close_position",
        "gap_open",
        "index_mom20",
        "entry_open",
        "fwd_ret_open_to_close_5d",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["entry_date", "code", "entry_open", "fwd_ret_open_to_close_5d"]).copy()
    df["struct_tiebreak"] = (
        (1.0 - df["range_pos60"].clip(0, 1)).fillna(0.0) * 10.0
        + df["close_position"].clip(0, 1).fillna(0.0)
        + df["amount_ratio20"].clip(0, 3).fillna(0.0) * 0.05
    )
    return df.sort_values(["entry_date", "struct_tiebreak", "code"], ascending=[True, False, True])


def variant_mask(df: pd.DataFrame, variant: str) -> pd.Series:
    floor10 = df["range_pos60"].le(0.10)
    floor15 = df["range_pos60"].le(0.15)
    reclaim60 = df["close_position"].ge(0.60)
    capitulation = df["big_down_rate"].ge(0.10)
    ice = df["up_rate"].le(0.25)
    low_amount = df["amount_ratio20"].lt(1.0)
    strong_amount = df["amount_ratio20"].ge(1.0)
    gap_down = df["gap_open"].le(0)
    if variant == "range_box_all":
        return pd.Series(True, index=df.index)
    if variant == "floor10_reclaim60":
        return floor10 & reclaim60
    if variant == "floor10_capitulation_reclaim":
        return floor10 & capitulation & reclaim60
    if variant == "floor10_ice_reclaim":
        return floor10 & ice & reclaim60
    if variant == "floor10_low_amount_reclaim":
        return floor10 & low_amount & reclaim60
    if variant == "floor10_strong_amount_reclaim":
        return floor10 & strong_amount & reclaim60
    if variant == "floor15_gap_reclaim":
        return floor15 & gap_down & reclaim60
    if variant == "floor10_deep_gap_reclaim":
        return floor10 & gap_down & reclaim60
    raise ValueError(variant)


def exit_dates(entry_dates: pd.Series) -> dict[pd.Timestamp, pd.Timestamp]:
    dates = pd.to_datetime(entry_dates).dropna().drop_duplicates().sort_values()
    cal = _trade_calendar(dates.min(), dates.max() + pd.Timedelta(days=40))
    pos = {pd.Timestamp(day).normalize(): i for i, day in enumerate(cal)}
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for day in dates.dt.normalize():
        idx = pos.get(pd.Timestamp(day).normalize())
        if idx is None:
            continue
        j = idx + HOLD_DAYS - 1
        if j < len(cal):
            out[pd.Timestamp(day).normalize()] = pd.Timestamp(cal[j]).normalize()
    return out


def prepare_candidates(df: pd.DataFrame, spec: dict[str, str], profile: dict[str, Any]) -> pd.DataFrame:
    part = df[variant_mask(df, spec["variant"])].copy()
    if part.empty:
        return part
    part["variant"] = spec["variant"]
    part["desc"] = spec["desc"]
    part["entry_date_ts"] = pd.to_datetime(part["entry_date"], errors="coerce").dt.normalize()
    part["policy_exit_date"] = part["entry_date_ts"].map(exit_dates(part["entry_date_ts"]))
    part["policy_net_ret"] = (
        pd.to_numeric(part["fwd_ret_open_to_close_5d"], errors="coerce")
        - float(profile["cost_bps"]) / 10000.0
        - float(profile["shock"])
    )
    return part.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret"]).copy()


def simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.sort_values(["entry_date_ts", "struct_tiebreak", "code"], ascending=[True, False, True]).copy()
    cal = _trade_calendar(d["entry_date_ts"].min(), d["policy_exit_date"].max())
    by_day = {day: g.copy() for day, g in d.groupby("entry_date_ts")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []

    for day in cal:
        day = pd.Timestamp(day).normalize()
        still: list[dict[str, Any]] = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
            else:
                still.append(pos)
        open_pos = still

        opened = 0
        todays = by_day.get(day)
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
        curve_rows.append({"date": day, "cash": cash, "equity": equity, "open_positions": len(open_pos), "opened": opened})

    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, spec: dict[str, str], profile: str) -> dict[str, Any]:
    if curve.empty:
        return {"variant": spec["variant"], "desc": spec["desc"], "profile": profile, "trade_count": 0}
    ret = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": spec["variant"],
        "desc": spec["desc"],
        "profile": profile,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": max_drawdown(curve["equity"]),
        "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
        "avg_trade_return": float(ret.mean()) if len(ret) else 0.0,
        "worst_trade": float(ret.min()) if len(ret) else 0.0,
    }


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window, (start, end) in WINDOWS.items():
        part = curve[pd.to_datetime(curve["date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        c = closed[pd.to_datetime(closed.get("entry_date_ts", pd.Series(dtype="datetime64[ns]"))).between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        ret = pd.to_numeric(c.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "window": window,
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0) if len(part) else 0.0,
                "max_drawdown": max_drawdown(part["equity"]) if len(part) else 0.0,
                "trade_count": int(len(c)),
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return rows


def no_future_source(df: pd.DataFrame) -> pd.DataFrame:
    drop_cols = [c for c in df.columns if c.startswith("fwd_ret_")]
    keep = df.drop(columns=drop_cols, errors="ignore").copy()
    keep["research_source"] = "gen3_range_box_source_map_v1"
    return keep


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []

    for spec in VARIANTS:
        raw = base[variant_mask(base, spec["variant"])].copy()
        coverage_rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "raw_signals": int(len(raw)),
                "signal_days": int(raw["entry_date"].nunique()) if len(raw) else 0,
            }
        )
        if not raw.empty:
            no_future_source(raw).to_csv(OUT_DIR / f"{spec['variant']}_source_no_future.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            candidates = prepare_candidates(base, spec, profile)
            curve, closed = simulate(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, spec, str(profile["profile"])))
            window_rows.extend(window_metrics(curve, closed, spec["variant"], str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    coverage = pd.DataFrame(coverage_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    report = [
        "# G3 横盘/箱体底部独立候选源地图 v1",
        "",
        "## 本轮目标",
        "",
        "- 回到主研究：重建横盘/箱体底部独立候选源。",
        "- 不用 `score/rank` 做过滤；仅用箱体位置、日线修复、冰点/出清、低开修复、量能结构。",
        "- 当前仍是离线研究，不接入 G3 实盘，不影响 G2。",
        "",
        "## 英文名解释",
        "",
        "- `range_box_all`：横盘箱体底部原始全样本。",
        "- `floor10_reclaim60`：箱体位置低于10%，且日线收盘修复位置高于60%。",
        "- `floor10_capitulation_reclaim`：箱体位置低于10%，市场有大跌出清，同时日线修复高于60%。",
        "- `floor10_ice_reclaim`：箱体位置低于10%，市场上涨率处于冰点，同时日线修复高于60%。",
        "- `floor10_low_amount_reclaim`：箱体位置低于10%，日线修复高于60%，但当日量能没有明显放大。",
        "- `floor10_strong_amount_reclaim`：箱体位置低于10%，日线修复高于60%，且当日量能明显放大。",
        "- `floor15_gap_reclaim`：箱体位置低于15%，低开后日线修复高于60%。",
        "- `floor10_deep_gap_reclaim`：箱体位置低于10%，低开后日线修复高于60%。",
        "",
        "## 覆盖率",
        "",
        md_table(coverage),
        "",
        "## slot5 复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口稳定性",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 下一步判断",
        "",
        "- 若某一源在 `cost30`、`cost100` 和 `shock2_cost30` 下都明显优于原始样本，且 2024-2025/2026YTD 不崩，才进入下一步 30m 真实可见确认。",
        "- 若只有 full 好、验证/盲测弱，则视为历史集中收益，不进入正式候选。",
        "- 若所有结构源都不稳定，说明当前横盘定义还不够独立，需要重写箱体底部事件定义，而不是继续加过滤。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
