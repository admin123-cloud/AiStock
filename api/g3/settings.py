"""G3 策略合约与风控配置 API：展示生效中的做T规则（R34/R53/R54）、L2 风控闸门与保留的有效策略合约。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from api.g3.desk import get_g3_market_gate
from services.g3.attribution import build_four_dim_attribution
from services.g3.strategy_matrix import build_strategy_matrix
from services.g3.trading_coach import build_trading_coach_report
from utils.common.paths import PROJECT_ROOT, runtime_path

router = APIRouter()


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@router.get("/settings")
def get_g3_settings() -> dict[str, Any]:
    """返回当前生效的 G3 做T参数、L2 风控配置与保留的有效策略合约清单。"""
    l2_risk = _read_json_safe(runtime_path("t_tracker", "l2_risk_config.json"))
    contract_stop = _read_json_safe(runtime_path("t_tracker", "contract_stop.json"))

    contracts_dir = PROJECT_ROOT / "config" / "strategy_contracts"
    contracts: list[dict[str, Any]] = []
    if contracts_dir.exists():
        for f in sorted(contracts_dir.glob("*.json")):
            data = _read_json_safe(f)
            if not data:
                continue
            contracts.append({
                "file": f.name,
                "strategy_id": data.get("strategy_id") or f.stem,
                "name": data.get("strategy_name") or data.get("name") or f.stem,
                "status": data.get("status") or data.get("lifecycle_state") or "unknown",
                "version": data.get("contract_version") or data.get("version"),
                "description": data.get("description") or data.get("summary") or "",
                "entry": data.get("entry") or {},
                "exit": data.get("exit") or {},
                "risk": data.get("risk") or {},
            })

    matrix = _build_live_matrix()
    return {
        "t_trade_rules": {
            "regime_engine": "R34 日线三周期判定（MA5/MA10/MA20 + pos20）",
            "direction_gate": "R53 混合门（日线定许可 + 横盘日 15m 连续6根走弱禁买T）",
            "uptrend_params": {
                "source": "R54 Owner 裁决（卖晚买早）",
                "sell_trim_rsi5m": 75,
                "sell_exit_rsi5m": 80,
                "buy_normal_rsi5m": 35,
                "buy_conviction_rsi5m": 20,
            },
            "position_discipline": {
                "t_lot_formula": "floor(当前底仓股数 / 3 / 100) × 100",
                "max_daily_triggers_per_stock": 2,
                "cooldown_minutes": 30,
                "lunch_gate": "R56 午休闸（建议不等于成交）",
            },
        },
        "strategy_matrix": matrix,
        "l2_risk_config": l2_risk,
        "contract_stops": contract_stop,
        "strategy_contracts": contracts,
    }


def _build_live_matrix() -> dict[str, Any]:
    try:
        gate = get_g3_market_gate()
    except Exception:
        gate = None
    try:
        attr_sum = build_four_dim_attribution(days=30).get("summary")
    except Exception:
        attr_sum = None
    try:
        coach = build_trading_coach_report(days=14, market_gate=gate)
    except Exception:
        coach = None
    return build_strategy_matrix(
        market_gate=gate,
        attribution_summary=attr_sum,
        coach_report=coach,
    )


@router.get("/strategy-matrix")
def get_g3_strategy_matrix() -> dict[str, Any]:
    """Phase 4：三腿互补策略矩阵、动态资金配额、6 大宽基 ETF 趋势与治理门禁。"""
    return _build_live_matrix()
