"""RED-stage policy tests for the daily kline pipeline schedule contract.

Task brief:
F:/Aistock/data/data/runtime/task_briefs/20261105_daily_pipeline_schedule_policy_test.md

These tests pin the Owner-approved scheduling policy BEFORE the
implementation exists (TDD RED stage):

* ``SCHEDULER_CONFIG["jobs"]["daily_kline_pipeline"]`` is enabled at
  ``16:15`` with timezone ``Asia/Shanghai``;
* ``scheduler.config.daily_kline_pipeline_due(now, last_successful_run_date)``
  is callable with an aware ``datetime`` and a ``date | None``;
* 16:14 Beijing time is not due, 16:15 Beijing time is due, and the same
  instant expressed as an aware UTC datetime is also due;
* once the pipeline has succeeded on the current Asia/Shanghai calendar
  date it is not due again that day;
* naive datetimes are rejected with ``ValueError``.

Missing implementation/config items must fail as plain assertion failures
with fixed contract labels -- never as import/collection errors. These
tests use only synthetic in-memory values and touch no data files, real
runtime state, credentials, logs, databases, or network.
"""

import datetime as dt

import pytest

import scheduler.config as scheduler_config

# Asia/Shanghai is a fixed +08:00 offset (no DST); a static timezone keeps
# the synthetic inputs independent of tzdata availability.
BEIJING = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")


def _require_due_callable():
    fn = getattr(scheduler_config, "daily_kline_pipeline_due", None)
    assert callable(fn), "contract: daily_kline_pipeline_due callable missing"
    return fn


def test_scheduler_config_declares_daily_kline_pipeline_job():
    config = getattr(scheduler_config, "SCHEDULER_CONFIG", None)
    assert isinstance(config, dict), "contract: SCHEDULER_CONFIG dict missing"
    jobs = config.get("jobs")
    assert isinstance(jobs, dict), "contract: SCHEDULER_CONFIG['jobs'] dict missing"
    job = jobs.get("daily_kline_pipeline")
    assert isinstance(job, dict), (
        "contract: SCHEDULER_CONFIG['jobs']['daily_kline_pipeline'] missing"
    )
    assert job.get("enabled") is True, "contract: daily_kline_pipeline.enabled must be True"
    assert job.get("time") == "16:15", "contract: daily_kline_pipeline.time must be '16:15'"
    assert job.get("timezone") == "Asia/Shanghai", (
        "contract: daily_kline_pipeline.timezone must be 'Asia/Shanghai'"
    )


def test_daily_kline_pipeline_due_is_callable():
    _require_due_callable()


@pytest.mark.parametrize(
    ("now", "last_successful_run_date", "expected_due"),
    [
        # 16:14 Beijing time is not yet due.
        pytest.param(
            dt.datetime(2026, 11, 5, 16, 14, tzinfo=BEIJING),
            None,
            False,
            id="beijing_1614_not_due",
        ),
        # 16:15 Beijing time (inclusive) is due.
        pytest.param(
            dt.datetime(2026, 11, 5, 16, 15, tzinfo=BEIJING),
            None,
            True,
            id="beijing_1615_due",
        ),
        # The same instant expressed as an aware UTC datetime must also be due.
        pytest.param(
            dt.datetime(2026, 11, 5, 8, 15, tzinfo=dt.timezone.utc),
            None,
            True,
            id="same_instant_utc_due",
        ),
        # A success already recorded on the current Beijing date suppresses
        # the trigger for the rest of that day.
        pytest.param(
            dt.datetime(2026, 11, 5, 17, 0, tzinfo=BEIJING),
            dt.date(2026, 11, 5),
            False,
            id="success_same_beijing_day_not_due",
        ),
        # A success on an earlier natural day does not suppress today's
        # trigger.
        pytest.param(
            dt.datetime(2026, 11, 5, 17, 0, tzinfo=BEIJING),
            dt.date(2026, 11, 4),
            True,
            id="success_previous_day_due",
        ),
        # Every natural day applies: Saturday after 16:15 Beijing is still
        # due (no weekend/holiday exclusion in this contract).
        pytest.param(
            dt.datetime(2026, 11, 7, 16, 15, tzinfo=BEIJING),
            None,
            True,
            id="saturday_1615_due",
        ),
    ],
)
def test_daily_kline_pipeline_due_policy(now, last_successful_run_date, expected_due):
    due = _require_due_callable()
    assert due(now, last_successful_run_date) is expected_due, (
        "contract: daily_kline_pipeline_due policy"
    )


def test_daily_kline_pipeline_due_rejects_naive_datetime():
    due = _require_due_callable()
    with pytest.raises(ValueError):
        due(dt.datetime(2026, 11, 5, 16, 15), None)
