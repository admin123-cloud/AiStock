# -*- coding: utf-8 -*-
"""G3 Phase 1A：大盘择时总闸门（Market Regime Gate）× R51 仓位水位联动引擎。

核心职责：
1. 聚合四大市场观测源，并为每个源附带独立新鲜度状态（fresh / stale / missing）：
   - 全市场宽度（breadth：above_ma20_pct / level / hl_ratio）
   - 市场情绪（runtime/market_mood/YYYYMMDD.json：涨跌停家数、封板率、最高连板）
   - 基准指数趋势与动量（ClickHouse 000001.SH：index_mom60 + R51 N=2 确认周期）
   - 全市场成交额预测（runtime/market_turnover_forecast/latest.json，严格新鲜度门禁）
2. 输出统一的三档市场状态与 R51 仓位上限：
   - offensive（进攻）：目标仓位上限 75%，允许主升开新仓 + 做T
   - balanced（震荡）：目标仓位上限 50%，精选主升 + 常规做T
   - defensive（防守）：目标仓位上限 25%，严禁开新主升仓，优先降水位与反T降本
3. 结合双账号真实总资产与持仓市值，计算当前实际水位与目标上限差值，输出 R51 分步调仓建议。
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))

# R51 定稿水位映射（2026-11-20 纳入主线）
R51_POSITION_CAPS: dict[str, float] = {
    "offensive": 0.75,
    "balanced": 0.50,
    "defensive": 0.25,
}
R51_MAX_STEP_RATIO = 0.15  # 单次调仓步长上限 1.5 成
R51_DEADBAND_RATIO = 0.05  # 水位匹配死区 ±5%

_INDEX_CACHE: dict[str, Any] = {"expires_at": 0.0, "date_key": None, "payload": None}
_TURNOVER_CACHE: dict[str, Any] = {"expires_at": 0.0, "date_key": None, "payload": None}
_INDEX_CACHE_TTL_SEC = 120.0


def _safe_float(v: Any) -> Optional[float]:
    if v in (None, "", "-"):
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_date(raw: Any) -> Optional[date]:
    s = str(raw or "").strip()
    if not s:
        return None
    digits = s.replace("-", "").replace("/", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def _is_date_fresh(doc_date_raw: Any, ref_date: date, *, max_calendar_lag_days: int = 4) -> tuple[bool, Optional[int]]:
    """判断日期相对参考日是否新鲜（考虑周末/小长假，默认允许最多 4 自然日滞后）。"""
    d = _parse_date(doc_date_raw)
    if d is None:
        return False, None
    lag = (ref_date - d).days
    return (0 <= lag <= max_calendar_lag_days), lag


# ---------------------------------------------------------------------------
# 1. R51 N=2 指数周期确认算法（纯函数）
# ---------------------------------------------------------------------------

def compute_r51_index_regime(closes: Sequence[float], confirm_days: int = 2) -> dict[str, Any]:
    """根据按时间升序排列的指数收盘价序列，计算 MA5/10/20、60日动量与 R51 N=2 确认周期。"""
    vals = [float(x) for x in closes if _safe_float(x) is not None and float(x) > 0]
    n = len(vals)
    if n < 20:
        return {
            "status": "missing",
            "raw_regime": "unknown",
            "confirmed_regime": "unknown",
            "streak_days": 0,
            "close": vals[-1] if vals else None,
            "ma5": None,
            "ma10": None,
            "ma20": None,
            "index_mom60": None,
        }

    def _raw_at(idx: int) -> str:
        c = vals[idx]
        ma5 = sum(vals[idx - 4 : idx + 1]) / 5.0
        ma10 = sum(vals[idx - 9 : idx + 1]) / 10.0
        ma20 = sum(vals[idx - 19 : idx + 1]) / 20.0
        if ma5 > ma10 > ma20 and c >= ma5:
            return "up"
        if ma5 < ma10 < ma20 and c <= ma5:
            return "down"
        return "sideways"

    raw_series = [_raw_at(i) for i in range(19, n)]
    # 逐日推进 R51 N=2 确认状态
    confirmed = raw_series[0]
    pending: Optional[str] = None
    pending_count = 0
    for r in raw_series[1:]:
        if r == confirmed:
            pending = None
            pending_count = 0
        else:
            if r == pending:
                pending_count += 1
            else:
                pending = r
                pending_count = 1
            if pending_count >= confirm_days:
                confirmed = r
                pending = None
                pending_count = 0

    # 计算最新 raw 连续天数
    latest_raw = raw_series[-1]
    streak = 0
    for r in reversed(raw_series):
        if r == latest_raw:
            streak += 1
        else:
            break

    ma5 = round(sum(vals[-5:]) / 5.0, 2)
    ma10 = round(sum(vals[-10:]) / 10.0, 2)
    ma20 = round(sum(vals[-20:]) / 20.0, 2)
    mom60 = None
    if n >= 61 and vals[-61] > 0:
        mom60 = round((vals[-1] - vals[-61]) / vals[-61], 4)

    return {
        "status": "fresh",
        "raw_regime": latest_raw,
        "confirmed_regime": confirmed,
        "streak_days": streak,
        "close": round(vals[-1], 2),
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "index_mom60": mom60,
    }


def _fetch_sh_index_closes_from_ch(ref_date_str: str) -> tuple[Optional[str], list[float]]:
    """直连 ClickHouse 获取上证指数近 85 个交易日收盘价（升序）。fail-safe。"""
    now_mono = time.monotonic()
    if (
        _INDEX_CACHE["payload"] is not None
        and _INDEX_CACHE["date_key"] == ref_date_str
        and now_mono < float(_INDEX_CACHE["expires_at"])
    ):
        return _INDEX_CACHE["payload"]

    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
            port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
            username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
            password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
            database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
            connect_timeout=5,
            send_receive_timeout=15,
        )
        try:
            res = client.query(
                """
                SELECT trade_date, anyLast(close) AS close
                FROM stock.kline_daily
                WHERE code IN ('000001.SH', '999999.SH')
                  AND trade_date <= toDate({d:String})
                  AND trade_date >= toDate({d:String}) - 160
                GROUP BY trade_date
                ORDER BY trade_date ASC
                """,
                parameters={"d": ref_date_str},
            )
            rows = res.result_rows or []
        finally:
            client.close()

        if not rows:
            return None, []
        latest_dt = str(rows[-1][0])[:10]
        closes = [float(r[1]) for r in rows if r[1] is not None and float(r[1]) > 0]
        payload = (latest_dt, closes[-85:])
        _INDEX_CACHE["date_key"] = ref_date_str
        _INDEX_CACHE["payload"] = payload
        _INDEX_CACHE["expires_at"] = now_mono + _INDEX_CACHE_TTL_SEC
        return payload
    except Exception:
        return None, []


# ---------------------------------------------------------------------------
# 2. 各观测源读取器
# ---------------------------------------------------------------------------

def _load_latest_market_mood(ref_date: date, mood_dir: Optional[Path] = None) -> dict[str, Any]:
    directory = mood_dir or runtime_path("market_mood")
    if not directory.exists():
        return {"status": "missing", "reason": "market_mood 目录不存在"}

    files = sorted(directory.glob("20*.json"), reverse=True)
    for f in files[:5]:
        doc = _read_json_safe(f)
        if not doc:
            continue
        doc_date = doc.get("date") or f.stem
        final_blk = doc.get("final") if isinstance(doc.get("final"), dict) else None
        rt_blk = doc.get("realtime") if isinstance(doc.get("realtime"), dict) else None
        blk = final_blk or rt_blk
        if not blk:
            continue
        fresh, lag = _is_date_fresh(doc_date, ref_date, max_calendar_lag_days=4)
        lu = int(blk.get("limit_up_count") or 0)
        ld = int(blk.get("limit_down_count") or 0)
        broke = int(blk.get("broke_count") or 0)
        seal_rate = _safe_float(blk.get("seal_rate"))
        max_streak = int(blk.get("max_streak") or 0)

        # 情绪定性
        if ld >= 50 and ld > lu:
            mood_state = "freezing"
            mood_label = "退潮冰点"
        elif ld >= 30 or (seal_rate is not None and seal_rate < 0.55):
            mood_state = "weak"
            mood_label = "情绪偏弱"
        elif lu >= 60 and ld <= 15 and (seal_rate is None or seal_rate >= 0.70):
            mood_state = "warm"
            mood_label = "情绪活跃"
        else:
            mood_state = "neutral"
            mood_label = "情绪中性"

        return {
            "status": "fresh" if fresh else "stale",
            "date": str(doc_date),
            "lag_days": lag,
            "mode": "final" if final_blk else "realtime",
            "limit_up_count": lu,
            "limit_down_count": ld,
            "broke_count": broke,
            "seal_rate": round(seal_rate, 4) if seal_rate is not None else None,
            "max_streak": max_streak,
            "mood_state": mood_state,
            "mood_label": mood_label,
        }

    return {"status": "missing", "reason": "未找到有效 market_mood 快照"}


def _fetch_daily_market_turnover_from_ch(ref_date_str: str) -> Optional[dict[str, Any]]:
    """直连 ClickHouse 获取最近 6 个交易日全市场股票真实收盘成交额及 5 日量比。fail-safe。"""
    now_mono = time.monotonic()
    if (
        _TURNOVER_CACHE["payload"] is not None
        and _TURNOVER_CACHE["date_key"] == ref_date_str
        and now_mono < float(_TURNOVER_CACHE["expires_at"])
    ):
        return _TURNOVER_CACHE["payload"]

    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
            port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
            username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
            password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
            database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
            connect_timeout=5,
            send_receive_timeout=15,
        )
        try:
            res = client.query(
                """
                SELECT trade_date, sum(amount) AS total_amt, count() AS cnt
                FROM (
                    SELECT code, trade_date, anyLast(amount) AS amount
                    FROM stock.kline_daily
                    WHERE trade_date <= toDate({d:String})
                      AND trade_date >= toDate({d:String}) - 25
                      AND (code LIKE '%.SH' OR code LIKE '%.SZ' OR code LIKE '%.BJ')
                      AND NOT (code LIKE '000%' AND code LIKE '%.SH')
                      AND NOT (code LIKE '399%' AND code LIKE '%.SZ')
                      AND NOT (code LIKE '880%' AND code LIKE '%.SH')
                      AND NOT (code LIKE '999%' AND code LIKE '%.SH')
                    GROUP BY code, trade_date
                )
                GROUP BY trade_date
                HAVING cnt >= 3000
                ORDER BY trade_date DESC
                LIMIT 6
                """,
                parameters={"d": ref_date_str},
            )
            rows = res.result_rows or []
        finally:
            client.close()

        if not rows:
            return None
        latest_dt = str(rows[0][0])[:10]
        latest_amt = float(rows[0][1] or 0.0)
        latest_cnt = int(rows[0][2] or 0)
        prev_amts = [float(r[1] or 0.0) for r in rows[1:6] if r[1] is not None and float(r[1]) > 0]
        ma5_amt = sum(prev_amts) / len(prev_amts) if prev_amts else None
        vol_ratio = round(latest_amt / ma5_amt, 2) if (ma5_amt and ma5_amt > 0) else None
        payload = {
            "trade_date": latest_dt,
            "amount": latest_amt,
            "trillion": round(latest_amt / 1e12, 2),
            "ma5_trillion": round(ma5_amt / 1e12, 2) if ma5_amt else None,
            "vol_ratio_5d": vol_ratio,
            "stock_count": latest_cnt,
        }
        _TURNOVER_CACHE["date_key"] = ref_date_str
        _TURNOVER_CACHE["payload"] = payload
        _TURNOVER_CACHE["expires_at"] = now_mono + _INDEX_CACHE_TTL_SEC
        return payload
    except Exception:
        return None


def _load_turnover_forecast(
    ref_date: date,
    forecast_dir: Optional[Path] = None,
    turnover_fetcher: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    directory = forecast_dir or runtime_path("market_turnover_forecast")
    latest_file = directory / "latest.json"
    doc = _read_json_safe(latest_file)
    if doc and doc.get("available"):
        td = doc.get("trade_date") or doc.get("as_of")
        # 仅当日盘中预测视为 fresh（滞后 0 天）
        fresh, lag = _is_date_fresh(td, ref_date, max_calendar_lag_days=0)
        if fresh:
            forecast_amt = _safe_float(doc.get("forecast_amount"))
            forecast_wan_yi = round(forecast_amt / 1e12, 2) if forecast_amt is not None else None
            return {
                "status": "fresh",
                "mode": "intraday_forecast",
                "trade_date": str(doc.get("trade_date") or ""),
                "as_of": doc.get("as_of"),
                "lag_days": lag,
                "forecast_amount": forecast_amt,
                "forecast_trillion": forecast_wan_yi,
                "vol_ratio_5d": None,
                "confidence": _safe_float(doc.get("confidence")),
                "note": None,
            }

    # 盘后或盘中预测未刷新时，自动回退 ClickHouse 最新交易日全市场真实收盘成交额
    fetcher = turnover_fetcher if turnover_fetcher is not None else _fetch_daily_market_turnover_from_ch
    daily = fetcher(ref_date.strftime("%Y-%m-%d"))
    if daily and daily.get("amount"):
        fresh, lag = _is_date_fresh(daily.get("trade_date"), ref_date, max_calendar_lag_days=4)
        return {
            "status": "fresh" if fresh else "stale",
            "mode": "daily_actual",
            "trade_date": str(daily.get("trade_date") or ""),
            "as_of": None,
            "lag_days": lag,
            "forecast_amount": daily["amount"],
            "forecast_trillion": daily["trillion"],
            "ma5_trillion": daily.get("ma5_trillion"),
            "vol_ratio_5d": daily.get("vol_ratio_5d"),
            "stock_count": daily.get("stock_count"),
            "confidence": 1.0,
            "note": None if fresh else f"收盘成交额滞后（最后更新 {daily.get('trade_date')}）",
        }

    if doc and doc.get("available"):
        td = doc.get("trade_date") or doc.get("as_of")
        _, lag = _is_date_fresh(td, ref_date, max_calendar_lag_days=0)
        forecast_amt = _safe_float(doc.get("forecast_amount"))
        return {
            "status": "stale",
            "mode": "intraday_forecast_stale",
            "trade_date": str(doc.get("trade_date") or ""),
            "as_of": doc.get("as_of"),
            "lag_days": lag,
            "forecast_amount": forecast_amt,
            "forecast_trillion": round(forecast_amt / 1e12, 2) if forecast_amt is not None else None,
            "vol_ratio_5d": None,
            "confidence": _safe_float(doc.get("confidence")),
            "note": f"成交额预测已过期（最后更新 {doc.get('trade_date')}），已降权不参与打分",
        }

    return {"status": "missing", "mode": "missing", "reason": "无成交额预测及收盘成交额数据"}


# ---------------------------------------------------------------------------
# 3. 综合择时总闸门与 R51 水位联动（核心入口）
# ---------------------------------------------------------------------------

def evaluate_market_regime_gate(
    *,
    breadth: Optional[Mapping[str, Any]] = None,
    total_assets: Optional[float] = None,
    total_market_value: Optional[float] = None,
    today: Optional[date] = None,
    mood_dir: Optional[Path] = None,
    forecast_dir: Optional[Path] = None,
    index_fetcher: Optional[Callable[[str], tuple[Optional[str], list[float]]]] = None,
    turnover_fetcher: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """综合四大观测源与双账号真实水位，产出 Phase 1A 大盘择时总闸门决策。"""
    ref_date = today or datetime.now(BJ_TZ).date()
    ref_date_str = ref_date.strftime("%Y-%m-%d")

    # 1. 市场宽度
    b = dict(breadth or {})
    ma20_pct = _safe_float(b.get("above_ma20_pct"))
    b_level = str(b.get("level") or "unknown").lower()
    b_date = b.get("date")
    if ma20_pct is not None or b_level in ("weak", "neutral", "strong"):
        b_fresh, b_lag = _is_date_fresh(b_date, ref_date, max_calendar_lag_days=4) if b_date else (True, 0)
        breadth_src = {
            "status": "fresh" if b_fresh else "stale",
            "date": b_date,
            "lag_days": b_lag,
            "level": b_level,
            "above_ma20_pct": ma20_pct,
            "above_ma5_pct": _safe_float(b.get("above_ma5_pct")),
            "hl_ratio": _safe_float(b.get("hl_ratio")),
        }
    else:
        breadth_src = {"status": "missing", "level": "unknown", "above_ma20_pct": None}

    # 2. 市场情绪
    mood_src = _load_latest_market_mood(ref_date, mood_dir=mood_dir)

    # 3. 上证指数 R51 v2.1（300股大样本实证最优：N=3 连续 3 日确认周期 + index_mom60）
    fetcher = index_fetcher or _fetch_sh_index_closes_from_ch
    idx_date, idx_closes = fetcher(ref_date_str)
    idx_r51 = compute_r51_index_regime(idx_closes, confirm_days=3)
    if idx_r51["status"] == "fresh":
        idx_fresh, idx_lag = _is_date_fresh(idx_date, ref_date, max_calendar_lag_days=4)
        idx_r51["status"] = "fresh" if idx_fresh else "stale"
        idx_r51["date"] = idx_date
        idx_r51["lag_days"] = idx_lag
        idx_r51["code"] = "000001.SH"
    else:
        idx_r51["date"] = idx_date
        idx_r51["code"] = "000001.SH"

    # 4. 成交额（盘中预测优先，盘后/过期自动回退 ClickHouse 最新真实收盘成交额）
    turnover_src = _load_turnover_forecast(ref_date, forecast_dir=forecast_dir, turnover_fetcher=turnover_fetcher)

    # 5. 三档综合判定（Fail-Closed）
    reasons: list[str] = []
    warnings: list[str] = []
    degraded = False

    core_fresh_count = sum(
        1 for s in (breadth_src, mood_src, idx_r51) if s.get("status") == "fresh"
    )
    if turnover_src.get("status") == "stale" and turnover_src.get("note"):
        warnings.append(str(turnover_src["note"]))
    if idx_r51.get("status") != "fresh":
        warnings.append("指数日线趋势源暂不可用或滞后，已保守处理")

    # 判定各项风险与进攻信号
    is_idx_down = idx_r51.get("status") == "fresh" and idx_r51.get("confirmed_regime") == "down"
    is_idx_up = idx_r51.get("status") == "fresh" and idx_r51.get("confirmed_regime") == "up"
    is_breadth_weak = breadth_src.get("status") == "fresh" and (
        breadth_src.get("level") == "weak"
        or (breadth_src.get("above_ma20_pct") is not None and float(breadth_src["above_ma20_pct"]) < 25.0)
    )
    is_breadth_strong = breadth_src.get("status") == "fresh" and (
        breadth_src.get("level") == "strong"
        or (breadth_src.get("above_ma20_pct") is not None and float(breadth_src["above_ma20_pct"]) >= 55.0)
    )
    is_mood_freezing = mood_src.get("status") == "fresh" and mood_src.get("mood_state") == "freezing"
    is_mood_weak = mood_src.get("status") == "fresh" and mood_src.get("mood_state") in ("freezing", "weak")
    is_mood_warm = mood_src.get("status") == "fresh" and mood_src.get("mood_state") == "warm"

    if core_fresh_count == 0:
        regime = "defensive"
        degraded = True
        reasons.append("核心市场源（宽度/情绪/指数）均缺失或过期，触发 fail-closed 防守保护")
    elif is_idx_down or is_breadth_weak or is_mood_freezing:
        regime = "defensive"
        if is_idx_down:
            reasons.append(f"上证指数 R51 确认处于下降周期（连续 {idx_r51.get('streak_days')} 日）")
        if is_breadth_weak:
            reasons.append(f"全市场宽度弱势（站上MA20占比 {breadth_src.get('above_ma20_pct')}%）")
        if is_mood_freezing:
            reasons.append(
                f"市场情绪退潮冰点（跌停 {mood_src.get('limit_down_count')} 家 > 涨停 {mood_src.get('limit_up_count')} 家）"
            )
    elif (is_idx_up and is_breadth_strong and not is_mood_weak) or (is_idx_up and is_mood_warm and not is_breadth_weak):
        regime = "offensive"
        reasons.append("指数 R51 确认上升周期且市场宽度/情绪配合良好，允许按上限积极布局")
    else:
        regime = "balanced"
        if is_mood_weak:
            reasons.append(
                f"情绪偏弱（跌停 {mood_src.get('limit_down_count')} 家、封板率 {round((mood_src.get('seal_rate') or 0) * 100, 1)}%），控制半仓以内"
            )
        else:
            reasons.append("指数与市场宽度处于结构震荡区，维持中性半仓并依托带位做T")

    # 额外检查 index_mom60（对齐主升浪 Score88 合约 <= 5% 诊断）
    mom60 = idx_r51.get("index_mom60")
    if mom60 is not None and mom60 > 0.05:
        warnings.append(f"上证 60 日动量 {mom60 * 100:.1f}% > 5%（主升浪合约过热提示，追高需谨慎）")

    # 6. 映射 R51 v2.1 仓位上限、五态细分（含两个极端态）与实盘水位对标
    target_cap = R51_POSITION_CAPS[regime]
    allow_new_mainwave = regime != "defensive"

    # 五态细分识别（在三档硬仓位上限之上叠加两个极端态剧本提示）
    ld_cnt = int(mood_src.get("limit_down_count") or 0) if mood_src.get("status") == "fresh" else 0
    lu_cnt = int(mood_src.get("limit_up_count") or 0) if mood_src.get("status") == "fresh" else 0
    sr_val = _safe_float(mood_src.get("seal_rate")) if mood_src.get("status") == "fresh" else None
    ma20_val = _safe_float(breadth_src.get("above_ma20_pct")) if breadth_src.get("status") == "fresh" else None

    if regime == "defensive" and ld_cnt >= 80 and ld_cnt > lu_cnt and (ma20_val is None or ma20_val < 15.0):
        phase_5 = "extreme_panic"
        regime_label = "极端防守·恐慌冰点"
        tone = "red"
        action_summary = "死守现金严禁开新主升仓 · 等首次缩量止跌且经AI定性非衰退后才可≤10%试错宽基/红利"
    elif regime == "offensive" and lu_cnt >= 80 and (sr_val is None or sr_val >= 0.75) and (ma20_val is None or ma20_val >= 65.0):
        phase_5 = "extreme_offensive"
        regime_label = "极端进攻·情绪高潮"
        tone = "emerald"
        action_summary = "主升绿灯池积极布局 + 卖晚买早正T · 同步盯防高潮转分歧预警"
    else:
        phase_5 = regime
        meta_map = {
            "offensive": ("常态进攻·温和上升", "emerald", "允许主升浪精选开新仓 + 积极正T"),
            "balanced": ("常态震荡·箱体轮动", "amber", "仅限高确定性精选主升 + 双向带位做T + 关注稳健腿ETF"),
            "defensive": ("常态防守·趋势下跌", "red", "严禁开新主升仓 · 确认转弱果断分步降水位，不傻等5日反弹"),
        }
        regime_label, tone, action_summary = meta_map[regime]

    assets_val = _safe_float(total_assets)
    mv_val = _safe_float(total_market_value)
    actual_ratio = None
    gap_ratio = None
    rebalance_status = "unknown"
    rebalance_advice = "暂无双账号总资产数据，无法核对实际水位"

    if assets_val is not None and assets_val > 0 and mv_val is not None and mv_val >= 0:
        actual_ratio = round(mv_val / assets_val, 4)
        gap_ratio = round(actual_ratio - target_cap, 4)
        if gap_ratio > R51_DEADBAND_RATIO:
            step_pct = min(round(gap_ratio * 100, 1), round(R51_MAX_STEP_RATIO * 100, 1))
            rebalance_status = "overweight"
            rebalance_advice = (
                f"当前实际水位 {actual_ratio * 100:.1f}% 高于今日上限 {target_cap * 100:.0f}%（超配 {gap_ratio * 100:.1f}%）："
                f"按 R51 v2.1（300股大样本实证），次日开盘/盘中果断分步减仓 ≤{step_pct:.1f}%，勿傻等5日反弹卖带"
            )
        elif gap_ratio < -R51_DEADBAND_RATIO and allow_new_mainwave:
            step_pct = min(round(abs(gap_ratio) * 100, 1), round(R51_MAX_STEP_RATIO * 100, 1))
            rebalance_status = "underweight"
            rebalance_advice = (
                f"当前实际水位 {actual_ratio * 100:.1f}% 低于目标上限 {target_cap * 100:.0f}%："
                f"按 R51 v2.1（N=3 确认），次日开盘/盘中分步加仓 ≤{step_pct:.1f}% 或切入主升绿灯精选"
            )
        else:
            rebalance_status = "aligned"
            rebalance_advice = (
                f"当前实际水位 {actual_ratio * 100:.1f}% 符合今日目标上限 {target_cap * 100:.0f}%，"
                "维持现有底仓专注带位做T降本"
            )

    return {
        "schema": "g3_market_regime_gate/1",
        "as_of": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "ref_date": ref_date_str,
        "regime": regime,
        "phase_5": phase_5,
        "regime_label": regime_label,
        "tone": tone,
        "degraded": degraded,
        "target_position_cap": target_cap,
        "target_position_cap_pct": round(target_cap * 100, 1),
        "allow_new_mainwave": allow_new_mainwave,
        "action_summary": action_summary,
        "reasons": reasons,
        "warnings": warnings,
        "r51_waterline": {
            "actual_ratio": actual_ratio,
            "actual_pct": round(actual_ratio * 100, 1) if actual_ratio is not None else None,
            "target_cap_ratio": target_cap,
            "target_cap_pct": round(target_cap * 100, 1),
            "gap_ratio": gap_ratio,
            "gap_pct": round(gap_ratio * 100, 1) if gap_ratio is not None else None,
            "max_step_pct": round(R51_MAX_STEP_RATIO * 100, 1),
            "status": rebalance_status,
            "advice": rebalance_advice,
        },
        "sources": {
            "breadth": breadth_src,
            "market_mood": mood_src,
            "index_r51": idx_r51,
            "turnover_forecast": turnover_src,
        },
    }
