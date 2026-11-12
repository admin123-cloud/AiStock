"""
R56 看板 P1 三件套（概念/行业热度、竞价异动、情绪雷达）

- GET /api/market/sector-heat             概念/行业热度榜（东财只读接口）
- GET /api/market/auction-anomaly?date=   竞价异动（盘前两拍快照，落 runtime）
- GET /api/market/sentiment-radar         情绪雷达六维评分（kline_daily 聚合）
"""

from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, date as date_type
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from utils.logger import get_logger
from utils.market_warehouse import (
    clickhouse_available,
    clickhouse_query_df,
    clickhouse_scalar,
    clickhouse_table_exists,
)
from utils.paths import runtime_path

logger = get_logger("market_board")

router = APIRouter(prefix="/api/market", tags=["市场看板"])

# 强制注册别名，供 operations_preview 用
market_board_routes = router.routes

BJ_TZ = ZoneInfo("Asia/Shanghai")

# ──────────────────────────────────────────────
# 1) 概念/行业热度榜（东财只读限频≥3s）
# ──────────────────────────────────────────────

_SECTOR_HEAT_CACHE: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
_SECTOR_HEAT_TTL = 10.0  # 盘中小 TTL 吸收轮询，不突破东财限频


@router.get("/sector-heat")
def get_sector_heat(
    board_type: Optional[str] = Query(None, description="板块类型：concept/industry/region"),
    sort_by: str = Query("change_pct", description="排序字段：change_pct/up_count"),
    top_n: int = Query(20, ge=5, le=100),
):
    """概念/行业热度榜。

    盘中小 TTL=10s 避免突破东财限频（≥3s 间）。
    数据源：东财 pushdata 板块行情列表（只读，版块涨跌幅+涨跌家数）。
    """
    now = time.monotonic()
    cached = _SECTOR_HEAT_CACHE.get("payload")
    if cached is not None and now < _SECTOR_HEAT_CACHE.get("expires_at", 0.0):
        return cached

    try:
        boards = _fetch_eastmoney_board_heat(board_type, top_n)
    except Exception as exc:
        logger.warning(f"sector-heat fetch failed: {exc}")
        _SECTOR_HEAT_CACHE["expires_at"] = now + 5.0
        raise HTTPException(status_code=502, detail=f"东财板块接口不可用: {exc}")

    payload = {
        "as_of": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "board_type": board_type or "all",
        "total": len(boards),
        "items": boards,
        "source": "eastmoney",
    }
    _SECTOR_HEAT_CACHE.update({"expires_at": now + _SECTOR_HEAT_TTL, "payload": payload})
    return payload


def _fetch_eastmoney_board_heat(board_type: Optional[str], top_n: int) -> list[dict]:
    """从东财 `pushdata.eastmoney.com` 采集板块行情列表。

    接口：https://pushdata.eastmoney.com/api/qt/clist/get
    fs=m:90+t:2 （概念） / m:90+t:3 （行业） / m:90+t:1 （地域）
    限频：≥3s 间隔；本函数不并发，由调用方 TTL 兜底。
    """
    import requests

    type_map = {
        "concept": "m:90+t:2",
        "industry": "m:90+t:3",
        "region": "m:90+t:1",
    }

    results: list[dict] = []

    scope_list = [type_map[board_type]] if board_type and board_type in type_map else list(type_map.values())

    for fs in scope_list:
        url = (
            "https://pushdata.eastmoney.com/api/qt/clist/get"
            f"?fs={fs}&po=1&pz={top_n}&pn=1&np=1&fltt=2"
            "&fid=f3"
            "&fields=f2,f3,f4,f12,f14,f15,f16,f17,f18,f20,f21,f104,f105"
        )
        try:
            resp = requests.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://data.eastmoney.com/",
            })
            data = resp.json()
            items = (data.get("data", {}) or {}).get("diff", []) or []
        except Exception as exc:
            logger.warning(f"_fetch_eastmoney_board_heat fs={fs} failed: {exc}")
            continue

        for item in items:
            try:
                code = str(item.get("f12") or "")
                name = str(item.get("f14") or "")
                change_pct = _to_float(item.get("f3"))
                price = _to_float(item.get("f2"))
                volume = _to_float(item.get("f15")) * 100  # 万手→手
                amount = _to_float(item.get("f20")) * 10000  # 万元→元
                up_count = int(_to_float(item.get("f104")))
                down_count = int(_to_float(item.get("f105")))
                rise_count = int(_to_float(item.get("f17") or 0))
                fall_count = int(_to_float(item.get("f18") or 0))
            except (ValueError, TypeError):
                continue

            if not code:
                continue

            results.append({
                "code": code,
                "name": name,
                "type": _infer_board_type(fs),
                "change_pct": change_pct,
                "price": price,
                "volume": volume,
                "amount": amount,
                "up_count": up_count,
                "down_count": down_count,
                "rise_count": rise_count,
                "fall_count": fall_count,
                "total_count": up_count + down_count if (up_count + down_count) > 0 else None,
            })

    results.sort(key=lambda x: x.get("change_pct") or 0, reverse=True)
    return results[:top_n]


