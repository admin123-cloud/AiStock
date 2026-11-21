# -*- coding: utf-8 -*-
"""G3 前向验真与稳健腿 ETF 影子台账引擎（Phase 2A & Phase 4 验收闭环）。

两大核心能力（零未来函数、严格交易日口径）：
1. AI 排雷前向绩效追踪（evaluate_ai_screening_forward）：
   - 扫描 runtime/g3_ai_screening/20*.json 每日排雷档案；
   - 按绿灯 / 黄灯 / 红灯分组，从 ClickHouse 拉取决策日及之后真实交易日收盘价，
     计算 T+5 / T+10 真实涨跌幅、胜率与“绿灯 vs 红灯”排雷超额；
   - 尚未满 T+1 / T+5 / T+10 的批次诚实标注 pending_t1 / tracking，绝不虚构。
2. 稳健腿 6 大宽基 ETF 自动影子台账（evaluate_defense_etf_shadow）：
   - 严格按 T-1 日收盘计算 compute_voltarget_trend_signal（MA20≥MA60 上升趋势 + 15% 目标波动率），
     T 日按该仓位生效（全破位自动 100% 现金防守），计入双边千一成本；
   - 输出近 60 交易日影子净值、最大回撤、收益回撤比，并与沪深300 满仓不动直接对照。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from services.g3.strategy_matrix import (
    DEFENSE_ETF_UNIVERSE,
    compute_voltarget_trend_signal,
)
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
ETF_TURNOVER_COST_RATE = 0.0005  # ETF 单边交易成本+冲击估算 5bp（免印花税）


def _read_json_safe(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _norm_code6(code: Any) -> str:
    return str(code or "").split(".")[0].strip().zfill(6)


def _to_ch_code(code: str) -> str:
    s = str(code or "").strip().upper()
    if "." in s:
        return s
    c6 = s.zfill(6)
    if c6.startswith(("6", "9", "5")):
        return f"{c6}.SH"
    if c6.startswith(("8", "4", "92")):
        return f"{c6}.BJ"
    return f"{c6}.SZ"


def _fetch_stock_closes_from_ch(codes: Sequence[str], min_date: str) -> dict[str, list[tuple[str, float]]]:
    """批量拉取股票自 min_date 起的升序日收盘价 [(YYYY-MM-DD, close), ...]。fail-safe。"""
    ch_codes = sorted({_to_ch_code(c) for c in codes if c})
    if not ch_codes:
        return {}
    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
            port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
            username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
            password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
            database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
            connect_timeout=5,
            send_receive_timeout=20,
        )
        try:
            res = client.query(
                """
                SELECT code, trade_date, anyLast(close) AS close
                FROM stock.kline_daily
                WHERE code IN {codes:Array(String)}
                  AND trade_date >= toDate({d:String})
                GROUP BY code, trade_date
                ORDER BY code ASC, trade_date ASC
                """,
                parameters={"codes": ch_codes, "d": min_date},
            )
            rows = res.result_rows or []
        finally:
            client.close()

        out: dict[str, list[tuple[str, float]]] = {}
        for code, dt_val, cl in rows:
            if cl is None or float(cl) <= 0:
                continue
            out.setdefault(_norm_code6(code), []).append((str(dt_val)[:10], float(cl)))
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 1. AI 排雷前向绩效追踪（T+5 / T+10）
# ---------------------------------------------------------------------------

def evaluate_ai_screening_forward(
    *,
    max_batches: int = 30,
    screening_dir: Optional[Path] = None,
    closes_fetcher: Optional[Callable[[Sequence[str], str], dict[str, list[tuple[str, float]]]]] = None,
) -> dict[str, Any]:
    """统计历史各批次 AI 排雷池（绿灯 / 黄灯 / 红灯）的真实 T+5 / T+10 表现。"""
    store = screening_dir or runtime_path("g3_ai_screening")
    batch_files = sorted(
        [f for f in store.glob("20*.json") if re.fullmatch(r"20\d{6}\.json", f.name)],
        reverse=True,
    )[:max_batches] if store.exists() else []

    batches: list[dict[str, Any]] = []
    all_codes: set[str] = set()
    min_date: Optional[str] = None

    for f in batch_files:
        doc = _read_json_safe(f)
        if not doc:
            continue
        d_date = str(doc.get("decision_date") or f"{f.stem[:4]}-{f.stem[4:6]}-{f.stem[6:8]}")
        items_map = doc.get("items_by_code")
        if not isinstance(items_map, Mapping) or not items_map:
            continue
        batches.append({"decision_date": d_date, "doc": doc, "items": list(items_map.values())})
        for it in items_map.values():
            if isinstance(it, Mapping) and it.get("code"):
                all_codes.add(str(it["code"]))
        if min_date is None or d_date < min_date:
            min_date = d_date

    fetcher = closes_fetcher or _fetch_stock_closes_from_ch
    closes_by_c6 = fetcher(sorted(all_codes), min_date) if (all_codes and min_date) else {}

    groups: dict[str, dict[str, Any]] = {
        light: {"total": 0, "t5_rets": [], "t10_rets": [], "latest_rets": []}
        for light in ("green", "yellow", "red")
    }
    records: list[dict[str, Any]] = []

    for b in batches:
        d_date = b["decision_date"]
        for raw in b["items"]:
            if not isinstance(raw, Mapping):
                continue
            code = str(raw.get("code") or "")
            c6 = _norm_code6(code)
            light = str(raw.get("risk_light") or "yellow").lower()
            if light not in groups:
                light = "yellow"

            seq = closes_by_c6.get(c6, [])
            # 找到决策日当日（或之前最近）的收盘价作为基准，以及决策日之后的真实交易日序列
            base_px: Optional[float] = None
            after: list[tuple[str, float]] = []
            for dt_s, cl in seq:
                if dt_s <= d_date:
                    base_px = cl
                else:
                    after.append((dt_s, cl))

            ret_5d: Optional[float] = None
            ret_10d: Optional[float] = None
            ret_latest: Optional[float] = None
            days_elapsed = len(after)

            if base_px and base_px > 0 and after:
                ret_latest = round((after[-1][1] - base_px) / base_px * 100.0, 2)
                if days_elapsed >= 5:
                    ret_5d = round((after[4][1] - base_px) / base_px * 100.0, 2)
                if days_elapsed >= 10:
                    ret_10d = round((after[9][1] - base_px) / base_px * 100.0, 2)

            status = (
                "finalized_10d"
                if ret_10d is not None
                else ("finalized_5d" if ret_5d is not None else ("tracking" if days_elapsed > 0 else "pending_t1"))
            )

            g = groups[light]
            g["total"] += 1
            if ret_5d is not None:
                g["t5_rets"].append(ret_5d)
            if ret_10d is not None:
                g["t10_rets"].append(ret_10d)
            if ret_latest is not None:
                g["latest_rets"].append(ret_latest)

            records.append({
                "decision_date": d_date,
                "code": code,
                "name": str(raw.get("name") or c6),
                "industry": str(raw.get("industry") or ""),
                "risk_light": light,
                "catalyst_score": int(raw.get("catalyst_score") or 3),
                "base_close": round(base_px, 2) if base_px else None,
                "days_elapsed": days_elapsed,
                "ret_latest_pct": ret_latest,
                "ret_5d_pct": ret_5d,
                "ret_10d_pct": ret_10d,
                "status": status,
            })

    def _agg(vals: list[float]) -> dict[str, Any]:
        if not vals:
            return {"n": 0, "avg_pct": None, "win_rate_pct": None}
        wins = sum(1 for x in vals if x > 0)
        return {
            "n": len(vals),
            "avg_pct": round(sum(vals) / len(vals), 2),
            "win_rate_pct": round(wins / len(vals) * 100.0, 1),
        }

    group_summary = {}
    for light, g in groups.items():
        group_summary[light] = {
            "total_samples": g["total"],
            "t5": _agg(g["t5_rets"]),
            "t10": _agg(g["t10_rets"]),
            "latest": _agg(g["latest_rets"]),
        }

    g5 = group_summary["green"]["t5"]["avg_pct"]
    r5 = group_summary["red"]["t5"]["avg_pct"]
    alpha_green_vs_red_5d = round(g5 - r5, 2) if (g5 is not None and r5 is not None) else None

    return {
        "schema": "g3_ai_screening_forward_eval/1",
        "as_of": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "batches_count": len(batches),
        "samples_total": len(records),
        "alpha_green_vs_red_5d_pct": alpha_green_vs_red_5d,
        "by_light": group_summary,
        "records": records[:80],
        "note": (
            "首日排雷批次已锁定基准价，T+1 起每日收盘自动累计 T+5 / T+10 真实胜率与绿红差。"
            if group_summary["green"]["t5"]["n"] == 0
            else "已按严格交易日序列统计绿灯/黄灯/红灯真实 T+5 与 T+10 绩效。"
        ),
    }


# ---------------------------------------------------------------------------
# 2. 稳健腿 6 大宽基 ETF 自动影子台账（近 60 交易日无未来函数回放）
# ---------------------------------------------------------------------------

def _fetch_defense_index_series_from_ch() -> dict[str, list[tuple[str, float]]]:
    """拉取 6 大宽基指数近 140 个交易日收盘价序列（约 210 自然日，留足 60 日 MA 预热 + 60 日影子回放）。"""
    codes = [item["index_code"] for item in DEFENSE_ETF_UNIVERSE]
    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
            port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
            username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
            password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
            database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
            connect_timeout=5,
            send_receive_timeout=20,
        )
        try:
            res = client.query(
                """
                SELECT code, trade_date, anyLast(close) AS close
                FROM stock.kline_daily
                WHERE code IN {codes:Array(String)}
                  AND trade_date >= today() - 240
                GROUP BY code, trade_date
                ORDER BY code ASC, trade_date ASC
                """,
                parameters={"codes": codes},
            )
            rows = res.result_rows or []
        finally:
            client.close()

        out: dict[str, list[tuple[str, float]]] = {}
        for code, dt_val, cl in rows:
            if cl is None or float(cl) <= 0:
                continue
            out.setdefault(str(code), []).append((str(dt_val)[:10], float(cl)))
        return out
    except Exception:
        return {}


def _calc_max_drawdown_pct(navs: Sequence[float]) -> float:
    if not navs:
        return 0.0
    peak = navs[0]
    mdd = 0.0
    for v in navs:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak * 100.0
            if dd < mdd:
                mdd = dd
    return round(mdd, 2)


def evaluate_defense_etf_shadow(
    *,
    window_trading_days: int = 60,
    series_fetcher: Optional[Callable[[], dict[str, list[tuple[str, float]]]]] = None,
    shadow_dir: Optional[Path] = None,
    persist: bool = True,
) -> dict[str, Any]:
    """严格 T-1 无未来函数回放稳健腿 6 大宽基 ETF 波动率目标策略影子台账，并对比沪深300。"""
    fetcher = series_fetcher or _fetch_defense_index_series_from_ch
    series_map = fetcher()

    # 构建共同交易日轴
    all_dates = sorted({dt for seq in series_map.values() for dt, _ in seq})
    if len(all_dates) < 35:
        return {
            "schema": "g3_defense_etf_shadow/1",
            "as_of": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
            "available": False,
            "reason": "宽基指数历史日线不足，暂无法回放稳健腿影子台账",
            "daily_curve": [],
        }

    # 为每个指数建立 date -> close 索引
    px_map: dict[str, dict[str, float]] = {
        code: {dt: cl for dt, cl in seq} for code, seq in series_map.items()
    }
    codes = [item["index_code"] for item in DEFENSE_ETF_UNIVERSE]
    etf_name_by_idx = {item["index_code"]: item["etf_name"] for item in DEFENSE_ETF_UNIVERSE}

    # 回放起点：至少预留前 60 根（或 25 根）用于计算 MA60
    warmup = min(60, max(25, len(all_dates) - window_trading_days))
    start_idx = max(warmup, len(all_dates) - window_trading_days)

    nav = 1.0
    bench_nav = 1.0
    prev_weights: dict[str, float] = {c: 0.0 for c in codes}
    nav_series = [1.0]
    bench_series = [1.0]
    daily_curve: list[dict[str, Any]] = []
    switch_events = 0

    for i in range(start_idx, len(all_dates)):
        dt_today = all_dates[i]
        dt_prev = all_dates[i - 1]

        # 严格用截至 T-1 的收盘价决定 T 日生效权重（零未来函数！）
        raw_weights: dict[str, float] = {}
        active_etfs: list[str] = []
        for c in codes:
            hist_closes = [px_map.get(c, {}).get(d) for d in all_dates[:i]]
            hist_clean = [x for x in hist_closes if x is not None and x > 0]
            sig = compute_voltarget_trend_signal(hist_clean)
            if sig["signal"] == "long_trend":
                raw_weights[c] = float(sig["vol_target_weight_pct"]) / 100.0
                active_etfs.append(etf_name_by_idx.get(c, c))
            else:
                raw_weights[c] = 0.0

        # 等权分配给所有处于上升趋势的标的（每只上限 1/N * vol_target_weight，总仓位 ≤ 100%）
        n_active = len(active_etfs)
        weights: dict[str, float] = {}
        if n_active > 0:
            for c in codes:
                weights[c] = round(raw_weights[c] / n_active, 4) if raw_weights[c] > 0 else 0.0
        else:
            weights = {c: 0.0 for c in codes}

        total_weight = round(sum(weights.values()), 4)
        turnover = sum(abs(weights[c] - prev_weights[c]) for c in codes)
        if turnover >= 0.05:
            switch_events += 1
        cost = turnover * ETF_TURNOVER_COST_RATE

        # 计算 T 日组合收益
        port_ret = 0.0
        for c, w in weights.items():
            if w <= 0:
                continue
            p0 = px_map.get(c, {}).get(dt_prev)
            p1 = px_map.get(c, {}).get(dt_today)
            if p0 and p1 and p0 > 0:
                port_ret += w * ((p1 - p0) / p0)
        port_ret -= cost

        # 沪深300 基准收益
        b0 = px_map.get("000300.SH", {}).get(dt_prev)
        b1 = px_map.get("000300.SH", {}).get(dt_today)
        bench_ret = ((b1 - b0) / b0) if (b0 and b1 and b0 > 0) else 0.0

        nav *= (1.0 + port_ret)
        bench_nav *= (1.0 + bench_ret)
        nav_series.append(nav)
        bench_series.append(bench_nav)
        prev_weights = weights

        daily_curve.append({
            "date": dt_today,
            "invested_pct": round(total_weight * 100.0, 1),
            "cash_pct": round((1.0 - total_weight) * 100.0, 1),
            "active_etfs": active_etfs,
            "day_ret_pct": round(port_ret * 100.0, 2),
            "bench_day_ret_pct": round(bench_ret * 100.0, 2),
            "nav": round(nav, 4),
            "bench_nav": round(bench_nav, 4),
        })

    cum_ret_pct = round((nav - 1.0) * 100.0, 2)
    bench_cum_ret_pct = round((bench_nav - 1.0) * 100.0, 2)
    mdd_pct = _calc_max_drawdown_pct(nav_series)
    bench_mdd_pct = _calc_max_drawdown_pct(bench_series)
    ret_dd_ratio = round(cum_ret_pct / abs(mdd_pct), 2) if mdd_pct < -0.01 else None

    result = {
        "schema": "g3_defense_etf_shadow/1",
        "as_of": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "available": True,
        "window_days": len(daily_curve),
        "from_date": daily_curve[0]["date"] if daily_curve else None,
        "to_date": daily_curve[-1]["date"] if daily_curve else None,
        "summary": {
            "cum_ret_pct": cum_ret_pct,
            "bench_cum_ret_pct": bench_cum_ret_pct,
            "excess_ret_pct": round(cum_ret_pct - bench_cum_ret_pct, 2),
            "max_drawdown_pct": mdd_pct,
            "bench_max_drawdown_pct": bench_mdd_pct,
            "drawdown_saved_pct": round(abs(bench_mdd_pct) - abs(mdd_pct), 2),
            "ret_dd_ratio": ret_dd_ratio,
            "switch_events": switch_events,
            "latest_invested_pct": daily_curve[-1]["invested_pct"] if daily_curve else 0.0,
            "latest_active_etfs": daily_curve[-1]["active_etfs"] if daily_curve else [],
        },
        "daily_curve": list(reversed(daily_curve[-20:])),
    }

    if persist:
        store = shadow_dir or runtime_path("g3_etf_shadow")
        store.mkdir(parents=True, exist_ok=True)
        (store / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result
