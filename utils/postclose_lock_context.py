"""Injected lease context for offline preparation, not server-side fencing.

Provider contract (to be implemented and reviewed in a later slice): endpoint
and database identify the actual connected DB; acquire/heartbeat/release are
atomic conditional lease operations, read returns (table,name,holder,expiry)
with expiry on the injected process-monotonic clock. No table provisioning.
Heartbeat must atomically match the holder and require the old lease to remain
unexpired at the provider's authoritative time; it must never reclaim a lease.
This module cannot establish the provider's honesty or constrain other writers.
"""
import math
import time
from uuid import uuid4

LOCK_TABLE = 'stock.daily_backfill_distributed_lock'
LOCK_NAME = 'tdx_daily_backfill'
LEASE_TTL_SECONDS = 300
MAX_HOLD_SECONDS = 1800
_TOKEN = object()


class LockError(RuntimeError):
    pass


class LockContext:
    def __init__(self, token, provider, endpoint, database, plan_id, generation, clock, holder, start):
        if token is not _TOKEN:
            raise LockError('use acquire')
        self._provider = provider
        self._endpoint = endpoint
        self._database = database
        self._plan_id = plan_id
        self._generation = generation
        self._clock = clock
        self._holder = holder
        self._start = start
        self._last = start
        self._local_expiry = start + LEASE_TTL_SECONDS
        self._stopped = False
        self._released = False

    @property
    def endpoint(self):
        return self._endpoint

    @property
    def database(self):
        return self._database

    @property
    def released(self):
        return self._released

    @classmethod
    def acquire(cls, provider, *, endpoint, database, plan_id, generation, clock=time.monotonic):
        if (not isinstance(endpoint, str) or not endpoint.strip() or database != 'stock'
                or not isinstance(plan_id, str) or not plan_id.strip()
                or type(generation) is not int or generation < 1
                or provider.endpoint != endpoint or provider.database != database):
            raise LockError('invalid DB or plan identity')
        try:
            start = clock()
            if not isinstance(start, (float, int)) or not math.isfinite(start):
                raise ValueError('invalid clock')
            holder = uuid4().hex
            acquired = provider.acquire(lock_table=LOCK_TABLE, lock_name=LOCK_NAME,
                                        holder=holder, ttl=LEASE_TTL_SECONDS)
            if acquired is not True:
                raise LockError('lease not acquired')
            context = cls(_TOKEN, provider, endpoint, database, plan_id, generation, clock, holder, start)
            context.assert_held(endpoint=endpoint, database=database, plan_id=plan_id, generation=generation)
            return context
        except LockError:
            raise
        except Exception as exc:
            raise LockError('lease acquisition uncertain') from exc

    def _check_time(self):
        now = self._clock()
        if (not isinstance(now, (int, float)) or not math.isfinite(now)
                or now < self._last or now - self._start >= MAX_HOLD_SECONDS):
            raise LockError('clock or absolute holding budget invalid')
        self._last = now
        return now

    def assert_held(self, *, endpoint, database, plan_id, generation):
        if self._stopped or self._released:
            raise LockError('context stopped')
        try:
            if ((endpoint, database, plan_id, generation) !=
                    (self._endpoint, self._database, self._plan_id, self._generation)
                    or self._provider.endpoint != self._endpoint
                    or self._provider.database != self._database):
                raise LockError('identity mismatch')
            now = self._check_time()
            record = self._provider.read(lock_table=LOCK_TABLE, lock_name=LOCK_NAME)
            # Recheck budget after the provider call as well.
            now = max(now, self._check_time())
            if (not isinstance(record, tuple) or len(record) != 4
                    or record[:3] != (LOCK_TABLE, LOCK_NAME, self._holder)
                    or isinstance(record[3], bool)
                    or not isinstance(record[3], (int, float))
                    or not math.isfinite(record[3]) or record[3] <= now
                    or now >= self._local_expiry):
                raise LockError('lease readback invalid')
        except Exception as exc:
            self._stopped = True
            if isinstance(exc, LockError):
                raise
            raise LockError('lease assertion failed') from exc

    def heartbeat(self):
        self.assert_held(endpoint=self._endpoint, database=self._database,
                         plan_id=self._plan_id, generation=self._generation)
        try:
            renewal_started = self._check_time()
            if renewal_started >= self._local_expiry:
                raise LockError('lease expired before heartbeat')
            if self._provider.heartbeat(lock_table=LOCK_TABLE, lock_name=LOCK_NAME,
                                        holder=self._holder, ttl=LEASE_TTL_SECONDS) is not True:
                raise LockError('heartbeat failed')
            self._local_expiry = renewal_started + LEASE_TTL_SECONDS
            self.assert_held(endpoint=self._endpoint, database=self._database,
                             plan_id=self._plan_id, generation=self._generation)
        except Exception as exc:
            self._stopped = True
            if isinstance(exc, LockError):
                raise
            raise LockError('heartbeat uncertain') from exc

    def release(self):
        # Release is deliberately callable even after a stop; integration must
        # first resolve outstanding writes and approve release as appropriate.
        if self._released:
            raise LockError('already released')
        self._stopped = True
        try:
            if self._provider.release(lock_table=LOCK_TABLE, lock_name=LOCK_NAME,
                                      holder=self._holder) is not True:
                raise LockError('release not confirmed')
            if self._provider.read(lock_table=LOCK_TABLE, lock_name=LOCK_NAME) is not None:
                raise LockError('release readback uncertain')
            self._released = True
        except Exception as exc:
            if isinstance(exc, LockError):
                raise
            raise LockError('release uncertain') from exc
