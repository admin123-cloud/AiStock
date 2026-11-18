"""Offline post-close contract values. Nothing here issues a write permit.

The expected certificate digest must come from an independently trusted record;
a digest calculated from the object under inspection proves no authenticity.
Manifest validation checks shape, not predicates, permits or evidence truth.
Cleanup prerequisites are unsupported and deny-only; no positive authorization
chain, lock, service-side hard deadline, or production write is implemented.
"""
import hashlib
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum, IntEnum
from zoneinfo import ZoneInfo


class Purpose(Enum):
    INTRADAY_PROVISIONAL = "intraday_provisional"
    AFTERCLOSE_CLEANUP = "afterclose_cleanup"
    AFTERCLOSE_AUTHORITATIVE = "afterclose_authoritative"
    RECOVERY = "recovery"
    OPERATIONAL_EVIDENCE = "operational_evidence"


class StageState(Enum):
    INTRADAY = "INTRADAY"
    DRAINING = "DRAINING"
    INTRADAY_CLOSED = "INTRADAY_CLOSED"
    PREPARED = "PREPARED"
    BACKED_UP = "BACKED_UP"
    CLEANING = "CLEANING"
    CLEANED = "CLEANED"
    IMPORTING = "IMPORTING"
    VERIFYING = "VERIFYING"
    CERTIFIED = "CERTIFIED"
    BLOCKED = "BLOCKED"
    SOURCE_FAILED = "SOURCE_FAILED"
    DEGRADED = "DEGRADED"
    DEADLINE_MISSED = "DEADLINE_MISSED"
    UNKNOWN_WRITES = "UNKNOWN_WRITES"
    IMPORT_FAILED = "IMPORT_FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    TERMINAL_REJECTED_GAPS = "TERMINAL_REJECTED_GAPS"
    NO_OP_CLOSED = "NO_OP_CLOSED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class ReturnCode(IntEnum):
    COMPLETED = 0
    FAILED = 1
    BLOCKED = 2
    SOURCE_FAILED = 3
    DEGRADED = 4
    DEADLINE_MISSED = 5
    UNKNOWN_WRITES = 6
    RECOVERY_REQUIRED = 7


CLEANUP_OBJECTS = (
    ("stock.kline_minute_5", "delete", "datetime"),
    ("stock.kline_minute_15", "delete", "datetime"),
    ("stock.kline_minute_30", "delete", "datetime"),
    ("stock.kline_minute_60", "delete", "datetime"),
    ("stock.intraday_market_snapshot", "delete", "snapshot_ts"),
    ("stock.intraday_index_snapshot", "delete", "snapshot_ts"),
    ("stock.kline_daily_intraday", "delete", "trade_date"),
    ("runtime/market_mood/YYYYMMDD.json", "invalidate_realtime", "date"),
)


def day_window(day):
    if type(day) is not date:
        raise ValueError("a date is required")
    zone = ZoneInfo("Asia/Shanghai")
    return (datetime.combine(day, time.min, zone),
            datetime.combine(day + timedelta(days=1), time.min, zone))


def in_day_window(instant, day):
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    start, end = day_window(day)
    return start <= instant < end


def cleanup_operation_allowed(target, operation):
    """Scope check only, not authorization or a substitute for permits."""
    return any(target == name and operation == op for name, op, _ in CLEANUP_OBJECTS)


@dataclass(frozen=True)
class PlanManifest:
    target_date: date
    plan_id: str
    generation: int
    approval_id: str
    policy_decision_ref: str
    schema_version: int = 1
    timezone: str = "Asia/Shanghai"
    operation_permit_refs: tuple = ()
    target_schema_fingerprints: tuple = ()
    predicate_digest: str = ""
    observed_raw_keys_digest: str = ""
    observed_final_keys_digest: str = ""
    observed_payload_digest: str = ""
    identity_version: str = ""
    expected_keys_digest: str = ""
    exception_ledger_ref: str = ""
    excluded_objects: tuple = ()
    certificate_digests: tuple = ()
    non_target_sentinel_digest: str = ""
    delete_start_latest: datetime = None
    clean_deadline: datetime = None
    import_not_before: datetime = None
    lease_budget: timedelta = None
    recovery_strategy: str = ""
    read_impact: str = ""
    alarm_owner: str = ""
    cleanup_objects: tuple = CLEANUP_OBJECTS
    scope_kind: str = "full_day_all_raw"
    window_start: datetime = None
    window_end: datetime = None

    def __post_init__(self):
        _primitive(self)


