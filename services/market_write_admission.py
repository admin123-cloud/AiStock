"""Offline attempt accounting; no driver, permit, phase policy or fencing.

A journal must provide durable atomic append/CAS in a real integration. An
issued intent may or may not have been dispatched after a crash. Neither a
query id nor a successful response proves exactly-once or verified execution.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from enum import Enum
from typing import Callable, Optional, Protocol


class AttemptState(str, Enum):
    PLANNED = 'planned'
    ISSUED = 'issued'
    ACKNOWLEDGED = 'acknowledged'
    VERIFIED = 'verified'
    UNKNOWN = 'unknown'
    FAILED = 'failed'
    CANCELLED_VERIFIED = 'cancelled_verified'


@dataclass(frozen=True)
class WriteAttempt:
    D: date
    plan_id: str
    generation: int
    writer_id: str
    purpose: str
    attempt_id: str
    table: str
    operation: str
    key_digest: str
    payload_digest: str
    query_id: str
    issued_at: Optional[datetime] = None
    ack_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    mutation_id: Optional[str] = None
    state: AttemptState = AttemptState.PLANNED

    def __post_init__(self):
        if type(self.D) is not date or type(self.generation) is not int or self.generation < 1:
            raise ValueError('invalid date or generation')
        for field in ('plan_id', 'writer_id', 'purpose', 'attempt_id', 'table',
                      'operation', 'key_digest', 'payload_digest', 'query_id'):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError('invalid ' + field)
        if not isinstance(self.state, AttemptState):
            raise ValueError('invalid state')
        for field in ('issued_at', 'ack_at', 'verified_at'):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
                raise ValueError('timestamp must be aware')


class JournalSink(Protocol):
    """Append must be durable before returning; atomically enforce transitions."""
    def append(self, attempt: WriteAttempt) -> None: ...
    def get(self, attempt_id: str) -> WriteAttempt: ...
    def unresolved(self, D: date, plan_id: str, generation: int): ...


class WriteAdmission:
    """Bookkeeping wrapper only, not an authorization or exactly-once gate.

    The injected sender must be a controlled gateway. The caller/integration
    must separately enforce phase, permit, payload identity and fencing.
    """
    def __init__(self, journal: JournalSink, lock, sender: Callable, *, now=None):
        self._journal = journal
        self._lock = lock
        self._sender = sender
        self._now = now or (lambda: datetime.now(timezone.utc))

    def unresolved(self, D, plan_id, generation):
        # A response is not independent terminal verification either.
        return tuple(replace(a, state=AttemptState.UNKNOWN)
                     if a.state in (AttemptState.PLANNED, AttemptState.ISSUED,
                                    AttemptState.ACKNOWLEDGED) else a
                     for a in self._journal.unresolved(D, plan_id, generation))

    def _assert_held(self, attempt):
        # Interface identity assertion; a dishonest adapter can still lie.
        target = getattr(self._sender, '__self__', None)
        self._lock.assert_held(endpoint=getattr(target, 'endpoint', None),
                               database=getattr(target, 'database', None),
                               plan_id=attempt.plan_id, generation=attempt.generation)

    def issue(self, attempt: WriteAttempt):
        if attempt.state is not AttemptState.PLANNED or any(
                x is not None for x in (attempt.issued_at, attempt.ack_at,
                                        attempt.verified_at, attempt.mutation_id)):
            raise ValueError('new attempt must be pristine planned')
        self._assert_held(attempt)
        self._journal.append(attempt)
        issued = replace(attempt, state=AttemptState.ISSUED, issued_at=self._now())
        self._journal.append(issued)
        # Fresh lease read, after journal I/O and immediately before dispatch.
        self._assert_held(attempt)
        try:
            result = self._sender(issued)
        except Exception:
            # Failure to record UNKNOWN must not hide the still-unresolved ISSUED.
            try:
                self._journal.append(replace(issued, state=AttemptState.UNKNOWN))
            except Exception:
                pass
            raise
        ack = replace(issued, state=AttemptState.ACKNOWLEDGED, ack_at=self._now(),
                      mutation_id=result)
        self._journal.append(ack)
        return ack
