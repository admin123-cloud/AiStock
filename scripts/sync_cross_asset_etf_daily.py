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

# 防止受系统代理影响直连国内行情接口
os.environ["NO_PROXY"] = "*"

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

# 扩展池：47 只主流 A 股行业/主题 ETF + 扩展商品与全球跨境 ETF（独立 asset_class 隔离，绝不污染现役 14 只核心跨资产池）
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
    # C. 大金融、顺周期资源与小微盘风向标（10 只）
    {"code": "512880.SH", "tx": "sh512880", "name": "证券ETF", "asset_class": "cn_sector"},
    {"code": "512800.SH", "tx": "sh512800", "name": "银行ETF", "asset_class": "cn_sector"},
    {"code": "512400.SH", "tx": "sh512400", "name": "有色金属ETF", "asset_class": "cn_sector"},
    {"code": "517520.SH", "tx": "sh517520", "name": "黄金股ETF", "asset_class": "cn_sector"},
    {"code": "516780.SH", "tx": "sh516780", "name": "稀土ETF", "asset_class": "cn_sector"},
    {"code": "515220.SH", "tx": "sh515220", "name": "煤炭ETF", "asset_class": "cn_sector"},
    {"code": "516020.SH", "tx": "sh516020", "name": "化工ETF", "asset_class": "cn_sector"},
    {"code": "515210.SH", "tx": "sh515210", "name": "钢铁ETF", "asset_class": "cn_sector"},
    {"code": "512200.SH", "tx": "sh512200", "name": "房地产ETF", "asset_class": "cn_sector"},
    {"code": "563300.SH", "tx": "sh563300", "name": "中证2000ETF", "asset_class": "cn_sector"},
    # D. 大消费、医药与农林（6 只）
    {"code": "512690.SH", "tx": "sh512690", "name": "酒ETF", "asset_class": "cn_sector"},
    {"code": "159928.SZ", "tx": "sz159928", "name": "消费ETF", "asset_class": "cn_sector"},
    {"code": "512010.SH", "tx": "sh512010", "name": "医药ETF", "asset_class": "cn_sector"},
    {"code": "159992.SZ", "tx": "sz159992", "name": "创新药ETF", "asset_class": "cn_sector"},
    {"code": "512170.SH", "tx": "sh512170", "name": "医疗ETF", "asset_class": "cn_sector"},
    {"code": "159865.SZ", "tx": "sz159865", "name": "养殖ETF", "asset_class": "cn_sector"},
    # E. 扩展商品与全球跨境资产（17 只，asset_class=commodity_ext/overseas_ext 严格隔离于线上核心池）
    {"code": "159985.SZ", "tx": "sz159985", "name": "豆粕ETF", "asset_class": "commodity_ext"},
    {"code": "159980.SZ", "tx": "sz159980", "name": "有色期货ETF", "asset_class": "commodity_ext"},
    {"code": "159981.SZ", "tx": "sz159981", "name": "能源化工ETF", "asset_class": "commodity_ext"},
    {"code": "513350.SH", "tx": "sh513350", "name": "标普油气ETF", "asset_class": "commodity_ext"},
    {"code": "513520.SH", "tx": "sh513520", "name": "日经ETF", "asset_class": "overseas_ext"},
    {"code": "513030.SH", "tx": "sh513030", "name": "德国ETF", "asset_class": "overseas_ext"},
    {"code": "513290.SH", "tx": "sh513290", "name": "纳指生物科技ETF", "asset_class": "overseas_ext"},
    {"code": "513590.SH", "tx": "sh513590", "name": "香港消费ETF", "asset_class": "overseas_ext"},
    {"code": "513400.SH", "tx": "sh513400", "name": "道琼斯ETF", "asset_class": "overseas_ext"},
    {"code": "159509.SZ", "tx": "sz159509", "name": "纳指科技ETF", "asset_class": "overseas_ext"},
    {"code": "513690.SH", "tx": "sh513690", "name": "港股红利ETF", "asset_class": "overseas_ext"},
    {"code": "513060.SH", "tx": "sh513060", "name": "恒生医疗ETF", "asset_class": "overseas_ext"},
    {"code": "159920.SZ", "tx": "sz159920", "name": "恒生ETF", "asset_class": "overseas_ext"},
    {"code": "513080.SH", "tx": "sh513080", "name": "法国CAC40ETF", "asset_class": "overseas_ext"},
    {"code": "520870.SH", "tx": "sh520870", "name": "巴西ETF", "asset_class": "overseas_ext"},
    {"code": "520830.SH", "tx": "sh520830", "name": "沙特ETF", "asset_class": "overseas_ext"},
    {"code": "513730.SH", "tx": "sh513730", "name": "东南亚科技ETF", "asset_class": "overseas_ext"},
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
    import time as _time
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tx_sym},day,{start},{end},640,qfq"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=15) as r:
                doc = json.loads(r.read().decode("utf-8"))
            d = (doc.get("data") or {}).get(tx_sym) or {}
            return list(d.get("qfqday") or d.get("day") or [])
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _time.sleep(0.4 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _fetch_em_qfq_range(code: str, start: str, end: str) -> list[list[Any]]:
    """腾讯接口限流(501)时的东方财富前复权日K回退源。"""
    import time as _time
    import akshare as ak
    code6 = code.split(".")[0]
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            df = ak.fund_etf_hist_em(
                symbol=code6,
                period="daily",
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
                adjust="qfq",
            )
            if df is None or df.empty:
                return []
            out: list[list[Any]] = []
            for _, row in df.iterrows():
                out.append([
                    str(row["日期"])[:10],
                    float(row["开盘"]),
                    float(row["收盘"]),
                    float(row["最高"]),
                    float(row["最低"]),
                    float(row["成交量"]),
                    float(row.get("成交额") or 0.0),
                ])
            return out
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _time.sleep(0.5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _fetch_sina_range(tx_sym: str, start: str, end: str) -> list[list[Any]]:
    """东财也不可用时的新浪日K兜底源（最近增量区间通常无复权因子变化）。"""
    import akshare as ak
    df = ak.fund_etf_hist_sina(symbol=tx_sym)
    if df is None or df.empty:
        return []
    out: list[list[Any]] = []
    for _, row in df.iterrows():
        d = str(row["date"])[:10]
        if start <= d <= end:
            out.append([
                d,
                float(row["open"]),
                float(row["close"]),
                float(row["high"]),
                float(row["low"]),
                float(row["volume"]),
                float(row.get("amount") or 0.0),
            ])
    return out


def _fetch_tdx_local_day_range(
    tx_sym: str,
    start: str,
    end: str,
    *,
    ref_close_by_date: dict[str, float] | None = None,
) -> list[list[Any]]:
    """外网接口滞后/限流时的本地通达信 .day 兜底源（强制重叠日价格偏差 <= 0.002 硬校验，零复权漂移）。"""
    import struct
    from pathlib import Path

    vipdoc_candidates = []
    if os.environ.get("AISTOCK_TDX_VIPDOC_ROOT"):
        vipdoc_candidates.append(Path(os.environ["AISTOCK_TDX_VIPDOC_ROOT"]))
    for env_key in ("AISTOCK_TDX_ROOT", "TDX_BASE"):
        if os.environ.get(env_key):
            vipdoc_candidates.append(Path(os.environ[env_key]) / "vipdoc")
    vipdoc_candidates.extend([Path("/d/TDX/vipdoc"), Path("D:/TDX/vipdoc")])

    mkt = tx_sym[:2].lower()
    day_file = None
    for vroot in vipdoc_candidates:
        cand = vroot / mkt / "lday" / f"{tx_sym.lower()}.day"
        if cand.exists():
            day_file = cand
            break
    if day_file is None:
        return []
    raw = day_file.read_bytes()
    n = len(raw) // 32
    if n <= 0:
        return []
    parsed: list[list[Any]] = []
    # 只扫描末尾最多 90 根以保持毫秒级速度
    for i in range(max(0, n - 90), n):
        d_int, o_i, h_i, l_i, c_i, amt_f, vol_i, _ = struct.unpack("<IIIIIfII", raw[i * 32:(i + 1) * 32])
        d_str = f"{d_int // 10000:04d}-{(d_int // 100) % 100:02d}-{d_int % 100:02d}"
        if start <= d_str <= end:
            # 通达信 ETF .day 价格单位为 1/1000 元，成交量为股（转换为手 = vol/100）
            parsed.append([
                d_str,
                round(o_i / 1000.0, 4),
                round(c_i / 1000.0, 4),
                round(h_i / 1000.0, 4),
                round(l_i / 1000.0, 4),
                round(vol_i / 100.0, 2),
                round(float(amt_f), 2),
            ])
    if not parsed:
        return []
    # 若有腾讯前复权重叠日，必须至少有 1 个重叠日收盘价完全对齐（偏差 <= 0.002），否则 fail-closed 拒用
    if ref_close_by_date:
        overlaps = [
            abs(float(r[2]) - ref_close_by_date[r[0]])
            for r in parsed
            if r[0] in ref_close_by_date
        ]
        if not overlaps or min(overlaps) > 0.002:
            return []
    return parsed


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


def sync_all_etfs(*, include_sectors: bool = True, recent_days: int | None = 30) -> dict[str, Any]:
    ensure_etf_table()
    now_bj = datetime.now(BJ_TZ)
    now_str = now_bj.strftime("%Y-%m-%d %H:%M:%S")
    end_str = now_bj.strftime("%Y-%m-%d")

    # 已有历史数据的标的默认只做近 N 天增量同步，避免每日重拉 4 年全量触发腾讯 501 限流；
    # 若某只标的为首次入库（历史行数 < 20）或显式传 recent_days=None，则自动按约 1.5 年分段全量回补。
    full_segments = [
        ("2022-06-01", "2023-12-31"),
        ("2023-12-01", "2025-06-30"),
        ("2025-06-01", end_str),
    ]
    existing_by_code: dict[str, int] = {}
    raw_counts = _ch_exec(
        "SELECT code, count() FROM stock.etf_kline_daily FINAL GROUP BY code FORMAT TabSeparated"
    )
    for line in raw_counts.splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 2:
            try:
                existing_by_code[parts[0]] = int(parts[1])
            except ValueError:
                pass
    inc_segments: list[tuple[str, str]] | None = None
    if recent_days is not None and recent_days > 0:
        start_inc = (now_bj - timedelta(days=int(recent_days))).strftime("%Y-%m-%d")
        inc_segments = [(start_inc, end_str)]

    summary: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    all_lines: list[str] = []
    target_list = ALL_SYNC_ETFS if include_sectors else CROSS_ASSET_ETFS

    for item in target_list:
        by_date: dict[str, list[Any]] = {}
        src_used = "tencent_qfq"
        item_segments = (
            inc_segments
            if (inc_segments is not None and existing_by_code.get(item["code"], 0) >= 20)
            else full_segments
        )
        for s, e in item_segments:
            rows: list[list[Any]] = []
            try:
                rows = _fetch_tx_qfq_range(item["tx"], s, e)
            except Exception:
                rows = []

            # 腾讯偶尔静默漏出当日最新一根 bar（不抛异常但滞后）：若最新日期早于段末且盘后已收盘，
            # 优先毫秒级尝试本地 TDX .day（强制重叠日价格偏差 <= 0.002 硬校验），无本地文件再回退东财/新浪。
            tx_last = max((str(r[0])[:10] for r in rows if len(r) >= 6), default="")
            need_fallback = (not rows) or (
                e == end_str and (now_bj.hour, now_bj.minute) >= (15, 30) and tx_last < end_str
            )
            if need_fallback:
                resolved = False
                ref_closes = {
                    str(r[0])[:10]: float(r[2])
                    for r in rows
                    if len(r) >= 6 and float(r[2]) > 0
                }
                tdx_rows = _fetch_tdx_local_day_range(
                    item["tx"], s, e, ref_close_by_date=ref_closes or None
                )
                tdx_last = max((str(r[0])[:10] for r in tdx_rows if len(r) >= 6), default="")
                if tdx_rows and (not rows or tdx_last > tx_last):
                    merged_map = {str(r[0])[:10]: r for r in rows if len(r) >= 6}
                    for r in tdx_rows:
                        if str(r[0])[:10] > tx_last or not rows:
                            merged_map[str(r[0])[:10]] = r
                    rows = [merged_map[k] for k in sorted(merged_map.keys())]
                    src_used = "tdx_local_day"
                    resolved = True
                if not resolved:
                    try:
                        em_rows = _fetch_em_qfq_range(item["code"], s, e)
                        em_last = max((str(r[0])[:10] for r in em_rows if len(r) >= 6), default="")
                        if em_rows and (not rows or em_last > tx_last):
                            rows = em_rows
                            src_used = "eastmoney_qfq"
                            resolved = True
                    except Exception:
                        resolved = False
                if not resolved and not rows:
                    try:
                        rows = _fetch_sina_range(item["tx"], s, e)
                        src_used = "sina_fallback"
                    except Exception as exc:
                        errors.append({"code": item["code"], "error": str(exc)})
                        rows = []
            for r in rows:
                if len(r) >= 6:
                    by_date[str(r[0])[:10]] = r

        sorted_dates = sorted(by_date.keys())
        # 交易日盘中（15:05 尚未收盘前）严禁把腾讯返回的当日未收盘半日 K 线写入正式日线表，避免造成核心池日期割裂
        allow_today = (now_bj.hour, now_bj.minute) >= (15, 5)
        for d in sorted_dates:
            if not allow_today and d >= end_str:
                continue
            r = by_date[d]
            o, c, h, l, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
            if o <= 0 or c <= 0:
                continue
            amt = float(r[6]) if len(r) >= 7 and float(r[6]) > 0 else round(v * c * 100.0, 2)
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
                "amount": amt,
                "adj_type": "qfq",
                "source": src_used,
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
