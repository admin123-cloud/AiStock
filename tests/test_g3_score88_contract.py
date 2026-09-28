import json
from pathlib import Path


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "config" / "strategy_contracts" / "g3_institutional_mainwave_score88_v1.json"


def test_score88_formal_shadow_contract_is_explicit_and_order_locked():
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["strategy_id"] == "g3_institutional_mainwave_score88_v1"
    assert contract["display_name"] == "G3\u673a\u6784\u4e3b\u5347 Score88"
    assert contract["status"] == "formal_shadow_only"
    assert contract["entry"] == {
        "score_field": "wave_style_score",
        "minimum_score": 88,
        "index_mom60_max": 0.05,
        "minimum_same_day_industry_mainwave_count": 2,
        "confirmation": "first_completed_30m_volume_breakout_prior20_high",
    }
    assert contract["portfolio"] == {
        "slots": 2,
        "slot_pct": 0.5,
        "daily_open_limit": 2,
        "cash_is_valid_decision": True,
    }
    assert contract["exit"]["hard_stop_loss_pct"] == 0.10
    assert contract["exit"]["take_profit_pct"] == 0.10
    assert contract["exit"]["take_profit_sell_ratio"] == 0.50
    assert contract["guardrails"] == {
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "shadow_trading_enabled": True,
    }


def test_disabled_order_state_is_migrated_to_score88_and_rejects_score120():
    from execution.gen3_auto_order_executor import ALLOWED_STRATEGIES, STRATEGY_ID, load_state_from

    state = load_state_from({"strategy_id": "g3_mainwave_continuation_breakout_cash_v1", "enabled": False})
    assert state["strategy_id"] == STRATEGY_ID
    assert ALLOWED_STRATEGIES == {"institutional_mainwave_score88"}


def test_all_formal_consumers_share_the_same_contract_identity_and_fingerprint():
    from api.gen3_state_alpha import FORMAL_G3_CONTRACT as api_contract
    from execution.gen3_auto_order_executor import FORMAL_G3_CONTRACT as executor_contract
    from scripts.gen3_state_router_shadow_daily_v1 import FORMAL_G3_CONTRACT as router_contract
    from utils.strategy_contracts import formal_g3_score88_contract_metadata

    metadata = formal_g3_score88_contract_metadata()
    assert api_contract is router_contract is executor_contract
    assert metadata["strategy_id"] == api_contract["strategy_id"]
    assert len(metadata["sha256"]) == 64
