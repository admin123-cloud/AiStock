# -*- coding: utf-8 -*-
"""R55（2026-11-11）：市场宽度（Breadth）+ 涨跌分布直方图（P0 三件套 ①②）。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261111_market_breadth_r55.md

数据源（零新外部接口，全部库内）：
  - 盘后/历史宽度与分布：stock.kline_daily（raw+argMax 去重，R44 惯例）
    + stocks 主档 type='stock'（涨跌停/ST 照常含入，与 R20b/R28 既有口径一致）；
  - 盘中分布：stock.intraday_market_snapshot（R28 东财快照，5min 一拍）。

口径定义：
  - 宽度：站上 MA5/MA20/MA60 占比（close ≥ 均线−1e-6；1e-6 价格容差＝远低于
    2dp 价粒度、远高于 CH 并行求和的 FP ulp 抖动，消除恰在均线上股票的
    计数不确定性；分母=该均线可计算的股票数，即窗口内满 5/20/60 根 K 线
    且有当日 bar）；60 日新高/新低（当日 close 为
    60 交易日窗（含当日）内最高/最低，分母要求满 60 根）；高低比=新高÷新低
    （新低=0 → None）。一条聚合 SQL（build_breadth_sql）。
  - 分布：13 分桶（<-10/-10~-8/-8~-6/-6~-4/-4~-2/-2~0/0/0~2/2~4/4~6/6~8/
    8~10/>10，左闭右开、0 单列、外侧含∞）+ 平均/中位（quantileExact）。
  - 双模式：盘中=最新快照（新鲜度 ≤600s，镜像 R28 is_fresh 口径）拍全市场
    pct 分布；不新鲜/缺表 → 盘后日线模式（fail-closed，不造数）。

全部查询 raw=True 基表直查（R20b 教训：不碰 FINAL 视图）；日期谓词用裸列。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from utils.logger import get_logger
from utils.market_warehouse import (
    clickhouse_available,
    clickhouse_query_df,
    clickhouse_scalar,
    clickhouse_table_exists,
)

logger = get_logger("market_breadth")

#: 盘中快照新鲜度阈值（与 services.intraday_market_snapshot.FRESH_WINDOW_SEC 一致）
FRESH_WINDOW_SEC = 600.0
#: 轻量 TTL 缓存（历史日期结果可复用；当日结果随新快照 5min 演进）
_CACHE_TTL_SEC = 60.0

# ── 13 分桶（唯一定义，SQL 与纯函数同源）────────────────────────
#: 分界值：桶 i 覆盖 [b, next_b)，外侧桶含 ±∞；0 单列。
BUCKET_BOUNDS: tuple = (-10.0, -8.0, -6.0, -4.0, -2.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0)
BUCKET_LABELS: tuple = (
    "<-10", "-10~-8", "-8~-6", "-6~-4", "-4~-2", "-2~0", "0",
    "0~2", "2~4", "4~6", "6~8", "8~10", ">10",
)


def bucket_index(pct: Any) -> int:
    """涨跌幅 → 桶序号（0..12）。None/NaN → ValueError（fail-closed，不猜）。

    分区：(-∞,-10) [-10,-8) [-8,-6) [-6,-4) [-4,-2) [-2,0) {0} (0,2)
    [2,4) [4,6) [6,8) [8,10) [10,∞)。
    """
    if pct is None:
        raise ValueError("pct is None")
    try:
        value = float(pct)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"pct not numeric: {pct!r}") from exc
    if value != value:  # NaN
        raise ValueError("pct is NaN")
    if value == 0.0:
        return 6
    if value < BUCKET_BOUNDS[0]:
        return 0
    if value >= BUCKET_BOUNDS[-1]:
        return 12
    for i in range(len(BUCKET_BOUNDS) - 1):
        lo = BUCKET_BOUNDS[i]
        if lo <= value < BUCKET_BOUNDS[i + 1]:
            # {0} 单列占 6 号位：0 之后的区间（lo≥0）整体后移一位
            return i + 1 if lo < 0 else i + 2
    raise ValueError(f"pct unbucketed: {value}")  # pragma: no cover - 防御


def bucket_counts(pcts: Iterable[Any]) -> List[int]:
    counts = [0] * len(BUCKET_LABELS)
    for pct in pcts:
        counts[bucket_index(pct)] += 1
    return counts


def _bucket_countif_expressions() -> List[str]:
    """与 bucket_index 完全同源的 SQL countIf 表达式（按桶序）。"""
    exprs = ["change_pct < -10"]
    bounds = list(BUCKET_BOUNDS)
    for i, lo in enumerate(bounds[:-1]):
        hi = bounds[i + 1]
        if lo == 0.0:
            # {0} 与 (0,2) 两桶
            exprs.append(f"change_pct = 0")
            exprs.append(f"change_pct > 0 AND change_pct < 2")
        else:
            exprs.append(f"change_pct >= {lo:g} AND change_pct < {hi:g}")
    exprs.append("change_pct >= 10")
    assert len(exprs) == 13
    return exprs


# ── 宽度聚合 SQL ───────────────────────────────────────────────

def build_breadth_sql() -> str:
    """kline_daily 一条聚合 SQL：60 交易日窗 + MA5/20/60 + 60日新高新低。

    raw+argMax(close, created_at) 去重（R44 惯例）；窗口日期 row_number()
    编号（rn=0 为查询日）；仅统计当日有 bar 的股票（c0 > 0）。
    """
    return """
