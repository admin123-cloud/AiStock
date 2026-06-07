from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df


WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}

BOOK_FILES = {
    "base": ["slot5_20pct", "slot10_10pct"],
    "pause_weak_no_capitulation": ["slot5_20pct", "slot10_10pct"],
}


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{v * 100:.2f}%"


def _money(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2f}"


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_executed(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.normalize()
    d["exit_date"] = pd.to_datetime(d["exit_date"]).dt.normalize()
    d["stake"] = pd.to_numeric(d["stake"], errors="coerce")
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    d["entry_price_adjusted"] = pd.to_numeric(d["entry_price_adjusted"], errors="coerce")
    d = d[d.get("status", "closed").eq("closed")].copy()
    return d.dropna(subset=["code", "entry_date", "exit_date", "stake", "net_ret", "entry_price_adjusted"])


def _trade_calendar(start_date: pd.Timestamp, end_date: pd.Timestamp) -> list[pd.Timestamp]:
    sql = f"""
    SELECT DISTINCT trade_date
    FROM kline_daily
    WHERE trade_date BETWEEN toDate({_sql_literal(start_date.strftime('%Y-%m-%d'))})
      AND toDate({_sql_literal(end_date.strftime('%Y-%m-%d'))})
    ORDER BY trade_date
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return []
    return pd.to_datetime(df["trade_date"]).dt.normalize().drop_duplicates().sort_values().tolist()


def _load_daily_close(trades: pd.DataFrame) -> dict[tuple[str, pd.Timestamp], float]:
    codes = sorted({str(c) for c in trades["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return {}
    start_date = trades["entry_date"].min().strftime("%Y-%m-%d")
    end_date = trades["exit_date"].max().strftime("%Y-%m-%d")
    code_list = ",".join(_sql_literal(code) for code in codes)
    sql = f"""
    SELECT code, trade_date, close
    FROM kline_daily
    WHERE code IN ({code_list})
      AND trade_date BETWEEN toDate({_sql_literal(start_date)}) AND toDate({_sql_literal(end_date)})
    ORDER BY code, trade_date
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return {}
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["code", "trade_date", "close"])
    return {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in df.itertuples(index=False)}