@dataclass(frozen=True)
class Certificate:
    target_date: date
    plan_id: str
    generation: int
    approval_id: str
    issued_at: datetime
    expires_at: datetime
    issuer: str
    evidence_refs: tuple
    schema_digest: str

    def __post_init__(self):
        _primitive(self)


@dataclass(frozen=True)
class SourceCertificate(Certificate):
    """publish_ready is a necessary declaration, not publication permission.

    Publication also requires an independently validated PublishCertificate
    and dependency chain; neither is established by this source certificate.
    """
    source_digest: str
    accepted_keys_digest: str
    rejected_keys_digest: str
    coverage_status: str
    source_task_success: bool
    reconstruction_ready: bool
    publish_ready: bool


@dataclass(frozen=True)
class BackupCertificate(Certificate):
    backup_digest: str
    keys_digest: str
    baseline_digest: str
    recovery_evidence_ref: str
    recovery_verified: bool


@dataclass(frozen=True)
class CloseCertificate(Certificate):
    attempts_digest: str
    server_evidence_digest: str
    fencing_evidence_ref: str
    unresolved_writes: int
    closed_at: datetime


@dataclass(frozen=True)
class CleanCertificate(Certificate):
    manifest_digest: str
    source_digest: str
    backup_digest: str
    close_digest: str
    mutation_evidence_digest: str
    residual_digest: str
    completed_at: datetime


@dataclass(frozen=True)
class PublishCertificate(Certificate):
    manifest_digest: str
    clean_digest: str
    source_digest: str
    verification_digest: str
    coverage_status: str
    published_at: datetime


@dataclass(frozen=True)
class StageResult:
    """Offline snapshot of plan-wide facts, not a write authorization.

    write_attempted is cumulative across business mutations in this plan and
    generation, and becomes true when a business mutation is actually issued
    to its executor, not on planning, entry recording or acknowledgement.
    CLEANING/IMPORTING may describe pre-issue entry. A nonempty stage_entry_ref
    there is only a claimed persistent not-ready entry, not proof of persistence.
    Consumers must persist entry before planned and the issued intent before
    dispatch, fail closed on record failure and independently validate all write
    gates. A crash between issued intent and dispatch has an uncertain outcome;
    the journal, not this boolean alone, must drive conservative recovery.
    """
    target_date: date
    plan_id: str
    generation: int
    outcome: StageState
    reason_code: str
    rc: int
    source_attempted: bool
    source_completed: bool
    write_attempted: bool
    writes_known: bool
    accepted_keys: int
    rejected_keys: int
    exception_ledger_ref: str
    coverage_status: str
    certificate_ref: str
    retry_safe: bool
    alarm_severity: str
    concurrent_events: tuple = ()
    stage_entry_ref: str = None

    def __post_init__(self):
        _primitive(self)