WITH
    dates AS (
        SELECT trade_date, row_number() OVER (ORDER BY trade_date DESC) - 1 AS rn
        FROM (SELECT DISTINCT trade_date FROM stock.kline_daily
              WHERE trade_date <= {d0} ORDER BY trade_date DESC LIMIT 60)
    ),
    bars AS (
        SELECT k.code AS code, d.rn AS rn,
               argMax(k.close, k.created_at) AS close
        FROM stock.kline_daily AS k
        INNER JOIN dates AS d ON d.trade_date = k.trade_date
        WHERE k.code IN (SELECT code FROM stock.stocks WHERE type = 'stock')
        GROUP BY k.code, d.rn
        HAVING close > 0
    ),
    per_stock AS (
        SELECT code,
               maxIf(close, rn = 0) AS c0,
               countIf(rn < 5) AS n5,  sumIf(close, rn < 5) AS s5,
               countIf(rn < 20) AS n20, sumIf(close, rn < 20) AS s20,
               countIf(rn < 60) AS n60, sumIf(close, rn < 60) AS s60,
               max(close) AS hh60, min(close) AS lo60
        FROM bars GROUP BY code
    )
SELECT
    countIf(c0 > 0) AS total,
    countIf(n5 = 5 AND c0 > 0) AS ma5_base,
    countIf(n5 = 5 AND c0 > 0 AND c0 >= s5 / 5 - 1e-6) AS above_ma5,
    countIf(n20 = 20 AND c0 > 0) AS ma20_base,
    countIf(n20 = 20 AND c0 > 0 AND c0 >= s20 / 20 - 1e-6) AS above_ma20,
    countIf(n60 = 60 AND c0 > 0) AS ma60_base,
    countIf(n60 = 60 AND c0 > 0 AND c0 >= s60 / 60 - 1e-6) AS above_ma60,
    countIf(n60 = 60 AND c0 > 0 AND c0 >= hh60) AS new_high_60d,
    countIf(n60 = 60 AND c0 > 0 AND c0 <= lo60) AS new_low_60d
