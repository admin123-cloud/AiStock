"""
R56 看板 P1 三件套（概念/行业热度、竞价异动、情绪雷达）

双模数据策略（2026-11-20）：
- 盘中时段（存在新鲜的 intraday_market_snapshot 采集快照，或当日竞价快照 CSV）：
  优先使用实时采集数据（data_mode="intraday"）；
- 盘后 / 盘前 / 非交易时段（无新鲜盘中采集快照时）：
  自动使用 ClickHouse 正式日线表 kline_daily 最近一个完整交易日数据（data_mode="postclose"）。

路由：
- GET /api/market/sector-heat             概念/行业热度榜（盘中快照 / 盘后正式表）
- GET /api/market/sector-heat/{code}/stocks 板块成分股钻取（盘中快照 / 盘后正式表）
- GET /api/market/auction-anomaly?date=   竞价异动（盘前两拍快照 / 盘后正式表开盘跳空与放量）
- GET /api/market/sentiment-radar         情绪雷达六维评分（盘中快照 / 盘后正式表）
"""

from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from services.market.intraday_market_snapshot import (
    MIN_COVERED_COUNT,
    SNAPSHOT_TABLE,
    _coerce_beijing_dt,
    is_fresh,
)
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

_BOARD_TYPE_TO_SECTOR_TYPE: Dict[str, str] = {
    "concept": "tdx_sector",
    "industry": "sw_industry",
}
_SECTOR_TYPE_TO_BOARD_TYPE: Dict[str, str] = {
    "tdx_sector": "concept",
    "sw_industry": "industry",
}


# ──────────────────────────────────────────────
# 公共双模数据日期/快照探测
# ──────────────────────────────────────────────


def _latest_formal_trade_date() -> Optional[str]:
    """获取正式表 kline_daily 中最新有数据的交易日（YYYY-MM-DD）。"""
    if not clickhouse_available():
        return None
    try:
        val = clickhouse_scalar("SELECT max(trade_date) FROM kline_daily", raw=True)
        text = str(val or "")[:10]
        return text if len(text) == 10 and text != "1970-01-01" else None
    except Exception as exc:
        logger.warning(f"_latest_formal_trade_date failed: {exc}")
        return None


def _latest_fresh_intraday_snapshot() -> Optional[Tuple[str, str]]:
    """检查 intraday_market_snapshot 是否存在当日新鲜且覆盖率达标的盘中快照。

    Returns:
        (snapshot_ts_str, trade_date_str) 若新鲜可用；否则 None。
    """
    if not clickhouse_available() or not clickhouse_table_exists(SNAPSHOT_TABLE):
        return None
    try:
        df = clickhouse_query_df(
            f"""
            SELECT
                max(snapshot_ts) AS latest_ts,
                countIf(snapshot_ts = (SELECT max(snapshot_ts) FROM {SNAPSHOT_TABLE})) AS row_cnt
            FROM {SNAPSHOT_TABLE}
            """,
            raw=True,
        )
        if df is None or df.empty:
            return None
        latest_ts = df.iloc[0].get("latest_ts")
        row_cnt = int(df.iloc[0].get("row_cnt") or 0)
        ts_dt = _coerce_beijing_dt(latest_ts)
        if ts_dt is None or row_cnt < MIN_COVERED_COUNT:
            return None
        if not is_fresh(ts_dt):
            return None
        ts_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
        return ts_str, ts_dt.strftime("%Y-%m-%d")
    except Exception as exc:
        logger.warning(f"_latest_fresh_intraday_snapshot failed: {exc}")
        return None


# ──────────────────────────────────────────────
# 1) 概念/行业热度榜（盘中快照 / 盘后正式表）
# ──────────────────────────────────────────────

_SECTOR_HEAT_CACHE: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
_SECTOR_HEAT_TTL = 15.0


