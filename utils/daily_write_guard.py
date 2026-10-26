"""Shared scope contract for formal ``kline_daily`` writes.

Normal daily writes are limited to the active stock universe and the eleven
canonical indices. Historical or delisted stocks may be written only by an
explicit, caller-owned backfill permit. Callers that do not pass a permit
remain fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Iterable, Optional

from utils.daily_date_universe import (
    DailyExactKeyPermit,
    DailyDateUniverseReader,
    DateUniverseContractError,
    normalize_code,
    normalize_trade_date,
)


CORE_DAILY_INDEX_CODES = frozenset(
    {
        "000001.SH", "399001.SZ", "399006.SZ", "399106.SZ", "000985.SH",
        "000680.SH", "000300.SH", "000016.SH", "000905.SH", "000852.SH",
        "000688.SH",
    }
)


class DailyWriteBlockedError(RuntimeError):
    """Raised before a formal daily mutation when its scope is not allowed."""


@dataclass(frozen=True)
class DailyWriteScope:
    """Immutable permit carried by one formal daily write operation."""

    source: str
    current_stock_codes: frozenset[str] = field(default_factory=frozenset)
    historical_stock_codes: frozenset[str] = field(default_factory=frozenset)
    core_index_codes: frozenset[str] = CORE_DAILY_INDEX_CODES
    exact_key_permit: Optional[DailyExactKeyPermit] = None

    def __post_init__(self) -> None:
        for name in ("current_stock_codes", "historical_stock_codes", "core_index_codes"):
            values = getattr(self, name)
            object.__setattr__(
                self,
                name,
                frozenset(normalize_code(item) for item in values if normalize_code(item)),
            )

    @property
    def allowed_codes(self) -> frozenset[str]:
        return self.current_stock_codes | self.historical_stock_codes | self.core_index_codes


def block_unreviewed_daily_write(*, operation: str, source: str) -> None:
    """Stop a legacy formal daily mutation before any database call."""
    raise DailyWriteBlockedError(
        "blocked formal kline_daily "
        f"{operation} from {source}: a DailyWriteScope is required; "
        "only current stocks, the 11 core indices, or an explicit historical-stock permit may write"
    )


def make_daily_write_scope(
    *,
    source: str,
    current_stock_codes: Optional[Iterable[object]] = None,
    historical_stock_codes: Optional[Iterable[object]] = None,
    exact_key_permit: Optional[DailyExactKeyPermit] = None,
) -> DailyWriteScope:
    """Create a scope from explicit codes and an optional exact-key permit.

    A scope without ``exact_key_permit`` is useful for code classification but
    cannot pass a formal daily INSERT/DELETE validation.  This keeps existing
    callers fail-closed while allowing a future orchestration layer to attach a
    permit produced by ``DailyDateUniverseReader``.
    """
    if current_stock_codes is None:
        raise DailyWriteBlockedError(
            f"blocked formal kline_daily scope from {source}: current stock universe is missing"
        )
    return DailyWriteScope(
        source=source,
        current_stock_codes=frozenset(current_stock_codes),
        historical_stock_codes=frozenset(historical_stock_codes or ()),
        exact_key_permit=exact_key_permit,
    )


def load_current_stock_scope(
    *,
    source: str,
    historical_stock_codes: Optional[Iterable[object]] = None,
    client=None,
) -> DailyWriteScope:
    """Load the active current-stock universe from the canonical stocks table."""
    try:
        query = """
            SELECT code
            FROM stocks FINAL
            WHERE type = 'stock'
              AND listing_status = 'active'
              AND quit = 0
              AND (delist_date IS NULL OR delist_date >= today())
            """
        if client is None:
            from utils.market_warehouse import clickhouse_query_df

            frame = clickhouse_query_df(query)
            codes = [] if frame is None or frame.empty else frame["code"].tolist()
        else:
            result = client.query(query, settings={"readonly": 1})
            names = list(getattr(result, "column_names", ()) or ())
            rows = [dict(zip(names, row)) for row in getattr(result, "result_rows", ())]
            codes = [row.get("code") for row in rows]
    except Exception as exc:  # pragma: no cover - runtime failure path
        raise DailyWriteBlockedError(
            f"blocked formal kline_daily scope from {source}: cannot load current stock universe: {exc}"
        ) from exc
    return make_daily_write_scope(
        source=source,
        current_stock_codes=codes,
        historical_stock_codes=historical_stock_codes,
    )


def load_daily_write_scope_for_keys(
    keys: Iterable[tuple[object, object]],
    *,
    source: str,
    scope: Optional[DailyWriteScope] = None,
    client=None,
    historical_stock_codes: Optional[Iterable[object]] = None,
    database: str = "stock",
) -> DailyWriteScope:
    """Attach a read-only registry/member permit to a daily write scope.

    The scope keeps its caller-provided code classification.  When no scope is
    supplied, the active stock universe is read first from ``stocks``.  The
    exact keys are then proven by ``DailyDateUniverseReader`` before a writer
    can proceed.
    """
    try:
        requested = tuple(
            (normalize_code(code), normalize_trade_date(trade_date))
            for code, trade_date in keys
        )
    except DateUniverseContractError as exc:
        raise DailyWriteBlockedError(
            f"blocked formal kline_daily from {source}: invalid exact-key input: {exc}"
        ) from exc
    if not requested:
        return scope or make_daily_write_scope(
            source=source,
            current_stock_codes=(),
            historical_stock_codes=historical_stock_codes,
        )
    if scope is None:
        if client is None:
            from utils.market_warehouse import clickhouse_client

            client = clickhouse_client()
        scope = load_current_stock_scope(
            source=source,
            historical_stock_codes=historical_stock_codes,
            client=client,
        )
    if scope.exact_key_permit is not None:
        return scope
    if client is None:
        from utils.market_warehouse import clickhouse_client

        client = clickhouse_client()
    try:
        permit = DailyDateUniverseReader.from_clickhouse(
            client, database=database
        ).exact_key_permit(requested, source=source)
    except Exception as exc:
        raise DailyWriteBlockedError(
            f"blocked formal kline_daily from {source}: registry/member exact-key readback failed: {exc}"
        ) from exc
    return replace(scope, exact_key_permit=permit)


def ensure_daily_write_scope_for_dataframe(
    frame,
    *,
    source: str,
    scope: Optional[DailyWriteScope] = None,
    client=None,
    historical_stock_codes: Optional[Iterable[object]] = None,
    database: str = "stock",
) -> Optional[DailyWriteScope]:
    """Resolve a scope and exact-key permit for a daily DataFrame."""
    if frame is None or frame.empty:
        return scope
    date_column = (
        "trade_date"
        if "trade_date" in frame.columns
        else "date"
        if "date" in frame.columns
        else None
    )
    if "code" not in frame.columns or date_column is None:
        return scope
    keys = zip(frame["code"].tolist(), frame[date_column].tolist())
    return load_daily_write_scope_for_keys(
        keys,
        source=source,
        scope=scope,
        client=client,
        historical_stock_codes=historical_stock_codes,
        database=database,
    )


def validate_daily_codes(
    codes: Iterable[object], *, scope: Optional[DailyWriteScope], operation: str
) -> list[str]:
    """Validate and normalize every code before a formal mutation."""
    if scope is None:
        block_unreviewed_daily_write(operation=operation, source="unknown")
    normalized = [normalize_code(code) for code in codes if normalize_code(code)]
    blocked = sorted(set(normalized) - scope.allowed_codes)
    if blocked:
        suffix = "..." if len(blocked) > 20 else ""
        raise DailyWriteBlockedError(
            f"blocked formal kline_daily {operation} from {scope.source}: "
            f"codes outside fixed scope ({', '.join(blocked[:20])}{suffix})"
        )
    return normalized


def validate_daily_dataframe(frame, *, scope: Optional[DailyWriteScope], operation: str):
    """Validate a DataFrame and return a normalized copy before writing."""
    if frame is None or frame.empty:
        return frame
    if "code" not in frame.columns:
        raise DailyWriteBlockedError(
            f"blocked formal kline_daily {operation}: code column is required"
        )
    date_column = "trade_date" if "trade_date" in frame.columns else "date" if "date" in frame.columns else None
    if date_column is None:
        raise DailyWriteBlockedError(
            f"blocked formal kline_daily {operation}: trade_date/date column is required for exact-key validation"
        )
    validate_daily_keys(
        zip(frame["code"].tolist(), frame[date_column].tolist()),
        scope=scope,
        operation=operation,
    )
    out = frame.copy()
    out["code"] = out["code"].map(normalize_code)
    return out


def validate_daily_keys(
    keys: Iterable[tuple[object, object]],
    *,
    scope: Optional[DailyWriteScope],
    operation: str,
) -> list[tuple[str, object]]:
    """Validate exact ``(code, trade_date)`` keys before a formal mutation."""
    rows = list(keys)
    if not rows:
        return []
    if scope is None:
        block_unreviewed_daily_write(operation=operation, source="unknown")
    normalized: list[tuple[str, date]] = []
    for code, trade_date in rows:
        normalized.append((normalize_code(code), normalize_trade_date(trade_date)))
    validate_daily_codes((code for code, _ in normalized), scope=scope, operation=operation)
    if scope.exact_key_permit is None:
        raise DailyWriteBlockedError(
            f"blocked formal kline_daily {operation} from {scope.source}: "
            "an accepted date-universe exact-key permit is required"
        )
    try:
        scope.exact_key_permit.assert_allows(normalized, operation=operation)
    except DateUniverseContractError as exc:
        raise DailyWriteBlockedError(
            f"blocked formal kline_daily {operation} from {scope.source}: {exc}"
        ) from exc
    return normalized
