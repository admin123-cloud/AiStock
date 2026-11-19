"""Immutable daily date-universe records and exact-key permits.

The formal daily table must never be used to infer its own expected dates.  A
date universe is published from an independently reviewed source and then
read back by ``DailyDateUniverseReader`` before a writer receives an exact-key
permit.  This module contains the pure validation and hashing contract; the
ClickHouse publisher accepts injected sinks so unit tests never need to issue
DDL or DML.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DATE_UNIVERSE_COMPLETE = "complete"
DATE_UNIVERSE_UNAVAILABLE = "unavailable"
DATE_UNIVERSE_UNKNOWN = "unknown"
DATE_UNIVERSE_STATES = frozenset(
    {DATE_UNIVERSE_COMPLETE, DATE_UNIVERSE_UNAVAILABLE, DATE_UNIVERSE_UNKNOWN}
)


class DateUniverseContractError(ValueError):
    """Raised when a registry/member payload cannot be trusted."""


def normalize_code(value: object) -> str:
    code = str(value or "").strip().upper()
    if not code:
        raise DateUniverseContractError("security code is required")
    return code


def normalize_trade_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        raise DateUniverseContractError("trade date is required")
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise DateUniverseContractError(f"invalid trade date: {value!r}") from exc


def _normalize_dates(values: Iterable[object]) -> tuple[date, ...]:
    result = sorted({normalize_trade_date(value) for value in values})
    return tuple(result)


def date_set_sha256(code: object, dates: Iterable[object]) -> str:
    canonical_code = normalize_code(code)
    normalized = _normalize_dates(dates)
    payload = "".join(f"{canonical_code}\t{item.isoformat()}\n" for item in normalized)
    return sha256(payload.encode("utf-8")).hexdigest()


def permit_sha256(keys: Iterable[tuple[object, object]], dataset_ids: Iterable[object]) -> str:
    normalized = sorted(
        (normalize_code(code), normalize_trade_date(trade_date).isoformat())
        for code, trade_date in keys
    )
    dataset_part = "\n".join(sorted(str(item).strip() for item in dataset_ids if str(item).strip()))
    key_part = "\n".join(f"{code}\t{trade_date}" for code, trade_date in normalized)
    return sha256((dataset_part + "\n--keys--\n" + key_part).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DailyExactKeyPermit:
    """A caller-owned permit for an immutable set of ``(code, trade_date)`` keys."""

    source: str
    keys: frozenset[tuple[str, date]]
    dataset_ids: frozenset[str]
    date_set_digests: Mapping[str, str]
    permit_digest: str

    @classmethod
    def from_keys(
        cls,
        keys: Iterable[tuple[object, object]],
        *,
        source: str,
        dataset_ids: Iterable[object] = (),
        date_set_digests: Mapping[object, object] | None = None,
    ) -> "DailyExactKeyPermit":
        normalized = frozenset(
            (normalize_code(code), normalize_trade_date(trade_date))
            for code, trade_date in keys
        )
        datasets = frozenset(str(item).strip() for item in dataset_ids if str(item).strip())
        digests = {
            normalize_code(code): str(digest).strip()
            for code, digest in (date_set_digests or {}).items()
            if str(digest).strip()
        }
        return cls(
            source=str(source).strip() or "unknown",
            keys=normalized,
            dataset_ids=datasets,
            date_set_digests=digests,
            permit_digest=permit_sha256(normalized, datasets),
        )

    def missing(self, keys: Iterable[tuple[object, object]]) -> list[tuple[str, date]]:
        requested = {
            (normalize_code(code), normalize_trade_date(trade_date))
            for code, trade_date in keys
        }
        return sorted(requested - self.keys)

    def assert_allows(self, keys: Iterable[tuple[object, object]], *, operation: str) -> None:
        missing = self.missing(keys)
        if missing:
            sample = ", ".join(f"{code}@{trade_date.isoformat()}" for code, trade_date in missing[:20])
            suffix = "..." if len(missing) > 20 else ""
            raise DateUniverseContractError(
                f"exact-key permit {operation} rejected {len(missing)} key(s): {sample}{suffix}"
            )


@dataclass(frozen=True)
class DateUniverseRecord:
    """One active registry row and its immutable member digest."""

    dataset_id: str
    code: str
    security_type: str
    identity_status: str
    source_kind: str
    source_ref: str
    source_sha256: str
    coverage_start: date | None
    coverage_end: date | None
    completeness_state: str
    date_set_sha256: str
    member_count: int
    published_at: datetime

    def registry_row(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "code": self.code,
            "security_type": self.security_type,
            "identity_status": self.identity_status,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "source_sha256": self.source_sha256,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "completeness_state": self.completeness_state,
            "date_set_sha256": self.date_set_sha256,
            "member_count": self.member_count,
            "published_at": self.published_at,
            "is_active": 1,
        }


@dataclass(frozen=True)
class DateUniversePayload:
    registry: DateUniverseRecord
    member_rows: tuple[dict[str, Any], ...]

    @property
    def keys(self) -> frozenset[tuple[str, date]]:
        return frozenset((row["code"], row["trade_date"]) for row in self.member_rows)

    def exact_key_permit(self, *, source: str | None = None) -> DailyExactKeyPermit:
        return DailyExactKeyPermit.from_keys(
            self.keys,
            source=source or f"date-universe:{self.registry.dataset_id}",
            dataset_ids=(self.registry.dataset_id,),
            date_set_digests={self.registry.code: self.registry.date_set_sha256},
        )


def build_date_universe_payload(
    *,
    code: object,
    security_type: str,
    identity_status: str,
    source_kind: str,
    source_ref: str,
    source_sha256: str,
    dates: Iterable[object],
    completeness_state: str = DATE_UNIVERSE_COMPLETE,
    published_at: datetime | None = None,
) -> DateUniversePayload:
    """Validate and freeze a registry row plus its member rows."""

    canonical_code = normalize_code(code)
    normalized_dates = _normalize_dates(dates)
    state = str(completeness_state or "").strip().lower()
    if state not in DATE_UNIVERSE_STATES:
        raise DateUniverseContractError(f"unsupported completeness_state: {completeness_state!r}")
    if not str(identity_status or "").strip():
        raise DateUniverseContractError("identity_status is required")
    if not str(source_kind or "").strip() or not str(source_ref or "").strip():
        raise DateUniverseContractError("source_kind and source_ref are required")
    if not str(source_sha256 or "").strip():
        raise DateUniverseContractError("source_sha256 is required")
    if state == DATE_UNIVERSE_COMPLETE and not normalized_dates:
        raise DateUniverseContractError("complete date universe must contain at least one date")
    if state != DATE_UNIVERSE_COMPLETE and normalized_dates:
        raise DateUniverseContractError("unavailable/unknown date universe cannot contain members")

    digest = date_set_sha256(canonical_code, normalized_dates)
    dataset_id = sha256(
        "\0".join(
            [
                canonical_code,
                str(source_kind).strip(),
                str(source_sha256).strip(),
                digest,
            ]
        ).encode("utf-8")
    ).hexdigest()[:32]
    timestamp = published_at or datetime.now(BUSINESS_TZ)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=BUSINESS_TZ)
    registry = DateUniverseRecord(
        dataset_id=dataset_id,
        code=canonical_code,
        security_type=str(security_type or "").strip().lower(),
        identity_status=str(identity_status).strip(),
        source_kind=str(source_kind).strip(),
        source_ref=str(source_ref).strip(),
        source_sha256=str(source_sha256).strip(),
        coverage_start=normalized_dates[0] if normalized_dates else None,
        coverage_end=normalized_dates[-1] if normalized_dates else None,
        completeness_state=state,
        date_set_sha256=digest,
        member_count=len(normalized_dates),
        published_at=timestamp,
    )
    member_rows = tuple(
        {
            "dataset_id": dataset_id,
            "code": canonical_code,
            "trade_date": trade_date,
            "member_ordinal": ordinal,
            "date_set_sha256": digest,
            "published_at": timestamp,
        }
        for ordinal, trade_date in enumerate(normalized_dates, start=1)
    )
    return DateUniversePayload(registry=registry, member_rows=member_rows)


class DateUniversePublisher:
    """Publish through injected sinks and verify the exact payload read back.

    The production caller can bind these sinks to a transaction later.  No
    ClickHouse client is created here, which keeps this layer safe to test and
    prevents this feature from issuing production DDL/DML by accident.
    """

    def __init__(
        self,
        *,
        registry_sink: Callable[[Mapping[str, Any]], None],
        member_sink: Callable[[Sequence[Mapping[str, Any]]], None],
        readback: Callable[[DateUniversePayload], None] | None = None,
    ) -> None:
        self._registry_sink = registry_sink
        self._member_sink = member_sink
        self._readback = readback

    def publish(self, payload: DateUniversePayload) -> DateUniverseRecord:
        self._member_sink(payload.member_rows)
        self._registry_sink(payload.registry.registry_row())
        if self._readback is not None:
            self._readback(payload)
        return payload.registry


def build_exact_key_permit_from_rows(
    requested_keys: Iterable[tuple[object, object]],
    *,
    registry_rows: Iterable[Mapping[str, Any]],
    member_rows: Iterable[Mapping[str, Any]],
    source: str,
) -> DailyExactKeyPermit:
    """Create a permit only when registry and member readback prove every key."""

    requested = frozenset(
        (normalize_code(code), normalize_trade_date(trade_date))
        for code, trade_date in requested_keys
    )
    if not requested:
        raise DateUniverseContractError("cannot create an empty exact-key permit")

    active: dict[str, Mapping[str, Any]] = {}
    for row in registry_rows:
        code = normalize_code(row.get("code"))
        if int(row.get("is_active", 1) or 0) != 1:
            continue
        if str(row.get("completeness_state") or "").strip().lower() != DATE_UNIVERSE_COMPLETE:
            continue
        if not str(row.get("dataset_id") or "").strip():
            raise DateUniverseContractError(f"registry row for {code} has no dataset_id")
        if code in active and active[code].get("dataset_id") != row.get("dataset_id"):
            raise DateUniverseContractError(f"multiple active date universes for {code}")
        active[code] = row

    missing_codes = sorted({code for code, _ in requested} - set(active))
    if missing_codes:
        raise DateUniverseContractError(
            "no active complete date universe for: " + ", ".join(missing_codes[:20])
        )

    members: set[tuple[str, date]] = set()
    for row in member_rows:
        code = normalize_code(row.get("code"))
        trade_date = normalize_trade_date(row.get("trade_date"))
        dataset_id = str(row.get("dataset_id") or "").strip()
        if code not in active or dataset_id != str(active[code].get("dataset_id") or "").strip():
            continue
        expected_digest = str(active[code].get("date_set_sha256") or "").strip()
        actual_digest = str(row.get("date_set_sha256") or expected_digest).strip()
        if expected_digest and actual_digest != expected_digest:
            raise DateUniverseContractError(f"member digest mismatch for {code}@{trade_date}")
        key = (code, trade_date)
        if key in requested:
            members.add(key)

    missing = requested - members
    if missing:
        sample = ", ".join(f"{code}@{trade_date.isoformat()}" for code, trade_date in sorted(missing)[:20])
        raise DateUniverseContractError(f"date-universe member readback missing: {sample}")

    return DailyExactKeyPermit.from_keys(
        requested,
        source=source,
        dataset_ids=(str(active[code]["dataset_id"]) for code, _ in requested),
        date_set_digests={
            code: str(active[code].get("date_set_sha256") or "")
            for code, _ in requested
        },
    )


class DailyDateUniverseReader:
    """Read-only adapter with injectable registry/member query functions."""

    def __init__(
        self,
        *,
        registry_loader: Callable[[Sequence[str]], Iterable[Mapping[str, Any]]],
        member_loader: Callable[[Sequence[str], Sequence[tuple[str, date]]], Iterable[Mapping[str, Any]]],
    ) -> None:
        self._registry_loader = registry_loader
        self._member_loader = member_loader

    @classmethod
    def from_clickhouse(cls, client: Any, *, database: str = "stock") -> "DailyDateUniverseReader":
        """Build a read-only ClickHouse adapter.

        The adapter issues SELECTs with ``readonly=1`` only.  It intentionally
        does not create tables or publish rows; publishing remains an explicit
        caller-owned operation through :class:`DateUniversePublisher`.
        """

        safe_database = str(database or "stock").strip()
        if not safe_database.replace("_", "").isalnum():
            raise DateUniverseContractError("invalid ClickHouse database name")

        def quote(value: object) -> str:
            return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

        def as_dicts(result: Any) -> list[dict[str, Any]]:
            names = list(getattr(result, "column_names", ()) or ())
            return [dict(zip(names, row)) for row in getattr(result, "result_rows", ())]

        def registry_loader(codes: Sequence[str]) -> Iterable[Mapping[str, Any]]:
            if not codes:
                return ()
            code_sql = ", ".join(quote(code) for code in codes)
            result = client.query(
                f"""
                SELECT dataset_id, code, completeness_state, date_set_sha256, is_active
                FROM {safe_database}.daily_date_universe_registry FINAL
                WHERE code IN ({code_sql})
                """,
                settings={"readonly": 1},
            )
            return as_dicts(result)

        def member_loader(
            codes: Sequence[str], keys: Sequence[tuple[str, date]]
        ) -> Iterable[Mapping[str, Any]]:
            if not keys:
                return ()
            key_sql = ", ".join(
                f"({quote(code)}, toDate({quote(trade_date.isoformat())}))"
                for code, trade_date in keys
            )
            result = client.query(
                f"""
                SELECT dataset_id, code, trade_date, date_set_sha256
                FROM {safe_database}.daily_date_universe_member
                WHERE (code, trade_date) IN ({key_sql})
                """,
                settings={"readonly": 1},
            )
            return as_dicts(result)

        return cls(registry_loader=registry_loader, member_loader=member_loader)

    def exact_key_permit(
        self, keys: Iterable[tuple[object, object]], *, source: str
    ) -> DailyExactKeyPermit:
        requested = tuple(
            (normalize_code(code), normalize_trade_date(trade_date))
            for code, trade_date in keys
        )
        codes = sorted({code for code, _ in requested})
        registry_rows = tuple(self._registry_loader(codes))
        member_rows = tuple(self._member_loader(codes, list(requested)))
        return build_exact_key_permit_from_rows(
            requested,
            registry_rows=registry_rows,
            member_rows=member_rows,
            source=source,
        )
