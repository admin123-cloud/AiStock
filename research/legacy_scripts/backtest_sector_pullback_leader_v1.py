"""在“强势行业首次回撤 + 市场恐慌”事件中，回测板块内龙头。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import backtest_sector_first_pullback_panic_v1 as base  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


def _event_values(events: pd.DataFrame) -> str:
    items = []
    for row in events.itertuples(index=False):
        code = str(row.code).replace("'", "\\'")
        date = pd.Timestamp(row.signal_date).strftime("%Y-%m-%d")
        items.append(f"SELECT {int(row.event_id)} AS event_id, '{code}' AS sector_code, toDate('{date}') AS signal_date")
    return " UNION ALL ".join(items)


def _load_event_stock_windows(events: pd.DataFrame) -> pd.DataFrame:
    """一次查询提取事件板块成员的信号日前 45 个自然日到后 7 日行情。"""
    if events.empty:
        return pd.DataFrame()
    query = f"""
    WITH event_list AS ({_event_values(events)})
    SELECT e.event_id, ss.stock_code AS code, k.trade_date, k.open, k.high, k.low, k.close
    FROM event_list AS e
    INNER JOIN sector_stocks AS ss ON ss.sector_code = e.sector_code
    INNER JOIN kline_daily AS k ON k.code = ss.stock_code
    WHERE k.trade_date BETWEEN e.signal_date - INTERVAL 45 DAY AND e.signal_date + INTERVAL 7 DAY
      AND k.open > 0 AND k.high > 0 AND k.low > 0 AND k.close > 0
    ORDER BY e.event_id, code, k.trade_date
    """
    out = clickhouse_query_df(query)
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    for col in ["open", "high", "low", "close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["event_id", "code", "trade_date", "open", "close"])


def _limit_ratio(code: str) -> float:
    code = str(code)
    if code.startswith(("300", "688")):
        return 0.195
    if code.endswith(".BJ") or code.startswith(("43", "83", "87", "92")):
        return 0.295
    return 0.095


def _build_leader_trades(events: pd.DataFrame, prices: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    event_map = events.set_index("event_id")
    for event_id, panel in prices.groupby("event_id", sort=False):
        event = event_map.loc[event_id]
        signal_date = pd.Timestamp(event.signal_date)
        entry_date = pd.Timestamp(event.entry_date)
        candidates: list[dict[str, Any]] = []
        for code, stock in panel.groupby("code", sort=False):
            stock = stock.sort_values("trade_date").reset_index(drop=True)
            signal_ix = stock.index[stock["trade_date"] == signal_date]
            entry_ix = stock.index[stock["trade_date"] == entry_date]
            if len(signal_ix) != 1 or len(entry_ix) != 1:
                continue
            si, ei = int(signal_ix[0]), int(entry_ix[0])
            if si < 15 or ei <= si:
                continue
            signal_close = float(stock.at[si, "close"])
            prior_close = float(stock.at[si - 15, "close"])
            entry_open = float(stock.at[ei, "open"])
            if prior_close <= 0 or entry_open <= 0:
                continue
            # 次日一字涨停不能以开盘价买到：在实盘应跳过，故不让它进入候选。
            if entry_open / signal_close - 1.0 >= _limit_ratio(str(code)):
                continue
            item: dict[str, Any] = {
                "event_id": int(event_id), "code": str(code), "stock_runup_15d": signal_close / prior_close - 1.0,
                "entry_open": entry_open, "entry_gap": entry_open / signal_close - 1.0,
            }
            ok = True
            for hold in base.HOLDS:
                exit_date = pd.Timestamp(event[f"exit_{hold}d_date"])
                exit_ix = stock.index[stock["trade_date"] == exit_date]
                if len(exit_ix) != 1:
                    ok = False
                    break
                x = int(exit_ix[0])
                item[f"gross_ret_{hold}d"] = float(stock.at[x, "close"]) / entry_open - 1.0
                item[f"mfe_{hold}d"] = float(stock.loc[ei:x, "high"].max()) / entry_open - 1.0
                item[f"mae_{hold}d"] = float(stock.loc[ei:x, "low"].min()) / entry_open - 1.0
            if ok:
                candidates.append(item)
        ranked = sorted(candidates, key=lambda x: x["stock_runup_15d"], reverse=True)
        for top_n in (1, 3):
            chosen = ranked[:top_n]
            if len(chosen) < top_n:
                continue
            result: dict[str, Any] = {"event_id": int(event_id), "leader_mode": f"top{top_n}", "selected_count": len(chosen),
                                      "leader_codes": "|".join(x["code"] for x in chosen),
                                      "leader_runup_15d": sum(x["stock_runup_15d"] for x in chosen) / len(chosen),
                                      "avg_entry_gap": sum(x["entry_gap"] for x in chosen) / len(chosen)}
            for hold in base.HOLDS:
                result[f"gross_ret_{hold}d"] = sum(x[f"gross_ret_{hold}d"] for x in chosen) / len(chosen)
                result[f"mfe_{hold}d"] = sum(x[f"mfe_{hold}d"] for x in chosen) / len(chosen)
                result[f"mae_{hold}d"] = sum(x[f"mae_{hold}d"] for x in chosen) / len(chosen)
                result[f"net_ret_{hold}d"] = result[f"gross_ret_{hold}d"] - cost_bps / 10000.0
                result[f"excess_vs_csi1000_{hold}d"] = result[f"gross_ret_{hold}d"] - float(event[f"csi1000_ret_{hold}d"])
            rows.append(result)
    return pd.DataFrame(rows)


def _summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for threshold in (0.60, 0.65, 0.70):
        for mode, part in trades[trades["down_ratio"] >= threshold].groupby("leader_mode"):
            for hold in base.HOLDS:
                ret = pd.to_numeric(part[f"net_ret_{hold}d"], errors="coerce").dropna()
                ex = pd.to_numeric(part[f"excess_vs_csi1000_{hold}d"], errors="coerce").dropna()
                rows.append({"market_down_ratio_min": threshold, "leader_mode": mode, "hold_days": hold,
                             "event_count": len(ret), "avg_net_ret": ret.mean(), "median_net_ret": ret.median(),
                             "win_rate_net": (ret > 0).mean(), "avg_excess_vs_csi1000": ex.mean(),
                             "excess_win_rate": (ex > 0).mean(), "p25_net_ret": ret.quantile(.25), "worst_net_ret": ret.min(),
                             "avg_mfe": part[f"mfe_{hold}d"].mean(), "avg_mae": part[f"mae_{hold}d"].mean()})
    return pd.DataFrame(rows)


def _report(summary: pd.DataFrame, meta: dict[str, Any]) -> str:
    lines = ["# 强势板块首次回撤：板块内龙头回测", "", "## 龙头与执行定义", "",
             "- 先沿用基准事件：15 个交易日行业指数涨幅至少 20%、涨多跌少；随后第一次从峰值回撤 5%--8%；全市场下跌家数达到阈值。",
             "- 龙头：信号日行业现有成分中，过去 15 个交易日累计涨幅排名第 1；另报告 Top3 等权，排名只使用信号日及更早数据。",
             "- 执行：信号日收盘后确认，下一交易日开盘买入；若该股次日开盘即涨停，则判为不可成交并跳过。持有 N 个交易日后收盘卖出，双边成本 20bp。",
             f"- 样本：{meta['start']} 至 {meta['end']}，行业事件 {meta['base_events']} 个；可形成 Top1 龙头交易 {meta['top1_events']} 个。", "",
             "## 结果", "", "| 下跌家数阈值 | 龙头组合 | 持有日 | 事件数 | 平均净收益 | 胜率 | 相对中证1000平均超额 | 超额胜率 | P25净收益 | 最差净收益 |", "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in summary.itertuples(index=False):
        lines.append(f"| >={r.market_down_ratio_min:.0%} | {r.leader_mode} | {r.hold_days} | {r.event_count} | {base._pct(r.avg_net_ret)} | {base._pct(r.win_rate_net)} | {base._pct(r.avg_excess_vs_csi1000)} | {base._pct(r.excess_win_rate)} | {base._pct(r.p25_net_ret)} | {base._pct(r.worst_net_ret)} |")
    lines.extend(["", "## 边界", "", "- 成分股映射是当前申万二级行业映射回放，无法完全消除行业归属历史变动带来的偏差。", "- Top1/Top3 是单事件收益，不处理同日板块之间的资金容量竞争；不能直接复利成组合净值。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=base.DEFAULT_START); parser.add_argument("--end", default=base.DEFAULT_END)
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--output-dir", default=str(report_path("sector_pullback_leader_v1")))
    args = parser.parse_args()
    sector = base._load_sectors(args.start, args.end)
    breadth = base._load_market_breadth(args.start, args.end)
    events = base._arm_and_find_first_pullback(sector, 15, .20, 2.0).reset_index(drop=True)
    events["event_id"] = events.index.astype(int)
    events = events.merge(breadth[["trade_date", "down_ratio"]], left_on="signal_date", right_on="trade_date", how="left").drop(columns="trade_date")
    events = base._attach_csi1000_returns(events, args.start, args.end)
    events = events[events["down_ratio"] >= .60].copy()
    prices = _load_event_stock_windows(events)
    trades = _build_leader_trades(events, prices, args.cost_bps).merge(events[["event_id", "signal_date", "code", "down_ratio"]], on="event_id", how="left")
    summary = _summary(trades)
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    meta = {"start": args.start, "end": args.end, "base_events": int(len(events)), "top1_events": int((trades.leader_mode == 'top1').sum()), "top3_events": int((trades.leader_mode == 'top3').sum()), "price_rows": int(len(prices))}
    events.to_csv(out / "base_events.csv", index=False, encoding="utf-8-sig")
    trades.sort_values(["signal_date", "leader_mode"]).to_csv(out / "leader_trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
    (out / "report.md").write_text(_report(summary, meta), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps({"meta": meta, "summary": summary.to_dict("records")}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output_dir": str(out), **meta}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
