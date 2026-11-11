# -*- coding: utf-8 -*-
"""R55（2026-11-11）：涨停生态聚合（情绪温度计，P0 三件套 ③）。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261111_market_breadth_r55.md

口径定义：
  - 涨停价 = 昨收 × (1 + 板块规则比例)，四舍五入到分（ROUND_HALF_UP，Decimal；
    交易所口径）；板块比例复用 R20b 分类器（api.market_overview.
    _limit_up_threshold / R28 SQL 同规则）：ST 5% < 北交所 30% <
    创业板(300/301)/科创板(688) 20% < 其余主板 10%，ST 判定优先。
  - 曾涨停 = 当日最高价触及过涨停价（盘中口径=当日各快照最大价，5min 粒度，
    极短暂触板可能漏记——盘后终算用 kline_daily.high 精确覆盖）；
  - 现仍封板 = 现价（盘中=最新快照价 / 盘后=收盘价）≥ 涨停价；
  - 封板率 = 现仍封板 ÷ 曾涨停（曾=0 → None）；炸板数 = 曾涨停 − 现仍封板；
  - 连板 = 连续收盘封板交易日数（盘中再 +1 当实时仍封）；
  - 梯队表 = streak 分桶 + 数量 + 代表股（当日成交额降序 top N）。

数据源（零新外部接口）：
  - 盘中：stock.intraday_market_snapshot（R28 东财快照）+ kline_daily 历史
    连板（快照采集后追加计算，pipeline_scheduler intraday 节拍）；
  - 盘后终算：kline_daily 当日（16:40 finalize 节拍覆盖）。
产物：runtime/market_mood/YYYYMMDD.json（realtime/final 双键，原子写）。
全部查询 raw=True 基表直查（R20b 教训）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils.logger import get_logger
from utils.market_warehouse import (
    clickhouse_available,
    clickhouse_query_df,
    clickhouse_table_exists,
)
from utils.paths import runtime_path

logger = get_logger("market_mood")

SCHEMA_MOOD = "market_mood/1"
SCHEMA_DAY_FILE = "market_mood_day/1"
#: 盘中快照新鲜度（与 R28 FRESH_WINDOW_SEC 一致）
FRESH_WINDOW_SEC = 600.0
#: 连板回溯窗口（交易日数；与既有 limit-up-ladder max 20 板对齐 + 余量）
STREAK_LOOKBACK_DATES = 21
#: 梯队代表股每档最多展示
LADDER_TOP_N = 5
#: 候选预筛松弛（比例下限 5% - 0.1%；精确分类在 Python Decimal 完成）
_CANDIDATE_SLACK = 1.049

BJ = "Asia/Shanghai"


# ── 板块规则分类器（R20b 口径，纯函数）────────────────────────

def limit_ratio(code: str, name: str) -> float:
    """板块涨停比例：ST 5% / 北交所 30% / 创业板·科创板 20% / 其余 10%。

    与 api.market_overview._limit_up_threshold（ST 4.8/BJ 29.8/20cm 19.8/
    9.8）同一判定顺序与同一代码前缀集合；ST 判定优先（含 *ST）。
    """
    code = str(code or "").upper()
    name = str(name or "").upper()
    if name.startswith("ST") or name.startswith("*ST"):
        return 0.05
    if code.endswith(".BJ"):
        return 0.30
    if code.startswith(("688", "300", "301")):
        return 0.20
    return 0.10


def _round_price(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def limit_up_price(prev_close: float, ratio: float) -> float:
    """涨停价 = 昨收 × (1+比例)，四舍五入到分（ROUND_HALF_UP）。"""
    if prev_close is None or float(prev_close) <= 0:
        raise ValueError(f"invalid prev_close: {prev_close!r}")
    return float((Decimal(str(prev_close)) * (Decimal("1") + Decimal(str(ratio))))
                 .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_down_price(prev_close: float, ratio: float) -> float:
    """跌停价 = 昨收 × (1−比例)，四舍五入到分。"""
    if prev_close is None or float(prev_close) <= 0:
        raise ValueError(f"invalid prev_close: {prev_close!r}")
    return float((Decimal(str(prev_close)) * (Decimal("1") - Decimal(str(ratio))))
                 .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ── 个股分类 / 封板率口径（纯函数）────────────────────────────

def classify_stock(rec: Dict[str, Any]) -> Dict[str, Any]:
    """单股分类。rec: code/name/prev_close/last_price/day_high/day_low/amount。

    比较按 2 位小数（Decimal ROUND_HALF_UP），消除快照浮点噪声。
    """
    code = str(rec.get("code") or "")
    name = str(rec.get("name") or "")
    prev_close = float(rec.get("prev_close") or 0.0)
    ratio = limit_ratio(code, name)
    up = Decimal(str(limit_up_price(prev_close, ratio)))
    down = Decimal(str(limit_down_price(prev_close, ratio)))
    last = _round_price(rec.get("last_price") or 0.0)
    high = _round_price(rec.get("day_high") or 0.0)
    out = dict(rec)
    out.update({
        "limit_ratio": ratio,
        "limit_up_price": float(up),
        "limit_down_price": float(down),
        "at_limit_up": last >= up,
        "touched_limit_up": high >= up,
        "at_limit_down": last <= down,
    })
    return out


def summarize_limit_stats(classified: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总：涨停数（现仍封）/跌停数/曾涨停/炸板数/封板率。"""
    limit_up = sum(1 for c in classified if c.get("at_limit_up"))
    limit_down = sum(1 for c in classified if c.get("at_limit_down"))
    ever = sum(1 for c in classified if c.get("touched_limit_up"))
    broke = ever - limit_up
    seal_rate = round(limit_up / ever, 4) if ever > 0 else None
    return {
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "ever_limit_up": ever,
        "broke_count": broke,
        "seal_rate": seal_rate,
    }


