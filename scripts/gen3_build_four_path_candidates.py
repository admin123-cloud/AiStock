from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen3_four_path_independent_candidates"
INDEX_CODE = "999999.SH"
MARKET_STYLES = (
    "standard_uptrend",
    "weak_rebound",
    "standard_range",
    "standard_downtrend",
)


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if pd.isna(value):
        return None
    return str(value)


def _date(value: Any) -> str:
    return pd.to_datetime(value, errors="coerce").strftime("%Y-%m-%d")


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _load_trade_dates(start_date: str, end_date: str) -> list[str]:
    d = clickhouse_query_df(
        """
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE code = %(index_code)s
          AND trade_date BETWEEN subtractDays(toDate(%(start_date)s), 260) AND toDate(%(end_date)s)
        ORDER BY trade_date
        """,
        {"index_code": INDEX_CODE, "start_date": start_date, "end_date": end_date},
    )
    if d.empty:
        raise RuntimeError(f"No trade dates found for {INDEX_CODE}")
    return pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna().tolist()


def _load_stock_daily(start_date: str, end_date: str, max_codes: int = 0) -> pd.DataFrame:
    code_limit = ""
    if int(max_codes or 0) > 0:
        code_limit = f"LIMIT {int(max_codes)}"
    df = clickhouse_query_df(
        f"""
        WITH active_codes AS (
            SELECT code
            FROM stocks
            WHERE type = 'stock'
              AND COALESCE(st, 0) = 0
              AND COALESCE(quit, 0) = 0
            ORDER BY cityHash64(code)
            {code_limit}
        )
        SELECT
            k.code AS code,
            s.name AS name,
            k.trade_date,
            k.open,
            k.high,
            k.low,
            k.close,
            k.volume,
            k.amount,
            k.turnover_rate
        FROM kline_daily AS k
        INNER JOIN active_codes AS ac ON ac.code = k.code
        LEFT JOIN stocks AS s ON s.code = k.code
        WHERE k.trade_date BETWEEN subtractDays(toDate(%(start_date)s), 260) AND toDate(%(end_date)s)
        ORDER BY k.code, k.trade_date
        """,
        {"start_date": start_date, "end_date": end_date},
    )
    if df.empty:
        raise RuntimeError("No stock daily data loaded")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["code", "trade_date", "close"]).sort_values(["code", "trade_date"]).reset_index(drop=True)


