"""Read-only daily-table closure snapshot and value-domain classification.

The snapshot is deliberately independent from ClickHouse writes.  Callers
provide an accepted :class:`DailyExactKeyPermit`, the formal rows, optional
source rows and optional lifecycle/status evidence.  The permit supplies the
expected key set; the formal table is never allowed to define its own
completeness denominator.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
import math
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from utils.daily_date_universe import (
    DailyExactKeyPermit,
    DateUniverseContractError,
    normalize_code,
    normalize_trade_date,
)


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

CLOSURE_HEALTHY = "healthy"
CLOSURE_REVIEW = "review"
CLOSURE_BLOCKED = "blocked"

CLASS_NONFINITE_OR_NEGATIVE = "nonfinite_or_negative_value"
CLASS_OHLC_ENVELOPE = "ohlc_envelope_violation"
CLASS_AMOUNT_POSITIVE_VOLUME_NONPOSITIVE = "amount_positive_volume_nonpositive"
CLASS_VOLUME_POSITIVE_AMOUNT_ZERO = "volume_positive_amount_zero_value_domain"
CLASS_VOLUME_POSITIVE_ZERO_OHLC = "volume_positive_zero_ohlc_amount_zero"
CLASS_VOLUME_POSITIVE_AMOUNT_ZERO_SOURCE = "volume_positive_amount_zero_source_coverage_exception"
CLASS_VOLUME_POSITIVE_AMOUNT_ZERO_LIFECYCLE = "volume_positive_amount_zero_lifecycle_exception"

SOURCE_EXCEPTION_STATUSES = frozenset(
    {"source_absent", "ths_date_absent", "ths_coverage_unknown"}
)
LIFECYCLE_EXCEPTION_STATUSES = frozenset(
    {"suspended", "delisted", "not_trading", "formal_bar_missing"}
)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _key(row: Mapping[str, Any]) -> tuple[str, date]:
    return normalize_code(row.get("code")), normalize_trade_date(
        row.get("trade_date")
    )


def _status_names(status_rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({str(row.get("status") or "").strip() for row in status_rows if row.get("status")}))


def _review_states(status_rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(row.get("review_state") or "").strip()
                for row in status_rows
                if row.get("review_state")
            }
        )
    )


def _classification(
    row: Mapping[str, Any],
    *,
    status_rows: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any] | None:
    """Classify one formal daily row, without changing the row."""

    code, trade_date = _key(row)
    statuses = tuple(status_rows)
    names = _status_names(statuses)
    review_states = _review_states(statuses)
    fields = {
        name: _as_float(row.get(name))
        for name in ("open", "high", "low", "close", "volume", "amount")
    }
    values = tuple(fields.values())
    evidence = {
        "code": code,
        "trade_date": trade_date.isoformat(),
        **fields,
        "status": list(names),
        "review_state": list(review_states),
    }

    if any(value is None or not math.isfinite(value) for value in values) or any(
        value < 0 for value in values if value is not None
    ):
        return {
            **evidence,
            "classification": CLASS_NONFINITE_OR_NEGATIVE,
            "action": "blocked_until_source_evidence",
            "severity": CLOSURE_BLOCKED,
        }

    op, high, low, close, volume, amount = values
    if high < max(op, close) or low > min(op, close) or high < low:
        return {
            **evidence,
            "classification": CLASS_OHLC_ENVELOPE,
            "action": "review",
            "severity": CLOSURE_REVIEW,
        }

    if amount > 0 and volume <= 0:
        return {
            **evidence,
            "classification": CLASS_AMOUNT_POSITIVE_VOLUME_NONPOSITIVE,
            "action": "review",
            "severity": CLOSURE_REVIEW,
        }

    if volume > 0 and amount <= 0:
        if op == 0 and high == 0 and low == 0 and close == 0:
            return {
                **evidence,
                "classification": CLASS_VOLUME_POSITIVE_ZERO_OHLC,
                "action": "blocked_until_source_evidence",
                "severity": CLOSURE_BLOCKED,
            }
        if SOURCE_EXCEPTION_STATUSES.intersection(names):
            return {
                **evidence,
                "classification": CLASS_VOLUME_POSITIVE_AMOUNT_ZERO_SOURCE,
                "action": "review_with_source_evidence",
                "severity": CLOSURE_REVIEW,
            }
        if LIFECYCLE_EXCEPTION_STATUSES.intersection(names):
            return {
                **evidence,
                "classification": CLASS_VOLUME_POSITIVE_AMOUNT_ZERO_LIFECYCLE,
                "action": "review_with_lifecycle_evidence",
                "severity": CLOSURE_REVIEW,
            }
        return {
            **evidence,
            "classification": CLASS_VOLUME_POSITIVE_AMOUNT_ZERO,
            "action": "blocked_until_source_evidence",
            "severity": CLOSURE_BLOCKED,
        }
    return None


def classify_daily_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    status_rows: Iterable[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], ...]:
    """Return every anomalous row with a deterministic category and action."""

    statuses_by_code: defaultdict[str, list[tuple[date, date, Mapping[str, Any]]]] = defaultdict(list)
    for status in status_rows:
        try:
            code = normalize_code(status.get("code"))
            start = normalize_trade_date(status.get("start_date"))
            end = normalize_trade_date(status.get("end_date"))
        except DateUniverseContractError:
            continue
        statuses_by_code[code].append((start, end, status))

    output: list[dict[str, Any]] = []
    for row in rows:
        code, trade_date = _key(row)
        matching = [
            status
            for start, end, status in statuses_by_code.get(code, ())
            if start <= trade_date <= end
        ]
        item = _classification(row, status_rows=matching)
        if item is not None:
            output.append(item)
    return tuple(output)


def _normalise_keys(keys: Iterable[tuple[object, object]]) -> frozenset[tuple[str, date]]:
    return frozenset(
        (normalize_code(code), normalize_trade_date(trade_date))
        for code, trade_date in keys
    )


def _float_equal(left: Any, right: Any, *, rel_tol: float, abs_tol: float) -> bool:
    left_value = _as_float(left)
    right_value = _as_float(right)
    if left_value is None or right_value is None:
        return left_value is right_value
    return math.isclose(left_value, right_value, rel_tol=rel_tol, abs_tol=abs_tol)


def _source_reconciliation(
    formal_rows: Sequence[Mapping[str, Any]],
    source_rows: Sequence[Mapping[str, Any]] | None,
    *,
    rel_tol: float,
    abs_tol: float,
) -> dict[str, Any]:
    if source_rows is None:
        return {
            "status": CLOSURE_REVIEW,
            "reason": "source_rows_not_supplied",
            "formal_rows_compared": 0,
            "source_rows_compared": 0,
            "missing_source_keys": 0,
            "mismatched_rows": 0,
        }
    formal_by_key = {_key(row): row for row in formal_rows}
    source_by_key = {_key(row): row for row in source_rows}
    missing = sorted(set(formal_by_key) - set(source_by_key))
    mismatches: list[tuple[str, date]] = []
    compare_fields = ("open", "high", "low", "close", "volume", "amount")
    for key in sorted(set(formal_by_key) & set(source_by_key)):
        left, right = formal_by_key[key], source_by_key[key]
        if any(
            not _float_equal(left.get(field), right.get(field), rel_tol=rel_tol, abs_tol=abs_tol)
            for field in compare_fields
        ):
            mismatches.append(key)
    status = CLOSURE_BLOCKED if mismatches else CLOSURE_REVIEW if missing else CLOSURE_HEALTHY
    return {
        "status": status,
        "reason": "source_value_mismatch" if mismatches else "source_key_missing" if missing else "matched",
        "formal_rows_compared": len(formal_by_key),
        "source_rows_compared": len(source_by_key),
        "missing_source_keys": len(missing),
        "missing_source_samples": [f"{code}@{day.isoformat()}" for code, day in missing[:20]],
        "mismatched_rows": len(mismatches),
        "mismatch_samples": [f"{code}@{day.isoformat()}" for code, day in mismatches[:20]],
    }


def _digest_classifications(rows: Sequence[Mapping[str, Any]]) -> str:
    body = "\n".join(
        json.dumps(dict(sorted(row.items())), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in sorted(rows, key=lambda item: (str(item.get("code")), str(item.get("trade_date")), str(item.get("classification"))))
    )
    return sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DailyClosureSnapshot:
    """Serializable result of the four read-only daily closure gates."""

    generated_at: datetime
    status: str
    expected_key_count: int
    formal_key_count: int
    permit_digest: str | None
    gates: Mapping[str, Mapping[str, Any]]
    anomaly_counts: Mapping[str, int]
    anomaly_samples: Mapping[str, tuple[Mapping[str, Any], ...]]
    anomaly_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": self.generated_at.isoformat(),
            "readonly": True,
            "status": self.status,
            "expected_key_count": self.expected_key_count,
            "formal_key_count": self.formal_key_count,
            "permit_digest": self.permit_digest,
            "gates": {name: dict(value) for name, value in self.gates.items()},
            "anomaly_counts": dict(self.anomaly_counts),
            "anomaly_samples": {
                name: [dict(item) for item in values]
                for name, values in self.anomaly_samples.items()
            },
            "anomaly_digest": self.anomaly_digest,
        }


def build_daily_closure_snapshot(
    *,
    permit: DailyExactKeyPermit | None,
    formal_rows: Iterable[Mapping[str, Any]],
    expected_keys: Iterable[tuple[object, object]] | None = None,
    source_rows: Iterable[Mapping[str, Any]] | None = None,
    status_rows: Iterable[Mapping[str, Any]] = (),
    expected_latest_trade_date: object | None = None,
    source_rel_tol: float = 1e-9,
    source_abs_tol: float = 1e-9,
    sample_limit: int = 5,
    permit_error: str | None = None,
    observed_at: datetime | None = None,
) -> DailyClosureSnapshot:
    """Build a closure snapshot from read-only evidence.

    ``permit.keys`` is the expected denominator when a permit is supplied. If
    a separate expected set is supplied it must be a subset of that permit;
    this prevents a local manifest from silently bypassing registry/member
    readback.
    """

    formal = tuple(formal_rows)
    source = None if source_rows is None else tuple(source_rows)
    statuses = tuple(status_rows)
    generated_at = observed_at or datetime.now(BUSINESS_TZ)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=BUSINESS_TZ)

    requested = (
        _normalise_keys(expected_keys)
        if expected_keys is not None
        else (permit.keys if permit is not None else frozenset())
    )
    permit_errors: list[str] = []
    if permit is None:
        permit_errors.append(permit_error or "registry_member_exact_key_permit_missing")
    elif permit_error:
        # A local accepted registry/member evidence read can prove the exact
        # keys while the online production tables are still absent.  Keep the
        # permit for denominator checks, but retain the deployment gate as
        # blocked instead of silently upgrading local evidence to production.
        permit_errors.append(permit_error)
    if permit is not None and expected_keys is not None:
        try:
            permit.assert_allows(requested, operation="daily_closure_snapshot")
        except DateUniverseContractError as exc:
            permit_errors.append(str(exc))

    formal_keys = [_key(row) for row in formal]
    formal_key_set = frozenset(formal_keys)
    expected_latest = (
        normalize_trade_date(expected_latest_trade_date)
        if expected_latest_trade_date is not None
        else (max((key[1] for key in requested), default=None))
    )
    formal_latest = max((key[1] for key in formal_keys), default=None)
    missing = sorted(requested - formal_key_set)
    outside = sorted(formal_key_set - requested) if requested else []
    duplicate_counts = Counter(formal_keys)
    duplicate_keys = sorted(key for key, count in duplicate_counts.items() if count > 1)

    classifications = classify_daily_rows(formal, status_rows=statuses)
    anomaly_counts = Counter(item["classification"] for item in classifications)
    samples: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in classifications:
        category = str(item["classification"])
        if len(samples[category]) < sample_limit:
            samples[category].append(item)

    permit_gate: dict[str, Any] = {
        "status": CLOSURE_BLOCKED if permit_errors else CLOSURE_HEALTHY,
        "reason": permit_errors[0] if permit_errors else "registry_member_exact_key_readback_verified",
        "permit_digest": permit.permit_digest if permit is not None else None,
        "dataset_ids": sorted(permit.dataset_ids) if permit is not None else [],
        "errors": permit_errors,
    }
    coverage_errors = []
    if not requested:
        coverage_errors.append("expected_key_set_missing")
    if missing:
        coverage_errors.append("formal_keys_missing")
    if outside:
        coverage_errors.append("formal_keys_outside_expected_universe")
    if duplicate_keys:
        coverage_errors.append("duplicate_formal_keys")
    if expected_latest is not None and (formal_latest is None or formal_latest < expected_latest):
        coverage_errors.append("latest_formal_trade_date_behind_expected")
    coverage_gate = {
        "status": CLOSURE_BLOCKED if coverage_errors else CLOSURE_HEALTHY,
        "reason": coverage_errors[0] if coverage_errors else "coverage_and_physical_checks_passed",
        "expected_key_count": len(requested),
        "formal_key_count": len(formal_key_set),
        "missing_key_count": len(missing),
        "missing_key_samples": [f"{code}@{day.isoformat()}" for code, day in missing[:20]],
        "outside_key_count": len(outside),
        "outside_key_samples": [f"{code}@{day.isoformat()}" for code, day in outside[:20]],
        "duplicate_key_count": len(duplicate_keys),
        "duplicate_key_samples": [f"{code}@{day.isoformat()}" for code, day in duplicate_keys[:20]],
        "expected_latest_trade_date": expected_latest.isoformat() if expected_latest else None,
        "formal_latest_trade_date": formal_latest.isoformat() if formal_latest else None,
    }
    blocking_anomalies = sum(
        1 for item in classifications if item["severity"] == CLOSURE_BLOCKED
    )
    review_anomalies = len(classifications) - blocking_anomalies
    value_gate = {
        "status": CLOSURE_BLOCKED if blocking_anomalies else CLOSURE_REVIEW if review_anomalies else CLOSURE_HEALTHY,
        "reason": "blocking_value_domain_anomalies" if blocking_anomalies else "review_value_domain_anomalies" if review_anomalies else "value_domain_passed",
        "anomaly_rows": len(classifications),
        "blocking_rows": blocking_anomalies,
        "review_rows": review_anomalies,
        "classification_complete": True,
    }
    source_gate = _source_reconciliation(
        formal,
        source,
        rel_tol=source_rel_tol,
        abs_tol=source_abs_tol,
    )
    gate_statuses = [permit_gate["status"], coverage_gate["status"], value_gate["status"], source_gate["status"]]
    status = (
        CLOSURE_BLOCKED
        if CLOSURE_BLOCKED in gate_statuses
        else CLOSURE_REVIEW
        if CLOSURE_REVIEW in gate_statuses
        else CLOSURE_HEALTHY
    )
    return DailyClosureSnapshot(
        generated_at=generated_at,
        status=status,
        expected_key_count=len(requested),
        formal_key_count=len(formal_key_set),
        permit_digest=permit.permit_digest if permit is not None else None,
        gates={
            "registry_member_exact_key": permit_gate,
            "formal_coverage_and_physical": coverage_gate,
            "value_domain": value_gate,
            "source_reconciliation": source_gate,
        },
        anomaly_counts=dict(sorted(anomaly_counts.items())),
        anomaly_samples={name: tuple(values) for name, values in sorted(samples.items())},
        anomaly_digest=_digest_classifications(classifications),
    )


def build_daily_closure_snapshot_from_reader(
    *,
    reader: Any,
    expected_keys: Iterable[tuple[object, object]],
    formal_rows: Iterable[Mapping[str, Any]],
    source: str,
    source_rows: Iterable[Mapping[str, Any]] | None = None,
    status_rows: Iterable[Mapping[str, Any]] = (),
    **kwargs: Any,
) -> DailyClosureSnapshot:
    """Read registry/member through the injected reader, then build a snapshot.

    A registry/member readback failure is represented as a blocked snapshot so
    an audit artifact remains available without weakening the write gate.
    """

    requested = tuple(expected_keys)
    permit_error_override = kwargs.pop("permit_error", None)
    try:
        permit = reader.exact_key_permit(requested, source=source)
        error = None
    except Exception as exc:  # pragma: no cover - concrete reader failures are integration-specific
        permit = None
        error = f"registry_member_exact_key_readback_failed: {type(exc).__name__}: {exc}"
    return build_daily_closure_snapshot(
        permit=permit,
        expected_keys=requested,
        formal_rows=formal_rows,
        source_rows=source_rows,
        status_rows=status_rows,
        permit_error=error or permit_error_override,
        **kwargs,
    )

