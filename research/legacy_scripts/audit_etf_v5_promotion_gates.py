"""Audit v5 promotion gates without enabling any order path."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def run(args: argparse.Namespace) -> dict:
    contract = json.loads(Path(args.contract).read_text(encoding="utf-8"))
    liquidity = json.loads(Path(args.liquidity).read_text(encoding="utf-8"))
    stress = json.loads(Path(args.stress).read_text(encoding="utf-8"))
    lot = json.loads(Path(args.lot).read_text(encoding="utf-8"))
    ledger = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
    gates = {
        "research_only_status": contract.get("status") == "research_shadow_only",
        "formal_buy_disabled": contract["guardrails"].get("formal_buy_signal") is False,
        "auto_order_disabled": contract["guardrails"].get("auto_order_allowed") is False,
        "liquidity_all_pass": all(x.get("passes_p10_gate") for x in liquidity.get("rows", [])),
        "stress_300_validation_positive": stress["rows"][0].get("validation_2020_2023", 0) > 0,
        "stress_300_blind_positive": stress["rows"][0].get("blind_2024_2026", 0) > 0,
        "lot_500k_validation_positive": lot["rows"][0].get("validation_2020_2023", 0) > 0,
        "lot_500k_blind_positive": lot["rows"][0].get("blind_2024_2026", 0) > 0,
        "forward_record_exists": bool(ledger),
        "forward_record_has_source_date": bool(ledger) and all(bool(x.get("as_of")) for x in ledger),
    }
    result = {
        "strategy_id": contract.get("strategy_id"),
        "research_only": True,
        "promotion_state": "forward_paper_only" if all(gates.values()) else "blocked_by_gate",
        "gates": gates,
        "required_future_evidence": contract["guardrails"].get("promotion_blockers", []),
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--contract", default="config/strategy_contracts/etf_trend_entryfixed_shadow_v5.json")
    p.add_argument("--liquidity", default=r"F:\AiStock\research\reports\etf_rebalance_policy_v1\liquidity_audit.json")
    p.add_argument("--stress", default=r"F:\AiStock\research\reports\etf_rebalance_policy_v1\lot_v5_raw_300.json")
    p.add_argument("--lot", default=r"F:\AiStock\research\reports\etf_rebalance_policy_v1\lot_v5_raw_300.json")
    p.add_argument("--ledger", default=r"F:\AiStock\research\reports\etf_trend_voltarget_forward_paper_v1\v5_raw_ledger.json")
    p.add_argument("--output", required=True)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))
