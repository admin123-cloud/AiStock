"""Offline contracts for the DDL-marker database write lock."""

from __future__ import annotations

import re
from ast import literal_eval
from datetime import datetime, timedelta

import pytest

from utils.db_write_lock import (
    DEFAULT_TTL_SECONDS,
    DbWriteLock,
    LockError,
    LockHeldError,
    LockOwnerMismatchError,
    lock_table,
    safe_scope,
)

SCOPE = "kline_daily_schema_migration"
LOCK_TABLE = f"stock.write_lock_{SCOPE}"
NOW = datetime(2026, 11, 1, 12, 0, 0)
LOCK_FIELDS = ("scope", "owner", "purpose", "acquired_at", "host", "pid")


def _clock(offset_seconds: float = 0.0):
    return lambda: NOW + timedelta(seconds=offset_seconds)


def _holder_row(owner: str = "other-owner", *, acquired_at: str = "2026-11-01 11:59:00", purpose: str = "nightly rebuild"):
    return {
        "scope": SCOPE,
        "owner": owner,
        "purpose": purpose,
        "acquired_at": acquired_at,
        "host": "worker-1",
        "pid": 111,
    }


class _Result:
    def __init__(self, names, rows):
        self.column_names = names
        self.result_rows = rows


class _FakeClient:
    """ClickHouse double that only accepts lock-marker verbs and tables."""

    def __init__(self, *, locks=None, lose_metadata_after_insert: bool = False):
        self.tables = {name: [dict(row) for row in rows] for name, rows in (locks or {}).items()}
        self.lose_metadata_after_insert = lose_metadata_after_insert
        self.calls = []

    def command(self, sql):
        self._guard(sql)
        self.calls.append(("command", sql))
        statement = " ".join(sql.split())
        if statement.startswith("CREATE TABLE "):
            name = statement.split()[2]
            if name in self.tables:
                raise RuntimeError(f"Table {name} already exists")
            self.tables[name] = []
            return None
        if statement.startswith("INSERT INTO "):
            match = re.match(r"INSERT INTO (\S+) \(([^)]*)\) VALUES \((.*)\)$", statement)
            assert match, statement
            fields = [field.strip() for field in match.group(2).split(",")]
            values = literal_eval(f"({match.group(3)})")
            self.tables[match.group(1)].append(dict(zip(fields, values)))
            if self.lose_metadata_after_insert:
                self.tables[match.group(1)] = []
            return None
        if statement.startswith("DROP TABLE "):
            name = statement.split()[2]
            if name not in self.tables:
                raise RuntimeError(f"unknown table {name}")
            del self.tables[name]
            return None
        raise AssertionError(f"unexpected command: {statement}")

    def query(self, sql, settings=None):
        self._guard(sql)
        self.calls.append(("query", sql))
        statement = " ".join(sql.split())
        if "count() AS table_count" in statement:
            match = re.search(r"name='([^']+)'", statement)
            exists = bool(match) and f"stock.{match.group(1)}" in self.tables
            return _Result(["table_count"], [(1 if exists else 0,)])
        match = re.search(r"FROM (stock\.write_lock_[a-z0-9_]+)", statement)
        if match:
            rows = sorted(self.tables.get(match.group(1), []), key=lambda row: row["acquired_at"], reverse=True)[:1]
            return _Result(list(LOCK_FIELDS), [tuple(row[field] for field in LOCK_FIELDS) for row in rows])
        raise AssertionError(f"unexpected query: {statement}")

    def _guard(self, sql):
        statement = " ".join(sql.split())
        verb = statement.split()[0]
        assert verb in {"CREATE", "INSERT", "SELECT", "DROP", "RENAME"}, statement
        for token in re.findall(r"\b(?:stock|system)\.[A-Za-z0-9_]+", statement):
            assert token.startswith("stock.write_lock_") or token == "system.tables", token

    def commands(self):
        return [sql for kind, sql in self.calls if kind == "command"]

    def verbs(self):
        return [sql.split()[0] for sql in self.commands()]


