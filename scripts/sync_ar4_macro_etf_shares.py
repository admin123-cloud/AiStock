# -*- coding: utf-8 -*-
"""AR4 宏观资金面与国家队 ETF 份额独立表同步脚本（2021-01-01 至今，全量 + 增量幂等）。

严格遵守数据治理铁律：
1. 仅写入 2 张隔离独立研究表：
   - stock.ar4_etf_share_daily（上交所官方核心宽基/国家队/跨资产 ETF 每日份额与规模）
   - stock.ar4_macro_daily（国家队持仓代理 + 股债性价比 ERP + 沪深两融 + 全市场拥挤度）
   绝不写入或修改 stock.kline_daily。
2. 全部采用 ReplacingMergeTree，支持任意时间幂等重跑与日常增量补齐。
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

# 防止受系统代理影响直连国内官方交易所接口
os.environ["NO_PROXY"] = "*"

import akshare as ak
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

BJ_TZ = timezone(timedelta(hours=8))

# 国家队核心宽基 + AR3 跨资产核心上交所 ETF（全为上交所官方日频份额覆盖标的）
WATCH_SSE_ETFS: dict[str, tuple[str, str]] = {
    "510300": ("华泰柏瑞沪深300ETF", "hs300_core"),
    "510310": ("易方达沪深300ETF", "hs300_core"),
    "510330": ("华夏沪深300ETF", "hs300_core"),
    "510050": ("华夏上证50ETF", "sz50_core"),
    "510500": ("南方中证500ETF", "zz500_core"),
    "512100": ("南方中证1000ETF", "zz1000_core"),
    "588000": ("华夏科创50ETF", "kc50_core"),
    "510880": ("华泰柏瑞红利ETF", "dividend"),
    "512890": ("华泰柏瑞红利低波ETF", "dividend"),
    "518880": ("华安黄金ETF", "gold"),
    "513100": ("国泰纳指ETF", "overseas"),
    "513500": ("博时标普500ETF", "overseas"),
    "513180": ("华夏恒生科技ETF", "overseas"),
    "511260": ("国泰十年国债ETF", "bond"),
    "511090": ("鹏扬30年国债ETF", "bond"),
}

DDL_ETF_SHARE = """
CREATE TABLE IF NOT EXISTS stock.ar4_etf_share_daily (
    code String,
    name String,
    bucket String,
    trade_date Date,
    shares_wan Float64,
    close Float64,
    scale_yi Float64,
    updated_at DateTime('Asia/Shanghai')
) ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYear(trade_date)
ORDER BY (code, trade_date)
"""

DDL_MACRO = """
CREATE TABLE IF NOT EXISTS stock.ar4_macro_daily (
    trade_date Date,
    hs300_close Float64,
    hs300_pe_ttm Float64,
    cn_bond_10y Float64,
    cn_bond_2y Float64,
    us_bond_10y Float64,
    erp_pct Float64,
    margin_balance_yi Float64,
    hs300_top3_shares_yi Float64,
    hs300_top3_scale_yi Float64,
    broad6_shares_yi Float64,
    broad6_scale_yi Float64,
    market_total_amt_yi Float64,
    top5pct_amt_share Float64,
    nt_ratio_proxy Float64,
    nt_ratio_pctile_250d Float64,
    nt_share_chg_5d_pct Float64,
    nt_share_chg_20d_pct Float64,
    broad6_net_inflow_5d_yi Float64,
    updated_at DateTime('Asia/Shanghai')
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (trade_date)
"""


def _ch_exec(sql: str, body: bytes | None = None, timeout: int = 60) -> str:
    host = (os.environ.get("AISTOCK_CLICKHOUSE_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("AISTOCK_CLICKHOUSE_PORT") or "8123").strip()
    if body is None:
        req = urllib.request.Request(f"http://{host}:{port}/", data=sql.encode("utf-8"), method="POST")
    else:
        q = urllib.parse.urlencode({"query": sql})
        req = urllib.request.Request(f"http://{host}:{port}/?{q}", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8").strip()


def _ch_query_df(sql: str, timeout: int = 60) -> pd.DataFrame:
    host = (os.environ.get("AISTOCK_CLICKHOUSE_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("AISTOCK_CLICKHOUSE_PORT") or "8123").strip()
    req = urllib.request.Request(
        f"http://{host}:{port}/",
        data=(sql.strip() + "\nFORMAT Parquet").encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return pq.read_table(io.BytesIO(r.read())).to_pandas()


def _fetch_sse_etf_shares_one_day(trade_date: str) -> list[dict[str, Any]]:
    url = (
        "https://query.sse.com.cn/commonQuery.do?"
        f"sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE={trade_date}&isPagination=false"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.sse.com.cn/",
    }
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
            obj = json.loads(raw)
            rows = obj.get("result") or (obj.get("pageHelp") or {}).get("data") or []
            out: list[dict[str, Any]] = []
            for r in rows:
                code = str(r.get("SEC_CODE") or "").strip()
                if code not in WATCH_SSE_ETFS:
                    continue
                stat_dt = str(r.get("STAT_DATE") or trade_date).strip()[:10]
                if stat_dt != trade_date:
                    continue
                vol_str = str(r.get("TOT_VOL") or "").replace(",", "").strip()
                if not vol_str:
                    continue
                shares_wan = float(vol_str)
                if shares_wan <= 0:
                    continue
                name, bucket = WATCH_SSE_ETFS[code]
                out.append({
                    "code": f"{code}.SH",
                    "name": name,
                    "bucket": bucket,
                    "trade_date": trade_date,
                    "shares_wan": shares_wan,
                })
            return out
        except Exception:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return []


def _fetch_etf_close_history(code6: str, start_ymd: str = "20210101", end_ymd: str = "20261231") -> pd.DataFrame:
    """获取 ETF 不复权收盘价（优先新浪源零限流，失败回退东方财富；计算真实规模不可用前复权！）。"""
    # 1. 优先新浪 ETF 日线（不复权原价、稳定无限流）
    for attempt in range(3):
        try:
            df = ak.fund_etf_hist_sina(symbol=f"sh{code6}")
            if df is not None and not df.empty:
                dts = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                out = pd.DataFrame({
                    "code": f"{code6}.SH",
                    "trade_date": dts,
                    "close": pd.to_numeric(df["close"], errors="coerce"),
                }).dropna()
                s_dash = f"{start_ymd[:4]}-{start_ymd[4:6]}-{start_ymd[6:8]}"
                e_dash = f"{end_ymd[:4]}-{end_ymd[4:6]}-{end_ymd[6:8]}"
                out = out[(out["trade_date"] >= s_dash) & (out["trade_date"] <= e_dash)]
                if not out.empty:
                    return out
        except Exception:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))

    # 2. 回退东方财富
    for attempt in range(2):
        try:
            df = ak.fund_etf_hist_em(
                symbol=code6, period="daily", start_date=start_ymd, end_date=end_ymd, adjust=""
            )
            if df is not None and not df.empty:
                out = pd.DataFrame({
                    "code": f"{code6}.SH",
                    "trade_date": pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"),
                    "close": pd.to_numeric(df["收盘"], errors="coerce"),
                }).dropna()
                return out
        except Exception:
            time.sleep(0.8)
    return pd.DataFrame(columns=["code", "trade_date", "close"])


def sync_all(start_date: str = "2021-01-01") -> dict[str, Any]:
    t0 = time.time()
    _ch_exec(DDL_ETF_SHARE)
    _ch_exec(DDL_MACRO)

    # 1. 从本地 ClickHouse kline_daily 获取全部真实交易日（与回测基准 100% 对齐）
    cal_df = _ch_query_df(
        f"""
        SELECT DISTINCT toString(trade_date) AS dt
        FROM stock.kline_daily
        WHERE trade_date >= '{start_date}' AND trade_date <= '2026-11-22'
        ORDER BY dt
        """
    )
    all_dates = cal_df["dt"].tolist()

    # 查已入库日期实现增量跳过
    existing_df = _ch_query_df(
        "SELECT DISTINCT toString(trade_date) AS dt FROM stock.ar4_etf_share_daily FINAL"
    )
    existing_dates = set(existing_df["dt"].tolist()) if not existing_df.empty else set()
    missing_dates = [d for d in all_dates if d not in existing_dates]
    print(f"[AR4-Sync] 总交易日={len(all_dates)}，已入库={len(existing_dates)}，待拉取上交所份额={len(missing_dates)}")

    # 2. 并发拉取缺失交易日的上交所官方 ETF 每日份额
    new_share_rows: list[dict[str, Any]] = []
    if missing_dates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_sse_etf_shares_one_day, d): d for d in missing_dates}
            done_cnt = 0
            for fut in concurrent.futures.as_completed(futs):
                done_cnt += 1
                rows = fut.result()
                if rows:
                    new_share_rows.extend(rows)
                if done_cnt % 250 == 0 or done_cnt == len(missing_dates):
                    print(f"[AR4-Sync] 上交所份额进度 {done_cnt}/{len(missing_dates)}，累计行={len(new_share_rows)}")

    # 3. 检查是否还有 close <= 0 的历史行需要补齐收盘价与规模（幂等自愈）
    zero_px_df = _ch_query_df(
        "SELECT code, name, bucket, toString(trade_date) AS trade_date, shares_wan "
        "FROM stock.ar4_etf_share_daily FINAL WHERE close <= 0"
    )
    if not zero_px_df.empty:
        new_share_rows.extend(zero_px_df.to_dict(orient="records"))

    if new_share_rows:
        print(f"[AR4-Sync] 拉取 15 只核心 ETF 不复权日收盘价（待补全 {len(new_share_rows)} 行）...")
        price_frames = []
        for code6 in WATCH_SSE_ETFS:
            pf = _fetch_etf_close_history(code6, start_ymd=start_date.replace("-", ""))
            if not pf.empty:
                price_frames.append(pf)
        price_all = pd.concat(price_frames, ignore_index=True) if price_frames else pd.DataFrame(columns=["code", "trade_date", "close"])
        px_map = dict(zip(zip(price_all["code"], price_all["trade_date"]), price_all["close"]))

        # 对 510310 / 510330 等若个别日收盘缺口，按最近前值填充
        price_piv = price_all.pivot(index="trade_date", columns="code", values="close").sort_index().ffill()

        now_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
        insert_lines = []
        for r in new_share_rows:
            c = r["code"]
            dt = r["trade_date"]
            px = px_map.get((c, dt))
            if px is None or pd.isna(px):
                if c in price_piv.columns and dt in price_piv.index and pd.notna(price_piv.loc[dt, c]):
                    px = float(price_piv.loc[dt, c])
                else:
                    px = 0.0
            scale_yi = round(r["shares_wan"] * float(px) / 10000.0, 4) if px > 0 else 0.0
            r["close"] = round(float(px), 4)
            r["scale_yi"] = scale_yi
            r["updated_at"] = now_str
            insert_lines.append(json.dumps(r, ensure_ascii=False))

        for i in range(0, len(insert_lines), 2000):
            batch = "\n".join(insert_lines[i:i + 2000]).encode("utf-8")
            _ch_exec("INSERT INTO stock.ar4_etf_share_daily FORMAT JSONEachRow", body=batch)
        print(f"[AR4-Sync] ar4_etf_share_daily 写入 {len(insert_lines)} 行完成")

    # 4. 读取完整 ar4_etf_share_daily 计算国家队核心日频序列
    share_df = _ch_query_df(
        f"""
        SELECT code, bucket, toString(trade_date) AS dt, shares_wan, close, scale_yi
        FROM stock.ar4_etf_share_daily FINAL
        WHERE trade_date >= '{start_date}'
        ORDER BY trade_date, code
        """
    )

    # 5. 拉取国债收益率 + 沪深300 PE + 两融余额 + 全市场成交额拥挤度
    print("[AR4-Sync] 拉取中美国债收益率、沪深300 PE、沪深两融余额与全市场成交额拥挤度...")
    bond_df = ak.bond_zh_us_rate(start_date=start_date.replace("-", ""))
    bond_df = pd.DataFrame({
        "dt": pd.to_datetime(bond_df["日期"]).dt.strftime("%Y-%m-%d"),
        "cn_bond_10y": pd.to_numeric(bond_df["中国国债收益率10年"], errors="coerce"),
        "cn_bond_2y": pd.to_numeric(bond_df["中国国债收益率2年"], errors="coerce"),
        "us_bond_10y": pd.to_numeric(bond_df["美国国债收益率10年"], errors="coerce"),
    }).dropna(subset=["dt"]).drop_duplicates("dt", keep="last").set_index("dt").sort_index()

    pe_df = ak.stock_index_pe_lg(symbol="沪深300")
    pe_df = pd.DataFrame({
        "dt": pd.to_datetime(pe_df["日期"]).dt.strftime("%Y-%m-%d"),
        "hs300_close_lg": pd.to_numeric(pe_df["指数"], errors="coerce"),
        "hs300_pe_ttm": pd.to_numeric(pe_df["滚动市盈率"], errors="coerce"),
    }).dropna(subset=["dt"]).drop_duplicates("dt", keep="last").set_index("dt").sort_index()

    # 沪深300 日收盘补齐（优先新浪源，失败回退东方财富/本地 510300 代理）
    hs300_px = pd.DataFrame(columns=["hs300_close"])
    for fn_get in (
        lambda: ak.stock_zh_index_daily(symbol="sh000300"),
        lambda: ak.stock_zh_index_daily_em(symbol="sh000300"),
    ):
        for attempt in range(3):
            try:
                tmp = fn_get()
                if tmp is not None and not tmp.empty:
                    hs300_px = pd.DataFrame({
                        "dt": pd.to_datetime(tmp["date"]).dt.strftime("%Y-%m-%d"),
                        "hs300_close": pd.to_numeric(tmp["close"], errors="coerce"),
                    }).dropna().drop_duplicates("dt", keep="last").set_index("dt").sort_index()
                    break
            except Exception:
                time.sleep(0.8 * (attempt + 1))
        if not hs300_px.empty:
            break
    if hs300_px.empty:
        hs300_px = pe_df[["hs300_close_lg"]].rename(columns={"hs300_close_lg": "hs300_close"})

    # 两融余额（沪 + 深 + 本地 market_margin_sentiment 最新补齐）
    m_sh = ak.macro_china_market_margin_sh()
    m_sz = ak.macro_china_market_margin_sz()
    m_sh_s = pd.Series(
        pd.to_numeric(m_sh["融资余额"], errors="coerce").values + pd.to_numeric(m_sh["融券余额"], errors="coerce").values,
        index=pd.to_datetime(m_sh["日期"]).dt.strftime("%Y-%m-%d"),
    )
    m_sz_s = pd.Series(
        pd.to_numeric(m_sz["融资余额"], errors="coerce").values + pd.to_numeric(m_sz["融券余额"], errors="coerce").values,
        index=pd.to_datetime(m_sz["日期"]).dt.strftime("%Y-%m-%d"),
    )
    margin_yi = (m_sh_s.add(m_sz_s, fill_value=np.nan) / 1e8).dropna()
    # 合并本地最新两融
    loc_m = _ch_query_df("SELECT toString(trade_date) AS dt, margin_balance FROM stock.market_margin_sentiment FINAL")
    if not loc_m.empty:
        for _, row in loc_m.iterrows():
            val = float(row["margin_balance"] or 0.0)
            if val > 1e8:
                val = val / 1e8
            if val > 1000:
                margin_yi.loc[str(row["dt"])] = val
    margin_yi = margin_yi.sort_index()

    # 全市场成交额与前 5% 抱团拥挤度（只读聚合 stock.kline_daily）
    crowd_df = _ch_query_df(
        f"""
        WITH ranked AS (
            SELECT
                toString(trade_date) AS dt,
                amount,
                row_number() OVER (PARTITION BY trade_date ORDER BY amount DESC) AS rn,
                count() OVER (PARTITION BY trade_date) AS cnt
            FROM stock.kline_daily FINAL
            WHERE trade_date >= '{start_date}' AND amount > 0
        )
        SELECT
            dt,
            sum(amount) / 1e8 AS market_total_amt_yi,
            sumIf(amount, rn <= greatest(1, toInt64(cnt * 0.05))) / nullIf(sum(amount), 0) AS top5pct_amt_share
        FROM ranked
        GROUP BY dt
        ORDER BY dt
        """
    ).set_index("dt")

    # 聚合每日国家队指标
    hs300_sub = share_df[share_df["bucket"] == "hs300_core"]
    broad6_sub = share_df[share_df["bucket"].isin(["hs300_core", "sz50_core", "zz500_core", "zz1000_core"])]

    hs300_agg = hs300_sub.groupby("dt").agg(
        hs300_top3_shares_yi=("shares_wan", lambda s: float(s.sum()) / 10000.0),
        hs300_top3_scale_yi=("scale_yi", "sum"),
    )
    broad6_agg = broad6_sub.groupby("dt").agg(
        broad6_shares_yi=("shares_wan", lambda s: float(s.sum()) / 10000.0),
        broad6_scale_yi=("scale_yi", "sum"),
    )

    # 逐只计算每日净申购资金 = Δ份额(万份) * 当日收盘价 / 10000 (亿元)，避免把指数涨跌误算成申购！
    piv_sh = broad6_sub.pivot(index="dt", columns="code", values="shares_wan").sort_index().ffill()
    piv_px = broad6_sub.pivot(index="dt", columns="code", values="close").sort_index().ffill()
    daily_net_inflow_yi = (piv_sh.diff() * piv_px / 10000.0).sum(axis=1)
    broad6_net_inflow_5d_yi = daily_net_inflow_yi.rolling(5, min_periods=1).sum()

    # 拼装完整交易日面板
    panel = pd.DataFrame(index=all_dates)
    panel.index.name = "dt"
    panel = panel.join(hs300_px[["hs300_close"]], how="left")
    panel = panel.join(pe_df[["hs300_pe_ttm"]], how="left")
    panel = panel.join(bond_df[["cn_bond_10y", "cn_bond_2y", "us_bond_10y"]], how="left")
    panel["margin_balance_yi"] = margin_yi.reindex(panel.index)
    panel = panel.join(hs300_agg, how="left")
    panel = panel.join(broad6_agg, how="left")
    panel = panel.join(crowd_df, how="left")
    panel["broad6_net_inflow_5d_yi"] = broad6_net_inflow_5d_yi.reindex(panel.index)

    # 前向填充低频/节假日字段（PE_TTM、国债收益率、两融、份额）
    for col in ("hs300_close", "hs300_pe_ttm", "cn_bond_10y", "cn_bond_2y", "us_bond_10y",
                "margin_balance_yi", "hs300_top3_shares_yi", "hs300_top3_scale_yi",
                "broad6_shares_yi", "broad6_scale_yi"):
        panel[col] = panel[col].ffill()

    # 股债性价比 ERP (%) = 100 / PE_TTM - 10年期国债收益率(%)
    panel["erp_pct"] = (100.0 / panel["hs300_pe_ttm"]) - panel["cn_bond_10y"]

    # 国家队持仓比例代理：由于沪深300流通市值与沪深300指数点位高度正比，
    # 头部300ETF规模 / 沪深300指数点位 等价于 头部300ETF规模 / 沪深300流通市值（精确到常数比例因子），
    # 因而它的「250日滚动分位」与原版「头部300ETF规模 / 沪深300流通市值」250日分位数学上几乎完全一致！
    # 同时我们额外保留纯份额 5日 / 20日增速（完全剥离指数涨跌）。
    panel["nt_ratio_proxy"] = panel["hs300_top3_scale_yi"] / panel["hs300_close"].replace(0, np.nan) * 100.0
    panel["nt_ratio_pctile_250d"] = (
        panel["nt_ratio_proxy"]
        .rolling(250, min_periods=60)
        .apply(lambda w: float(np.mean(w <= w.iloc[-1]) * 100.0), raw=False)
    )
    panel["nt_share_chg_5d_pct"] = panel["hs300_top3_shares_yi"].pct_change(5) * 100.0
    panel["nt_share_chg_20d_pct"] = panel["hs300_top3_shares_yi"].pct_change(20) * 100.0

    # 写入 stock.ar4_macro_daily
    now_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    macro_lines = []
    for dt, row in panel.iterrows():
        rec = {
            "trade_date": str(dt),
            "hs300_close": round(float(row["hs300_close"]), 4) if pd.notna(row["hs300_close"]) else 0.0,
            "hs300_pe_ttm": round(float(row["hs300_pe_ttm"]), 4) if pd.notna(row["hs300_pe_ttm"]) else 0.0,
            "cn_bond_10y": round(float(row["cn_bond_10y"]), 4) if pd.notna(row["cn_bond_10y"]) else 0.0,
            "cn_bond_2y": round(float(row["cn_bond_2y"]), 4) if pd.notna(row["cn_bond_2y"]) else 0.0,
            "us_bond_10y": round(float(row["us_bond_10y"]), 4) if pd.notna(row["us_bond_10y"]) else 0.0,
            "erp_pct": round(float(row["erp_pct"]), 4) if pd.notna(row["erp_pct"]) else 0.0,
            "margin_balance_yi": round(float(row["margin_balance_yi"]), 2) if pd.notna(row["margin_balance_yi"]) else 0.0,
            "hs300_top3_shares_yi": round(float(row["hs300_top3_shares_yi"]), 4) if pd.notna(row["hs300_top3_shares_yi"]) else 0.0,
            "hs300_top3_scale_yi": round(float(row["hs300_top3_scale_yi"]), 2) if pd.notna(row["hs300_top3_scale_yi"]) else 0.0,
            "broad6_shares_yi": round(float(row["broad6_shares_yi"]), 4) if pd.notna(row["broad6_shares_yi"]) else 0.0,
            "broad6_scale_yi": round(float(row["broad6_scale_yi"]), 2) if pd.notna(row["broad6_scale_yi"]) else 0.0,
            "market_total_amt_yi": round(float(row["market_total_amt_yi"]), 2) if pd.notna(row["market_total_amt_yi"]) else 0.0,
            "top5pct_amt_share": round(float(row["top5pct_amt_share"]), 4) if pd.notna(row["top5pct_amt_share"]) else 0.0,
            "nt_ratio_proxy": round(float(row["nt_ratio_proxy"]), 4) if pd.notna(row["nt_ratio_proxy"]) else 0.0,
            "nt_ratio_pctile_250d": round(float(row["nt_ratio_pctile_250d"]), 2) if pd.notna(row["nt_ratio_pctile_250d"]) else 50.0,
            "nt_share_chg_5d_pct": round(float(row["nt_share_chg_5d_pct"]), 3) if pd.notna(row["nt_share_chg_5d_pct"]) else 0.0,
            "nt_share_chg_20d_pct": round(float(row["nt_share_chg_20d_pct"]), 3) if pd.notna(row["nt_share_chg_20d_pct"]) else 0.0,
            "broad6_net_inflow_5d_yi": round(float(row["broad6_net_inflow_5d_yi"]), 2) if pd.notna(row["broad6_net_inflow_5d_yi"]) else 0.0,
            "updated_at": now_str,
        }
        macro_lines.append(json.dumps(rec, ensure_ascii=False))

    _ch_exec("INSERT INTO stock.ar4_macro_daily FORMAT JSONEachRow", body="\n".join(macro_lines).encode("utf-8"))
    elapsed = round(time.time() - t0, 1)
    print(f"[AR4-Sync] 全部完成！ar4_macro_daily 写入 {len(macro_lines)} 天，耗时 {elapsed}s")
    print("[AR4-Sync] 最新 5 日宏观 & 国家队切片：")
    cols_show = ["hs300_close", "erp_pct", "hs300_top3_scale_yi", "nt_ratio_pctile_250d",
                 "nt_share_chg_5d_pct", "broad6_net_inflow_5d_yi", "margin_balance_yi", "top5pct_amt_share"]
    print(panel[cols_show].tail(5).to_string())
    return {"macro_days": len(macro_lines), "elapsed_sec": elapsed}


if __name__ == "__main__":
    sync_all()
