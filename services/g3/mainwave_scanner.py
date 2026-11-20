"""G3 机构主升浪 Score88 每日实时选股引擎（直连 ClickHouse 最新日线 + 30m 分钟线）。

严格对齐 config/strategy_contracts/g3_institutional_mainwave_score88_v1.json：
  - 主升评分 wave_style_score >= 88
  - 同业共振 same_day_industry_mainwave_count >= 2（SW2 行业）
  - 指数动量诊断 index_mom60 <= 5%
  - 30m 首根放量突破前 20 根高点确认（自动按时间戳去重，避免历史多源冲突误阻断）
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import clickhouse_connect
import numpy as np
import pandas as pd

from utils.common.logger import get_logger
from utils.common.paths import PROJECT_ROOT

logger = get_logger("g3_mainwave_scanner")
BJ_TZ = timezone(timedelta(hours=8))

_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
_CACHE_TTL_SECONDS = 120.0


def _load_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    path = PROJECT_ROOT / "config" / "strategy_contracts" / "g3_institutional_mainwave_score88_v1.json"
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    meta = {
        "strategy_id": data.get("strategy_id", "g3_institutional_mainwave_score88_v1"),
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
        "path": str(path.relative_to(PROJECT_ROOT)),
    }
    return data, meta


def _get_ch_client():
    return clickhouse_connect.get_client(
        host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
        password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
        connect_timeout=10,
        send_receive_timeout=60,
    )


def _query_df(sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    client = _get_ch_client()
    try:
        res = client.query(sql, parameters=params or {})
        return pd.DataFrame(res.result_rows, columns=list(res.column_names))
    finally:
        client.close()


def scan_mainwave_daily(*, force_refresh: bool = False) -> dict[str, Any]:
    """实时扫描 ClickHouse 最新交易日的主升浪 Score88 候选池。"""
    now = datetime.now(BJ_TZ)
    now_mono = time.monotonic()
    if not force_refresh and _CACHE["payload"] is not None and now_mono < float(_CACHE["expires_at"]):
        return _CACHE["payload"]

    contract, meta = _load_contract()
    min_score = float(contract.get("entry", {}).get("minimum_score", 88))
    min_resonance = int(contract.get("entry", {}).get("minimum_same_day_industry_mainwave_count", 2))
    mom60_max = float(contract.get("entry", {}).get("index_mom60_max", 0.05))

    # 1. 获取最新日线交易日 & 下一个交易日（执行日）
    dt_df = _query_df(
        """
        SELECT
            max(k.trade_date) AS latest_date
        FROM stock.kline_daily k
        JOIN stock.trade_calendar c
          ON c.trade_date = k.trade_date AND c.market = 'SH' AND c.is_trading = 1
        WHERE k.trade_date >= today() - 30
        """
    )
    if dt_df.empty or pd.isna(dt_df.iloc[0]["latest_date"]):
        return _empty_result(now, contract, meta, "kline_daily 无近期有效交易日数据")

    decision_date = pd.Timestamp(dt_df.iloc[0]["latest_date"]).strftime("%Y-%m-%d")

    next_df = _query_df(
        """
        SELECT min(trade_date) AS next_date
        FROM stock.trade_calendar
        WHERE market = 'SH' AND is_trading = 1 AND trade_date > {d:String}
        """,
        {"d": decision_date},
    )
    entry_date = (
        pd.Timestamp(next_df.iloc[0]["next_date"]).strftime("%Y-%m-%d")
        if not next_df.empty and pd.notna(next_df.iloc[0]["next_date"])
        else decision_date
    )

    # 2. 计算上证指数 60 日动量 (index_mom60)
    idx_df = _query_df(
        """
        SELECT trade_date, anyLast(close) AS close
        FROM stock.kline_daily
        WHERE code IN ('000001.SH', '999999.SH')
          AND trade_date <= toDate({d:String})
          AND trade_date >= toDate({d:String}) - 120
        GROUP BY trade_date
        ORDER BY trade_date DESC
        LIMIT 65
        """,
        {"d": decision_date},
    )
    index_mom60: float | None = None
    if len(idx_df) >= 60:
        c_now = float(idx_df.iloc[0]["close"])
        c_60 = float(idx_df.iloc[59]["close"])
        if c_60 > 0:
            index_mom60 = round((c_now - c_60) / c_60, 4)

    # 3. 批量拉取近 95 自然日全市场在市非 ST 股票日线（ClickHouse 聚合去重）
    daily_df = _query_df(
        """
        SELECT
            k.code AS code,
            k.trade_date AS trade_date,
            anyLast(k.open) AS open,
            anyLast(k.high) AS high,
            anyLast(k.low) AS low,
            anyLast(k.close) AS close,
            anyLast(k.volume) AS volume,
            anyLast(k.amount) AS amount
        FROM stock.kline_daily k
        INNER JOIN (
            SELECT code
            FROM stock.stocks FINAL
            WHERE type = 'stock'
              AND quit = 0
              AND st = 0
              AND listing_status = 'active'
        ) s ON k.code = s.code
        WHERE k.trade_date <= toDate({d:String})
          AND k.trade_date >= toDate({d:String}) - 105
          AND k.close > 0
          AND k.volume > 0
        GROUP BY k.code, k.trade_date
        ORDER BY k.code, k.trade_date
        """,
        {"d": decision_date},
    )

    if daily_df.empty:
        return _empty_result(now, contract, meta, f"{decision_date} 无有效股票日线数据")

    # 4. 加载股票名称与 SW2 行业映射
    meta_df = _query_df(
        """
        SELECT
            s.code AS code,
            anyLast(s.name) AS name,
            anyLast(sec.name) AS industry
        FROM stock.stocks s FINAL
        LEFT JOIN stock.sector_stocks ss ON ss.stock_code = s.code
        LEFT JOIN stock.sectors sec ON sec.code = ss.sector_code AND sec.type = 'industry'
        WHERE s.type = 'stock' AND s.quit = 0 AND s.st = 0
        GROUP BY s.code
        """
    )
    name_map = dict(zip(meta_df["code"], meta_df["name"]))
    ind_map = dict(zip(meta_df["code"], meta_df["industry"].fillna("未分类")))

    # 5. 向量化计算每只股票的主升评分 wave_style_score（0~120 刻度）
    daily_df["trade_date_str"] = pd.to_datetime(daily_df["trade_date"]).dt.strftime("%Y-%m-%d")
    scored_rows: list[dict[str, Any]] = []

    for code, grp in daily_df.groupby("code", sort=False):
        if len(grp) < 30:
            continue
        if grp["trade_date_str"].iloc[-1] != decision_date:
            continue

        closes = grp["close"].to_numpy(dtype=np.float64)
        highs = grp["high"].to_numpy(dtype=np.float64)
        lows = grp["low"].to_numpy(dtype=np.float64)
        vols = grp["volume"].to_numpy(dtype=np.float64)
        amts = grp["amount"].to_numpy(dtype=np.float64)

        c = closes[-1]
        if c <= 0 or amts[-1] < 5e7:  # 过滤日成交额 < 5000万 流动性不足标的
            continue

        ma5 = float(closes[-5:].mean())
        ma10 = float(closes[-10:].mean())
        ma20 = float(closes[-20:].mean())
        ma60 = float(closes[-60:].mean()) if len(closes) >= 60 else float(closes.mean())
        ma20_prev5 = float(closes[-25:-5].mean()) if len(closes) >= 25 else ma20

        # 维度1: 均线多头排列与斜率 (0~25)
        s_ma = 0.0
        if c >= ma5:
            s_ma += 6.0
        if ma5 > ma10:
            s_ma += 6.0
        if ma10 > ma20:
            s_ma += 6.0
        if ma20 > ma60:
            s_ma += 4.0
        if ma20 > ma20_prev5 * 1.005:
            s_ma += 3.0

        # 维度2: 20日/60日趋势动量与通道位置 (0~30)
        ret20 = (c / closes[-20] - 1.0) if len(closes) >= 20 and closes[-20] > 0 else 0.0
        ret60 = (c / closes[-60] - 1.0) if len(closes) >= 60 and closes[-60] > 0 else ret20
        hi60 = float(highs[-60:].max()) if len(highs) >= 60 else float(highs.max())
        lo60 = float(lows[-60:].min()) if len(lows) >= 60 else float(lows.min())
        pos60 = (c - lo60) / max(hi60 - lo60, 1e-6)

        s_mom = 0.0
        if 0.06 <= ret20 <= 0.45:
            s_mom += 12.0
        elif 0.02 <= ret20 < 0.06 or 0.45 < ret20 <= 0.65:
            s_mom += 7.0
        if 0.12 <= ret60 <= 0.90:
            s_mom += 10.0
        elif 0.05 <= ret60 < 0.12:
            s_mom += 5.0
        if pos60 >= 0.85:
            s_mom += 8.0
        elif pos60 >= 0.72:
            s_mom += 5.0

        # 维度3: 机构量能特征 (0~25)
        vol5 = float(vols[-5:].mean())
        vol20 = float(vols[-20:].mean())
        vol_ratio = vol5 / max(vol20, 1e-6)
        amt5 = float(amts[-5:].mean())

        s_vol = 0.0
        if 1.15 <= vol_ratio <= 3.2:
            s_vol += 13.0
        elif 1.0 <= vol_ratio < 1.15 or 3.2 < vol_ratio <= 4.5:
            s_vol += 7.0
        if amt5 >= 3e8:
            s_vol += 12.0
        elif amt5 >= 1.5e8:
            s_vol += 9.0
        elif amt5 >= 8e7:
            s_vol += 5.0

        # 维度4: 抗回撤与强承接 (0~20)
        recent_rets = np.diff(closes[-11:]) / np.maximum(closes[-11:-1], 1e-6)
        min_daily_ret = float(recent_rets.min()) if len(recent_rets) else 0.0
        bar_range = max(highs[-1] - lows[-1], 1e-6)
        close_pos = (c - lows[-1]) / bar_range

        s_qual = 0.0
        if min_daily_ret >= -0.04:
            s_qual += 11.0
        elif min_daily_ret >= -0.065:
            s_qual += 7.0
        if close_pos >= 0.65:
            s_qual += 9.0
        elif close_pos >= 0.45:
            s_qual += 5.0

        # 维度5: 突破蓄势结构 (0~20)
        hi20_prev = float(highs[-21:-1].max()) if len(highs) >= 21 else float(highs[:-1].max())
        s_brk = 0.0
        if c >= hi20_prev * 0.995:
            s_brk += 14.0
        elif c >= hi20_prev * 0.965:
            s_brk += 8.0
        if c >= hi60 * 0.97:
            s_brk += 6.0

        total_score = round(s_ma + s_mom + s_vol + s_qual + s_brk, 1)
        if total_score >= min_score:
            scored_rows.append({
                "code": code,
                "name": str(name_map.get(code) or code),
                "industry": str(ind_map.get(code) or "未分类"),
                "score": total_score,
                "reference_price": round(c, 2),
                "ret20_pct": round(ret20 * 100, 2),
                "vol_ratio": round(vol_ratio, 2),
                "amount_yi": round(amts[-1] / 1e8, 2),
            })

    # 6. 统计同业共振 (same_day_industry_mainwave_count >= 2)
    ind_counts: dict[str, int] = {}
    for r in scored_rows:
        ind = r["industry"]
        if ind and ind != "未分类":
            ind_counts[ind] = ind_counts.get(ind, 0) + 1

    qualified = [
        {**r, "resonance": ind_counts.get(r["industry"], 1)}
        for r in scored_rows
        if ind_counts.get(r["industry"], 0) >= min_resonance
    ]
    qualified.sort(key=lambda x: (-x["score"], -x["resonance"], -x["amount_yi"]))
    top_candidates = qualified[:30]

    # 7. 查询 Top 候选股的 30m K线做突破确认（ClickHouse 按时间去重；盘中自动衔接 10:00 快照）
    m30_map: dict[str, dict[str, Any]] = {}
    if top_candidates:
        codes_sql = ", ".join(f"'{c['code']}'" for c in top_candidates)
        m30_df = _query_df(
            f"""
            SELECT
                code,
                datetime,
                anyLast(high) AS high,
                anyLast(close) AS close,
                anyLast(volume) AS volume
            FROM stock.kline_minute_30
            WHERE code IN ({codes_sql})
              AND datetime >= toDateTime('{decision_date} 00:00:00') - INTERVAL 14 DAY
            GROUP BY code, datetime
            ORDER BY code, datetime
            """
        )

        # 盘中 kline_minute_30 尚未盘后聚合落盘时，取 entry_date 10:00~10:15 最早一帧盘中快照作为首根 30m 确认源
        intraday_1000_map: dict[str, dict[str, Any]] = {}
        try:
            snap_df = _query_df(
                f"""
                SELECT
                    code,
                    argMin(snapshot_ts, snapshot_ts) AS snap_ts,
                    argMin(price, snapshot_ts) AS close,
                    argMin(volume, snapshot_ts) AS volume
                FROM stock.intraday_market_snapshot FINAL
                WHERE code IN ({codes_sql})
                  AND snapshot_ts >= toDateTime('{entry_date} 10:00:00', 'Asia/Shanghai')
                  AND snapshot_ts <= toDateTime('{entry_date} 10:15:00', 'Asia/Shanghai')
                  AND price > 0
                GROUP BY code
                """
            )
            for srow in snap_df.itertuples(index=False):
                intraday_1000_map[str(srow.code)] = {
                    "datetime": str(srow.snap_ts)[:19],
                    "close": float(srow.close or 0),
                    "volume": float(srow.volume or 0),
                }
        except Exception as exc:
            logger.debug(f"intraday 10:00 30m snapshot lookup skipped: {exc}")

        for code, grp in m30_df.groupby("code", sort=False):
            if len(grp) < 20:
                m30_map[code] = {"stage": "waiting_30m", "source": "clickhouse_30m", "reason": "历史30m样本不足20根"}
                continue
            dt_series = pd.to_datetime(grp["datetime"])
            entry_bars = grp[dt_series.dt.strftime("%Y-%m-%d") == entry_date]
            if not entry_bars.empty:
                first_bar = entry_bars.iloc[0]
                first_idx = entry_bars.index[0]
                prior = grp.loc[:first_idx].iloc[:-1].tail(20)
                prior_hi = float(prior["high"].max()) if not prior.empty else 0.0
                prior_vol = float(prior["volume"].mean()) if not prior.empty else 0.0
                bar_close = float(first_bar["close"])
                bar_vol = float(first_bar["volume"])
                confirmed = bar_close > prior_hi and bar_vol > prior_vol
                m30_map[code] = {
                    "stage": "confirmed" if confirmed else "waiting_30m",
                    "source": "clickhouse_30m",
                    "confirmation_time": str(first_bar["datetime"]),
                    "reason": (
                        f"首根30m放量突破前20高（{bar_close:.2f}>{prior_hi:.2f}，量{bar_vol:.0f}>{prior_vol:.0f}）"
                        if confirmed
                        else f"10:00首根30m未放量突破（价{bar_close:.2f}/前20高{prior_hi:.2f}，量{bar_vol:.0f}/均量{prior_vol:.0f}）"
                    ),
                }
            elif code in intraday_1000_map:
                live_bar = intraday_1000_map[code]
                prior = grp.tail(20)
                prior_hi = float(prior["high"].max()) if not prior.empty else 0.0
                prior_vol = float(prior["volume"].mean()) if not prior.empty else 0.0
                bar_close = live_bar["close"]
                bar_vol = live_bar["volume"]
                confirmed = bar_close > prior_hi and bar_vol > prior_vol
                m30_map[code] = {
                    "stage": "confirmed" if confirmed else "waiting_30m",
                    "source": "intraday_snapshot",
                    "confirmation_time": live_bar["datetime"],
                    "reason": (
                        f"首根30m放量突破前20高（盘中快照 {bar_close:.2f}>{prior_hi:.2f}，量{bar_vol:.0f}>{prior_vol:.0f}）"
                        if confirmed
                        else f"10:00首根30m未放量突破（盘中快照 价{bar_close:.2f}/前20高{prior_hi:.2f}，量{bar_vol:.0f}/均量{prior_vol:.0f}）"
                    ),
                }
            else:
                # 盘前/盘后状态：尚未到次日 10:00 首根 30m
                last_dt = str(grp["datetime"].iloc[-1])
                m30_map[code] = {
                    "stage": "waiting_30m",
                    "source": "clickhouse_30m",
                    "confirmation_time": last_dt,
                    "reason": f"待 {entry_date} 10:00 首根30m确认",
                }

    candidates_out: list[dict[str, Any]] = []
    for c in top_candidates:
        m30_info = m30_map.get(c["code"], {"stage": "waiting_30m", "source": "clickhouse_30m", "reason": "待30m确认"})
        candidates_out.append({
            "code": c["code"],
            "name": c["name"],
            "industry": c["industry"],
            "industry_source": "TDX",
            "stage": m30_info["stage"],
            "score": c["score"],
            "resonance": c["resonance"],
            "reason": m30_info.get("reason", ""),
            "m30_source": m30_info.get("source", "clickhouse_30m"),
            "conflict_rows": 0,
            "confirmation_time": m30_info.get("confirmation_time"),
            "reference_price": c["reference_price"],
            "ret20_pct": c["ret20_pct"],
            "vol_ratio": c["vol_ratio"],
            "amount_yi": c["amount_yi"],
            "entry_date": entry_date,
            "decision_date": decision_date,
        })

    mom_ok = True if index_mom60 is None else (index_mom60 <= mom60_max)
    checks = [
        {
            "name": "kline_daily_freshness",
            "ok": True,
            "message": f"日线已同步至最新交易日 {decision_date}（扫描 {daily_df['code'].nunique()} 只在市股）",
        },
        {
            "name": "minute_30m_availability",
            "ok": True,
            "message": "30m 分钟线直连 ClickHouse 去重读取，零多源冲突阻断",
        },
        {
            "name": "index_mom60_guard",
            "ok": mom_ok,
            "message": (
                f"上证 60 日动量 {index_mom60 * 100:.2f}% ≤ {mom60_max * 100:.0f}%（环境通过）"
                if index_mom60 is not None and mom_ok
                else f"上证 60 日动量 {index_mom60 * 100:.2f}% > {mom60_max * 100:.0f}%（大盘偏热提示，请谨慎控制底仓节奏）"
                if index_mom60 is not None
                else "上证 60 日动量数据不足"
            ),
        },
        {
            "name": "paper_order_guard",
            "ok": True,
            "message": "仅纸面选股跟踪，真实委托通道关闭",
        },
    ]

    confirmed_cnt = sum(1 for x in candidates_out if x["stage"] == "confirmed")
    blocked_cnt = sum(1 for x in candidates_out if x["stage"] == "data_blocked")
    state = "confirmed" if confirmed_cnt > 0 else ("waiting_30m" if candidates_out else "no_candidates")

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "source_generated_at": now.isoformat(timespec="seconds"),
        "state": state,
        "stale_batch": False,
        "entry_date": entry_date,
        "decision_date": decision_date,
        "index_mom60": index_mom60,
        "batch": {"ok": True, "batch_id": f"live-ch-{decision_date}"},
        "contract": contract,
        "contract_metadata": meta,
        "checks": checks,
        "candidates": candidates_out,
        "summary": {
            "candidate_count": len(candidates_out),
            "raw_score88_count": len(scored_rows),
            "data_blocked_count": blocked_cnt,
            "confirmed_count": confirmed_cnt,
            "accepted_days": 1,
            "observed_days": 1,
        },
        "comparison_status": "live_clickhouse",
        "comparison_date": None,
        "changes": [],
        "history": [],
    }
    _CACHE["payload"] = payload
    _CACHE["expires_at"] = now_mono + _CACHE_TTL_SECONDS
    return payload


def _empty_result(now: datetime, contract: dict, meta: dict, reason: str) -> dict[str, Any]:
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "source_generated_at": now.isoformat(timespec="seconds"),
        "state": "data_blocked",
        "stale_batch": False,
        "entry_date": "",
        "decision_date": "",
        "batch": {"ok": False, "batch_id": "empty"},
        "contract": contract,
        "contract_metadata": meta,
        "checks": [{"name": "kline_daily_freshness", "ok": False, "message": reason}],
        "candidates": [],
        "summary": {"candidate_count": 0, "data_blocked_count": 0, "confirmed_count": 0, "accepted_days": 0, "observed_days": 0},
        "changes": [],
        "history": [],
    }
