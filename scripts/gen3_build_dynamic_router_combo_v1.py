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


PANIC_PATH = ROOT / "reports" / "gen3_panic_v2_research" / "final_candidate_v1" / "m30_close5_full_nextopen_cost30_closed_trades.csv"
RANGE_PATH = ROOT / "reports" / "gen3_range_v3_mtm_pressure_v1" / "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv"
STRONG_PATH = ROOT / "reports" / "gen3_strong_v2_independent_source_v1" / "strong_v2_main_up_only_hold5_closed_trades.csv"
OUT_DIR = ROOT / "reports" / "gen3_dynamic_router_combo_v1"

INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SLOT_PCT = 0.20
DAILY_OPEN_LIMIT = 2
ROUTE_DAILY_LIMIT = {"down_panic": 2, "range_gap": 1, "strong_main": 1}
ROUTE_PRIORITY = {"down_panic": 3, "strong_main": 2, "range_gap": 1}
SOURCE_COST_BPS = 30.0
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


def standardize_panic() -> pd.DataFrame:
    d = pd.read_csv(PANIC_PATH)
    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "down_panic"
    out["route_source"] = "panic_v2_m30_close5_full_nextopen"
    out["route_priority"] = ROUTE_PRIORITY["down_panic"]
    out["score"] = pd.to_numeric(d.get("candidate_score", 0.0), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_price_adjusted", d.get("entry_price")), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("policy_net_ret", d.get("net_ret")), errors="coerce")
    return out


def standardize_range() -> pd.DataFrame:
    d = pd.read_csv(RANGE_PATH)
    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "range_gap"
    out["route_source"] = "range_v3_weak_low_not_chasing_h5"
    out["route_priority"] = ROUTE_PRIORITY["range_gap"]
    out["score"] = pd.to_numeric(d.get("range_v3_score", d.get("candidate_score", 0.0)), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_open"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("policy_net_ret"), errors="coerce")
    return out


def standardize_strong() -> pd.DataFrame:
    d = pd.read_csv(STRONG_PATH)
    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "strong_main"
    out["route_source"] = "strong_v2_main_up_only_h5"
    out["route_priority"] = ROUTE_PRIORITY["strong_main"]
    out["score"] = pd.to_numeric(d.get("g3_strong_score", d.get("score_volume5", d.get("v4_score", 0.0))), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_price"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("net_ret"), errors="coerce")
    return out


def load_candidates() -> pd.DataFrame:
    d = pd.concat([standardize_panic(), standardize_range(), standardize_strong()], ignore_index=True)
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"]).copy()
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


def load_daily_close(candidates: pd.DataFrame) -> dict[tuple[str, pd.Timestamp], float]:
    codes = sorted({str(c) for c in candidates["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
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


def simulate(candidates: pd.DataFrame, cost_bps: float = SOURCE_COST_BPS) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = candidates.copy()
    extra_cost = (cost_bps - SOURCE_COST_BPS) / 10000.0
    candidates["policy_net_ret"] = pd.to_numeric(candidates["policy_net_ret"], errors="coerce") - extra_cost
    cal = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    close_map = load_daily_close(candidates)
    by_day = {day: g.copy() for day, g in candidates.groupby("entry_date")}
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
                closed.append(out)
            else:
                still.append(pos)
        open_pos = still

        opened = 0
        skipped = 0
        route_opened = {k: 0 for k in ROUTE_DAILY_LIMIT}
        todays = by_day.get(day)
        if todays is not None:
            todays = todays.sort_values(["route_priority", "score"], ascending=[False, False])
            for row in todays.itertuples(index=False):
                route = str(row.route)
                if opened >= DAILY_OPEN_LIMIT or len(open_pos) >= SLOTS:
                    skipped += 1
                    continue
                if route_opened.get(route, 0) >= ROUTE_DAILY_LIMIT.get(route, 1):
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
                route_opened[route] = route_opened.get(route, 0) + 1
                opened += 1

        mtm_value = 0.0
        worst_open_mtm_ret = 0.0
        missing_close_positions = 0
        for pos in open_pos:
            close = close_map.get((str(pos["code"]), day))
            entry_price = float(pos["entry_price"])
            if close is None or entry_price <= 0:
                mtm_value += float(pos["stake"])
                missing_close_positions += 1
                continue
            mtm_ret = close / entry_price - 1.0 - mtm_cost
            worst_open_mtm_ret = min(worst_open_mtm_ret, float(mtm_ret))
            mtm_value += float(pos["stake"]) * (1.0 + float(mtm_ret))
        equity = cash + mtm_value
        route_exposure = {f"open_{route}": sum(1 for p in open_pos if p["route"] == route) for route in ROUTE_DAILY_LIMIT}
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
                **route_exposure,
            }
        )
    curve = pd.DataFrame(rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed)


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, cost_bps: float) -> dict:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "cost_bps": cost_bps,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": max_drawdown(curve["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        "missing_close_days": int((curve["missing_close_positions"] > 0).sum()),
    }


def summarize_windows(curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, (start, end) in WINDOWS.items():
        part = curve[curve["date"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        trades = closed[pd.to_datetime(closed["entry_date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        ret = float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0) if not part.empty else 0.0
        rows.append(
            {
                "window": name,
                "return": ret,
                "max_drawdown": max_drawdown(part["equity"]) if not part.empty else 0.0,
                "trade_count": int(len(trades)),
                "win_rate": float((trades["policy_net_ret"] > 0).mean()) if not trades.empty else 0.0,
            }
        )
    return pd.DataFrame(rows)


def summarize_routes(closed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for route, part in closed.groupby("route"):
        net = pd.to_numeric(part["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "route": route,
                "trade_count": int(len(part)),
                "win_rate": float((net > 0).mean()),
                "avg_trade_return": float(net.mean()),
                "worst_trade": float(net.min()),
                "sum_realized_pnl": float(part["realized_pnl"].sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("sum_realized_pnl", ascending=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    candidates.to_csv(OUT_DIR / "dynamic_router_candidates_standardized.csv", index=False, encoding="utf-8-sig")
    stress_rows = []
    stress_window_rows = []
    for cost_bps in [30.0, 50.0, 100.0]:
        stress_curve, stress_closed = simulate(candidates, cost_bps)
        stress_rows.append(summarize(stress_curve, stress_closed, cost_bps))
        w = summarize_windows(stress_curve, stress_closed)
        w.insert(0, "cost_bps", cost_bps)
        stress_window_rows.append(w)
        stress_curve.to_csv(OUT_DIR / f"dynamic_router_cost{int(cost_bps)}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        stress_closed.to_csv(OUT_DIR / f"dynamic_router_cost{int(cost_bps)}_closed_trades.csv", index=False, encoding="utf-8-sig")
    stress_df = pd.DataFrame(stress_rows)
    stress_window_df = pd.concat(stress_window_rows, ignore_index=True)
    stress_df.to_csv(OUT_DIR / "dynamic_router_stress_summary.csv", index=False, encoding="utf-8-sig")
    stress_window_df.to_csv(OUT_DIR / "dynamic_router_stress_windows.csv", index=False, encoding="utf-8-sig")
    curve, closed = simulate(candidates, SOURCE_COST_BPS)
    curve.to_csv(OUT_DIR / "dynamic_router_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(OUT_DIR / "dynamic_router_closed_trades.csv", index=False, encoding="utf-8-sig")
    window_df = summarize_windows(curve, closed)
    route_df = summarize_routes(closed)
    window_df.to_csv(OUT_DIR / "dynamic_router_window_summary.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(OUT_DIR / "dynamic_router_route_attribution.csv", index=False, encoding="utf-8-sig")
    summary = summarize(curve, closed, SOURCE_COST_BPS)
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "rules": {
                    "slots": SLOTS,
                    "slot_pct": SLOT_PCT,
                    "daily_open_limit": DAILY_OPEN_LIMIT,
                    "route_daily_limit": ROUTE_DAILY_LIMIT,
                    "route_priority": ROUTE_PRIORITY,
                    "source_cost_bps": SOURCE_COST_BPS,
                },
                "summary": summary,
                "stress_summary": str(OUT_DIR / "dynamic_router_stress_summary.csv"),
                "stress_windows": str(OUT_DIR / "dynamic_router_stress_windows.csv"),
                "next_step": "audit_dynamic_router_stress_and_reduce_drawdown",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    lines = [
        "# G3 三链动态路由组合 V1",
        "",
        "## 固定规则",
        "",
        "- 弱势：`down_panic` = panic_v2 30m 退出候选。",
        "- 震荡/弱反弹缺口：`range_gap` = range_v3 weak_low_not_chasing_h5。",
        "- 强势：`strong_main` = strong_v2 main_up_only_h5，吸收 G2 full 的 volume5/runup/sector 思路，但不直接复刻 G2 full。",
        "- slot5，每笔 20%，每日最多开 2 笔；同日冲突时按 down_panic > strong_main > range_gap 固定优先级。",
        "",
        "## 总结果",
        "",
        f"- 全周期收益：{pct(summary['total_return'])}，最大回撤：{pct(summary['max_drawdown'])}，交易数：{summary['trade_count']}。",
        f"- 胜率：{pct(summary['win_rate'])}，最差单笔：{pct(summary['worst_trade'])}，最差持仓浮亏：{pct(summary['worst_open_mtm_ret'])}。",
        "",
        "## 分段",
        "",
        md_table(window_df, {"return", "max_drawdown", "win_rate"}),
        "",
        "## 链路贡献",
        "",
        md_table(route_df, {"win_rate", "avg_trade_return", "worst_trade"}),
        "",
        "## 成本压力",
        "",
        md_table(stress_df, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"}),
        "",
        "## 判断",
        "",
        "- 这是 G3 第一次把强势、弱势、震荡/弱反弹缺口放在一个统一 slot 账户里看，不是上线版本。",
        "- 下一步需要做 50/100bps、跌停不可卖、尾盘次日开盘冲击，并针对回撤来源做 attribution。",
    ]
    (OUT_DIR / "dynamic_router_combo_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
