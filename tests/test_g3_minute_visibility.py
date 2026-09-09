from datetime import datetime, timezone

import pandas as pd

import scripts.gen3_institutional_mainwave_current_v1 as institutional
import scripts.gen3_pretrade_smoke_test_v1 as smoke
import scripts.gen3_state_router_shadow_daily_v1 as router
from scripts.gen3_state_router_shadow_daily_v1 import _select_router_candidates
from scripts.qmt_xtquant_minute_backfill_validate import _as_clickhouse_market_datetime
from utils.g3_mainwave_confirmation import confirm_first_completed_30m_breakout
from utils.g3_minute_visibility import load_visible_minute_bars, valid_bar_times


def _epoch(value: str) -> int:
    return int(pd.Timestamp(value, tz="UTC").timestamp())


def _rows(code: str, business_times: list[str], *, stored_as_utc_like: bool, close: float = 10.0) -> pd.DataFrame:
    records = []
    for text in business_times:
        business = pd.Timestamp(text)
        epoch_text = (
            business.strftime("%Y-%m-%d %H:%M:%S")
            if stored_as_utc_like
            else (business - pd.Timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        )
        raw_text = (
            (business + pd.Timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
            if stored_as_utc_like
            else business.strftime("%Y-%m-%d %H:%M:%S")
        )
        records.append(
            {
                "code": code,
                "datetime": raw_text,
                "datetime_epoch": _epoch(epoch_text),
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 100.0,
                "amount": 1000.0,
            }
        )
    return pd.DataFrame(records)


def test_stage_fallback_recovers_utc_like_rows_as_business_time():
    code = "601038.SH"
    stage = _rows(code, ["2026-07-27 10:00:00", "2026-07-27 10:30:00"], stored_as_utc_like=True)

    def fake_query(sql, params):
        if "qmt_xtquant_minute_stage" in sql:
            return stage
        return pd.DataFrame()

    bars, meta = load_visible_minute_bars(
        code,
        30,
        "2026-07-27",
        "2026-07-27",
        query_fn=fake_query,
    )

    assert valid_bar_times(30) >= {"10:00", "10:30"}
    assert meta["status"] == "recovered_from_stage"
    assert meta["source"] == "stage_fallback"
    assert meta["source_ok"] is True
    assert meta["recovered_rows"] == 2
    assert bars["business_datetime"].dt.strftime("%H:%M").tolist() == ["10:00", "10:30"]


def test_main_rows_keep_business_time_when_stage_is_unavailable():
    code = "002774.SZ"
    main = _rows(code, ["2026-07-27 10:00:00"], stored_as_utc_like=False)

    def fake_query(sql, params):
        if "qmt_xtquant_minute_stage" in sql:
            raise RuntimeError("stage table unavailable")
        return main

    bars, meta = load_visible_minute_bars(
        code,
        30,
        "2026-07-27",
        "2026-07-27",
        query_fn=fake_query,
    )

    assert meta["status"] == "main_final_stage_unavailable"
    assert meta["source_ok"] is True
    assert bars.iloc[0]["business_datetime"] == pd.Timestamp("2026-07-27 10:00:00")


def test_conflicting_main_and_stage_values_block_visibility():
    code = "002832.SZ"
    main = _rows(code, ["2026-07-31 10:00:00"], stored_as_utc_like=False, close=24.83)
    stage = _rows(code, ["2026-07-31 10:00:00"], stored_as_utc_like=True, close=25.10)

    def fake_query(sql, params):
        if "qmt_xtquant_minute_stage" in sql:
            return stage
        return main

    bars, meta = load_visible_minute_bars(
        code,
        30,
        "2026-07-31",
        "2026-07-31",
        query_fn=fake_query,
    )

    assert meta["status"] == "data_conflict"
    assert meta["source_ok"] is False
    assert meta["conflict_rows"] == 1
    assert not bars.empty


def test_qmt_minute_writer_persists_naive_business_wall_clock():
    local = datetime(2026, 7, 27, 10, 0, 0)
    aware_utc = datetime(2026, 7, 27, 10, 0, 0, tzinfo=timezone.utc)

    assert _as_clickhouse_market_datetime(local) == local
    assert _as_clickhouse_market_datetime(local).tzinfo is None
    assert _as_clickhouse_market_datetime(aware_utc) == datetime(2026, 7, 27, 18, 0, 0)


def test_institutional_feature_builder_accepts_verified_stage_fallback(monkeypatch):
    history = []
    for offset in range(20, 0, -1):
        day = pd.Timestamp("2026-07-31") - pd.Timedelta(days=offset)
        history.append(
            {
                "code": "601038.SH",
                "business_datetime": day + pd.Timedelta(hours=10),
                "open": 9.8,
                "high": 10.0,
                "low": 9.5,
                "close": 9.9,
                "volume": 100,
                "amount": 100.0,
            }
        )
    history.append(
        {
            "code": "601038.SH",
            "business_datetime": pd.Timestamp("2026-07-31 10:00:00"),
            "open": 10.5,
            "high": 11.2,
            "low": 10.4,
            "close": 11.0,
            "volume": 200,
            "amount": 150.0,
        }
    )
    bars = pd.DataFrame(history)
    meta = {
        "status": "recovered_from_stage",
        "source": "stage_fallback",
        "source_ok": True,
        "main_rows": 0,
        "stage_rows": len(bars),
        "recovered_rows": len(bars),
        "conflict_rows": 0,
        "day_counts": {"2026-07-31": 1},
    }
    monkeypatch.setattr(institutional, "load_visible_minute_bars", lambda *args, **kwargs: (bars.copy(), meta))
    candidates = pd.DataFrame(
        [
            {
                "code_raw": "601038.SH",
                "close": 11.0,
            }
        ]
    )

    out, _ = institutional._compute_30m_features(candidates, "2026-07-31", period=30)

    assert bool(out.iloc[0]["m30_ok"]) is True
    assert bool(out.iloc[0]["m30_breakout_confirmed"]) is True
    assert out.iloc[0]["m30_source"] == "stage_fallback"
    assert out.iloc[0]["m30_visibility_status"] == "recovered_from_stage"


def test_first_completed_30m_bar_cannot_be_replaced_by_later_breakout():
    rows = []
    for offset in range(20, 0, -1):
        day = pd.Timestamp("2026-07-31") - pd.Timedelta(days=offset)
        rows.append(
            {
                "business_datetime": day + pd.Timedelta(hours=10),
                "open": 9.8,
                "high": 10.0,
                "low": 9.5,
                "close": 9.9,
                "amount": 100.0,
            }
        )
    rows.extend(
        [
            {
                "business_datetime": pd.Timestamp("2026-07-31 10:00:00"),
                "open": 9.9,
                "high": 10.1,
                "low": 9.7,
                "close": 9.8,
                "amount": 300.0,
            },
            {
                "business_datetime": pd.Timestamp("2026-07-31 10:30:00"),
                "open": 10.2,
                "high": 11.3,
                "low": 10.1,
                "close": 11.0,
                "amount": 400.0,
            },
        ]
    )

    result = confirm_first_completed_30m_breakout(pd.DataFrame(rows), "2026-07-31")

    assert result["confirmed"] is False
    assert result["status"] == "first_completed_30m_bar_not_confirmed"
    assert result["confirm_datetime"] == "2026-07-31 10:00:00"


def test_router_source_builder_failure_is_a_data_wall_even_when_rows_empty():
    walls = router._summarize_source_data_walls(
        [
            {"source": "panic_current_builder", "status": "retired_from_runtime", "rows": 0},
            {"source": "institutional_mainwave_current_builder_v1", "status": "failed", "rows": 0, "error": "boom"},
        ],
        "2026-07-31",
    )

    assert walls["count"] == 1
    assert walls["builder_failure"] is True
    assert walls["reasons"] == ["failed"]


def test_smoke_reads_summary_source_failure_without_all_source_rows():
    failures = smoke._summary_source_data_walls(
        {
            "sources": [
                {"source": "institutional_mainwave_current_builder_v1", "status": "failed", "rows": 0},
            ]
        },
        live_confirmation_window=False,
    )

    assert failures == [
        {
            "source": "institutional_mainwave_current_builder_v1",
            "status": "failed",
            "reason": "failed",
            "rows": 0,
            "candidate_snapshot_required": False,
            "candidate_snapshot_loaded": False,
            "candidate_snapshot_status": "",
        }
    ]


def test_recent_week_router_keeps_two_confirmed_slots_and_drops_third():
    rows_0727 = pd.DataFrame(
        [
            {"code": "601038.SH", "route": "institutional_mainwave", "router_eligible": True, "score": 96.0, "entry_date": "2026-07-27", "sector_name": "A"},
            {"code": "002774.SZ", "route": "institutional_mainwave", "router_eligible": True, "score": 94.0, "entry_date": "2026-07-27", "sector_name": "A"},
        ]
    )
    rows_0731 = pd.DataFrame(
        [
            {"code": "002832.SZ", "route": "institutional_mainwave", "router_eligible": True, "score": 97.0, "entry_date": "2026-07-31", "sector_name": "B"},
            {"code": "000948.SZ", "route": "institutional_mainwave", "router_eligible": True, "score": 96.0, "entry_date": "2026-07-31", "sector_name": "B"},
            {"code": "300996.SZ", "route": "institutional_mainwave", "router_eligible": True, "score": 95.0, "entry_date": "2026-07-31", "sector_name": "B"},
        ]
    )

    selected_0727, meta_0727 = _select_router_candidates(rows_0727, pd.DataFrame())
    selected_0731, meta_0731 = _select_router_candidates(rows_0731, pd.DataFrame())

    assert selected_0727["code"].tolist() == ["601038.SH", "002774.SZ"]
    assert selected_0731["code"].tolist() == ["002832.SZ", "000948.SZ"]
    assert "300996.SZ" not in selected_0731["code"].tolist()
    assert meta_0727["daily_open_limit"] == meta_0731["daily_open_limit"] == 2
