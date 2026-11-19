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
from scripts.gen3_build_dynamic_router_combo_v1 import (
    DAILY_OPEN_LIMIT,
    INITIAL_CAPITAL,
    ROUTE_DAILY_LIMIT,
    SLOTS,
    SLOT_PCT,
    max_drawdown,
    md_table,
    pct,
    sql_literal,
)
from scripts.gen3_build_dynamic_router_guarded_v1 import candidate_set
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "gen3_guarded_execution_stress_v1"
GUARD_NAME = "strong_breadth_score_volume5_guard"
STRESS_SPECS = [
    {"profile": "close_30bps", "cost_bps": 30.0, "exit_mode": "close", "limit_delay": False, "open_haircut": 0.0},
    {"profile": "close_50bps", "cost_bps": 50.0, "exit_mode": "close", "limit_delay": False, "open_haircut": 0.0},
    {"profile": "close_100bps", "cost_bps": 100.0, "exit_mode": "close", "limit_delay": False, "open_haircut": 0.0},
    {"profile": "nextopen_30bps", "cost_bps": 30.0, "exit_mode": "next_open", "limit_delay": False, "open_haircut": 0.0},
    {"profile": "limitdown_delay_30bps", "cost_bps": 30.0, "exit_mode": "close", "limit_delay": True, "open_haircut": 0.0},
    {"profile": "nextopen_limitdown_30bps", "cost_bps": 30.0, "exit_mode": "next_open", "limit_delay": True, "open_haircut": 0.0},
    {"profile": "nextopen_haircut2_30bps", "cost_bps": 30.0, "exit_mode": "next_open", "limit_delay": True, "open_haircut": 0.02},
]
WINDOWS = {
    "weak_gap_2022_2024": ("2022-01-01", "2024-12-31"),
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2020-01-01", "2026-05-29"),
}


def limit_pct(code: str, name: str | None = None) -> float:
    text = "" if name is None or pd.isna(name) else str(name).upper()
    if "ST" in text:
        return 0.05
    code = str(code)
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("8", "4", "920")):
        return 0.30
    return 0.10


