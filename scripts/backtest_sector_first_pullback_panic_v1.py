"""事件研究：强势行业首次回撤叠加全市场下跌扩散后的短持有收益。

信号在收盘后确认，统一按下一交易日开盘买、持有 N 个交易日到收盘卖，
因此不会用到信号日之后的价格。输出是板块指数代理的单事件收益，不是将
重叠事件强行叠加后的组合净值。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


DEFAULT_START = "2020-01-01"
DEFAULT_END = "2025-12-31"  # 2026 行业口径切换，主报告不混入未完成年度
HOLDS = (1, 2, 3)
PANIC_THRESHOLDS = (0.00, 0.60, 0.65, 0.70)


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return ""


def _load_sectors(start: str, end: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT code, trade_date, open, high, low, close, change_pct,
               stock_count, rise_count, fall_count
        FROM sector_kline_daily
        WHERE trade_date BETWEEN ? AND ?
          AND open > 0 AND high > 0 AND low > 0 AND close > 0
        ORDER BY code, trade_date
        """,
        [start, end],
    )
    if df.empty:
        raise RuntimeError("sector_kline_daily has no usable OHLC rows in requested period")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for col in ["open", "high", "low", "close", "change_pct", "stock_count", "rise_count", "fall_count"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "trade_date", "open", "high", "low", "close"])
    return df.drop_duplicates(["code", "trade_date"], keep="last").sort_values(["code", "trade_date"]).reset_index(drop=True)


def _load_market_breadth(start: str, end: str) -> pd.DataFrame:
    # 用股票原始日线重建“下跌家数”；保留退市股票在其历史存续期内的观察，
    # 不以当前是否 ST/退市过滤，尽量降低幸存者偏差。
    df = clickhouse_query_df(
        """
        SELECT k.trade_date,
               count() AS stock_count,
               sum(k.change_pct < -0.001) AS down_count,
               sum(k.change_pct > 0.001) AS up_count
        FROM kline_daily AS k
        WHERE k.code IN (SELECT code FROM stocks WHERE type = 'stock')
          AND k.trade_date BETWEEN ? AND ?
          AND k.close > 0
        GROUP BY k.trade_date
        ORDER BY k.trade_date
        """,
        [start, end],
    )
    if df.empty:
        raise RuntimeError("could not reconstruct daily market breadth from kline_daily")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for col in ["stock_count", "down_count", "up_count"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["trade_date", "stock_count"]).copy()
    df["down_ratio"] = df["down_count"] / df["stock_count"].replace(0, np.nan)
    return df