@router.get("/sector-heat")
def get_sector_heat(
    board_type: Optional[str] = Query(None, description="板块类型：concept/industry/region"),
    sort_by: str = Query("change_pct", description="排序字段：change_pct/up_count"),
    top_n: int = Query(20, ge=5, le=100),
):
    """概念/行业热度榜：盘中采用实时采集快照聚合，盘后使用正式表 kline_daily 聚合。"""
    cache_key = (board_type or "all", sort_by, top_n)
    now = time.monotonic()
    cached = _SECTOR_HEAT_CACHE.get(cache_key)
    if cached is not None and now < float(cached.get("expires_at") or 0.0):
        return cached["payload"]

    boards: List[Dict[str, Any]] = []
    source = "kline_daily"
    data_mode = "postclose"
    data_date: Optional[str] = None
    snapshot_ts: Optional[str] = None

    intraday = _latest_fresh_intraday_snapshot()
    if intraday is not None:
        snapshot_ts, data_date = intraday
        boards = _query_sector_heat_from_intraday(snapshot_ts, board_type, sort_by, top_n)
        source = SNAPSHOT_TABLE
        data_mode = "intraday"

    if not boards:
        data_date = _latest_formal_trade_date()
        if data_date:
            boards = _query_sector_heat_from_formal(data_date, board_type, sort_by, top_n)
            source = "kline_daily"
            data_mode = "postclose"

    payload = {
        "as_of": snapshot_ts or datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "date": data_date,
        "data_mode": data_mode,
        "board_type": board_type or "all",
        "total": len(boards),
        "items": boards,
        "source": source,
    }
    if boards:
        _SECTOR_HEAT_CACHE[cache_key] = {
            "expires_at": now + _SECTOR_HEAT_TTL,
            "payload": payload,
        }
    return payload


def _sector_type_filter(board_type: Optional[str]) -> Tuple[str, List[Any]]:
    if board_type and board_type in _BOARD_TYPE_TO_SECTOR_TYPE:
        return "ss.sector_type = ?", [_BOARD_TYPE_TO_SECTOR_TYPE[board_type]]
    return "ss.sector_type IN ('tdx_sector', 'sw_industry')", []


def _format_sector_heat_rows(df) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    results: List[Dict[str, Any]] = []
    for row in df.itertuples(index=False):
        code = str(getattr(row, "sector_code", "") or "")
        if not code:
            continue
        up_cnt = int(getattr(row, "up_count", 0) or 0)
        down_cnt = int(getattr(row, "down_count", 0) or 0)
        total_cnt = int(getattr(row, "total_count", 0) or 0)
        sector_type = str(getattr(row, "sector_type", "") or "")
        results.append({
            "code": code,
            "name": str(getattr(row, "name", code) or code),
            "type": _SECTOR_TYPE_TO_BOARD_TYPE.get(sector_type, "concept"),
            "change_pct": _to_float(getattr(row, "change_pct", None)),
            "price": _to_float(getattr(row, "avg_price", None)),
            "volume": _to_float(getattr(row, "volume", None), 0.0),
            "amount": _to_float(getattr(row, "amount", None), 0.0),
            "up_count": up_cnt,
            "down_count": down_cnt,
            "rise_count": up_cnt,
            "fall_count": down_cnt,
            "total_count": total_cnt or None,
        })
    return results


def _query_sector_heat_from_intraday(
    snapshot_ts: str,
    board_type: Optional[str],
    sort_by: str,
    top_n: int,
) -> List[Dict[str, Any]]:
    if not clickhouse_available() or not clickhouse_table_exists("source_sector_stocks"):
        return []
    where_clause, extra_params = _sector_type_filter(board_type)
    order_col = "up_count DESC, change_pct DESC" if sort_by == "up_count" else "change_pct DESC, up_count DESC"
    try:
        df = clickhouse_query_df(
            f"""
            WITH members AS (
                SELECT DISTINCT sector_type, sector_code, sector_name, canonical_code
                FROM source_sector_stocks ss
                WHERE {where_clause}
                  AND canonical_code != ''
            )
            SELECT
                m.sector_type AS sector_type,
                m.sector_code AS sector_code,
                any(m.sector_name) AS name,
                round(avg(q.change_pct), 2) AS change_pct,
                round(avg(q.price), 2) AS avg_price,
                sum(coalesce(q.volume, 0)) AS volume,
                sum(coalesce(q.amount, 0)) AS amount,
                countIf(q.change_pct > 0) AS up_count,
                countIf(q.change_pct < 0) AS down_count,
                count() AS total_count
            FROM members m
            JOIN {SNAPSHOT_TABLE} q FINAL ON q.code = m.canonical_code
            WHERE q.snapshot_ts = ?
              AND q.price > 0
            GROUP BY m.sector_type, m.sector_code
            HAVING total_count >= 5
            ORDER BY {order_col}
            LIMIT ?
            """,
            extra_params + [snapshot_ts, int(top_n)],
            raw=True,
        )
        return _format_sector_heat_rows(df)
    except Exception as exc:
        logger.warning(f"_query_sector_heat_from_intraday failed: {exc}")
        return []


