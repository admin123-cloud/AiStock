# -*- coding: utf-8 -*-
"""AI 策略挖掘工坊 —— 只读 ClickHouse 特征宽表与本地 Parquet 缓存引擎。

核心铁律：
1. 对 ClickHouse 严格只读（仅执行 SELECT，绝不写入或修改 stock.kline_daily）；
2. 全部特征严格 T 日收盘已知（零未来函数），交易撮合统一从 T+1 日开盘开始；
3. 内建全市场流动性达标门禁（默认 20 日均成交额 >= 5000 万元）与 ST/退市/次新过滤。
"""
from __future__ import annotations

import io
import os
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))

# 可供规则挖掘器使用的全部明文特征（均严格 T 日收盘已知，无任何未来函数）
MINING_FEATURE_META: dict[str, dict[str, str]] = {
    "ret_1d_pct": {"label": "当日涨跌幅(%)", "group": "momentum"},
    "ret_3d_pct": {"label": "3日累计涨幅(%)", "group": "momentum"},
    "ret_5d_pct": {"label": "5日累计涨幅(%)", "group": "momentum"},
    "ret_10d_pct": {"label": "10日累计涨幅(%)", "group": "momentum"},
    "ret_20d_pct": {"label": "20日累计涨幅(%)", "group": "momentum"},
    "ret_60d_pct": {"label": "60日累计涨幅(%)", "group": "momentum"},
    "rs_5d_vs_hs300": {"label": "5日相对沪深300超额(%)", "group": "relative_strength"},
    "rs_20d_vs_hs300": {"label": "20日相对沪深300超额(%)", "group": "relative_strength"},
    "ma5_bias_pct": {"label": "收盘偏离MA5(%)", "group": "trend"},
    "ma10_bias_pct": {"label": "收盘偏离MA10(%)", "group": "trend"},
    "ma20_bias_pct": {"label": "收盘偏离MA20(%)", "group": "trend"},
    "ma60_bias_pct": {"label": "收盘偏离MA60(%)", "group": "trend"},
    "ma_bull_align": {"label": "均线多头排列(MA5>10>20)", "group": "trend"},
    "pos_20d": {"label": "20日价格区间位置(0~1)", "group": "position"},
    "pos_60d": {"label": "60日价格区间位置(0~1)", "group": "position"},
    "dist_to_hi20_pct": {"label": "距20日新高回撤(%)", "group": "position"},
    "vol_ratio_5d": {"label": "成交额/5日均额量比", "group": "volume"},
    "vol_ratio_20d": {"label": "成交额/20日均额量比", "group": "volume"},
    "amt_20d_yi": {"label": "20日均成交额(亿元)", "group": "liquidity"},
    "close_pos_in_day": {"label": "日内收盘实体位置(0~1)", "group": "candle"},
    "upper_shadow_pct": {"label": "上影线幅度(%)", "group": "candle"},
    "atr_14_pct": {"label": "14日振幅波动率(%)", "group": "volatility"},
    "industry_ret_5d_rank": {"label": "所属行业5日强弱分位(0~1)", "group": "sector"},
    "industry_above_ma20_pct": {"label": "所属行业站上MA20占比(%)", "group": "sector"},
    "market_above_ma20_pct": {"label": "全市场站上MA20宽度(%)", "group": "market"},
    "idx_mom20_pct": {"label": "上证20日动量(%)", "group": "market"},
    "idx_mom60_pct": {"label": "上证60日动量(%)", "group": "market"},
}


def _clickhouse_url() -> str:
    host = (
        os.environ.get("AISTOCK_CLICKHOUSE_HOST")
        or os.environ.get("CLICKHOUSE_HOST")
        or "127.0.0.1"
    ).strip() or "127.0.0.1"
    port = (
        os.environ.get("AISTOCK_CLICKHOUSE_PORT")
        or os.environ.get("CLICKHOUSE_HTTP_PORT")
        or "8123"
    ).strip() or "8123"
    return f"http://{host}:{port}/"


