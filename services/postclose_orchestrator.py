# -*- coding: utf-8 -*-
"""S8：盘后单一进程权威编排器（Close → Source → Backup → Clean(<16:15) → Daily → 5m → 15/30/60 → Verify → Publish）。

核心不变量：
1. 单一进程单一 ``LockContext``：禁止跨子进程各自建锁或按非目标域各自换源。
2. 固定阶段顺序与严格前提依赖：
   - 日历 ``closed`` → ``NO_OP_CLOSED (rc=0)``；``unknown`` → ``BLOCKED (rc=2)``；
   - 未通过 ``CloseCertificate`` / ``SourceCertificate`` / ``BackupCertificate`` → 零清理零写；
   - ``CleanCertificate`` 未严格于 ``< 16:15:00`` 签发 → 禁止进入任何导入阶段；
   - 导入时刻严格 ``>= manifest.import_not_before``（默认 ``16:15:00``）；
   - 源读取在 S5 冻结，导入阶段严禁联网换源；
   - 日线只写缺失键且重叠冲突 fail-closed；5m 全 D 重建；15/30/60 从冻结 5m
     按严格交易时段桶重算（绝不跨午休；任一成分 ``amount=None`` 则聚合额为 ``None``）；
   - 每批写前登记 ``WriteAdmission`` 并 ``lock_ctx.assert_held()``；
   - 全量读回校验通过后方签发 ``PublishCertificate`` 与 ``StageState.CERTIFIED (rc=0)``。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

from services.market_write_admission import AttemptState, WriteAdmission, WriteAttempt
from services.postclose_backup import (
    BackupBundle,
    MINUTE_BACKUP_TABLES,
    MinuteBackupReader,
    export_and_verify_minute_backup,
)
from services.postclose_cleanup import CleanupGateway, CleanupReport, run_postclose_cleanup
from services.postclose_contracts import (
    BackupCertificate,
    CLEANUP_OBJECTS,
    CleanCertificate,
    CloseCertificate,
    PlanManifest,
    PublishCertificate,
    Purpose,
    ReturnCode,
    SourceCertificate,
    StageResult,
    StageState,
    digest_object,
    validate_certificate,
    validate_manifest,
)
from services.postclose_journal import AttemptJournal
from services.postclose_source_prepare import (
    LocalFileSnapshot,
    PreparedSourceBundle,
    canonical_sha256,
    prepare_postclose_sources,
    verify_candidate_digest,
)
from utils.tdx_lc5 import STANDARD_5M_BAR_ENDS

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# 严格盘中时段分桶（绝不跨 11:30–13:05 午休边界）：
# 15m=16 桶、30m=8 桶、60m=4 桶，各桶由连续 3 / 6 / 12 根已对齐的 5m bar 组成。
SESSION_5M_SLOTS: tuple[str, ...] = tuple(
    t.strftime("%H:%M") for t in sorted(STANDARD_5M_BAR_ENDS)
)
DERIVED_PERIOD_GROUP_SIZES: dict[str, int] = {
    "15m": 3,
    "30m": 6,
    "60m": 12,
}
DERIVED_PERIOD_TABLES: dict[str, str] = {
    "15m": "stock.kline_minute_15",
    "30m": "stock.kline_minute_30",
    "60m": "stock.kline_minute_60",
}


class PostcloseImportGateway(Protocol):
    """盘后正式导入与独立验证网关（可注入 fake，无全局默认可写连接）。"""

    def fetch_existing_daily_rows(self, target_date: date) -> list[dict[str, Any]]:
        ...

    def insert_daily_rows(self, rows: Sequence[Mapping[str, Any]], *, attempt_id: str) -> int:
        ...

    def verify_daily_rows(self, target_date: date) -> list[dict[str, Any]]:
        ...

    def insert_minute_rows(
        self, table: str, rows: Sequence[Mapping[str, Any]], *, attempt_id: str
    ) -> int:
        ...

    def verify_minute_rows(self, table: str, target_date: date) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class OrchestrationResult:
    target_date: date
    plan_id: str
    generation: int
    source_bundle: Optional[PreparedSourceBundle]
    backup_bundle: Optional[BackupBundle]
    cleanup_report: Optional[CleanupReport]
    publish_certificate: Optional[PublishCertificate]
    stage_result: StageResult
    daily_inserted: int
    minute_5m_inserted: int
    derived_inserted: dict[str, int]


def _to_bj(dt: datetime) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(BEIJING_TZ)


def aggregate_derived_bars_from_5m(
    target_date: date,
    bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    period: str,
) -> list[dict[str, Any]]:
    """按严格 A 股交易时段分桶将 48 根 5m bar 聚合为 15m/30m/60m（零跨午休、NULL 传播）。"""
    if period not in DERIVED_PERIOD_GROUP_SIZES:
        raise ValueError(f"unsupported derived period: {period!r}")
    group_size = DERIVED_PERIOD_GROUP_SIZES[period]
    slot_index = {slot: idx for idx, slot in enumerate(SESSION_5M_SLOTS)}
    expected_len = len(SESSION_5M_SLOTS)
    target_iso = target_date.isoformat()

    out_rows: list[dict[str, Any]] = []
    for code in sorted(bars_by_code):
        bars = list(bars_by_code[code])
        if len(bars) != expected_len:
            raise ValueError(f"{code}: expected {expected_len} 5m bars, got {len(bars)}")

        by_slot: dict[str, Mapping[str, Any]] = {}
        for bar in bars:
            hhmm = str(bar["datetime"])[11:16]
            if hhmm not in slot_index:
                raise ValueError(f"{code}: invalid 5m slot {hhmm!r}")
            by_slot[hhmm] = bar
        if len(by_slot) != expected_len:
            raise ValueError(f"{code}: incomplete 5m slots for period aggregation")

        ordered = [by_slot[slot] for slot in SESSION_5M_SLOTS]
        # 严格校验午休边界：前 24 根在 09:35..11:30，后 24 根在 13:05..15:00
        morning_end = ordered[23]
        afternoon_start = ordered[24]
        if str(morning_end["datetime"])[11:16] != "11:30" or str(afternoon_start["datetime"])[11:16] != "13:05":
            raise ValueError(f"{code}: session split sanity check failed")

        for offset in range(0, expected_len, group_size):
            bucket = ordered[offset:offset + group_size]
            # 严禁跨上午/下午午休边界（morning idx 0..23 vs afternoon idx 24..47）
            if offset < 24 and (offset + group_size) > 24:
                raise RuntimeError("bucket crossed lunch boundary")

            end_slot = SESSION_5M_SLOTS[offset + group_size - 1]
            # 任一成分 bar 的 amount 为 None（NULL）→ 整个聚合桶保持 None
            if any(b.get("amount") is None for b in bucket):
                agg_amount: Optional[float] = None
            else:
                agg_amount = float(sum(float(b["amount"]) for b in bucket))

            out_rows.append(
                {
                    "code": code,
                    "datetime": f"{target_iso} {end_slot}:00",
                    "open": float(bucket[0]["open"]),
                    "high": float(max(float(b["high"]) for b in bucket)),
                    "low": float(min(float(b["low"]) for b in bucket)),
                    "close": float(bucket[-1]["close"]),
                    "volume": float(sum(float(b["volume"]) for b in bucket)),
                    "amount": agg_amount,
                }
            )
    return out_rows


def _safe_stage_result(
    manifest: PlanManifest,
    *,
    outcome: StageState,
    reason_code: str,
    rc: int,
    source_attempted: bool,
    source_completed: bool,
    write_attempted: bool,
    writes_known: bool,
    coverage_status: str,
    retry_safe: bool,
    alarm_severity: str,
    certificate_ref: Optional[str] = None,
    accepted_keys: int = 0,
    rejected_keys: int = 0,
    exception_ledger_ref: str = "",
    concurrent_events: tuple[str, ...] = (),
    stage_entry_ref: Optional[str] = None,
) -> StageResult:
    return StageResult(
        target_date=manifest.target_date,
        plan_id=manifest.plan_id,
        generation=manifest.generation,
        outcome=outcome,
        reason_code=reason_code,
        rc=rc,
        source_attempted=source_attempted,
        source_completed=source_completed,
        write_attempted=write_attempted,
        writes_known=writes_known,
        accepted_keys=accepted_keys,
        rejected_keys=rejected_keys,
        exception_ledger_ref=exception_ledger_ref,
        coverage_status=coverage_status,
        certificate_ref=certificate_ref,
        retry_safe=retry_safe,
        alarm_severity=alarm_severity,
        concurrent_events=concurrent_events,
        stage_entry_ref=stage_entry_ref,
    )


def run_postclose_orchestration(
    *,
    manifest: PlanManifest,
    close_cert: CloseCertificate,
    trusted_issuers: tuple[str, ...],
    calendar_state: str,
    operation_permits: Mapping[str, str],
    schema_digest: str,
    # S5 inputs
    download_report: Optional[Mapping[str, Any]],
    file_snapshots: Sequence[LocalFileSnapshot],
    daily_rows: Sequence[Mapping[str, Any]],
    expected_minute_keys: Sequence[str],
    minute_bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    daily_source_mode: str = "local_lday",
    minute_fetch_errors: Optional[Mapping[str, str]] = None,
    accepted_exceptions: Sequence[str] = (),
    # S6 / S7 / S8 gateways & infrastructure
    backup_reader: MinuteBackupReader,
    backup_root: Path,
    cleanup_gateway: CleanupGateway,
    import_gateway: PostcloseImportGateway,
    journal: AttemptJournal,
    lock_ctx: Any,
    now_fn: Callable[[], datetime],
    admission: Optional[WriteAdmission] = None,
    mood_invalidator: Optional[Callable[[date, datetime], Mapping[str, Any]]] = None,
    dry_run: bool = True,
    price_tol: float = 0.01,
    qty_tol: float = 0.03,
    issuer: str = "postclose_orchestrator",
    certificate_ttl: timedelta = timedelta(hours=6),
) -> OrchestrationResult:
    """盘后单进程全链编排入口：先全证备清(<16:15)，后16:15+顺序导入与全集认证。"""
    now_bj = _to_bj(now_fn())

    # 0) 日历三态首闸：closed=NO_OP_CLOSED(rc0)，unknown/异常=BLOCKED(rc2)
    if calendar_state == "closed":
        sr = StageResult(
            target_date=manifest.target_date,
            plan_id=manifest.plan_id,
            generation=manifest.generation,
            outcome=StageState.NO_OP_CLOSED,
            reason_code="calendar_closed",
            rc=int(ReturnCode.COMPLETED),
            source_attempted=False,
            source_completed=False,
            write_attempted=False,
            writes_known=True,
            accepted_keys=0,
            rejected_keys=0,
            exception_ledger_ref="",
            coverage_status="closed",
            certificate_ref=None,
            retry_safe=False,
            alarm_severity="none",
        )
        return OrchestrationResult(
            target_date=manifest.target_date,
            plan_id=manifest.plan_id,
            generation=manifest.generation,
            source_bundle=None,
            backup_bundle=None,
            cleanup_report=None,
            publish_certificate=None,
            stage_result=sr,
            daily_inserted=0,
            minute_5m_inserted=0,
            derived_inserted={},
        )

    if calendar_state != "trading":
        sr = _safe_stage_result(
            manifest,
            outcome=StageState.BLOCKED,
            reason_code=f"calendar_unknown_or_invalid:{calendar_state}",
            rc=int(ReturnCode.BLOCKED),
            source_attempted=False,
            source_completed=False,
            write_attempted=False,
            writes_known=True,
            coverage_status="blocked",
            retry_safe=True,
            alarm_severity="critical",
            concurrent_events=("blocked",),
        )
        return OrchestrationResult(
            manifest.target_date, manifest.plan_id, manifest.generation,
            None, None, None, None, sr, 0, 0, {},
        )

    # 1) 验证 PlanManifest 基础完整性（先占位或核对既有证书摘要）+ CloseCertificate + 许可
    if not validate_manifest(manifest):
        sr = _safe_stage_result(
            manifest,
            outcome=StageState.BLOCKED,
            reason_code="invalid_plan_manifest",
            rc=int(ReturnCode.BLOCKED),
            source_attempted=False,
            source_completed=False,
            write_attempted=False,
            writes_known=True,
            coverage_status="blocked",
            retry_safe=True,
            alarm_severity="critical",
            concurrent_events=("blocked",),
        )
        return OrchestrationResult(
            manifest.target_date, manifest.plan_id, manifest.generation,
            None, None, None, None, sr, 0, 0, {},
        )

    expected_permits = dict(manifest.operation_permit_refs)
    if (
        operation_permits.get("delete") != expected_permits.get("delete")
        or operation_permits.get("insert") != expected_permits.get("insert")
        or operation_permits.get("invalidate") != expected_permits.get("invalidate")
    ):
        sr = _safe_stage_result(
            manifest,
            outcome=StageState.BLOCKED,
            reason_code="operation_permit_mismatch",
            rc=int(ReturnCode.BLOCKED),
            source_attempted=False,
            source_completed=False,
            write_attempted=False,
            writes_known=True,
            coverage_status="blocked",
            retry_safe=True,
            alarm_severity="critical",
            concurrent_events=("blocked",),
        )
        return OrchestrationResult(
            manifest.target_date, manifest.plan_id, manifest.generation,
            None, None, None, None, sr, 0, 0, {},
        )

    manifest_certs = dict(manifest.certificate_digests)
    if not validate_certificate(
        close_cert,
        target_date=manifest.target_date,
        plan_id=manifest.plan_id,
        generation=manifest.generation,
        approval_id=manifest.approval_id,
        expected_digest=manifest_certs.get("close", ""),
        trusted_issuers=trusted_issuers,
        now=now_bj,
    ) or close_cert.unresolved_writes != 0:
        sr = _safe_stage_result(
            manifest,
            outcome=StageState.BLOCKED,
            reason_code="invalid_close_certificate",
            rc=int(ReturnCode.BLOCKED),
            source_attempted=False,
            source_completed=False,
            write_attempted=False,
            writes_known=True,
            coverage_status="blocked",
            retry_safe=True,
            alarm_severity="critical",
            concurrent_events=("blocked",),
        )
        return OrchestrationResult(
            manifest.target_date, manifest.plan_id, manifest.generation,
            None, None, None, None, sr, 0, 0, {},
        )

    # 2) S5：盘后多源准备与冻结（全过程零写入正式表）
    src_bundle = prepare_postclose_sources(
        target_date=manifest.target_date,
        plan_id=manifest.plan_id,
        generation=manifest.generation,
        approval_id=manifest.approval_id,
        schema_digest=schema_digest,
        download_report=download_report,
        file_snapshots=file_snapshots,
        daily_rows=daily_rows,
        expected_minute_keys=expected_minute_keys,
        minute_bars_by_code=minute_bars_by_code,
        now=now_bj,
        issuer=issuer,
        daily_source_mode=daily_source_mode,
        accepted_exceptions=accepted_exceptions,
        minute_fetch_errors=minute_fetch_errors,
        exception_ledger_ref=manifest.exception_ledger_ref,
        certificate_ttl=certificate_ttl,
    )
    if src_bundle.certificate is None or not src_bundle.minute.reconstruction_ready:
        # 全拒绝/缺源/不可重建 → 阻断整个日重建，禁止删除旧数据
        if src_bundle.stage_result.outcome is StageState.TERMINAL_REJECTED_GAPS:
            blocked_sr = _safe_stage_result(
                manifest,
                outcome=StageState.BLOCKED,
                reason_code="terminal_rejected_gaps_blocks_cleanup",
                rc=int(ReturnCode.BLOCKED),
                source_attempted=True,
                source_completed=True,
                write_attempted=False,
                writes_known=True,
                coverage_status="blocked",
                retry_safe=False,
                alarm_severity="critical",
                accepted_keys=src_bundle.stage_result.accepted_keys,
                rejected_keys=src_bundle.stage_result.rejected_keys,
                exception_ledger_ref=src_bundle.stage_result.exception_ledger_ref,
                concurrent_events=("blocked",),
            )
        else:
            blocked_sr = src_bundle.stage_result
        return OrchestrationResult(
            manifest.target_date, manifest.plan_id, manifest.generation,
            src_bundle, None, None, None, blocked_sr, 0, 0, {},
        )

    # 绑定当前源证书摘要供本次执行链内部一致性校对（如与 manifest 预置不一致立即 fail-closed）
    if manifest_certs.get("source") != digest_object(src_bundle.certificate):
        sr = _safe_stage_result(
            manifest,
            outcome=StageState.BLOCKED,
            reason_code="source_certificate_digest_mismatch_manifest",
            rc=int(ReturnCode.BLOCKED),
            source_attempted=True,
            source_completed=True,
            write_attempted=False,
            writes_known=True,
            coverage_status="blocked",
            retry_safe=True,
            alarm_severity="critical",
            concurrent_events=("blocked",),
        )
        return OrchestrationResult(
            manifest.target_date, manifest.plan_id, manifest.generation,
            src_bundle, None, None, None, sr, 0, 0, {},
        )

    # 3) 获取并持有单一 LockContext（先持锁重验基线与分钟备份）
    acquired_here = False
    try:
        lock_ctx.acquire()
        acquired_here = True
    except Exception as exc:
        sr = _safe_stage_result(
            manifest,
            outcome=StageState.BLOCKED,
            reason_code=f"lock_acquire_failed:{type(exc).__name__}",
            rc=int(ReturnCode.BLOCKED),
            source_attempted=True,
            source_completed=True,
            write_attempted=False,
            writes_known=True,
            coverage_status="blocked",
            retry_safe=True,
            alarm_severity="critical",
            concurrent_events=("blocked",),
        )
        return OrchestrationResult(
            manifest.target_date, manifest.plan_id, manifest.generation,
            src_bundle, None, None, None, sr, 0, 0, {},
        )

    writes_uncertain = False
    backup_bundle: Optional[BackupBundle] = None
    cleanup_report: Optional[CleanupReport] = None
    try:
        # 4) S6：四张分钟表写前备份与孤立可恢复核验
        backup_bundle = export_and_verify_minute_backup(
            reader=backup_reader,
            backup_root=backup_root,
            target_date=manifest.target_date,
            plan_id=manifest.plan_id,
            generation=manifest.generation,
            approval_id=manifest.approval_id,
            schema_digest=schema_digest,
            now=_to_bj(now_fn()),
            issuer=issuer,
            certificate_ttl=certificate_ttl,
        )
        if backup_bundle.certificate is None or manifest_certs.get("backup") != digest_object(backup_bundle.certificate):
            reason = (
                "backup_certificate_digest_mismatch_manifest"
                if backup_bundle.certificate is not None
                else backup_bundle.stage_result.reason_code
            )
            sr = _safe_stage_result(
                manifest,
                outcome=StageState.BLOCKED,
                reason_code=reason,
                rc=int(ReturnCode.BLOCKED),
                source_attempted=True,
                source_completed=True,
                write_attempted=False,
                writes_known=True,
                coverage_status="blocked",
                retry_safe=True,
                alarm_severity="critical",
                concurrent_events=("blocked",),
            )
            return OrchestrationResult(
                manifest.target_date, manifest.plan_id, manifest.generation,
                src_bundle, backup_bundle, None, None, sr, 0, 0, {},
            )

        # dry_run 模式到此前置预览即安全返回，零清理零正式写入
        if dry_run:
            cleanup_preview = run_postclose_cleanup(
                manifest=manifest,
                source_cert=src_bundle.certificate,
                backup_cert=backup_bundle.certificate,
                close_cert=close_cert,
                trusted_issuers=trusted_issuers,
                calendar_state=calendar_state,
                operation_permits=operation_permits,
                gateway=cleanup_gateway,
                admission=admission,
                journal=journal,
                lock_ctx=lock_ctx,
                now_fn=now_fn,
                schema_digest=schema_digest,
                issuer=issuer,
                dry_run=True,
            )
            return OrchestrationResult(
                manifest.target_date,
                manifest.plan_id,
                manifest.generation,
                src_bundle,
                backup_bundle,
                cleanup_preview,
                None,
                cleanup_preview.stage_result,
                0,
                0,
                {},
            )

        # 5) S7：16:15 前精准清理与 CleanCertificate 签发
        cleanup_report = run_postclose_cleanup(
            manifest=manifest,
            source_cert=src_bundle.certificate,
            backup_cert=backup_bundle.certificate,
            close_cert=close_cert,
            trusted_issuers=trusted_issuers,
            calendar_state=calendar_state,
            operation_permits=operation_permits,
            gateway=cleanup_gateway,
            admission=admission,
            journal=journal,
            lock_ctx=lock_ctx,
            now_fn=now_fn,
            mood_invalidator=mood_invalidator,
            schema_digest=schema_digest,
            issuer=issuer,
            certificate_ttl=certificate_ttl,
            dry_run=False,
        )
        if not cleanup_report.stage_result.writes_known:
            writes_uncertain = True
        if cleanup_report.certificate is None:
            return OrchestrationResult(
                manifest.target_date, manifest.plan_id, manifest.generation,
                src_bundle, backup_bundle, cleanup_report, None,
                cleanup_report.stage_result, 0, 0, {},
            )

        # 6) 16:15+ 导入门槛核验 + 冻结候选不可变摘要复验 + CleanCertificate 时效核验
        import_now = _to_bj(now_fn())
        if import_now < manifest.import_not_before:
            sr = _safe_stage_result(
                manifest,
                outcome=StageState.BLOCKED,
                reason_code="import_attempted_before_1615",
                rc=int(ReturnCode.BLOCKED),
                source_attempted=True,
                source_completed=True,
                write_attempted=True,
                writes_known=True,
                coverage_status="complete",
                retry_safe=False,
                alarm_severity="critical",
                concurrent_events=("blocked",),
                stage_entry_ref=cleanup_report.stage_result.stage_entry_ref,
            )
            return OrchestrationResult(
                manifest.target_date, manifest.plan_id, manifest.generation,
                src_bundle, backup_bundle, cleanup_report, None, sr, 0, 0, {},
            )

        if not validate_certificate(
            cleanup_report.certificate,
            target_date=manifest.target_date,
            plan_id=manifest.plan_id,
            generation=manifest.generation,
            approval_id=manifest.approval_id,
            expected_digest=digest_object(cleanup_report.certificate),
            trusted_issuers=trusted_issuers,
            now=import_now,
        ):
            sr = _safe_stage_result(
                manifest,
                outcome=StageState.BLOCKED,
                reason_code="clean_certificate_expired_or_invalid",
                rc=int(ReturnCode.BLOCKED),
                source_attempted=True, source_completed=True,
                write_attempted=True, writes_known=True,
                coverage_status="complete", retry_safe=False, alarm_severity="critical",
                concurrent_events=("blocked",),
            )
            return OrchestrationResult(
                manifest.target_date, manifest.plan_id, manifest.generation,
                src_bundle, backup_bundle, cleanup_report, None, sr, 0, 0, {},
            )

        # 源冻结不可变复核（严禁中途换源/改数据）
        frozen_payload = {
            "target_date": manifest.target_date.isoformat(),
            "daily": src_bundle.daily.accepted_rows,
            "minute": src_bundle.minute.accepted_bars_by_code,
        }
        verify_candidate_digest(frozen_payload, src_bundle.candidate_digest)

        # 持久登记 IMPORTING 状态入口
        importing_planned = WriteAttempt(
            D=manifest.target_date,
            plan_id=manifest.plan_id,
            generation=manifest.generation,
            writer_id=issuer,
            purpose=Purpose.AFTERCLOSE_AUTHORITATIVE.value,
            attempt_id=f"{manifest.plan_id}:g{manifest.generation}:stage:IMPORTING",
            table="postclose_stage",
            operation="stage_entry_IMPORTING",
            key_digest=canonical_sha256({"target_date": manifest.target_date.isoformat()}),
            payload_digest=canonical_sha256({"stage": "IMPORTING", "target_date": manifest.target_date.isoformat()}),
            query_id=f"stage_importing_{manifest.plan_id}_g{manifest.generation}",
        )
        journal.append(importing_planned)
        journal.append(replace(importing_planned, state=AttemptState.ISSUED, issued_at=import_now))
        stage_ref = f"journal:{importing_planned.attempt_id}"

        pending_imports: dict[str, dict[str, Any]] = {}

        def _dispatch_import(attempt: WriteAttempt) -> None:
            meta = pending_imports[attempt.attempt_id]
            if attempt.table == "stock.kline_daily":
                meta["count"] = int(import_gateway.insert_daily_rows(meta["rows"], attempt_id=attempt.attempt_id))
            else:
                meta["count"] = int(import_gateway.insert_minute_rows(attempt.table, meta["rows"], attempt_id=attempt.attempt_id))

        active_import_admission = admission or WriteAdmission(journal, lock_ctx, _dispatch_import, now=now_fn)

        # 7a) 日线阶段（kline_daily）：只插缺失键，重叠键冲突立即 fail-closed 停止
        lock_ctx.assert_held()
        lock_ctx.heartbeat()
        existing_daily = {
            str(r["code"]).upper(): r
            for r in import_gateway.fetch_existing_daily_rows(manifest.target_date)
        }
        daily_to_insert: list[dict[str, Any]] = []
        for cand in src_bundle.daily.accepted_rows:
            code = cand["code"]
            if code in existing_daily:
                old = existing_daily[code]
                old_c = float(old.get("close") or 0)
                cand_c = float(cand["close"])
                if old_c <= 0 or abs(cand_c - old_c) / max(old_c, 1e-9) > price_tol:
                    sr = _safe_stage_result(
                        manifest,
                        outcome=StageState.IMPORT_FAILED,
                        reason_code=f"daily_overlap_conflict:{code}",
                        rc=int(ReturnCode.FAILED),
                        source_attempted=True, source_completed=True,
                        write_attempted=True, writes_known=True,
                        coverage_status="complete", retry_safe=False, alarm_severity="critical",
                        stage_entry_ref=stage_ref,
                    )
                    return OrchestrationResult(
                        manifest.target_date, manifest.plan_id, manifest.generation,
                        src_bundle, backup_bundle, cleanup_report, None, sr, 0, 0, {},
                    )
                continue
            daily_to_insert.append(cand)

        daily_inserted = 0
        if daily_to_insert:
            lock_ctx.assert_held()
            d_attempt = WriteAttempt(
                D=manifest.target_date,
                plan_id=manifest.plan_id,
                generation=manifest.generation,
                writer_id=issuer,
                purpose=Purpose.AFTERCLOSE_AUTHORITATIVE.value,
                attempt_id=f"{manifest.plan_id}:g{manifest.generation}:insert:kline_daily",
                table="stock.kline_daily",
                operation="insert",
                key_digest=canonical_sha256([r["code"] for r in daily_to_insert]),
                payload_digest=canonical_sha256(daily_to_insert),
                query_id=f"ins_daily_{manifest.plan_id}_g{manifest.generation}",
            )
            pending_imports[d_attempt.attempt_id] = {"rows": daily_to_insert, "count": 0}
            try:
                d_ack = active_import_admission.issue(d_attempt)
                daily_inserted = pending_imports[d_attempt.attempt_id]["count"]
            except Exception as exc:
                writes_uncertain = True
                sr = _safe_stage_result(
                    manifest,
                    outcome=StageState.UNKNOWN_WRITES,
                    reason_code=f"daily_insert_uncertain:{type(exc).__name__}",
                    rc=int(ReturnCode.UNKNOWN_WRITES),
                    source_attempted=True, source_completed=True,
                    write_attempted=True, writes_known=False,
                    coverage_status="complete", retry_safe=False, alarm_severity="critical",
                    concurrent_events=("unknown_writes",),
                    stage_entry_ref=stage_ref,
                )
                return OrchestrationResult(
                    manifest.target_date, manifest.plan_id, manifest.generation,
                    src_bundle, backup_bundle, cleanup_report, None, sr, 0, 0, {},
                )
            journal.append(replace(d_ack, state=AttemptState.VERIFIED, verified_at=_to_bj(now_fn())))

        verified_daily = import_gateway.verify_daily_rows(manifest.target_date)
        observed_daily_codes = {str(r["code"]).upper() for r in verified_daily}
        expected_daily_codes = {r["code"] for r in src_bundle.daily.accepted_rows}
        if observed_daily_codes != expected_daily_codes:
            sr = _safe_stage_result(
                manifest,
                outcome=StageState.IMPORT_FAILED,
                reason_code="daily_verification_mismatch",
                rc=int(ReturnCode.FAILED),
                source_attempted=True, source_completed=True,
                write_attempted=True, writes_known=True,
                coverage_status="complete", retry_safe=False, alarm_severity="critical",
                stage_entry_ref=stage_ref,
            )
            return OrchestrationResult(
                manifest.target_date, manifest.plan_id, manifest.generation,
                src_bundle, backup_bundle, cleanup_report, None, sr, daily_inserted, 0, {},
            )

        # 7b) 5m 全 D 重建（写入冻结候选全量 5m bar 并独立读回验证）
        m5_rows: list[dict[str, Any]] = []
        for code in sorted(src_bundle.minute.accepted_bars_by_code):
            m5_rows.extend(src_bundle.minute.accepted_bars_by_code[code])

        lock_ctx.assert_held()
        lock_ctx.heartbeat()
        m5_attempt = WriteAttempt(
            D=manifest.target_date,
            plan_id=manifest.plan_id,
            generation=manifest.generation,
            writer_id=issuer,
            purpose=Purpose.AFTERCLOSE_AUTHORITATIVE.value,
            attempt_id=f"{manifest.plan_id}:g{manifest.generation}:insert:kline_minute_5",
            table="stock.kline_minute_5",
            operation="insert",
            key_digest=canonical_sha256(sorted(src_bundle.minute.accepted_bars_by_code)),
            payload_digest=canonical_sha256(m5_rows),
            query_id=f"ins_5m_{manifest.plan_id}_g{manifest.generation}",
        )
        pending_imports[m5_attempt.attempt_id] = {"rows": m5_rows, "count": 0}
        try:
            m5_ack = active_import_admission.issue(m5_attempt)
            m5_inserted = pending_imports[m5_attempt.attempt_id]["count"]
        except Exception as exc:
            writes_uncertain = True
            sr = _safe_stage_result(
                manifest,
                outcome=StageState.UNKNOWN_WRITES,
                reason_code=f"minute_5m_insert_uncertain:{type(exc).__name__}",
                rc=int(ReturnCode.UNKNOWN_WRITES),
                source_attempted=True, source_completed=True,
                write_attempted=True, writes_known=False,
                coverage_status="complete", retry_safe=False, alarm_severity="critical",
                concurrent_events=("unknown_writes",),
                stage_entry_ref=stage_ref,
            )
            return OrchestrationResult(
                manifest.target_date, manifest.plan_id, manifest.generation,
                src_bundle, backup_bundle, cleanup_report, None, sr, daily_inserted, 0, {},
            )
        journal.append(replace(m5_ack, state=AttemptState.VERIFIED, verified_at=_to_bj(now_fn())))

        observed_5m = import_gateway.verify_minute_rows("stock.kline_minute_5", manifest.target_date)
        if len(observed_5m) != len(m5_rows) or {(r["code"], str(r["datetime"])[:19]) for r in observed_5m} != {(r["code"], r["datetime"]) for r in m5_rows}:
            sr = _safe_stage_result(
                manifest,
                outcome=StageState.IMPORT_FAILED,
                reason_code="minute_5m_verification_mismatch",
                rc=int(ReturnCode.FAILED),
                source_attempted=True, source_completed=True,
                write_attempted=True, writes_known=True,
                coverage_status="complete", retry_safe=False, alarm_severity="critical",
                stage_entry_ref=stage_ref,
            )
            return OrchestrationResult(
                manifest.target_date, manifest.plan_id, manifest.generation,
                src_bundle, backup_bundle, cleanup_report, None, sr, daily_inserted, m5_inserted, {},
            )

        # 7c) 15m / 30m / 60m 从冻结 5m 同源重算并逐表写入+独立全集验证
        derived_inserted: dict[str, int] = {}
        derived_digests: dict[str, str] = {}
        for period in ("15m", "30m", "60m"):
            table = DERIVED_PERIOD_TABLES[period]
            agg_rows = aggregate_derived_bars_from_5m(
                manifest.target_date, src_bundle.minute.accepted_bars_by_code, period
            )
            lock_ctx.assert_held()
            lock_ctx.heartbeat()
            p_attempt = WriteAttempt(
                D=manifest.target_date,
                plan_id=manifest.plan_id,
                generation=manifest.generation,
                writer_id=issuer,
                purpose=Purpose.AFTERCLOSE_AUTHORITATIVE.value,
                attempt_id=f"{manifest.plan_id}:g{manifest.generation}:insert:{table}",
                table=table,
                operation="insert",
                key_digest=canonical_sha256(sorted({r["code"] for r in agg_rows})),
                payload_digest=canonical_sha256(agg_rows),
                query_id=f"ins_{period}_{manifest.plan_id}_g{manifest.generation}",
            )
            pending_imports[p_attempt.attempt_id] = {"rows": agg_rows, "count": 0}
            try:
                p_ack = active_import_admission.issue(p_attempt)
                derived_inserted[period] = pending_imports[p_attempt.attempt_id]["count"]
            except Exception as exc:
                writes_uncertain = True
                sr = _safe_stage_result(
                    manifest,
                    outcome=StageState.UNKNOWN_WRITES,
                    reason_code=f"derived_insert_uncertain:{period}:{type(exc).__name__}",
                    rc=int(ReturnCode.UNKNOWN_WRITES),
                    source_attempted=True, source_completed=True,
                    write_attempted=True, writes_known=False,
                    coverage_status="complete", retry_safe=False, alarm_severity="critical",
                    concurrent_events=("unknown_writes",),
                    stage_entry_ref=stage_ref,
                )
                return OrchestrationResult(
                    manifest.target_date, manifest.plan_id, manifest.generation,
                    src_bundle, backup_bundle, cleanup_report, None, sr,
                    daily_inserted, m5_inserted, derived_inserted,
                )
            journal.append(replace(p_ack, state=AttemptState.VERIFIED, verified_at=_to_bj(now_fn())))

            obs_p = import_gateway.verify_minute_rows(table, manifest.target_date)
            obs_norm = sorted(
                (
                    {
                        "code": str(r["code"]),
                        "datetime": str(r["datetime"])[:19],
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["volume"]),
                        "amount": None if r.get("amount") is None else float(r["amount"]),
                    }
                    for r in obs_p
                ),
                key=lambda x: (x["code"], x["datetime"]),
            )
            if obs_norm != sorted(agg_rows, key=lambda x: (x["code"], x["datetime"])):
                sr = _safe_stage_result(
                    manifest,
                    outcome=StageState.EXECUTION_FAILED,
                    reason_code=f"derived_verification_mismatch:{period}",
                    rc=int(ReturnCode.FAILED),
                    source_attempted=True, source_completed=True,
                    write_attempted=True, writes_known=True,
                    coverage_status="complete", retry_safe=False, alarm_severity="critical",
                    stage_entry_ref=stage_ref,
                )
                return OrchestrationResult(
                    manifest.target_date, manifest.plan_id, manifest.generation,
                    src_bundle, backup_bundle, cleanup_report, None, sr,
                    daily_inserted, m5_inserted, derived_inserted,
                )
            derived_digests[period] = canonical_sha256(obs_norm)

        # 8) 全量独立校验闭合 → 签发 PublishCertificate + StageState.CERTIFIED (rc=0)
        published_bj = _to_bj(now_fn())
        verification_digest = canonical_sha256(
            {
                "daily_codes": sorted(observed_daily_codes),
                "m5_count": len(observed_5m),
                "derived_digests": derived_digests,
            }
        )
        pub_cert = PublishCertificate(
            target_date=manifest.target_date,
            plan_id=manifest.plan_id,
            generation=manifest.generation,
            approval_id=manifest.approval_id,
            issued_at=published_bj,
            expires_at=published_bj + certificate_ttl,
            issuer=issuer,
            evidence_refs=(
                f"daily:{len(observed_daily_codes)}",
                f"minute_5m:{len(observed_5m)}",
                f"derived:{','.join(sorted(derived_digests))}",
            ),
            schema_digest=schema_digest,
            manifest_digest=digest_object(manifest),
            clean_digest=digest_object(cleanup_report.certificate),
            source_digest=digest_object(src_bundle.certificate),
            verification_digest=verification_digest,
            coverage_status="complete",
            published_at=published_bj,
        )
        sr = _safe_stage_result(
            manifest,
            outcome=StageState.CERTIFIED,
            reason_code="postclose_orchestration_certified",
            rc=int(ReturnCode.COMPLETED),
            source_attempted=True,
            source_completed=True,
            write_attempted=True,
            writes_known=True,
            coverage_status="complete",
            retry_safe=False,
            alarm_severity="none",
            certificate_ref=f"publish_cert:{digest_object(pub_cert)}",
            accepted_keys=src_bundle.stage_result.accepted_keys,
            rejected_keys=0,
            stage_entry_ref=stage_ref,
        )
        return OrchestrationResult(
            target_date=manifest.target_date,
            plan_id=manifest.plan_id,
            generation=manifest.generation,
            source_bundle=src_bundle,
            backup_bundle=backup_bundle,
            cleanup_report=cleanup_report,
            publish_certificate=pub_cert,
            stage_result=sr,
            daily_inserted=daily_inserted,
            minute_5m_inserted=m5_inserted,
            derived_inserted=derived_inserted,
        )
    finally:
        # 不变量：一旦 writes_known=False（UNKNOWN_WRITES），严禁释放标准锁让后来者抢写
        if acquired_here and not writes_uncertain:
            try:
                lock_ctx.release()
            except Exception:
                pass
