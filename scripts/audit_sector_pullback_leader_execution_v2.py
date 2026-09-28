"""审计龙头恐慌回撤策略的自然风控与 30 分钟确认，不改变原始选股排名。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_sector_first_pullback_panic_v1 import _pct  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE = report_path("sector_pullback_leader_v1")
OUT = report_path("sector_pullback_leader_execution_v2")
COST = 0.002


def _selection_cte(parts: pd.DataFrame) -> str:
    rows = []
    for r in parts.itertuples(index=False):
        rows.append(
            f"SELECT {int(r.event_id)} AS event_id, '{str(r.stock_code)}' AS code, "
            f"toDate('{pd.Timestamp(r.entry_date):%Y-%m-%d}') AS entry_date, "
            f"toDate('{pd.Timestamp(r.exit_1d_date):%Y-%m-%d}') AS exit_1d_date, "
            f"toDate('{pd.Timestamp(r.exit_2d_date):%Y-%m-%d}') AS exit_2d_date, "
            f"toDate('{pd.Timestamp(r.exit_3d_date):%Y-%m-%d}') AS exit_3d_date"
        )
    return " UNION ALL ".join(rows)


def _load_components() -> tuple[pd.DataFrame, pd.DataFrame]:
    events = pd.read_csv(SOURCE / "base_events.csv", parse_dates=["signal_date", "entry_date", "exit_1d_date", "exit_2d_date", "exit_3d_date"])
    trades = pd.read_csv(SOURCE / "leader_trades.csv", parse_dates=["signal_date"])
    top3 = trades[trades["leader_mode"] == "top3"].copy()
    rows = []
    for r in top3.itertuples(index=False):
        for code in str(r.leader_codes).split("|"):
            rows.append({"event_id": int(r.event_id), "stock_code": code})
    components = pd.DataFrame(rows).merge(events, on="event_id", how="left")
    components = components.merge(top3[["event_id", "leader_runup_15d", "avg_entry_gap"]], on="event_id", how="left")
    return top3, components


def _load_prices(parts: pd.DataFrame) -> pd.DataFrame:
    cte = _selection_cte(parts)
    minute = clickhouse_query_df(
        f"""
        WITH selection AS ({cte})
        SELECT s.event_id, s.code, m.datetime, m.open, m.close
        FROM selection s INNER JOIN kline_minute_30 m ON m.code=s.code
        WHERE toDate(m.datetime) BETWEEN s.entry_date AND s.exit_3d_date
          AND ((toHour(m.datetime)=10 AND toMinute(m.datetime)=0)
            OR (toHour(m.datetime)=15 AND toMinute(m.datetime)=0))
        """
    )
    for col in ["open", "close"]:
        minute[col] = pd.to_numeric(minute[col], errors="coerce")
    minute["datetime"] = pd.to_datetime(minute["datetime"], errors="coerce")
    return minute


def _load_daily_paths(parts: pd.DataFrame) -> pd.DataFrame:
    cte = _selection_cte(parts)
    out = clickhouse_query_df(
        f"""
        WITH selection AS ({cte})
        SELECT s.event_id, s.code, k.trade_date, k.open, k.low, k.close
        FROM selection s INNER JOIN kline_daily k ON k.code=s.code
        WHERE k.trade_date BETWEEN s.entry_date AND s.exit_3d_date
        """
    )
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    for col in ["open", "low", "close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["event_id", "code", "trade_date", "open", "low", "close"])


def _stop_trades(components: pd.DataFrame, daily: pd.DataFrame, stop: float) -> pd.DataFrame:
    """日线低点触发的保守止损代理：若跳空越过止损，以开盘价成交。"""
    rows: list[dict[str, Any]] = []
    for event_id, members in components.groupby("event_id", sort=False):
        base = members.iloc[0]
        returns: dict[int, list[float]] = {1: [], 2: [], 3: []}
        valid = True
        for stock in members.itertuples(index=False):
            path = daily[(daily["event_id"] == event_id) & (daily["code"] == stock.stock_code)].sort_values("trade_date")
            entry = path[path["trade_date"] == pd.Timestamp(base.entry_date)]
            if len(entry) != 1:
                valid = False; break
            entry_open = float(entry.iloc[0].open)
            if entry_open <= 0:
                valid = False; break
            for hold in (1, 2, 3):
                exit_date = pd.Timestamp(base[f"exit_{hold}d_date"])
                segment = path[(path["trade_date"] >= pd.Timestamp(base.entry_date)) & (path["trade_date"] <= exit_date)]
                if segment.empty or segment.iloc[-1].trade_date != exit_date:
                    valid = False; break
                ret = None
                for bar in segment.itertuples(index=False):
                    open_ret = float(bar.open) / entry_open - 1.0
                    low_ret = float(bar.low) / entry_open - 1.0
                    if open_ret <= -stop:
                        ret = open_ret; break
                    if low_ret <= -stop:
                        ret = -stop; break
                if ret is None:
                    ret = float(segment.iloc[-1].close) / entry_open - 1.0
                returns[hold].append(ret - COST)
            if not valid:
                break
        if valid and all(len(returns[h]) == 3 for h in returns):
            row: dict[str, Any] = {"event_id": int(event_id), "variant": f"daily_stop_{stop:.0%}", "signal_date": base.signal_date,
                                    "down_ratio": base.down_ratio, "leader_runup_15d": base.leader_runup_15d, "avg_entry_gap": base.avg_entry_gap}
            for hold in (1, 2, 3):
                row[f"net_ret_{hold}d"] = sum(returns[hold]) / 3.0
            rows.append(row)
    return pd.DataFrame(rows)


def _confirmed_trades(components: pd.DataFrame, minute: pd.DataFrame) -> pd.DataFrame:
    entry_bars = minute[(minute["datetime"].dt.hour == 10) & (minute["datetime"].dt.minute == 0)].copy()
    m = entry_bars.rename(columns={"code": "stock_code", "open": "m30_open", "close": "m30_close"})
    x = components.merge(m[["event_id", "stock_code", "m30_open", "m30_close"]], on=["event_id", "stock_code"], how="left")
    x["confirmed"] = x["m30_close"].gt(x["m30_open"])
    exit_bars = minute[(minute["datetime"].dt.hour == 15) & (minute["datetime"].dt.minute == 0)].copy()
    exit_bars["trade_date"] = exit_bars["datetime"].dt.tz_localize(None).dt.normalize()
    minute_wide = exit_bars.pivot_table(index=["event_id", "code"], columns="trade_date", values="close", aggfunc="last")
    rows: list[dict[str, Any]] = []
    for event_id, g in x.groupby("event_id", sort=False):
        base = g.iloc[0]
        confirmed = g[g["confirmed"]].copy()
        for name, selected in [("m30_green_all3", confirmed if len(confirmed) == 3 else confirmed.iloc[0:0]), ("m30_green_atleast2", confirmed if len(confirmed) >= 2 else confirmed.iloc[0:0])]:
            if selected.empty:
                continue
            row: dict[str, Any] = {"event_id": int(event_id), "variant": name, "selected_count": len(selected),
                                    "signal_date": base.signal_date, "down_ratio": base.down_ratio,
                                    "leader_runup_15d": base.leader_runup_15d, "avg_entry_gap": base.avg_entry_gap}
            ok = True
            for hold in (1, 2, 3):
                exit_date = pd.Timestamp(base[f"exit_{hold}d_date"])
                rets = []
                for s in selected.itertuples(index=False):
                    try:
                        exit_price = float(minute_wide.loc[(int(event_id), s.stock_code), exit_date])
                    except (KeyError, TypeError, ValueError):
                        ok = False; break
                    if not pd.notna(exit_price) or s.m30_close <= 0:
                        ok = False; break
                    rets.append(exit_price / float(s.m30_close) - 1.0)
                if not ok:
                    break
                row[f"net_ret_{hold}d"] = sum(rets) / len(rets) - COST
            if ok:
                rows.append(row)
    return pd.DataFrame(rows)


def _summarize(label: str, data: pd.DataFrame, filter_desc: str) -> list[dict[str, Any]]:
    rows = []
    for hold in (1, 2, 3):
        x = pd.to_numeric(data[f"net_ret_{hold}d"], errors="coerce").dropna()
        rows.append({"variant": label, "filter": filter_desc, "hold_days": hold, "event_count": len(x),
                     "avg_net_ret": x.mean(), "win_rate": (x > 0).mean(), "p25": x.quantile(.25), "worst": x.min()})
    return rows


def main() -> int:
    top3, components = _load_components()
    minute = _load_prices(components)
    daily_paths = _load_daily_paths(components)
    confirmed = _confirmed_trades(components, minute)
    stopped = pd.concat([_stop_trades(components, daily_paths, stop) for stop in (.06, .08, .10)], ignore_index=True)
    all_rows: list[dict[str, Any]] = []
    all_rows += _summarize("baseline_top3_open", top3, "原始开盘买入")
    for cap in (.40, .60, .80):
        all_rows += _summarize(f"runup_cap_{cap:.0%}", top3[top3["leader_runup_15d"] <= cap], f"Top3平均15日涨幅≤{cap:.0%}")
    for gap in (-.03, -.02, -.01):
        all_rows += _summarize(f"open_gap_ge_{gap:.0%}", top3[top3["avg_entry_gap"] >= gap], f"次日平均开盘缺口≥{gap:.0%}")
    for name, part in confirmed.groupby("variant"):
        all_rows += _summarize(name, part, "10:00首根30m阳线确认后，以30m收盘买入")
    for name, part in stopped.groupby("variant"):
        all_rows += _summarize(name, part, "次日开盘买入；日线触及止损即退出，跳空越过按开盘价")
    summary = pd.DataFrame(all_rows)
    OUT.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUT / "summary.csv", index=False, encoding="utf-8-sig")
    confirmed.to_csv(OUT / "confirmed_trades.csv", index=False, encoding="utf-8-sig")
    stopped.to_csv(OUT / "stopped_trades.csv", index=False, encoding="utf-8-sig")
    lines = ["# 龙头恐慌回撤：执行确认与尾部审计", "", "所有样本均为下跌家数≥60%的 Top3 龙头等权事件；收益已扣双边 20bp。", "", "| 变体 | 过滤/入场 | 持有日 | 事件数 | 平均净收益 | 胜率 | P25 | 最差 |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in summary.itertuples(index=False):
        lines.append(f"| {r.variant} | {r.filter} | {r.hold_days} | {r.event_count} | {_pct(r.avg_net_ret)} | {_pct(r.win_rate)} | {_pct(r.p25)} | {_pct(r.worst)} |")
    lines.extend(["", "## 审计原则", "", "- 不按历史收益挑选一个唯一参数；只关注多个相邻阈值是否同时改善。", "- 30 分钟确认使用当日 10:00 已完成 K 线，买价为该 K 线收盘，未使用未来信息。", "- 此处仍是事件研究，尚未处理同日事件的容量和持仓上限。", ""])
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps({"summary": summary.to_dict("records"), "minute_rows": len(minute), "confirmed_rows": len(confirmed)}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output_dir": str(OUT), "minute_rows": len(minute), "confirmed_rows": len(confirmed)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