FROM per_stock
"""


def parse_breadth_row(row: Any, trade_date: str) -> Dict[str, Any]:
    """聚合行 → 宽度 payload（纯函数，分母 0 → None，不猜）。"""
    def _int(name: str) -> int:
        return int(getattr(row, name) if hasattr(row, name) else (row.get(name) or 0))

    def _pct(num: int, base: int) -> Optional[float]:
        return round(num / base * 100, 2) if base > 0 else None

    new_high = _int("new_high_60d")
    new_low = _int("new_low_60d")
    hl_ratio = round(new_high / new_low, 2) if new_low > 0 else None
    return {
        "date": trade_date,
        "total": _int("total"),
        "above_ma5_pct": _pct(_int("above_ma5"), _int("ma5_base")),
        "above_ma20_pct": _pct(_int("above_ma20"), _int("ma20_base")),
        "above_ma60_pct": _pct(_int("above_ma60"), _int("ma60_base")),
        "base_counts": {
            "ma5": _int("ma5_base"), "ma20": _int("ma20_base"),
            "ma60": _int("ma60_base"),
        },
        "new_high_60d": new_high,
        "new_low_60d": new_low,
        "hl_ratio": hl_ratio,
        "source": "clickhouse:kline_daily",
    }


_breadth_cache: Dict[str, Dict[str, Any]] = {}


def _resolve_latest_and_check(trade_date: Optional[str]):
    """轻量预检：返回 (effective_date, err_payload)。ok 时 err=None。

    双列结果必须走 clickhouse_query_df（clickhouse_scalar 只回首列单值）。
    """
    try:
        if trade_date:
            pre_df = clickhouse_query_df(
                "SELECT countIf(trade_date = ?) AS cnt, max(trade_date) AS latest "
                "FROM stock.kline_daily",
                [trade_date], raw=True,
            )
        else:
            pre_df = clickhouse_query_df(
                "SELECT count() AS cnt, max(trade_date) AS latest "
                "FROM stock.kline_daily", raw=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"breadth precheck failed: {exc}")
        return None, {"error": f"precheck_failed:{exc}"}
    if pre_df is None or pre_df.empty:
        return None, {"error": "precheck_malformed"}
    try:
        row = pre_df.iloc[0]
        rows_cnt = int(row.get("cnt") or 0)
        latest = str(row.get("latest"))[:10] if row.get("latest") is not None else None
    except (TypeError, ValueError, IndexError):
        return None, {"error": "precheck_malformed"}
    if trade_date:
        if rows_cnt <= 0:
            return None, {"error": "no_data_for_date", "date": trade_date,
                          "latest_trade_date": latest}
        return trade_date, None
    if not latest:
        return None, {"error": "kline_daily_empty"}
    return latest, None


def market_breadth(trade_date: Optional[str] = None) -> Dict[str, Any]:
    """市场宽度（历史任一日可查；默认=最新交易日）。fail-closed 返回 error payload。"""
    if not clickhouse_available():
        return {"error": "clickhouse_unavailable"}
    effective, err = _resolve_latest_and_check(trade_date)
    if err is not None:
        return err

    cache_key = f"breadth|{effective}"
    now = time.monotonic()
    cached = _breadth_cache.get(cache_key)
    if cached and now < cached.get("expires_at", 0.0):
        return cached["payload"]

    sql = build_breadth_sql().replace("{d0}", f"'{effective}'")
    try:
        df = clickhouse_query_df(sql, raw=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"breadth aggregation failed: {exc}")
        return {"error": f"aggregation_failed:{exc}"}
    if df is None or df.empty:
        return {"error": "no_data_for_date", "date": effective}

    payload = parse_breadth_row(df.iloc[0], effective)
    _breadth_cache[cache_key] = {"expires_at": now + _CACHE_TTL_SEC, "payload": payload}
    return payload


# ── 涨跌分布直方图（双模式）───────────────────────────────────

def build_distribution_sql() -> str:
    """分布聚合 SQL 模板（kline_daily / 快照两用，列名 change_pct）。

    中位数用 quantileExact（全市场 ~5.5k 行量级，精确分位无压力）。
    """
    exprs = _bucket_countif_expressions()
    bucket_cols = ",\n    ".join(
        f"countIf({expr}) AS bucket_{i}" for i, expr in enumerate(exprs)
    )
    return f"""
SELECT
    {bucket_cols},
    avg(change_pct) AS avg_pct,
    quantileExact(0.5)(change_pct) AS median_pct,
    count() AS covered
"""


def _buckets_payload(row: Any) -> List[Dict[str, Any]]:
    return [
        {"label": label, "count": int(row.get(f"bucket_{i}") or 0),
         "side": "down" if i < 6 else ("flat" if i == 6 else "up")}
        for i, label in enumerate(BUCKET_LABELS)
    ]


def _parse_beijing(value: Any) -> Optional[datetime]:
    from services.intraday_market_snapshot import _coerce_beijing_dt

    return _coerce_beijing_dt(value)


def _snapshot_fresh(ts: Any, now: Optional[datetime]) -> bool:
    from services.intraday_market_snapshot import is_fresh

    return is_fresh(ts, now)


def advance_decline_distribution(
    trade_date: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """涨跌分布（13 桶 + 平均/中位）。盘中快照新鲜 → intraday；否则日线。

    显式 trade_date → 一律日线口径（历史分布）。
    """
    if not clickhouse_available():
        return {"error": "clickhouse_unavailable"}

    if not trade_date:
        intraday = _distribution_intraday(now)
        if intraday is not None:
            return intraday
    return _distribution_daily(trade_date)


def _distribution_intraday(now: Optional[datetime]) -> Optional[Dict[str, Any]]:
    from services.intraday_market_snapshot import (
        SNAPSHOT_TABLE,
        _coerce_beijing_dt,
        is_fresh,
    )

    if not clickhouse_table_exists(SNAPSHOT_TABLE):
        return None
    sql = build_distribution_sql() + f""",
    max(snapshot_ts) AS latest_ts
FROM {SNAPSHOT_TABLE} AS k
INNER JOIN stock.stocks AS s ON s.code = k.code
WHERE s.type = 'stock'
  AND k.price > 0
  AND k.snapshot_ts = (
      SELECT max(snapshot_ts) FROM {SNAPSHOT_TABLE}
      WHERE snapshot_ts >= now('Asia/Shanghai') - INTERVAL 2 DAY
  )
