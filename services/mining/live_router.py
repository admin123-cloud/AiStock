# -*- coding: utf-8 -*-
"""AiStock 周期自适应策略路由器与实时全市场选股扫描器（Step 2 交易闭环中枢）。

核心职责：
1. 维护五态大盘周期（offensive / extreme_offensive / balanced / defensive / extreme_panic）
   到挖掘影子合约（runtime/mining_contracts/*.json）或现金防御（cash_defense）的映射表；
2. 每日依据 Market Regime Gate 当前周期，自动路由出「今日生效策略」；
3. 读取最新全市场特征宽表截面，按生效策略明文 DSL 条件实时选股、叠加 AI 排雷哨兵，
   输出含参考买价 / 硬止损价 / 止盈价 / 持有天数的可执行计划，供 /g3/desk 一键 L2 预检与同花顺预填。

安全铁律：严格零自动下单，任何交易必须经人工 L2 预检与同花顺手动确认。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from services.g3.ai_screening import load_saved_screening, lookup_stock_ai_sentinel
from services.mining.backtest_engine import apply_entry_conditions
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))

ALL_REGIMES: list[dict[str, str]] = [
    {"regime": "offensive", "label": "主升进攻"},
    {"regime": "extreme_offensive", "label": "极端高潮"},
    {"regime": "balanced", "label": "横盘平衡"},
    {"regime": "defensive", "label": "防守退潮"},
    {"regime": "extreme_panic", "label": "极端恐慌"},
]

# 默认周期推荐绑定（仅绑定已通过 5/5 硬门禁的真金影子合约；防守/恐慌默认现金防御）
_DEFAULT_PREFERENCE: dict[str, list[str]] = {
    "offensive": [
        "mined_mw_hyp_04_rs_momentum_clean_candle_shadow_v1",
        "mined_mw_hyp_01_trend_breakout_sector_shadow_v1",
    ],
    "extreme_offensive": [
        "mined_mw_hyp_01_trend_breakout_sector_shadow_v1",
        "mined_mw_hyp_04_rs_momentum_clean_candle_shadow_v1",
    ],
    "balanced": [
        "mined_sector_rotation_tree_05_shadow_v1",
    ],
    "defensive": ["cash_defense"],
    "extreme_panic": ["cash_defense"],
}


def _contracts_dir(base_dir: Optional[Path] = None) -> Path:
    d = base_dir or runtime_path("mining_contracts")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _router_config_path(base_dir: Optional[Path] = None) -> Path:
    return _contracts_dir(base_dir) / "regime_router.json"


def _load_all_contracts(base_dir: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    c_dir = _contracts_dir(base_dir)
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(c_dir.glob("*.json")):
        if f.name == "regime_router.json":
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("strategy_id"):
                out[str(doc["strategy_id"])] = doc
        except Exception:
            continue
    return out


def load_regime_router_config(base_dir: Optional[Path] = None) -> dict[str, Any]:
    """读取五态周期路由配置；未手动配置时按默认 5/5 冠军合约智能初始化。"""
    contracts = _load_all_contracts(base_dir)
    cfg_path = _router_config_path(base_dir)
    saved_routes: dict[str, str] = {}
    updated_at: Optional[str] = None
    if cfg_path.exists():
        try:
            doc = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                saved_routes = {str(k): str(v) for k, v in (doc.get("routes") or {}).items()}
                updated_at = doc.get("updated_at")
        except Exception:
            saved_routes = {}

    routes: dict[str, str] = {}
    for item in ALL_REGIMES:
        rg = item["regime"]
        chosen = saved_routes.get(rg)
        if chosen and (chosen == "cash_defense" or chosen in contracts):
            routes[rg] = chosen
            continue
        # 智能默认回退
        picked = "cash_defense"
        for pref in _DEFAULT_PREFERENCE.get(rg, []):
            if pref == "cash_defense" or pref in contracts:
                picked = pref
                break
        routes[rg] = picked

    return {
        "schema": "g3_regime_router/1",
        "updated_at": updated_at,
        "routes": routes,
        "regimes": ALL_REGIMES,
        "available_contracts": [
            {
                "strategy_id": sid,
                "display_name": c.get("display_name") or sid,
                "all_gates_passed": bool((c.get("origin") or {}).get("all_gates_passed")),
                "mining_score": (c.get("origin") or {}).get("mining_score"),
                "target_regimes": (c.get("entry") or {}).get("target_regimes") or [],
            }
            for sid, c in contracts.items()
        ],
    }


def update_regime_route(
    regime: str,
    strategy_id: str,
    *,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """将指定大盘周期绑定到某挖掘影子合约或 cash_defense（空仓防守）。"""
    valid_regimes = {x["regime"] for x in ALL_REGIMES}
    if regime not in valid_regimes:
        raise ValueError(f"未知大盘周期: {regime}")
    contracts = _load_all_contracts(base_dir)
    sid = str(strategy_id or "cash_defense").strip()
    if sid != "cash_defense" and sid not in contracts:
        raise ValueError(f"未找到影子合约 strategy_id={sid}")

    cfg = load_regime_router_config(base_dir)
    routes = dict(cfg["routes"])
    routes[regime] = sid
    now_str = datetime.now(BJ_TZ).isoformat(timespec="seconds")
    payload = {
        "schema": "g3_regime_router/1",
        "updated_at": now_str,
        "routes": routes,
    }
    _router_config_path(base_dir).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_regime_router_config(base_dir)


def _load_latest_feature_slice(cache_dir: Optional[Path] = None) -> tuple[Optional[str], pd.DataFrame]:
    """读取本地最新 Parquet 特征宽表的最后 1 个交易日截面（毫秒级，零阻塞）。"""
    c_dir = cache_dir or runtime_path("mining_cache")
    if not c_dir.exists():
        return None, pd.DataFrame()
    parquets = sorted(c_dir.glob("panel_*.parquet"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not parquets:
        return None, pd.DataFrame()
    try:
        df = pq.read_table(parquets[0]).to_pandas()
        if df.empty or "trade_date" not in df.columns:
            return None, pd.DataFrame()
        latest_date = str(df["trade_date"].max())
        sl = df[df["trade_date"] == latest_date].copy()
        return latest_date, sl
    except Exception:
        return None, pd.DataFrame()


def scan_active_strategy_signals(
    *,
    current_regime: str,
    regime_label: str = "",
    allow_new_mainwave: bool = False,
    target_position_cap_pct: float = 0.0,
    top_n: int = 5,
    contracts_dir: Optional[Path] = None,
    cache_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """根据当前大盘五态周期自动路由生效策略，并实时扫描最新全市场截面输出可执行买卖计划。"""
    router_cfg = load_regime_router_config(contracts_dir)
    contracts = _load_all_contracts(contracts_dir)
    active_sid = router_cfg["routes"].get(current_regime, "cash_defense")

    base_resp: dict[str, Any] = {
        "schema": "g3_live_strategy_router/1",
        "as_of": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "current_regime": current_regime,
        "regime_label": regime_label or current_regime,
        "allow_new_mainwave": bool(allow_new_mainwave),
        "target_position_cap_pct": float(target_position_cap_pct),
        "active_strategy_id": active_sid,
        "mode": "cash_defense" if active_sid == "cash_defense" else "active_strategy",
        "routes": router_cfg["routes"],
        "available_contracts": router_cfg["available_contracts"],
    }

    if active_sid == "cash_defense" or active_sid not in contracts:
        return {
            **base_resp,
            "active_strategy_name": "现金防守 / 严禁开新仓",
            "strategy_thesis": "当前大盘处于防守/恐慌退潮周期，经全市场盲测实证，空仓避险 + 宽基 ETF 趋势防守腿为最优解。",
            "readable_conditions": ["大盘防守/恐慌周期自动转现金，不强行开新仓"],
            "exit_plan": None,
            "data_date": None,
            "matched_total": 0,
            "picks": [],
            "action_hint": "保持低水位/现金防守，已有持仓优先反弹减水位或反T降本。",
        }

    contract = contracts[active_sid]
    entry = contract.get("entry") or {}
    exit_pol = contract.get("exit") or {}
    origin = contract.get("origin") or {}
    dsl_conds = entry.get("dsl_conditions") or []
    rank_by = str(entry.get("rank_by") or "rs_20d_vs_hs300")
    rank_asc = bool(entry.get("rank_ascending", False))
    hold_days = int(exit_pol.get("hold_days") or 5)
    stop_pct = abs(float(exit_pol.get("hard_stop_loss_pct") or 0.07))
    tp_pct = abs(float(exit_pol.get("take_profit_pct") or 0.15))

    latest_date, sl = _load_latest_feature_slice(cache_dir)
    if sl.empty or not dsl_conds:
        return {
            **base_resp,
            "active_strategy_name": contract.get("display_name") or active_sid,
            "strategy_thesis": contract.get("thesis") or "",
            "readable_conditions": entry.get("readable_conditions") or [],
            "exit_plan": {"hold_days": hold_days, "stop_loss_pct": -stop_pct, "take_profit_pct": tp_pct},
            "data_date": latest_date,
            "matched_total": 0,
            "picks": [],
            "action_hint": "全市场特征宽表尚未刷新，请先在「AI 策略挖掘」运行一次挖掘或等待周日自动更新。",
        }

    # 最新交易日截面没有 T+1 开盘价，临时将 entry_tradable 置 True 仅用于执行入场条件筛选
    sl["entry_tradable"] = True
    mask = apply_entry_conditions(sl, dsl_conds, target_regimes=None)
    matched = sl.loc[mask].copy()
    matched_total = int(len(matched))

    picks: list[dict[str, Any]] = []
    if matched_total > 0:
        if rank_by in matched.columns:
            matched.sort_values(rank_by, ascending=rank_asc, inplace=True)
        saved_screening = load_saved_screening()
        slots = max(1, int((contract.get("portfolio") or {}).get("slots") or 3))
        per_slot_cap = round(float(target_position_cap_pct) / slots, 1) if target_position_cap_pct > 0 else 10.0

        for _, row in matched.head(max(top_n, slots)).iterrows():
            code_full = str(row["code"])
            code6 = code_full.split(".")[0].zfill(6)
            close_px = float(row["close"])
            stop_px = round(close_px * (1.0 - stop_pct), 2)
            tp_px = round(close_px * (1.0 + tp_pct), 2)
            sentinel = lookup_stock_ai_sentinel(code6, saved_doc=saved_screening)
            risk_light = (sentinel or {}).get("risk_light") or "unknown"
            ai_blocked = risk_light == "red"
            picks.append(
                {
                    "code": code6,
                    "code_full": code_full,
                    "name": str(row.get("name") or code6),
                    "industry": str(row.get("industry") or ""),
                    "ref_price": round(close_px, 2),
                    "stop_loss_price": stop_px,
                    "take_profit_price": tp_px,
                    "hold_days": hold_days,
                    "suggested_slot_pct": per_slot_cap,
                    "ret_1d_pct": round(float(row.get("ret_1d_pct") or 0.0), 2),
                    "ret_5d_pct": round(float(row.get("ret_5d_pct") or 0.0), 2),
                    "rs_20d_vs_hs300": round(float(row.get("rs_20d_vs_hs300") or 0.0), 2),
                    "industry_above_ma20_pct": round(float(row.get("industry_above_ma20_pct") or 0.0), 1),
                    "rank_metric_name": rank_by,
                    "rank_metric_val": round(float(row.get(rank_by) or 0.0), 2) if rank_by in row else None,
                    "ai_risk_light": risk_light,
                    "ai_blocked": ai_blocked,
                    "ai_thesis": (sentinel or {}).get("one_line_thesis") or "",
                }
            )

    return {
        **base_resp,
        "active_strategy_name": contract.get("display_name") or active_sid,
        "strategy_thesis": contract.get("thesis") or "",
        "all_gates_passed": bool(origin.get("all_gates_passed")),
        "mining_score": origin.get("mining_score"),
        "readable_conditions": entry.get("readable_conditions") or [],
        "exit_plan": {
            "hold_days": hold_days,
            "stop_loss_pct": round(-stop_pct * 100.0, 1),
            "take_profit_pct": round(tp_pct * 100.0, 1),
        },
        "data_date": latest_date,
        "matched_total": matched_total,
        "picks": picks,
        "action_hint": (
            f"今日处于「{regime_label or current_regime}」周期，已自动激活 {contract.get('display_name') or active_sid}；"
            f"共命中 {matched_total} 只标的，展示 Top {len(picks)}（点击可一键带入 L2 预检与同花顺预填，AI 红灯标的一票否决）。"
            if matched_total > 0
            else f"今日「{contract.get('display_name') or active_sid}」严格按明文条件扫描全市场，0 只达标，遵循「空仓也是有效决策」。"
        ),
    }
