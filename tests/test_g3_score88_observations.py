from api.gen3_state_alpha import (
    STRATEGY_ID,
    _accepted_observation_dates,
    _formal_score88_observation_rows,
    _formal_score88_paper_rows,
    _formal_observation_date,
)


def test_only_formal_score88_snapshots_advance_observation_window():
    rows = [
        {
            "observation_date": "2026-07-24",
            "accepted": True,
            "strategy_id": "g3_mainwave_continuation_breakout_cash_v1",
            "contract": "g3_mainwave_continuation_breakout_cash_v1",
        },
        {
            "observation_date": "2026-07-25",
            "accepted": True,
            "strategy_id": STRATEGY_ID,
            "contract": STRATEGY_ID,
        },
        {
            "observation_date": "2026-07-28",
            "accepted": False,
            "strategy_id": STRATEGY_ID,
            "contract": STRATEGY_ID,
        },
    ]

    assert _accepted_observation_dates(rows) == ["2026-07-25"]
    assert _formal_score88_observation_rows(rows) == [rows[1], rows[2]]


def test_only_formal_score88_paper_rows_are_usable_for_reproduction():
    rows = [
        {"strategy_id": "g3_mainwave_continuation_breakout_cash_v1", "contract": "g3_mainwave_continuation_breakout_cash_v1"},
        {"strategy_id": STRATEGY_ID, "contract": STRATEGY_ID},
        {"strategy_id": STRATEGY_ID, "contract": "legacy_contract"},
    ]

    assert _formal_score88_paper_rows(rows) == [rows[1]]


def test_explicit_backfill_date_is_used_for_formal_observation():
    assert _formal_observation_date("2026-07-27 15:40:00") == "2026-07-27"
