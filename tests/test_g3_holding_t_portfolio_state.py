import pytest

from services.g3_holding_t_portfolio_state import confirm_position_action, load_state


def test_manual_position_confirmation_persists_price_and_shares(tmp_path):
    path = tmp_path / "portfolio_state.json"

    result = confirm_position_action(
        {"code": "300037.SZ", "side": "sell", "weight_pct": 25, "price": 77.777, "shares": 300},
        path=path,
    )

    event = result["event"]
    assert event["price"] == 77.777
    assert event["shares"] == 300
    assert event["notional"] == 23333.1
    assert event["before_weight_pct"] == 50.0
    assert event["after_weight_pct"] == 25.0
    assert load_state(path)["events"][-1] == event


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "300037.SZ", "side": "buy", "shares": 100},
        {"code": "300037.SZ", "side": "buy", "price": 10},
        {"code": "300037.SZ", "side": "buy", "price": 0, "shares": 100},
        {"code": "300037.SZ", "side": "buy", "price": 10, "shares": 100.5},
    ],
)
def test_manual_position_confirmation_requires_positive_price_and_whole_shares(tmp_path, payload):
    with pytest.raises(ValueError, match="positive price and whole shares are required"):
        confirm_position_action(payload, path=tmp_path / "portfolio_state.json")