def _infer_board_type(fs: str) -> str:
    if "t:2" in fs:
        return "concept"
    if "t:3" in fs:
        return "industry"
    if "t:1" in fs:
        return "region"
    return "unknown"


@router.get("/sector-heat/{code}/stocks")
def get_sector_heat_stocks(code: str):
    """成分股钻取：东财板块成分股列表（只读限频）。"""
    import requests

    url = (
        "https://pushdata.eastmoney.com/api/qt/clist/get"
        f"?fs=b:{code}+f:!50&po=1&pz=500&pn=1&np=1&fltt=2"
        "&fid=f3"
        "&fields=f2,f3,f4,f12,f14,f15,f16,f17,f18"
    )
    try:
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        })
        data = resp.json()
        raw_items = ((data.get("data", {}) or {}).get("diff", []) or [])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"成分股接口不可用: {exc}")

    stocks: list[dict] = []
    for item in raw_items:
        try:
            stock_code = str(item.get("f12") or "")
            name = str(item.get("f14") or "")
            price = _to_float(item.get("f2"))
            change_pct = _to_float(item.get("f3"))
            volume = _to_float(item.get("f15")) * 100 if _to_float(item.get("f15")) else None
            amount = _to_float(item.get("f20")) * 10000 if _to_float(item.get("f20")) else None
        except (ValueError, TypeError):
            continue
        if stock_code:
            stocks.append({
                "code": stock_code,
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "volume": volume,
                "amount": amount,
            })

    return {"code": code, "total": len(stocks), "stocks": stocks, "source": "eastmoney"}


# ──────────────────────────────────────────────
# 2) 竞价异动（9:20/9:24 两拍快照 + GET 查询）
# ──────────────────────────────────────────────

_AUCTION_RUNTIME_DIR = "l2_r56"


@router.get("/auction-anomaly")
def get_auction_anomaly(
    date: Optional[str] = Query(None, description="日期 YYYY-MM-DD，默认今日"),
    snap: Optional[str] = Query(None, description="快照标识：snap1_920 / snap2_924"),
):
    """竞价异动查询。

    快照文件：F:/Aistock/data/data/runtime/l2_r56/
      auction_snap1_920_{date}.csv
      auction_snap2_924_{date}.csv
    """
    target_date = date or datetime.now(BJ_TZ).strftime("%Y-%m-%d")

    snap_dir = runtime_path(_AUCTION_RUNTIME_DIR)
    snap_files: list[Path] = []

    if snap:
        pattern = f"auction_{snap}_{target_date}.csv"
        candidate = snap_dir / pattern
        if candidate.exists():
            snap_files = [candidate]
    else:
        for fname in ["auction_snap1_920", "auction_snap2_924"]:
            candidate = snap_dir / f"{fname}_{target_date}.csv"
            if candidate.exists():
                snap_files.append(candidate)

    if not snap_files:
        return {
            "date": target_date,
            "available": False,
            "message": f"{target_date} 无竞价快照（非交易日或快照未采集）",
            "snap1": [],
            "snap2": [],
        }

    result: Dict[str, Any] = {"date": target_date, "available": True}
    for sp in snap_files:
        key = "snap1_920" if "snap1_920" in sp.name else "snap2_924"
        records = _read_auction_csv(sp)
        result[key] = records

    # 如果两拍都有，计算 delta
    snap1 = result.get("snap1_920", [])
    snap2 = result.get("snap2_924", [])
    if snap1 and snap2:
        result["anomaly_summary"] = _build_anomaly_summary(snap1, snap2)

    return result


