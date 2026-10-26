from scripts.dry_run_index_reconciliation_permit import classify_residual


def _row(**changes):
    row = {
        "formal_values": {"open": 1.003, "high": 1.003, "low": 0.997, "close": 1.003, "volume": 100.0, "amount": 1_000_000.0},
        "candidate_values": {"open": 1.00, "high": 1.00, "low": 1.00, "close": 1.00, "volume": 100.0, "amount": 1_000_010.0},
        "residual_substantive_fields": ["open", "high", "low", "close", "amount"],
    }
    row.update(changes)
    return row


def test_residual_classifies_price_display_and_near_equal_amount():
    result = classify_residual(_row())
    assert result["price_class"] == "price_display_rounding"
    assert result["amount_class"] == "amount_near_equal"
    assert result["value_gate"] == "blocked_residual_fields"


def test_residual_free_row_is_volume_only_eligible():
    result = classify_residual(
        {
            "formal_values": {"volume": 100.0},
            "candidate_values": {"volume": 100.0},
            "residual_substantive_fields": [],
        }
    )
    assert result["residual_class"] == "none"
    assert result["value_gate"] == "eligible_volume_only"


def test_material_price_is_separate_class():
    result = classify_residual(
        {
            "formal_values": {"open": 1.0},
            "candidate_values": {"open": 1.02},
            "residual_substantive_fields": ["open"],
        }
    )
    assert result["price_class"] == "price_material"
    assert result["residual_class"] == "price_material"