def _query_sector_heat_from_formal(
    trade_date: str,
    board_type: Optional[str],
    sort_by: str,
    top_n: int,
) -> List[Dict[str, Any]]:
    if not clickhouse_available() or not clickhouse_table_exists("source_sector_stocks"):
        return []
    where_clause, extra_params = _sector_type_filter(board_type)
    order_col = "up_count DESC, change_pct DESC" if sort_by == "up_count" else "change_pct DESC, up_count DESC"
    try:
        df = clickhouse_query_df(
            f"""
            WITH members AS (
                SELECT DISTINCT sector_type, sector_code, sector_name, canonical_code
                FROM source_sector_stocks ss
                WHERE {where_clause}
                  AND canonical_code != ''
            )
            SELECT
                m.sector_type AS sector_type,
                m.sector_code AS sector_code,
                any(m.sector_name) AS name,
                round(avg(k.change_pct), 2) AS change_pct,
                round(avg(k.close), 2) AS avg_price,
                sum(coalesce(k.volume, 0)) AS volume,
                sum(coalesce(k.amount, 0)) AS amount,
                countIf(k.change_pct > 0) AS up_count,
                countIf(k.change_pct < 0) AS down_count,
                count() AS total_count
            FROM members m
            JOIN kline_daily k FINAL ON k.code = m.canonical_code
            WHERE k.trade_date = ?
              AND k.open > 0
            GROUP BY m.sector_type, m.sector_code
            HAVING total_count >= 5
            ORDER BY {order_col}
            LIMIT ?
            """,
            extra_params + [trade_date, int(top_n)],
            raw=True,
        )
        return _format_sector_heat_rows(df)
    except Exception as exc:
        logger.warning(f"_query_sector_heat_from_formal failed: {exc}")
        return []


@router.get("/sector-heat/{code}/stocks")
def get_sector_heat_stocks(code: str):
    """成分股钻取：盘中取实时采集快照，盘后取正式表 kline_daily。"""
    if not clickhouse_available() or not clickhouse_table_exists("source_sector_stocks"):
        return {"code": code, "total": 0, "stocks": [], "source": "kline_daily", "data_mode": "postclose"}

    intraday = _latest_fresh_intraday_snapshot()
    if intraday is not None:
        snapshot_ts, data_date = intraday
        try:
            df = clickhouse_query_df(
                f"""
                WITH members AS (
                    SELECT DISTINCT canonical_code
                    FROM source_sector_stocks
                    WHERE sector_code = ? AND canonical_code != ''
                )
                SELECT
                    q.code AS code,
                    coalesce(nullIf(q.name, ''), s.name, q.code) AS name,
                    q.price AS price,
                    q.change_pct AS change_pct,
                    q.volume AS volume,
                    q.amount AS amount
                FROM members m
                JOIN {SNAPSHOT_TABLE} q FINAL ON q.code = m.canonical_code
                LEFT JOIN stocks s ON s.code = q.code
                WHERE q.snapshot_ts = ?
                  AND q.price > 0
                ORDER BY q.change_pct DESC
                LIMIT 200
                """,
                [code, snapshot_ts],
                raw=True,
            )
            stocks = _format_sector_stocks(df)
            if stocks:
                return {
                    "code": code,
                    "date": data_date,
                    "data_mode": "intraday",
                    "total": len(stocks),
                    "stocks": stocks,
                    "source": SNAPSHOT_TABLE,
                }
        except Exception as exc:
            logger.warning(f"get_sector_heat_stocks intraday failed: {exc}")

    trade_date = _latest_formal_trade_date()
    if not trade_date:
        return {"code": code, "total": 0, "stocks": [], "source": "kline_daily", "data_mode": "postclose"}

    try:
        df = clickhouse_query_df(
            """
            WITH members AS (
                SELECT DISTINCT canonical_code
                FROM source_sector_stocks
                WHERE sector_code = ? AND canonical_code != ''
            )
            SELECT
                k.code AS code,
                coalesce(nullIf(s.name, ''), k.code) AS name,
                k.close AS price,
                k.change_pct AS change_pct,
                k.volume AS volume,
                k.amount AS amount
            FROM members m
            JOIN kline_daily k FINAL ON k.code = m.canonical_code
            LEFT JOIN stocks s ON s.code = k.code
            WHERE k.trade_date = ?
              AND k.open > 0
            ORDER BY k.change_pct DESC
            LIMIT 200
            """,
            [code, trade_date],
            raw=True,
        )
        stocks = _format_sector_stocks(df)
        return {
            "code": code,
            "date": trade_date,
            "data_mode": "postclose",
            "total": len(stocks),
            "stocks": stocks,
            "source": "kline_daily",
        }
    except Exception as exc:
        logger.warning(f"get_sector_heat_stocks formal failed: {exc}")
        raise HTTPException(status_code=500, detail=f"成分股查询失败: {exc}")


