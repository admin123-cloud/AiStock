"""Offline S3a contract tests: synthetic clocks, leases, journal and server only."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from services.market_write_admission import AttemptState, WriteAttempt, WriteAdmission
from services.postclose_journal import MemoryJournal
from services import postclose_contracts as contracts

# Avoid executing utils/__init__.py and its config/database imports.
_spec = importlib.util.spec_from_file_location(
    's3a_lock_context', Path(__file__).resolve().parents[1] / 'utils' / 'postclose_lock_context.py')
_lock_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _lock_module
_spec.loader.exec_module(_lock_module)
LockContext, LockError = _lock_module.LockContext, _lock_module.LockError
LOCK_NAME, LOCK_TABLE = _lock_module.LOCK_NAME, _lock_module.LOCK_TABLE


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def utc():
    return datetime(2026, 11, 18, tzinfo=timezone.utc)


class FakeLease:
    def __init__(self, clock):
        self.clock = clock
        self.endpoint = 'db:8123'
        self.database = 'stock'
        self.current = None
        self.fail_heartbeat = False
        self.fail_release = False
        self.events = []

    def acquire(self, *, lock_table, lock_name, holder, ttl):
        self.events.append(('acquire', lock_table, lock_name, holder, ttl))
        if self.current is not None:
            return False  # even an expired owner must be reconciled externally
        self.current = (lock_table, lock_name, holder, self.clock() + ttl)
        return True

    def read(self, *, lock_table, lock_name):
        self.events.append(('read', lock_table, lock_name))
        return self.current

    def heartbeat(self, *, lock_table, lock_name, holder, ttl):
        self.events.append(('heartbeat', holder))
        now = self.clock()
        if (self.fail_heartbeat or not self.current or
                self.current[:3] != (lock_table, lock_name, holder) or self.current[3] <= now):
            return False
        self.current = (lock_table, lock_name, holder, now + ttl)
        return True

    def release(self, *, lock_table, lock_name, holder):
        self.events.append(('release', holder))
        if self.fail_release:
            raise RuntimeError('release uncertain')
        if not self.current or self.current[2] != holder:
            return False
        self.current = None
        return True


class FakeServer:
    def __init__(self):
        self.endpoint = 'db:8123'
        self.database = 'stock'
        self.events = []
        self.queue = []
        self.rows = []
        self.mode = 'committed'
        self.protocol_errors = []
        self.timeline = None

    def send(self, attempt):
        self.events.append(('send', attempt.attempt_id, attempt.query_id))
        if self.timeline is not None:
            self.timeline.append(('send', attempt.attempt_id))
        if self.mode == 'committed':
            self.rows.append(attempt.attempt_id)
            return None
        if self.mode == 'lost':
            self.rows.append(attempt.attempt_id)
            raise TimeoutError('response lost')
        if self.mode == 'pending':
            self.queue.append(attempt)
            raise TimeoutError('request still pending')
        if self.mode == 'failed':
            raise RuntimeError('result is unknown to caller')
        self.protocol_errors.append('invalid fake mode')
        raise ValueError('invalid mode')

    def cancel_requested(self):
        self.events.append(('cancel_requested',))
        # Request only: server-side execution may still occur.

    def deliver_late(self):
        self.rows.extend(item.attempt_id for item in self.queue)
        self.queue.clear()


class FakeJournal(MemoryJournal):
    def __init__(self, events):
        super().__init__()
        self.events = events
        self.fail_state = None

    def append(self, attempt):
        self.events.append(('journal', attempt.state.value, attempt.attempt_id))
        if attempt.state == self.fail_state:
            raise OSError('journal failure')
        return super().append(attempt)


def attempt(attempt_id='a1', table='stock.intraday_market_snapshot', query_id='q1'):
    return WriteAttempt(D=date(2026, 11, 18), plan_id='plan', generation=1,
                        writer_id='writer', purpose='intraday_provisional',
                        attempt_id=attempt_id, table=table, operation='insert',
                        key_digest='key-sha', payload_digest='payload-sha',
                        query_id=query_id)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.lease = FakeLease(self.clock)
        self.lock = LockContext.acquire(self.lease, endpoint='db:8123', database='stock',
                                        plan_id='plan', generation=1, clock=self.clock)
        self.events = []
        self.journal = FakeJournal(self.events)
        self.server = FakeServer()
        self.server.timeline = self.events
        self.gateway = WriteAdmission(self.journal, self.lock, self.server.send, now=utc)

    def assert_clean_fake(self):
        self.assertEqual(self.server.protocol_errors, [])

    def test_register_before_send_and_ack_not_verified(self):
        self.gateway.issue(attempt())
        self.assertEqual(self.events, [('journal', 'planned', 'a1'), ('journal', 'issued', 'a1'),
                                       ('send', 'a1'),
                                       ('journal', 'acknowledged', 'a1')])
        self.assertEqual(self.server.rows, ['a1'])
        self.assertEqual(self.journal.get('a1').state, AttemptState.ACKNOWLEDGED)
        self.assertEqual(len(self.journal.unresolved(date(2026, 11, 18), 'plan', 1)), 1)
        self.assert_clean_fake()

    def test_persist_failure_at_planned_and_issued_means_zero_send(self):
        for state in (AttemptState.PLANNED, AttemptState.ISSUED):
            with self.subTest(state=state):
                self.setUp()
                self.journal.fail_state = state
                with self.assertRaises(OSError):
                    self.gateway.issue(attempt())
                self.assertEqual(self.server.events, [])
                self.assert_clean_fake()

    def test_lost_first_response_and_second_failure_remain_unknown(self):
        self.server.mode = 'lost'
        with self.assertRaises(TimeoutError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.rows, ['a1'])
        self.assertEqual(self.journal.get('a1').state, AttemptState.UNKNOWN)
        self.server.mode = 'failed'
        with self.assertRaises(RuntimeError):
            self.gateway.issue(attempt('a2', 'stock.intraday_index_snapshot'))
        self.assertEqual(self.journal.get('a2').state, AttemptState.UNKNOWN)
        self.assertEqual(len(self.journal.unresolved(date(2026, 11, 18), 'plan', 1)), 2)
        self.assert_clean_fake()

    def test_query_id_reuse_is_not_deduplication(self):
        self.gateway.issue(attempt())
        self.gateway.issue(attempt('a2', query_id='q1'))
        self.assertEqual(self.server.rows, ['a1', 'a2'])
        with self.assertRaises(ValueError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.rows, ['a1', 'a2'])

    def test_pending_cancel_late_mutation_replay_and_restart_remain_unresolved(self):
        self.server.mode = 'pending'
        with self.assertRaises(TimeoutError):
            self.gateway.issue(attempt())
        self.server.cancel_requested()
        self.server.deliver_late()
        self.assertEqual(self.server.rows, ['a1'])
        restarted = WriteAdmission(self.journal, self.lock, self.server.send, now=utc)
        self.assertEqual(restarted.unresolved(date(2026, 11, 18), 'plan', 1)[0].state,
                         AttemptState.UNKNOWN)
        with self.assertRaises(ValueError):
            restarted.issue(attempt())
        self.assertEqual(self.server.rows, ['a1'])
        self.assert_clean_fake()

    def test_planned_or_issued_crash_is_treated_unknown_on_recovery(self):
        self.journal.append(attempt())
        self.assertEqual(self.gateway.unresolved(date(2026, 11, 18), 'plan', 1)[0].state,
                         AttemptState.UNKNOWN)
        self.journal.append(replace(attempt(), state=AttemptState.ISSUED, issued_at=utc()))
        self.assertEqual(self.gateway.unresolved(date(2026, 11, 18), 'plan', 1)[0].state,
                         AttemptState.UNKNOWN)

    def test_acknowledgement_is_not_verified_after_restart(self):
        self.gateway.issue(attempt())
        restarted = WriteAdmission(self.journal, self.lock, self.server.send, now=utc)
        self.assertEqual(restarted.unresolved(date(2026, 11, 18), 'plan', 1)[0].state,
                         AttemptState.UNKNOWN)

    def test_gateway_db_identity_must_match_lease_provider(self):
        self.server.endpoint = 'another-db:8123'
        with self.assertRaises(LockError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.events, [])
        self.assertEqual(self.journal.history(), ())

    def test_provider_cannot_extend_ttl_by_readback_alone(self):
        t, n, h, _ = self.lease.current
        self.lease.current = (t, n, h, 900)
        self.clock.value = 301
        with self.assertRaises(LockError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.events, [])

    def test_bad_transition_and_terminal_replay_rejected(self):
        self.journal.append(attempt())
        with self.assertRaises(ValueError):
            self.journal.append(replace(attempt(), state=AttemptState.VERIFIED, verified_at=utc()))
        with self.assertRaises(ValueError):
            self.journal.append(replace(attempt(), table='stock.other', state=AttemptState.ISSUED,
                                        issued_at=utc()))

    def test_plan_and_database_binding_fail_before_send(self):
        for item in (replace(attempt(), plan_id='other'), replace(attempt(), generation=2)):
            with self.assertRaises(LockError):
                self.gateway.issue(item)
        self.assertEqual(self.server.events, [])
        with self.assertRaises(LockError):
            LockContext.acquire(self.lease, endpoint='elsewhere', database='stock',
                                plan_id='plan', generation=1, clock=self.clock)
        with self.assertRaises(LockError):
            LockContext.acquire(self.lease, endpoint='db:8123', database='other',
                                plan_id='plan', generation=1, clock=self.clock)

    def test_fake_holder_changed_lock_name_and_expiry_are_rejected(self):
        for mutation in ('holder', 'name', 'ttl', 'budget', 'heartbeat'):
            with self.subTest(mutation=mutation):
                self.setUp()
                if mutation == 'holder':
                    t, n, _, expiry = self.lease.current
                    self.lease.current = (t, n, 'impostor', expiry)
                elif mutation == 'name':
                    t, _, h, expiry = self.lease.current
                    self.lease.current = (t, 'other', h, expiry)
                elif mutation == 'ttl':
                    self.clock.value = self.lease.current[3]
                elif mutation == 'budget':
                    self.clock.value = 1800
                    t, n, h, _ = self.lease.current
                    self.lease.current = (t, n, h, 5000)
                else:
                    self.lease.fail_heartbeat = True
                    with self.assertRaises(LockError):
                        self.lock.heartbeat()
                with self.assertRaises(LockError):
                    self.gateway.issue(attempt())
                self.assertEqual(self.server.events, [])
                self.assertEqual(len([e for e in self.lease.events if e[0] == 'acquire']), 1)

    def test_lost_between_intent_and_send_stops_send(self):
        original = self.journal.append
        def append(item):
            original(item)
            if item.state == AttemptState.ISSUED:
                t, n, _, expiry = self.lease.current
                self.lease.current = (t, n, 'other', expiry)
        self.journal.append = append
        with self.assertRaises(LockError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.events, [])
        self.assertEqual(self.gateway.unresolved(date(2026, 11, 18), 'plan', 1)[0].state,
                         AttemptState.UNKNOWN)

    def test_no_caller_lock_identity_or_ttl_and_release_failure(self):
        for kw in ({'holder': 'forged'}, {'lock_name': 'other'}, {'ttl': 99999}):
            with self.assertRaises(TypeError):
                LockContext.acquire(self.lease, endpoint='db:8123', database='stock',
                                    plan_id='plan', generation=1, clock=self.clock, **kw)
        self.lease.fail_release = True
        with self.assertRaises(LockError):
            self.lock.release()
        self.assertFalse(self.lock.released)
        with self.assertRaises(LockError):
            self.lock.assert_held(endpoint='db:8123', database='stock', plan_id='plan', generation=1)

    def test_original_owner_alive_no_self_reclaim(self):
        self.clock.value = 1801
        with self.assertRaises(LockError):
            LockContext.acquire(self.lease, endpoint='db:8123', database='stock',
                                plan_id='plan', generation=1, clock=self.clock)
        self.assertEqual(self.lease.current[2], self.lease.events[0][3])
        with self.assertRaises(LockError):
            self.lock.assert_held(endpoint='db:8123', database='stock', plan_id='plan', generation=1)
        self.assertEqual(self.lease.events[0][1:3], (LOCK_TABLE, LOCK_NAME))

    def test_repeated_heartbeats_do_not_reset_absolute_budget(self):
        for tick in (250, 500, 750, 1000, 1250, 1500, 1750, 1799):
            self.clock.value = tick
            self.lock.heartbeat()
        self.clock.value = 1800
        with self.assertRaises(LockError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.events, [])

    def test_ack_persistence_error_leaves_issued_and_cannot_replay(self):
        self.journal.fail_state = AttemptState.ACKNOWLEDGED
        with self.assertRaises(OSError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.rows, ['a1'])
        self.assertEqual(self.gateway.unresolved(date(2026, 11, 18), 'plan', 1)[0].state,
                         AttemptState.UNKNOWN)
        with self.assertRaises(ValueError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.rows, ['a1'])

    def test_no_network_or_filesystem_during_fake_gateway(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch('builtins.open', side_effect=AssertionError('file IO forbidden')), \
             patch.object(Path, 'open', side_effect=AssertionError('file IO forbidden')):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.rows, ['a1'])
        self.assert_clean_fake()

    def test_successful_release_requires_readback_and_stops_context(self):
        self.lock.release()
        self.assertTrue(self.lock.released)
        with self.assertRaises(LockError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.events, [])

    def test_heartbeat_expiry_between_assert_and_renewal_stops_before_provider(self):
        self.clock.value = 299
        with patch.object(self.lock, '_clock', side_effect=[299, 299, 300]):
            with self.assertRaises(LockError):
                self.lock.heartbeat()
        self.assertFalse(any(event[0] == 'heartbeat' for event in self.lease.events))
        with self.assertRaises(LockError):
            self.gateway.issue(attempt())
        with self.assertRaises(LockError):
            self.lock.heartbeat()
        self.assertEqual(self.server.events, [])

    def test_heartbeat_expiry_inside_provider_must_not_revive(self):
        self.clock.value = 299
        original = self.lease.heartbeat
        def delayed(**kwargs):
            self.clock.value = 300
            return original(**kwargs)
        with patch.object(self.lease, 'heartbeat', side_effect=delayed):
            with self.assertRaises(LockError):
                self.lock.heartbeat()
        self.assertEqual(self.lease.current[3], 300)
        with self.assertRaises(LockError):
            self.gateway.issue(attempt())
        self.assertEqual(self.server.events, [])

    def test_fake_provider_rejects_expired_or_wrong_holder_heartbeat(self):
        table, name, holder, expiry = self.lease.current
        self.assertFalse(self.lease.heartbeat(lock_table=table, lock_name=name,
                                              holder='wrong', ttl=300))
        self.clock.value = expiry
        self.assertFalse(self.lease.heartbeat(lock_table=table, lock_name=name,
                                              holder=holder, ttl=300))
        self.assertEqual(self.lease.current[3], expiry)

    def test_heartbeat_before_expiry_renews_normally(self):
        self.clock.value = 299
        self.lock.heartbeat()
        self.clock.value = 300
        self.gateway.issue(attempt())
        self.assertEqual(self.server.rows, ['a1'])
        self.assertEqual(self.lease.current[3], 599)

    def test_generation_manifest_boundary(self):
        day = date(2026, 11, 18)
        start, end = contracts.day_window(day)
        deadline = start + timedelta(hours=16, minutes=15)
        digest = 'a' * 64
        manifest = contracts.PlanManifest(
            target_date=day, plan_id='plan', generation=1, approval_id='approval',
            policy_decision_ref='decision',
            operation_permit_refs=tuple((op, 'permit:' + op) for op in ('delete', 'insert', 'invalidate', 'recovery')),
            target_schema_fingerprints=tuple((item[0], digest) for item in contracts.CLEANUP_OBJECTS[:-1]),
            predicate_digest=digest, observed_raw_keys_digest=digest, observed_final_keys_digest=digest,
            observed_payload_digest=digest, identity_version='identity', expected_keys_digest=digest,
            exception_ledger_ref='ledger', excluded_objects=('stock.kline_minute_1', 'stock.kline_daily', 'account_objects'),
            certificate_digests=(('source', digest), ('backup', digest), ('close', digest)),
            non_target_sentinel_digest=digest, delete_start_latest=deadline - timedelta(minutes=5),
            clean_deadline=deadline, import_not_before=deadline, lease_budget=timedelta(minutes=10),
            recovery_strategy='recovery', read_impact='not_ready', alarm_owner='owner',
            window_start=start, window_end=end)
        self.assertTrue(contracts.validate_manifest(manifest))
        self.assertFalse(contracts.validate_manifest(replace(manifest, generation=0)))

    def test_generation_stage_result_boundary(self):
        day = date(2026, 11, 18)
        result = contracts.StageResult(target_date=day, plan_id='plan', generation=1,
            outcome=contracts.StageState.INTRADAY, reason_code='ok', rc=0,
            source_attempted=False, source_completed=False, write_attempted=False,
            writes_known=True, accepted_keys=0, rejected_keys=0, exception_ledger_ref='',
            coverage_status='unknown', certificate_ref=None, retry_safe=False, alarm_severity='none')
        self.assertTrue(contracts.validate_stage_result(result, target_date=day, plan_id='plan', generation=1))
        self.assertFalse(contracts.validate_stage_result(replace(result, generation=0),
                                                        target_date=day, plan_id='plan', generation=0))

    def test_generation_certificate_boundary(self):
        day = date(2026, 11, 18)
        now = utc()
        digest = 'a' * 64
        cert = contracts.SourceCertificate(target_date=day, plan_id='plan', generation=1,
            approval_id='approval', issued_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=1), issuer='issuer', evidence_refs=('evidence',),
            schema_digest=digest, source_digest=digest, accepted_keys_digest=digest,
            rejected_keys_digest=digest, coverage_status='complete', source_task_success=True,
            reconstruction_ready=True, publish_ready=False)
        def valid(item, generation):
            return contracts.validate_certificate(item, target_date=day, plan_id='plan',
                generation=generation, approval_id='approval', expected_digest=contracts.digest_object(item),
                trusted_issuers=('issuer',), now=now)
        self.assertTrue(valid(cert, 1))
        self.assertFalse(valid(replace(cert, generation=0), 0))

    def test_generation_s3a_rejects_zero_accepts_one(self):
        with self.assertRaises(ValueError):
            replace(attempt(), generation=0)
        self.assertEqual(attempt().generation, 1)
        lease = FakeLease(self.clock)
        with self.assertRaises(LockError):
            LockContext.acquire(lease, endpoint='db:8123', database='stock',
                                plan_id='plan', generation=0, clock=self.clock)
        self.assertEqual(lease.events, [])
        self.assertIsNotNone(LockContext.acquire(lease, endpoint='db:8123', database='stock',
                                                 plan_id='plan', generation=1, clock=self.clock))

    def test_acknowledged_to_verified_and_terminal_replay(self):
        ack = self.gateway.issue(attempt())
        verified = replace(ack, state=AttemptState.VERIFIED, verified_at=utc())
        self.journal.append(verified)
        self.assertEqual(self.journal.get('a1'), verified)
        self.assertEqual(self.journal.unresolved(verified.D, verified.plan_id, verified.generation), ())
        for state in AttemptState:
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.journal.append(replace(verified, state=state))

    def test_unknown_to_each_terminal_requires_real_unknown_and_evidence(self):
        for terminal in (AttemptState.VERIFIED, AttemptState.FAILED, AttemptState.CANCELLED_VERIFIED):
            with self.subTest(terminal=terminal):
                self.setUp()
                self.journal.append(attempt())
                issued = replace(attempt(), state=AttemptState.ISSUED, issued_at=utc())
                self.journal.append(issued)
                # unresolved is a projection only; journal still holds ISSUED.
                projected = self.gateway.unresolved(issued.D, issued.plan_id, issued.generation)[0]
                self.assertEqual(projected.state, AttemptState.UNKNOWN)
                self.assertEqual(self.journal.get('a1').state, AttemptState.ISSUED)
                with self.assertRaises(ValueError):
                    self.journal.append(replace(projected, state=terminal, verified_at=utc()))
                # A future durable reconciler must CAS the real predecessor to UNKNOWN first.
                self.journal.append(projected)
                with self.assertRaises(ValueError):
                    self.journal.append(replace(projected, state=terminal))
                final = replace(projected, state=terminal, verified_at=utc())
                self.journal.append(final)
                self.assertEqual(self.journal.get('a1'), final)
                for state in AttemptState:
                    with self.subTest(replay=state), self.assertRaises(ValueError):
                        self.journal.append(replace(final, state=state))

    def test_ack_terminal_missing_time_and_evidence_tampering_rejected(self):
        planned = attempt()
        self.journal.append(planned)
        issued = replace(planned, state=AttemptState.ISSUED, issued_at=utc())
        self.journal.append(issued)
        ack = replace(issued, state=AttemptState.ACKNOWLEDGED, ack_at=utc(), mutation_id='mutation')
        self.journal.append(ack)
        with self.assertRaises(ValueError):
            self.journal.append(replace(ack, state=AttemptState.VERIFIED))
        for field, value in (('issued_at', utc() + timedelta(seconds=1)),
                             ('ack_at', utc() + timedelta(seconds=1)),
                             ('mutation_id', 'changed')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.journal.append(replace(ack, state=AttemptState.VERIFIED,
                                            verified_at=utc(), **{field: value}))


if __name__ == '__main__':
    unittest.main()