def _read_auction_csv(path: Path) -> list[dict]:
    """读取竞价快照 CSV（码表分离）。"""
    if not path.exists():
        return []
    records: list[dict] = []
    with open(str(path), "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                records.append({
                    "code": row.get("code", ""),
                    "name": row.get("name", ""),
                    "price": _to_float(row.get("price")),
                    "pre_close": _to_float(row.get("pre_close")),
                    "volume": int(row.get("volume", 0) or 0),
                    "amount": _to_float(row.get("amount")),
                    "gap_pct": _to_float(row.get("gap_pct")),
                    "auction_vol": int(row.get("auction_vol", 0) or 0),
                    "vol_ratio_30d": _to_float(row.get("vol_ratio_30d")),
                })
            except (ValueError, TypeError) as exc:
                logger.warning(f"auction csv parse skip row: {exc}")
                continue
    return records


def _build_anomaly_summary(snap1: list[dict], snap2: list[dict]) -> dict:
    """构建竞价异动摘要。"""
    code_map2 = {s["code"]: s for s in snap2}

    merged: list[dict] = []
    for s1 in snap1:
        s2 = code_map2.get(s1["code"])
        if not s2:
            continue
        merged.append({
            "code": s1["code"],
            "name": s1["name"] or s2["name"],
            "gap_pct_920": s1.get("gap_pct"),
            "gap_pct_924": s2.get("gap_pct"),
            "gap_delta": (
                round((s2.get("gap_pct") or 0) - (s1.get("gap_pct") or 0), 2)
                if s1.get("gap_pct") is not None and s2.get("gap_pct") is not None
                else None
            ),
            "vol_920": s1.get("auction_vol", 0),
            "vol_924": s2.get("auction_vol", 0),
            "vol_ratio_30d": s2.get("vol_ratio_30d"),
        })

    # 按 gap_pct 异动排序
    merged.sort(key=lambda x: abs(x.get("gap_delta") or 0), reverse=True)

    gap_big = [m for m in merged if m.get("gap_pct_924") is not None and abs(m["gap_pct_924"]) >= 5]
    gap_mid = [m for m in merged if m.get("gap_pct_924") is not None and 3 <= abs(m["gap_pct_924"]) < 5]
    vol_spike = sorted(
        [m for m in merged if m.get("vol_ratio_30d") is not None and m["vol_ratio_30d"] >= 2.0],
        key=lambda x: x["vol_ratio_30d"],
        reverse=True,
    )

    return {
        "total_merged": len(merged),
        "gap_ge_5pct": len(gap_big),
        "gap_3_to_5pct": len(gap_mid),
        "vol_spike_ge_2x": len(vol_spike),
        "top_gap_up": merged[:10] if merged else [],
        "top_vol_spike": vol_spike[:10],
    }


# ──────────────────────────────────────────────
# 3) 情绪雷达 + 综合评分（六维雷达图）
# ──────────────────────────────────────────────

_SENTIMENT_RADAR_CACHE: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
_SENTIMENT_RADAR_TTL = 60.0  # 1 分钟


