# ---------------------------------------------------------------------------
# Database-scoped distributed apply lock (stock.daily_backfill_distributed_lock)
# ---------------------------------------------------------------------------
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from utils.daily_date_universe import (
    BUSINESS_TZ as _BUSINESS_TZ,
    DateUniverseContractError,
)

import utils.tdx_daily_backfill as _backfill

class _FakeLockClickHouse:
    """Emulates the tiny SQL subset the distributed lock provider issues."""

    BASE = datetime(2026, 11, 1, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def __init__(self, *, holder="", expires=None):
        self.row = {
            "lock_name": "tdx_daily_backfill",
            "holder_id": holder,
            "acquired_at": self.BASE,
            "heartbeat_at": self.BASE,
            "expires_at": self.BASE if expires is None else expires,
            "note": "",
        }
        self.table_exists = False
        self.commands = []

    def close(self):
        return None

    # -- SQL emulation --------------------------------------------------
    def command(self, sql, settings=None):
        self.commands.append(sql)
        if sql.startswith("CREATE TABLE"):
            self.table_exists = True
            return
        if sql.startswith("INSERT INTO"):
            return
        match = re.fullmatch(
            r"ALTER TABLE \S+ UPDATE (.+) WHERE (.+) SETTINGS mutations_sync=1", sql, re.S
        )
        assert match, f"unexpected lock SQL: {sql}"
        if self._predicate_holds(match.group(2)):
            self._apply(match.group(1))

    @staticmethod
    def _parse_datetime(literal):
        value = datetime.fromisoformat(literal)
        return value.replace(tzinfo=_BUSINESS_TZ) if value.tzinfo is None else value

    def _predicate_holds(self, predicate):
        prefix = "lock_name='tdx_daily_backfill' AND "
        predicate = " ".join(predicate.split())
        assert predicate.startswith(prefix), predicate
        rest = predicate[len(prefix):]
        match = re.fullmatch(r"\(holder_id='' OR expires_at < toDateTime64\('([^']+)'[^)]*\)\)", rest)
        if match:
            now = self._parse_datetime(match.group(1))
            expires = self.row["expires_at"]
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=_BUSINESS_TZ)
            return self.row["holder_id"] == "" or expires < now
        match = re.fullmatch(r"holder_id='([^']*)'", rest)
        if match:
            return self.row["holder_id"] == match.group(1)
        raise AssertionError(f"unhandled lock predicate: {rest}")

    def _apply(self, assignments):
        for match in re.finditer(
            r"(\w+)=('([^']*)'|toDateTime64\('([^']*)'[^)]*\)|(\w+))", assignments
        ):
            field = match.group(1)
            if match.group(3) is not None:
                self.row[field] = match.group(3)
            elif match.group(4) is not None:
                value = self._parse_datetime(match.group(4))
                self.row[field] = (
                    value.replace(tzinfo=_BUSINESS_TZ) if value.tzinfo is None else value
                )
            # bare identifier (identity assignment note=note) is a no-op

    def query(self, sql, settings=None):
        sql_flat = " ".join(sql.split())
        if sql_flat.startswith("SELECT count() FROM system.tables"):
            rows = [(1 if self.table_exists else 0,)]
            return SimpleNamespace(column_names=["count()"], result_rows=rows)
        if sql_flat.startswith("SELECT currentUser()"):
            return SimpleNamespace(column_names=["user", "readonly"], result_rows=[("default", 0)])
        if sql_flat.startswith("SELECT count() FROM stock.daily_backfill_distributed_lock"):
            return SimpleNamespace(column_names=["count()"], result_rows=[(1,)])
        if sql_flat.startswith("SELECT lock_name, holder_id"):
            columns = ["lock_name", "holder_id", "acquired_at", "heartbeat_at", "expires_at", "note"]
        elif sql_flat.startswith("SELECT holder_id, expires_at"):
            columns = ["holder_id", "expires_at"]
        else:
            raise AssertionError(f"unexpected lock query: {sql_flat}")
        return SimpleNamespace(
            column_names=columns, result_rows=[tuple(self.row[name] for name in columns)]
        )


