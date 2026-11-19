"""Fail-closed database write locks based on atomic DDL table markers.

The lock is the table itself: ``CREATE TABLE stock.write_lock_<scope>`` either
wins the name (lock acquired) or fails because the name already exists (lock
held by someone else).  A single metadata row inside the marker table records
who owns the lock; acquisition is only confirmed after the row is read back.

Stale locks (holder older than a TTL) are reported but never broken silently:
breaking one requires an explicit ``force`` and is recorded through the
injectable journal so the break leaves an audit trail.
"""

from __future__ import annotations

import os
import re
import socket
from datetime import datetime
from typing import Any, Callable, Optional

DEFAULT_DATABASE = "stock"
LOCK_TABLE_PREFIX = "write_lock_"
DEFAULT_TTL_SECONDS = 3600.0
LOCK_METADATA_FIELDS = ("scope", "owner", "purpose", "acquired_at", "host", "pid")

_SCOPE_PATTERN = re.compile(r"^[a-z0-9_]{1,60}$")
_READ_ONLY_SETTINGS = {"readonly": 1, "max_execution_time": 120}


class LockError(RuntimeError):
    """Base class for write-lock failures."""


class LockHeldError(LockError):
    """Raised when another owner holds the lock marker table."""

    def __init__(self, message: str, *, scope: str, table: str, report: dict[str, Any]):
        super().__init__(message)
        self.scope = scope
        self.table = table
        self.report = report

    @property
    def holder(self) -> Optional[dict[str, Any]]:
        return self.report.get("holder")


class LockOwnerMismatchError(LockError):
    """Raised when releasing a lock that belongs to a different owner."""

    def __init__(self, message: str, *, scope: str, table: str, holder: Optional[dict[str, Any]]):
        super().__init__(message)
        self.scope = scope
        self.table = table
        self.holder = holder


def safe_scope(scope: str) -> str:
    """Validate a lock scope so it can never inject SQL or path separators."""
    scope = str(scope or "")
    if not _SCOPE_PATTERN.fullmatch(scope):
        raise ValueError(f"unsafe lock scope: {scope!r}")
    return scope


def lock_table(scope: str, database: str = DEFAULT_DATABASE) -> str:
    return f"{database}.{LOCK_TABLE_PREFIX}{safe_scope(scope)}"


