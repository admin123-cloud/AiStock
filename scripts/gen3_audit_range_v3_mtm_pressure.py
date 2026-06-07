from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar
from utils.market_warehouse import clickhouse_query_df


SOURCE_DIR = ROOT / "reports" / "gen3_range_v3_gap_candidate_source_v1"
OUT_DIR = ROOT / "reports" / "gen3_range_v3_mtm_pressure_v1"
INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 1
VARIANTS = [
    "range_v3_weak_low_not_chasing_h5",
    "range_v3_weak_low_not_chasing_h10",
    "range_v3_hybrid_range_weak_h5",
]
COST_BPS_LIST = [30.0, 50.0, 100.0]
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
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def load_variant(variant: str) -> pd.DataFrame:
    path = SOURCE_DIR / f"{variant}_closed_trades.csv"
    d = pd.read_csv(path)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    for col in [
        "entry_open",
        "fwd_ret_open_to_close_3d",
        "fwd_ret_open_to_close_5d",
        "fwd_ret_open_to_close_10d",
        "hold_days",
        "range_v3_score",
        "rank_in_day",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_open", "code"]).sort_values(
        ["entry_date", "range_v3_score"], ascending=[True, False]
    )


def load_daily_close(candidates: pd.DataFrame) -> dict[tuple[str, pd.Timestamp], float]:
    codes = sorted({str(c) for c in candidates["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return {}
    start = candidates["entry_date"].min().strftime("%Y-%m-%d")
    end = candidates["policy_exit_date"].max().strftime("%Y-%m-%d")
    code_list = ",".join(sql_literal(code) for code in codes)
    sql = f"""
    SELECT code, trade_date, close
    FROM kline_daily
    WHERE code IN ({code_list})
      AND trade_date BETWEEN toDate({sql_literal(start)}) AND toDate({sql_literal(end)})
    ORDER BY code, trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return {}
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "close"])
    return {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in d.itertuples(index=False)}


def simulate_mtm(candidates: pd.DataFrame, cost_bps: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.copy()
    hold_days = int(d["hold_days"].dropna().iloc[0])
    ret_col = f"fwd_ret_open_to_close_{hold_days}d"
    d["policy_net_ret"] = pd.to_numeric(d[ret_col], errors="coerce") - cost_bps / 10000.0
    d = d.dropna(subset=["policy_net_ret", "policy_exit_date"]).copy()
    cal = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max())
    close_map = load_daily_close(d)
    by_day = {day: g.copy() for day, g in d.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict] = []
    closed: list[dict] = []
    rows: list[dict] = []
    mtm_cost = cost_bps / 10000.0

    for day in cal:
        realized = 0.0
        still = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                pnl = exit_value - float(pos["stake"])
                realized += pnl
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                out["status"] = "closed"
                closed.append(out)
            else:
                still.append(pos)
        open_pos = still

        opened = 0
        skipped = 0
        todays = by_day.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= DAILY_OPEN_LIMIT or len(open_pos) >= SLOTS:
                    skipped += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * SLOT_PCT
                if stake <= 0 or cash < stake:
                    skipped += 1
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
            entry_open = float(pos["entry_open"])
            close = close_map.get((str(pos["code"]), day))
            if close is None or entry_open <= 0:
                mtm_value += float(pos["stake"])
                missing_close_positions += 1
                continue
            mtm_ret = close / entry_open - 1.0 - mtm_cost
            worst_open_mtm_ret = min(worst_open_mtm_ret, float(mtm_ret))
            mtm_value += float(pos["stake"]) * (1.0 + float(mtm_ret))
        equity = cash + mtm_value
        rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["stake"]) for p in open_pos),
                "mtm_value": mtm_value,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped": skipped,
                "realized_pnl": realized,
                "worst_open_mtm_ret": worst_open_mtm_ret,
                "missing_close_positions": missing_close_positions,
            }
        )
    curve = pd.DataFrame(rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed)


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, cost_bps: float) -> dict:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "cost_bps": cost_bps,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0,
        "max_drawdown": max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()) if not curve.empty else 0.0,
        "missing_close_days": int((curve["missing_close_positions"] > 0).sum()) if not curve.empty else 0,
    }


def summarize_windows(curve: pd.DataFrame, variant: str, cost_bps: float) -> list[dict]:
    rows = []
    for name, (start, end) in WINDOWS.items():
        part = curve[curve["date"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        if part.empty:
            ret = 0.0
            dd = 0.0
        else:
            ret = float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0)
            dd = max_drawdown(part["equity"])
        rows.append(
            {
                "variant": variant,
                "cost_bps": cost_bps,
                "window": name,
                "return": ret,
                "max_drawdown": dd,
                "days": int(len(part)),
            }
        )
    return rows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    window_rows = []
    for variant in VARIANTS:
        candidates = load_variant(variant)
        for cost_bps in COST_BPS_LIST:
            curve, closed = simulate_mtm(candidates, cost_bps)
            tag = f"{variant}_cost{int(cost_bps)}"
            curve.to_csv(OUT_DIR / f"{tag}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(OUT_DIR / f"{tag}_closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, variant, cost_bps))
            window_rows.extend(summarize_windows(curve, variant, cost_bps))
    summary_df = pd.DataFrame(summary_rows).sort_values(["cost_bps", "total_return"], ascending=[True, False])
    window_df = pd.DataFrame(window_rows)
    summary_df.to_csv(OUT_DIR / "range_v3_mtm_pressure_summary.csv", index=False, encoding="utf-8-sig")
    window_df.to_csv(OUT_DIR / "range_v3_mtm_pressure_windows.csv", index=False, encoding="utf-8-sig")
    main_candidate = summary_df[
        summary_df["variant"].eq("range_v3_weak_low_not_chasing_h5") & summary_df["cost_bps"].eq(30.0)
    ].iloc[0].to_dict()
    stress100 = summary_df[
        summary_df["variant"].eq("range_v3_weak_low_not_chasing_h5") & summary_df["cost_bps"].eq(100.0)
    ].iloc[0].to_dict()
    lines = [
        "# G3 range_v3 逐日 MTM 与成本压力审计",
        "",
        "## 结论",
        "",
        f"- 主候选 `range_v3_weak_low_not_chasing_h5` 在 30bps 逐日 MTM 下：收益 {pct(main_candidate['total_return'])}，最大回撤 {pct(main_candidate['max_drawdown'])}，最差持仓浮亏 {pct(main_candidate['worst_open_mtm_ret'])}。",
        f"- 同一主候选在 100bps 压力下：收益 {pct(stress100['total_return'])}，最大回撤 {pct(stress100['max_drawdown'])}。",
        "- 这是比上一版更真实的持仓期曲线，但仍未处理跌停不可卖和尾盘触发次日低开冲击。",
        "",
        "## 总表",
        "",
        md_table(summary_df, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"}),
        "",
        "## 分段窗口",
        "",
        md_table(window_df, {"return", "max_drawdown"}),
        "",
        "## 下一步",
        "",
        "1. 若接受 `weak_low_not_chasing_h5`，下一步把它与 down_panic_v3、strong_v2 做动态路由组合。",
        "2. 组合前还要补跌停不可卖/延迟卖出压力，避免把持仓期的退出价格看得太理想。",
    ]
    (OUT_DIR / "range_v3_mtm_pressure_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "main_candidate": main_candidate,
                "main_candidate_100bps": stress100,
                "summary": str(OUT_DIR / "range_v3_mtm_pressure_summary.csv"),
                "windows": str(OUT_DIR / "range_v3_mtm_pressure_windows.csv"),
                "next_step": "dynamic_router_combo_down_range_strong",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
