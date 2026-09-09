import json

from services.g3_holding_t_daily_review import record_manual_execution, run_daily_review


def test_daily_review_pairs_manual_sell_and_buy_and_preserves_tick_context(tmp_path):
    ledger = tmp_path / "plans.jsonl"
    snapshots = tmp_path / "tick_snapshots"
    executions = tmp_path / "manual_executions.jsonl"
    reviews = tmp_path / "daily_reviews"
    snapshots.mkdir()
    ledger.write_text(json.dumps({"checked_at": "2026-07-24 10:00:00", "plans": [{"code": "300054.SZ"}]}) + "\n", encoding="utf-8")
    snapshots.joinpath("2026-07-24.jsonl").write_text(json.dumps({
        "code": "300054.SZ", "tick": {"last_price": 10.0, "tick_fresh": True, "bid_price": [9.99]},
        "minute": {"minute_fresh": True},
        "decision": {"checked_at": "2026-07-24 10:00:00", "action": "watch_sell_t_signal", "market_price": 10.0},
    }) + "\n", encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    record_manual_execution({"code": "300054.SZ", "side": "sell", "price": 10, "shares": 100, "filled_at": "2026-07-24 10:01:00"}, executions_path=executions, confirmed_inventory_path=inventory)
    record_manual_execution({"code": "300054.SZ", "side": "buy", "price": 9.8, "shares": 100, "filled_at": "2026-07-24 14:30:00"}, executions_path=executions, confirmed_inventory_path=inventory)
    review = run_daily_review("2026-07-24", review_dir=reviews, ledger_path=ledger, snapshot_dir=snapshots, executions_path=executions)
    stock = review["stocks"][0]
    assert stock["data_quality"]["five_level_book_count"] == 1
    assert stock["realized_t_cycles"][0]["t_leg_excess_pct"] > 0
    assert reviews.joinpath("2026-07-24.json").exists()
