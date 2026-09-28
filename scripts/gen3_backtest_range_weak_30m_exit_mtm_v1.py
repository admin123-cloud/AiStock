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


SOURCE = ROOT / "reports" / "gen3_range_weak_30m_tail_visibility_v1" / "range_weak_30m_visibility_trades.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_weak_30m_exit_mtm_v1"
INITIAL_CAPITAL = 150000.0
SLOT_PCT = 0.20
SLOTS = 5

BOOK_POLICIES = {
    "fixed": {
        "range_stress_top1": "fixed",
        "weak_low_top1": "fixed",
    },
    "range_close5_weak_close5": {
        "range_stress_top1": "m30_close_m5_exit",
        "weak_low_top1": "m30_close_m5_exit",
    },
    "range_close8_weak_close5": {
        "range_stress_top1": "m30_close_m8_exit",
        "weak_low_top1": "m30_close_m5_exit",
    },
}


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _pct(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _load_source() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    for col in [
        "trade_date",
        "entry_date",
        "policy_exit_date",
        "m30_close_m5_exit_datetime",
        "m30_close_m8_exit_datetime",
    ]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce")
    for col in [
        "entry_price",
        "net_ret",
        "cost_bps",
        "candidate_score",
        "amount20",
        "m30_close_m5_exit_net_ret",
        "m30_close_m8_exit_net_ret",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d[d["status"].eq("ok")].copy()
    return d.dropna(subset=["book", "code", "entry_date", "policy_exit_date", "entry_price", "net_ret"]).reset_index(drop=True)


def _apply_policy(source: pd.DataFrame, combo: str, book: str, policy: str) -> pd.DataFrame:
    d = source[source["book"].eq(book)].copy()
    d["combo"] = combo
    d["exec_policy"] = policy
    d["fixed_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["fixed_net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    d["exec_hit"] = False
    if policy == "fixed":
        d["policy_exit_date"] = d["fixed_exit_date"]
        d["net_ret"] = d["fixed_net_ret"]
        return d.sort_values(["entry_date", "candidate_score", "amount20"], ascending=[True, False, False])

    hit_col = f"{policy}_hit"
    dt_col = f"{policy}_datetime"
    ret_col = f"{policy}_net_ret"
    if hit_col not in d.columns or dt_col not in d.columns or ret_col not in d.columns:
        raise ValueError(f"missing policy columns for {policy}")
    hit = d[hit_col].astype(str).str.lower().eq("true")
    d["exec_hit"] = hit
    d.loc[hit, "policy_exit_date"] = pd.to_datetime(d.loc[hit, dt_col], errors="coerce").dt.normalize()
    d.loc[~hit, "policy_exit_date"] = d.loc[~hit, "fixed_exit_date"]
    d.loc[hit, "net_ret"] = pd.to_numeric(d.loc[hit, ret_col], errors="coerce")
    d.loc[~hit, "net_ret"] = d.loc[~hit, "fixed_net_ret"]
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    return d.dropna(subset=["policy_exit_date", "net_ret"]).sort_values(
        ["entry_date", "candidate_score", "amount20"], ascending=[True, False, False]
    )


def _load_daily_prices(candidates: pd.DataFrame) -> dict[tuple[str, pd.Timestamp], float]:
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
    prices = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if prices.empty:
        return {}
    prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce").dt.normalize()
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    prices = prices.dropna(subset=["code", "trade_date", "close"])
    return {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in prices.itertuples(index=False)}


def _simulate_mtm(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = candidates.copy()
    candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce").dt.normalize()
    candidates["policy_exit_date"] = pd.to_datetime(candidates["policy_exit_date"], errors="coerce").dt.normalize()
    calendar = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    by_entry = {day: g.copy() for day, g in candidates.groupby("entry_date")}
    prices = _load_daily_prices(candidates)
    cash = INITIAL_CAPITAL
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

        still_open = []
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


def _metrics(combo: str, book: str, curve: pd.DataFrame, closed: pd.DataFrame) -> dict:
    return {
        "combo": combo,
        "book": book,
        "start": pd.Timestamp(curve["date"].min()).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(curve["date"].max()).strftime("%Y-%m-%d"),
        "total_ret": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": float(curve["drawdown"].min()),
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        "max_open_positions": int(curve["open_positions"].max()),
        "closed": int(len(closed)),
        "early_exits": int(closed["exec_hit"].astype(bool).sum()) if "exec_hit" in closed.columns and not closed.empty else 0,
        "early_exit_rate": float(closed["exec_hit"].astype(bool).mean()) if "exec_hit" in closed.columns and not closed.empty else 0.0,
        "win_rate": float((closed["net_ret"] > 0).mean()) if not closed.empty else 0.0,
        "mean_trade_ret": float(closed["net_ret"].mean()) if not closed.empty else 0.0,
        "median_trade_ret": float(closed["net_ret"].median()) if not closed.empty else 0.0,
        "worst_trade": float(closed["net_ret"].min()) if not closed.empty else 0.0,
        "bad10_rate": float((closed["net_ret"] <= -0.10).mean()) if not closed.empty else 0.0,
    }


def _annual(combo: str, book: str, curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    closed = closed.copy()
    if not closed.empty:
        closed["entry_date"] = pd.to_datetime(closed["entry_date"], errors="coerce")
    for year, part in curve.groupby(pd.to_datetime(curve["date"]).dt.year):
        part = part.sort_values("date")
        trades = closed[closed["entry_date"].dt.year.eq(year)] if not closed.empty else pd.DataFrame()
        rows.append(
            {
                "combo": combo,
                "book": book,
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": float(part["equity"].div(part["equity"].cummax()).sub(1.0).min()),
                "worst_open_mtm_ret": float(part["worst_open_mtm_ret"].min()),
                "closed": int(len(trades)),
                "early_exits": int(trades["exec_hit"].astype(bool).sum()) if "exec_hit" in trades.columns and not trades.empty else 0,
                "win_rate": float((trades["net_ret"] > 0).mean()) if not trades.empty else 0.0,
                "mean_trade_ret": float(trades["net_ret"].mean()) if not trades.empty else 0.0,
                "worst_trade": float(trades["net_ret"].min()) if not trades.empty else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _combine_50_50(curves: dict[str, pd.DataFrame], combo: str) -> pd.DataFrame:
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
    out["combo"] = combo
    out["book"] = "range_weak_50_50"
    return out


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame) -> None:
    pct_cols = {
        "total_ret",
        "max_drawdown",
        "worst_open_mtm_ret",
        "early_exit_rate",
        "win_rate",
        "mean_trade_ret",
        "median_trade_ret",
        "worst_trade",
        "bad10_rate",
        "return",
    }
    full = summary.sort_values(["book", "combo"]).copy()
    combo_annual = annual[annual["book"].eq("range_weak_50_50")].sort_values(["combo", "year"]).copy()
    lines = [
        "# G3 Range/Weak 30m 执行退出 MTM V1",
        "",
        "## 口径",
        "",
        "- 输入：第42步 30m 可见性逐笔结果，样本固定为第39步 30bps shadow 成交。",
        "- 基准：固定持有 5 个交易日。",
        "- 规则一：`range_stress_top1` 与 `weak_low_top1` 均使用 `30m close <= -5%` 全部退出。",
        "- 规则二：`range_stress_top1` 使用 `30m close <= -8%`，`weak_low_top1` 使用 `30m close <= -5%`。",
        "- 资金：每条袖珍账户 slot5、单笔 20%、同日最多开 1 笔；再做 range/weak 50/50 合成。",
        "- 该步骤仍是 shadow 研究，不进入正式 G3，也不写 G2 runtime。",
        "",
        "## Full 窗口",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 50/50 年度",
        "",
        _md_table(combo_annual, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 若 30m 退出降低最差交易和 bad10，但同步显著降低总收益或没有改善组合回撤，则只能作为影子风控继续观察。",
        "- 若 50/50 曲线在 2025/2026 拖累进一步扩大，说明 range/weak 的问题不只是退出，还包括候选源本身质量不足。",
        "- 下一步应补充跌停不可卖、次日开盘成交和 50/100bps 滑点压力，确认 30m 退出的执行收益是否仍存在。",
        "",
    ]
    (OUT_DIR / "range_weak_30m_exit_mtm_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = _load_source()
    summary_rows = []
    annual_parts = []
    for combo, book_policy in BOOK_POLICIES.items():
        curves: dict[str, pd.DataFrame] = {}
        closed_parts: list[pd.DataFrame] = []
        for book, policy in book_policy.items():
            candidates = _apply_policy(source, combo, book, policy)
            closed, curve = _simulate_mtm(candidates)
            stem = f"{combo}_{book}"
            candidates.to_csv(OUT_DIR / f"{stem}_candidates.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(OUT_DIR / f"{stem}_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(OUT_DIR / f"{stem}_mtm_curve.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(_metrics(combo, book, curve, closed))
            annual_parts.append(_annual(combo, book, curve, closed))
            curves[book] = curve
            closed_parts.append(closed.assign(source_book=book) if not closed.empty else closed)

        combo_curve = _combine_50_50(curves, combo)
        combo_closed = pd.concat(closed_parts, ignore_index=True) if closed_parts else pd.DataFrame()
        combo_curve.to_csv(OUT_DIR / f"{combo}_range_weak_50_50_mtm_curve.csv", index=False, encoding="utf-8-sig")
        combo_closed.to_csv(OUT_DIR / f"{combo}_range_weak_50_50_closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(_metrics(combo, "range_weak_50_50", combo_curve, combo_closed))
        annual_parts.append(_annual(combo, "range_weak_50_50", combo_curve, combo_closed))

    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_parts, ignore_index=True)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual)
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "summary": summary[summary["book"].eq("range_weak_50_50")].to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
