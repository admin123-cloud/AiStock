import importlib.util
import dataclasses
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from services.postclose_contracts import Purpose


# Loading this single module directly avoids utils/__init__.py, which imports
# application configuration and database modules before the tested module.
_spec = importlib.util.spec_from_file_location("s1_market_phase_policy", Path(__file__).resolve().parents[1] / "utils" / "calendar" / "market_phase_policy.py")
p = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = p
_spec.loader.exec_module(p)

D = date(2026, 11, 18)
BJ = timezone(timedelta(hours=8))


def at(hour, minute, second=0):
    return datetime(2026, 11, 18, hour, minute, second, tzinfo=BJ)


def evidence(state="trading", **changes):
    values = dict(state=state, target_date=D, markets=("SH", "SZ", "BJ"), version="v1",
                  coverage_complete=True, checked_at=at(8, 0), expires_at=at(23, 0), generation=7)
    values.update(changes)
    return p.CalendarEvidence(**values)


def budget(now, *, seconds=60, margin=10, drain_at=None, previous="auto", elapsed="auto"):
    if previous == "auto":
        previous = now - timedelta(seconds=1)
    if elapsed == "auto":
        elapsed = timedelta(seconds=1)
    return p.PhaseBudget(measured_worst_total=timedelta(seconds=seconds),
        margin=timedelta(seconds=margin), drain_at=drain_at,
        previous_wall=previous, monotonic_elapsed=elapsed)