def test_safe_scope_rejects_injection_and_non_canonical_scopes():
    for bad in ("kline; DROP TABLE stock.kline_daily", "Kline-Daily", "", "a b", "../escape", "锁", "scope--x"):
        with pytest.raises(ValueError):
            safe_scope(bad)
    assert safe_scope(SCOPE) == SCOPE
    assert lock_table(SCOPE) == LOCK_TABLE


def test_acquire_creates_marker_inserts_metadata_and_confirms_readback():
    client = _FakeClient()
    lock = DbWriteLock(client, clock=_clock())

    info = lock.acquire(SCOPE, "owner-a", "rebuild candidate")

    assert info["scope"] == SCOPE
    assert info["table"] == LOCK_TABLE
    assert info["owner"] == "owner-a"
    metadata = info["metadata"]
    assert metadata["scope"] == SCOPE
    assert metadata["owner"] == "owner-a"
    assert metadata["purpose"] == "rebuild candidate"
    assert metadata["acquired_at"] == "2026-11-01 12:00:00"
    assert metadata["host"]
    assert int(metadata["pid"]) > 0

    commands = client.commands()
    assert [sql.split()[0] for sql in commands] == ["CREATE", "INSERT"]
    assert commands[0].startswith(f"CREATE TABLE {LOCK_TABLE} (")
    assert commands[1].startswith(f"INSERT INTO {LOCK_TABLE} (scope, owner, purpose, acquired_at, host, pid) VALUES")
    assert any(
        kind == "query" and f"FROM {LOCK_TABLE}" in sql
        for kind, sql in client.calls
    )


def test_acquire_on_held_lock_reports_holder_without_mutations():
    client = _FakeClient(locks={LOCK_TABLE: [_holder_row("nightly-job")]})
    lock = DbWriteLock(client, clock=_clock())

    with pytest.raises(LockHeldError) as excinfo:
        lock.acquire(SCOPE, "owner-b", "attempt")

    error = excinfo.value
    assert error.table == LOCK_TABLE
    assert error.scope == SCOPE
    assert error.holder["owner"] == "nightly-job"
    assert error.holder["purpose"] == "nightly rebuild"
    assert error.holder["acquired_at"] == "2026-11-01 11:59:00"
    assert error.report["stale"] is False
    assert "nightly-job" in str(error)
    assert client.verbs() == ["CREATE"]  # only the losing create attempt
    assert client.tables[LOCK_TABLE][0]["owner"] == "nightly-job"  # untouched


def test_acquire_drops_marker_when_readback_cannot_confirm_owner():
    client = _FakeClient(lose_metadata_after_insert=True)
    lock = DbWriteLock(client, clock=_clock())

    with pytest.raises(LockError):
        lock.acquire(SCOPE, "owner-a", "purpose")

    assert LOCK_TABLE not in client.tables
    assert client.verbs() == ["CREATE", "INSERT", "DROP"]


def test_release_validates_owner_before_drop():
    client = _FakeClient(locks={LOCK_TABLE: [_holder_row("owner-a")]})
    lock = DbWriteLock(client, clock=_clock())

    with pytest.raises(LockOwnerMismatchError):
        lock.release(SCOPE, "owner-b")
    assert client.tables[LOCK_TABLE][0]["owner"] == "owner-a"

    released = lock.release(SCOPE, "owner-a")
    assert released["released"] == "owner-a"
    assert LOCK_TABLE not in client.tables
    assert client.verbs() == ["DROP"]


def test_release_without_lock_is_a_noop():
    client = _FakeClient()
    lock = DbWriteLock(client, clock=_clock())

    assert lock.release(SCOPE, "owner-a") is None
    assert client.commands() == []


