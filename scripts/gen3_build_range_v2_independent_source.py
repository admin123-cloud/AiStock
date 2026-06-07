from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar


SOURCE = ROOT / "reports" / "gen3_four_path_independent_candidates" / "validation_v1" / "labeled_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_v2_independent_source_v1"
INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 1
HOLD_DAYS = 5
COST_BPS = 30.0
WINDOWS = {
    "weak_gap_2022_2024": ("2022-01-01", "2024-12-31"),
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2020-01-01", "2026-05-29"),
}


def pct(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def num(value: float | int | None, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.{digits}f}"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = num(value, 4)
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def load_base() -> pd.DataFrame:
    df = pd.read_parquet(SOURCE).copy()
    df = df[df["g3_chain"].eq("range_box_bottom")].copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.normalize()
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    numeric_cols = [
        "range_pos60",
        "range_pos20",
        "up_rate",
        "big_down_rate",
        "drawdown20",
        "runup_from_60d_low",
        "amount_ratio20",
        "amount_ratio5",
        "lower_shadow_ratio",
        "close_position",
        "high60",
        "low60",
        "mom20",
        "gap_open",
        "candidate_score",
        "entry_open",
        "fwd_ret_open_to_close_5d",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["box_width60"] = df["high60"] / df["low60"] - 1.0
    df["reclaim_flag"] = df["close_position"].ge(0.55) | df["lower_shadow_ratio"].ge(0.25)
    df["liquid_ok"] = df["amount_ratio20"].ge(0.75)
    df["not_crash_day"] = df["big_down_rate"].lt(0.35)
    df["range_v2_score"] = (
        (1.0 - df["range_pos60"].clip(0, 1)).fillna(0) * 0.30
        + (0.35 - df["up_rate"].clip(0, 0.35)).clip(lower=0).fillna(0) * 0.80
        + df["lower_shadow_ratio"].clip(0, 0.5).fillna(0) * 0.25
        + df["close_position"].clip(0, 1).fillna(0) * 0.15
        + df["amount_ratio20"].clip(0, 2).fillna(0) * 0.05
        - df["runup_from_60d_low"].clip(lower=0).fillna(0) * 0.08
    )
    return df.dropna(subset=["entry_date", "code", "entry_open", "fwd_ret_open_to_close_5d"]).sort_values(
        ["entry_date", "range_v2_score", "candidate_score", "amount"], ascending=[True, False, False, False]
    )


def variant_mask(df: pd.DataFrame, variant: str) -> pd.Series:
    bottom20 = df["range_pos60"].le(0.20)
    bottom30 = df["range_pos60"].le(0.30)
    ice = df["up_rate"].le(0.25)
    cold = df["up_rate"].le(0.35)
    stress = df["big_down_rate"].ge(0.10)
    wash = df["drawdown20"].le(-0.10)
    low_runup = df["runup_from_60d_low"].le(0.35)
    squeeze = df["box_width60"].between(0.12, 0.55, inclusive="both")
    reclaim = df["reclaim_flag"]
    liquid = df["liquid_ok"]
    not_crash = df["not_crash_day"]
    if variant == "range_v2_ice_reclaim":
        return bottom20 & ice & reclaim & liquid & not_crash & low_runup
    if variant == "range_v2_stress_reclaim":
        return bottom30 & stress & reclaim & liquid & not_crash & low_runup
    if variant == "range_v2_squeeze_bottom":
        return bottom30 & cold & squeeze & reclaim & liquid & not_crash
    if variant == "range_v2_wash_reclaim":
        return bottom30 & wash & reclaim & liquid & not_crash & low_runup
    if variant == "range_v2_base_bottom":
        return bottom30 & reclaim & liquid & not_crash
    raise ValueError(variant)


def select_daily(df: pd.DataFrame, variant: str, top_n: int) -> pd.DataFrame:
    part = df[variant_mask(df, variant)].copy()
    if part.empty:
        return part
    part["range_v2_variant"] = variant
    part["rank_in_day"] = part.groupby("entry_date")["range_v2_score"].rank(method="first", ascending=False)
    return part[part["rank_in_day"].le(top_n)].sort_values(["entry_date", "rank_in_day", "range_v2_score"])


def exit_dates(entry_dates: pd.Series) -> dict[pd.Timestamp, pd.Timestamp]:
    cal = _trade_calendar(entry_dates.min(), entry_dates.max() + pd.Timedelta(days=30))
    out = {}
    for idx, day in enumerate(cal):
        j = idx + HOLD_DAYS - 1
        if j < len(cal):
            out[day] = cal[j]
    return out


def simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.copy()
    d["policy_exit_date"] = d["entry_date"].map(exit_dates(d["entry_date"]))
    d["net_ret"] = d["fwd_ret_open_to_close_5d"] - COST_BPS / 10000.0
    d = d.dropna(subset=["policy_exit_date", "net_ret"]).copy()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"]).dt.normalize()
    cal = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max())
    by_day = {day: g.copy() for day, g in d.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict] = []
    closed: list[dict] = []
    curve_rows = []
    for day in cal:
        still = []
        realized = 0.0
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                pnl = exit_value - float(pos["stake"])
                realized += pnl
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
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
    closed_df = pd.DataFrame(closed)
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return closed_df, curve


def metrics(variant: str, top_n: int, closed: pd.DataFrame, curve: pd.DataFrame) -> dict:
    if curve.empty:
        return {"variant": variant, "top_n": top_n, "trade_count": 0}
    ret = pd.to_numeric(closed.get("net_ret"), errors="coerce") if not closed.empty else pd.Series(dtype="float64")
    return {
        "variant": variant,
        "top_n": top_n,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": max_drawdown(curve["equity"]),
        "win_rate": float((ret > 0).mean()) if not ret.empty else math.nan,
        "avg_trade_return": float(ret.mean()) if not ret.empty else math.nan,
        "worst_trade": float(ret.min()) if not ret.empty else math.nan,
        "bad10_rate": float((ret <= -0.10).mean()) if not ret.empty else math.nan,
        "start_date": curve["date"].iloc[0].date().isoformat(),
        "end_date": curve["date"].iloc[-1].date().isoformat(),
    }


def window_metrics(variant: str, top_n: int, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, (start, end) in WINDOWS.items():
        d = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].sort_values("date")
        if d.empty:
            rows.append({"variant": variant, "top_n": top_n, "window": name, "return": math.nan, "max_drawdown": math.nan, "days": 0})
            continue
        rows.append(
            {
                "variant": variant,
                "top_n": top_n,
                "window": name,
                "return": float(d["equity"].iloc[-1] / d["equity"].iloc[0] - 1.0),
                "max_drawdown": max_drawdown(d["equity"]),
                "days": len(d),
            }
        )
    return pd.DataFrame(rows)


def no_future_source(candidates: pd.DataFrame) -> pd.DataFrame:
    drop_prefixes = ("fwd_ret_",)
    cols = [c for c in candidates.columns if not c.startswith(drop_prefixes)]
    keep = candidates[cols].copy()
    keep["research_source"] = "g3_range_v2_independent_source_v1"
    keep["candidate_family"] = "range_v2_box_bottom_ice_reclaim"
    return keep


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    variants = [
        "range_v2_ice_reclaim",
        "range_v2_stress_reclaim",
        "range_v2_squeeze_bottom",
        "range_v2_wash_reclaim",
        "range_v2_base_bottom",
    ]
    all_candidates = []
    summaries = []
    windows = []
    for variant in variants:
        for top_n in [1, 2, 3]:
            selected = select_daily(base, variant, top_n)
            if selected.empty:
                continue
            selected["top_n"] = top_n
            all_candidates.append(selected)
            no_future_source(selected).to_csv(OUT_DIR / f"{variant}_top{top_n}_candidates_no_future.csv", index=False, encoding="utf-8-sig")
            closed, curve = simulate(selected)
            closed.to_csv(OUT_DIR / f"{variant}_top{top_n}_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(OUT_DIR / f"{variant}_top{top_n}_curve.csv", index=False, encoding="utf-8-sig")
            summaries.append(metrics(variant, top_n, closed, curve))
            windows.append(window_metrics(variant, top_n, curve))

    source = pd.concat(all_candidates, ignore_index=True) if all_candidates else pd.DataFrame()
    if not source.empty:
        no_future_source(source).to_csv(OUT_DIR / "range_v2_candidate_source_no_future.csv", index=False, encoding="utf-8-sig")
    summary_df = pd.DataFrame(summaries).sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    window_df = pd.concat(windows, ignore_index=True) if windows else pd.DataFrame()
    summary_df.to_csv(OUT_DIR / "range_v2_eval_summary.csv", index=False, encoding="utf-8-sig")
    window_df.to_csv(OUT_DIR / "range_v2_window_summary.csv", index=False, encoding="utf-8-sig")

    weak_gap = window_df[window_df["window"].eq("weak_gap_2022_2024")].copy()
    weak_gap = weak_gap.sort_values(["return", "max_drawdown"], ascending=[False, False])
    best = summary_df.iloc[0].to_dict() if not summary_df.empty else {}
    best_gap = weak_gap.iloc[0].to_dict() if not weak_gap.empty else {}

    report = [
        "# G3 Range V2 独立候选源 V1",
        "",
        "## 本步目标",
        "",
        "重建横盘震荡买法，不沿用旧 `range_bottom_icepoint`。这版只验证箱体底部、冰点/出清、反抽确认是否能在 2022-2024 弱收益窗口提供正贡献。",
        "",
        "## 口径",
        "",
        f"- 输入源：`{SOURCE.relative_to(ROOT)}`",
        "- 只取 `g3_chain=range_box_bottom`。",
        "- 入场代理：次日开盘。",
        "- 退出代理：5 个交易日后收盘。",
        "- 成本：30bps。",
        "- 组合：slot5，每日最多开 1 笔，每笔 20%。",
        "- 候选源与前向收益分离，`*_candidates_no_future.csv` 不包含 forward return 字段。",
        "",
        "## Full 摘要",
        "",
        md_table(
            summary_df[
                [
                    "variant",
                    "top_n",
                    "trade_count",
                    "total_return",
                    "max_drawdown",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "bad10_rate",
                ]
            ],
            {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "bad10_rate"},
        ),
        "",
        "## 2022-2024 弱收益窗口",
        "",
        md_table(
            weak_gap[["variant", "top_n", "window", "return", "max_drawdown", "days"]],
            {"return", "max_drawdown"},
        ),
        "",
        "## 初步结论",
        "",
        f"- Full 最好候选：`{best.get('variant', '')}` top{best.get('top_n', '')}，收益 `{pct(best.get('total_return'))}`，回撤 `{pct(best.get('max_drawdown'))}`。",
        f"- 2022-2024 最好候选：`{best_gap.get('variant', '')}` top{best_gap.get('top_n', '')}，收益 `{pct(best_gap.get('return'))}`，回撤 `{pct(best_gap.get('max_drawdown'))}`。",
        "- 如果 2022-2024 最好候选仍不能稳定为正，说明当前横盘定义还不够，需要继续重建，而不是把它并入正式组合。",
        "- 若 2022-2024 为正但 full 或盲测较差，则只能作为市场风格条件触发的专用链路，不能全市场常开。",
        "",
        "## 本步完成",
        "",
        "- 已生成 range_v2 独立候选源。",
        "- 已完成 Top1/2/3、五类横盘定义的 5 日代理评估。",
        "- 已单独输出 2022-2024 验收窗口。",
        "",
        "## 下一步目标",
        "",
        "审计 range_v2 的结果：若 2022-2024 已明显超过基数，则进入 30m 可见性和压力测试；若没有超过，则继续重写横盘定义或转向 down_panic_v3。",
        "",
    ]
    (OUT_DIR / "range_v2_independent_source_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "candidate_source": str(OUT_DIR / "range_v2_candidate_source_no_future.csv"),
                "summary": str(OUT_DIR / "range_v2_eval_summary.csv"),
                "best_full": best,
                "best_2022_2024": best_gap,
                "next_step": "audit_range_v2_or_build_down_panic_v3",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