# ── 连板计数（纯函数）─────────────────────────────────────────

def compute_streak(
    hist_rows: Sequence[Dict[str, Any]],
    today_at_limit: Optional[bool] = None,
) -> int:
    """连续收盘封板交易日数。hist_rows 由旧到新（每行 close/prev_close/code/name）。

    today_at_limit=True（盘中实时仍封）→ 历史 streak + 1；None/False 不加。
    """
    streak = 0
    for row in reversed(list(hist_rows)):
        close = _round_price(row.get("close") or 0.0)
        prev = float(row.get("prev_close") or 0.0)
        if prev <= 0:
            break
        up = Decimal(str(limit_up_price(prev, limit_ratio(
            row.get("code") or "", row.get("name") or ""))))
        if close >= up:
            streak += 1
        else:
            break
    if today_at_limit:
        streak += 1
    return streak


# ── 梯队表（纯函数）───────────────────────────────────────────

def build_ladder(
    pairs: Sequence[Tuple[Dict[str, Any], int]],
    *,
    max_streak: int = 10,
    top_n: int = LADDER_TOP_N,
) -> List[Dict[str, Any]]:
    """pairs: (classify_stock 结果, streak)。返回非空梯队（streak 降序）。"""
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    for classified, streak in pairs:
        bucket = min(int(streak), max_streak)
        prev = float(classified.get("prev_close") or 0.0)
        last = float(classified.get("last_price") or 0.0)
        pct = round((last / prev - 1) * 100, 2) if prev > 0 and last > 0 else None
        buckets.setdefault(bucket, []).append({
            "code": classified.get("code"),
            "name": classified.get("name"),
            "close": round(last, 2),
            "change_pct": pct,
            "amount": float(classified.get("amount") or 0.0),
        })
    rows: List[Dict[str, Any]] = []
    for streak in sorted(buckets, reverse=True):
        items = sorted(
            buckets[streak], key=lambda r: r.get("amount") or 0.0, reverse=True)
        rows.append({
            "streak": streak,
            "label": f"{streak}板",
            "count": len(items),
            "reps": items[:top_n],
        })
    return rows


# ── runtime/market_mood/YYYYMMDD.json（原子写，realtime/final 双键）──

def _day_dir(out_root: Optional[Any] = None) -> Any:
    if out_root is not None:
        path = Path(out_root) / "market_mood"
    else:
        path = runtime_path("market_mood")
    path.mkdir(parents=True, exist_ok=True)
    return path


def day_path(day: str, out_root: Optional[Any] = None) -> Any:
    return _day_dir(out_root) / f"{day}.json"