def _attach_csi1000_returns(events: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """以中证1000衡量该类板块事件是否只是市场反弹的同义词。"""
    index = clickhouse_query_df(
        """
        SELECT trade_date, open, close FROM kline_daily
        WHERE code = '000852.SH' AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """, [start, end]
    )
    index["trade_date"] = pd.to_datetime(index["trade_date"], errors="coerce")
    for col in ["open", "close"]:
        index[col] = pd.to_numeric(index[col], errors="coerce")
    index = index.dropna(subset=["trade_date", "open", "close"]).set_index("trade_date")
    out = events.copy()
    for hold in HOLDS:
        entry = out["entry_date"].map(index["open"])
        exit_price = out[f"exit_{hold}d_date"].map(index["close"])
        out[f"csi1000_ret_{hold}d"] = exit_price / entry - 1.0
        out[f"excess_vs_csi1000_{hold}d"] = out[f"gross_ret_{hold}d"] - out[f"csi1000_ret_{hold}d"]
    return out


def _arm_and_find_first_pullback(sector: pd.DataFrame, runup_days: int, min_runup: float, up_down_ratio: float) -> pd.DataFrame:
    """强势期形成后只记录第一次落入 5%--8% 的回撤，不反复触发。"""
    out: list[dict[str, Any]] = []
    for code, frame in sector.groupby("code", sort=False):
        f = frame.sort_values("trade_date").reset_index(drop=True).copy()
        dates = f["trade_date"].to_numpy()
        closes = f["close"].to_numpy(dtype=float)
        opens = f["open"].to_numpy(dtype=float)
        highs = f["high"].to_numpy(dtype=float)
        lows = f["low"].to_numpy(dtype=float)
        daily_ret = np.zeros(len(f), dtype=float)
        daily_ret[1:] = closes[1:] / closes[:-1] - 1.0
        # 前缀和避免在每个交易日反复切片 DataFrame；回测期约 19 万个板块日。
        def prefix(values: np.ndarray) -> np.ndarray:
            return np.concatenate(([0.0], np.cumsum(values)))
        up_count = prefix((daily_ret > 0.0001).astype(float))
        down_count = prefix((daily_ret < -0.0001).astype(float))
        pos_total = prefix(np.where(daily_ret > 0, daily_ret, 0.0))
        neg_total = prefix(np.where(daily_ret < 0, -daily_ret, 0.0))
        armed = False
        peak_close = np.nan
        peak_date: pd.Timestamp | None = None
        prev_drawdown = 0.0
        arm_runup = np.nan
        arm_ups = arm_downs = 0
        arm_pos_sum = arm_neg_sum = np.nan
        for i in range(runup_days, len(f) - max(HOLDS) - 1):
            left, right = i - runup_days + 1, i + 1
            start_close = closes[i - runup_days]
            close = closes[i]
            runup = close / start_close - 1.0 if start_close > 0 else np.nan
            ups = int(up_count[right] - up_count[left])
            downs = int(down_count[right] - down_count[left])
            pos_sum = float(pos_total[right] - pos_total[left])
            neg_sum = float(neg_total[right] - neg_total[left])
            strong_shape = ups >= max(6, int(np.ceil(downs * up_down_ratio))) and pos_sum >= max(0.001, neg_sum * up_down_ratio)
            if (not armed) and runup >= min_runup and strong_shape:
                armed, peak_close, peak_date, prev_drawdown = True, close, pd.Timestamp(dates[i]), 0.0
                arm_runup, arm_ups, arm_downs = runup, ups, downs
                arm_pos_sum, arm_neg_sum = pos_sum, neg_sum
                continue
            if not armed:
                continue
            if close >= peak_close:
                peak_close, peak_date, prev_drawdown = close, pd.Timestamp(dates[i]), 0.0
                continue
            drawdown = close / peak_close - 1.0
            # “首次出现回撤 5%-8%”：首次跌破 5%，且当日未超过 8%。
            if prev_drawdown > -0.05 and -0.08 <= drawdown <= -0.05:
                event: dict[str, Any] = {
                    "code": str(code), "signal_date": pd.Timestamp(dates[i]), "peak_date": peak_date,
                    "runup": arm_runup, "ups": arm_ups, "downs": arm_downs, "pos_sum": arm_pos_sum,
                    "neg_sum": arm_neg_sum, "signal_window_runup": runup,
                    "pullback": drawdown, "signal_close": close,
                    "entry_date": pd.Timestamp(dates[i + 1]), "entry_open": opens[i + 1],
                }
                for hold in HOLDS:
                    entry_open = event["entry_open"]
                    event[f"exit_{hold}d_date"] = pd.Timestamp(dates[i + hold])
                    event[f"gross_ret_{hold}d"] = closes[i + hold] / entry_open - 1.0
                    event[f"mfe_{hold}d"] = highs[i + 1 : i + hold + 1].max() / entry_open - 1.0
                    event[f"mae_{hold}d"] = lows[i + 1 : i + hold + 1].min() / entry_open - 1.0
                out.append(event)
                armed = False  # 本轮上涨只允许一次“首次回撤”
            elif drawdown < -0.08:
                armed = False
            else:
                prev_drawdown = drawdown
    return pd.DataFrame(out)


def _summarize(events: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if events.empty:
        return pd.DataFrame()
    for threshold in PANIC_THRESHOLDS:
        subset = events if threshold == 0 else events[events["down_ratio"] >= threshold]
        for hold in HOLDS:
            gross = pd.to_numeric(subset[f"gross_ret_{hold}d"], errors="coerce").dropna()
            net = gross - cost_bps / 10000.0
            rows.append(
                {
                    "market_down_ratio_min": threshold, "hold_days": hold, "event_count": len(net),
                    "avg_gross_ret": gross.mean(), "median_gross_ret": gross.median(),
                    "win_rate_gross": (gross > 0).mean(), "avg_net_ret": net.mean(),
                    "win_rate_net": (net > 0).mean(), "p25_net_ret": net.quantile(0.25),
                    "p75_net_ret": net.quantile(0.75), "worst_net_ret": net.min(),
                    "avg_excess_vs_csi1000": pd.to_numeric(subset[f"excess_vs_csi1000_{hold}d"], errors="coerce").mean(),
                    "excess_win_rate_vs_csi1000": (pd.to_numeric(subset[f"excess_vs_csi1000_{hold}d"], errors="coerce") > 0).mean(),
                    "avg_mfe": pd.to_numeric(subset[f"mfe_{hold}d"], errors="coerce").mean(),
                    "avg_mae": pd.to_numeric(subset[f"mae_{hold}d"], errors="coerce").mean(),
                }
            )
    return pd.DataFrame(rows)


def _yearly(events: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for threshold in (0.60, 0.65, 0.70):
        for year, part in events[events["down_ratio"] >= threshold].groupby(events["signal_date"].dt.year):
            for hold in HOLDS:
                net = pd.to_numeric(part[f"gross_ret_{hold}d"], errors="coerce") - cost_bps / 10000.0
                rows.append({"year": int(year), "market_down_ratio_min": threshold, "hold_days": hold,
                             "event_count": len(net), "avg_net_ret": net.mean(), "win_rate_net": (net > 0).mean()})
    return pd.DataFrame(rows)


def _markdown(summary: pd.DataFrame, yearly: pd.DataFrame, meta: dict[str, Any]) -> str:
    lines = ["# 强势板块首次回撤 + 市场恐慌：短持有事件回测", "", "## 规则", "",
             f"- 样本：{meta['start']} 至 {meta['end']}，{meta['sector_count']} 个有完整 OHLC 的行业板块。",
             f"- 强势期：过去 {meta['runup_days']} 个交易日涨幅至少 {_pct(meta['min_runup'])}；上涨日数至少为下跌日数的 {meta['up_down_ratio']:.1f} 倍，且上涨日累计涨幅同样至少为下跌日累计绝对跌幅的 {meta['up_down_ratio']:.1f} 倍。",
             "- 入场：上述强势期后的第一次从阶段峰值回撤 5%--8%（首次跌破 5%，未深于 8%）；信号收盘后确认，下一交易日开盘买入。",
             "- 恐慌：信号日全市场下跌股票占比达到 60% / 65% / 70%。下跌定义为日涨跌幅 < -0.001%。",
             f"- 出场：持有 1 / 2 / 3 个交易日后收盘卖出；净收益扣除双边合计 {meta['cost_bps']:.0f}bp。板块指数代理不可直接等同于单只股票或可交易 ETF。", "",
             "## 全样本结果", "",
             "| 下跌家数阈值 | 持有日 | 事件数 | 平均净收益 | 胜率 | 相对中证1000平均超额 | 超额胜率 | P25净收益 | P75净收益 |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in summary.itertuples(index=False):
        label = "不加情绪" if r.market_down_ratio_min == 0 else f">={r.market_down_ratio_min:.0%}"
        lines.append(f"| {label} | {r.hold_days} | {r.event_count} | {_pct(r.avg_net_ret)} | {_pct(r.win_rate_net)} | {_pct(r.avg_excess_vs_csi1000)} | {_pct(r.excess_win_rate_vs_csi1000)} | {_pct(r.p25_net_ret)} | {_pct(r.p75_net_ret)} |")
    lines.extend(["", "## 分年稳定性（仅恐慌样本）", "", "| 年份 | 下跌家数阈值 | 持有日 | 事件数 | 平均净收益 | 胜率 |", "|---:|---:|---:|---:|---:|---:|"])
    for r in yearly.itertuples(index=False):
        lines.append(f"| {r.year} | >={r.market_down_ratio_min:.0%} | {r.hold_days} | {r.event_count} | {_pct(r.avg_net_ret)} | {_pct(r.win_rate_net)} |")
    lines.extend(["", "## 解读边界", "", "- 这是事件研究：同日多个板块事件会并存，不能将全部事件收益直接复利为一个可执行组合。", "- 2020--2025 使用历史行业指数口径；2026 行业体系发生切换，因此未纳入主结论。", "- 下一步若要形成实盘规则，应先明确买板块 ETF、板块内龙头或等权成分股；三者的滑点、涨跌停约束与收益会明显不同。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest strong-sector first pullback under market-down breadth")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--runup-days", type=int, default=15)
    parser.add_argument("--min-runup", type=float, default=0.20)
    parser.add_argument("--up-down-ratio", type=float, default=2.0)
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--output-dir", default=str(report_path("sector_first_pullback_panic_v1")))
    args = parser.parse_args()
    sector = _load_sectors(args.start, args.end)
    breadth = _load_market_breadth(args.start, args.end)
    events = _arm_and_find_first_pullback(sector, args.runup_days, args.min_runup, args.up_down_ratio)
    if events.empty:
        raise RuntimeError("no first-pullback events; relax the rule or inspect sector data")
    events = events.merge(breadth[["trade_date", "stock_count", "down_count", "up_count", "down_ratio"]], left_on="signal_date", right_on="trade_date", how="left").drop(columns="trade_date")
    events = _attach_csi1000_returns(events, args.start, args.end)
    events["net_cost"] = args.cost_bps / 10000.0
    for hold in HOLDS:
        events[f"net_ret_{hold}d"] = events[f"gross_ret_{hold}d"] - events["net_cost"]
    summary = _summarize(events, args.cost_bps)
    yearly = _yearly(events, args.cost_bps)
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    meta = {"start": args.start, "end": args.end, "runup_days": args.runup_days, "min_runup": args.min_runup, "up_down_ratio": args.up_down_ratio, "cost_bps": args.cost_bps, "sector_count": int(sector["code"].nunique()), "event_count_before_panic": int(len(events)), "breadth_coverage": int(events["down_ratio"].notna().sum())}
    events.sort_values(["signal_date", "code"]).to_csv(out / "events.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(out / "yearly.csv", index=False, encoding="utf-8-sig")
    (out / "summary.json").write_text(json.dumps({"meta": meta, "summary": summary.to_dict("records"), "yearly": yearly.to_dict("records")}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(_markdown(summary, yearly, meta), encoding="utf-8")
    print(json.dumps({"output_dir": str(out), **meta}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