def _load_index_daily(start_date: str, end_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT trade_date, open, high, low, close, volume, amount, turnover_rate
        FROM kline_daily
        WHERE code = %(index_code)s
          AND trade_date BETWEEN subtractDays(toDate(%(start_date)s), 260) AND toDate(%(end_date)s)
        ORDER BY trade_date
        """,
        {"index_code": INDEX_CODE, "start_date": start_date, "end_date": end_date},
    )
    if df.empty:
        raise RuntimeError(f"No index daily data loaded for {INDEX_CODE}")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)


def _add_stock_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy().replace([np.inf, -np.inf], np.nan)
    g = d.groupby("code", sort=False)
    d["ret1"] = g["close"].pct_change()
    d["ma5"] = g["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    d["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    d["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["ma60"] = g["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    d["high20"] = g["high"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).max())
    d["high60"] = g["high"].transform(lambda s: s.shift(1).rolling(60, min_periods=60).max())
    d["low20"] = g["low"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).min())
    d["low60"] = g["low"].transform(lambda s: s.shift(1).rolling(60, min_periods=60).min())
    d["mom5"] = g["close"].transform(lambda s: s / s.shift(5) - 1.0)
    d["mom10"] = g["close"].transform(lambda s: s / s.shift(10) - 1.0)
    d["mom20"] = g["close"].transform(lambda s: s / s.shift(20) - 1.0)
    d["drawdown5"] = d["close"] / g["high"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).max()) - 1.0
    d["drawdown10"] = d["close"] / g["high"].transform(lambda s: s.shift(1).rolling(10, min_periods=10).max()) - 1.0
    d["drawdown20"] = d["close"] / g["high"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).max()) - 1.0
    d["runup_from_60d_low"] = d["close"] / d["low60"] - 1.0
    d["range_pos20"] = (d["close"] - d["low20"]) / (d["high20"] - d["low20"])
    d["range_pos60"] = (d["close"] - d["low60"]) / (d["high60"] - d["low60"])
    d["amount5"] = g["amount"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    d["amount20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["amount_ratio5"] = d["amount"] / d["amount5"]
    d["amount_ratio20"] = d["amount"] / d["amount20"]
    d["lower_shadow_ratio"] = np.where(
        (d["high"] - d["low"]) > 0,
        (np.minimum(d["open"], d["close"]) - d["low"]) / (d["high"] - d["low"]),
        np.nan,
    )
    d["close_position"] = np.where((d["high"] - d["low"]) > 0, (d["close"] - d["low"]) / (d["high"] - d["low"]), np.nan)
    d["gap_open"] = d["open"] / g["close"].shift(1) - 1.0
    return d.replace([np.inf, -np.inf], np.nan)


def _add_index_features(index_df: pd.DataFrame) -> pd.DataFrame:
    d = index_df.copy().replace([np.inf, -np.inf], np.nan)
    d["ret1"] = d["close"].pct_change()
    for n in [20, 60, 120]:
        d[f"ma{n}"] = d["close"].rolling(n, min_periods=n).mean()
    d["mom20"] = d["close"] / d["close"].shift(20) - 1.0
    d["mom60"] = d["close"] / d["close"].shift(60) - 1.0
    d["amount20"] = d["amount"].rolling(20, min_periods=20).mean()
    d["amount60"] = d["amount"].rolling(60, min_periods=60).mean()
    d["index_amount_ratio20"] = d["amount"] / d["amount20"]
    d["turnover20"] = d["turnover_rate"].rolling(20, min_periods=20).mean()
    d["turnover60"] = d["turnover_rate"].rolling(60, min_periods=60).mean()
    d["turnover20_vs_60"] = d["turnover20"] / d["turnover60"]

    prev_close = d["close"].shift(1)
    up_move = d["high"].diff()
    down_move = -d["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat(
        [
            (d["high"] - d["low"]).abs(),
            (d["high"] - prev_close).abs(),
            (d["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr20 = tr.rolling(20, min_periods=20).mean()
    d["plus_di20"] = 100.0 * pd.Series(plus_dm, index=d.index).rolling(20, min_periods=20).mean() / atr20
    d["minus_di20"] = 100.0 * pd.Series(minus_dm, index=d.index).rolling(20, min_periods=20).mean() / atr20
    di_sum = d["plus_di20"] + d["minus_di20"]
    dx = 100.0 * (d["plus_di20"] - d["minus_di20"]).abs() / di_sum
    d["adx20"] = dx.rolling(20, min_periods=20).mean()
    return d.replace([np.inf, -np.inf], np.nan)


def _build_market_context(stock_features: pd.DataFrame, index_features: pd.DataFrame, min_amount20: float) -> pd.DataFrame:
    liquid = stock_features[stock_features["amount20"] >= float(min_amount20)].copy()
    liquid["above_ma20"] = liquid["close"] > liquid["ma20"]
    liquid["above_ma60"] = liquid["close"] > liquid["ma60"]
    liquid["big_down"] = liquid["ret1"] <= -0.05
    liquid["limit_down_proxy"] = liquid["ret1"] <= -0.095
    breadth = (
        liquid.groupby("trade_date", sort=True)
        .agg(
            stock_count=("code", "count"),
            up_rate=("ret1", lambda s: float((s > 0).mean())),
            big_down_rate=("big_down", "mean"),
            limit_down_proxy_rate=("limit_down_proxy", "mean"),
            breadth_ma20=("above_ma20", "mean"),
            breadth_ma60=("above_ma60", "mean"),
            median_ret=("ret1", "median"),
            median_mom20=("mom20", "median"),
            market_amount=("amount", "sum"),
            market_amount20=("amount20", "sum"),
        )
        .reset_index()
    )
    breadth["market_amount_ratio20"] = breadth["market_amount"] / breadth["market_amount20"]
    ctx = index_features.merge(breadth, on="trade_date", how="left")
    ctx["ma_skeleton"] = np.select(
        [
            (ctx["close"] > ctx["ma20"]) & (ctx["ma20"] > ctx["ma60"]) & (ctx["ma60"] > ctx["ma120"]),
            (ctx["close"] >= ctx["ma20"]) & (ctx["ma20"] < ctx["ma60"]),
            (ctx["close"] < ctx["ma20"]) & (ctx["ma20"] < ctx["ma60"]) & (ctx["ma60"] < ctx["ma120"]),
        ],
        ["bull_stack", "weak_repair", "bear_stack"],
        default="mixed",
    )
    ctx["volume_price_layer"] = np.select(
        [
            (ctx["index_amount_ratio20"] >= 1.05) & (ctx["turnover20_vs_60"] >= 1.0) & (ctx["ret1"] > 0),
            (ctx["index_amount_ratio20"] >= 1.15) & (ctx["ret1"] < 0),
            (ctx["index_amount_ratio20"] <= 0.90) & (ctx["turnover20_vs_60"] <= 1.0),
        ],
        ["up_with_volume", "down_with_volume", "shrinking_balance"],
        default="neutral_volume_price",
    )
    ctx["adx_layer"] = np.select(
        [
            (ctx["adx20"] >= 20) & (ctx["plus_di20"] > ctx["minus_di20"]),
            (ctx["adx20"] >= 20) & (ctx["plus_di20"] < ctx["minus_di20"]),
            ctx["adx20"] < 18,
        ],
        ["uptrend_strength", "downtrend_strength", "range_strength"],
        default="weak_trend_strength",
    )
    ctx["market_style"] = ctx.apply(_classify_market_style, axis=1)
    return ctx


def _classify_market_style(row: pd.Series) -> str:
    breadth = row.get("breadth_ma20")
    up_rate = row.get("up_rate")
    close = row.get("close")
    ma20 = row.get("ma20")
    ma60 = row.get("ma60")
    ma120 = row.get("ma120")
    mom20 = row.get("mom20")
    adx = row.get("adx20")
    plus_di = row.get("plus_di20")
    minus_di = row.get("minus_di20")
    amount_ratio = row.get("market_amount_ratio20")

    if pd.isna(close) or pd.isna(ma20) or pd.isna(ma60) or pd.isna(ma120):
        return "standard_range"

    bull_ma = close > ma20 and ma20 > ma60 and ma60 > ma120
    bear_ma = close < ma20 and ma20 < ma60 and ma60 < ma120
    trend_up = pd.notna(adx) and adx >= 20 and pd.notna(plus_di) and plus_di > minus_di
    trend_down = pd.notna(adx) and adx >= 20 and pd.notna(minus_di) and minus_di > plus_di
    healthy_breadth = pd.notna(breadth) and breadth >= 0.55 and pd.notna(up_rate) and up_rate >= 0.50
    weak_breadth = pd.notna(breadth) and breadth <= 0.35
    icepoint = pd.notna(up_rate) and up_rate <= 0.25

    if bull_ma and trend_up and healthy_breadth and pd.notna(mom20) and mom20 > 0:
        return "standard_uptrend"
    if bear_ma and (trend_down or weak_breadth or icepoint):
        return "standard_downtrend"
    if (close >= ma20 or close >= ma60) and not bull_ma and pd.notna(mom20) and mom20 > -0.03:
        if pd.notna(amount_ratio) and amount_ratio >= 0.90 and pd.notna(up_rate) and up_rate >= 0.40:
            return "weak_rebound"
    return "standard_range"


def _with_entry_date(d: pd.DataFrame, trade_dates: list[str]) -> pd.DataFrame:
    next_map = {trade_dates[i]: trade_dates[i + 1] for i in range(len(trade_dates) - 1)}
    out = d.copy()
    out["entry_date"] = out["trade_date"].map(next_map)
    return out.dropna(subset=["entry_date"]).copy()


def _rank_chain(d: pd.DataFrame, chain: str, top_n: int) -> pd.DataFrame:
    if d.empty:
        return d
    out = d.copy()
    if chain == "strong_trend_breakout":
        out["candidate_score"] = (
            0.30 * out["mom20"].rank(pct=True)
            + 0.25 * out["mom10"].rank(pct=True)
            + 0.25 * out["amount_ratio20"].clip(0, 3).rank(pct=True)
            + 0.20 * out["range_pos60"].clip(0, 1).rank(pct=True)
        )
        out["entry_weight_hint"] = 1.0
    elif chain == "weak_rebound_repair":
        out["candidate_score"] = (
            0.30 * out["close_position"].rank(pct=True)
            + 0.25 * out["amount_ratio20"].clip(0, 3).rank(pct=True)
            + 0.25 * (-out["drawdown20"]).rank(pct=True)
            + 0.20 * out["mom5"].rank(pct=True)
        )
        out["entry_weight_hint"] = 0.40
    elif chain == "range_box_bottom":
        out["candidate_score"] = (
            0.35 * (1.0 - out["range_pos60"].clip(0, 1)).rank(pct=True)
            + 0.25 * out["close_position"].rank(pct=True)
            + 0.20 * out["amount_ratio20"].clip(0, 3).rank(pct=True)
            + 0.20 * (-out["drawdown20"]).rank(pct=True)
        )
        out["entry_weight_hint"] = 0.30
    elif chain == "downtrend_panic_capitulation":
        out["candidate_score"] = (
            0.35 * (-out["drawdown10"]).rank(pct=True)
            + 0.25 * out["lower_shadow_ratio"].rank(pct=True)
            + 0.25 * out["amount_ratio20"].clip(0, 4).rank(pct=True)
            + 0.15 * out["close_position"].rank(pct=True)
        )
        out["entry_weight_hint"] = 0.20
    else:
        raise ValueError(f"Unknown chain: {chain}")
    out["g3_chain"] = chain
    out = out.sort_values(["trade_date", "candidate_score", "amount20"], ascending=[True, False, False])
    out["chain_rank"] = out.groupby("trade_date").cumcount() + 1
    return out[out["chain_rank"] <= int(top_n)].copy()


def _select_four_path_candidates(features: pd.DataFrame, ctx: pd.DataFrame, top_n: int, min_amount20: float) -> pd.DataFrame:
    d = features.merge(
        ctx[
            [
                "trade_date",
                "market_style",
                "ma_skeleton",
                "volume_price_layer",
                "adx_layer",
                "breadth_ma20",
                "breadth_ma60",
                "up_rate",
                "big_down_rate",
                "limit_down_proxy_rate",
                "market_amount_ratio20",
                "adx20",
                "plus_di20",
                "minus_di20",
                "mom20",
            ]
        ].rename(columns={"mom20": "index_mom20"}),
        on="trade_date",
        how="left",
    )
    d = d[d["amount20"] >= float(min_amount20)].copy()
    d = d[d["close"] > 0].copy()

    strong = d[
        (d["market_style"] == "standard_uptrend")
        & (d["close"] > d["ma20"])
        & (d["ma20"] > d["ma60"])
        & (d["mom20"] >= 0.08)
        & (d["mom10"] >= 0.03)
        & (d["close"] >= d["high20"] * 0.995)
        & (d["amount_ratio20"].between(1.0, 3.5))
        & (d["range_pos60"] >= 0.55)
    ].copy()

    weak = d[
        (d["market_style"] == "weak_rebound")
        & (d["drawdown20"].between(-0.35, -0.08))
        & (d["close"] >= d["ma5"])
        & (d["close_position"] >= 0.55)
        & (d["amount_ratio20"].between(0.8, 3.0))
        & (d["range_pos60"].between(0.15, 0.65))
    ].copy()

    range_bottom = d[
        (d["market_style"] == "standard_range")
        & (d["range_pos60"] <= 0.28)
        & (d["drawdown20"] <= -0.08)
        & ((d["breadth_ma20"] <= 0.45) | (d["up_rate"] <= 0.35))
        & ((d["close"] > d["open"]) | (d["close_position"] >= 0.55) | (d["lower_shadow_ratio"] >= 0.35))
        & (d["amount_ratio20"].between(0.7, 3.5))
    ].copy()

    panic = d[
        (d["market_style"] == "standard_downtrend")
        & ((d["up_rate"] <= 0.25) | (d["big_down_rate"] >= 0.18) | (d["limit_down_proxy_rate"] >= 0.015))
        & ((d["drawdown10"] <= -0.12) | (d["drawdown20"] <= -0.20))
        & (d["close_position"] >= 0.35)
        & (d["lower_shadow_ratio"] >= 0.25)
        & (d["ret1"] > -0.095)
        & (d["amount_ratio20"].between(1.0, 4.5))
        & (d["range_pos60"] <= 0.45)
    ].copy()

    parts = [
        _rank_chain(strong, "strong_trend_breakout", top_n),
        _rank_chain(weak, "weak_rebound_repair", top_n),
        _rank_chain(range_bottom, "range_box_bottom", top_n),
        _rank_chain(panic, "downtrend_panic_capitulation", top_n),
    ]
    selected = pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(not p.empty for p in parts) else pd.DataFrame()
    if selected.empty:
        return selected
    return selected.sort_values(["trade_date", "g3_chain", "chain_rank", "code"]).reset_index(drop=True)


def _write_report(output_dir: Path, summary: dict[str, Any], market_context: pd.DataFrame, candidates: pd.DataFrame) -> None:
    lines: list[str] = [
        "# G3 四链路独立候选源 V1",
        "",
        "## 定位",
        "",
        "- 这是 G3 第三代策略研究候选源，不写入 G2 runtime，不触发 G2 official rebuild，也不改变 G2 买点监控。",
        "- 候选从全市场日线数据独立生成，不再从 `g2_v2_complete` 的 volume5/突破池里反切弱势或震荡买法。",
        "- 信号日使用当日收盘后可确认的日线状态，`entry_date` 固定为下一交易日；后续接 15m/30m 确认时再做盘中可见性审计。",
        "",
        "## 四条链路",
        "",
        "| 链路 | 市场状态 | 选股核心 | 仓位提示 |",
        "| --- | --- | --- | ---: |",
        "| strong_trend_breakout | 标准上升主升 | 个股多头、20日高位/突破、放量但不过热 | 100% |",
        "| weak_rebound_repair | 弱势反弹 | 中短期回撤后修复 MA5、收在日内较强位置 | 40% |",
        "| range_box_bottom | 标准震荡 | 60日箱体底部、市场宽度偏冷、出现反抽/下影 | 30% |",
        "| downtrend_panic_capitulation | 标准下跌 | 市场冰点/大跌扩散、个股深回撤后恐慌出清 | 20% |",
        "",
        "## 市场状态不是只看指数强弱",
        "",
        "- 均线骨架层：指数 MA20/MA60/MA120 与收盘相对位置。",
        "- 量价结构层：指数成交额/换手的 20日、60日结构，以及放量上涨/放量下跌/缩量均衡。",
        "- ADX 趋势强度层：ADX20、+DI、-DI 判断趋势强度和方向。",
        "- 全市场宽度层：全市场可交易股票的 MA20/MA60 宽度、上涨率、大跌率、跌停近似率。",
        "- 个股结构层：位置、箱体分位、回撤、量能、下影/收盘位置，决定同一市场状态下买哪类票。",
        "",
        "## 本次输出摘要",
        "",
        f"- 日期范围：`{summary['start_date']}` ~ `{summary['end_date']}`",
        f"- 股票日线样本：`{summary['stock_rows']}` 行，股票数 `{summary['stock_count']}`",
        f"- 候选总数：`{summary['candidate_rows']}`",
        "",
        "| 链路 | 候选数 | 覆盖交易日 | 平均每日候选 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for item in summary["chain_summary"]:
        lines.append(
            f"| {item['g3_chain']} | {item['rows']} | {item['days']} | {item['avg_per_day']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## 市场状态分布",
            "",
            "| 市场状态 | 天数 | 占比 |",
            "| --- | ---: | ---: |",
        ]
    )
    style_counts = market_context[(market_context["trade_date"] >= summary["start_date"]) & (market_context["trade_date"] <= summary["end_date"])][
        "market_style"
    ].value_counts()
    total_days = int(style_counts.sum())
    for style in MARKET_STYLES:
        cnt = int(style_counts.get(style, 0))
        ratio = cnt / total_days if total_days else 0.0
        lines.append(f"| {style} | {cnt} | {_pct(ratio)} |")
    lines.extend(
        [
            "",
            "## 防过拟合约束",
            "",
            "- V1 只定义可解释、粗粒度候选生成，不根据回测收益调细参数。",
            "- 四条链路先独立验证，再考虑路由组合；弱势/震荡链路不允许靠强势链路的收益掩盖亏损。",
            "- 下一步需要按 train/valid/blind/full 分段做前瞻标签和组合回测，未通过前不进入正式交易页或自动下单。",
            "",
        ]
    )
    output_dir.joinpath("README_CN.md").write_text("\n".join(lines), encoding="utf-8")


def build_candidates(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    trade_dates = _load_trade_dates(args.start_date, args.end_date)
    stocks = _load_stock_daily(args.start_date, args.end_date, max_codes=int(args.max_codes or 0))
    index = _load_index_daily(args.start_date, args.end_date)
    features = _add_stock_features(stocks)
    index_features = _add_index_features(index)
    market_context = _build_market_context(features, index_features, min_amount20=float(args.min_amount20))
    candidates = _select_four_path_candidates(features, market_context, top_n=int(args.top_n), min_amount20=float(args.min_amount20))
    candidates = _with_entry_date(candidates, trade_dates)
    candidates = candidates[(candidates["entry_date"] >= args.start_date) & (candidates["entry_date"] <= args.end_date)].copy()

    keep_cols = [
        "trade_date",
        "entry_date",
        "code",
        "name",
        "g3_chain",
        "market_style",
        "chain_rank",
        "candidate_score",
        "entry_weight_hint",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "breadth_ma20",
        "breadth_ma60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "adx20",
        "plus_di20",
        "minus_di20",
        "index_mom20",
        "open",
        "high",
        "low",
        "close",
        "ret1",
        "mom5",
        "mom10",
        "mom20",
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "high20",
        "low20",
        "high60",
        "low60",
        "drawdown5",
        "drawdown10",
        "drawdown20",
        "runup_from_60d_low",
        "range_pos20",
        "range_pos60",
        "amount",
        "amount20",
        "amount_ratio5",
        "amount_ratio20",
        "turnover_rate",
        "lower_shadow_ratio",
        "close_position",
        "gap_open",
    ]
    candidates = candidates[[col for col in keep_cols if col in candidates.columns]].sort_values(
        ["entry_date", "g3_chain", "chain_rank", "code"]
    )

    market_context_path = output_dir / "market_context.csv"
    candidates_path = output_dir / "g3_daily_candidates.parquet"
    market_context.to_csv(market_context_path, index=False, encoding="utf-8-sig")
    candidates.to_parquet(candidates_path, index=False)
    candidates.to_csv(output_dir / "g3_daily_candidates.csv", index=False, encoding="utf-8-sig")

    chain_summary = []
    if not candidates.empty:
        for chain, group in candidates.groupby("g3_chain", sort=True):
            days = int(group["trade_date"].nunique())
            rows = int(len(group))
            chain_summary.append({"g3_chain": chain, "rows": rows, "days": days, "avg_per_day": rows / days if days else 0.0})
    summary = {
        "version": "g3_four_path_independent_candidates_v1",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "index_code": INDEX_CODE,
        "output_dir": str(output_dir),
        "market_context_path": str(market_context_path),
        "candidates_path": str(candidates_path),
        "stock_rows": int(len(stocks)),
        "stock_count": int(stocks["code"].nunique()),
        "candidate_rows": int(len(candidates)),
        "top_n_per_chain_per_day": int(args.top_n),
        "min_amount20": float(args.min_amount20),
        "max_codes": int(args.max_codes or 0),
        "chain_summary": chain_summary,
        "g2_runtime_touched": False,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, summary, market_context, candidates)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build G3 four-path independent daily candidate sources.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-05-29")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--top-n", type=int, default=20, help="Max candidates per chain per signal day.")
    parser.add_argument("--min-amount20", type=float, default=30000.0, help="20-day avg amount liquidity floor; local amount unit follows kline_daily.")
    parser.add_argument("--max-codes", type=int, default=0, help="Smoke-test limit. 0 means all active stocks.")
    args = parser.parse_args()

    summary = build_candidates(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
