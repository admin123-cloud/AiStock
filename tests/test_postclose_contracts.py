import dataclasses
import unittest
from datetime import date, datetime, timedelta, timezone

from services import postclose_contracts as c


D = date(2026, 12, 31)
NOW = datetime(2026, 12, 31, 8, tzinfo=timezone.utc)
DIGEST = "a" * 64


class ContractsTest(unittest.TestCase):
    def test_half_open_boundaries_across_month_and_year(self):
        for day, following in ((date(2026, 11, 30), date(2026, 12, 1)),
                               (D, date(2027, 1, 1))):
            start, end = c.day_window(day)
            self.assertEqual(start.date(), day)
            self.assertEqual(end.date(), following)
            self.assertEqual((end - start).total_seconds(), 86400)
            self.assertTrue(c.in_day_window(start, day))
            self.assertTrue(c.in_day_window(end - timedelta(microseconds=1), day))
            self.assertFalse(c.in_day_window(end, day))
            self.assertFalse(c.in_day_window(start - timedelta(microseconds=1), day))

    def test_exact_cleanup_scope_and_forbidden_operations(self):
        self.assertEqual(c.CLEANUP_OBJECTS, (
            ("stock.kline_minute_5", "delete", "datetime"),
            ("stock.kline_minute_15", "delete", "datetime"),
            ("stock.kline_minute_30", "delete", "datetime"),
            ("stock.kline_minute_60", "delete", "datetime"),
            ("stock.intraday_market_snapshot", "delete", "snapshot_ts"),
            ("stock.intraday_index_snapshot", "delete", "snapshot_ts"),
            ("stock.kline_daily_intraday", "delete", "trade_date"),
            ("runtime/market_mood/YYYYMMDD.json", "invalidate_realtime", "date"),
        ))
        for table in ("stock.kline_minute_1", "stock.kline_daily", "stock.account", "stock.positions"):
            self.assertFalse(c.cleanup_operation_allowed(table, "delete"))
        self.assertFalse(c.cleanup_operation_allowed("runtime/market_mood/YYYYMMDD.json", "delete"))
        self.assertTrue(c.cleanup_operation_allowed("stock.kline_minute_5", "delete"))

    def test_immutable_manifest_and_stable_digest(self):
        manifest = c.PlanManifest(target_date=D, plan_id="p1", generation=2,
                                  approval_id="approval", policy_decision_ref="decision")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.plan_id = "other"
        self.assertEqual(c.digest_object(manifest), c.digest_object(dataclasses.replace(manifest)))
        self.assertNotEqual(c.digest_object(manifest), c.digest_object(dataclasses.replace(manifest, generation=3)))
        self.assertEqual(len(c.canonical_json(manifest)), len(c.canonical_json(dataclasses.replace(manifest))))

    def certificate(self):
        return c.SourceCertificate(target_date=D, plan_id="p1", generation=2,
            approval_id="approval", issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=1), issuer="reviewed-issuer",
            evidence_refs=("evidence:source",), source_digest=DIGEST,
            schema_digest="b" * 64, accepted_keys_digest="c" * 64,
            rejected_keys_digest="d" * 64, coverage_status="complete",
            source_task_success=True, reconstruction_ready=True, publish_ready=False)

    def test_certificate_binding_expiry_and_integrity(self):
        cert = self.certificate()
        expected = c.digest_object(cert)
        def check(item=cert, day=D, plan="p1", gen=2, approval="approval", digest=expected, now=NOW):
            return c.validate_certificate(item, target_date=day, plan_id=plan, generation=gen,
                approval_id=approval, expected_digest=digest, trusted_issuers=("reviewed-issuer",), now=now)
        self.assertTrue(check())
        self.assertFalse(check(day=date(2026, 12, 30)))
        self.assertFalse(check(plan="p2"))
        self.assertFalse(check(gen=3))
        self.assertFalse(check(approval="other"))
        self.assertFalse(check(now=cert.expires_at))
        self.assertFalse(check(item=dataclasses.replace(cert, source_digest="e" * 64)))
        self.assertFalse(check(item=dataclasses.replace(cert, source_digest="broken"), digest=c.digest_object(dataclasses.replace(cert, source_digest="broken"))))
        self.assertFalse(check(digest="0" * 64))
        self.assertFalse(c.validate_certificate(cert, target_date=D, plan_id="p1", generation=2,
            approval_id="approval", expected_digest=expected, trusted_issuers=(), now=NOW))
        self.assertFalse(check(now=NOW.replace(tzinfo=None)))

    def test_all_rejected_terminal_is_not_clean_permission(self):
        cert = dataclasses.replace(self.certificate(), coverage_status="all_rejected",
                                   reconstruction_ready=False, publish_ready=False)
        result = c.StageResult(target_date=D, plan_id="p1", generation=2, outcome=c.StageState.TERMINAL_REJECTED_GAPS,
            reason_code="all_rejected", rc=0, source_attempted=True, source_completed=True,
            write_attempted=False, writes_known=True, accepted_keys=0, rejected_keys=10,
            exception_ledger_ref="ledger", coverage_status="blocked", certificate_ref=None,
            retry_safe=False, alarm_severity="warning")
        self.assertEqual(result.rc, 0)
        self.assertFalse(c.source_reconstruction_ready(cert))
        self.assertTrue(c.source_reconstruction_ready(self.certificate()))
        # A ready source alone never grants a deletion permit.
        self.assertFalse(c.cleanup_prerequisites_satisfied(None, self.certificate(), None, None))

    def test_state_purpose_and_rc_contract(self):
        self.assertEqual({x.value for x in c.Purpose}, {"intraday_provisional", "afterclose_cleanup",
            "afterclose_authoritative", "recovery", "operational_evidence"})
        self.assertEqual([c.ReturnCode(i).value for i in range(8)], list(range(8)))
        for name in ("INTRADAY", "DRAINING", "INTRADAY_CLOSED", "PREPARED", "BACKED_UP", "CLEANING", "CLEANED", "IMPORTING", "VERIFYING", "CERTIFIED", "BLOCKED", "DEADLINE_MISSED", "UNKNOWN_WRITES", "IMPORT_FAILED", "RECOVERY_REQUIRED", "TERMINAL_REJECTED_GAPS"):
            self.assertEqual(c.StageState[name].value, name)

    def test_certificate_types_exist_and_are_frozen(self):
        for name in ("SourceCertificate", "BackupCertificate", "CloseCertificate", "CleanCertificate", "PublishCertificate", "StageResult", "PlanManifest"):
            self.assertTrue(dataclasses.is_dataclass(getattr(c, name)))
            self.assertTrue(getattr(c, name).__dataclass_params__.frozen)

    def test_all_certificate_kinds_bind_identity_integrity_and_expiry(self):
        base = dict(target_date=D, plan_id="p1", generation=2, approval_id="approval",
                    issued_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=1),
                    issuer="reviewed-issuer", evidence_refs=("evidence",), schema_digest=DIGEST)
        all_certs = (self.certificate(),
            c.BackupCertificate(**base, backup_digest=DIGEST, keys_digest=DIGEST,
                baseline_digest=DIGEST, recovery_evidence_ref="restore", recovery_verified=True),
            c.CloseCertificate(**base, attempts_digest=DIGEST, server_evidence_digest=DIGEST,
                fencing_evidence_ref="fencing", unresolved_writes=0,
                closed_at=datetime(2026, 12, 31, 6, 59, tzinfo=timezone.utc)),
            c.CleanCertificate(**base, manifest_digest=DIGEST, source_digest=DIGEST,
                backup_digest=DIGEST, close_digest=DIGEST, mutation_evidence_digest=DIGEST,
                residual_digest=DIGEST, completed_at=NOW - timedelta(minutes=1)),
            c.PublishCertificate(**dict(base, issued_at=NOW + timedelta(minutes=16), expires_at=NOW + timedelta(minutes=17)), manifest_digest=DIGEST, clean_digest=DIGEST,
                source_digest=DIGEST, verification_digest=DIGEST, coverage_status="complete",
                published_at=NOW + timedelta(minutes=16)))
        for cert in all_certs:
            with self.subTest(kind=type(cert).__name__):
                kw = dict(target_date=D, plan_id="p1", generation=2, approval_id="approval",
                          expected_digest=c.digest_object(cert), trusted_issuers=("reviewed-issuer",), now=max(NOW, cert.issued_at))
                self.assertTrue(c.validate_certificate(cert, **kw))
                for field, value in (("target_date", date(2026, 12, 30)), ("plan_id", "wrong"),
                                     ("generation", 3), ("approval_id", "wrong"), ("now", cert.expires_at),
                                     ("expected_digest", "0" * 64)):
                    bad = dict(kw)
                    bad[field] = value
                    self.assertFalse(c.validate_certificate(cert, **bad), field)
                changed = dataclasses.replace(cert, evidence_refs=("tampered",))
                self.assertFalse(c.validate_certificate(changed, **kw))

    def test_manifest_default_is_not_a_valid_cleanup_manifest(self):
        m = c.PlanManifest(target_date=D, plan_id="p1", generation=2,
                           approval_id="approval", policy_decision_ref="decision")
        self.assertFalse(c.validate_manifest(m))
        with self.assertRaises((TypeError, ValueError)):
            c.PlanManifest(target_date=D, plan_id="p1", generation=2,
                           approval_id="approval", policy_decision_ref="decision",
                           operation_permit_refs=["mutable"])

    def test_manifest_full_scope_and_required_fields(self):
        start, end = c.day_window(D)
        deadline = start + timedelta(hours=16, minutes=15)
        m = c.PlanManifest(target_date=D, plan_id="p1", generation=2, approval_id="approval",
            policy_decision_ref="decision", operation_permit_refs=tuple((op, "permit:" + op) for op in ("delete", "insert", "invalidate", "recovery")),
            target_schema_fingerprints=tuple((obj[0], DIGEST) for obj in c.CLEANUP_OBJECTS[:-1]),
            predicate_digest=DIGEST, observed_raw_keys_digest=DIGEST, observed_final_keys_digest=DIGEST,
            observed_payload_digest=DIGEST, identity_version="identity-v1", expected_keys_digest=DIGEST,
            exception_ledger_ref="ledger", excluded_objects=("stock.kline_minute_1", "stock.kline_daily", "account_objects"),
            certificate_digests=(("source", DIGEST), ("backup", DIGEST), ("close", DIGEST)),
            non_target_sentinel_digest=DIGEST, delete_start_latest=deadline - timedelta(minutes=5),
            clean_deadline=deadline, import_not_before=deadline, lease_budget=timedelta(minutes=10),
            recovery_strategy="recovery", read_impact="not_ready", alarm_owner="oncall",
            window_start=start, window_end=end)
        self.assertTrue(c.validate_manifest(m))
        for change in (dict(operation_permit_refs=m.operation_permit_refs[:-1]),
                       dict(target_schema_fingerprints=m.target_schema_fingerprints[:-1]),
                       dict(certificate_digests=m.certificate_digests[:-1]),
                       dict(window_end=end - timedelta(microseconds=1)),
                       dict(scope_kind="source_codes_only"), dict(expected_keys_digest=""),
                       dict(cleanup_objects=m.cleanup_objects[:-1]),
                       dict(delete_start_latest=deadline), dict(lease_budget=timedelta(0))):
            with self.subTest(change=change):
                self.assertFalse(c.validate_manifest(dataclasses.replace(m, **change)))
        self.assertFalse(c.cleanup_prerequisites_satisfied(m, self.certificate(), None, None))

    def test_failure_results_cannot_claim_success(self):
        base = dict(target_date=D, plan_id="p1", generation=2, reason_code="unknown", source_attempted=True, source_completed=False,
                    write_attempted=True, writes_known=False, accepted_keys=0, rejected_keys=0,
                    exception_ledger_ref="", coverage_status="unknown", certificate_ref=None,
                    retry_safe=False, alarm_severity="critical")
        good = c.StageResult(outcome=c.StageState.UNKNOWN_WRITES, rc=6, **base)
        ctx = dict(target_date=D, plan_id="p1", generation=2)
        self.assertTrue(c.validate_stage_result(good, **ctx))
        self.assertFalse(c.validate_stage_result(dataclasses.replace(good, rc=0), **ctx))
        self.assertFalse(c.validate_stage_result(dataclasses.replace(good, retry_safe=True), **ctx))
        self.assertFalse(c.validate_stage_result(good, target_date=date(2026, 12, 30), plan_id="p1", generation=2))
        self.assertFalse(c.validate_stage_result(good, target_date=D, plan_id="p2", generation=2))
        self.assertFalse(c.validate_stage_result(good, target_date=D, plan_id="p1", generation=3))
        for outcome, rc in ((c.StageState.SOURCE_FAILED, 3), (c.StageState.DEGRADED, 4)):
            item = dataclasses.replace(good, outcome=outcome, rc=rc, writes_known=True)
            self.assertTrue(c.validate_stage_result(item, **ctx))
            self.assertFalse(c.validate_stage_result(dataclasses.replace(item, rc=0), **ctx))

    def test_certificate_evidence_deadlines_fail_closed(self):
        base = dict(target_date=D, plan_id="p1", generation=2, approval_id="approval",
                    issued_at=NOW, expires_at=NOW + timedelta(minutes=1), issuer="reviewed-issuer",
                    evidence_refs=("evidence",), schema_digest=DIGEST)
        close = c.CloseCertificate(**base, attempts_digest=DIGEST, server_evidence_digest=DIGEST,
            fencing_evidence_ref="fencing", unresolved_writes=0,
            closed_at=datetime(2026, 12, 31, 6, 59, tzinfo=timezone.utc))
        backup = c.BackupCertificate(**base, backup_digest=DIGEST, keys_digest=DIGEST,
            baseline_digest=DIGEST, recovery_evidence_ref="restore", recovery_verified=True)
        clean = c.CleanCertificate(**base, manifest_digest=DIGEST, source_digest=DIGEST,
            backup_digest=DIGEST, close_digest=DIGEST, mutation_evidence_digest=DIGEST,
            residual_digest=DIGEST, completed_at=NOW)
        def valid(cert):
            return c.validate_certificate(cert, target_date=D, plan_id="p1", generation=2,
                approval_id="approval", expected_digest=c.digest_object(cert),
                trusted_issuers=("reviewed-issuer",), now=NOW)
        for cert in (close, backup, clean):
            self.assertTrue(valid(cert))
        self.assertFalse(valid(dataclasses.replace(close, unresolved_writes=1)))
        self.assertFalse(valid(dataclasses.replace(close, closed_at=datetime(2026, 12, 31, 7, 5, tzinfo=timezone.utc))))
        self.assertFalse(valid(dataclasses.replace(backup, recovery_verified=False)))
        self.assertFalse(valid(dataclasses.replace(clean, completed_at=datetime(2026, 12, 31, 8, 15, tzinfo=timezone.utc))))

    def test_source_readiness_cross_product(self):
        base = self.certificate()
        def check(cert):
            return c.validate_certificate(cert, target_date=D, plan_id="p1", generation=2,
                approval_id="approval", expected_digest=c.digest_object(cert),
                trusted_issuers=("reviewed-issuer",), now=NOW)
        self.assertTrue(check(base))  # complete, reconstruction ready, not publish ready
        for coverage in ("complete", "with_exceptions", "blocked", "all_rejected"):
            for source in (False, True):
                for rebuild in (False, True):
                    for publish in (False, True):
                        cert = dataclasses.replace(base, coverage_status=coverage,
                            source_task_success=source, reconstruction_ready=rebuild, publish_ready=publish)
                        expected = (not rebuild and not publish) or (coverage == "complete" and source and rebuild)
                        with self.subTest(coverage=coverage, source=source, rebuild=rebuild, publish=publish):
                            self.assertEqual(check(cert), expected)
        terminal = dataclasses.replace(base, coverage_status="all_rejected", reconstruction_ready=False)
        self.assertTrue(check(terminal))
        self.assertFalse(c.source_reconstruction_ready(terminal))
        self.assertFalse(c.cleanup_prerequisites_satisfied(None, terminal, None, None))

    def test_stage_result_rc_matrix_and_contradictions(self):
        ctx = dict(target_date=D, plan_id="p1", generation=2)
        base = dict(**ctx, reason_code="test", source_attempted=True, source_completed=False,
                    write_attempted=True, writes_known=True, accepted_keys=0, rejected_keys=0,
                    exception_ledger_ref="", coverage_status="unknown", certificate_ref=None,
                    retry_safe=False, alarm_severity="critical")
        samples = (
            dict(outcome=c.StageState.CERTIFIED, rc=0, source_completed=True, coverage_status="complete", certificate_ref="cert:publish"),
            dict(outcome=c.StageState.EXECUTION_FAILED, rc=1),
            dict(outcome=c.StageState.BLOCKED, rc=2),
            dict(outcome=c.StageState.SOURCE_FAILED, rc=3),
            dict(outcome=c.StageState.DEGRADED, rc=4),
            dict(outcome=c.StageState.DEADLINE_MISSED, rc=5),
            dict(outcome=c.StageState.UNKNOWN_WRITES, rc=6, writes_known=False),
            dict(outcome=c.StageState.RECOVERY_REQUIRED, rc=7),
        )
        for sample in samples:
            result = c.StageResult(**dict(base, **sample))
            with self.subTest(outcome=result.outcome):
                self.assertTrue(c.validate_stage_result(result, **ctx))
                for rc in range(8):
                    if rc != result.rc:
                        self.assertFalse(c.validate_stage_result(dataclasses.replace(result, rc=rc), **ctx))
        cleaned = c.StageResult(**dict(base, outcome=c.StageState.CLEANED, rc=3))
        self.assertFalse(c.validate_stage_result(cleaned, **ctx))
        terminal = c.StageResult(**dict(base, outcome=c.StageState.TERMINAL_REJECTED_GAPS, rc=0,
            source_completed=True, accepted_keys=0, rejected_keys=3, exception_ledger_ref="ledger", coverage_status="blocked", alarm_severity="warning"))
        self.assertTrue(c.validate_stage_result(terminal, **ctx))
        for change in (dict(source_attempted=False, source_completed=False), dict(source_completed=False),
                       dict(accepted_keys=1), dict(rejected_keys=0), dict(exception_ledger_ref="")):
            self.assertFalse(c.validate_stage_result(dataclasses.replace(terminal, **change), **ctx), change)
        certified = c.StageResult(**dict(base, **samples[0]))
        for change in (dict(coverage_status="unknown"), dict(source_completed=False),
                       dict(certificate_ref=42), dict(certificate_ref="  ")):
            self.assertFalse(c.validate_stage_result(dataclasses.replace(certified, **change), **ctx), change)
        closed = c.StageResult(**dict(base, outcome=c.StageState.NO_OP_CLOSED, rc=0,
            source_attempted=False, source_completed=False, write_attempted=False,
            coverage_status="closed", alarm_severity="none", reason_code="calendar_closed"))
        self.assertTrue(c.validate_stage_result(closed, **ctx))
        self.assertFalse(c.validate_stage_result(dataclasses.replace(closed, coverage_status="unknown"), **ctx))
        self.assertFalse(c.validate_stage_result(dataclasses.replace(closed, write_attempted=True), **ctx))
        for sample in samples:
            if sample["rc"] != 6:
                result = c.StageResult(**dict(base, **sample, writes_known=False))
                self.assertFalse(c.validate_stage_result(result, **ctx))

    def test_unknown_preserves_concurrent_event_facts(self):
        item = c.StageResult(target_date=D, plan_id="p1", generation=2,
            outcome=c.StageState.UNKNOWN_WRITES, reason_code="unknown", rc=6,
            source_attempted=True, source_completed=False, write_attempted=True, writes_known=False,
            accepted_keys=0, rejected_keys=0, exception_ledger_ref="", coverage_status="unknown",
            certificate_ref=None, retry_safe=False, alarm_severity="critical",
            concurrent_events=("source_failed", "deadline_missed", "recovery_required"))
        self.assertTrue(c.validate_stage_result(item, target_date=D, plan_id="p1", generation=2))
        self.assertEqual(item.concurrent_events, ("source_failed", "deadline_missed", "recovery_required"))

    def test_existing_stage_contradictions_are_rejected(self):
        ctx = dict(target_date=D, plan_id="p1", generation=2)
        base = c.StageResult(**ctx, outcome=c.StageState.CLEANED, rc=3, reason_code="bad",
            source_attempted=False, source_completed=False, write_attempted=False, writes_known=True,
            accepted_keys=0, rejected_keys=0, exception_ledger_ref="", coverage_status="unknown",
            certificate_ref=None, retry_safe=False, alarm_severity="critical")
        self.assertFalse(c.validate_stage_result(base, **ctx))
        rejected = dataclasses.replace(base, outcome=c.StageState.TERMINAL_REJECTED_GAPS, rc=0,
            coverage_status="blocked", accepted_keys=1, rejected_keys=0)
        self.assertFalse(c.validate_stage_result(rejected, **ctx))
        certified = dataclasses.replace(base, outcome=c.StageState.CERTIFIED, rc=0, certificate_ref="cert")
        self.assertFalse(c.validate_stage_result(certified, **ctx))
        unknown = dataclasses.replace(base, outcome=c.StageState.SOURCE_FAILED, rc=3, writes_known=False)
        self.assertFalse(c.validate_stage_result(unknown, **ctx))

    def test_stage_intermediate_state_facts_and_concurrent_priority(self):
        ctx = dict(target_date=D, plan_id="p1", generation=2)
        base = c.StageResult(**ctx, outcome=c.StageState.CLEANED, rc=0, reason_code="cleaned",
            source_attempted=True, source_completed=True, write_attempted=True, writes_known=True,
            accepted_keys=10, rejected_keys=0, exception_ledger_ref="", coverage_status="complete",
            certificate_ref=None, retry_safe=False, alarm_severity="none")
        self.assertTrue(c.validate_stage_result(base, **ctx))
        self.assertFalse(c.validate_stage_result(dataclasses.replace(base, write_attempted=False), **ctx))
        self.assertFalse(c.validate_stage_result(dataclasses.replace(base, concurrent_events=("source_failed",)), **ctx))
        for state in (c.StageState.CLEANING, c.StageState.IMPORTING, c.StageState.VERIFYING):
            self.assertTrue(c.validate_stage_result(dataclasses.replace(base, outcome=state), **ctx))
            self.assertFalse(c.validate_stage_result(dataclasses.replace(base, outcome=state, write_attempted=False), **ctx))

    def test_preissue_stage_entry_and_ordered_synthetic_attempts(self):
        ctx = dict(target_date=D, plan_id="p1", generation=2)
        def make(state, attempted=False, **overrides):
            values = dict(**ctx, outcome=state, rc=0, reason_code="stage_entered",
                source_attempted=True, source_completed=True, write_attempted=attempted,
                writes_known=True, accepted_keys=10, rejected_keys=0,
                exception_ledger_ref="", coverage_status="complete", certificate_ref=None,
                retry_safe=False, alarm_severity="none", stage_entry_ref="entry:1")
            values.update(overrides)
            return c.StageResult(**values)

        def fake_run(state, attempted=False, fail_at=None):
            events = ["source_ready", "backup_ready"]
            result = make(state, attempted)
            if not c.validate_stage_result(result, **ctx):
                return events
            for event in ("stage_not_ready_recorded", "planned", "issued"):
                if fail_at == event:
                    return events
                events.append(event)
            assert events[-3:] == ["stage_not_ready_recorded", "planned", "issued"]
            events.append("fake_mutation")
            return events

        for state in (c.StageState.CLEANING, c.StageState.IMPORTING):
            with self.subTest(state=state):
                self.assertTrue(c.validate_stage_result(make(state), **ctx))
                self.assertEqual(fake_run(state), ["source_ready", "backup_ready", "stage_not_ready_recorded", "planned", "issued", "fake_mutation"])
                for fail_at in ("stage_not_ready_recorded", "planned", "issued"):
                    self.assertNotIn("fake_mutation", fake_run(state, fail_at=fail_at))
                self.assertEqual(fake_run(state, fail_at="stage_not_ready_recorded"), ["source_ready", "backup_ready"])
                for changes in (dict(stage_entry_ref=None), dict(stage_entry_ref=" "),
                                dict(retry_safe=True), dict(writes_known=False),
                                dict(source_completed=False), dict(certificate_ref="cert")):
                    self.assertFalse(c.validate_stage_result(make(state, **changes), **ctx), changes)
        # Cumulative scope: importing may already contain the cleanup attempt.
        self.assertTrue(c.validate_stage_result(make(c.StageState.IMPORTING, True), **ctx))
        self.assertEqual(fake_run(c.StageState.IMPORTING, True)[2:],
                         ["stage_not_ready_recorded", "planned", "issued", "fake_mutation"])
        self.assertFalse(c.validate_stage_result(make(c.StageState.CLEANED), **ctx))
        self.assertFalse(c.validate_stage_result(make(c.StageState.VERIFYING), **ctx))

    def test_missing_stage_positive_controls_and_priority_negatives(self):
        ctx = dict(target_date=D, plan_id="p1", generation=2)
        base = c.StageResult(**ctx, outcome=c.StageState.INTRADAY, rc=0, reason_code="ok",
            source_attempted=False, source_completed=False, write_attempted=False, writes_known=True,
            accepted_keys=0, rejected_keys=0, exception_ledger_ref="", coverage_status="unknown",
            certificate_ref=None, retry_safe=False, alarm_severity="none")
        for state in (c.StageState.INTRADAY, c.StageState.DRAINING, c.StageState.INTRADAY_CLOSED,
                      c.StageState.PREPARED, c.StageState.BACKED_UP):
            item = dataclasses.replace(base, outcome=state)
            self.assertTrue(c.validate_stage_result(item, **ctx), state)
            self.assertFalse(c.validate_stage_result(dataclasses.replace(item, rc=1), **ctx), state)
        failed = dataclasses.replace(base, outcome=c.StageState.IMPORT_FAILED, rc=1,
            source_attempted=True, source_completed=True, write_attempted=True,
            accepted_keys=10, coverage_status="complete", alarm_severity="critical")
        self.assertTrue(c.validate_stage_result(failed, **ctx))
        self.assertFalse(c.validate_stage_result(dataclasses.replace(failed, rc=0), **ctx))
        for state, rc in ((c.StageState.EXECUTION_FAILED, 1), (c.StageState.IMPORT_FAILED, 1),
                          (c.StageState.BLOCKED, 2), (c.StageState.SOURCE_FAILED, 3),
                          (c.StageState.DEGRADED, 4), (c.StageState.DEADLINE_MISSED, 5),
                          (c.StageState.RECOVERY_REQUIRED, 7)):
            item = dataclasses.replace(base, outcome=state, rc=rc, alarm_severity="critical")
            for event, permitted in (("unknown_writes", ()),
                                     ("recovery_required", (c.StageState.RECOVERY_REQUIRED,)),
                                     ("deadline_missed", (c.StageState.RECOVERY_REQUIRED, c.StageState.DEADLINE_MISSED))):
                if state not in permitted:
                    with self.subTest(state=state, event=event):
                        self.assertFalse(c.validate_stage_result(dataclasses.replace(item, concurrent_events=(event,)), **ctx))

    def test_source_classifier_rejects_contradictory_status(self):
        self.assertFalse(c.source_reconstruction_ready(dataclasses.replace(self.certificate(), publish_ready=1)))


if __name__ == "__main__":
    unittest.main()
