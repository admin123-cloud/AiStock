from datetime import date

from scripts.qmtmini_daily_backfill_validate import abnormal_daily_data_policy, expected_daily_coverage_keys, known_market_status_keys, remediation_plan


def test_expected_daily_coverage_excludes_pre_listing_and_post_delisting_dates():
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    expected, counts = expected_daily_coverage_keys(
        ["000001.SZ", "000002.SZ"],
        dates,
        {
            "000001.SZ": {"list_date": date(2026, 1, 6), "delist_date": None},
            "000002.SZ": {"list_date": date(2026, 1, 5), "delist_date": date(2026, 1, 6)},
        },
    )

    assert expected == {
        ("000001.SZ", date(2026, 1, 6)),
        ("000001.SZ", date(2026, 1, 7)),
        ("000002.SZ", date(2026, 1, 5)),
        ("000002.SZ", date(2026, 1, 6)),
    }
    assert counts == {
        "excluded_pre_listing_code_dates": 1,
        "excluded_post_delisting_code_dates": 1,
        "metadata_review_codes": 0,
    }


def test_expected_daily_coverage_retains_unknown_listing_date_for_review():
    expected, counts = expected_daily_coverage_keys(
        ["000003.SZ"],
        [date(2026, 1, 5)],
        {"000003.SZ": {"list_date": None, "delist_date": None}},
    )

    assert expected == {("000003.SZ", date(2026, 1, 5))}
    assert counts["metadata_review_codes"] == 1


def test_known_market_status_excludes_only_matching_announcement_interval():
    expected = {
        ("000001.SZ", date(2026, 1, 5)),
        ("000001.SZ", date(2026, 1, 6)),
        ("000002.SZ", date(2026, 1, 5)),
    }

    excluded = known_market_status_keys(
        expected,
        [("000001.SZ", date(2026, 1, 6), date(2026, 1, 7), "suspended", "https://example.test", "公告停牌")],
    )

    assert excluded == {("000001.SZ", date(2026, 1, 6))}


def test_remediation_plan_routes_only_unverified_qmt_empty_days_to_retry_and_tdx():
    expected = {
        ("000001.SZ", date(2026, 1, 5)),
        ("000001.SZ", date(2026, 1, 6)),
        ("000002.SZ", date(2026, 1, 5)),
    }
    plan = remediation_plan(
        expected,
        {("000001.SZ", date(2026, 1, 5))},
        {("000001.SZ", date(2026, 1, 6))},
        {("000002.SZ", date(2026, 1, 5))},
        candidate_limit=0,
    )

    assert plan["repair_backlog_code_dates"] == 1
    assert plan["qmt_retry_code_dates"] == 1
    assert plan["tdx_fallback_code_dates"] == 1
    assert plan["missing_by_code"] == []
    assert plan["qmt_retry_by_code"] == [
        {"code": "000001.SZ", "missing_days": 1, "first_missing": "2026-01-06", "last_missing": "2026-01-06"}
    ]


def test_abnormal_data_policy_never_auto_exempts_or_writes_a_provider_conflict():
    policy = abnormal_daily_data_policy()

    assert policy["recovery_windows"] == ["after_close", "overnight"]
    assert "never auto-written or auto-exempted" in policy["conflict_rule"]