def _simulate_mtm(
    trades: pd.DataFrame,
    daily_close: dict[tuple[str, pd.Timestamp], float],
    initial_capital: float,
    mtm_cost_bps: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if trades.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    min_date = trades["entry_date"].min()
    max_date = trades["exit_date"].max()
    calendar = _trade_calendar(min_date, max_date)
    if not calendar:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    cash = float(initial_capital)
    open_positions: list[dict] = []
    rows: list[dict] = []
    trade_rows: list[dict] = []
    position_rows: list[dict] = []
    mtm_cost = mtm_cost_bps / 10000.0

    by_entry = {d: g.copy() for d, g in trades.groupby("entry_date")}

    for current_date in calendar:
        realized_pnl = 0.0
        closed_count = 0
        still_open: list[dict] = []
        for pos in open_positions:
            if pos["exit_date"] <= current_date:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                pnl = exit_value - float(pos["stake"])
                cash += exit_value
                realized_pnl += pnl
                closed_count += 1
                out = pos.copy()
                out["realized_pnl"] = pnl
                out["exit_value"] = exit_value
                trade_rows.append(out)
            else:
                still_open.append(pos)
        open_positions = still_open

        opened = 0
        todays = by_entry.get(current_date)
        if todays is not None:
            todays = todays.sort_values(["candidate_score", "chain_rank"], ascending=[False, True])
            for row in todays.itertuples(index=False):
                pos = row._asdict()
                stake = float(pos["stake"])
                cash -= stake
                open_positions.append(pos)
                opened += 1

        mtm_value = 0.0
        missing_close = 0
        worst_open_ret = 0.0
        for pos in open_positions:
            key = (str(pos["code"]), current_date)
            close = daily_close.get(key)
            if close is None or float(pos["entry_price_adjusted"]) <= 0:
                mtm_value += float(pos["stake"])
                missing_close += 1
                position_rows.append(
                    {
                        "date": current_date,
                        "code": pos.get("code"),
                        "name": pos.get("name"),
                        "entry_date": pos.get("entry_date"),
                        "exit_date": pos.get("exit_date"),
                        "stake": float(pos["stake"]),
                        "entry_price_adjusted": float(pos["entry_price_adjusted"]),
                        "daily_close": None,
                        "mtm_ret": 0.0,
                        "mtm_value": float(pos["stake"]),
                        "mtm_pnl": 0.0,
                        "g3_repair_env_label": pos.get("g3_repair_env_label"),
                        "market_style": pos.get("market_style"),
                    }
                )
                continue
            mtm_ret = close / float(pos["entry_price_adjusted"]) - 1.0 - mtm_cost
            worst_open_ret = min(worst_open_ret, mtm_ret)
            pos_value = float(pos["stake"]) * (1.0 + mtm_ret)
            mtm_value += pos_value
            position_rows.append(
                {
                    "date": current_date,
                    "code": pos.get("code"),
                    "name": pos.get("name"),
                    "entry_date": pos.get("entry_date"),
                    "exit_date": pos.get("exit_date"),
                    "stake": float(pos["stake"]),
                    "entry_price_adjusted": float(pos["entry_price_adjusted"]),
                    "daily_close": close,
                    "mtm_ret": mtm_ret,
                    "mtm_value": pos_value,
                    "mtm_pnl": pos_value - float(pos["stake"]),
                    "g3_repair_env_label": pos.get("g3_repair_env_label"),
                    "market_style": pos.get("market_style"),
                }
            )

        reserved_principal = sum(float(p["stake"]) for p in open_positions)
        equity = cash + mtm_value
        rows.append(
            {
                "date": current_date,
                "cash": cash,
                "reserved_principal": reserved_principal,
                "mtm_value": mtm_value,
                "equity": equity,
                "open_positions": len(open_positions),
                "opened": opened,
                "closed": closed_count,
                "realized_pnl": realized_pnl,
                "missing_close_positions": missing_close,
                "worst_open_mtm_ret": worst_open_ret,
            }
        )

    curve = pd.DataFrame(rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / initial_capital - 1.0
        curve["daily_ret"] = curve["equity"].pct_change().fillna(0.0)
    executed = pd.DataFrame(trade_rows)
    position_marks = pd.DataFrame(position_rows)
    return executed, curve, position_marks


def _metrics(executed: pd.DataFrame, curve: pd.DataFrame, window: str, policy: str, book: str) -> dict:
    if curve.empty:
        return {"window": window, "policy": policy, "book": book, "closed": 0}
    start, end = WINDOWS[window]
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    cw = curve[(curve["date"] >= start_ts) & (curve["date"] <= end_ts)].copy()
    tw = executed[(executed["entry_date"] >= start_ts) & (executed["entry_date"] <= end_ts)].copy() if not executed.empty else pd.DataFrame()
    if cw.empty:
        return {"window": window, "policy": policy, "book": book, "closed": 0}
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    local_equity = cw["equity"] / start_equity
    local_dd = local_equity / local_equity.cummax() - 1.0
    return {
        "window": window,
        "policy": policy,
        "book": book,
        "closed": int(len(tw)),
        "unique_codes": int(tw["code"].nunique()) if not tw.empty else 0,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_ret": end_equity / start_equity - 1.0,
        "max_drawdown": float(local_dd.min()),
        "max_open_positions": int(cw["open_positions"].max()),
        "worst_open_mtm_ret": float(cw["worst_open_mtm_ret"].min()),
        "missing_close_positions": int(cw["missing_close_positions"].sum()),
        "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
        "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
        "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
    }


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in ["total_ret", "max_drawdown", "worst_open_mtm_ret", "win_rate", "mean_trade_ret", "worst_trade"]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    for col in ["start_equity", "end_equity"]:
        if col in out.columns:
            out[col] = out[col].map(_money)
    return out


def _drawdown_attribution(policy: str, book: str, curve: pd.DataFrame, marks: pd.DataFrame) -> pd.DataFrame:
    if curve.empty or marks.empty:
        return pd.DataFrame()
    worst_date = pd.Timestamp(curve.sort_values("drawdown").iloc[0]["date"]).normalize()
    d = marks[pd.to_datetime(marks["date"]).dt.normalize().eq(worst_date)].copy()
    if d.empty:
        return pd.DataFrame()
    d["policy"] = policy
    d["book"] = book
    d["worst_drawdown_date"] = worst_date
    d["portfolio_drawdown"] = float(curve.loc[pd.to_datetime(curve["date"]).dt.normalize().eq(worst_date), "drawdown"].iloc[0])
    d["portfolio_equity"] = float(curve.loc[pd.to_datetime(curve["date"]).dt.normalize().eq(worst_date), "equity"].iloc[0])
    return d.sort_values("mtm_pnl")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mark-to-market capital curve for G3 panic module.")
    parser.add_argument("--input-dir", default="reports/gen3_panic_v2_research/capital_curve_v1")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/mtm_capital_curve_v1")
    parser.add_argument("--initial-capital", type=float, default=150000.0)
    parser.add_argument("--mtm-cost-bps", type=float, default=30.0, help="Conservative liquidation cost deducted from open MTM value.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_metrics: list[dict] = []
    all_trades: list[pd.DataFrame] = []
    for policy, books in BOOK_FILES.items():
        for book in books:
            source = input_dir / f"{policy}_{book}_executed_trades.csv"
            trades = _load_executed(source)
            daily_close = _load_daily_close(trades)
            executed, curve, marks = _simulate_mtm(trades, daily_close, args.initial_capital, args.mtm_cost_bps)
            executed.to_csv(out_dir / f"{policy}_{book}_mtm_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(out_dir / f"{policy}_{book}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            marks.to_csv(out_dir / f"{policy}_{book}_mtm_position_marks.csv", index=False, encoding="utf-8-sig")
            attr = _drawdown_attribution(policy, book, curve, marks)
            attr.to_csv(out_dir / f"{policy}_{book}_max_drawdown_attribution.csv", index=False, encoding="utf-8-sig")
            labeled = trades.copy()
            labeled["policy"] = policy
            labeled["book"] = book
            all_trades.append(labeled)
            for window in WINDOWS:
                all_metrics.append(_metrics(executed, curve, window, policy, book))

    raw = pd.DataFrame(all_metrics)
    raw.to_csv(out_dir / "mtm_capital_curve_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "mtm_capital_curve_summary_display.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    pause_slot5 = display[
        display["policy"].eq("pause_weak_no_capitulation")
        & display["book"].eq("slot5_20pct")
    ]
    attr_source = out_dir / "pause_weak_no_capitulation_slot5_20pct_max_drawdown_attribution.csv"
    attr_display = pd.DataFrame()
    if attr_source.exists():
        attr_display = pd.read_csv(attr_source)
        if not attr_display.empty:
            attr_display = attr_display.head(10).copy()
            for col in ["mtm_ret", "portfolio_drawdown"]:
                attr_display[col] = attr_display[col].map(_pct)
            for col in ["stake", "mtm_value", "mtm_pnl", "portfolio_equity"]:
                attr_display[col] = attr_display[col].map(_money)
    lines = [
        "# G3 Panic 逐日盯市资金曲线 V1",
        "",
        "## 口径",
        "",
        f"- 输入目录：`{input_dir}`",
        f"- 输出目录：`{out_dir}`",
        f"- 初始资金：`{args.initial_capital:.2f}`",
        f"- 持仓期逐日用 `kline_daily.close` 按 `entry_price_adjusted` 估值，并从未平仓市值中保守扣除 `{args.mtm_cost_bps:.1f}bps` 退出成本。",
        "- 交易列表仍来自上一版资金占用模拟，因此这一版只回答“持仓过程中的真实波动和回撤是否被低估”，不重新优化选股或开仓。",
        "- 退出日按既定 `net_ret` 结算；同日先结算退出，再记录既有开仓。",
        "",
        "## full 对照",
        "",
        full[
            [
                "policy",
                "book",
                "closed",
                "total_ret",
                "max_drawdown",
                "worst_open_mtm_ret",
                "win_rate",
                "mean_trade_ret",
                "worst_trade",
                "max_open_positions",
                "missing_close_positions",
            ]
        ].to_markdown(index=False),
        "",
        "## 主暂停口径分段",
        "",
        pause_slot5[
            [
                "window",
                "closed",
                "total_ret",
                "max_drawdown",
                "worst_open_mtm_ret",
                "win_rate",
                "mean_trade_ret",
                "worst_trade",
                "max_open_positions",
            ]
        ].to_markdown(index=False),
        "",
        "## 主暂停口径最大回撤日持仓拆解",
        "",
        attr_display[
            [
                "worst_drawdown_date",
                "portfolio_drawdown",
                "code",
                "name",
                "entry_date",
                "exit_date",
                "stake",
                "mtm_ret",
                "mtm_pnl",
                "g3_repair_env_label",
                "market_style",
            ]
        ].to_markdown(index=False)
        if not attr_display.empty
        else "无可拆解持仓。",
        "",
        "## 判断",
        "",
        "1. 逐日盯市后，若回撤仍明显低于事件曲线，说明仓位约束确实降低了组合层面的尾部风险；若回撤显著放大，则说明上一版退出日曲线低估了持仓过程风险。",
        "2. 这一版没有加入盘中失败退出、跌停不可卖、真实滑点和成交排队，仍然不能作为正式实盘结论。",
        "3. 下一步更关键的是把最大持仓期回撤对应日期和个股拆出来，判断风险来自系统性环境还是少数单票崩塌。",
    ]
    (out_dir / "mtm_capital_curve_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input_dir": str(input_dir),
        "output_dir": str(out_dir),
        "initial_capital": args.initial_capital,
        "mtm_cost_bps": args.mtm_cost_bps,
        "summary_rows": int(len(raw)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