def _ch_query_df(sql: str, timeout: int = 180) -> pd.DataFrame:
    """只读执行 ClickHouse SQL，使用原生 Parquet 二进制格式高速返回 DataFrame（禁止写操作）。"""
    sql_clean = sql.strip()
    if not sql_clean.upper().startswith("SELECT") and not sql_clean.upper().startswith("WITH"):
        raise ValueError("feature_store 仅允许只读 SELECT/WITH 查询")
    payload = (sql_clean + "\nFORMAT Parquet").encode("utf-8")
    req = urllib.request.Request(_clickhouse_url(), data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return pd.DataFrame()
    return pq.read_table(io.BytesIO(raw)).to_pandas()


def _cache_dir() -> Path:
    d = runtime_path("mining_cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _classify_market_regime(idx_df: pd.DataFrame) -> pd.DataFrame:
    """按 R51 v2.1 口径（上证 60 日动量 + MA20 趋势 + 全市场宽度）标注每日大盘五态周期。"""
    df = idx_df.sort_values("trade_date").copy()
    close = df["idx_close"].astype(float)
    ma20 = close.rolling(20, min_periods=10).mean()
    ma60 = close.rolling(60, min_periods=20).mean()
    df["idx_mom20_pct"] = (close / close.shift(20) - 1.0) * 100.0
    df["idx_mom60_pct"] = (close / close.shift(60) - 1.0) * 100.0

    # 简单稳健的 N=3 趋势确认（与 market_regime_gate 保持逻辑一致）
    raw_up = (close > ma20) & (df["idx_mom60_pct"] > 0)
    raw_down = (close < ma20) & (df["idx_mom60_pct"] < -1.0)
    up_streak = raw_up.astype(int).rolling(3, min_periods=3).sum() == 3
    down_streak = raw_down.astype(int).rolling(3, min_periods=3).sum() == 3

    breadth = df["market_above_ma20_pct"].fillna(50.0)
    regimes: list[str] = []
    for u, d, b, m20 in zip(up_streak, down_streak, breadth, df["idx_mom20_pct"].fillna(0.0)):
        if d and b <= 20.0:
            regimes.append("extreme_panic")
        elif d or b < 30.0:
            regimes.append("defensive")
        elif u and b >= 75.0 and m20 >= 6.0:
            regimes.append("extreme_offensive")
        elif u and b >= 50.0:
            regimes.append("offensive")
        else:
            regimes.append("balanced")
    df["market_regime"] = regimes
    return df[["trade_date", "idx_close", "idx_mom20_pct", "idx_mom60_pct", "market_above_ma20_pct", "market_regime"]]


def _limit_up_threshold(code: str) -> float:
    c = str(code)
    if c.endswith(".BJ") or c.startswith(("8", "4", "92")):
        return 1.292
    if c.startswith(("300", "301", "688", "689")):
        return 1.192
    return 1.095


def _limit_down_threshold(code: str) -> float:
    c = str(code)
    if c.endswith(".BJ") or c.startswith(("8", "4", "92")):
        return 0.708
    if c.startswith(("300", "301", "688", "689")):
        return 0.808
    return 0.905


def build_or_load_feature_panel(
    *,
    start_date: str = "2023-01-01",
    end_date: Optional[str] = None,
    min_amt_20d: float = 50_000_000.0,
    min_list_days: int = 120,
    force_rebuild: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """构建或加载全市场流动性达标特征宽表（Parquet 缓存）与基准指数表。

    返回:
      (panel_df, bench_df)
      - panel_df: 每行是 (trade_date, code)，含全部 T-1 特征 + 未来 1..10 日开/高/低/收序列（供回测精确撮合）
      - bench_df: 沪深300（000300.SH）与上证（000001.SH）日线及五态周期
    """
    end_str = end_date or datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    cache_tag = f"panel_{start_date}_{end_str}_amt{int(min_amt_20d // 1_000_000)}m"
    panel_path = _cache_dir() / f"{cache_tag}.parquet"
    bench_path = _cache_dir() / f"bench_{start_date}_{end_str}.parquet"

    if not force_rebuild and panel_path.exists() and bench_path.exists():
        return pq.read_table(panel_path).to_pandas(), pq.read_table(bench_path).to_pandas()

    # 1. 拉取正常上市非 ST/退市 A 股主数据
    stocks_df = _ch_query_df(
        """
        SELECT code, name, coalesce(industry, '未分类') AS industry,
               toString(list_date) AS list_date
        FROM stock.stocks FINAL
        WHERE type = 'stock'
          AND st = 0
          AND quit = 0
          AND listing_status = 'active'
          AND positionCaseInsensitive(name, 'ST') = 0
          AND position(name, '退') = 0
        """
    )
    valid_codes = set(stocks_df["code"].astype(str))
    code_to_ind = {
        str(c): str(ind).strip()
        for c, ind in zip(stocks_df["code"], stocks_df["industry"])
        if str(ind or "").strip() and str(ind or "").strip() != "未分类"
    }
    # 优先从 stock.sector_stocks + stock.sectors (type='industry') 补齐真实 145 个行业归属
    try:
        sec_map_df = _ch_query_df(
            """
            SELECT ss.stock_code AS code, any(s.name) AS ind_name
            FROM stock.sector_stocks ss
            INNER JOIN stock.sectors s ON ss.sector_code = s.code
            WHERE s.type = 'industry' AND length(trim(s.name)) > 0
            GROUP BY ss.stock_code
            """
        )
        for c, ind in zip(sec_map_df["code"].astype(str), sec_map_df["ind_name"].astype(str)):
            if c not in code_to_ind or not code_to_ind[c]:
                code_to_ind[c] = ind.strip()
    except Exception:
        pass
    code_to_name = dict(zip(stocks_df["code"].astype(str), stocks_df["name"].astype(str)))

    # 2. 往前多取 90 天用于计算 MA60 / 60日动量热身，避免窗口期前段 NaN
    warmup_dt = (date.fromisoformat(start_date) - timedelta(days=140)).isoformat()

    # 拉取基准指数（上证 000001.SH + 沪深300 000300.SH）
    idx_raw = _ch_query_df(
        f"""
        SELECT code, toString(trade_date) AS trade_date, open, close
        FROM stock.kline_daily FINAL
        WHERE code IN ('000001.SH', '000300.SH')
          AND trade_date >= '{warmup_dt}'
          AND trade_date <= '{end_str}'
        ORDER BY trade_date
        """
    )
    sh_df = idx_raw[idx_raw["code"] == "000001.SH"][["trade_date", "close"]].rename(columns={"close": "idx_close"})
    hs_df = idx_raw[idx_raw["code"] == "000300.SH"][["trade_date", "open", "close"]].rename(
        columns={"open": "hs300_open", "close": "hs300_close"}
    )
    hs_df = hs_df.sort_values("trade_date").copy()
    hs_df["hs300_ret_5d"] = (hs_df["hs300_close"] / hs_df["hs300_close"].shift(5) - 1.0) * 100.0
    hs_df["hs300_ret_20d"] = (hs_df["hs300_close"] / hs_df["hs300_close"].shift(20) - 1.0) * 100.0
    hs_df["hs300_next_open"] = hs_df["hs300_open"].shift(-1)
    hs_df["hs300_fwd_ret_5d"] = (hs_df["hs300_close"].shift(-5) / hs_df["hs300_next_open"] - 1.0) * 100.0

    # 3. 拉取全市场 A 股日线（排除北交所以降低微盘噪声干扰，保留沪深主板+创业板+科创板）
    kline_df = _ch_query_df(
        f"""
        SELECT code, toString(trade_date) AS trade_date,
               toFloat32(open) AS open,
               toFloat32(high) AS high,
               toFloat32(low) AS low,
               toFloat32(close) AS close,
               toFloat64(amount) AS amount
        FROM stock.kline_daily FINAL
        WHERE trade_date >= '{warmup_dt}'
          AND trade_date <= '{end_str}'
          AND (endsWith(code, '.SH') OR endsWith(code, '.SZ'))
          AND close > 0 AND open > 0 AND amount > 0
        ORDER BY code, trade_date
        """,
        timeout=180,
    )
    kline_df = kline_df[kline_df["code"].isin(valid_codes)].copy()
    kline_df.sort_values(["code", "trade_date"], inplace=True)
    kline_df.reset_index(drop=True, inplace=True)

    # 4. 逐股向量化计算严格 T-1 特征与未来撮合价格
    g = kline_df.groupby("code", sort=False)

    kline_df["prev_close"] = g["close"].shift(1)
    kline_df["bar_idx"] = g.cumcount()

    # 涨跌幅与动量
    kline_df["ret_1d_pct"] = (kline_df["close"] / kline_df["prev_close"] - 1.0) * 100.0
    kline_df["ret_3d_pct"] = (kline_df["close"] / g["close"].shift(3) - 1.0) * 100.0
    kline_df["ret_5d_pct"] = (kline_df["close"] / g["close"].shift(5) - 1.0) * 100.0
    kline_df["ret_10d_pct"] = (kline_df["close"] / g["close"].shift(10) - 1.0) * 100.0
    kline_df["ret_20d_pct"] = (kline_df["close"] / g["close"].shift(20) - 1.0) * 100.0
    kline_df["ret_60d_pct"] = (kline_df["close"] / g["close"].shift(60) - 1.0) * 100.0

    # 均线与乖离率
    ma5 = g["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    ma10 = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    ma20 = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    ma60 = g["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())

    kline_df["ma5_bias_pct"] = (kline_df["close"] / ma5 - 1.0) * 100.0
    kline_df["ma10_bias_pct"] = (kline_df["close"] / ma10 - 1.0) * 100.0
    kline_df["ma20_bias_pct"] = (kline_df["close"] / ma20 - 1.0) * 100.0
    kline_df["ma60_bias_pct"] = (kline_df["close"] / ma60 - 1.0) * 100.0
    kline_df["ma_bull_align"] = ((ma5 > ma10) & (ma10 > ma20)).astype(np.float32)
    kline_df["above_ma20"] = (kline_df["close"] > ma20).astype(np.float32)

    # 区间位置与新高回撤
    hi20 = g["high"].transform(lambda s: s.rolling(20, min_periods=20).max())
    lo20 = g["low"].transform(lambda s: s.rolling(20, min_periods=20).min())
    hi60 = g["high"].transform(lambda s: s.rolling(60, min_periods=60).max())
    lo60 = g["low"].transform(lambda s: s.rolling(60, min_periods=60).min())
    kline_df["pos_20d"] = (kline_df["close"] - lo20) / (hi20 - lo20 + 1e-6)
    kline_df["pos_60d"] = (kline_df["close"] - lo60) / (hi60 - lo60 + 1e-6)
    kline_df["dist_to_hi20_pct"] = (kline_df["close"] / (hi20 + 1e-6) - 1.0) * 100.0

    # 量能与流动性
    amt5 = g["amount"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    amt20 = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    kline_df["vol_ratio_5d"] = kline_df["amount"] / (amt5 + 1.0)
    kline_df["vol_ratio_20d"] = kline_df["amount"] / (amt20 + 1.0)
    kline_df["amt_20d_mean"] = amt20
    kline_df["amt_20d_yi"] = amt20 / 1e8

    # K线形态与波动率
    hl_span = (kline_df["high"] - kline_df["low"]).clip(lower=1e-4)
    kline_df["close_pos_in_day"] = (kline_df["close"] - kline_df["low"]) / hl_span
    body_top = np.maximum(kline_df["open"], kline_df["close"])
    kline_df["upper_shadow_pct"] = (kline_df["high"] - body_top) / (kline_df["close"] + 1e-6) * 100.0
    amp_day = (kline_df["high"] - kline_df["low"]) / (kline_df["prev_close"] + 1e-6) * 100.0
    kline_df["atr_14_pct"] = amp_day.groupby(kline_df["code"], sort=False).transform(
        lambda s: s.rolling(14, min_periods=10).mean()
    )

    # 5. 未来 1~10 日撮合价格（严格用于回测执行，严禁进入挖掘特征！）
    kline_df["next_open"] = g["open"].shift(-1)
    kline_df["next_high"] = g["high"].shift(-1)
    kline_df["next_low"] = g["low"].shift(-1)
    kline_df["next_close"] = g["close"].shift(-1)
    for d in range(2, 11):
        kline_df[f"fwd_open_{d}"] = g["open"].shift(-d)
        kline_df[f"fwd_high_{d}"] = g["high"].shift(-d)
        kline_df[f"fwd_low_{d}"] = g["low"].shift(-d)
        kline_df[f"fwd_close_{d}"] = g["close"].shift(-d)

    # T+1 一字涨停买不进过滤（用 T 日收盘与 T+1 开盘比较）
    lu_thresh = kline_df["code"].map(_limit_up_threshold).astype(np.float32)
    ld_thresh = kline_df["code"].map(_limit_down_threshold).astype(np.float32)
    kline_df["entry_tradable"] = (
        kline_df["next_open"].notna()
        & (kline_df["next_open"] < kline_df["close"] * lu_thresh)
    )
    kline_df["ld_thresh"] = ld_thresh

    # 6. 全市场截面宽度与行业板块截面强弱（严格 T 日收盘截面）
    kline_df["industry"] = kline_df["code"].map(code_to_ind).fillna("未分类")
    kline_df["name"] = kline_df["code"].map(code_to_name).fillna(kline_df["code"])

    breadth_daily = (
        kline_df.groupby("trade_date", as_index=False)["above_ma20"]
        .mean()
        .rename(columns={"above_ma20": "market_above_ma20_pct"})
    )
    breadth_daily["market_above_ma20_pct"] = breadth_daily["market_above_ma20_pct"] * 100.0

    ind_daily = (
        kline_df.groupby(["trade_date", "industry"], as_index=False)
        .agg(
            ind_ret_5d=("ret_5d_pct", "mean"),
            industry_above_ma20_pct=("above_ma20", "mean"),
        )
    )
    ind_daily["industry_above_ma20_pct"] = ind_daily["industry_above_ma20_pct"] * 100.0
    ind_daily["industry_ret_5d_rank"] = ind_daily.groupby("trade_date")["ind_ret_5d"].rank(pct=True)

    # 合并大盘五态周期与沪深300相对强度
    bench_df = sh_df.merge(hs_df, on="trade_date", how="inner").merge(breadth_daily, on="trade_date", how="left")
    regime_df = _classify_market_regime(bench_df)
    bench_df = bench_df.merge(
        regime_df[["trade_date", "idx_mom20_pct", "idx_mom60_pct", "market_regime"]],
        on="trade_date",
        how="left",
    )

    kline_df = kline_df.merge(
        bench_df[
            [
                "trade_date",
                "hs300_ret_5d",
                "hs300_ret_20d",
                "hs300_fwd_ret_5d",
                "market_above_ma20_pct",
                "idx_mom20_pct",
                "idx_mom60_pct",
                "market_regime",
            ]
        ],
        on="trade_date",
        how="inner",
    )
    kline_df = kline_df.merge(
        ind_daily[["trade_date", "industry", "industry_ret_5d_rank", "industry_above_ma20_pct"]],
        on=["trade_date", "industry"],
        how="left",
    )

    kline_df["rs_5d_vs_hs300"] = kline_df["ret_5d_pct"] - kline_df["hs300_ret_5d"]
    kline_df["rs_20d_vs_hs300"] = kline_df["ret_20d_pct"] - kline_df["hs300_ret_20d"]

    # 7. 应用目标时间段 + 全市场流动性达标门禁（20日均成交额 >= 5000万）+ 上市满 120 日
    mask_keep = (
        (kline_df["trade_date"] >= start_date)
        & (kline_df["trade_date"] <= end_str)
        & (kline_df["amt_20d_mean"] >= float(min_amt_20d))
        & (kline_df["bar_idx"] >= int(min_list_days) - 60)  # 热身期已含近100交易日，再叠加 list_date 校验
    )
    # 严格按 list_date 剔除上市不满 min_list_days 天的次新股
    list_date_map = dict(zip(stocks_df["code"].astype(str), stocks_df["list_date"].astype(str)))
    ld_series = pd.to_datetime(kline_df["code"].map(list_date_map), errors="coerce")
    td_series = pd.to_datetime(kline_df["trade_date"], errors="coerce")
    age_days = (td_series - ld_series).dt.days
    mask_keep &= age_days.isna() | (age_days >= int(min_list_days))

    panel_df = kline_df.loc[mask_keep].copy()
    # 压缩浮点列为 float32 以节省内存并加速向量化
    float_cols = [c for c in panel_df.columns if panel_df[c].dtype == np.float64 and c != "amt_20d_mean"]
    for c in float_cols:
        panel_df[c] = panel_df[c].astype(np.float32)

    bench_out = bench_df[(bench_df["trade_date"] >= start_date) & (bench_df["trade_date"] <= end_str)].copy()
    bench_out.reset_index(drop=True, inplace=True)
    panel_df.reset_index(drop=True, inplace=True)

    pq.write_table(pa.Table.from_pandas(panel_df, preserve_index=False), panel_path)
    pq.write_table(pa.Table.from_pandas(bench_out, preserve_index=False), bench_path)
    return panel_df, bench_out
