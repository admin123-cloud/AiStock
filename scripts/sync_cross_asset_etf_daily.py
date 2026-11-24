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

# 扩展池：32 只主流 A 股行业/主题 ETF + 扩展商品与全球跨境 ETF（独立 asset_class 隔离，绝不污染现役 14 只核心跨资产池）
SECTOR_AND_EXTENDED_ETFS: list[dict[str, str]] = [
    # A. 科技成长与硬科技（8 只）
    {"code": "512480.SH", "tx": "sh512480", "name": "半导体ETF", "asset_class": "cn_sector"},
    {"code": "159995.SZ", "tx": "sz159995", "name": "芯片ETF", "asset_class": "cn_sector"},
    {"code": "515070.SH", "tx": "sh515070", "name": "人工智能AIETF", "asset_class": "cn_sector"},
    {"code": "515880.SH", "tx": "sh515880", "name": "通信ETF", "asset_class": "cn_sector"},
    {"code": "512720.SH", "tx": "sh512720", "name": "计算机ETF", "asset_class": "cn_sector"},
    {"code": "159732.SZ", "tx": "sz159732", "name": "消费电子ETF", "asset_class": "cn_sector"},
    {"code": "562500.SH", "tx": "sh562500", "name": "机器人ETF", "asset_class": "cn_sector"},
    {"code": "159869.SZ", "tx": "sz159869", "name": "游戏ETF", "asset_class": "cn_sector"},
    # B. 新能源、高端制造与公用事业（6 只）
    {"code": "515030.SH", "tx": "sh515030", "name": "新能源车ETF", "asset_class": "cn_sector"},
    {"code": "515790.SH", "tx": "sh515790", "name": "光伏ETF", "asset_class": "cn_sector"},
    {"code": "159755.SZ", "tx": "sz159755", "name": "电池ETF", "asset_class": "cn_sector"},
    {"code": "512660.SH", "tx": "sh512660", "name": "军工ETF", "asset_class": "cn_sector"},
    {"code": "159611.SZ", "tx": "sz159611", "name": "电力ETF", "asset_class": "cn_sector"},
    {"code": "516970.SH", "tx": "sh516970", "name": "基建50ETF", "asset_class": "cn_sector"},
    # C. 大金融与顺周期资源（8 只）
    {"code": "512880.SH", "tx": "sh512880", "name": "证券ETF", "asset_class": "cn_sector"},
    {"code": "512800.SH", "tx": "sh512800", "name": "银行ETF", "asset_class": "cn_sector"},
    {"code": "512400.SH", "tx": "sh512400", "name": "有色金属ETF", "asset_class": "cn_sector"},
    {"code": "516780.SH", "tx": "sh516780", "name": "稀土ETF", "asset_class": "cn_sector"},
    {"code": "515220.SH", "tx": "sh515220", "name": "煤炭ETF", "asset_class": "cn_sector"},
    {"code": "516020.SH", "tx": "sh516020", "name": "化工ETF", "asset_class": "cn_sector"},
    {"code": "515210.SH", "tx": "sh515210", "name": "钢铁ETF", "asset_class": "cn_sector"},
    {"code": "512200.SH", "tx": "sh512200", "name": "房地产ETF", "asset_class": "cn_sector"},
    # D. 大消费、医药与农林（6 只）
    {"code": "512690.SH", "tx": "sh512690", "name": "酒ETF", "asset_class": "cn_sector"},
    {"code": "159928.SZ", "tx": "sz159928", "name": "消费ETF", "asset_class": "cn_sector"},
    {"code": "512010.SH", "tx": "sh512010", "name": "医药ETF", "asset_class": "cn_sector"},
    {"code": "159992.SZ", "tx": "sz159992", "name": "创新药ETF", "asset_class": "cn_sector"},
    {"code": "512170.SH", "tx": "sh512170", "name": "医疗ETF", "asset_class": "cn_sector"},
    {"code": "159865.SZ", "tx": "sz159865", "name": "养殖ETF", "asset_class": "cn_sector"},
    # E. 扩展商品与全球跨境资产（4 只）
    {"code": "159985.SZ", "tx": "sz159985", "name": "豆粕ETF", "asset_class": "commodity_ext"},
    {"code": "159980.SZ", "tx": "sz159980", "name": "有色期货ETF", "asset_class": "commodity_ext"},
    {"code": "513520.SH", "tx": "sh513520", "name": "日经ETF", "asset_class": "overseas_ext"},
    {"code": "513030.SH", "tx": "sh513030", "name": "德国ETF", "asset_class": "overseas_ext"},
]

ALL_SYNC_ETFS: list[dict[str, str]] = CROSS_ASSET_ETFS + SECTOR_AND_EXTENDED_ETFS


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


def sync_all_etfs(*, include_sectors: bool = True) -> dict[str, Any]:
    ensure_etf_table()
    now_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    end_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    segments = [
        ("2022-06-01", "2024-06-30"),
        ("2024-06-01", end_str),
    ]

    summary: list[dict[str, Any]] = []
    all_lines: list[str] = []
    target_list = ALL_SYNC_ETFS if include_sectors else CROSS_ASSET_ETFS

    for item in target_list:
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