def write_day_payload(
    day: str,
    *,
    realtime: Optional[Dict[str, Any]] = None,
    final: Optional[Dict[str, Any]] = None,
    out_root: Optional[Any] = None,
) -> None:
    """合并写（保留另一键的历史）；tmp + os.replace 原子落盘。"""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    path = day_path(day, out_root)
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 损坏则重建（保留 realtime/final 入参为准）
            existing = {}
    payload = dict(existing)
    payload.update({
        "schema": SCHEMA_DAY_FILE,
        "date": f"{day[:4]}-{day[4:6]}-{day[6:8]}",
        "updated_at": _dt.now(ZoneInfo(BJ)).isoformat(),
    })
    if realtime is not None:
        payload["realtime"] = realtime
    if final is not None:
        payload["final"] = final
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2,
                   default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_day_payload(day: str, out_root: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    path = day_path(day, out_root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ── 盘中计算（快照采集后追加）─────────────────────────────────

def _now_beijing() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(BJ))


def _snapshot_is_fresh(ts: Any, now: Optional[datetime]) -> bool:
    from services.intraday_market_snapshot import is_fresh

    return is_fresh(ts, now)


def _candidate_rows_intraday(day_text: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """当日快照聚合候选（曾触板/触跌停），SQL 宽筛 + Python 精筛。"""
    from services.intraday_market_snapshot import SNAPSHOT_TABLE

    sql = f"""
WITH day_agg AS (
    SELECT
        k.code AS code,
        argMax(k.name, k.created_at) AS name,
        argMax(k.prev_close, k.created_at) AS prev_close,
        argMax(k.price, k.created_at) AS last_price,
        max(k.price) AS day_high,
        min(k.price) AS day_low,
        argMax(k.amount, k.created_at) AS amount
    FROM {SNAPSHOT_TABLE} AS k
    INNER JOIN stock.stocks AS s ON s.code = k.code
    WHERE s.type = 'stock'
      AND toDate(k.snapshot_ts) = '{day_text}'
      AND k.price > 0 AND k.prev_close > 0
    GROUP BY k.code
)
SELECT code, name, prev_close, last_price, day_high, day_low, amount
FROM day_agg
WHERE day_high >= prev_close * {_CANDIDATE_SLACK}
   OR day_low <= prev_close * (2 - {_CANDIDATE_SLACK})
"""
    try:
        df = clickhouse_query_df(sql, raw=True)
    except Exception as exc:  # noqa: BLE001
        return None, f"candidate_query_failed:{exc}"
    if df is None or df.empty:
        return [], None
    return df.to_dict("records"), None


def _covered_count_intraday(day_text: str) -> int:
    from services.intraday_market_snapshot import SNAPSHOT_TABLE

    try:
        df = clickhouse_query_df(
            f"SELECT uniqExact(code) AS cnt FROM {SNAPSHOT_TABLE} "
            f"WHERE toDate(snapshot_ts) = '{day_text}'",
            raw=True,
        )
        if df is None or df.empty:
            return 0
        return int(df.iloc[0].get("cnt") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"covered count query failed: {exc}")
        return 0


def _latest_snapshot_ts(day_text: str) -> Optional[Any]:
    from services.intraday_market_snapshot import SNAPSHOT_TABLE

    try:
        df = clickhouse_query_df(
            f"SELECT max(snapshot_ts) AS latest_ts FROM {SNAPSHOT_TABLE} "
            f"WHERE toDate(snapshot_ts) = '{day_text}'",
            raw=True,
        )
        if df is None or df.empty:
            return None
        return df.iloc[0].get("latest_ts")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"latest snapshot ts query failed: {exc}")
        return None


def _daily_history_rows(
    codes: Sequence[str],
    *,
    before_date: Optional[str] = None,
    through_date: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """codes 的近 STREAK_LOOKBACK_DATES 根日线（raw+argMax 去重），由旧到新。

    before_date：窗口 = < before_date（盘中连板历史，不含当日）；
    through_date：窗口 = ≤ through_date（盘后终算，含当日）。
    每行附 prev_close（前一交易日收盘，用于涨停价计算）。
    """
    if not codes:
        return {}
    bound = before_date if before_date else through_date
    op = "<" if before_date else "<="
    code_list = ",".join("'" + str(c).replace("'", "") + "'" for c in codes)
    sql = f"""
WITH dates AS (
    SELECT DISTINCT trade_date FROM stock.kline_daily
    WHERE trade_date {op} '{bound}'
    ORDER BY trade_date DESC LIMIT {STREAK_LOOKBACK_DATES}
),
bars AS (
    SELECT k.code AS code, k.trade_date AS trade_date,
           argMax(k.close, k.created_at) AS close
    FROM stock.kline_daily AS k
    WHERE k.code IN ({code_list})
      AND k.trade_date IN (SELECT trade_date FROM dates)
    GROUP BY k.code, k.trade_date
)
SELECT code, trade_date, close FROM bars ORDER BY code, trade_date
"""
    try:
        df = clickhouse_query_df(sql, raw=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"daily history query failed: {exc}")
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    if df is None or df.empty:
        return out
    for row in df.to_dict("records"):
        out.setdefault(str(row.get("code")), []).append({
            "code": row.get("code"),
            "trade_date": str(row.get("trade_date"))[:10],
            "close": row.get("close"),
        })
    # 每行补 prev_close（前一根收盘；首根无前收 → 0，streak 在此断开）
    for rows in out.values():
        for i, row in enumerate(rows):
            row["prev_close"] = rows[i - 1]["close"] if i > 0 else 0.0
        # 名称取末行 code 对应（调用方有 name 时会覆盖 ratio 判定输入）
    return out


def compute_intraday_mood(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """盘中涨停生态（快照采集后追加计算）。不可用 → None（fail-closed）。"""
    from services.intraday_market_snapshot import SNAPSHOT_TABLE

    if not clickhouse_available() or not clickhouse_table_exists(SNAPSHOT_TABLE):
        return None
    now = now or _now_beijing()
    day_text = now.strftime("%Y-%m-%d")
    latest_ts = _latest_snapshot_ts(day_text)
    if not _snapshot_is_fresh(latest_ts, now):
        logger.info("market_mood intraday: snapshot stale/missing, skip")
        return None
    candidates, err = _candidate_rows_intraday(day_text)
    if err is not None:
        logger.warning(f"market_mood intraday: {err}")
        return None
    classified = [classify_stock(rec) for rec in candidates]
    at_limit = [c for c in classified if c.get("at_limit_up")]
    # 连板历史（不含当日）：codes 去重
    names = {str(c.get("code")): str(c.get("name") or "") for c in classified}
    hist = _daily_history_rows(
        [str(c.get("code")) for c in at_limit], before_date=day_text)
    pairs: List[Tuple[Dict[str, Any], int]] = []
    max_streak = 0
    for c in at_limit:
        code = str(c.get("code"))
        rows = [dict(r, code=code, name=names.get(code, "")) for r in hist.get(code, [])]
        streak = compute_streak(rows, today_at_limit=True)
        max_streak = max(max_streak, streak)
        pairs.append((c, streak))
    summary = summarize_limit_stats(classified)
    from services.intraday_market_snapshot import _coerce_beijing_dt

    latest_dt = _coerce_beijing_dt(latest_ts)
    return {
        "schema": SCHEMA_MOOD,
        "date": day_text,
        "mode": "intraday",
        "as_of": latest_dt.strftime("%Y-%m-%d %H:%M") if latest_dt else None,
        **summary,
        "max_streak": max_streak,
        "ladder": build_ladder(pairs),
        "total_covered": _covered_count_intraday(day_text),
        "source": "eastmoney:intraday_market_snapshot+kline_daily",
        "note": "曾涨停=当日快照最大价触及涨停价（5min 粒度，极短触板可能漏记）；"
                "盘后终算以日线 high 覆盖。",
    }


# ── 盘后终算（kline_daily）────────────────────────────────────

def _candidate_rows_final(day_text: str, prev_text: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    sql = f"""
WITH today AS (
    SELECT k.code AS code,
           argMax(k.close, k.created_at) AS close,
           argMax(k.high, k.created_at) AS high,
           argMax(k.amount, k.created_at) AS amount
    FROM stock.kline_daily AS k
    WHERE k.trade_date = '{day_text}'
    GROUP BY k.code
),
prev AS (
    SELECT k.code AS code, argMax(k.close, k.created_at) AS close
    FROM stock.kline_daily AS k
    WHERE k.trade_date = '{prev_text}'
    GROUP BY k.code
)
SELECT t.code AS code, s.name AS name, t.close AS last_price,
       t.high AS day_high, t.close AS day_low, t.amount AS amount,
       p.close AS prev_close
FROM today AS t
INNER JOIN prev AS p ON p.code = t.code
INNER JOIN stock.stocks AS s ON s.code = t.code
WHERE s.type = 'stock'
  AND t.close > 0 AND p.close > 0
  AND (t.high >= p.close * {_CANDIDATE_SLACK}
       OR t.close <= p.close * (2 - {_CANDIDATE_SLACK}))
"""
    try:
        df = clickhouse_query_df(sql, raw=True)
    except Exception as exc:  # noqa: BLE001
        return None, f"candidate_query_failed:{exc}"
    if df is None or df.empty:
        return [], None
    return df.to_dict("records"), None


def compute_final_mood(trade_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """盘后日线终算（收盘封板口径，覆盖盘中 realtime）。无数据 → None。"""
    if not clickhouse_available():
        return None
    try:
        if trade_date:
            day_text = str(trade_date)[:10]
            exists = clickhouse_query_df(
                "SELECT count() AS cnt FROM stock.kline_daily WHERE trade_date = ?",
                [day_text], raw=True,
            )
            if exists is None or exists.empty or int(exists.iloc[0].get("cnt") or 0) <= 0:
                return None  # 非交易日/无数据 → fail-closed，不写全零 final
        else:
            latest = clickhouse_query_df(
                "SELECT max(trade_date) AS d FROM stock.kline_daily", raw=True)
            if latest is None or latest.empty:
                return None
            day_text = str(latest.iloc[0].get("d"))[:10]
        prev_df = clickhouse_query_df(
            "SELECT max(trade_date) AS d FROM stock.kline_daily WHERE trade_date < ?",
            [day_text], raw=True,
        )
        if prev_df is None or prev_df.empty or prev_df.iloc[0].get("d") is None:
            return None
        prev_text = str(prev_df.iloc[0].get("d"))[:10]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"market_mood final date resolve failed: {exc}")
        return None

    candidates, err = _candidate_rows_final(day_text, prev_text)
    if err is not None:
        logger.warning(f"market_mood final: {err}")
        return None
    classified = [classify_stock(rec) for rec in candidates]
    closed_at_limit = [c for c in classified if c.get("at_limit_up")]
    names = {str(c.get("code")): str(c.get("name") or "") for c in classified}
    hist = _daily_history_rows(
        [str(c.get("code")) for c in closed_at_limit], through_date=day_text)
    pairs: List[Tuple[Dict[str, Any], int]] = []
    max_streak = 0
    for c in closed_at_limit:
        code = str(c.get("code"))
        rows = [dict(r, code=code, name=names.get(code, "")) for r in hist.get(code, [])]
        streak = compute_streak(rows)
        max_streak = max(max_streak, streak)
        pairs.append((c, streak))
    summary = summarize_limit_stats(classified)
    covered = 0
    try:
        cov_df = clickhouse_query_df(
            "SELECT uniqExact(k.code) AS cnt FROM stock.kline_daily AS k "
            "INNER JOIN stock.stocks AS s ON s.code = k.code "
            "WHERE s.type = 'stock' AND k.trade_date = ?",
            [day_text], raw=True,
        )
        if cov_df is not None and not cov_df.empty:
            covered = int(cov_df.iloc[0].get("cnt") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"final covered count failed: {exc}")
    return {
        "schema": SCHEMA_MOOD,
        "date": day_text,
        "mode": "final",
        "as_of": None,
        **summary,
        "max_streak": max_streak,
        "ladder": build_ladder(pairs),
        "total_covered": covered,
        "source": "clickhouse:kline_daily",
        "note": "收盘封板口径：现仍封=收盘价≥涨停价；曾涨停=当日 high 触及；"
                "连板=连续收盘封板交易日。",
    }


# ── API 侧模式选择 ────────────────────────────────────────────

def _parse_as_of(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    from zoneinfo import ZoneInfo

    try:
        return datetime.strptime(str(text)[:16], "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo(BJ))
    except ValueError:
        return None


def limit_up_stats(
    date: Optional[str] = None,
    now: Optional[datetime] = None,
    out_root: Optional[Any] = None,
) -> Dict[str, Any]:
    """GET /market/limit-up-stats：盘中=runtime JSON realtime（新鲜）；盘后=final。

    runtime JSON 缺失 → 按需日线终算（source=on_demand_daily）；
    全部不可用 → error payload（fail-closed，不造数）。
    """
    from zoneinfo import ZoneInfo

    now = now or datetime.now(ZoneInfo(BJ))
    if date:
        day = str(date)[:10].replace("-", "")
        payload = load_day_payload(day, out_root)
        final = (payload or {}).get("final")
        if final and final.get("date") == str(date)[:10]:
            return final
        mood = compute_final_mood(date)
        if mood is None:
            return {"error": "no_data_for_date", "date": date}
        return {**mood, "source": "on_demand_daily"}

    today_compact = now.strftime("%Y%m%d")
    payload = load_day_payload(today_compact, out_root) or {}
    realtime = payload.get("realtime")
    if realtime and realtime.get("date") == now.strftime("%Y-%m-%d"):
        as_of = _parse_as_of(realtime.get("as_of"))
        if as_of and (now - as_of).total_seconds() <= FRESH_WINDOW_SEC:
            return realtime
    final = payload.get("final")
    if final and final.get("date") == now.strftime("%Y-%m-%d"):
        return final
    mood = compute_final_mood()
    if mood is None:
        return {"error": "mood_unavailable",
                "reason": "runtime JSON 缺失且日线终算不可用"}
    return {**mood, "source": "on_demand_daily"}
