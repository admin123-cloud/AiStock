# -*- coding: utf-8 -*-
"""AI 策略挖掘工坊 —— 标准策略合约导出器（兼容 config/strategy_contracts/）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from services.mining.feature_store import MINING_FEATURE_META
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))


def _contracts_dir(base_dir: Optional[Path] = None) -> Path:
    d = base_dir or runtime_path("mining_contracts")
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_candidate_contract(
    evaluated_item: Mapping[str, Any],
    *,
    run_id: str,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """将挖掘评估项导出为兼容 config/strategy_contracts/ 的影子观察合约 JSON。

    安全铁律：
    - status 固定为 candidate_shadow（绝不直接激活实盘）；
    - guardrails.auto_order_allowed / formal_buy_signal 永久为 False。
    """
    rule = evaluated_item.get("rule") or {}
    segs = evaluated_item.get("segments") or {}
    gate = evaluated_item.get("quality_gates") or {}
    exit_pol = rule.get("exit_policy") or {}

    rule_id = str(rule.get("rule_id") or "mined_rule")
    strategy_id = f"mined_{rule_id}_shadow_v1"

    readable_conditions = [
        f"{MINING_FEATURE_META.get(c.get('feature', ''), {}).get('label', c.get('feature'))} {c.get('op')} {c.get('value')}"
        for c in (rule.get("entry_conditions") or [])
    ]

    contract = {
        "schema_version": 1,
        "strategy_id": strategy_id,
        "trade_strategy": rule_id,
        "display_name": f"[挖掘候选] {rule.get('name') or rule_id}",
        "status": "candidate_shadow",
        "origin": {
            "source": rule.get("source") or "mining_studio",
            "run_id": run_id,
            "template_id": rule.get("template_id"),
            "exported_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
            "all_gates_passed": bool(gate.get("all_passed")),
            "mining_score": gate.get("mining_score"),
        },
        "thesis": rule.get("thesis") or "",
        "entry": {
            "target_regimes": list(rule.get("target_regimes") or []),
            "dsl_conditions": list(rule.get("entry_conditions") or []),
            "readable_conditions": readable_conditions,
            "rank_by": rule.get("rank_by"),
            "rank_ascending": bool(rule.get("rank_ascending", False)),
            "confirmation": "T+1_open_non_limit_up",
        },
        "portfolio": {
            "slots": int(rule.get("top_k_per_day") or 3),
            "daily_open_limit": int(rule.get("top_k_per_day") or 3),
            "cash_is_valid_decision": True,
        },
        "exit": {
            "hold_days": int(exit_pol.get("hold_days") or 5),
            "hard_stop_loss_pct": abs(float(exit_pol.get("stop_loss_pct") or -0.07)),
            "take_profit_pct": abs(float(exit_pol.get("take_profit_pct") or 0.15)),
        },
        "validation_evidence": {
            "is": segs.get("is"),
            "val": segs.get("val"),
            "oos": segs.get("oos"),
            "quality_gates": gate,
        },
        "guardrails": {
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "shadow_trading_enabled": True,
        },
    }

    out_path = _contracts_dir(base_dir) / f"{strategy_id}.json"
    out_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"strategy_id": strategy_id, "path": str(out_path), "contract": contract}