def _quote(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dict_rows(result: Any) -> list[dict[str, Any]]:
    names = list(getattr(result, "column_names", ()) or ())
    return [dict(zip(names, row)) for row in getattr(result, "result_rows", ()) or ()]


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def create_marker_table_sql(table: str) -> str:
    return (
        f"CREATE TABLE {table} "
        "(scope String, owner String, purpose String, acquired_at DateTime, host String, pid UInt64) "
        "ENGINE = MergeTree ORDER BY scope"
    )


def insert_marker_sql(table: str, metadata: dict[str, Any]) -> str:
    columns = ", ".join(LOCK_METADATA_FIELDS)
    values = ", ".join(
        str(int(metadata["pid"])) if field == "pid" else _quote(metadata[field])
        for field in LOCK_METADATA_FIELDS
    )
    return f"INSERT INTO {table} ({columns}) VALUES ({values})"


def select_marker_sql(table: str) -> str:
    return (
        "SELECT scope, owner, purpose, toString(acquired_at) AS acquired_at, host, pid "
        f"FROM {table} ORDER BY acquired_at DESC LIMIT 1"
    )


def table_exists_sql(table: str, database: str = DEFAULT_DATABASE) -> str:
    return (
        "SELECT count() AS table_count FROM system.tables "
        f"WHERE database={_quote(database)} AND name={_quote(table.split('.')[-1])}"
    )


class DbWriteLock:
    """Guard serialised database writers with atomic DDL marker tables."""

    def __init__(
        self,
        client: Any,
        *,
        database: str = DEFAULT_DATABASE,
        journal: Optional[Callable[[dict], None]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        default_ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.client = client
        self.database = database
        self.journal = journal
        self.clock = clock or datetime.now
        self.default_ttl_seconds = float(default_ttl_seconds)

    # ------------------------------------------------------------------ API
    def acquire(
        self,
        scope: str,
        owner: str,
        purpose: str = "",
        *,
        force: bool = False,
        ttl_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        scope = safe_scope(scope)
        table = lock_table(scope, self.database)
        ttl = self.default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        metadata = {
            "scope": scope,
            "owner": str(owner),
            "purpose": str(purpose or ""),
            "acquired_at": self.clock().strftime("%Y-%m-%d %H:%M:%S"),
            "host": socket.gethostname(),
            "pid": os.getpid(),
        }
        try:
            self.client.command(create_marker_table_sql(table))
        except Exception as exc:
            self._handle_existing_marker(scope, table, metadata, ttl, force, exc)
        return self._write_marker(table, metadata)

    def release(self, scope: str, owner: str, *, force: bool = False) -> Optional[dict[str, Any]]:
        scope = safe_scope(scope)
        table = lock_table(scope, self.database)
        holder = self._read_holder(table)
        if holder is None:
            if not self._table_exists(table):
                return None
            if not force:
                raise LockError(
                    f"write lock {table} exists without a metadata row; refusing to release without force"
                )
            self._journal_event(
                {"event": "write_lock_orphan_broken", "scope": scope, "table": table, "forced_by": str(owner)}
            )
            self.client.command(f"DROP TABLE {table}")
            return {"scope": scope, "table": table, "released": None, "holder": None, "orphan": True}
        if str(holder.get("owner")) != str(owner):
            raise LockOwnerMismatchError(
                f"write lock {table} is owned by {holder.get('owner')!r}, not {owner!r}",
                scope=scope,
                table=table,
                holder=holder,
            )
        self.client.command(f"DROP TABLE {table}")
        return {"scope": scope, "table": table, "released": str(owner), "holder": holder, "orphan": False}

    def inspect(self, scope: str, *, ttl_seconds: Optional[float] = None) -> dict[str, Any]:
        scope = safe_scope(scope)
        table = lock_table(scope, self.database)
        ttl = self.default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        if not self._table_exists(table):
            return {
                "scope": scope,
                "table": table,
                "held": False,
                "holder": None,
                "stale": False,
                "age_seconds": None,
                "ttl_seconds": ttl,
                "orphan": False,
            }
        holder = self._read_holder(table)
        report = self._stale_report(holder, ttl)
        return {"scope": scope, "table": table, "held": True, **report, "orphan": holder is None}

    # ------------------------------------------------------------ internals
    def _handle_existing_marker(
        self,
        scope: str,
        table: str,
        metadata: dict[str, Any],
        ttl: float,
        force: bool,
        exc: Exception,
    ) -> None:
        if not self._table_exists(table):
            raise LockError(
                f"write lock {table} could not be created: {type(exc).__name__}: {exc}"
            ) from exc
        holder = self._read_holder(table)
        report = self._stale_report(holder, ttl)
        breakable = holder is None or report["stale"]
        if not (force and breakable):
            message = (
                f"write lock {table} is held by "
                f"{holder.get('owner') if holder else '<missing metadata row>'}"
            )
            raise LockHeldError(message, scope=scope, table=table, report=report) from exc
        self._journal_event(
            {
                "event": "write_lock_orphan_broken" if holder is None else "write_lock_stale_broken",
                "scope": scope,
                "table": table,
                "previous_holder": holder,
                "age_seconds": report.get("age_seconds"),
                "ttl_seconds": ttl,
                "forced_by": metadata["owner"],
            }
        )
        try:
            self.client.command(f"DROP TABLE {table}")
            self.client.command(create_marker_table_sql(table))
        except Exception as recreate_exc:
            raise LockError(
                f"write lock {table} could not be re-created after a forced break: "
                f"{type(recreate_exc).__name__}: {recreate_exc}"
            ) from recreate_exc

    def _write_marker(self, table: str, metadata: dict[str, Any]) -> dict[str, Any]:
        try:
            self.client.command(insert_marker_sql(table, metadata))
            confirmed = self._read_holder(table)
        except Exception as exc:
            raise LockError(
                f"write lock {table} metadata write failed: {type(exc).__name__}: {exc}"
            ) from exc
        if (
            confirmed is None
            or str(confirmed.get("owner")) != metadata["owner"]
            or str(confirmed.get("scope")) != metadata["scope"]
        ):
            try:
                self.client.command(f"DROP TABLE {table}")
            except Exception:
                pass
            raise LockError(f"write lock {table} readback did not confirm owner {metadata['owner']!r}")
        return {
            "scope": metadata["scope"],
            "table": table,
            "owner": metadata["owner"],
            "metadata": dict(confirmed),
        }

    def _table_exists(self, table: str) -> bool:
        records = _dict_rows(
            self.client.query(table_exists_sql(table, self.database), settings=_READ_ONLY_SETTINGS)
        )
        return bool(records and int(records[0].get("table_count") or 0) > 0)

    def _read_holder(self, table: str) -> Optional[dict[str, Any]]:
        try:
            records = _dict_rows(self.client.query(select_marker_sql(table), settings=_READ_ONLY_SETTINGS))
        except Exception:
            return None
        if not records:
            return None
        row = records[0]
        return {field: row.get(field) for field in LOCK_METADATA_FIELDS}

    def _stale_report(self, holder: Optional[dict[str, Any]], ttl: float) -> dict[str, Any]:
        if holder is None:
            return {"holder": None, "stale": False, "age_seconds": None, "ttl_seconds": ttl}
        acquired = _parse_datetime(holder.get("acquired_at"))
        age = max((self.clock() - acquired).total_seconds(), 0.0) if acquired is not None else None
        return {"holder": holder, "stale": age is not None and age > ttl, "age_seconds": age, "ttl_seconds": ttl}

    def _journal_event(self, event: dict[str, Any]) -> None:
        if self.journal is None:
            return
        try:
            self.journal(dict(event))
        except Exception:
            pass  # journaling must never break locking semantics
