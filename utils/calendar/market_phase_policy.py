"""Pure phase classification; no client, permit issuer, lock or clock source.

The caller must provide trusted fresh calendar and paired clock observations.
An affirmative result is necessary but never sufficient for a production write.
Production import requires S2 to resolve utils/__init__ side effects and ensure
one shared module/class identity; this offline test loader is not that solution.
S2 must authenticate calendar, admission, permit, lease and paired samples and
call this policy again immediately before each write or rollback mutation.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.postclose_contracts import Purpose

# Temporary fixed upper bound, pending measurement and PM/Owner approval.
# It is not caller-configurable and is charged to each deadline budget.
CLOCK_SKEW_BOUND = timedelta(seconds=1)


@dataclass(frozen=True)
class CalendarEvidence:
    state: str
    target_date: date
    markets: tuple
    version: str
    coverage_complete: bool
    checked_at: datetime
    expires_at: datetime
    generation: int


@dataclass(frozen=True)
class PhaseBudget:
    measured_worst_total: timedelta
    margin: timedelta
    drain_at: datetime = None
    previous_wall: datetime = None
    monotonic_elapsed: timedelta = None


@dataclass(frozen=True)
class CloseoutContext:
    """A structural claim, not an authenticated admission or drain permit.

    new_closeout may only admit a task narrowly scoped to the 15:00 bar;
    in_flight is an already admitted 5m task, restricted to the final bar
    after 15:00, and checked independently. The consumer must bind the claimed
    attempt, target, operation, bar, time and permit to verified records.
    """
    mode: str
    attempt_id: str
    admitted_attempt_id: str
    generation: int
    operation: str
    admitted_operation: str
    target: str
    admitted_target: str
    bar_at: datetime
    admitted_at: datetime
    permit_ref: str
    permit_expires_at: datetime
    remaining_worst: timedelta


@dataclass(frozen=True)
class PhaseDecision:
    reason: str
    now_bj: datetime = None
    last_start: datetime = None
    last_bar: datetime = None
    admit_new: bool = False
    may_write: bool = False
    may_rollback_mutation: bool = False


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def evaluate_phase(now_aware, calendar_evidence, purpose, target_date, budget, generation, *, closeout=None):
    """Classify one observation. All exceptions in malformed data fail closed.

    The one-second clock bound is uncalibrated and charged to the budget;
    paired monotonic elapsed must originate in this process. It does not prove
    sample authenticity or establish a production-approved clock policy.
    Other purposes require a separate future permit validator and remain blocked.
    """
    try:
        if not _aware(now_aware):
            return PhaseDecision("naive_or_invalid_clock")
        now = now_aware.astimezone(ZoneInfo("Asia/Shanghai"))
        if type(target_date) is not date or now.date() != target_date:
            return PhaseDecision("date_mismatch", now_bj=now)
        if type(generation) is not int or generation < 0:
            return PhaseDecision("invalid_generation", now_bj=now)
        if purpose is not Purpose.INTRADAY_PROVISIONAL:
            return PhaseDecision("purpose_requires_permit", now_bj=now)
        if not isinstance(calendar_evidence, CalendarEvidence):
            return PhaseDecision("calendar_unknown", now_bj=now)
        ev = calendar_evidence
        if (ev.state not in ("trading", "closed", "unknown") or ev.target_date != target_date or
                type(ev.generation) is not int or ev.generation != generation or
                not isinstance(ev.markets, tuple) or set(ev.markets) != {"SH", "SZ", "BJ"} or
                not isinstance(ev.version, str) or not ev.version.strip() or ev.coverage_complete is not True or
                not _aware(ev.checked_at) or not _aware(ev.expires_at) or
                not ev.checked_at <= now < ev.expires_at):
            return PhaseDecision("calendar_unknown", now_bj=now)
        if ev.state == "unknown":
            return PhaseDecision("calendar_unknown", now_bj=now)
        if ev.state == "closed":
            return PhaseDecision("non_trading_day", now_bj=now)
        if not isinstance(budget, PhaseBudget):
            return PhaseDecision("budget_unavailable", now_bj=now)
        if (not isinstance(budget.measured_worst_total, timedelta) or
                not isinstance(budget.margin, timedelta) or
                budget.measured_worst_total <= timedelta(0) or budget.margin < timedelta(0) or
                not _aware(budget.previous_wall) or not isinstance(budget.monotonic_elapsed, timedelta) or
                budget.monotonic_elapsed < timedelta(0)):
            return PhaseDecision("budget_or_clock_invalid", now_bj=now)
        previous = budget.previous_wall.astimezone(timezone.utc)
        wall_elapsed = now.astimezone(timezone.utc) - previous
        if (budget.previous_wall.astimezone(now.tzinfo).date() != target_date or
                wall_elapsed < timedelta(0) or
                (wall_elapsed == timedelta(0) and budget.monotonic_elapsed > timedelta(0)) or
                abs(wall_elapsed - budget.monotonic_elapsed) > CLOCK_SKEW_BOUND):
            return PhaseDecision("clock_discontinuity", now_bj=now)
        cutoff = datetime.combine(target_date, time(15, 5), now.tzinfo)
        last_start = cutoff - budget.measured_worst_total - budget.margin - CLOCK_SKEW_BOUND
        if now >= cutoff:
            return PhaseDecision("cutoff", now_bj=now, last_start=last_start)
        draining = False
        if budget.drain_at is not None:
            if not _aware(budget.drain_at) or budget.drain_at.astimezone(now.tzinfo).date() != target_date:
                return PhaseDecision("invalid_drain", now_bj=now, last_start=last_start)
            draining = now >= budget.drain_at
        wall = now.timetz().replace(tzinfo=None)
        if time(11, 30) <= wall < time(13, 0):
            return PhaseDecision("lunch", now_bj=now, last_start=last_start)
        if not (time(9, 30) <= wall < time(11, 30) or time(13, 0) <= wall < time(15, 5)):
            return PhaseDecision("outside_session", now_bj=now, last_start=last_start)
        if closeout is not None:
            c = closeout
            last_bar = datetime.combine(target_date, time(15, 0), now.tzinfo)
            if (type(c) is not CloseoutContext or c.mode not in ("new_closeout", "in_flight") or
                    type(c.attempt_id) is not str or not c.attempt_id.strip() or
                    c.attempt_id != c.admitted_attempt_id or type(c.generation) is not int or
                    c.generation != generation or c.operation not in ("write", "rollback") or
                    c.operation != c.admitted_operation or
                    c.target != "stock.kline_minute_5" or c.admitted_target != c.target or
                    type(c.permit_ref) is not str or not c.permit_ref.strip() or
                    not _aware(c.bar_at) or
                    not _aware(c.admitted_at) or c.admitted_at.astimezone(now.tzinfo).date() != target_date or
                    c.admitted_at > now or not _aware(c.permit_expires_at) or
                    c.permit_expires_at <= now or c.permit_expires_at > cutoff or
                    not isinstance(c.remaining_worst, timedelta) or c.remaining_worst <= timedelta(0)):
                return PhaseDecision("invalid_closeout", now_bj=now, last_start=last_start)
            bar = c.bar_at.astimezone(now.tzinfo)
            bar_wall = bar.timetz().replace(tzinfo=None)
            if (bar.date() != target_date or bar > now or bar.second != 0 or bar.microsecond != 0 or
                    bar.minute % 5 != 0 or not (time(9, 35) <= bar_wall <= time(11, 30) or
                                                time(13, 5) <= bar_wall <= time(15, 0)) or
                    (wall >= time(15, 0) and bar != last_bar)):
                return PhaseDecision("invalid_closeout", now_bj=now, last_start=last_start)
            if c.mode == "new_closeout":
                if draining or c.operation != "write" or c.admitted_at != now or bar != last_bar:
                    return PhaseDecision("draining" if draining else "invalid_closeout", now_bj=now, last_start=last_start)
                if now > last_start or (now == last_start and budget.margin == timedelta(0)):
                    return PhaseDecision("budget_exhausted", now_bj=now, last_start=last_start)
                if now + budget.measured_worst_total + budget.margin + CLOCK_SKEW_BOUND > c.permit_expires_at:
                    return PhaseDecision("budget_exhausted", now_bj=now, last_start=last_start)
                return PhaseDecision("closeout_admission", now_bj=now, last_start=last_start,
                                     last_bar=last_bar, admit_new=True)
            if budget.drain_at is not None and c.admitted_at >= budget.drain_at:
                return PhaseDecision("invalid_closeout", now_bj=now, last_start=last_start)
            if now + c.remaining_worst + budget.margin + CLOCK_SKEW_BOUND >= min(cutoff, c.permit_expires_at):
                return PhaseDecision("budget_exhausted", now_bj=now, last_start=last_start)
            return PhaseDecision("closeout_in_flight", now_bj=now, last_start=last_start,
                                 last_bar=bar, may_write=c.operation == "write",
                                 may_rollback_mutation=c.operation == "rollback")
        if draining:
            return PhaseDecision("draining", now_bj=now, last_start=last_start)
        if wall >= time(15, 0):
            return PhaseDecision("closeout_context_required", now_bj=now, last_start=last_start)
        if now > last_start or (now == last_start and budget.margin == timedelta(0)):
            return PhaseDecision("budget_exhausted", now_bj=now, last_start=last_start)
        return PhaseDecision("intraday", now_bj=now, last_start=last_start, admit_new=True)
    except (TypeError, ValueError, OverflowError, AttributeError, ZoneInfoNotFoundError):
        return PhaseDecision("invalid_input")