def _format_sector_stocks(df) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    stocks: List[Dict[str, Any]] = []
    for row in df.itertuples(index=False):
        stock_code = str(getattr(row, "code", "") or "")
        if not stock_code:
            continue
        stocks.append({
            "code": stock_code,
            "name": str(getattr(row, "name", stock_code) or stock_code),
            "price": _to_float(getattr(row, "price", None)),
            "change_pct": _to_float(getattr(row, "change_pct", None)),
            "volume": _to_float(getattr(row, "volume", None)),
            "amount": _to_float(getattr(row, "amount", None)),
        })
    return stocks


# ──────────────────────────────────────────────
# 2) 竞价异动（盘中两拍快照 CSV / 盘后正式表开盘跳空+放量）
# ──────────────────────────────────────────────

_AUCTION_RUNTIME_DIR = "l2_r56"
_AUCTION_FORMAL_CACHE: Dict[str, Dict[str, Any]] = {}
_AUCTION_FORMAL_TTL = 60.0


@router.get("/auction-anomaly")
def get_auction_anomaly(
    date: Optional[str] = Query(None, description="日期 YYYY-MM-DD，默认今日/最新交易日"),
    snap: Optional[str] = Query(None, description="快照标识：snap1_920 / snap2_924"),
):
    """竞价异动查询：盘中有竞价快照 CSV 时读取快照，盘后/无快照时回退正式表 kline_daily 开盘跳空与放量异动。"""
    target_date = date or datetime.now(BJ_TZ).strftime("%Y-%m-%d")

    snap_dir = runtime_path(_AUCTION_RUNTIME_DIR)
    snap_files: List[Path] = []

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

    if snap_files:
        result: Dict[str, Any] = {
            "date": target_date,
            "available": True,
            "source": "auction_snapshot",
            "data_mode": "intraday",
        }
        for sp in snap_files:
            key = "snap1_920" if "snap1_920" in sp.name else "snap2_924"
            records = _read_auction_csv(sp)
            result[key] = records

        snap1 = result.get("snap1_920", [])
        snap2 = result.get("snap2_924", [])
        if snap1 and snap2:
            result["anomaly_summary"] = _build_anomaly_summary(snap1, snap2)
        return result

    # 盘后或当日无竞价快照：使用正式表 kline_daily 最新交易日（或指定日期）计算开盘跳空与放量异动
    formal_date = date or _latest_formal_trade_date()
    if formal_date:
        formal_payload = _compute_formal_auction_anomaly(formal_date)
        if formal_payload is not None:
            return formal_payload

    return {
        "date": target_date,
        "available": False,
        "source": "none",
        "data_mode": "postclose",
        "message": f"{target_date} 无竞价快照（非交易日或快照未采集）",
        "snap1": [],
        "snap2": [],
    }