"""
    try:
        df = clickhouse_query_df(sql, raw=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"intraday distribution query failed: {exc}")
        return None
    if df is None or df.empty:
        return None
    row = df.iloc[0]
    if not is_fresh(row.get("latest_ts"), now):
        return None
    latest_dt = _coerce_beijing_dt(row.get("latest_ts"))
    return {
        "mode": "intraday",
        "date": latest_dt.strftime("%Y-%m-%d") if latest_dt else None,
        "as_of": latest_dt.strftime("%Y-%m-%d %H:%M") if latest_dt else None,
        "buckets": _buckets_payload(row),
        "avg_change_pct": round(float(row.get("avg_pct") or 0.0), 3),
        "median_change_pct": round(float(row.get("median_pct") or 0.0), 3),
        "covered": int(row.get("covered") or 0),
        "source": "eastmoney:intraday_market_snapshot",
    }


def _distribution_daily(trade_date: Optional[str]) -> Dict[str, Any]:
    try:
        if trade_date:
            date_text = str(trade_date)[:10]
            exists = clickhouse_scalar(
                "SELECT count() FROM stock.kline_daily WHERE trade_date = ?",
                [date_text], raw=True,
            )
            if not exists:
                return {"error": "no_data_for_date", "date": date_text}
        else:
            latest = clickhouse_scalar(
                "SELECT max(trade_date) FROM stock.kline_daily", raw=True)
            if latest is None:
                return {"error": "kline_daily_empty"}
            date_text = str(latest)[:10]
    except Exception as exc:  # noqa: BLE001
        return {"error": f"date_resolve_failed:{exc}"}

    # raw+argMax 去重（R44）：先按 code 聚当日最新版本再分桶
    sql = (f"""WITH dedup AS (
    SELECT k.code AS code, argMax(k.change_pct, k.created_at) AS change_pct
    FROM stock.kline_daily AS k
    INNER JOIN stock.stocks AS s ON s.code = k.code
    WHERE s.type = 'stock' AND k.trade_date = '{date_text}'
    GROUP BY k.code
)
""" + build_distribution_sql() + "\nFROM dedup")
    try:
        df = clickhouse_query_df(sql, raw=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"daily distribution query failed: {exc}")
        return {"error": f"query_failed:{exc}"}
    if df is None or df.empty:
        return {"error": "no_data_for_date", "date": date_text}
    row = df.iloc[0]
    return {
        "mode": "daily",
        "date": date_text,
        "as_of": None,
        "buckets": _buckets_payload(row),
        "avg_change_pct": round(float(row.get("avg_pct") or 0.0), 3),
        "median_change_pct": round(float(row.get("median_pct") or 0.0), 3),
        "covered": int(row.get("covered") or 0),
        "source": "clickhouse:kline_daily",
    }


# ── regime 09:25 水位确认因子（观察期，只标注不改判定）──────────

_BREADTH_STRONG_MIN = 60.0
_BREADTH_WEAK_MAX = 40.0


def breadth_level(breadth: Optional[Dict[str, Any]]) -> str:
    """宽度水位：above_ma20_pct ≥60 strong / ≤40 weak / 其余 neutral。"""
    if not breadth:
        return "unknown"
    pct = breadth.get("above_ma20_pct")
    if pct is None:
        return "unknown"
    if pct >= _BREADTH_STRONG_MIN:
        return "strong"
    if pct <= _BREADTH_WEAK_MAX:
        return "weak"
    return "neutral"


def breadth_divergence(regime: str, breadth: Dict[str, Any]) -> Optional[str]:
    """周期态与市场宽度的背离标注（观察期提示，不参与任何判定）。

    - up 周期 × 宽度弱 → breadth_weak_vs_up（个股升势缺市场宽度佐证）
    - down 周期 × 宽度强 → breadth_strong_vs_down
    - sideways / 无背离 → None
    """
    level = breadth_level(breadth)
    if regime == "up" and level == "weak":
        return "breadth_weak_vs_up"
    if regime == "down" and level == "strong":
        return "breadth_strong_vs_down"
    return None


def breadth_water_annotation(
    breadth: Optional[Dict[str, Any]],
    regimes: Dict[str, str],
) -> Dict[str, Any]:
    """组装 regime_<date>.json 的 breadth 键。breadth 不可用 → status=unavailable。"""
    if not breadth or "error" in breadth or breadth.get("above_ma20_pct") is None:
        return {"status": "unavailable",
                "note": "宽度不可用（fail-closed），未标注；观察期，不改判定式"}
    divergence = {
        code: note
        for code, regime in (regimes or {}).items()
        if (note := breadth_divergence(regime, breadth)) is not None
    }
    return {
        "status": "ok",
        "date": breadth.get("date"),
        "above_ma5_pct": breadth.get("above_ma5_pct"),
        "above_ma20_pct": breadth.get("above_ma20_pct"),
        "above_ma60_pct": breadth.get("above_ma60_pct"),
        "new_high_60d": breadth.get("new_high_60d"),
        "new_low_60d": breadth.get("new_low_60d"),
        "hl_ratio": breadth.get("hl_ratio"),
        "level": breadth_level(breadth),
        "divergence": divergence,
        "note": "水位确认因子（观察期）：只标注 divergence，不改判定式；"
                "宽度=市场整体 MA20 站上比，与个股周期态对照。",
    }
