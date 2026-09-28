from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df


SOURCE = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "sources" / "g2_v2_complete.parquet"
OUT_DIR = ROOT / "reports" / "gen3_strong_v2_independent_source_v1"
INITIAL_CAPITAL = 1_000_000.0
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 2
COST_BPS = 30.0
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2020-01-01", "2026-05-29"),
}


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


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


def classify_g3_market_style(row: pd.Series) -> str:
    close = row.get("index_close")
    ma20 = row.get("index_ma20")
    ma60 = row.get("index_ma60")
    mom20 = row.get("index_mom20")
    breadth = row.get("market_breadth")
    if pd.notna(close) and pd.notna(ma20) and pd.notna(ma60) and close >= ma20 >= ma60:
        return "main_up"
    if (
        pd.notna(close)
        and pd.notna(ma20)
        and pd.notna(ma60)
        and pd.notna(mom20)
        and pd.notna(breadth)
        and ma20 < ma60
        and close >= ma60
        and mom20 > 0
        and breadth >= 0.5
    ):
        return "weak_recovery"
    if pd.notna(close) and pd.notna(ma20) and close < ma20:
        return "defense_or_failed"
    return "neutral"


def trade_calendar(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    sql = f"""
    SELECT DISTINCT trade_date
    FROM kline_daily
    WHERE trade_date BETWEEN toDate({sql_literal(start.strftime('%Y-%m-%d'))})
      AND toDate({sql_literal(end.strftime('%Y-%m-%d'))})
    ORDER BY trade_date
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return []
    return pd.to_datetime(df["trade_date"], errors="coerce").dt.normalize().dropna().tolist()


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def pick_gross_ret(df: pd.DataFrame, hold_days: int) -> pd.Series:
    cols = [f"outcome_fwd_ret_{hold_days}d", f"fwd_ret_{hold_days}d"]
    ret = pd.Series([math.nan] * len(df), index=df.index, dtype="float64")
    for col in cols:
        if col in df.columns:
            v = pd.to_numeric(df[col], errors="coerce")
            ret = ret.where(ret.notna(), v)
    return ret


def build_base() -> pd.DataFrame:
    df = pd.read_parquet(SOURCE).copy()
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df[df["source_family"].eq("volume5")].copy()
    df = df.dropna(subset=["entry_date", "code"]).copy()

    df["g3_market_style"] = df.apply(classify_g3_market_style, axis=1)
    df["v4_rank"] = pd.to_numeric(df.get("v4_rank"), errors="coerce").fillna(999999)
    df["v4_score"] = pd.to_numeric(df.get("v4_score"), errors="coerce").fillna(-999999)
    df["score_volume5"] = pd.to_numeric(df.get("score_volume5"), errors="coerce")
    df["runup_from_60d_low"] = pd.to_numeric(df.get("runup_from_60d_low"), errors="coerce")
    df["sector_score_bonus"] = pd.to_numeric(df.get("sector_score_bonus"), errors="coerce").fillna(0.0)
    df["sector_strong"] = df.get("sector_strong", False).fillna(False).astype(bool)
    df["l3_s3"] = pd.to_numeric(df.get("l3_s3"), errors="coerce")
    df["l2_s3"] = pd.to_numeric(df.get("l2_s3"), errors="coerce")
    df["g3_strong_score"] = (
        df["v4_score"].fillna(0.0)
        + df["sector_score_bonus"].fillna(0.0)
        + df["l3_s3"].fillna(0.0) * 0.03
        + df["l2_s3"].fillna(0.0) * 0.02
    )
    df["g3_route_weight"] = df["g3_market_style"].map({"main_up": 1.0, "weak_recovery": 0.4}).fillna(0.0)
    df["g3_strong_v2_signal"] = (
        df["g3_market_style"].isin(["main_up", "weak_recovery"])
        & df["runup_from_60d_low"].le(1.0).fillna(True)
        & df["score_volume5"].notna()
    )
    return df.sort_values(["entry_date", "g3_strong_score", "v4_rank", "code"], ascending=[True, False, True, True])


def candidate_source(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "entry_date",
        "code",
        "name",
        "g3_market_style",
        "g3_route_weight",
        "g3_strong_score",
        "v4_rank",
        "v4_score",
        "score_volume5",
        "runup_from_60d_low",
        "sector_strong",
        "sector_score_bonus",
        "l3_s3",
        "l2_s3",
        "g2_v2_buy_logic",
        "signal_family",
        "entry_price",
        "confirm_datetime",
        "intraday_normal_datetime",
        "intraday_normal_mode",
        "index_close",
        "index_ma20",
        "index_ma60",
        "index_mom20",
        "market_breadth",
    ]
    existing = [c for c in cols if c in df.columns]
    out = df.loc[df["g3_strong_v2_signal"], existing].copy()
    out["research_source"] = "g3_strong_v2_independent_source_v1"
    out["candidate_family"] = "strong_v2_volume5_sector_continuation"
    return out


def variant_frame(base: pd.DataFrame, variant: str) -> pd.DataFrame:
    df = base[base["g3_strong_v2_signal"]].copy()
    if variant == "strong_v2_main_up_only":
        return df[df["g3_market_style"].eq("main_up")].assign(position_weight=1.0)
    if variant == "strong_v2_main_up_plus_weak04":
        return df.assign(position_weight=df["g3_route_weight"])
    if variant == "strong_v2_sector_confirmed":
        return df[df["sector_strong"].eq(True)].assign(position_weight=df["g3_route_weight"])
    if variant == "strong_v2_top_rank80":
        return df[df["v4_rank"].le(80)].assign(position_weight=df["g3_route_weight"])
    raise ValueError(variant)


def exit_map(calendar: list[pd.Timestamp], hold_days: int) -> dict[pd.Timestamp, pd.Timestamp]:
    out = {}
    for idx, day in enumerate(calendar):
        j = idx + hold_days - 1
        if j < len(calendar):
            out[day] = calendar[j]
    return out


def simulate(candidates: pd.DataFrame, hold_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.copy()
    d["gross_ret"] = pick_gross_ret(d, hold_days)
    d = d.dropna(subset=["entry_date", "gross_ret", "position_weight"]).copy()
    d["net_ret"] = d["gross_ret"] - COST_BPS / 10000.0
    calendar = trade_calendar(d["entry_date"].min(), d["entry_date"].max() + pd.Timedelta(days=hold_days * 3 + 10))
    exits = exit_map(calendar, hold_days)
    d["policy_exit_date"] = d["entry_date"].map(exits)
    d = d.dropna(subset=["policy_exit_date"]).copy()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"]).dt.normalize()
    d = d.sort_values(["entry_date", "g3_strong_score", "v4_rank", "code"], ascending=[True, False, True, True])

    sim_calendar = trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max())
    by_day = {day: g.copy() for day, g in d.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict] = []
    closed: list[dict] = []
    curve_rows: list[dict] = []

    for day in sim_calendar:
        still_open = []
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
                still_open.append(pos)
        open_pos = still_open

        opened = 0
        todays = by_day.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= DAILY_OPEN_LIMIT or len(open_pos) >= SLOTS:
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * SLOT_PCT * float(getattr(row, "position_weight"))
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


def summarize(variant: str, hold_days: int, closed: pd.DataFrame, curve: pd.DataFrame) -> dict:
    if curve.empty:
        return {
            "variant": variant,
            "hold_days": hold_days,
            "trade_count": 0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": math.nan,
            "avg_trade_return": math.nan,
            "worst_trade": math.nan,
        }
    total = float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0)
    ret = pd.to_numeric(closed.get("net_ret"), errors="coerce") if not closed.empty else pd.Series(dtype="float64")
    return {
        "variant": variant,
        "hold_days": hold_days,
        "trade_count": int(len(closed)),
        "total_return": total,
        "max_drawdown": max_drawdown(curve["equity"]),
        "win_rate": float((ret > 0).mean()) if not ret.empty else math.nan,
        "avg_trade_return": float(ret.mean()) if not ret.empty else math.nan,
        "worst_trade": float(ret.min()) if not ret.empty else math.nan,
        "bad10_rate": float((ret <= -0.10).mean()) if not ret.empty else math.nan,
        "start_date": curve["date"].iloc[0].date().isoformat(),
        "end_date": curve["date"].iloc[-1].date().isoformat(),
    }


def window_summary(curve: pd.DataFrame, variant: str, hold_days: int) -> pd.DataFrame:
    rows = []
    for name, (start, end) in WINDOWS.items():
        d = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
        if d.empty:
            rows.append({"variant": variant, "hold_days": hold_days, "window": name, "return": math.nan, "max_drawdown": math.nan, "days": 0})
            continue
        rows.append(
            {
                "variant": variant,
                "hold_days": hold_days,
                "window": name,
                "return": float(d["equity"].iloc[-1] / d["equity"].iloc[0] - 1.0),
                "max_drawdown": max_drawdown(d["equity"]),
                "days": len(d),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = build_base()
    src = candidate_source(base)
    src.to_csv(OUT_DIR / "strong_v2_candidate_source_no_future.csv", index=False, encoding="utf-8-sig")

    source_counts = (
        src.groupby(["g3_market_style", "sector_strong"], dropna=False)
        .agg(rows=("code", "size"), unique_days=("entry_date", "nunique"), avg_score=("g3_strong_score", "mean"))
        .reset_index()
    )
    source_counts.to_csv(OUT_DIR / "strong_v2_source_counts.csv", index=False, encoding="utf-8-sig")

    variants = [
        "strong_v2_main_up_only",
        "strong_v2_main_up_plus_weak04",
        "strong_v2_sector_confirmed",
        "strong_v2_top_rank80",
    ]
    summaries = []
    windows = []
    for variant in variants:
        frame = variant_frame(base, variant)
        eval_cols = [
            c
            for c in frame.columns
            if not c.startswith("fwd_ret_")
            and not c.startswith("mfe_close_")
            and not c.startswith("mae_close_")
        ]
        frame[eval_cols].to_csv(OUT_DIR / f"{variant}_candidates_no_forward.csv", index=False, encoding="utf-8-sig")
        for hold_days in [5, 10, 20]:
            closed, curve = simulate(frame, hold_days)
            closed.to_csv(OUT_DIR / f"{variant}_hold{hold_days}_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(OUT_DIR / f"{variant}_hold{hold_days}_curve.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(variant, hold_days, closed, curve))
            if not curve.empty:
                windows.append(window_summary(curve, variant, hold_days))

    summary_df = pd.DataFrame(summaries).sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    window_df = pd.concat(windows, ignore_index=True) if windows else pd.DataFrame()
    summary_df.to_csv(OUT_DIR / "strong_v2_eval_summary.csv", index=False, encoding="utf-8-sig")
    window_df.to_csv(OUT_DIR / "strong_v2_window_summary.csv", index=False, encoding="utf-8-sig")

    best = summary_df.iloc[0].to_dict() if not summary_df.empty else {}
    selected = summary_df[
        summary_df["variant"].isin(["strong_v2_main_up_only", "strong_v2_main_up_plus_weak04"])
        & summary_df["hold_days"].isin([10, 20])
    ].copy()

    report = [
        "# G3 Strong V2 独立候选源 V1",
        "",
        "## 本步目标",
        "",
        "把 G2 full 的强势赚钱思想转成 G3 独立候选源：强势主升做资金延续，弱修复只小权重试错；候选源和前向评估分离，避免把未来收益混入实盘候选。",
        "",
        "## 候选源口径",
        "",
        f"- 输入源：`{SOURCE.relative_to(ROOT)}`",
        "- 仅使用 `source_family=volume5`。",
        "- 强势思想：`volume5_keep80_runup + sector_score_bonus`。",
        "- 市场路由：`main_up` 权重 1.0，`weak_recovery` 权重 0.4，其他状态不买。",
        "- 过滤：`runup_from_60d_low <= 100%`，且存在 `score_volume5`。",
        "- 评估：slot5、单日最多开 2 笔、30bps 成本，分别测试 5/10/20 日持有代理。",
        "",
        "## 候选源分布",
        "",
        md_table(source_counts),
        "",
        "## Full 评估摘要",
        "",
        md_table(
            summary_df[
                [
                    "variant",
                    "hold_days",
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
        "## 重点候选分窗口",
        "",
        md_table(
            window_df[
                window_df["variant"].isin(["strong_v2_main_up_only", "strong_v2_main_up_plus_weak04"])
                & window_df["hold_days"].isin([10, 20])
            ],
            {"return", "max_drawdown"},
        ),
        "",
        "## 初步结论",
        "",
        f"- 当前收益最高候选：`{best.get('variant', '')}` hold{best.get('hold_days', '')}，full 收益 `{pct(best.get('total_return'))}`，最大回撤 `{pct(best.get('max_drawdown'))}`。",
        "- `strong_v2` 已经比当前 G3 正式组合更接近 G2 的强势进攻思想，但这一步仍是前向收益代理，不是最终实盘撮合。",
        "- 如果强势候选在 2022/2023 窗口仍薄弱，这不是 strong_v2 的职责；应由 `range_v2` 和 `down_panic_v3` 补齐。",
        "- 下一步不能继续只优化 strong；必须开始重建震荡周期 `range_v2`，并用 2022/2023/2024 作为主要验收窗口。",
        "",
        "## 本步完成",
        "",
        "- 已生成 G3 strong_v2 独立候选源。",
        "- 已完成 5/10/20 日代理持有评估。",
        "- 已把候选源与前向收益评估拆开保存。",
        "",
        "## 下一步目标",
        "",
        "开始 `range_v2`：重新定义箱体底部、冰点、缩量与反抽确认，目标是在 2022/2023/2024 震荡或弱势震荡窗口取得超过空仓/基准的正收益。",
        "",
    ]
    (OUT_DIR / "strong_v2_independent_source_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "candidate_source": str(OUT_DIR / "strong_v2_candidate_source_no_future.csv"),
                "summary": str(OUT_DIR / "strong_v2_eval_summary.csv"),
                "best": best,
                "next_step": "build_range_v2_independent_source",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