def _compute_formal_auction_anomaly(trade_date: str) -> Optional[Dict[str, Any]]:
    """基于正式表 kline_daily 计算指定交易日的开盘跳空（集合竞价终值）与 30 日量比异动。"""
    if not clickhouse_available():
        return None
    now = time.monotonic()
    cached = _AUCTION_FORMAL_CACHE.get(trade_date)
    if cached is not None and now < float(cached.get("expires_at") or 0.0):
        return cached["payload"]

    cal_30d = _latest_30_trade_dates(trade_date)
    if len(cal_30d) < 2:
        return None
    prev_date = cal_30d[-2]
    hist_dates = cal_30d[:-1]
    ph_hist = ",".join(["?"] * len(hist_dates))

    try:
        df = clickhouse_query_df(
            f"""
            WITH
            today_k AS (
                SELECT k.code, k.open, k.close, k.change_pct, k.volume
                FROM kline_daily k FINAL
                JOIN stocks s ON s.code = k.code
                WHERE s.type = 'stock'
                  AND k.trade_date = ?
                  AND k.open > 0
            ),
            prev_k AS (
                SELECT code, close AS prev_close
                FROM kline_daily FINAL
                WHERE trade_date = ?
                  AND close > 0
            ),
            vol_30d AS (
                SELECT code, avg(volume) AS avg_vol
                FROM kline_daily FINAL
                WHERE trade_date IN ({ph_hist})
                  AND volume > 0
                GROUP BY code
            )
            SELECT
                t.code AS code,
                coalesce(nullIf(s.name, ''), t.code) AS name,
                t.open AS open,
                t.close AS close,
                t.change_pct AS close_change_pct,
                p.prev_close AS prev_close,
                round((t.open - p.prev_close) / p.prev_close * 100, 2) AS gap_pct,
                toInt64(coalesce(t.volume, 0)) AS volume,
                if(v.avg_vol > 0, round(t.volume / v.avg_vol, 2), NULL) AS vol_ratio_30d
            FROM today_k t
            JOIN prev_k p ON p.code = t.code
            LEFT JOIN vol_30d v ON v.code = t.code
            LEFT JOIN stocks s ON s.code = t.code
            """,
            [trade_date, prev_date] + hist_dates,
            raw=True,
        )
        if df is None or df.empty:
            return None

        items: List[Dict[str, Any]] = []
        for row in df.itertuples(index=False):
            gap_pct = _to_float(getattr(row, "gap_pct", None))
            vol_ratio = _to_float(getattr(row, "vol_ratio_30d", None))
            vol = int(getattr(row, "volume", 0) or 0)
            items.append({
                "code": str(getattr(row, "code", "") or ""),
                "name": str(getattr(row, "name", "") or ""),
                "gap_pct_920": None,
                "gap_pct_924": gap_pct,
                "gap_delta": gap_pct,
                "close_change_pct": _to_float(getattr(row, "close_change_pct", None)),
                "vol_920": 0,
                "vol_924": vol,
                "vol_ratio_30d": vol_ratio,
            })

        gap_big = [m for m in items if m["gap_pct_924"] is not None and m["gap_pct_924"] >= 5.0]
        gap_mid = [m for m in items if m["gap_pct_924"] is not None and 3.0 <= m["gap_pct_924"] < 5.0]
        vol_spike = sorted(
            [m for m in items if m["vol_ratio_30d"] is not None and m["vol_ratio_30d"] >= 2.0],
            key=lambda x: x["vol_ratio_30d"] or 0.0,
            reverse=True,
        )
        top_gap_up = sorted(
            [m for m in items if m["gap_pct_924"] is not None],
            key=lambda x: abs(x["gap_pct_924"] or 0.0),
            reverse=True,
        )[:10]

        payload = {
            "date": trade_date,
            "available": True,
            "source": "kline_daily",
            "data_mode": "postclose",
            "anomaly_summary": {
                "total_merged": len(items),
                "gap_ge_5pct": len(gap_big),
                "gap_3_to_5pct": len(gap_mid),
                "vol_spike_ge_2x": len(vol_spike),
                "top_gap_up": top_gap_up,
                "top_vol_spike": vol_spike[:10],
            },
        }
        _AUCTION_FORMAL_CACHE[trade_date] = {
            "expires_at": now + _AUCTION_FORMAL_TTL,
            "payload": payload,
        }
        return payload
    except Exception as exc:
        logger.warning(f"_compute_formal_auction_anomaly({trade_date}) failed: {exc}")
        return None


