"""Injectable attempt journal contract and in-memory test implementation.

MemoryJournal is neither durable nor suitable for production. A production sink
must persist append atomically, reject duplicate attempt IDs, and implement
compare-and-swap across writers; it must not silently discard unresolved work.
"""
from dataclasses import fields
from threading import RLock
from typing import Protocol

from services.market_write_admission import AttemptState, WriteAttempt


class AttemptJournal(Protocol):
    def append(self, attempt: WriteAttempt) -> None: ...
    def get(self, attempt_id: str) -> WriteAttempt: ...
    def unresolved(self, D, plan_id: str, generation: int): ...


_TRANSITIONS = {
    AttemptState.PLANNED: {AttemptState.ISSUED, AttemptState.UNKNOWN},
    AttemptState.ISSUED: {AttemptState.ACKNOWLEDGED, AttemptState.UNKNOWN},
    AttemptState.ACKNOWLEDGED: {AttemptState.VERIFIED, AttemptState.UNKNOWN},
    AttemptState.UNKNOWN: {AttemptState.VERIFIED, AttemptState.FAILED, AttemptState.CANCELLED_VERIFIED},
}
_MUTABLE_FIELDS = {'state', 'issued_at', 'ack_at', 'verified_at', 'mutation_id'}
_TERMINAL = {AttemptState.VERIFIED, AttemptState.FAILED, AttemptState.CANCELLED_VERIFIED}


class MemoryJournal:
    def __init__(self):
        self._items = {}
        self._history = []
        self._mutex = RLock()

    def append(self, attempt: WriteAttempt) -> None:
        if not isinstance(attempt, WriteAttempt):
            raise TypeError('expected WriteAttempt')
        with self._mutex:
            previous = self._items.get(attempt.attempt_id)
            if previous is None:
                if attempt.state is not AttemptState.PLANNED or any(
                        getattr(attempt, name) is not None
                        for name in ('issued_at', 'ack_at', 'verified_at', 'mutation_id')):
                    raise ValueError('first record must be pristine planned')
            else:
                if attempt.state not in _TRANSITIONS.get(previous.state, set()):
                    raise ValueError('invalid attempt transition')
                if any(getattr(previous, f.name) != getattr(attempt, f.name)
                       for f in fields(WriteAttempt) if f.name not in _MUTABLE_FIELDS):
                    raise ValueError('attempt identity cannot change')
                for name in ('issued_at', 'ack_at', 'verified_at', 'mutation_id'):
                    old, new = getattr(previous, name), getattr(attempt, name)
                    if old is not None and old != new:
                        raise ValueError('attempt evidence cannot change')
                if attempt.state is AttemptState.ISSUED and attempt.issued_at is None:
                    raise ValueError('issued timestamp required')
                if attempt.state is AttemptState.ACKNOWLEDGED and attempt.ack_at is None:
                    raise ValueError('ack timestamp required')
                if attempt.state in _TERMINAL and attempt.verified_at is None:
                    raise ValueError('terminal evidence timestamp required')
            self._items[attempt.attempt_id] = attempt
            self._history.append(attempt)

    def get(self, attempt_id: str) -> WriteAttempt:
        with self._mutex:
            return self._items[attempt_id]

    def unresolved(self, D, plan_id: str, generation: int):
        with self._mutex:
            return tuple(a for a in self._items.values()
                         if (a.D, a.plan_id, a.generation) == (D, plan_id, generation)
                         and a.state not in _TERMINAL)

    def history(self):
        with self._mutex:
            return tuple(self._history)
