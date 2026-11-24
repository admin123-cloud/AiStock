# -*- coding: utf-8 -*-
"""G3 Phase 4：多策略三腿互补矩阵与全站动态配额中枢（Strategy Matrix）。

核心职责（打通 Phase 1~4，绝不孤立）：
1. 盘活 config/strategy_contracts/ 下全部 9 份有效策略合约，编排为三条互补策略腿：
   - 进攻腿（offense）：个股机构主升浪 + 板块主升/回踩龙头；
   - 稳健/防守腿（defense_etf）：宽基/风格 ETF 与指数波动率目标趋势（实时扫描 ClickHouse 6 大核心指数）；
   - 增强腿（t_enhance）：G3 三周期做T引擎（R34/R51/R54/R56）。
2. 联动 Phase 1A（大盘择时总闸门）输出动态三腿资金配额（进攻 / 稳健ETF / 底仓做T / 现金，总和 100%）。
3. 联动 Phase 1B（四维归因）、Phase 3A（交易教练）与 R40 影子做T 4 周滚动统计，实时评估每条腿的实盘健康度与治理门禁状态。
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from utils.common.paths import PROJECT_ROOT, runtime_path

BJ_TZ = timezone(timedelta(hours=8))

# 6 大核心宽基/风格指数 -> 对应高流动性场内 ETF 交易标的
DEFENSE_ETF_UNIVERSE: tuple[dict[str, str], ...] = (
    {"index_code": "000016.SH", "index_name": "上证50", "etf_code": "510050.SH", "etf_name": "上证50ETF", "style": "大盘蓝筹"},
    {"index_code": "000300.SH", "index_name": "沪深300", "etf_code": "510300.SH", "etf_name": "沪深300ETF", "style": "核心宽基"},
    {"index_code": "000905.SH", "index_name": "中证500", "etf_code": "510500.SH", "etf_name": "中证500ETF", "style": "中盘均衡"},
    {"index_code": "000852.SH", "index_name": "中证1000", "etf_code": "512100.SH", "etf_name": "中证1000ETF", "style": "小盘成长"},
    {"index_code": "399006.SZ", "index_name": "创业板指", "etf_code": "159915.SZ", "etf_name": "创业板ETF", "style": "成长弹性"},
    {"index_code": "000688.SH", "index_name": "科创50", "etf_code": "588000.SH", "etf_name": "科创50ETF", "style": "硬科技"},
)

# 与 AR3 v3.0 终极双核实证定型对齐的三档市场动态配额（物理单账户总和 = 100%）
# 进攻期：50% AR3-F 基本面个股(AB混合 F3A回踩+F2A趋势) + 50% #01 跨资产ETF(含未满槽闲置现金)
# 平衡/防守期：0% 个股(严禁开新仓) + 100% #01 非A股跨资产ETF(红利/黄金/纳指/恒科/十年国债，极端冰点右侧切反抽)
REGIME_ALLOCATIONS: dict[str, dict[str, float]] = {
    "offensive": {"offense_pct": 50.0, "defense_etf_pct": 50.0, "t_enhance_pct": 0.0, "cash_pct": 0.0},
    "balanced": {"offense_pct": 0.0, "defense_etf_pct": 100.0, "t_enhance_pct": 0.0, "cash_pct": 0.0},
    "defensive": {"offense_pct": 0.0, "defense_etf_pct": 100.0, "t_enhance_pct": 0.0, "cash_pct": 0.0},
}

TARGET_ANNUAL_VOL = 0.15  # 稳健腿波动率目标 15%
_ETF_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
_ETF_CACHE_TTL_SEC = 120.0


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _classify_contract_leg(strategy_id: str, filename: str) -> str:
    key = f"{strategy_id} {filename}".lower()
    if "mainwave" in key or "sector_" in key:
        return "offense"
    if "etf_" in key or "index_" in key or "voltarget" in key:
        return "defense_etf"
    return "t_enhance"


def _load_strategy_contracts(contracts_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    directory = contracts_dir or (PROJECT_ROOT / "config" / "strategy_contracts")
    items: list[dict[str, Any]] = []
    if not directory.exists():
        return items
    for f in sorted(directory.glob("*.json")):
        doc = _read_json_safe(f)
        if not doc:
            continue
        sid = str(doc.get("strategy_id") or f.stem)
        items.append({
            "file": f.name,
            "strategy_id": sid,
            "name": str(doc.get("strategy_name") or doc.get("name") or f.stem),
            "leg": _classify_contract_leg(sid, f.name),
            "status": str(doc.get("status") or doc.get("lifecycle_state") or "shadow"),
            "version": doc.get("contract_version") or doc.get("version"),
            "description": str(doc.get("description") or doc.get("summary") or ""),
        })
    return items


# ---------------------------------------------------------------------------
# 1. 稳健/防守腿：6 大宽基指数波动率目标趋势计算（纯函数 + ClickHouse 读取）
# ---------------------------------------------------------------------------

def compute_voltarget_trend_signal(
    closes: Sequence[float],
    *,
    target_vol: float = TARGET_ANNUAL_VOL,
) -> dict[str, Any]:
    """根据升序收盘价计算 MA20/MA60 趋势方向、20 日年化波动率与目标仓位比例。"""
    vals = [float(x) for x in closes if x is not None and float(x) > 0]
    n = len(vals)
    if n < 25:
        return {
            "signal": "missing",
            "signal_label": "数据不足",
            "close": vals[-1] if vals else None,
            "ma20": None,
            "ma60": None,
            "ret_20d_pct": None,
            "ann_vol_20d_pct": None,
            "vol_target_weight_pct": 0.0,
        }

    close = vals[-1]
    ma20 = sum(vals[-20:]) / 20.0
    ma60 = sum(vals[-60:]) / 60.0 if n >= 60 else sum(vals) / n
    ret_20d = (close - vals[-21]) / vals[-21] * 100.0 if n >= 21 and vals[-21] > 0 else 0.0

    # 20 日对数/简单日收益率标准差年化
    rets = [(vals[i] - vals[i - 1]) / vals[i - 1] for i in range(n - 20, n) if vals[i - 1] > 0]
    if len(rets) >= 5:
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / max(1, len(rets) - 1)
        ann_vol = math.sqrt(var) * math.sqrt(252.0)
    else:
        ann_vol = target_vol

    vol_weight = min(1.0, target_vol / ann_vol) if ann_vol > 0.01 else 1.0

    if close >= ma20 and ma20 >= ma60:
        signal = "long_trend"
        label = "上升趋势·可配"
        weight_pct = round(vol_weight * 100.0, 1)
    elif close >= ma60:
        signal = "neutral"
        label = "震荡观察"
        weight_pct = round(vol_weight * 50.0, 1)
    else:
        signal = "cash_defense"
        label = "破位转现金"
        weight_pct = 0.0

    return {
        "signal": signal,
        "signal_label": label,
        "close": round(close, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "ret_20d_pct": round(ret_20d, 2),
        "ann_vol_20d_pct": round(ann_vol * 100.0, 1),
        "vol_target_weight_pct": weight_pct,
    }


def _fetch_defense_index_closes_from_ch() -> dict[str, tuple[str, list[float]]]:
    """直连 ClickHouse 一次性拉取 6 大宽基指数近 85 个交易日收盘价。fail-safe。"""
    now_mono = time.monotonic()
    if _ETF_CACHE["payload"] is not None and now_mono < float(_ETF_CACHE["expires_at"]):
        return _ETF_CACHE["payload"]

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
            send_receive_timeout=15,
        )
        try:
            res = client.query(
                """
                SELECT code, trade_date, anyLast(close) AS close
                FROM stock.kline_daily
                WHERE code IN {codes:Array(String)}
                  AND trade_date >= today() - 150
                GROUP BY code, trade_date
                ORDER BY code ASC, trade_date ASC
                """,
                parameters={"codes": codes},
            )
            rows = res.result_rows or []
        finally:
            client.close()

        grouped: dict[str, list[tuple[str, float]]] = {}
        for code, dt_val, close_val in rows:
            if close_val is None or float(close_val) <= 0:
                continue
            grouped.setdefault(str(code), []).append((str(dt_val)[:10], float(close_val)))

        out: dict[str, tuple[str, list[float]]] = {}
        for code, seq in grouped.items():
            if seq:
                out[code] = (seq[-1][0], [x[1] for x in seq[-85:]])
        _ETF_CACHE["payload"] = out
        _ETF_CACHE["expires_at"] = now_mono + _ETF_CACHE_TTL_SEC
        return out
    except Exception:
        return {}


def _load_r40_shadow_progress(tracker_dir: Optional[Path] = None) -> dict[str, Any]:
    shadow_dir = (tracker_dir or runtime_path("t_tracker")) / "shadow"
    if not shadow_dir.exists():
        return {"available": False}
    files = sorted(shadow_dir.glob("*_summary.json"), reverse=True)
    for f in files[:3]:
        doc = _read_json_safe(f)
        r4w = doc.get("rolling_4w") if isinstance(doc.get("rolling_4w"), Mapping) else None
        if r4w:
            stats = r4w.get("stats") or {}
            prog = r4w.get("progress") or {}
            return {
                "available": True,
                "as_of_date": doc.get("date"),
                "samples": stats.get("n", 0),
                "win_rate": stats.get("win_rate"),
                "avg_net_pct": stats.get("avg_net_pct"),
                "overall_pass": bool(prog.get("overall_pass", False)),
            }
    return {"available": False}


# ---------------------------------------------------------------------------
# 2. 全站统一策略矩阵主入口
# ---------------------------------------------------------------------------

def build_strategy_matrix(
    *,
    market_gate: Optional[Mapping[str, Any]] = None,
    attribution_summary: Optional[Mapping[str, Any]] = None,
    coach_report: Optional[Mapping[str, Any]] = None,
    contracts_dir: Optional[Path] = None,
    tracker_dir: Optional[Path] = None,
    index_closes_fetcher: Optional[Callable[[], dict[str, tuple[str, list[float]]]]] = None,
) -> dict[str, Any]:
    """构建三腿互补策略矩阵、动态资金配额、防守腿 ETF 趋势与策略治理门禁。"""
    now_bj = datetime.now(BJ_TZ)
    gate = dict(market_gate or {})
    regime = str(gate.get("regime") or "defensive")
    if regime not in REGIME_ALLOCATIONS:
        regime = "defensive"
    regime_label = str(gate.get("regime_label") or "防守模式")
    allow_new = bool(gate.get("allow_new_mainwave", regime != "defensive"))
    wl = gate.get("r51_waterline") if isinstance(gate.get("r51_waterline"), Mapping) else {}

    contracts = _load_strategy_contracts(contracts_dir)
    by_leg: dict[str, list[dict[str, Any]]] = {"offense": [], "defense_etf": [], "t_enhance": []}
    for c in contracts:
        by_leg.setdefault(c["leg"], []).append(c)

    # 实时计算稳健/防守腿 6 大宽基 ETF 趋势信号
    fetcher = index_closes_fetcher or _fetch_defense_index_closes_from_ch
    closes_map = fetcher()
    etf_signals: list[dict[str, Any]] = []
    long_etfs: list[dict[str, Any]] = []
    for item in DEFENSE_ETF_UNIVERSE:
        dt_str, closes = closes_map.get(item["index_code"], (None, []))
        sig = compute_voltarget_trend_signal(closes)
        row = {**item, "as_of_date": dt_str, **sig}
        etf_signals.append(row)
        if sig["signal"] == "long_trend":
            long_etfs.append(row)

    # AR3 v3.0 跨资产 ETF 引擎在 A 股破位时自动切入非 A 股跨资产（红利/黄金/美股/国债），不再把 ETF 预算硬归零
    base_alloc = dict(REGIME_ALLOCATIONS[regime])
    defense_auto_cash = False
    effective_alloc = dict(base_alloc)

    # 读取实盘归因、教练与 R40 影子状态，形成反馈闭环
    attr = dict(attribution_summary or {})
    coach = dict(coach_report or {})
    coach_metrics = coach.get("metrics") if isinstance(coach.get("metrics"), Mapping) else {}
    r40 = _load_r40_shadow_progress(tracker_dir)

    # 组装三条策略腿的健康度与实时状态（统一 AR4.6 终版口径）
    offense_status = "active" if regime == "offensive" else "halted"
    offense_status_label = (
        "AR4.6 进攻期开放（国家队分位动态 0/35/50/65%）"
        if regime == "offensive"
        else "非进攻期锁仓 0%（仅作备选观察）"
    )

    defense_status = "active"
    defense_status_label = (
        "进攻期承接剩余资金（A股宽基Top2）"
        if regime == "offensive"
        else "非进攻期 100% 承接（风险调整Top2逆波 + 南向港科滤网）"
    )

    t_pnl = attr.get("t_trade_pnl")
    stop_viol = int(coach_metrics.get("stop_ignored_codes") or 0)
    if stop_viol > 0:
        t_status = "review_needed"
        t_status_label = f"需先处理 {stop_viol} 只止损违约"
    elif r40.get("available") and not r40.get("overall_pass"):
        t_status = "observe_only"
        t_status_label = "实盘谨慎做T · 仅辅助底仓降本"
    else:
        t_status = "active"
        t_status_label = "按需辅助底仓做T"

    legs = [
        {
            "leg_id": "offense",
            "name": "进攻核 · AR4.6 基本面业绩 × 趋势 (AB混合)",
            "role": "仅右侧进攻期启动：按国家队250日分位动态调节股仓(0/35/50/65%)，F3A+F2A交替填4槽（单行业限1只，-10%硬止损）",
            "target_pct": effective_alloc["offense_pct"],
            "base_pct": base_alloc["offense_pct"],
            "status": offense_status,
            "status_label": offense_status_label,
            "contracts_count": len(by_leg["offense"]),
            "contracts": by_leg["offense"],
            "action_route": "/g3/mainwave",
            "action_label": "查看 AR4.6 双雷达与排雷",
            "feedback_note": (
                "AR4.6 物理单账户实证：4年 +308.0%、年化 +43.5%、回撤 19.1%、卡玛 2.28、OOS +11.3% / OOS回撤 10.7%。"
                + (
                    " 当前处于非进攻期，个股预算锁死 0%，严禁开新个股仓。"
                    if regime != "offensive"
                    else " 当前处于进攻期，按 AR4.6 双核执行单择优填槽（支持一键切换 L4 赚钱效应自动热切）。"
                )
            ),
        },
        {
            "leg_id": "defense_etf",
            "name": "压舱核 · AR4.6 风险调整 Top2 逆波 ETF",
            "role": "14只5大类场内ETF（A股宽基/红利/黄金/纳指标普/恒科/国债）+ 极端冰点企稳反抽",
            "target_pct": effective_alloc["defense_etf_pct"],
            "base_pct": base_alloc["defense_etf_pct"],
            "status": defense_status,
            "status_label": defense_status_label,
            "contracts_count": len(by_leg["defense_etf"]),
            "contracts": by_leg["defense_etf"],
            "action_route": "/g3/desk",
            "action_label": "前往 AR4.6 作战台查看今日目标ETF",
            "feedback_note": (
                "进攻期配置 A 股最强宽基 Top2 承接剩余资金；非进攻期 100% 切入非 A 股风险调整 Top2 逆波组合 + 南向港科滤网；极端冰点企稳自动触发 60% 双创反抽 + 40% 避险。"
            ),
        },
        {
            "leg_id": "t_enhance",
            "name": "辅助核 · 已有持仓反T/风控监控",
            "role": "仅对尚未清退完毕的历史底仓做反T降本与硬止损哨兵巡检（不额外占预算）",
            "target_pct": effective_alloc["t_enhance_pct"],
            "base_pct": base_alloc["t_enhance_pct"],
            "status": t_status,
            "status_label": t_status_label,
            "contracts_count": len(by_leg["t_enhance"]),
            "contracts": by_leg["t_enhance"],
            "action_route": "/g3/desk",
            "action_label": "前往作战台处理底仓",
            "feedback_note": (
                f"近窗做T净额={t_pnl if t_pnl is not None else '—'}；"
                + ("请优先按 AR4.6 执行单清退破止损/非目标个股再谈做T。" if stop_viol > 0 else "盘中 15 分钟哨兵自动盯紧 -10% 硬止损与冰点信号。")
            ),
        },
    ]

    # 策略研发与淘汰 4 道门禁看板（Phase 4 Research Copilot 规范）
    governance_pipeline = [
        {"stage": "1. T+1 与无未来函数审计", "rule": "强制校验 T+1 可卖、统一复权、首根已完成 K 线确认、真实费率滑点", "status": "enforced"},
        {"stage": "2. 4 周实时影子盘", "rule": "胜率与期望收益达标（如 R40 要求胜率≥60%、均值≥+0.3%）才允许晋级", "status": "enforced"},
        {"stage": "3. 小仓位实盘验证", "rule": "10%~20% 试跑并接入四维归因与 L2 硬风控，人工确认下单", "status": "enforced"},
        {"stage": "4. 季度归因末位淘汰", "rule": "与现有腿高相关或连续季度负 Alpha 策略自动降权/下线", "status": "scheduled"},
    ]

    return {
        "schema": "g3_strategy_matrix/1",
        "as_of": now_bj.isoformat(timespec="seconds"),
        "regime": regime,
        "regime_label": regime_label,
        "allow_new_mainwave": allow_new,
        "defense_auto_cash": defense_auto_cash,
        "allocation": {
            **effective_alloc,
            "total_invest_cap_pct": round(100.0 - effective_alloc["cash_pct"], 1),
            "actual_position_pct": wl.get("actual_pct"),
            "waterline_status": wl.get("status"),
        },
        "legs": legs,
        "defense_etf_signals": etf_signals,
        "r40_shadow_progress": r40,
        "governance_pipeline": governance_pipeline,
        "contracts_total": len(contracts),
    }
