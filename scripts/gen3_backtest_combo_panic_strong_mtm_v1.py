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


OUT_DIR = ROOT / "reports" / "gen3_combo_panic_strong_mtm_v1"
PANIC_SOURCE = (
    ROOT
    / "reports"
    / "gen3_panic_v2_research"
    / "final_candidate_v1"
    / "m30_close5_full_nextopen_cost30_policy_candidates.csv"
)
STRONG_SOURCE = (
    ROOT
    / "reports"
    / "gen3_strong_volume5_risk_layer_execution_stress_v1"
    / "half_d2_le0_then_d3_next_open_second_leg_30bps_candidates.csv"
)


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _normalize_panic() -> pd.DataFrame:
    d = pd.read_csv(PANIC_SOURCE)
    out = pd.DataFrame(
        {
            "chain": "panic",
            "entry_date": pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize(),
            "policy_exit_date": pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize(),
            "code": d["code"].astype(str),
            "name": d.get("name", pd.Series([""] * len(d))).astype(str),
            "rank": pd.to_numeric(d.get("chain_rank"), errors="coerce").fillna(9999),
            "score": pd.to_numeric(d.get("candidate_score"), errors="coerce").fillna(0.0),
            "entry_price": pd.to_numeric(d.get("entry_price_adjusted"), errors="coerce"),
            "net_ret": pd.to_numeric(d["net_ret"], errors="coerce"),
            "market_style": d.get("market_style", pd.Series([""] * len(d))).astype(str),
            "exit_reason": d.get("exit_source", pd.Series([""] * len(d))).astype(str),
        }
    )
    return out.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "net_ret"]).sort_values(
        ["entry_date", "rank", "score", "code"], ascending=[True, True, False, True]
    )


def _normalize_strong() -> pd.DataFrame:
    d = pd.read_csv(STRONG_SOURCE)
    out = pd.DataFrame(
        {
            "chain": "strong",
            "entry_date": pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize(),
            "policy_exit_date": pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize(),
            "code": d["code"].astype(str),
            "name": d.get("name", pd.Series([""] * len(d))).astype(str),
            "rank": pd.to_numeric(d.get("v4_rank"), errors="coerce").fillna(9999),
            "score": pd.to_numeric(d.get("v4_score"), errors="coerce").fillna(0.0),
            "entry_price": pd.to_numeric(d.get("entry_price"), errors="coerce"),
            "net_ret": pd.to_numeric(d["net_ret"], errors="coerce"),
            "market_style": d.get("g3_market_style", pd.Series([""] * len(d))).astype(str),
            "exit_reason": d.get("exit_reason_proxy", pd.Series([""] * len(d))).astype(str),
        }
    )
    return out.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "net_ret"]).sort_values(
        ["entry_date", "rank", "score", "code"], ascending=[True, True, False, True]
    )


