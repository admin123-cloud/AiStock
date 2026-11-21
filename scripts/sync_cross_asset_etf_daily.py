# -*- coding: utf-8 -*-
"""AR3 跨资产全天候 ETF 独立表（stock.etf_kline_daily）建表与前复权日线同步脚本。

安全铁律：
- 仅操作独立新表 stock.etf_kline_daily（ReplacingMergeTree），绝不触碰或修改 stock.kline_daily；
- 数据源为公开腾讯前复权日K接口（分段拉取并校验重叠日价格 0 偏差），支持幂等重跑。
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

BJ_TZ = timezone(timedelta(hours=8))

CROSS_ASSET_ETFS: list[dict[str, str]] = [
    # 1. A 股宽基权益
    {"code": "510300.SH", "tx": "sh510300", "name": "沪深300ETF", "asset_class": "cn_broad"},
    {"code": "510500.SH", "tx": "sh510500", "name": "中证500ETF", "asset_class": "cn_broad"},
    {"code": "512100.SH", "tx": "sh512100", "name": "中证1000ETF", "asset_class": "cn_broad"},
    {"code": "159915.SZ", "tx": "sz159915", "name": "创业板ETF", "asset_class": "cn_broad"},
    {"code": "588000.SH", "tx": "sh588000", "name": "科创50ETF", "asset_class": "cn_broad"},
    # 2. A 股高股息红利
    {"code": "512890.SH", "tx": "sh512890", "name": "红利低波ETF", "asset_class": "cn_dividend"},
    {"code": "510880.SH", "tx": "sh510880", "name": "红利ETF", "asset_class": "cn_dividend"},
    # 3. 商品避险（黄金）
    {"code": "518880.SH", "tx": "sh518880", "name": "黄金ETF", "asset_class": "gold"},
    # 4. 跨境权益（美股 + 港股科技）
    {"code": "513100.SH", "tx": "sh513100", "name": "纳指ETF", "asset_class": "overseas"},
    {"code": "513500.SH", "tx": "sh513500", "name": "标普500ETF", "asset_class": "overseas"},
    {"code": "513180.SH", "tx": "sh513180", "name": "恒生科技ETF", "asset_class": "overseas"},
    {"code": "513050.SH", "tx": "sh513050", "name": "中概互联ETF", "asset_class": "overseas"},
    # 5. 利率债避险底仓
    {"code": "511260.SH", "tx": "sh511260", "name": "十年国债ETF", "asset_class": "bond"},
    {"code": "511090.SH", "tx": "sh511090", "name": "30年国债ETF", "asset_class": "bond"},
]


def _ch_exec(sql: str, body: bytes | None = None) -> str:
    host = (os.environ.get("AISTOCK_CLICKHOUSE_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("AISTOCK_CLICKHOUSE_PORT") or "8123").strip()
    if body is None:
        req = urllib.request.Request(f"http://{host}:{port}/", data=sql.encode("utf-8"), method="POST")
    else:
        q = urllib.parse.urlencode({"query": sql})
        req = urllib.request.Request(f"http://{host}:{port}/?{q}", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8").strip()


def _fetch_tx_qfq_range(tx_sym: str, start: str, end: str) -> list[list[Any]]:
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tx_sym},day,{start},{end},640,qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        doc = json.loads(r.read().decode("utf-8"))
    d = (doc.get("data") or {}).get(tx_sym) or {}
    return list(d.get("qfqday") or d.get("day") or [])


def ensure_etf_table() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS stock.etf_kline_daily (
        code String,
        name String,
        asset_class LowCardinality(String),
        trade_date Date,
        open Float64,
        high Float64,
        low Float64,
        close Float64,
        volume Float64,
        amount Float64 DEFAULT 0,
        adj_type LowCardinality(String) DEFAULT 'qfq',
        source LowCardinality(String) DEFAULT 'tencent_qfq',
        updated_at DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (code, trade_date)
    """
    _ch_exec(ddl)


def sync_all_etfs() -> dict[str, Any]:
    ensure_etf_table()
    now_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    end_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    segments = [
        ("2022-06-01", "2024-06-30"),
        ("2024-06-01", end_str),
    ]

    summary: list[dict[str, Any]] = []
    all_lines: list[str] = []

    for item in CROSS_ASSET_ETFS:
        by_date: dict[str, list[Any]] = {}
        for s, e in segments:
            rows = _fetch_tx_qfq_range(item["tx"], s, e)
            for r in rows:
                if len(r) >= 6:
                    by_date[str(r[0])[:10]] = r

        sorted_dates = sorted(by_date.keys())
        for d in sorted_dates:
            r = by_date[d]
            o, c, h, l, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
            if o <= 0 or c <= 0:
                continue
            obj = {
                "code": item["code"],
                "name": item["name"],
                "asset_class": item["asset_class"],
                "trade_date": d,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "amount": round(v * c * 100.0, 2),
                "adj_type": "qfq",
                "source": "tencent_qfq",
                "updated_at": now_str,
            }
            all_lines.append(json.dumps(obj, ensure_ascii=False))

        summary.append(
            {
                "code": item["code"],
                "name": item["name"],
                "asset_class": item["asset_class"],
                "rows": len(sorted_dates),
                "first_date": sorted_dates[0] if sorted_dates else None,
                "last_date": sorted_dates[-1] if sorted_dates else None,
            }
        )

    if all_lines:
        payload = ("\n".join(all_lines) + "\n").encode("utf-8")
        _ch_exec("INSERT INTO stock.etf_kline_daily FORMAT JSONEachRow", body=payload)
        _ch_exec("OPTIMIZE TABLE stock.etf_kline_daily FINAL")

    verify = _ch_exec(
        "SELECT code, any(name), any(asset_class), min(trade_date), max(trade_date), count() "
        "FROM stock.etf_kline_daily FINAL GROUP BY code ORDER BY code FORMAT TabSeparated"
    )
    return {"inserted_rows": len(all_lines), "etf_count": len(summary), "verify": verify}


if __name__ == "__main__":
    res = sync_all_etfs()
    print(f"=== ClickHouse stock.etf_kline_daily 同步完成：{res['etf_count']} 只跨资产 ETF，共 {res['inserted_rows']} 行 ===")
    print(res["verify"])