def test_inspect_reports_fresh_stale_and_unheld_states():
    stale = _FakeClient(locks={LOCK_TABLE: [_holder_row(acquired_at="2026-11-01 06:00:00")]})
    fresh = _FakeClient(locks={LOCK_TABLE: [_holder_row(acquired_at="2026-11-01 11:58:00")]})

    report = DbWriteLock(stale, clock=_clock(), default_ttl_seconds=3600).inspect(SCOPE)
    assert report["held"] is True
    assert report["stale"] is True
    assert report["age_seconds"] >= 21599
    assert report["ttl_seconds"] == 3600
    assert report["holder"]["owner"] == "other-owner"

    assert DbWriteLock(fresh, clock=_clock(), default_ttl_seconds=3600).inspect(SCOPE)["stale"] is False
    assert DbWriteLock(_FakeClient(), clock=_clock()).inspect(SCOPE)["held"] is False


def test_stale_lock_requires_force_and_journals_the_break():
    client = _FakeClient(locks={LOCK_TABLE: [_holder_row("nightly-job", acquired_at="2026-11-01 06:00:00")]})
    events = []
    lock = DbWriteLock(client, clock=_clock(), journal=events.append, default_ttl_seconds=3600)

    with pytest.raises(LockHeldError) as excinfo:
        lock.acquire(SCOPE, "owner-b", "attempt")
    assert excinfo.value.report["stale"] is True
    assert excinfo.value.report["holder"]["owner"] == "nightly-job"

    info = lock.acquire(SCOPE, "owner-b", "retry", force=True)
    assert info["owner"] == "owner-b"
    assert client.tables[LOCK_TABLE][0]["owner"] == "owner-b"
    assert client.verbs() == ["CREATE", "CREATE", "DROP", "CREATE", "INSERT"]
    assert events[0]["event"] == "write_lock_stale_broken"
    assert events[0]["previous_holder"]["owner"] == "nightly-job"
    assert events[0]["forced_by"] == "owner-b"
    assert events[0]["ttl_seconds"] == 3600


def test_force_cannot_break_a_fresh_lock():
    client = _FakeClient(locks={LOCK_TABLE: [_holder_row("nightly-job", acquired_at="2026-11-01 11:59:30")]})
    lock = DbWriteLock(client, clock=_clock(), default_ttl_seconds=3600)

    with pytest.raises(LockHeldError):
        lock.acquire(SCOPE, "owner-b", "attempt", force=True)

    assert client.tables[LOCK_TABLE][0]["owner"] == "nightly-job"
    assert "DROP" not in client.verbs()


def test_orphan_marker_table_without_metadata_requires_force():
    client = _FakeClient(locks={LOCK_TABLE: []})
    lock = DbWriteLock(client, clock=_clock())

    with pytest.raises(LockHeldError) as excinfo:
        lock.acquire(SCOPE, "owner-b", "attempt")
    assert excinfo.value.holder is None

    events = []
    lock = DbWriteLock(client, clock=_clock(), journal=events.append)
    assert lock.acquire(SCOPE, "owner-b", "attempt", force=True)["owner"] == "owner-b"
    assert events[0]["event"] == "write_lock_orphan_broken"


def test_release_orphan_marker_requires_force():
    client = _FakeClient(locks={LOCK_TABLE: []})
    lock = DbWriteLock(client, clock=_clock())

    with pytest.raises(LockError):
        lock.release(SCOPE, "owner-a")
    assert LOCK_TABLE in client.tables

    released = lock.release(SCOPE, "owner-a", force=True)
    assert released["orphan"] is True
    assert LOCK_TABLE not in client.tables


def test_all_executed_sql_stays_within_the_lock_verb_and_table_whitelist():
    client = _FakeClient(locks={LOCK_TABLE: [_holder_row("nightly-job", acquired_at="2026-11-01 06:00:00")]})
    lock = DbWriteLock(client, clock=_clock(), default_ttl_seconds=600)

    with pytest.raises(LockHeldError):
        lock.acquire(SCOPE, "owner-b")
    lock.acquire(SCOPE, "owner-b", force=True)
    lock.release(SCOPE, "owner-b")

    assert client.calls  # the fake guard asserted verb/table legality on every call


def test_default_ttl_constant_is_positive_for_migration_use():
    assert DEFAULT_TTL_SECONDS > 0