def _load_daily_prices(candidates: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(candidates["code"].dropna().astype(str).unique().tolist())
    start = pd.Timestamp(candidates["entry_date"].min()).strftime("%Y-%m-%d")
    end = pd.Timestamp(candidates["policy_exit_date"].max()).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 300):
        quoted = ",".join(_sql_literal(c) for c in codes[i : i + 300])
        sql = f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
        ORDER BY code, trade_date
        """
        p = clickhouse_query_df(sql)
        if not p.empty:
            parts.append(p)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    return d.dropna(subset=["code", "trade_date", "close"])


def _price_map(daily: pd.DataFrame) -> dict[tuple[str, pd.Timestamp], float]:
    return {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in daily.itertuples(index=False)}


def _simulate_mtm(candidates: pd.DataFrame, initial_capital: float, slots: int = 5, slot_pct: float = 0.20, daily_open_limit: int = 2) -> tuple[pd.DataFrame, pd.DataFrame]:
    calendar = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    by_entry = {day: g.copy() for day, g in candidates.groupby("entry_date")}
    prices = _price_map(_load_daily_prices(candidates))
    cash = float(initial_capital)
    open_pos: list[dict] = []
    closed: list[dict] = []
    curve_rows: list[dict] = []

    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict] = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
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
        skipped_daily_limit = 0
        skipped_slots = 0
        todays = by_entry.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= daily_open_limit:
                    skipped_daily_limit += 1
                    continue
                if len(open_pos) >= slots:
                    skipped_slots += 1
                    continue
                mtm_open = 0.0
                for p in open_pos:
                    close = prices.get((str(p["code"]), day))
                    if close is None or float(p["entry_price"]) <= 0:
                        mtm_open += float(p["stake"])
                    else:
                        mtm_open += float(p["stake"]) * close / float(p["entry_price"])
                equity_before = cash + mtm_open
                stake = equity_before * slot_pct
                if stake <= 0 or cash < stake:
                    skipped_slots += 1
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                    exit_value = stake * (1.0 + float(pos["net_ret"]))
                    cash += exit_value
                    realized_pnl += exit_value - stake
                    pos["exit_value"] = exit_value
                    pos["realized_pnl"] = exit_value - stake
                    closed.append(pos)
                else:
                    open_pos.append(pos)
                opened += 1

        mtm_value = 0.0
        worst_open_mtm_ret = 0.0
        missing_close_positions = 0
        for pos in open_pos:
            close = prices.get((str(pos["code"]), day))
            if close is None or float(pos["entry_price"]) <= 0:
                mtm_value += float(pos["stake"])
                missing_close_positions += 1
                continue
            mtm_ret = close / float(pos["entry_price"]) - 1.0
            worst_open_mtm_ret = min(worst_open_mtm_ret, float(mtm_ret))
            mtm_value += float(pos["stake"]) * (1.0 + float(mtm_ret))
        reserved_principal = sum(float(p["stake"]) for p in open_pos)
        equity = cash + mtm_value
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": reserved_principal,
                "mtm_value": mtm_value,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped_slots": skipped_slots,
                "skipped_daily_limit": skipped_daily_limit,
                "realized_pnl": realized_pnl,
                "worst_open_mtm_ret": worst_open_mtm_ret,
                "missing_close_positions": missing_close_positions,
            }
        )

    closed_df = pd.DataFrame(closed)
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / initial_capital - 1.0
    return closed_df, curve


def _combine_curves(curves: dict[str, pd.DataFrame], weights: dict[str, float], name: str) -> pd.DataFrame:
    all_dates = sorted(set().union(*(set(pd.to_datetime(c["date"]).dt.normalize()) for c in curves.values())))
    out = pd.DataFrame({"date": all_dates})
    equity = pd.Series(0.0, index=out.index)
    open_positions = pd.Series(0, index=out.index)
    worst_open = pd.Series(0.0, index=out.index)
    for chain, curve in curves.items():
        c = curve.copy()
        c["date"] = pd.to_datetime(c["date"]).dt.normalize()
        c = c.set_index("date").reindex(all_dates).ffill()
        normalized = c["equity"].fillna(1.0)
        equity += weights[chain] * normalized.values
        open_positions += c["open_positions"].fillna(0).astype(int).values
        worst_open = pd.concat([worst_open, c["worst_open_mtm_ret"].fillna(0).reset_index(drop=True)], axis=1).min(axis=1)
    out["equity"] = equity
    out["open_positions"] = open_positions
    out["worst_open_mtm_ret"] = worst_open
    out["peak"] = out["equity"].cummax()
    out["drawdown"] = out["equity"] / out["peak"] - 1.0
    out["ret_from_start"] = out["equity"] - 1.0
    out["book"] = name
    return out


def _make_shared_candidates(panic: pd.DataFrame, strong: pd.DataFrame) -> pd.DataFrame:
    d = pd.concat([panic, strong], ignore_index=True)
    d["chain_priority"] = d["chain"].map({"panic": 0, "strong": 1}).fillna(9)
    return d.sort_values(
        ["entry_date", "chain_priority", "rank", "score", "code"],
        ascending=[True, True, True, False, True],
    ).drop(columns=["chain_priority"])


def _metrics(curve: pd.DataFrame, closed: pd.DataFrame | None, book: str) -> dict:
    if curve.empty:
        return {"book": book}
    return {
        "book": book,
        "start": pd.Timestamp(curve["date"].min()).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(curve["date"].max()).strftime("%Y-%m-%d"),
        "total_ret": float(curve["equity"].iloc[-1] - 1.0),
        "max_drawdown": float(curve["drawdown"].min()),
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()) if "worst_open_mtm_ret" in curve else 0.0,
        "max_open_positions": int(curve["open_positions"].max()) if "open_positions" in curve else 0,
        "closed": int(len(closed)) if closed is not None else 0,
        "win_rate": float((closed["net_ret"] > 0).mean()) if closed is not None and len(closed) else 0.0,
        "mean_trade_ret": float(closed["net_ret"].mean()) if closed is not None and len(closed) else 0.0,
        "worst_trade": float(closed["net_ret"].min()) if closed is not None and len(closed) else 0.0,
        "bad10_rate": float((closed["net_ret"] <= -0.10).mean()) if closed is not None and len(closed) else 0.0,
    }


def _annual(curve: pd.DataFrame, closed: pd.DataFrame | None, book: str) -> pd.DataFrame:
    rows = []
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"]).dt.normalize()
    for year, part in c.groupby(c["date"].dt.year):
        part = part.sort_values("date")
        trades = closed[pd.to_datetime(closed["entry_date"]).dt.year.eq(year)] if closed is not None and not closed.empty else pd.DataFrame()
        rows.append(
            {
                "book": book,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": float(part["equity"].div(part["equity"].cummax()).sub(1.0).min()),
                "worst_open_mtm_ret": float(part["worst_open_mtm_ret"].min()) if "worst_open_mtm_ret" in part else 0.0,
                "closed": int(len(trades)),
                "win_rate": float((trades["net_ret"] > 0).mean()) if len(trades) else 0.0,
                "mean_trade_ret": float(trades["net_ret"].mean()) if len(trades) else 0.0,
                "worst_trade": float(trades["net_ret"].min()) if len(trades) else 0.0,
                "bad10_rate": float((trades["net_ret"] <= -0.10).mean()) if len(trades) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _overlap_audit(panic: pd.DataFrame, strong: pd.DataFrame) -> pd.DataFrame:
    p = panic.copy()
    s = strong.copy()
    p["entry_date"] = pd.to_datetime(p["entry_date"]).dt.normalize()
    s["entry_date"] = pd.to_datetime(s["entry_date"]).dt.normalize()
    p_days = p.groupby("entry_date").agg(panic_candidates=("code", "count"), panic_mean_ret=("net_ret", "mean")).reset_index()
    s_days = s.groupby("entry_date").agg(strong_candidates=("code", "count"), strong_mean_ret=("net_ret", "mean")).reset_index()
    d = p_days.merge(s_days, on="entry_date", how="outer").fillna(0)
    d["both_active_signal_day"] = (d["panic_candidates"] > 0) & (d["strong_candidates"] > 0)
    return d.sort_values("entry_date")


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame, overlap: pd.DataFrame) -> None:
    pct_cols = {"total_ret", "max_drawdown", "worst_open_mtm_ret", "win_rate", "mean_trade_ret", "worst_trade", "bad10_rate", "return", "panic_mean_ret", "strong_mean_ret"}
    overlap_focus = overlap[overlap["both_active_signal_day"]].copy()
    overlap_summary = pd.DataFrame(
        [
            {
                "overlap_days": int(overlap_focus.shape[0]),
                "all_signal_days": int(((overlap["panic_candidates"] > 0) | (overlap["strong_candidates"] > 0)).sum()),
                "overlap_rate": float(overlap_focus.shape[0] / max(1, ((overlap["panic_candidates"] > 0) | (overlap["strong_candidates"] > 0)).sum())),
                "overlap_panic_mean_ret": float(overlap_focus["panic_mean_ret"].mean()) if len(overlap_focus) else 0.0,
                "overlap_strong_mean_ret": float(overlap_focus["strong_mean_ret"].mean()) if len(overlap_focus) else 0.0,
            }
        ]
    )
    lines = [
        "# G3 Panic + Strong 组合 MTM 审计 V1",
        "",
        "## 口径",
        "",
        "- Panic：`panic_v2_deep_wash_repair + low_safety_margin + avoid_midrisk + pause_weak_no_capitulation + slot5 + m30_close5_full_nextopen + 30bps`。",
        "- Strong：`core_recovery_volume5 + slot5_20pct_daily2 + half_d2_le0_then_d3 + next_open_second_leg + 30bps`。",
        "- 组合：固定 50/50 双袖珍账本，各自 slot5，不做权重搜索。",
        "- 共享资金：`shared_slots5` 使用同一资金池、总 slot5、daily_open_limit=2，同日 Panic 优先。",
        "- 本轮使用日线收盘逐日 MTM；仍未模拟真实逐笔排队和涨跌停队列。",
        "",
        "## Full 窗口",
        "",
        _md_table(summary, pct_cols=pct_cols),
        "",
        "## 年度拆分",
        "",
        _md_table(annual.sort_values(["book", "year"]), pct_cols=pct_cols),
        "",
        "## 同日信号重叠",
        "",
        _md_table(overlap_summary, pct_cols={"overlap_rate", "overlap_panic_mean_ret", "overlap_strong_mean_ret"}),
        "",
        _md_table(overlap_focus.tail(30), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果组合 2022/2023/2024 明显优于单链路，说明市场风格打法具有互补价值。",
        "- 如果组合收益主要仍由强势链路 2025/2026 贡献，G3 还不能说完成多环境打法。",
        "- 后续应先固定这个 50/50 审计口径，再决定是否做共享资金抢槽版本。",
        "",
    ]
    (OUT_DIR / "combo_panic_strong_mtm_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panic = _normalize_panic()
    strong = _normalize_strong()
    panic_closed, panic_curve = _simulate_mtm(panic, initial_capital=1.0, slots=5, slot_pct=0.20, daily_open_limit=2)
    strong_closed, strong_curve = _simulate_mtm(strong, initial_capital=1.0, slots=5, slot_pct=0.20, daily_open_limit=2)
    combo_curve = _combine_curves({"panic": panic_curve, "strong": strong_curve}, {"panic": 0.5, "strong": 0.5}, "combo_50_50")
    combo_closed = pd.concat([panic_closed.assign(chain="panic"), strong_closed.assign(chain="strong")], ignore_index=True)
    shared_candidates = _make_shared_candidates(panic, strong)
    shared_closed, shared_curve = _simulate_mtm(shared_candidates, initial_capital=1.0, slots=5, slot_pct=0.20, daily_open_limit=2)

    panic.to_csv(OUT_DIR / "panic_candidates.csv", index=False, encoding="utf-8-sig")
    strong.to_csv(OUT_DIR / "strong_candidates.csv", index=False, encoding="utf-8-sig")
    panic_closed.to_csv(OUT_DIR / "panic_closed_trades.csv", index=False, encoding="utf-8-sig")
    strong_closed.to_csv(OUT_DIR / "strong_closed_trades.csv", index=False, encoding="utf-8-sig")
    combo_closed.to_csv(OUT_DIR / "combo_closed_trades.csv", index=False, encoding="utf-8-sig")
    shared_candidates.to_csv(OUT_DIR / "shared_slots5_candidates.csv", index=False, encoding="utf-8-sig")
    shared_closed.to_csv(OUT_DIR / "shared_slots5_closed_trades.csv", index=False, encoding="utf-8-sig")
    panic_curve.assign(book="panic").to_csv(OUT_DIR / "panic_mtm_curve.csv", index=False, encoding="utf-8-sig")
    strong_curve.assign(book="strong").to_csv(OUT_DIR / "strong_mtm_curve.csv", index=False, encoding="utf-8-sig")
    combo_curve.to_csv(OUT_DIR / "combo_50_50_mtm_curve.csv", index=False, encoding="utf-8-sig")
    shared_curve.assign(book="shared_slots5").to_csv(OUT_DIR / "shared_slots5_mtm_curve.csv", index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(
        [
            _metrics(panic_curve, panic_closed, "panic"),
            _metrics(strong_curve, strong_closed, "strong"),
            _metrics(combo_curve, combo_closed, "combo_50_50"),
            _metrics(shared_curve, shared_closed, "shared_slots5"),
        ]
    )
    annual = pd.concat(
        [
            _annual(panic_curve, panic_closed, "panic"),
            _annual(strong_curve, strong_closed, "strong"),
            _annual(combo_curve, combo_closed, "combo_50_50"),
            _annual(shared_curve, shared_closed, "shared_slots5"),
        ],
        ignore_index=True,
    )
    overlap = _overlap_audit(panic, strong)
    summary.to_csv(OUT_DIR / "combo_summary_raw.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "combo_annual_raw.csv", index=False, encoding="utf-8-sig")
    overlap.to_csv(OUT_DIR / "combo_signal_overlap_by_day.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual, overlap)
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "summary": summary.to_dict(orient="records"),
                "overlap_days": int(overlap["both_active_signal_day"].sum()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