@router.get("/sentiment-radar")
def get_sentiment_radar():
    """六维情绪雷达 + 0-100 综合评分。

    六维：
      - 涨跌家数比 (up_down_ratio)
      - 封板率 (limit_up_seal_rate)
      - 连板高度 (max_continuous_board)
      - 炸板率 (limit_up_break_rate)
      - 宽度 MA20 占比 (ma20_breadth)
      - 成交额分位 (turnover_percentile)

    综合评分 = 分位加权（各维度 0-100，对应权重）
    """
    now = time.monotonic()
    cached = _SENTIMENT_RADAR_CACHE.get("payload")
    if cached is not None and now < _SENTIMENT_RADAR_CACHE.get("expires_at", 0.0):
        return cached

    try:
        payload = _compute_sentiment_radar()
    except Exception as exc:
        logger.error(f"sentiment-radar compute error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    _SENTIMENT_RADAR_CACHE.update({"expires_at": now + _SENTIMENT_RADAR_TTL, "payload": payload})
    return payload


def _compute_sentiment_radar() -> dict:
    """六维雷达计算。"""
    if not clickhouse_available():
        return {"available": False, "error": "ClickHouse 不可用"}

    trade_date = _latest_trade_date()
    if not trade_date:
        return {"available": False, "error": "无交易数据"}

    # 30d 日期范围（含本日）
    cal_30d = _latest_30_trade_dates(trade_date)

    # --- 维1：涨跌家数比 --------------------------------------------------------
    up_down = _dim_up_down_ratio(trade_date)

    # --- 维2：封板率 ------------------------------------------------------------
    seal_rate = _dim_limit_up_seal_rate(trade_date)

    # --- 维3：连板高度 ----------------------------------------------------------
    board_height = _dim_max_board_height(trade_date, cal_30d)

    # --- 维4：炸板率 ------------------------------------------------------------
    break_rate = _dim_limit_up_break_rate(trade_date)

    # --- 维5：宽度 MA20 占比 ----------------------------------------------------
    ma20_breadth = _dim_ma20_breadth(trade_date)

    # --- 维6：成交额分位 --------------------------------------------------------
    turnover_pct = _dim_turnover_percentile(trade_date, cal_30d)

    dims = {
        "up_down_ratio": {
            "label": "涨跌家数比",
            "value": up_down["ratio"],
            "raw_value": up_down["raw"],
            "weight": 0.25,
        },
        "limit_up_seal_rate": {
            "label": "封板率",
            "value": seal_rate["seal_rate"],
            "raw_value": seal_rate["raw"],
            "weight": 0.15,
        },
        "max_continuous_board": {
            "label": "连板高度",
            "value": board_height["score"],
            "raw_value": board_height["height"],
            "weight": 0.15,
        },
        "limit_up_break_rate": {
            "label": "炸板率",
            "value": break_rate["score"],
            "raw_value": break_rate["raw"],
            "weight": 0.15,
        },
        "ma20_breadth": {
            "label": "宽度MA20占比",
            "value": ma20_breadth["score"],
            "raw_value": ma20_breadth["raw"],
            "weight": 0.15,
        },
        "turnover_percentile": {
            "label": "成交额分位",
            "value": turnover_pct["score"],
            "raw_value": turnover_pct["raw"],
            "weight": 0.15,
        },
    }

    # 综合分 = 加权求和
    composite = sum(d["value"] * d["weight"] for d in dims.values())
    composite = round(min(100, max(0, composite)), 1)

    return {
        "date": trade_date,
        "as_of": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "composite_score": composite,
        "dimensions": dims,
        "weights": {k: v["weight"] for k, v in dims.items()},
        "available": True,
        "source": "kline_daily",
    }


# ── 子维度计算 ──────────────────────────────────────────────


def _latest_trade_date() -> Optional[str]:
    try:
        return str(clickhouse_scalar(
            "SELECT max(trade_date) FROM trade_calendar "
            "WHERE is_trading = 1 AND market = 'SH' AND trade_date <= today()",
            raw=True,
        ) or "")
    except Exception:
        return None


def _latest_30_trade_dates(up_to: str) -> list[str]:
    try:
        df = clickhouse_query_df(
            "SELECT trade_date FROM trade_calendar "
            "WHERE is_trading = 1 AND market = 'SH' AND trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT 30",
            [up_to],
            raw=True,
        )
        if df is not None and not df.empty:
            return [str(v)[:10] for v in reversed(df["trade_date"].tolist())]
    except Exception:
        pass
    return []


def _dim_up_down_ratio(trade_date: str) -> dict:
    """维1：涨跌家数比 → 0-100 分。

    上涨家数/(上涨+下跌+平盘)。
    score = ratio 归一化到 0-100（基准：50%→50，极端 0%→0，100%→100）。
    """
    try:
        df = clickhouse_query_df(
            """
            SELECT
                countIf(k.change_pct > 0) AS up,
                countIf(k.change_pct < 0) AS down,
                countIf(k.change_pct = 0) AS flat
            FROM kline_daily k FINAL
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock' AND k.trade_date = ? AND k.open > 0
            """,
            [trade_date],
            raw=True,
        )
        if df is None or df.empty:
            return {"ratio": 50.0, "raw": 0.5}
        row = df.iloc[0]
        up = int(row.get("up") or 0)
        down = int(row.get("down") or 0)
        total = up + down + int(row.get("flat") or 0)
        ratio = up / total if total > 0 else 0.5
        return {"ratio": round(ratio * 100, 1), "raw": round(ratio, 4)}
    except Exception:
        return {"ratio": 50.0, "raw": 0.5}


def _dim_limit_up_seal_rate(trade_date: str) -> dict:
    """维2：封板率 → 0-100 分。

    定义：连续涨停/total > 9.5% 的股票中的 seq_board >= 1 的比例。
    简化：涨/跌停总数中涨停占比（与 Sentiment 已有聚合一致）。
    """
    try:
        df = clickhouse_query_df(
            """
            SELECT
                countIf(k.change_pct >= 9.9) AS limit_up,
                countIf(k.change_pct <= -9.9) AS limit_down
            FROM kline_daily k FINAL
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock' AND k.trade_date = ? AND k.open > 0
            """,
            [trade_date],
            raw=True,
        )
        if df is None or df.empty:
            return {"seal_rate": 50.0, "raw": 0.5}
        row = df.iloc[0]
        lu = int(row.get("limit_up") or 0)
        ld = int(row.get("limit_down") or 0)
        total = lu + ld
        ratio = lu / total if total > 0 else 0.5
        # 映射到 0-100：封板率 0%→0，100%→100
        return {"seal_rate": round(ratio * 100, 1), "raw": round(ratio, 4)}
    except Exception:
        return {"seal_rate": 50.0, "raw": 0.5}


def _dim_max_board_height(trade_date: str, cal_30d: list[str]) -> dict:
    """维3：连板高度 → 0-100 分。

    简化版：取当日涨停股中，过去 5 日内涨停天数最多的作为连板高度。
    0→0 分，>=7→100 分（线性映射）。
    """
    if not cal_30d or len(cal_30d) < 2:
        return {"score": 50.0, "height": 0}

    lookback = cal_30d[-6:]  # 含今日的 6 个交易日
    try:
        # 今日涨停股
        lu_codes = clickhouse_query_df(
            """
            SELECT code FROM kline_daily FINAL
            WHERE trade_date = ? AND change_pct >= 9.9
            """,
            [trade_date],
            raw=True,
        )
        if lu_codes is None or lu_codes.empty:
            return {"score": 0.0, "height": 0}

        codes_list = [str(c) for c in lu_codes["code"].tolist() if c]
        if not codes_list:
            return {"score": 0.0, "height": 0}

        ph = ",".join(["?"] * len(codes_list))
        df = clickhouse_query_df(
            f"""
            SELECT code, countIf(change_pct >= 9.9) AS board_days
            FROM kline_daily FINAL
            WHERE trade_date IN ({','.join(['?'] * len(lookback))})
              AND code IN ({ph})
            GROUP BY code
            """,
            lookback + codes_list,
            raw=True,
        )
        if df is None or df.empty:
            return {"score": 0.0, "height": 0}
        max_board = int(df["board_days"].max() or 0)
        score = round(min(100, max_board / 7.0 * 100), 1)
        return {"score": score, "height": max_board}
    except Exception:
        return {"score": 0.0, "height": 0}


def _dim_limit_up_break_rate(trade_date: str) -> dict:
    """维4：炸板率 → 0-100 分（反转：炸板率低=高分）。

    简化：用 1 - 封板率 近似炸板率， score = (1 - raw) * 100。
    真实炸板需要盘中曾涨停又开板的数据（需要东财逐笔接口），
    P1 先用此近似，标注 note。
    """
    sr = _dim_limit_up_seal_rate(trade_date)
    break_raw = 1.0 - (sr["raw"] if sr["raw"] is not None else 0.5)
    score = round((1.0 - break_raw) * 100, 1)  # break_rate 低=好的方向
    return {"score": score, "raw": round(break_raw, 4), "note": "approximated_from_seal_rate"}


def _dim_ma20_breadth(trade_date: str) -> dict:
    """维5：宽度 MA20 占比 → 0-100 分。

    取 MA20 最新交易日（kline_daily 的 ma20 列），
    统计 close > ma20 的股票占比。
    """
    try:
        if not clickhouse_table_exists("kline_daily_ma20"):
            return {"score": 50.0, "raw": 0.5, "note": "ma20_table_missing"}
        df = clickhouse_query_df(
            """
            SELECT
                countIf(k.close > m.ma20) AS above,
                count() AS total
            FROM kline_daily k FINAL
            JOIN kline_daily_ma20 m ON m.code = k.code AND m.trade_date = k.trade_date
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock' AND k.trade_date = ? AND k.open > 0
            """,
            [trade_date],
            raw=True,
        )
        if df is None or df.empty:
            return {"score": 50.0, "raw": 0.5}
        row = df.iloc[0]
        above = int(row.get("above") or 0)
        total = int(row.get("total") or 0)
        ratio = above / total if total > 0 else 0.5
        return {"score": round(ratio * 100, 1), "raw": round(ratio, 4)}
    except Exception:
        return {"score": 50.0, "raw": 0.5, "note": "query_failed"}


def _dim_turnover_percentile(trade_date: str, cal_30d: list[str]) -> dict:
    """维6：成交额分位 → 0-100 分。

    当日成交额在近 30 个交易日中的分位值（0-100）。
    """
    if not cal_30d or len(cal_30d) < 5:
        return {"score": 50.0, "raw": 0.5}

    ph = ",".join(["?"] * len(cal_30d))
    try:
        df = clickhouse_query_df(
            f"""
            SELECT trade_date, sum(amount) AS total_amount
            FROM kline_daily k FINAL
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock' AND k.trade_date IN ({ph}) AND k.open > 0
            GROUP BY trade_date
            ORDER BY trade_date ASC
            """,
            cal_30d,
            raw=True,
        )
        if df is None or df.empty:
            return {"score": 50.0, "raw": 0.5}
        amounts = list(df["total_amount"])
        if not amounts:
            return {"score": 50.0, "raw": 0.5}

        today_amount = amounts[-1]
        below = sum(1 for a in amounts if a < today_amount)
        total = len(amounts)
        percentile = below / total if total > 0 else 0.5
        return {"score": round(percentile * 100, 1), "raw": round(percentile, 4)}
    except Exception:
        return {"score": 50.0, "raw": 0.5}


# ──────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        v = float(value)
        return v
    except (ValueError, TypeError):
        return default