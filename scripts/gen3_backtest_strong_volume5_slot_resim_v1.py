from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df


SOURCE = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "sources" / "g2_v2_complete.parquet"
OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_slot_resim_v1"
INITIAL_CAPITAL = 1_000_000.0
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _money(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2f}"


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif col in {"start_equity", "end_equity"}:
                item[col] = _money(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _trade_calendar(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    sql = f"""
    SELECT DISTINCT trade_date
    FROM kline_daily
    WHERE trade_date BETWEEN toDate({_sql_literal(start.strftime('%Y-%m-%d'))})
      AND toDate({_sql_literal(end.strftime('%Y-%m-%d'))})
    ORDER BY trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return []
    return pd.to_datetime(d["trade_date"]).dt.normalize().tolist()


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _classify_market_style(row: pd.Series) -> str:
    close = row.get("index_close")
    ma20 = row.get("index_ma20")
    ma60 = row.get("index_ma60")
    mom20 = row.get("index_mom20")
    breadth = row.get("market_breadth")
    if pd.notna(close) and pd.notna(ma20) and pd.notna(ma60) and close >= ma20 >= ma60:
        return "main_up"
    if (
        pd.notna(close)
        and pd.notna(ma60)
        and pd.notna(ma20)
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


def _exit_date_map(calendar: list[pd.Timestamp], hold_days: int) -> dict[pd.Timestamp, pd.Timestamp]:
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for idx, day in enumerate(calendar):
        exit_idx = idx + hold_days - 1
        if exit_idx < len(calendar):
            out[day] = calendar[exit_idx]
    return out


def _prepare_candidates(hold_days: int, cost_bps: float) -> pd.DataFrame:
    df = pd.read_parquet(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df = df[df["source_family"].eq("volume5")].copy()
    ret_col = f"outcome_fwd_ret_{hold_days}d"
    fallback_col = f"fwd_ret_{hold_days}d"
    if ret_col in df.columns and fallback_col in df.columns:
        df["gross_ret"] = pd.to_numeric(df[ret_col], errors="coerce").where(
            pd.to_numeric(df[ret_col], errors="coerce").notna(),
            pd.to_numeric(df[fallback_col], errors="coerce"),
        )
    elif ret_col in df.columns:
        df["gross_ret"] = pd.to_numeric(df[ret_col], errors="coerce")
    elif fallback_col in df.columns:
        df["gross_ret"] = pd.to_numeric(df[fallback_col], errors="coerce")
    else:
        raise ValueError(f"missing forward return columns for hold_days={hold_days}")

    df = df.dropna(subset=["entry_date", "gross_ret", "code"]).copy()
    df["net_ret"] = df["gross_ret"] - cost_bps / 10000.0
    df["v4_rank"] = pd.to_numeric(df.get("v4_rank"), errors="coerce").fillna(1e9)
    df["v4_score"] = pd.to_numeric(df.get("v4_score"), errors="coerce").fillna(-1e9)
    df["g3_market_style"] = df.apply(_classify_market_style, axis=1)

    calendar = _trade_calendar(df["entry_date"].min(), df["entry_date"].max() + pd.Timedelta(days=60))
    exit_map = _exit_date_map(calendar, hold_days)
    df["policy_exit_date"] = df["entry_date"].map(exit_map)
    df = df.dropna(subset=["policy_exit_date"]).copy()
    df["policy_exit_date"] = pd.to_datetime(df["policy_exit_date"]).dt.normalize()
    sort_cols = ["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"]
    existing_sort = [c for c in sort_cols if c in df.columns]
    ascending = [True, True, False, True, True][: len(existing_sort)]
    return df.sort_values(existing_sort, ascending=ascending)


def _variant_frame(base: pd.DataFrame, variant: str) -> pd.DataFrame:
    if variant == "all_volume5":
        return base.copy()
    if variant == "main_up_volume5":
        return base[base["g3_market_style"].eq("main_up")].copy()
    if variant == "core_recovery_volume5":
        return base[base["g3_market_style"].isin(["main_up", "weak_recovery"])].copy()
    raise ValueError(variant)


def _simulate_slots(
    candidates: pd.DataFrame,
    slots: int,
    slot_pct: float,
    daily_open_limit: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    calendar = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    by_entry = {day: g.copy() for day, g in candidates.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict] = []
    closed: list[dict] = []
    curve_rows: list[dict] = []

    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict] = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open

        opened = 0
        skipped_slots = 0
        skipped_daily_limit = 0
        todays = by_entry.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= daily_open_limit:
                    skipped_daily_limit += 1
                    continue
                if len(open_pos) >= slots:
                    skipped_slots += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * slot_pct
                if stake <= 0 or cash < stake:
                    skipped_slots += 1
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1

        reserved_principal = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved_principal
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": reserved_principal,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped_slots": skipped_slots,
                "skipped_daily_limit": skipped_daily_limit,
                "realized_pnl": realized_pnl,
            }
        )

    closed_df = pd.DataFrame(closed)
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return closed_df, curve


def _metrics(
    closed: pd.DataFrame,
    curve: pd.DataFrame,
    window: str,
    variant: str,
    hold_days: int,
    book: str,
    cost_bps: float,
) -> dict:
    if curve.empty:
        return {
            "window": window,
            "variant": variant,
            "hold_days": hold_days,
            "book": book,
            "cost_bps": cost_bps,
            "closed": 0,
        }
    start, end = WINDOWS[window]
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    if cw.empty:
        return {
            "window": window,
            "variant": variant,
            "hold_days": hold_days,
            "book": book,
            "cost_bps": cost_bps,
            "closed": 0,
        }
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local = cw["equity"] / start_equity
    dd = local / local.cummax() - 1.0
    return {
        "window": window,
        "variant": variant,
        "hold_days": hold_days,
        "book": book,
        "cost_bps": cost_bps,
        "closed": int(len(tw)),
        "unique_codes": int(tw["code"].nunique()) if not tw.empty else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(dd.min()),
        "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
        "max_open_positions": int(cw["open_positions"].max()),
        "skipped_slots": int(cw["skipped_slots"].sum()),
        "skipped_daily_limit": int(cw["skipped_daily_limit"].sum()),
    }


def _write_report(summary: pd.DataFrame, style_counts: dict[str, int]) -> None:
    pct_cols = {"total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade"}
    full = summary[summary["window"].eq("full")].sort_values(["variant", "hold_days", "book"]).copy()
    by_window = summary[
        summary["variant"].eq("core_recovery_volume5")
        & summary["book"].eq("slot2_50pct_daily1")
        & summary["hold_days"].isin([5, 10, 20])
    ].sort_values(["hold_days", "window"])

    report = [
        "# G3 强势链路 Volume5 槽位复算 V1",
        "",
        "## 口径",
        "",
        "- 源：只读 `reports/gen2_v2_complete_strategy_2020/sources/g2_v2_complete.parquet`，筛选 `source_family=volume5`。",
        "- 这不是 G2 正式回测复刻，也不触发 G2 重建；它是 G3 强势候选源迁移前的独立槽位压力检查。",
        "- 买入排序固定为 `entry_date -> v4_rank -> v4_score -> confirm_datetime -> code`。",
        "- 卖出用固定 5/10/20 交易日前瞻收益代理，扣 30bps 成本；当前曲线是已实现收益曲线，不做日内 MTM。",
        "- 槽位只测两种预注册账本：`slot2_50pct_daily1` 与 `slot5_20pct_daily2`，不按结果临时调参。",
        "",
        "## 市场风格分布",
        "",
        "```json",
        json.dumps(style_counts, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Full 窗口结果",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## Core Recovery 分窗口",
        "",
        _md_table(by_window, pct_cols=pct_cols),
        "",
        "## 结论",
        "",
        "- `volume5` 在槽位约束下仍有进攻性，但该版本仍是固定持有代理，不能等同于可实盘强势链路。",
        "- 如果强势链路要进入 G3 正式候选，下一步不应继续筛参数，而应迁移/重写 G2 的退出、冷却、止损与盘中可见性约束。",
        "- 如果某些年份仍有明显回撤，后续要从市场风格暂停与执行退出上处理，不应该把单一年份亏损样本反向刻成选股规则。",
        "",
    ]
    (OUT_DIR / "strong_volume5_slot_resim_report_cn.md").write_text("\n".join(report), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cost_bps = 30.0
    variants = ["all_volume5", "main_up_volume5", "core_recovery_volume5"]
    books = [
        ("slot2_50pct_daily1", 2, 0.50, 1),
        ("slot5_20pct_daily2", 5, 0.20, 2),
    ]

    summaries: list[dict] = []
    style_counts: dict[str, int] = {}
    for hold_days in [5, 10, 20]:
        base = _prepare_candidates(hold_days=hold_days, cost_bps=cost_bps)
        if hold_days == 5:
            style_counts = {str(k): int(v) for k, v in base["g3_market_style"].value_counts(dropna=False).to_dict().items()}
        for variant in variants:
            part = _variant_frame(base, variant)
            for book, slots, slot_pct, daily_open_limit in books:
                closed, curve = _simulate_slots(part, slots=slots, slot_pct=slot_pct, daily_open_limit=daily_open_limit)
                stem = f"{variant}_hold{hold_days}_{book}"
                closed.to_csv(OUT_DIR / f"{stem}_closed_trades.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(OUT_DIR / f"{stem}_curve.csv", index=False, encoding="utf-8-sig")
                for window in WINDOWS:
                    summaries.append(
                        _metrics(
                            closed,
                            curve,
                            window=window,
                            variant=variant,
                            hold_days=hold_days,
                            book=book,
                            cost_bps=cost_bps,
                        )
                    )

    summary = pd.DataFrame(summaries)
    summary.to_csv(OUT_DIR / "strong_volume5_slot_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = summary.copy()
    for col in ["total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade"]:
        if col in display.columns:
            display[col] = display[col].map(_pct)
    for col in ["start_equity", "end_equity"]:
        if col in display.columns:
            display[col] = display[col].map(_money)
    display.to_csv(OUT_DIR / "strong_volume5_slot_summary_display.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, style_counts)
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "summary_rows": int(len(summary)),
                "style_counts_hold5": style_counts,
                "full_core_slot2": summary[
                    summary["window"].eq("full")
                    & summary["variant"].eq("core_recovery_volume5")
                    & summary["book"].eq("slot2_50pct_daily1")
                ][["hold_days", "closed", "total_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade"]].to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
