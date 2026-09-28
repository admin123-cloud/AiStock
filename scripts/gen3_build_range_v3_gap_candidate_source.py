from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar


SOURCE = ROOT / "reports" / "gen3_four_path_independent_candidates" / "validation_v1" / "labeled_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_v3_gap_candidate_source_v1"
INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 1
COST_BPS = 30.0
WINDOWS = {
    "weak_gap_2022_2024": ("2022-01-01", "2024-12-31"),
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2020-01-01", "2026-05-29"),
}

VARIANTS = [
    {"variant": "range_v3_cold_floor_reclaim_h5", "family": "true_range", "hold_days": 5},
    {"variant": "range_v3_true_floor_reclaim_h3", "family": "true_range", "hold_days": 3},
    {"variant": "range_v3_weak_low_not_chasing_h5", "family": "weak_rebound_gap", "hold_days": 5},
    {"variant": "range_v3_weak_low_not_chasing_h10", "family": "weak_rebound_gap", "hold_days": 10},
    {"variant": "range_v3_weak_repair_market_ok_h10", "family": "weak_rebound_gap", "hold_days": 10},
    {"variant": "range_v3_hybrid_range_weak_h5", "family": "hybrid_gap", "hold_days": 5},
]


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
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.normalize()
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    numeric_cols = [
        "range_pos60",
        "range_pos20",
        "up_rate",
        "breadth_ma20",
        "big_down_rate",
        "drawdown20",
        "runup_from_60d_low",
        "amount_ratio20",
        "lower_shadow_ratio",
        "close_position",
        "high60",
        "low60",
        "mom5",
        "mom10",
        "candidate_score",
        "entry_open",
        "fwd_ret_open_to_close_3d",
        "fwd_ret_open_to_close_5d",
        "fwd_ret_open_to_close_10d",
        "index_mom20",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["box_width60"] = df["high60"] / df["low60"] - 1.0
    df["reclaim_flag"] = df["close_position"].ge(0.60) | df["lower_shadow_ratio"].ge(0.30)
    df["range_v3_score"] = (
        df["candidate_score"].fillna(0.0)
        + df["close_position"].clip(0, 1).fillna(0.0) * 0.20
        - df["runup_from_60d_low"].clip(lower=0).fillna(0.0) * 0.10
        + df["amount_ratio20"].clip(0, 2).fillna(0.0) * 0.03
        - df["big_down_rate"].clip(lower=0).fillna(0.0) * 0.05
    )
    return df.dropna(subset=["entry_date", "code", "entry_open"]).sort_values(
        ["entry_date", "range_v3_score", "candidate_score", "amount"], ascending=[True, False, False, False]
    )


def variant_mask(df: pd.DataFrame, variant: str) -> pd.Series:
    range_chain = df["g3_chain"].eq("range_box_bottom")
    weak_chain = df["g3_chain"].eq("weak_rebound_repair")
    reclaim = df["reclaim_flag"]
    range_cold = (
        range_chain
        & df["range_pos60"].le(0.25)
        & (df["up_rate"].le(0.35) | df["breadth_ma20"].le(0.40))
        & reclaim
        & df["big_down_rate"].lt(0.25)
        & df["index_mom20"].ge(-0.10)
    )
    range_true = (
        range_chain
        & df["box_width60"].between(0.18, 0.55, inclusive="both")
        & df["range_pos60"].le(0.20)
        & reclaim
        & df["big_down_rate"].lt(0.25)
        & df["index_mom20"].ge(-0.10)
    )
    weak_low = (
        weak_chain
        & df["range_pos60"].between(0.10, 0.45, inclusive="both")
        & df["runup_from_60d_low"].le(0.30)
        & df["mom10"].le(0.10)
        & df["close_position"].ge(0.55)
        & df["index_mom20"].ge(-0.08)
    )
    weak_repair = (
        weak_chain
        & df["drawdown20"].between(-0.25, -0.08, inclusive="both")
        & df["range_pos60"].le(0.45)
        & df["close_position"].ge(0.60)
        & df["mom5"].ge(0.0)
        & df["index_mom20"].ge(-0.05)
    )
    hybrid = (
        range_chain
        & df["range_pos60"].le(0.25)
        & reclaim
        & df["big_down_rate"].lt(0.20)
        & df["index_mom20"].ge(-0.08)
    ) | weak_low
    if variant == "range_v3_cold_floor_reclaim_h5":
        return range_cold
    if variant == "range_v3_true_floor_reclaim_h3":
        return range_true
    if variant in {"range_v3_weak_low_not_chasing_h5", "range_v3_weak_low_not_chasing_h10"}:
        return weak_low
    if variant == "range_v3_weak_repair_market_ok_h10":
        return weak_repair
    if variant == "range_v3_hybrid_range_weak_h5":
        return hybrid
    raise ValueError(variant)


def select_daily(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    part = df[variant_mask(df, spec["variant"])].copy()
    if part.empty:
        return part
    part["range_v3_variant"] = spec["variant"]
    part["range_v3_family"] = spec["family"]
    part["hold_days"] = int(spec["hold_days"])
    part["rank_in_day"] = part.groupby("entry_date")["range_v3_score"].rank(method="first", ascending=False)
    return part[part["rank_in_day"].le(1)].sort_values(["entry_date", "rank_in_day", "range_v3_score"])


def exit_dates(entry_dates: pd.Series, hold_days: int) -> dict[pd.Timestamp, pd.Timestamp]:
    cal = _trade_calendar(entry_dates.min(), entry_dates.max() + pd.Timedelta(days=30))
    out = {}
    for idx, day in enumerate(cal):
        j = idx + hold_days - 1
        if j < len(cal):
            out[day] = cal[j]
    return out


def simulate(candidates: pd.DataFrame, hold_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    ret_col = f"fwd_ret_open_to_close_{hold_days}d"
    d = candidates.dropna(subset=[ret_col]).copy()
    d["policy_exit_date"] = d["entry_date"].map(exit_dates(d["entry_date"], hold_days))
    d["net_ret"] = pd.to_numeric(d[ret_col], errors="coerce") - COST_BPS / 10000.0
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
    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def summarize_curve(curve: pd.DataFrame, closed: pd.DataFrame, spec: dict) -> dict:
    if curve.empty:
        return {"variant": spec["variant"], "hold_days": spec["hold_days"], "trade_count": 0}
    net = pd.to_numeric(closed.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": spec["variant"],
        "family": spec["family"],
        "hold_days": int(spec["hold_days"]),
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": max_drawdown(curve["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "start_date": str(curve["date"].min().date()),
        "end_date": str(curve["date"].max().date()),
    }


def window_summary(curve: pd.DataFrame, spec: dict) -> list[dict]:
    rows = []
    for name, (start, end) in WINDOWS.items():
        if curve.empty:
            rows.append({"variant": spec["variant"], "window": name, "return": 0.0, "max_drawdown": 0.0, "days": 0})
            continue
        part = curve[curve["date"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        if part.empty:
            ret = 0.0
            dd = 0.0
        else:
            ret = float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0)
            dd = max_drawdown(part["equity"])
        rows.append(
            {
                "variant": spec["variant"],
                "family": spec["family"],
                "hold_days": int(spec["hold_days"]),
                "window": name,
                "return": ret,
                "max_drawdown": dd,
                "days": int(len(part)),
            }
        )
    return rows


def no_future_source(df: pd.DataFrame) -> pd.DataFrame:
    drop_cols = [c for c in df.columns if c.startswith("fwd_ret_") or c in {"net_ret", "policy_exit_date"}]
    keep = df.drop(columns=drop_cols, errors="ignore").copy()
    keep["research_source"] = "g3_range_v3_gap_candidate_source_v1"
    keep["candidate_family"] = keep["range_v3_family"]
    return keep


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    all_sources = []
    summary_rows = []
    window_rows = []
    for spec in VARIANTS:
        selected = select_daily(base, spec)
        all_sources.append(selected)
        curve, closed = simulate(selected, int(spec["hold_days"]))
        curve.to_csv(OUT_DIR / f"{spec['variant']}_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{spec['variant']}_closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(summarize_curve(curve, closed, spec))
        window_rows.extend(window_summary(curve, spec))
    source = pd.concat(all_sources, ignore_index=True) if all_sources else pd.DataFrame()
    no_future_source(source).to_csv(OUT_DIR / "range_v3_gap_candidate_source_no_future.csv", index=False, encoding="utf-8-sig")
    summary_df = pd.DataFrame(summary_rows).sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    window_df = pd.DataFrame(window_rows)
    summary_df.to_csv(OUT_DIR / "range_v3_gap_eval_summary.csv", index=False, encoding="utf-8-sig")
    window_df.to_csv(OUT_DIR / "range_v3_gap_window_summary.csv", index=False, encoding="utf-8-sig")
    best_full = summary_df.iloc[0].to_dict() if not summary_df.empty else {}
    weak_rows = window_df[window_df["window"].eq("weak_gap_2022_2024")].sort_values(["return", "max_drawdown"], ascending=[False, False])
    best_weak = weak_rows.iloc[0].to_dict() if not weak_rows.empty else {}
    true_range = summary_df[summary_df["family"].eq("true_range")]
    best_true_range = true_range.iloc[0].to_dict() if not true_range.empty else {}

    report = [
        "# G3 range_v3 震荡/弱反弹缺口候选源 V1",
        "",
        "## 口径",
        "",
        f"- 源文件：`{SOURCE.relative_to(ROOT)}`。",
        "- 不新增实盘入口，不复用 G2 运行链路；这里只做 G3 独立研究候选源。",
        "- 候选分两类：`true_range` 尝试真正横盘箱体底部；`weak_rebound_gap` 尝试弱反弹里的低位不追高修复。",
        "- 评估为次日开盘买入、固定持有 3/5/10 日、30bps 成本、slot5、每日最多开 1 笔；当前仍是研究口径，尚未做逐日 MTM 和 30m 可见性。",
        "",
        "## 核心结论",
        "",
        f"- Full 最好：`{best_full.get('variant')}`，收益 {pct(best_full.get('total_return'))}，回撤 {pct(best_full.get('max_drawdown'))}，交易 {best_full.get('trade_count')} 笔。",
        f"- 2022-2024 最好：`{best_weak.get('variant')}`，收益 {pct(best_weak.get('return'))}，回撤 {pct(best_weak.get('max_drawdown'))}。",
        f"- true_range 最好：`{best_true_range.get('variant')}`，收益 {pct(best_true_range.get('total_return'))}，回撤 {pct(best_true_range.get('max_drawdown'))}。",
        "- 判断：真正横盘箱体底部仍未通过；弱反弹低位不追高修复有明显改善，可以作为“震荡/弱反弹缺口”候选继续审计。",
        "",
        "## 全周期组合摘要",
        "",
        md_table(summary_df, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"}),
        "",
        "## 分段窗口",
        "",
        md_table(window_df, {"return", "max_drawdown"}),
        "",
        "## 下一步",
        "",
        "1. 对 `range_v3_weak_low_not_chasing_h5/h10` 做逐日 MTM、30m 可见性和滑点压力测试。",
        "2. true_range 暂不进入正式组合；要重写震荡箱体定义，不能再只靠箱体底部和冰点。",
        "3. 若 weak_low 通过压力测试，再与 down_panic_v3 和 strong_v2 做动态路由组合。",
    ]
    (OUT_DIR / "range_v3_gap_candidate_source_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "candidate_source": str(OUT_DIR / "range_v3_gap_candidate_source_no_future.csv"),
                "summary": str(OUT_DIR / "range_v3_gap_eval_summary.csv"),
                "best_full": best_full,
                "best_2022_2024": best_weak,
                "best_true_range": best_true_range,
                "next_step": "range_v3_weak_low_mtm_and_pressure",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