class PhaseTest(unittest.TestCase):
    def decision(self, now, ev=None, purpose=Purpose.INTRADAY_PROVISIONAL, b="default", generation=7, closeout=None):
        extra = {} if closeout is None else {"closeout": closeout}
        return p.evaluate_phase(now, evidence() if ev is None else ev, purpose, D,
                                budget(now) if b == "default" else b, generation, **extra)

    def closeout(self, **changes):
        values = dict(mode="in_flight", attempt_id="attempt-1", admitted_attempt_id="attempt-1",
                      generation=7, operation="write", admitted_operation="write", bar_at=at(15, 0),
                      admitted_at=at(14, 59), permit_ref="permit:closeout",
                      target="stock.kline_minute_5", admitted_target="stock.kline_minute_5",
                      permit_expires_at=at(15, 5), remaining_worst=timedelta(milliseconds=100))
        values.update(changes)
        return p.CloseoutContext(**values)

    def test_utc_conversion_and_naive_rejection(self):
        decision = self.decision(at(10, 0).astimezone(timezone.utc))
        self.assertTrue(decision.admit_new)
        self.assertEqual(decision.now_bj.hour, 10)
        self.assertFalse(self.decision(at(10, 0).replace(tzinfo=None)).admit_new)

    def test_unknown_not_closed_and_bad_evidence_blocked(self):
        unknown = self.decision(at(10, 0), evidence("unknown"))
        closed = self.decision(at(10, 0), evidence("closed"))
        self.assertEqual(unknown.reason, "calendar_unknown")
        self.assertEqual(closed.reason, "non_trading_day")
        self.assertFalse(unknown.admit_new)
        self.assertFalse(closed.admit_new)
        for ev in (evidence(target_date=date(2026, 11, 17)), evidence(markets=("SH",)),
                   evidence(version=""), evidence(coverage_complete=False),
                   evidence(expires_at=at(10, 0)), evidence(generation=6),
                   evidence(state="invalid"), evidence(checked_at=at(10, 1))):
            self.assertFalse(self.decision(at(10, 0), ev).admit_new)

    def test_lunch_and_last_bar(self):
        self.assertFalse(self.decision(at(12, 0)).admit_new)
        self.assertEqual(self.decision(at(12, 0)).reason, "lunch")
        self.assertEqual(self.decision(at(15, 0), b=budget(at(15, 0), seconds=10, margin=1), closeout=self.closeout()).last_bar,
                         at(15, 0))
        self.assertNotEqual(self.decision(at(15, 0), b=budget(at(15, 0), seconds=10, margin=1), closeout=self.closeout()).last_bar,
                            at(15, 5))

    def test_cutoff_strict_and_budget_boundary(self):
        tiny = budget(at(15, 4, 59), seconds=0.25, margin=0.25)
        self.assertFalse(self.decision(at(15, 4, 59), b=tiny).admit_new)
        self.assertFalse(self.decision(at(15, 4, 59), b=tiny).may_write)
        self.assertFalse(self.decision(at(15, 4, 59), b=tiny, closeout=self.closeout()).may_write)
        for instant in (at(15, 5), at(15, 5, 1)):
            decision = self.decision(instant, b=tiny)
            self.assertFalse(decision.admit_new)
            self.assertFalse(decision.may_write)
            self.assertFalse(decision.may_rollback_mutation)
        dec = self.decision(at(15, 3, 49), b=budget(at(15, 3, 49)), closeout=self.closeout(mode="new_closeout", admitted_at=at(15, 3, 49)))
        self.assertEqual(dec.last_start, at(15, 3, 49))
        self.assertTrue(dec.admit_new)
        self.assertFalse(self.decision(at(15, 3, 50), closeout=self.closeout(mode="new_closeout", admitted_at=at(15, 3, 50))).admit_new)
        self.assertFalse(self.decision(at(15, 0), b=None).admit_new)
        self.assertFalse(self.decision(at(15, 4, 59), b=budget(at(15, 4, 59), seconds=0, margin=0)).admit_new)

    def test_drain_disallows_new_even_if_time_budget_remains(self):
        b = budget(at(14, 0), drain_at=at(14, 0))
        dec = self.decision(at(14, 0), b=b)
        self.assertFalse(dec.admit_new)
        self.assertFalse(dec.may_write)
        self.assertEqual(dec.reason, "draining")

    def test_purpose_string_and_other_purposes_cannot_self_authorize(self):
        for purpose in ("intraday_provisional", "afterclose_cleanup", "bad", Purpose.AFTERCLOSE_CLEANUP,
                        Purpose.AFTERCLOSE_AUTHORITATIVE, Purpose.RECOVERY, Purpose.OPERATIONAL_EVIDENCE):
            dec = self.decision(at(10, 0), purpose=purpose)
            self.assertFalse(dec.admit_new)
            self.assertFalse(dec.may_write)

    def test_clock_regression_jump_and_date_mismatch(self):
        now = at(10, 0)
        self.assertFalse(self.decision(now, b=budget(now, previous=at(10, 1), elapsed=timedelta(seconds=1))).admit_new)
        self.assertFalse(self.decision(now, b=budget(now, previous=at(9, 0), elapsed=timedelta(seconds=1))).admit_new)
        self.assertTrue(self.decision(now, b=budget(now, previous=at(9, 59, 59), elapsed=timedelta(seconds=1))).admit_new)
        self.assertFalse(self.decision(now + timedelta(days=1)).admit_new)
        self.assertFalse(self.decision(now, b=budget(now, previous=at(9, 59), elapsed=None)).admit_new)
        self.assertFalse(self.decision(now, b=budget(now, previous=None, elapsed=None)).admit_new)

    def test_closeout_context_and_drain_are_separate(self):
        now = at(15, 4, 58)
        b = budget(now, seconds=0.25, margin=0.25, drain_at=at(15, 1))
        self.assertFalse(self.decision(now, b=b).admit_new)
        c = self.closeout()
        dec = self.decision(now, b=b, closeout=c)
        self.assertFalse(dec.admit_new)
        self.assertTrue(dec.may_write)
        self.assertFalse(dec.may_rollback_mutation)
        rollback = self.decision(now, b=b, closeout=dataclasses.replace(c, operation="rollback", admitted_operation="rollback"))
        self.assertTrue(rollback.may_rollback_mutation)
        self.assertFalse(rollback.may_write)
        for changes in (dict(attempt_id="other"), dict(generation=8), dict(permit_ref=""),
                        dict(operation="arbitrary"), dict(bar_at=at(14, 55)),
                        dict(admitted_at=at(15, 2)), dict(permit_expires_at=now),
                        dict(remaining_worst=timedelta(seconds=2)),
                        dict(mode="new_closeout", admitted_at=now)):
            with self.subTest(changes=changes):
                bad = self.decision(now, b=b, closeout=dataclasses.replace(c, **changes))
                self.assertFalse(bad.admit_new or bad.may_write or bad.may_rollback_mutation)
        for instant in (at(15, 5), at(15, 5, 1)):
            d = self.decision(instant, b=budget(instant, seconds=.25, margin=.25, drain_at=at(15, 1)), closeout=c)
            self.assertFalse(d.admit_new or d.may_write or d.may_rollback_mutation)

    def test_clock_tolerance_boundaries_and_conservative_cutoff(self):
        now = at(10, 0)
        self.assertTrue(self.decision(now, b=budget(now, previous=now-timedelta(seconds=2), elapsed=timedelta(seconds=1))).admit_new)
        self.assertFalse(self.decision(now, b=budget(now, previous=now-timedelta(seconds=2, microseconds=1), elapsed=timedelta(seconds=1))).admit_new)
        self.assertFalse(self.decision(now, b=budget(now, previous=now, elapsed=timedelta(seconds=1))).admit_new)
        self.assertFalse(self.decision(now, b=budget(now, previous=now-timedelta(seconds=1), elapsed=timedelta(seconds=-1))).admit_new)
        near = at(15, 4, 59)
        c = self.closeout(remaining_worst=timedelta(milliseconds=100))
        self.assertFalse(self.decision(near, b=budget(near, seconds=.1, margin=0), closeout=c).may_write)
        self.assertFalse(self.decision(at(0, 0), b=budget(at(0, 0), previous=at(0, 0)-timedelta(seconds=1))).may_write)

    def test_legacy_ordinary_request_after_close_has_no_admission(self):
        now = at(15, 4, 59)
        decision = p.evaluate_phase(now, evidence(), Purpose.INTRADAY_PROVISIONAL,
                                    D, budget(now, seconds=.25, margin=.25), 7)
        self.assertFalse(decision.admit_new or decision.may_write or decision.may_rollback_mutation)

    def test_closeout_target_and_short_permit_fail_closed(self):
        now = at(15, 3)
        c = self.closeout(target="stock.kline_minute_5", admitted_target="stock.kline_minute_5")
        self.assertTrue(self.decision(now, closeout=c).may_write)
        for changes in (dict(target="stock.kline_daily"), dict(admitted_target="stock.kline_daily"),
                        dict(target="stock.kline_minute_15", admitted_target="stock.kline_minute_15")):
            d = self.decision(now, closeout=dataclasses.replace(c, **changes))
            self.assertFalse(d.admit_new or d.may_write or d.may_rollback_mutation)
        short = dataclasses.replace(c, mode="new_closeout", admitted_at=now,
                                    permit_expires_at=now+timedelta(seconds=10))
        self.assertFalse(self.decision(now, closeout=short).admit_new)

    def test_exact_lunch_boundaries_and_negative_budgets(self):
        for instant, reason, admitted in ((at(11, 29, 59), "intraday", True),
                                          (at(11, 30), "lunch", False),
                                          (at(12, 59, 59), "lunch", False),
                                          (at(13, 0), "intraday", True)):
            with self.subTest(instant=instant):
                decision = self.decision(instant)
                self.assertEqual((decision.reason, decision.admit_new), (reason, admitted))
                self.assertFalse(decision.may_write or decision.may_rollback_mutation)
        now = at(10, 0)
        for seconds, margin in ((-1, 0), (1, -1)):
            decision = self.decision(now, b=budget(now, seconds=seconds, margin=margin))
            self.assertEqual(decision.reason, "budget_or_clock_invalid")
            self.assertFalse(decision.admit_new or decision.may_write or decision.may_rollback_mutation)

    def test_zero_margin_last_start_microsecond_boundary_and_rollback_cutoff(self):
        last = at(15, 3, 59)
        for instant, allowed in ((last - timedelta(microseconds=1), True),
                                 (last, False), (last + timedelta(microseconds=1), False)):
            closeout = self.closeout(mode="new_closeout", admitted_at=instant)
            decision = self.decision(instant, b=budget(instant, seconds=60, margin=0), closeout=closeout)
            self.assertEqual(decision.last_start, last)
            self.assertEqual(decision.admit_new, allowed)
            self.assertFalse(decision.may_write or decision.may_rollback_mutation)
        rollback = self.closeout(operation="rollback", admitted_operation="rollback")
        now = at(15, 4, 58)
        self.assertTrue(self.decision(now, b=budget(now, seconds=.25, margin=.25), closeout=rollback).may_rollback_mutation)
        for instant in (at(15, 5), at(15, 5, 1)):
            decision = self.decision(instant, b=budget(instant, seconds=.25, margin=.25), closeout=rollback)
            self.assertFalse(decision.admit_new or decision.may_write or decision.may_rollback_mutation)

    def test_intraday_admission_is_not_write_and_drain_can_finish_existing(self):
        now = at(14, 0)
        new = self.decision(now)
        self.assertTrue(new.admit_new)
        self.assertFalse(new.may_write or new.may_rollback_mutation)
        b = budget(now, drain_at=now)
        old = self.closeout(bar_at=at(13, 55), operation="write", admitted_operation="write",
                            admitted_at=at(13, 59))
        decision = self.decision(now, b=b, closeout=old)
        self.assertFalse(decision.admit_new)
        self.assertTrue(decision.may_write)
        self.assertFalse(decision.may_rollback_mutation)
        wrong_op = dataclasses.replace(old, operation="rollback")
        bad = self.decision(now, b=b, closeout=wrong_op)
        self.assertFalse(bad.admit_new or bad.may_write or bad.may_rollback_mutation)


if __name__ == "__main__":
    unittest.main()