@pytest.fixture
def _clear_held_distributed_lock():
    _backfill._HELD_DISTRIBUTED_LOCK = None
    yield
    _backfill._HELD_DISTRIBUTED_LOCK = None


def _fixed_clock(start=_FakeLockClickHouse.BASE):
    state = {"now": start}
    return state, (lambda: state["now"])


def test_distributed_lock_acquire_require_heartbeat_release_roundtrip(_clear_held_distributed_lock):
    fake = _FakeLockClickHouse()
    clock, now_fn = _fixed_clock()
    lock = _backfill.acquire_tdx_daily_distributed_lock(
        fake, holder_id="runner-a", ttl_seconds=600, heartbeat_interval_seconds=60, now_fn=now_fn
    )
    assert fake.row["holder_id"] == "runner-a"
    assert fake.table_exists is True

    _backfill.require_tdx_daily_distributed_lock()  # fresh heartbeat, must not raise

    clock["now"] = clock["now"] + timedelta(seconds=120)
    _backfill.require_tdx_daily_distributed_lock()  # renews the lease
    assert fake.row["heartbeat_at"] == clock["now"]
    assert fake.row["expires_at"] == clock["now"] + timedelta(seconds=600)

    lock.release()
    assert fake.row["holder_id"] == ""
    with pytest.raises(DateUniverseContractError, match="distributed apply lock"):
        _backfill.require_tdx_daily_distributed_lock()


def test_distributed_lock_rejects_a_second_claimant_while_held(_clear_held_distributed_lock):
    fake = _FakeLockClickHouse(
        holder="other-runner", expires=_FakeLockClickHouse.BASE + timedelta(hours=1)
    )
    _, now_fn = _fixed_clock(_FakeLockClickHouse.BASE)
    with pytest.raises(DateUniverseContractError, match="already held by another writer"):
        _backfill.acquire_tdx_daily_distributed_lock(fake, holder_id="runner-b", now_fn=now_fn)
    assert fake.row["holder_id"] == "other-runner"
    assert _backfill._HELD_DISTRIBUTED_LOCK is None


def test_distributed_lock_steals_an_expired_lease(_clear_held_distributed_lock):
    fake = _FakeLockClickHouse(holder="dead-runner", expires=_FakeLockClickHouse.BASE)
    clock, now_fn = _fixed_clock(_FakeLockClickHouse.BASE + timedelta(hours=2))
    lock = _backfill.acquire_tdx_daily_distributed_lock(
        fake, holder_id="runner-c", ttl_seconds=600, now_fn=now_fn
    )
    assert fake.row["holder_id"] == "runner-c"
    lock.release()


def test_require_detects_a_taken_over_or_expired_lock_row(_clear_held_distributed_lock):
    fake = _FakeLockClickHouse()
    clock, now_fn = _fixed_clock()
    _backfill.acquire_tdx_daily_distributed_lock(
        fake, holder_id="runner-d", ttl_seconds=600, heartbeat_interval_seconds=60, now_fn=now_fn
    )
    fake.row["holder_id"] = "intruder"
    with pytest.raises(DateUniverseContractError, match="no longer owned"):
        _backfill.require_tdx_daily_distributed_lock()

    fake.row["holder_id"] = "runner-d"
    fake.row["expires_at"] = clock["now"] - timedelta(seconds=1)
    with pytest.raises(DateUniverseContractError, match="lease expired"):
        _backfill.require_tdx_daily_distributed_lock()


def test_distributed_lock_provider_verify_never_claims_the_lock(_clear_held_distributed_lock):
    fake = _FakeLockClickHouse(
        holder="live-runner", expires=_FakeLockClickHouse.BASE + timedelta(hours=1)
    )
    fake.table_exists = True
    info = _backfill.verify_tdx_daily_distributed_lock_provider(fake)
    assert info["available"] is True
    assert info["status"] == "held"
    assert info["current_holder"] == "live-runner"
    # the probe is an identity mutation guarded by holder_id='' — nothing changed
    assert fake.row["holder_id"] == "live-runner"

    missing = _FakeLockClickHouse()
    info = _backfill.verify_tdx_daily_distributed_lock_provider(missing)
    assert info["available"] is False
    assert info["status"] == "not_provisioned"