def load_daily_ohlc(candidates: pd.DataFrame, extra_days: int = 20) -> pd.DataFrame:
    codes = sorted({str(c) for c in candidates["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    start = pd.Timestamp(candidates["entry_date"].min()).strftime("%Y-%m-%d")
    end = (pd.Timestamp(candidates["policy_exit_date"].max()) + pd.Timedelta(days=extra_days)).strftime("%Y-%m-%d")
    parts = []
    for i in range(0, len(codes), 500):
        code_list = ",".join(sql_literal(c) for c in codes[i : i + 500])
        sql = f"""
        SELECT code, trade_date, open, high, low, close
        FROM kline_daily
        WHERE code IN ({code_list})
          AND trade_date BETWEEN toDate({sql_literal(start)}) AND toDate({sql_literal(end)})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "trade_date"])
    d["pre_close"] = d.groupby("code")["close"].shift(1)
    return d


def prepare_daily_maps(daily: pd.DataFrame, candidates: pd.DataFrame) -> dict:
    name_map = {str(r.code): str(r.name) for r in candidates[["code", "name"]].drop_duplicates().itertuples(index=False)}
    daily = daily.copy()
    daily["limit_pct"] = [limit_pct(code, name_map.get(str(code))) for code in daily["code"]]
    daily["limit_down_price"] = daily["pre_close"] * (1.0 - daily["limit_pct"])
    daily["open_limitdown_proxy"] = daily["pre_close"].notna() & (daily["open"] <= daily["limit_down_price"] * 1.003)
    daily["close_limitdown_proxy"] = daily["pre_close"].notna() & (daily["close"] <= daily["limit_down_price"] * 1.003)
    daily["all_day_limitdown_proxy"] = daily["pre_close"].notna() & (daily["high"] <= daily["limit_down_price"] * 1.003)
    daily["open_tradable_proxy"] = ~daily["open_limitdown_proxy"]
    return {(str(r.code), pd.Timestamp(r.trade_date).normalize()): r._asdict() for r in daily.itertuples(index=False)}


def next_calendar_day(calendar: list[pd.Timestamp], day: pd.Timestamp) -> pd.Timestamp | None:
    day = pd.Timestamp(day).normalize()
    for item in calendar:
        if item > day:
            return item
    return None


def first_tradable_open(
    code: str,
    start_day: pd.Timestamp,
    calendar: list[pd.Timestamp],
    daily_map: dict,
) -> tuple[pd.Timestamp | None, float | None, int, bool]:
    start_day = pd.Timestamp(start_day).normalize()
    delay = 0
    saw_limit = False
    for day in calendar:
        if day < start_day:
            continue
        row = daily_map.get((str(code), day))
        if row is None:
            continue
        if bool(row.get("open_tradable_proxy", True)):
            return day, float(row["open"]), delay, saw_limit
        saw_limit = True
        delay += 1
    return None, None, delay, saw_limit


def apply_stress(candidates: pd.DataFrame, spec: dict, calendar: list[pd.Timestamp], daily_map: dict) -> pd.DataFrame:
    d = candidates.copy()
    d["stress_profile"] = spec["profile"]
    d["stress_exit_date"] = pd.NaT
    d["stress_exit_price"] = pd.NA
    d["stress_net_ret"] = pd.NA
    d["stress_exit_source"] = ""
    d["execution_delay_days"] = 0
    d["limitdown_delayed"] = False
    d["missing_exit_price"] = False
    cost = float(spec["cost_bps"]) / 10000.0
    open_haircut = float(spec.get("open_haircut", 0.0))

    for idx, row in d.iterrows():
        code = str(row["code"])
        planned_day = pd.Timestamp(row["policy_exit_date"]).normalize()
        planned_daily = daily_map.get((code, planned_day))
        exit_day = planned_day
        exit_price = None
        source = "planned_close"
        delayed = False
        delay = 0

        if spec["exit_mode"] == "close":
            if bool(spec["limit_delay"]) and planned_daily and bool(planned_daily.get("all_day_limitdown_proxy", False)):
                next_day = next_calendar_day(calendar, planned_day)
                if next_day is not None:
                    exit_day, exit_price, delay, saw_limit = first_tradable_open(code, next_day, calendar, daily_map)
                    source = "limitdown_delayed_open"
                    delayed = True or saw_limit
            elif planned_daily is not None:
                exit_price = float(planned_daily["close"])
        elif spec["exit_mode"] == "next_open":
            next_day = next_calendar_day(calendar, planned_day)
            if next_day is not None:
                if bool(spec["limit_delay"]):
                    exit_day, exit_price, delay, saw_limit = first_tradable_open(code, next_day, calendar, daily_map)
                    delayed = saw_limit
                    source = "next_tradable_open"
                else:
                    exit_day = next_day
                    next_daily = daily_map.get((code, next_day))
                    if next_daily is not None:
                        exit_price = float(next_daily["open"])
                    source = "next_open"
        else:
            raise ValueError(spec["exit_mode"])

        if exit_day is None or exit_price is None or pd.isna(exit_price):
            d.at[idx, "missing_exit_price"] = True
            continue
        if spec["exit_mode"] == "next_open" or source.endswith("open"):
            exit_price = float(exit_price) * (1.0 - open_haircut)
        d.at[idx, "stress_exit_date"] = pd.Timestamp(exit_day).normalize()
        d.at[idx, "stress_exit_price"] = float(exit_price)
        d.at[idx, "stress_net_ret"] = float(exit_price) / float(row["entry_price"]) - 1.0 - cost
        d.at[idx, "stress_exit_source"] = source
        d.at[idx, "execution_delay_days"] = int(delay)
        d.at[idx, "limitdown_delayed"] = bool(delayed)

    d["policy_exit_date"] = pd.to_datetime(d["stress_exit_date"], errors="coerce").dt.normalize()
    d["policy_net_ret"] = pd.to_numeric(d["stress_net_ret"], errors="coerce")
    return d.dropna(subset=["policy_exit_date", "policy_net_ret"]).copy()


def simulate(candidates: pd.DataFrame, calendar: list[pd.Timestamp], close_map: dict[tuple[str, pd.Timestamp], float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_day = {day: g.copy() for day, g in candidates.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict] = []
    closed: list[dict] = []
    rows: list[dict] = []
    for day in calendar:
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
            mtm_ret = close / entry_price - 1.0 - 0.003
            worst_open_mtm_ret = min(worst_open_mtm_ret, mtm_ret)
            mtm_value += float(pos["stake"]) * (1.0 + mtm_ret)
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


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, profile: str) -> dict:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "profile": profile,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0,
        "max_drawdown": max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()) if not curve.empty else 0.0,
        "limitdown_delay_count": int(closed.get("limitdown_delayed", pd.Series(dtype=bool)).astype(bool).sum()) if not closed.empty else 0,
        "avg_execution_delay": float(pd.to_numeric(closed.get("execution_delay_days", pd.Series(dtype=float)), errors="coerce").mean()) if not closed.empty else 0.0,
    }


def summarize_windows(curve: pd.DataFrame, closed: pd.DataFrame, profile: str) -> list[dict]:
    rows = []
    for name, (start, end) in WINDOWS.items():
        part = curve[curve["date"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        trades = closed[pd.to_datetime(closed["entry_date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        rows.append(
            {
                "profile": profile,
                "window": name,
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0) if not part.empty else 0.0,
                "max_drawdown": max_drawdown(part["equity"]) if not part.empty else 0.0,
                "trade_count": int(len(trades)),
                "win_rate": float((trades["policy_net_ret"] > 0).mean()) if not trades.empty else 0.0,
            }
        )
    return rows


def route_attribution(closed: pd.DataFrame, profile: str) -> pd.DataFrame:
    rows = []
    for route, part in closed.groupby("route"):
        net = pd.to_numeric(part["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "profile": profile,
                "route": route,
                "trade_count": int(len(part)),
                "win_rate": float((net > 0).mean()),
                "avg_trade_return": float(net.mean()),
                "worst_trade": float(net.min()),
                "sum_realized_pnl": float(part["realized_pnl"].sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    guard = {
        "name": GUARD_NAME,
        "desc": "strong_main 要求广度、强势分数，并保留 G2 volume5 高分确认。",
        "market_breadth_min": 0.56,
        "g3_strong_score_min": 0.626,
        "score_volume5_min": 0.70,
    }
    base_candidates = candidate_set(guard)
    base_candidates["entry_date"] = pd.to_datetime(base_candidates["entry_date"], errors="coerce").dt.normalize()
    base_candidates["policy_exit_date"] = pd.to_datetime(base_candidates["policy_exit_date"], errors="coerce").dt.normalize()
    daily = load_daily_ohlc(base_candidates)
    daily_map = prepare_daily_maps(daily, base_candidates)
    calendar = _trade_calendar(base_candidates["entry_date"].min(), base_candidates["policy_exit_date"].max() + pd.Timedelta(days=20))
    close_map = {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in daily.itertuples(index=False)}
    summary_rows = []
    window_rows = []
    route_parts = []
    for spec in STRESS_SPECS:
        stressed = apply_stress(base_candidates, spec, calendar, daily_map)
        curve, closed = simulate(stressed, calendar, close_map)
        profile = spec["profile"]
        stressed.to_csv(OUT_DIR / f"{profile}_stressed_candidates.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{profile}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{profile}_closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(summarize(curve, closed, profile))
        window_rows.extend(summarize_windows(curve, closed, profile))
        route_parts.append(route_attribution(closed, profile))
    summary_df = pd.DataFrame(summary_rows)
    window_df = pd.DataFrame(window_rows)
    route_df = pd.concat(route_parts, ignore_index=True)
    summary_df.to_csv(OUT_DIR / "execution_stress_summary.csv", index=False, encoding="utf-8-sig")
    window_df.to_csv(OUT_DIR / "execution_stress_windows.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(OUT_DIR / "execution_stress_route_attribution.csv", index=False, encoding="utf-8-sig")
    base = summary_df[summary_df["profile"].eq("close_30bps")].iloc[0].to_dict()
    severe = summary_df[summary_df["profile"].eq("nextopen_haircut2_30bps")].iloc[0].to_dict()
    lines = [
        "# G3 guarded 主候选执行偏差压力审计 V1",
        "",
        "## 口径",
        "",
        f"- 主候选：`{GUARD_NAME}`。",
        "- 用 ClickHouse 日线重新给退出定价：计划日收盘、次日开盘、跌停不可卖延迟、次日开盘再额外 2% 冲击。",
        "- 跌停不可卖为日线代理：普通 10%，创业板/科创板 20%，北交所 30%，ST 5%；若全日贴近跌停则延迟到下一次可开盘成交。",
        "",
        "## 总结",
        "",
        f"- 计划日收盘 30bps：收益 {pct(base['total_return'])}，回撤 {pct(base['max_drawdown'])}。",
        f"- 严苛 next-open + 跌停延迟 + 2% 开盘冲击：收益 {pct(severe['total_return'])}，回撤 {pct(severe['max_drawdown'])}。",
        "",
        "## 压力总表",
        "",
        md_table(summary_df, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"}),
        "",
        "## 分段窗口",
        "",
        md_table(window_df, {"return", "max_drawdown", "win_rate"}),
        "",
        "## 链路贡献",
        "",
        md_table(route_df, {"win_rate", "avg_trade_return", "worst_trade"}),
        "",
        "## 下一步",
        "",
        "1. 若严苛压力仍保持 2022-2024 正收益，可进入最终候选组合封装。",
        "2. 若压力下 2022-2024 或 2026 失效，需要继续降低 range_gap 或 strong_main 的执行敏感性。",
    ]
    (OUT_DIR / "guarded_execution_stress_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "guard": GUARD_NAME,
                "summary": str(OUT_DIR / "execution_stress_summary.csv"),
                "windows": str(OUT_DIR / "execution_stress_windows.csv"),
                "base": base,
                "severe": severe,
                "next_step": "package_final_guarded_candidate_if_stable",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