def _read_auction_csv(path: Path) -> List[Dict[str, Any]]:
    """读取竞价快照 CSV（码表分离）。"""
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
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


def _build_anomaly_summary(snap1: List[Dict[str, Any]], snap2: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构建竞价异动摘要。"""
    code_map2 = {s["code"]: s for s in snap2}

    merged: List[Dict[str, Any]] = []
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
# 3) 情绪雷达 + 综合评分（盘中快照 / 盘后正式表）
# ──────────────────────────────────────────────

_SENTIMENT_RADAR_CACHE: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
_SENTIMENT_RADAR_TTL = 60.0  # 1 分钟


@router.get("/sentiment-radar")
def get_sentiment_radar():
    """六维情绪雷达 + 0-100 综合评分：盘中采用实时采集快照，盘后使用正式表 kline_daily。"""
    now = time.monotonic()
    cached = _SENTIMENT_RADAR_CACHE.get("payload")
    if cached is not None and now < _SENTIMENT_RADAR_CACHE.get("expires_at", 0.0):
        return cached

    try:
        payload = _compute_sentiment_radar()
    except Exception as exc:
        logger.error(f"sentiment-radar compute error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    if payload.get("available"):
        _SENTIMENT_RADAR_CACHE.update({"expires_at": now + _SENTIMENT_RADAR_TTL, "payload": payload})
    return payload


def _compute_sentiment_radar() -> Dict[str, Any]:
    """六维雷达计算（盘中实时快照优先，盘后自动回退正式表最新交易日）。"""
    if not clickhouse_available():
        return {"available": False, "error": "ClickHouse 不可用"}

    formal_date = _latest_formal_trade_date()
    intraday = _latest_fresh_intraday_snapshot()

    if intraday is not None:
        snapshot_ts, trade_date = intraday
        cal_30d = _latest_30_trade_dates(formal_date or trade_date)
        up_down = _dim_up_down_ratio_intraday(snapshot_ts)
        seal_rate = _dim_limit_up_seal_rate_intraday(snapshot_ts)
        board_height = _dim_max_board_height_intraday(snapshot_ts, cal_30d)
        break_rate = _build_break_rate_from_seal(seal_rate)
        ma20_breadth = _dim_ma20_breadth_intraday(snapshot_ts, cal_30d)
        turnover_pct = _dim_turnover_percentile_intraday(snapshot_ts, cal_30d)
        source = SNAPSHOT_TABLE
        data_mode = "intraday"
        as_of = snapshot_ts
    else:
        trade_date = formal_date
        if not trade_date:
            return {"available": False, "error": "无正式日线交易数据"}
        cal_30d = _latest_30_trade_dates(trade_date)
        up_down = _dim_up_down_ratio(trade_date)
        seal_rate = _dim_limit_up_seal_rate(trade_date)
        board_height = _dim_max_board_height(trade_date, cal_30d)
        break_rate = _build_break_rate_from_seal(seal_rate)
        ma20_breadth = _dim_ma20_breadth(trade_date, cal_30d)
        turnover_pct = _dim_turnover_percentile(trade_date, cal_30d)
        source = "kline_daily"
        data_mode = "postclose"
        as_of = datetime.now(BJ_TZ).isoformat(timespec="seconds")

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

    composite = sum(d["value"] * d["weight"] for d in dims.values())
    composite = round(min(100, max(0, composite)), 1)

    return {
        "date": trade_date,
        "as_of": as_of,
        "data_mode": data_mode,
        "composite_score": composite,
        "dimensions": dims,
        "weights": {k: v["weight"] for k, v in dims.items()},
        "available": True,
        "source": source,
    }


# ── 子维度计算（正式表 + 盘中快照） ─────────────────────────


def _latest_30_trade_dates(up_to: str) -> List[str]:
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


def _build_break_rate_from_seal(sr: Dict[str, Any]) -> Dict[str, Any]:
    break_raw = 1.0 - (sr["raw"] if sr["raw"] is not None else 0.5)
    score = round((1.0 - break_raw) * 100, 1)
    return {"score": score, "raw": round(break_raw, 4), "note": "approximated_from_seal_rate"}


def _dim_up_down_ratio(trade_date: str) -> Dict[str, Any]:
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


def _dim_up_down_ratio_intraday(snapshot_ts: str) -> Dict[str, Any]:
    try:
        df = clickhouse_query_df(
            f"""
            SELECT
                countIf(q.change_pct > 0) AS up,
                countIf(q.change_pct < 0) AS down,
                countIf(q.change_pct = 0) AS flat
            FROM {SNAPSHOT_TABLE} q FINAL
            WHERE q.snapshot_ts = ? AND q.price > 0
            """,
            [snapshot_ts],
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


def _dim_limit_up_seal_rate(trade_date: str) -> Dict[str, Any]:
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
        return {"seal_rate": round(ratio * 100, 1), "raw": round(ratio, 4)}
    except Exception:
        return {"seal_rate": 50.0, "raw": 0.5}


def _dim_limit_up_seal_rate_intraday(snapshot_ts: str) -> Dict[str, Any]:
    try:
        df = clickhouse_query_df(
            f"""
            SELECT
                countIf(q.change_pct >= 9.9) AS limit_up,
                countIf(q.change_pct <= -9.9) AS limit_down
            FROM {SNAPSHOT_TABLE} q FINAL
            WHERE q.snapshot_ts = ? AND q.price > 0
            """,
            [snapshot_ts],
            raw=True,
        )
        if df is None or df.empty:
            return {"seal_rate": 50.0, "raw": 0.5}
        row = df.iloc[0]
        lu = int(row.get("limit_up") or 0)
        ld = int(row.get("limit_down") or 0)
        total = lu + ld
        ratio = lu / total if total > 0 else 0.5
        return {"seal_rate": round(ratio * 100, 1), "raw": round(ratio, 4)}
    except Exception:
        return {"seal_rate": 50.0, "raw": 0.5}


def _dim_max_board_height(trade_date: str, cal_30d: List[str]) -> Dict[str, Any]:
    if not cal_30d or len(cal_30d) < 2:
        return {"score": 50.0, "height": 0}

    lookback = cal_30d[-6:]
    try:
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


def _dim_max_board_height_intraday(snapshot_ts: str, cal_30d: List[str]) -> Dict[str, Any]:
    try:
        lu_codes = clickhouse_query_df(
            f"SELECT code FROM {SNAPSHOT_TABLE} FINAL WHERE snapshot_ts = ? AND change_pct >= 9.9",
            [snapshot_ts],
            raw=True,
        )
        if lu_codes is None or lu_codes.empty:
            return {"score": 0.0, "height": 0}
        codes_list = [str(c) for c in lu_codes["code"].tolist() if c]
        if not codes_list:
            return {"score": 0.0, "height": 0}
        lookback = cal_30d[-5:]
        if not lookback:
            return {"score": round(min(100, 1 / 7.0 * 100), 1), "height": 1}
        ph_d = ",".join(["?"] * len(lookback))
        ph_c = ",".join(["?"] * len(codes_list))
        df = clickhouse_query_df(
            f"""
            SELECT code, countIf(change_pct >= 9.9) AS prev_days
            FROM kline_daily FINAL
            WHERE trade_date IN ({ph_d}) AND code IN ({ph_c})
            GROUP BY code
            """,
            lookback + codes_list,
            raw=True,
        )
        prev_max = int(df["prev_days"].max() or 0) if (df is not None and not df.empty) else 0
        max_board = prev_max + 1
        score = round(min(100, max_board / 7.0 * 100), 1)
        return {"score": score, "height": max_board}
    except Exception:
        return {"score": 0.0, "height": 0}


def _dim_ma20_breadth(trade_date: str, cal_30d: List[str]) -> Dict[str, Any]:
    """维5：宽度 MA20 占比（直接基于 kline_daily 近 20 个交易日聚合计算）。"""
    window = cal_30d[-20:]
    if len(window) < 5:
        return {"score": 50.0, "raw": 0.5}
    ph = ",".join(["?"] * len(window))
    try:
        df = clickhouse_query_df(
            f"""
            WITH stats AS (
                SELECT
                    k.code AS code,
                    argMax(k.close, k.trade_date) AS latest_close,
                    max(k.trade_date) AS max_dt,
                    avg(k.close) AS ma20,
                    count() AS bars
                FROM kline_daily k FINAL
                JOIN stocks s ON s.code = k.code
                WHERE s.type = 'stock'
                  AND k.trade_date IN ({ph})
                  AND k.close > 0
                GROUP BY k.code
                HAVING max_dt = ? AND bars >= 15
            )
            SELECT
                countIf(latest_close >= ma20 - 1e-6) AS above,
                count() AS total
            FROM stats
            """,
            window + [trade_date],
            raw=True,
        )
        if df is None or df.empty:
            return {"score": 50.0, "raw": 0.5}
        row = df.iloc[0]
        above = int(row.get("above") or 0)
        total = int(row.get("total") or 0)
        ratio = above / total if total > 0 else 0.5
        return {"score": round(ratio * 100, 1), "raw": round(ratio, 4)}
    except Exception as exc:
        logger.warning(f"_dim_ma20_breadth failed: {exc}")
        return {"score": 50.0, "raw": 0.5}


def _dim_ma20_breadth_intraday(snapshot_ts: str, cal_30d: List[str]) -> Dict[str, Any]:
    window = cal_30d[-19:]
    if len(window) < 5:
        return {"score": 50.0, "raw": 0.5}
    ph = ",".join(["?"] * len(window))
    try:
        df = clickhouse_query_df(
            f"""
            WITH hist AS (
                SELECT code, sum(close) AS sum_close, count() AS bars
                FROM kline_daily FINAL
                WHERE trade_date IN ({ph}) AND close > 0
                GROUP BY code
                HAVING bars >= 14
            )
            SELECT
                countIf(q.price >= ((h.sum_close + q.price) / (h.bars + 1)) - 1e-6) AS above,
                count() AS total
            FROM {SNAPSHOT_TABLE} q FINAL
            JOIN hist h ON h.code = q.code
            WHERE q.snapshot_ts = ? AND q.price > 0
            """,
            window + [snapshot_ts],
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
        return {"score": 50.0, "raw": 0.5}


def _dim_turnover_percentile(trade_date: str, cal_30d: List[str]) -> Dict[str, Any]:
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
        amounts = [float(a or 0) for a in df["total_amount"].tolist()]
        if not amounts:
            return {"score": 50.0, "raw": 0.5}
        today_amount = amounts[-1]
        below = sum(1 for a in amounts if a < today_amount)
        percentile = below / len(amounts)
        return {"score": round(percentile * 100, 1), "raw": round(percentile, 4)}
    except Exception:
        return {"score": 50.0, "raw": 0.5}


def _dim_turnover_percentile_intraday(snapshot_ts: str, cal_30d: List[str]) -> Dict[str, Any]:
    if not cal_30d or len(cal_30d) < 5:
        return {"score": 50.0, "raw": 0.5}
    try:
        today_amt = clickhouse_scalar(
            f"SELECT sum(coalesce(amount, 0)) FROM {SNAPSHOT_TABLE} FINAL WHERE snapshot_ts = ? AND price > 0",
            [snapshot_ts],
            raw=True,
        )
        today_amount = float(today_amt or 0)
        if today_amount <= 0:
            return {"score": 0.0, "raw": 0.0}
        ph = ",".join(["?"] * len(cal_30d))
        df = clickhouse_query_df(
            f"""
            SELECT trade_date, sum(amount) AS total_amount
            FROM kline_daily k FINAL
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock' AND k.trade_date IN ({ph}) AND k.open > 0
            GROUP BY trade_date
            """,
            cal_30d,
            raw=True,
        )
        amounts = [float(a or 0) for a in df["total_amount"].tolist()] if (df is not None and not df.empty) else []
        amounts.append(today_amount)
        below = sum(1 for a in amounts if a < today_amount)
        percentile = below / len(amounts) if amounts else 0.5
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
        return float(value)
    except (ValueError, TypeError):
        return default