def _primitive(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _primitive(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {"microseconds": ((value.days * 86400 + value.seconds) * 1000000 + value.microseconds)}
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise TypeError("unsupported or mutable contract value")


def canonical_json(value):
    return json.dumps(_primitive(value), sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def digest_object(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def validate_manifest(manifest):
    """Structural completeness only. Never confers a permit or validates evidence."""
    try:
        m = manifest
        if (type(m) is not PlanManifest or type(m.target_date) is not date or
                type(m.generation) is not int or m.generation < 1 or m.schema_version != 1 or
                m.timezone != "Asia/Shanghai" or m.cleanup_objects != CLEANUP_OBJECTS or
                m.scope_kind != "full_day_all_raw"):
            return False
        strings = (m.plan_id, m.approval_id, m.policy_decision_ref, m.identity_version,
                   m.exception_ledger_ref, m.recovery_strategy, m.read_impact, m.alarm_owner)
        if not all(isinstance(x, str) and x.strip() for x in strings):
            return False
        digest_values = (m.predicate_digest, m.observed_raw_keys_digest, m.observed_final_keys_digest,
                         m.observed_payload_digest, m.expected_keys_digest, m.non_target_sentinel_digest)
        if not all(_digest(x) for x in digest_values):
            return False
        permits = dict(m.operation_permit_refs)
        if (len(m.operation_permit_refs) != 4 or set(permits) != {"delete", "insert", "invalidate", "recovery"} or
                not all(isinstance(v, str) and v.strip() for v in permits.values())):
            return False
        schemas = dict(m.target_schema_fingerprints)
        if (len(m.target_schema_fingerprints) != 7 or set(schemas) != {x[0] for x in CLEANUP_OBJECTS[:-1]} or
                not all(_digest(v) for v in schemas.values())):
            return False
        certs = dict(m.certificate_digests)
        if (len(m.certificate_digests) != 3 or set(certs) != {"source", "backup", "close"} or
                not all(_digest(v) for v in certs.values())):
            return False
        required_exclusions = {"stock.kline_minute_1", "stock.kline_daily", "account_objects"}
        if not isinstance(m.excluded_objects, tuple) or not required_exclusions.issubset(set(m.excluded_objects)):
            return False
        start, end = day_window(m.target_date)
        if not all(_aware(t) for t in (m.window_start, m.window_end, m.delete_start_latest,
                                        m.clean_deadline, m.import_not_before)):
            return False
        if m.window_start != start or m.window_end != end:
            return False
        expected_deadline = datetime.combine(m.target_date, time(16, 15), start.tzinfo)
        if (m.clean_deadline != expected_deadline or m.import_not_before < expected_deadline or
                not start <= m.delete_start_latest < expected_deadline or
                not isinstance(m.lease_budget, timedelta) or m.lease_budget <= timedelta(0)):
            return False
        _primitive(m)
        return True
    except (TypeError, ValueError, OverflowError, AttributeError, KeyError):
        return False


def validate_stage_result(result, *, target_date, plan_id, generation):
    """Validate a conservative v1 outcome/rc/facts matrix, not evidence truth.

    Unknown writes dominate all other events. Concurrent event codes retain
    additional facts; aggregation and explicit legacy CLI mapping belong to a
    future adapter. NO_OP_CLOSED requires independently authenticated calendar
    evidence in that adapter, not merely this structurally valid result.
    """
    if type(result) is not StageResult or type(result.outcome) is not StageState:
        return False
    if (type(target_date) is not date or type(result.target_date) is not date or
            result.target_date != target_date or not isinstance(plan_id, str) or not plan_id.strip() or
            result.plan_id != plan_id or type(generation) is not int or generation < 1 or
            type(result.generation) is not int or result.generation != generation):
        return False
    if (type(result.rc) is not int or result.rc not in range(8) or
            any(type(getattr(result, k)) is not bool for k in
                ("source_attempted", "source_completed", "write_attempted", "writes_known", "retry_safe")) or
            any(type(getattr(result, k)) is not int or getattr(result, k) < 0 for k in ("accepted_keys", "rejected_keys")) or
            not isinstance(result.reason_code, str) or not result.reason_code.strip() or
            type(result.coverage_status) is not str or
            type(result.alarm_severity) is not str or
            type(result.exception_ledger_ref) is not str or
            (result.stage_entry_ref is not None and
             (type(result.stage_entry_ref) is not str or not result.stage_entry_ref.strip())) or
            (result.certificate_ref is not None and
             (type(result.certificate_ref) is not str or not result.certificate_ref.strip())) or
            type(result.concurrent_events) is not tuple or
            any(type(event) is not str or event not in
                ("source_failed", "deadline_missed", "recovery_required", "blocked", "degraded", "unknown_writes")
                for event in result.concurrent_events) or
            len(set(result.concurrent_events)) != len(result.concurrent_events)):
        return False
    if result.source_completed and not result.source_attempted:
        return False
    if result.rejected_keys and not result.exception_ledger_ref.strip():
        return False
    if not result.source_attempted and (result.accepted_keys or result.rejected_keys):
        return False
    if result.coverage_status == "complete" and not result.source_completed:
        return False
    # Every state is explicitly listed. This is intentionally narrower than
    # accepting all enum values with rc=0; new states default to rejection.
    states = {
        StageState.INTRADAY: (0, {"unknown"}, {"none", "warning"}),
        StageState.DRAINING: (0, {"unknown"}, {"none", "warning"}),
        StageState.INTRADAY_CLOSED: (0, {"unknown"}, {"none", "warning"}),
        StageState.PREPARED: (0, {"unknown", "complete"}, {"none", "warning"}),
        StageState.BACKED_UP: (0, {"unknown", "complete"}, {"none", "warning"}),
        StageState.CLEANING: (0, {"complete"}, {"none", "warning"}),
        StageState.CLEANED: (0, {"complete"}, {"none", "warning"}),
        StageState.IMPORTING: (0, {"complete"}, {"none", "warning"}),
        StageState.VERIFYING: (0, {"complete"}, {"none", "warning"}),
        StageState.CERTIFIED: (0, {"complete"}, {"none", "warning", "critical"}),
        StageState.NO_OP_CLOSED: (0, {"closed"}, {"none"}),
        StageState.TERMINAL_REJECTED_GAPS: (0, {"blocked", "all_rejected"}, {"warning", "critical"}),
        StageState.EXECUTION_FAILED: (1, {"unknown", "blocked", "complete"}, {"warning", "critical"}),
        StageState.IMPORT_FAILED: (1, {"unknown", "blocked", "complete"}, {"warning", "critical"}),
        StageState.BLOCKED: (2, {"unknown", "blocked", "complete"}, {"warning", "critical"}),
        StageState.SOURCE_FAILED: (3, {"unknown", "blocked"}, {"warning", "critical"}),
        StageState.DEGRADED: (4, {"unknown", "blocked", "with_exceptions"}, {"warning", "critical"}),
        StageState.DEADLINE_MISSED: (5, {"unknown", "blocked", "complete"}, {"warning", "critical"}),
        StageState.UNKNOWN_WRITES: (6, {"unknown", "blocked", "complete", "with_exceptions"}, {"critical"}),
        StageState.RECOVERY_REQUIRED: (7, {"unknown", "blocked", "complete", "with_exceptions"}, {"critical"}),
    }
    rule = states.get(result.outcome)
    if rule is None or result.rc != rule[0]:
        return False
    if result.coverage_status not in rule[1] or result.alarm_severity not in rule[2]:
        return False
    if (result.outcome is StageState.UNKNOWN_WRITES) != (not result.writes_known):
        return False
    if result.outcome is StageState.UNKNOWN_WRITES and (result.retry_safe or not result.write_attempted):
        return False
    if result.outcome is StageState.RECOVERY_REQUIRED and result.retry_safe:
        return False
    if result.retry_safe and result.write_attempted:
        return False
    if result.outcome in (StageState.CLEANED, StageState.VERIFYING) and not result.write_attempted:
        return False
    if result.outcome in (StageState.CLEANING, StageState.IMPORTING) and not result.write_attempted:
        if result.stage_entry_ref is None or result.retry_safe or result.certificate_ref is not None:
            return False
    if result.rc == 0 and result.concurrent_events:
        return False
    if result.outcome is StageState.TERMINAL_REJECTED_GAPS and (
            not result.source_attempted or not result.source_completed or result.accepted_keys != 0 or
            result.rejected_keys == 0 or result.retry_safe or result.certificate_ref is not None):
        return False
    if result.outcome is StageState.CERTIFIED and (not result.source_completed or
            result.rejected_keys != 0 or result.certificate_ref is None or result.retry_safe):
        return False
    if result.outcome is StageState.NO_OP_CLOSED and (result.reason_code != "calendar_closed" or
            result.source_attempted or result.source_completed or result.write_attempted or
            result.accepted_keys or result.rejected_keys or result.retry_safe or result.certificate_ref is not None):
        return False
    if "unknown_writes" in result.concurrent_events and result.outcome is not StageState.UNKNOWN_WRITES:
        return False
    if "recovery_required" in result.concurrent_events and result.outcome not in (StageState.UNKNOWN_WRITES, StageState.RECOVERY_REQUIRED):
        return False
    if "deadline_missed" in result.concurrent_events and result.outcome not in (StageState.UNKNOWN_WRITES, StageState.RECOVERY_REQUIRED, StageState.DEADLINE_MISSED):
        return False
    return True


def _source_status_valid(cert):
    if cert.coverage_status not in ("complete", "with_exceptions", "blocked", "all_rejected"):
        return False
    if any(type(x) is not bool for x in (cert.source_task_success, cert.reconstruction_ready, cert.publish_ready)):
        return False
    return not ((cert.reconstruction_ready or cert.publish_ready) and
                (cert.coverage_status != "complete" or not cert.source_task_success or not cert.reconstruction_ready))


def validate_certificate(cert, *, target_date, plan_id, generation, approval_id,
                         expected_digest, trusted_issuers, now):
    """Validate against externally pinned identity, issuer and content digest.

    This is a structural/integrity check, not proof that evidence was reviewed,
    a signature, a permit, a lock, or a safe database operation.
    """
    try:
        if type(cert) not in (SourceCertificate, BackupCertificate, CloseCertificate, CleanCertificate, PublishCertificate):
            return False
        if (type(target_date) is not date or cert.target_date != target_date or
                type(generation) is not int or generation < 1 or type(cert.generation) is not int or
                cert.generation != generation or not isinstance(plan_id, str) or not plan_id.strip() or
                cert.plan_id != plan_id or not isinstance(approval_id, str) or not approval_id.strip() or
                cert.approval_id != approval_id):
            return False
        if (not isinstance(trusted_issuers, tuple) or cert.issuer not in trusted_issuers or
                not isinstance(cert.issuer, str) or not cert.issuer.strip() or
                not isinstance(cert.evidence_refs, tuple) or not cert.evidence_refs or
                not all(isinstance(ref, str) and ref.strip() for ref in cert.evidence_refs)):
            return False
        if not all(_aware(t) for t in (now, cert.issued_at, cert.expires_at)):
            return False
        if not cert.issued_at <= now < cert.expires_at or not _digest(expected_digest):
            return False
        digest_fields = {
            SourceCertificate: ("schema_digest", "source_digest", "accepted_keys_digest", "rejected_keys_digest"),
            BackupCertificate: ("schema_digest", "backup_digest", "keys_digest", "baseline_digest"),
            CloseCertificate: ("schema_digest", "attempts_digest", "server_evidence_digest"),
            CleanCertificate: ("schema_digest", "manifest_digest", "source_digest", "backup_digest", "close_digest", "mutation_evidence_digest", "residual_digest"),
            PublishCertificate: ("schema_digest", "manifest_digest", "clean_digest", "source_digest", "verification_digest"),
        }
        if not all(_digest(getattr(cert, key)) for key in digest_fields[type(cert)]):
            return False
        if isinstance(cert, SourceCertificate):
            if not _source_status_valid(cert):
                return False
        if isinstance(cert, BackupCertificate):
            if cert.recovery_verified is not True or not isinstance(cert.recovery_evidence_ref, str) or not cert.recovery_evidence_ref.strip():
                return False
        if isinstance(cert, CloseCertificate):
            if type(cert.unresolved_writes) is not int or cert.unresolved_writes != 0 or not isinstance(cert.fencing_evidence_ref, str) or not cert.fencing_evidence_ref.strip() or not _aware(cert.closed_at):
                return False
            start, _ = day_window(target_date)
            cutoff = datetime.combine(target_date, time(15, 5), start.tzinfo)
            if not start <= cert.closed_at < cutoff or cert.closed_at > cert.issued_at:
                return False
        if isinstance(cert, (CleanCertificate, PublishCertificate)):
            stamp = cert.completed_at if isinstance(cert, CleanCertificate) else cert.published_at
            if not _aware(stamp):
                return False
            start, _ = day_window(target_date)
            deadline = datetime.combine(target_date, time(16, 15), start.tzinfo)
            if stamp > cert.issued_at:
                return False
            if isinstance(cert, CleanCertificate) and not start <= stamp < deadline:
                return False
            if isinstance(cert, PublishCertificate) and stamp < deadline:
                return False
        if isinstance(cert, PublishCertificate) and cert.coverage_status not in ("complete", "with_exceptions"):
            return False
        return digest_object(cert) == expected_digest
    except (TypeError, ValueError, OverflowError, AttributeError, KeyError):
        return False


def source_reconstruction_ready(cert):
    """A source-only classification; it grants no clean or publication right."""
    return (type(cert) is SourceCertificate and _source_status_valid(cert) and cert.source_task_success is True and
            cert.reconstruction_ready is True and cert.coverage_status == "complete")


def cleanup_prerequisites_satisfied(manifest, source, backup, close):
    """Unsupported, deny-only API: never interpret False as permission to bypass.

    S1 cannot authenticate permits, independent evidence, dependencies or lock.
    """
    return False