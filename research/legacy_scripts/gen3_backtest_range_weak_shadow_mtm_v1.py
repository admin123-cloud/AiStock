from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table, _trade_calendar
from utils.market_warehouse import clickhouse_query_df


SOURCE = ROOT / "reports" / "gen3_range_weak_rank_capacity_v1" / "topn_selected_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_weak_shadow_mtm_v1"
INITIAL_CAPITAL = 150000.0
SLOT_PCT = 0.20
SLOTS = 5
HOLD_DAYS = 5
PROFILES = [
    {
        "book": "range_stress_top1",
        "probe_family": "range",
        "probe": "stress_no_crash",
        "top_n": 1,
        "note": "横盘 stress_no_crash Top1，固定 5 日持有",
    },
    {
        "book": "weak_low_top1",
        "probe_family": "weak_rebound",
        "probe": "low_not_chasing",
        "top_n": 1,
        "note": "弱反弹 low_not_chasing Top1，固定 5 日持有",
    },
]
COST_BPS_LIST = (0, 30)


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_source() -> pd.DataFrame:
    d = pd.read_parquet(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["top_n", "entry_open", "fwd_ret_open_to_close_5d", "candidate_score", "amount20"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["entry_date", "code", "entry_open", "fwd_ret_open_to_close_5d"])
    return d


def _next_exit_dates(entry_dates: pd.Series) -> dict[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(entry_dates.min()).normalize()
    end = pd.Timestamp(entry_dates.max()).normalize() + pd.Timedelta(days=30)
    cal = _trade_calendar(start, end)
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for i, day in enumerate(cal):
        if i + HOLD_DAYS - 1 < len(cal):
            out[day] = cal[i + HOLD_DAYS - 1]
    return out


def _select_book(source: pd.DataFrame, spec: dict, cost_bps: int) -> pd.DataFrame:
    d = source[
        source["probe_family"].eq(spec["probe_family"])
        & source["probe"].eq(spec["probe"])
        & source["top_n"].eq(spec["top_n"])
    ].copy()
    exit_map = _next_exit_dates(d["entry_date"])
    d["policy_exit_date"] = d["entry_date"].map(exit_map)
    d["book"] = spec["book"]
    d["rank"] = 1
    d["entry_price"] = d["entry_open"]
    d["net_ret"] = d["fwd_ret_open_to_close_5d"] - float(cost_bps) / 10000.0
    d["cost_bps"] = cost_bps
    d = d.dropna(subset=["policy_exit_date", "entry_price", "net_ret"])
    return d.sort_values(["entry_date", "candidate_score", "amount20"], ascending=[True, False, False])


def _load_daily_prices(candidates: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(candidates["code"].astype(str).unique().tolist())
    start = pd.Timestamp(candidates["entry_date"].min()).strftime("%Y-%m-%d")
    end = pd.Timestamp(candidates["policy_exit_date"].max()).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 500):
        quoted = ",".join(_sql_literal(c) for c in codes[i : i + 500])
        sql = f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if out.empty:
        return out
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.normalize()
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.dropna(subset=["code", "trade_date", "close"])


def _simulate_mtm(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    calendar = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    by_entry = {day: g.copy() for day, g in candidates.groupby("entry_date")}
    price_df = _load_daily_prices(candidates)
    prices = {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in price_df.itertuples(index=False)}
    cash = INITIAL_CAPITAL
    open_pos: list[dict] = []
    closed: list[dict] = []
    curve_rows: list[dict] = []

    for day in calendar:
        still_open: list[dict] = []
        realized_pnl = 0.0
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                done = pos.copy()
                done["exit_value"] = exit_value
                done["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(done)
            else:
                still_open.append(pos)
        open_pos = still_open

        opened = 0
        skipped_slots = 0
        todays = by_entry.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= 1 or len(open_pos) >= SLOTS:
                    skipped_slots += 1
                    continue
                mtm_open = 0.0
                for pos in open_pos:
                    close = prices.get((str(pos["code"]), day))
                    mtm_open += float(pos["stake"]) if close is None else float(pos["stake"]) * close / float(pos["entry_price"])
                equity_before = cash + mtm_open
                stake = equity_before * SLOT_PCT
                if stake <= 0 or cash < stake:
                    skipped_slots += 1
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1

        mtm_value = 0.0
        worst_open_mtm_ret = 0.0
        missing_close_positions = 0
        for pos in open_pos:
            close = prices.get((str(pos["code"]), day))
            if close is None:
                mtm_value += float(pos["stake"])
                missing_close_positions += 1
                continue
            mtm_ret = close / float(pos["entry_price"]) - 1.0
            worst_open_mtm_ret = min(worst_open_mtm_ret, float(mtm_ret))
            mtm_value += float(pos["stake"]) * (1.0 + float(mtm_ret))
        equity = cash + mtm_value
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "mtm_value": mtm_value,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped_slots": skipped_slots,
                "realized_pnl": realized_pnl,
                "worst_open_mtm_ret": worst_open_mtm_ret,
                "missing_close_positions": missing_close_positions,
            }
        )

    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    curve["peak"] = curve["equity"].cummax()
    curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
    curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return closed_df, curve


def _metrics(book: str, cost_bps: int, curve: pd.DataFrame, closed: pd.DataFrame) -> dict:
    return {
        "book": book,
        "cost_bps": cost_bps,
        "start": pd.Timestamp(curve["date"].min()).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(curve["date"].max()).strftime("%Y-%m-%d"),
        "total_ret": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": float(curve["drawdown"].min()),
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        "max_open_positions": int(curve["open_positions"].max()),
        "closed": int(len(closed)),
        "win_rate": float((closed["net_ret"] > 0).mean()) if not closed.empty else 0.0,
        "mean_trade_ret": float(closed["net_ret"].mean()) if not closed.empty else 0.0,
        "median_trade_ret": float(closed["net_ret"].median()) if not closed.empty else 0.0,
        "worst_trade": float(closed["net_ret"].min()) if not closed.empty else 0.0,
        "bad10_rate": float((closed["net_ret"] <= -0.10).mean()) if not closed.empty else 0.0,
    }


def _annual(book: str, cost_bps: int, curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    closed = closed.copy()
    if not closed.empty:
        closed["entry_date"] = pd.to_datetime(closed["entry_date"], errors="coerce")
    for year, part in curve.groupby(pd.to_datetime(curve["date"]).dt.year):
        part = part.sort_values("date")
        trades = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "book": book,
                "cost_bps": cost_bps,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": float(part["equity"].div(part["equity"].cummax()).sub(1.0).min()),
                "worst_open_mtm_ret": float(part["worst_open_mtm_ret"].min()),
                "closed": int(len(trades)),
                "win_rate": float((trades["net_ret"] > 0).mean()) if not trades.empty else 0.0,
                "mean_trade_ret": float(trades["net_ret"].mean()) if not trades.empty else 0.0,
                "worst_trade": float(trades["net_ret"].min()) if not trades.empty else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _combine_50_50(curves: dict[str, pd.DataFrame], cost_bps: int) -> pd.DataFrame:
    dates = sorted(set().union(*(set(pd.to_datetime(c["date"]).dt.normalize()) for c in curves.values())))
    out = pd.DataFrame({"date": dates})
    eq = pd.Series(0.0, index=out.index)
    opens = pd.Series(0, index=out.index)
    worst = pd.Series(0.0, index=out.index)
    for _, curve in curves.items():
        c = curve.copy()
        c["date"] = pd.to_datetime(c["date"]).dt.normalize()
        c = c.set_index("date").reindex(dates).ffill()
        c["equity"] = c["equity"].fillna(INITIAL_CAPITAL)
        c["open_positions"] = c["open_positions"].fillna(0)
        c["worst_open_mtm_ret"] = c["worst_open_mtm_ret"].fillna(0.0)
        eq += 0.5 * (c["equity"].reset_index(drop=True) / INITIAL_CAPITAL)
        opens += c["open_positions"].reset_index(drop=True).astype(int)
        worst = pd.concat([worst, c["worst_open_mtm_ret"].reset_index(drop=True)], axis=1).min(axis=1)
    out["equity"] = eq * INITIAL_CAPITAL
    out["open_positions"] = opens
    out["worst_open_mtm_ret"] = worst
    out["peak"] = out["equity"].cummax()
    out["drawdown"] = out["equity"] / out["peak"] - 1.0
    out["ret_from_start"] = out["equity"] / INITIAL_CAPITAL - 1.0
    out["book"] = "range_weak_50_50"
    out["cost_bps"] = cost_bps
    return out


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame) -> None:
    pct_cols = {
        "total_ret",
        "max_drawdown",
        "worst_open_mtm_ret",
        "win_rate",
        "mean_trade_ret",
        "median_trade_ret",
        "worst_trade",
        "bad10_rate",
        "return",
    }
    lines = [
        "# G3 横盘/弱反弹 Shadow MTM V1",
        "",
        "## 口径",
        "",
        "- 只验证第38步保留下来的两个 shadow 方向：`range_stress_top1` 与 `weak_low_top1`。",
        "- 信号日收盘后可见，下一交易日开盘买入，固定持有 5 个交易日收盘退出。",
        "- 每条袖珍账户 `slot5`，单笔 20%，同日最多开 1 笔；输出 0bps 与 30bps 两个成本口径。",
        "- 这是影子资金曲线，不进入 G3 正式组合，也不写 G2 runtime。",
        "",
        "## Full 窗口",
        "",
        _md_table(summary.sort_values(["cost_bps", "book"]), pct_cols=pct_cols),
        "",
        "## 年度",
        "",
        _md_table(annual.sort_values(["cost_bps", "book", "year"]), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 若 30bps 下任一 shadow 袖珍账户年度不稳定或尾部过深，只保留观察，不并入组合。",
        "- 若 `range_weak_50_50` 仍不能改善 2023/2024，则横盘/弱反弹链路需要继续重写候选源。",
        "",
    ]
    (OUT_DIR / "range_weak_shadow_mtm_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = _load_source()
    summary_rows = []
    annual_parts = []
    for cost_bps in COST_BPS_LIST:
        curves_for_combo: dict[str, pd.DataFrame] = {}
        closed_for_combo: list[pd.DataFrame] = []
        for spec in PROFILES:
            book = spec["book"]
            candidates = _select_book(source, spec, cost_bps)
            closed, curve = _simulate_mtm(candidates)
            candidates.to_csv(OUT_DIR / f"{book}_cost{cost_bps}_candidates.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(OUT_DIR / f"{book}_cost{cost_bps}_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(OUT_DIR / f"{book}_cost{cost_bps}_mtm_curve.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(_metrics(book, cost_bps, curve, closed))
            annual_parts.append(_annual(book, cost_bps, curve, closed))
            curves_for_combo[book] = curve
            closed_for_combo.append(closed.assign(book=book) if not closed.empty else closed)

        combo_curve = _combine_50_50(curves_for_combo, cost_bps)
        combo_closed = pd.concat(closed_for_combo, ignore_index=True) if closed_for_combo else pd.DataFrame()
        combo_curve.to_csv(OUT_DIR / f"range_weak_50_50_cost{cost_bps}_mtm_curve.csv", index=False, encoding="utf-8-sig")
        combo_closed.to_csv(OUT_DIR / f"range_weak_50_50_cost{cost_bps}_closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(_metrics("range_weak_50_50", cost_bps, combo_curve, combo_closed))
        annual_parts.append(_annual("range_weak_50_50", cost_bps, combo_curve, combo_closed))

    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_parts, ignore_index=True)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual)
    print(json.dumps({"out_dir": str(OUT_DIR), "summary": summary.to_dict(orient="records")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
