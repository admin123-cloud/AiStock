"""Build per-code daily coverage facts for downstream minute-line checks.

This module never infers an expected date from the formal daily table.  The
caller supplies an accepted date universe and a read-only formal date set;
every expected member is classified as present, verified suspension/delist,
status conflict, or an unresolved formal gap.  The resulting rows are stable
enough to persist in ``daily_coverage_audit`` and to drive minute completeness
checks later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from utils.daily_date_universe import normalize_code, normalize_trade_date


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


class DailyCoverageAuditError(ValueError):
    """Raised when coverage inputs cannot form a trustworthy audit."""


AUDIT_FORMAL_PRESENT = "formal_present"
AUDIT_SUSPENDED_VERIFIED = "suspended_verified"
AUDIT_STATUS_CONFLICT = "status_conflict"
AUDIT_FORMAL_MISSING_UNCONFIRMED = "formal_missing_unconfirmed"
AUDIT_FORMAL_OUTSIDE_UNIVERSE = "formal_outside_date_universe"


@dataclass(frozen=True)
class FormalDailyScopeSpan:
    """Per-code formal-table scope and observed first/last dates."""

    code: str
    scope_category: str
    row_count: int
    first_trade_date: date
    last_trade_date: date


@dataclass(frozen=True)
class FormalDailyScopeSets:
    """Frozen current/historical/core code sets for the formal daily table."""

    current_stock_codes: frozenset[str]
    historical_stock_codes: frozenset[str]
    core_index_codes: frozenset[str]

    @property
    def allowed_codes(self) -> frozenset[str]:
        return self.current_stock_codes | self.historical_stock_codes | self.core_index_codes


def derive_formal_scope_sets(
    master_rows: Iterable[Mapping[str, Any]],
    *,
    historical_candidate_codes: Iterable[object] = (),
    core_index_codes: Iterable[object],
    as_of: object,
) -> FormalDailyScopeSets:
    """Derive the formal table scope from master lifecycle and explicit history.

    A stock is current only when it is a stock row, not quit, and has no
    delist date before ``as_of``.  All other stock master rows with formal
    history are historical candidates; separately classified unmapped stock
    codes are included as historical candidates as well.
    """

    cutoff = normalize_trade_date(as_of)
    core = frozenset(normalize_code(code) for code in core_index_codes)
    explicit_history = {normalize_code(code) for code in historical_candidate_codes}
    current: set[str] = set()
    historical: set[str] = set(explicit_history)
    for row in master_rows:
        if str(row.get("type") or "").strip().lower() != "stock":
            continue
        code = normalize_code(row.get("code"))
        listing_status = str(row.get("listing_status") or "").strip().lower()
        if listing_status in {"pending_listing", "not_listed", "pre_listing"}:
            # A pending listing is a known absence of an expected formal bar,
            # not a current-stock gap and not historical daily scope.
            current.discard(code)
            historical.discard(code)
            continue
        delist = row.get("delist_date")
        quit_value = int(row.get("quit") or 0)
        active = quit_value == 0 and (
            delist in (None, "", "NaT") or normalize_trade_date(delist) >= cutoff
        )
        if active:
            current.add(code)
        else:
            historical.add(code)
    historical.difference_update(current)
    historical.difference_update(core)
    current.difference_update(core)
    return FormalDailyScopeSets(
        current_stock_codes=frozenset(current),
        historical_stock_codes=frozenset(historical),
        core_index_codes=core,
    )


def summarize_formal_scope(
    rows: Iterable[Mapping[str, Any]],
    *,
    current_stock_codes: Iterable[object],
    historical_stock_codes: Iterable[object],
    core_index_codes: Iterable[object],
) -> tuple[FormalDailyScopeSpan, ...]:
    """Summarize formal rows and flag codes outside the frozen scope.

    The function only summarizes supplied rows; it never chooses an expected
    date range from those rows.  Expected ranges come from the date-universe
    registry and are handled by :func:`build_daily_coverage_audit`.
    """

    current = {normalize_code(code) for code in current_stock_codes}
    historical = {normalize_code(code) for code in historical_stock_codes}
    core = {normalize_code(code) for code in core_index_codes}
    grouped: dict[str, list[date]] = {}
    for row in rows:
        code = normalize_code(row.get("code"))
        trade_date = normalize_trade_date(row.get("trade_date"))
        grouped.setdefault(code, []).append(trade_date)

    output: list[FormalDailyScopeSpan] = []
    for code in sorted(grouped):
        dates = grouped[code]
        if code in core:
            category = "core_index"
        elif code in current:
            category = "current_stock"
        elif code in historical:
            category = "historical_stock"
        else:
            category = "out_of_scope"
        output.append(
            FormalDailyScopeSpan(
                code=code,
                scope_category=category,
                row_count=len(dates),
                first_trade_date=min(dates),
                last_trade_date=max(dates),
            )
        )
    return tuple(output)


@dataclass(frozen=True)
class DailyCoverageAuditRow:
    audit_id: str
    code: str
    trade_date: date
    coverage_start: date
    coverage_end: date
    date_universe_id: str
    date_set_sha256: str
    audit_state: str
    status_audit_ref: str
    minute_reference_required: bool
    evidence_json: str
    observed_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "code": self.code,
            "trade_date": self.trade_date,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "date_universe_id": self.date_universe_id,
            "date_set_sha256": self.date_set_sha256,
            "audit_state": self.audit_state,
            "status_audit_ref": self.status_audit_ref,
            "minute_reference_required": int(self.minute_reference_required),
            "evidence_json": self.evidence_json,
            "observed_at": self.observed_at,
        }


class DailyCoverageAuditPublisher:
    """Publish coverage rows through injected sinks with readback validation.

    The default application path does not instantiate a ClickHouse writer.
    A deployment task can bind the sink to an explicit transaction after the
    online table migration has been reviewed.
    """

    def __init__(
        self,
        *,
        audit_sink: Callable[[Sequence[Mapping[str, Any]]], None],
        readback: Callable[[Sequence[DailyCoverageAuditRow]], None] | None = None,
    ) -> None:
        self._audit_sink = audit_sink
        self._readback = readback

    def publish(self, rows: Sequence[DailyCoverageAuditRow]) -> int:
        if not rows:
            return 0
        audit_ids = [row.audit_id for row in rows]
        if len(audit_ids) != len(set(audit_ids)):
            raise DailyCoverageAuditError("duplicate audit_id in coverage publish batch")
        keys = [(row.code, row.trade_date) for row in rows]
        if len(keys) != len(set(keys)):
            raise DailyCoverageAuditError("duplicate code/trade_date in coverage publish batch")
        self._audit_sink(tuple(row.as_dict() for row in rows))
        if self._readback is not None:
            self._readback(rows)
        return len(rows)


def _canonical_status_ref(status_rows: Sequence[Mapping[str, Any]]) -> str:
    if not status_rows:
        return ""
    payload = json.dumps(
        [dict(sorted((str(k), str(v)) for k, v in row.items())) for row in status_rows],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def build_daily_coverage_audit(
    *,
    code: object,
    expected_dates: Iterable[object],
    formal_dates: Iterable[object],
    date_universe_id: str,
    date_set_sha256: str,
    status_index: Any,
    observed_at: datetime | None = None,
    source_status_rows: Mapping[object, Sequence[Mapping[str, Any]]] | None = None,
) -> tuple[DailyCoverageAuditRow, ...]:
    """Classify the supplied source universe against formal dates.

    ``status_index`` follows ``utils.market_status.StatusIndex``: its
    ``decide(code, day)`` result is ``exempt``, ``conflict`` or
    ``unregistered``.  Only verified exemptions suppress the minute reference
    requirement; all other missing formal rows remain actionable.
    """

    canonical_code = normalize_code(code)
    expected = tuple(sorted({normalize_trade_date(item) for item in expected_dates}))
    formal = {normalize_trade_date(item) for item in formal_dates}
    if not date_universe_id or not date_set_sha256:
        raise DailyCoverageAuditError("date universe identity and digest are required")
    if not expected:
        raise DailyCoverageAuditError("expected date universe cannot be empty")
    coverage_start, coverage_end = expected[0], expected[-1]
    timestamp = observed_at or datetime.now(BUSINESS_TZ)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=BUSINESS_TZ)
    status_rows = source_status_rows or {}

    rows: list[DailyCoverageAuditRow] = []

    def add(day: date, state: str, *, minute_required: bool, extra: Mapping[str, Any]) -> None:
        day_rows = tuple(status_rows.get(day, status_rows.get(day.isoformat(), ())) or ())
        if not day_rows and hasattr(status_index, "matches"):
            day_rows = tuple(status_index.matches(canonical_code, day) or ())
        status_ref = _canonical_status_ref(day_rows)
        evidence = {
            "date_universe_id": date_universe_id,
            "date_set_sha256": date_set_sha256,
            "formal_present": day in formal,
            "status_decision": state,
            "status_rows": list(day_rows),
            **dict(extra),
        }
        audit_key = "\0".join(
            [canonical_code, day.isoformat(), date_universe_id, state, status_ref]
        )
        rows.append(
            DailyCoverageAuditRow(
                audit_id=sha256(audit_key.encode("utf-8")).hexdigest(),
                code=canonical_code,
                trade_date=day,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                date_universe_id=date_universe_id,
                date_set_sha256=date_set_sha256,
                audit_state=state,
                status_audit_ref=status_ref,
                minute_reference_required=minute_required,
                evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                observed_at=timestamp,
            )
        )

    for day in expected:
        if day in formal:
            add(day, AUDIT_FORMAL_PRESENT, minute_required=True, extra={})
            continue
        decision = str(status_index.decide(canonical_code, day) or "unregistered")
        if decision == "exempt":
            add(
                day,
                AUDIT_SUSPENDED_VERIFIED,
                minute_required=False,
                extra={"status_exemption": True},
            )
        elif decision == "conflict":
            add(
                day,
                AUDIT_STATUS_CONFLICT,
                minute_required=True,
                extra={"status_exemption": False},
            )
        else:
            add(
                day,
                AUDIT_FORMAL_MISSING_UNCONFIRMED,
                minute_required=True,
                extra={"status_exemption": False},
            )

    for day in sorted(formal - set(expected)):
        add(
            day,
            AUDIT_FORMAL_OUTSIDE_UNIVERSE,
            minute_required=False,
            extra={"outside_expected_universe": True},
        )
    return tuple(rows)
