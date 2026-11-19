# -*- coding: utf-8 -*-
"""S7：16:15 前精准清理执行器（单代清理、终态核验与 CleanCertificate 签发）。

核心不变量：
1. 必须同时通过：三态日历 ``"trading"``、完整合法的 ``PlanManifest``、
   ``SourceCertificate``（含 ``source_reconstruction_ready == True``）、
   ``BackupCertificate``（含 ``recovery_verified == True``）、
   ``CloseCertificate``（含 ``unresolved_writes == 0``）、
   与 ``manifest.operation_permit_refs`` 匹配的 ``delete``/``invalidate`` 许可、
   及有效持有的 ``LockContext`` 租约。任何一项缺失→零清理，返回 ``BLOCKED (rc=2)``。
2. 时间硬门槛：启动删除前必须严格 ``now < manifest.delete_start_latest < 16:15:00``，
   否则零删除直接返回 ``DEADLINE_MISSED (rc=5)``；全部 7 张表清理与终态回验以及
   mood realtime 失效必须严格在 ``< 16:15:00`` 完成，才可签发 ``CleanCertificate``。
3. 执行与状态机：
   - 先落盘 ``CLEANING`` 状态入口（``stage_entry_ref``）再执行首个删除；
   - 每次删除前通过 ``WriteAdmission.issue`` 登记唯一 ``WriteAttempt``，
     持锁期间不切锁名、不强解未知写锁；
   - 严格只清 ``CLEANUP_OBJECTS`` 清单与半开北京日窗 ``[D 00:00, D+1 00:00)``，
     绝不触碰 ``kline_daily``、``kline_minute_1`` 与账户表；
   - 每表等待 mutation 终态（``is_done == 1`` 且无错），全部清完后独立回验
     ``raw == 0``、``FINAL == 0`` 且非目标哨兵逻辑指纹保持一致；
   - 若发写后发生超时、观测失败或跨过 16:15 deadline，立即进入
     ``UNKNOWN_WRITES (rc=6)`` 且保留锁与证据不释放、不并发补删或回插。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

from services.market_write_admission import AttemptState, WriteAdmission, WriteAttempt
from services.postclose_contracts import (
    BackupCertificate,
    CLEANUP_OBJECTS,
    CleanCertificate,
    CloseCertificate,
    PlanManifest,
    Purpose,
    ReturnCode,
    SourceCertificate,
    StageResult,
    StageState,
    cleanup_operation_allowed,
    day_window,
    digest_object,
    source_reconstruction_ready,
    validate_certificate,
    validate_manifest,
)
from services.postclose_journal import AttemptJournal
from services.postclose_source_prepare import canonical_sha256

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class CleanupGateway(Protocol):
    """可注入清理执行与独立只读核验网关（无默认可写全局连接）。"""

    def snapshot_non_target_sentinels(self, tables: Sequence[str], target_date: date) -> dict[str, Any]:
        """采集非目标哨兵（如 D-1 / D+1 / 月界逻辑摘要），用于清后对比未被误改。"""
        ...

    def delete_day_window(
        self,
        table: str,
        window_start: datetime,
        window_end: datetime,
        time_col: str,
        *,
        attempt_id: str,
    ) -> dict[str, Any]:
        """发起单表半开日窗删除，返回包含 ``mutation_id``（若需要）或终态信息的字典。"""
        ...

    def wait_mutation_terminal(self, table: str, mutation_info: Mapping[str, Any]) -> dict[str, Any]:
        """观测单个 mutation 终态：必须明确返回 ``{"is_done": True, "error": ""}`` 才算确定终态。"""
        ...

    def verify_residual_zero(
        self, table: str, window_start: datetime, window_end: datetime, time_col: str
    ) -> dict[str, int]:
        """独立核验目标半开日窗在 ``raw`` 与 ``FINAL`` 口径下的剩余行数。"""
        ...


@dataclass(frozen=True)
class CleanupReport:
    target_date: date
    plan_id: str
    generation: int
    dry_run: bool
    deleted_tables: tuple[str, ...]
    mood_invalidated: bool
    certificate: Optional[CleanCertificate]
    stage_result: StageResult


def _to_bj(dt: datetime) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(BEIJING_TZ)


def _blocked_result(
    manifest: Optional[PlanManifest],
    *,
    target_date: date,
    plan_id: str,
    generation: int,
    reason_code: str,
    rc: int = int(ReturnCode.BLOCKED),
    outcome: StageState = StageState.BLOCKED,
    write_attempted: bool = False,
    writes_known: bool = True,
    retry_safe: bool = True,
    alarm_severity: str = "critical",
    stage_entry_ref: Optional[str] = None,
    concurrent_events: tuple[str, ...] = ("blocked",),
) -> CleanupReport:
    sr = StageResult(
        target_date=target_date,
        plan_id=plan_id,
        generation=generation,
        outcome=outcome,
        reason_code=reason_code,
        rc=rc,
        source_attempted=False,
        source_completed=False,
        write_attempted=write_attempted,
        writes_known=writes_known,
        accepted_keys=0,
        rejected_keys=0,
        exception_ledger_ref="",
        coverage_status="blocked",
        certificate_ref=None,
        retry_safe=retry_safe,
        alarm_severity=alarm_severity,
        concurrent_events=concurrent_events,
        stage_entry_ref=stage_entry_ref,
    )
    return CleanupReport(
        target_date=target_date,
        plan_id=plan_id,
        generation=generation,
        dry_run=True,
        deleted_tables=(),
        mood_invalidated=False,
        certificate=None,
        stage_result=sr,
    )


def run_postclose_cleanup(
    *,
    manifest: PlanManifest,
    source_cert: SourceCertificate,
    backup_cert: BackupCertificate,
    close_cert: CloseCertificate,
    trusted_issuers: tuple[str, ...],
    calendar_state: str,
    operation_permits: Mapping[str, str],
    gateway: Optional[CleanupGateway],
    journal: AttemptJournal,
    lock_ctx: Any,
    now_fn: Callable[[], datetime],
    admission: Optional[WriteAdmission] = None,
    mood_invalidator: Optional[Callable[[date, datetime], Mapping[str, Any]]] = None,
    schema_digest: str,
    issuer: str = "postclose_cleanup",
    certificate_ttl: timedelta = timedelta(hours=6),
    dry_run: bool = True,
    force: bool = False,  # noqa: ARG001 - 保留显式参数位置但禁止用 force 绕过任何门禁
) -> CleanupReport:
    """执行 16:15 前整日清理（默认 ``dry_run=True``，``apply`` 须全证全许全锁）。"""
    del force  # 明示设计律：force 绝不允许绕过日历/证书/许可/deadline/锁
    now_bj = _to_bj(now_fn())
    td = manifest.target_date if isinstance(manifest, PlanManifest) else date.min
    pid = manifest.plan_id if isinstance(manifest, PlanManifest) else "invalid"
    gen = manifest.generation if isinstance(manifest, PlanManifest) else 1

    # 1) Manifest + 三态日历 + 操作许可 + 3 张前置证书交叉绑定校验
    if not validate_manifest(manifest):
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code="invalid_plan_manifest",
        )
    if calendar_state != "trading":
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code=f"calendar_not_trading:{calendar_state}",
        )

    expected_permits = dict(manifest.operation_permit_refs)
    if (
        not operation_permits
        or operation_permits.get("delete") != expected_permits.get("delete")
        or operation_permits.get("invalidate") != expected_permits.get("invalidate")
    ):
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code="operation_permit_mismatch",
        )

    manifest_certs = dict(manifest.certificate_digests)
    cert_checks = (
        ("source", source_cert, SourceCertificate),
        ("backup", backup_cert, BackupCertificate),
        ("close", close_cert, CloseCertificate),
    )
    for key, cert_obj, cert_cls in cert_checks:
        if type(cert_obj) is not cert_cls:
            return _blocked_result(
                manifest, target_date=td, plan_id=pid, generation=gen,
                reason_code=f"missing_or_wrong_cert_type:{key}",
            )
        if not validate_certificate(
            cert_obj,
            target_date=manifest.target_date,
            plan_id=manifest.plan_id,
            generation=manifest.generation,
            approval_id=manifest.approval_id,
            expected_digest=manifest_certs.get(key, ""),
            trusted_issuers=trusted_issuers,
            now=now_bj,
        ):
            return _blocked_result(
                manifest, target_date=td, plan_id=pid, generation=gen,
                reason_code=f"invalid_prerequisite_cert:{key}",
            )

    if not source_reconstruction_ready(source_cert):
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code="source_not_reconstruction_ready",
        )
    if backup_cert.recovery_verified is not True:
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code="backup_recovery_not_verified",
        )
    if close_cert.unresolved_writes != 0:
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code="close_cert_has_unresolved_writes",
        )

    # 2) 最晚可启动时刻与 16:15 deadline 守卫（越点→零删除 DEADLINE_MISSED）
    if now_bj >= manifest.delete_start_latest or now_bj >= manifest.clean_deadline:
        return _blocked_result(
            manifest,
            target_date=td,
            plan_id=pid,
            generation=gen,
            reason_code="delete_start_deadline_missed",
            rc=int(ReturnCode.DEADLINE_MISSED),
            outcome=StageState.DEADLINE_MISSED,
            write_attempted=False,
            retry_safe=True,
            concurrent_events=("deadline_missed",),
        )

    # 3) 计划域验证与 dry_run plan-only 快速返回（不触碰 DB/mood）
    tables_to_clean = []
    for target, op, time_col in manifest.cleanup_objects:
        if not cleanup_operation_allowed(target, op):
            return _blocked_result(
                manifest, target_date=td, plan_id=pid, generation=gen,
                reason_code=f"disallowed_cleanup_object:{target}:{op}",
            )
        if op == "delete":
            tables_to_clean.append((target, time_col))

    if dry_run:
        return CleanupReport(
            target_date=td,
            plan_id=pid,
            generation=gen,
            dry_run=True,
            deleted_tables=tuple(t for t, _ in tables_to_clean),
            mood_invalidated=False,
            certificate=None,
            stage_result=StageResult(
                target_date=td,
                plan_id=pid,
                generation=gen,
                outcome=StageState.PREPARED,
                reason_code="cleanup_plan_only_dry_run",
                rc=int(ReturnCode.COMPLETED),
                source_attempted=True,
                source_completed=True,
                write_attempted=False,
                writes_known=True,
                accepted_keys=0,
                rejected_keys=0,
                exception_ledger_ref="",
                coverage_status="complete",
                certificate_ref=None,
                retry_safe=True,
                alarm_severity="none",
            ),
        )

    if gateway is None:
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code="cleanup_gateway_missing",
        )

    # 4) 持锁核验 + 写前采集非目标哨兵 + 持久落盘 CLEANING 入口
    try:
        lock_ctx.assert_held()
        lock_ctx.heartbeat()
    except Exception as exc:
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code=f"lock_not_held:{type(exc).__name__}",
        )

    win_start, win_end = day_window(td)
    if win_start != manifest.window_start or win_end != manifest.window_end:
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code="window_mismatch",
        )

    table_names = tuple(t for t, _ in tables_to_clean)
    try:
        baseline_sentinels = gateway.snapshot_non_target_sentinels(table_names, td)
    except Exception as exc:
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code=f"sentinel_snapshot_failed:{type(exc).__name__}",
        )

    cleaning_planned = WriteAttempt(
        D=td,
        plan_id=pid,
        generation=gen,
        writer_id=issuer,
        purpose=Purpose.AFTERCLOSE_CLEANUP.value,
        attempt_id=f"{pid}:g{gen}:stage:CLEANING",
        table="postclose_stage",
        operation="stage_entry_CLEANING",
        key_digest=canonical_sha256({"target_date": td.isoformat()}),
        payload_digest=canonical_sha256({"stage": "CLEANING", "target_date": td.isoformat()}),
        query_id=f"stage_cleaning_{pid}_g{gen}",
    )
    try:
        journal.append(cleaning_planned)
        journal.append(replace(cleaning_planned, state=AttemptState.ISSUED, issued_at=now_bj))
    except Exception as exc:
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code=f"journal_stage_entry_failed:{type(exc).__name__}",
        )
    stage_ref = f"journal:{cleaning_planned.attempt_id}"

    # 5) 顺序删除获批各表并等待每一 mutation 终态（任何不确定即 UNKNOWN_WRITES）
    deleted_tables: list[str] = []
    mutation_records: list[dict[str, Any]] = []
    pending_actions: dict[str, dict[str, Any]] = {}
    write_attempted = False

    def _dispatch_cleanup(attempt: WriteAttempt) -> None:
        nonlocal write_attempted
        write_attempted = True
        if attempt.operation == "invalidate":
            m_now = _to_bj(now_fn())
            if mood_invalidator is not None:
                res = mood_invalidator(td, m_now)
            else:
                from services.market_mood import invalidate_day_realtime

                res = invalidate_day_realtime(td.strftime("%Y%m%d"), invalidated_at=m_now)
            pending_actions[attempt.attempt_id] = dict(res)
            return
        meta = pending_actions[attempt.attempt_id]
        mut_info = gateway.delete_day_window(
            attempt.table, win_start, win_end, meta["time_col"], attempt_id=attempt.attempt_id
        )
        pending_actions[attempt.attempt_id]["mutation"] = mut_info

    active_admission = admission or WriteAdmission(journal, lock_ctx, _dispatch_cleanup, now=now_fn)

    for idx, (table, time_col) in enumerate(tables_to_clean, start=1):
        step_now = _to_bj(now_fn())
        if step_now >= manifest.clean_deadline:
            if not write_attempted:
                return _blocked_result(
                    manifest, target_date=td, plan_id=pid, generation=gen,
                    reason_code="deadline_missed_before_first_delete",
                    rc=int(ReturnCode.DEADLINE_MISSED),
                    outcome=StageState.DEADLINE_MISSED,
                    write_attempted=False,
                    retry_safe=True,
                    stage_entry_ref=stage_ref,
                    concurrent_events=("deadline_missed",),
                )
            return CleanupReport(
                target_date=td, plan_id=pid, generation=gen, dry_run=False,
                deleted_tables=tuple(deleted_tables), mood_invalidated=False,
                certificate=None,
                stage_result=StageResult(
                    target_date=td, plan_id=pid, generation=gen,
                    outcome=StageState.UNKNOWN_WRITES,
                    reason_code="deadline_missed_after_partial_delete",
                    rc=int(ReturnCode.UNKNOWN_WRITES),
                    source_attempted=False, source_completed=False,
                    write_attempted=True, writes_known=False,
                    accepted_keys=0, rejected_keys=0, exception_ledger_ref="",
                    coverage_status="blocked", certificate_ref=None,
                    retry_safe=False, alarm_severity="critical",
                    concurrent_events=("deadline_missed", "unknown_writes"),
                    stage_entry_ref=stage_ref,
                ),
            )

        try:
            lock_ctx.assert_held()
            lock_ctx.heartbeat()
        except Exception as exc:
            outcome = StageState.UNKNOWN_WRITES if write_attempted else StageState.BLOCKED
            rc = int(ReturnCode.UNKNOWN_WRITES if write_attempted else ReturnCode.BLOCKED)
            return _blocked_result(
                manifest, target_date=td, plan_id=pid, generation=gen,
                reason_code=f"lock_lost_during_cleanup:{type(exc).__name__}",
                rc=rc, outcome=outcome,
                write_attempted=write_attempted,
                writes_known=not write_attempted,
                retry_safe=not write_attempted,
                stage_entry_ref=stage_ref,
                concurrent_events=("unknown_writes",) if write_attempted else ("blocked",),
            )

        attempt = WriteAttempt(
            D=td,
            plan_id=pid,
            generation=gen,
            writer_id=issuer,
            purpose=Purpose.AFTERCLOSE_CLEANUP.value,
            attempt_id=f"{pid}:g{gen}:del:{idx}:{table}",
            table=table,
            operation="delete",
            key_digest=canonical_sha256({"table": table, "target_date": td.isoformat()}),
            payload_digest=canonical_sha256({"table": table, "start": win_start, "end": win_end}),
            query_id=f"del_{pid}_g{gen}_{idx}",
        )
        pending_actions[attempt.attempt_id] = {"time_col": time_col}
        try:
            ack_attempt = active_admission.issue(attempt)
            mut_info = pending_actions[attempt.attempt_id]["mutation"]
            term = gateway.wait_mutation_terminal(table, mut_info)
        except Exception as exc:
            outcome = StageState.UNKNOWN_WRITES if write_attempted else StageState.BLOCKED
            rc = int(ReturnCode.UNKNOWN_WRITES if write_attempted else ReturnCode.BLOCKED)
            return CleanupReport(
                target_date=td, plan_id=pid, generation=gen, dry_run=False,
                deleted_tables=tuple(deleted_tables), mood_invalidated=False,
                certificate=None,
                stage_result=StageResult(
                    target_date=td, plan_id=pid, generation=gen,
                    outcome=outcome,
                    reason_code=f"delete_or_mutation_uncertain:{table}:{type(exc).__name__}",
                    rc=rc,
                    source_attempted=False, source_completed=False,
                    write_attempted=write_attempted,
                    writes_known=not write_attempted,
                    accepted_keys=0, rejected_keys=0, exception_ledger_ref="",
                    coverage_status="blocked", certificate_ref=None,
                    retry_safe=not write_attempted, alarm_severity="critical",
                    concurrent_events=("unknown_writes",) if write_attempted else ("blocked",),
                    stage_entry_ref=stage_ref,
                ),
            )

        is_done = bool(term.get("is_done") is True)
        err_msg = str(term.get("error") or "").strip()
        if not is_done or err_msg:
            return CleanupReport(
                target_date=td, plan_id=pid, generation=gen, dry_run=False,
                deleted_tables=tuple(deleted_tables), mood_invalidated=False,
                certificate=None,
                stage_result=StageResult(
                    target_date=td, plan_id=pid, generation=gen,
                    outcome=StageState.UNKNOWN_WRITES,
                    reason_code=f"mutation_not_clean_terminal:{table}",
                    rc=int(ReturnCode.UNKNOWN_WRITES),
                    source_attempted=False, source_completed=False,
                    write_attempted=True, writes_known=False,
                    accepted_keys=0, rejected_keys=0, exception_ledger_ref="",
                    coverage_status="blocked", certificate_ref=None,
                    retry_safe=False, alarm_severity="critical",
                    concurrent_events=("unknown_writes",),
                    stage_entry_ref=stage_ref,
                ),
            )

        journal.append(replace(ack_attempt, state=AttemptState.VERIFIED, verified_at=_to_bj(now_fn())))
        deleted_tables.append(table)
        mutation_records.append({"table": table, "mutation": mut_info, "terminal": term})

    # 6) 独立回验：每表 raw == 0 且 FINAL == 0 + 非目标哨兵完整保持不变
    residual_report: dict[str, dict[str, int]] = {}
    for table, time_col in tables_to_clean:
        res = gateway.verify_residual_zero(table, win_start, win_end, time_col)
        raw_cnt = int(res.get("raw_count", -1))
        final_cnt = int(res.get("final_count", -1))
        residual_report[table] = {"raw_count": raw_cnt, "final_count": final_cnt}
        if raw_cnt != 0 or final_cnt != 0:
            return _blocked_result(
                manifest, target_date=td, plan_id=pid, generation=gen,
                reason_code=f"residual_non_zero:{table}:raw={raw_cnt},final={final_cnt}",
                rc=int(ReturnCode.RECOVERY_REQUIRED),
                outcome=StageState.RECOVERY_REQUIRED,
                write_attempted=True, writes_known=True, retry_safe=False,
                stage_entry_ref=stage_ref,
                concurrent_events=("recovery_required",),
            )

    post_sentinels = gateway.snapshot_non_target_sentinels(table_names, td)
    if canonical_sha256(post_sentinels) != canonical_sha256(baseline_sentinels):
        return _blocked_result(
            manifest, target_date=td, plan_id=pid, generation=gen,
            reason_code="non_target_sentinel_mutated",
            rc=int(ReturnCode.RECOVERY_REQUIRED),
            outcome=StageState.RECOVERY_REQUIRED,
            write_attempted=True, writes_known=True, retry_safe=False,
            stage_entry_ref=stage_ref,
            concurrent_events=("recovery_required",),
        )

    # 7) mood realtime 键级失效（登记 attempt，不触碰 final）
    mood_now = _to_bj(now_fn())
    if mood_now >= manifest.clean_deadline:
        return CleanupReport(
            target_date=td, plan_id=pid, generation=gen, dry_run=False,
            deleted_tables=tuple(deleted_tables), mood_invalidated=False,
            certificate=None,
            stage_result=StageResult(
                target_date=td, plan_id=pid, generation=gen,
                outcome=StageState.DEADLINE_MISSED,
                reason_code="clean_deadline_reached_before_mood_invalidate",
                rc=int(ReturnCode.DEADLINE_MISSED),
                source_attempted=True, source_completed=True,
                write_attempted=True, writes_known=True,
                accepted_keys=0, rejected_keys=0, exception_ledger_ref="",
                coverage_status="complete", certificate_ref=None,
                retry_safe=False, alarm_severity="critical",
                concurrent_events=("deadline_missed",),
                stage_entry_ref=stage_ref,
            ),
        )

    mood_attempt = WriteAttempt(
        D=td,
        plan_id=pid,
        generation=gen,
        writer_id=issuer,
        purpose=Purpose.AFTERCLOSE_CLEANUP.value,
        attempt_id=f"{pid}:g{gen}:invalidate:market_mood",
        table="runtime/market_mood/YYYYMMDD.json",
        operation="invalidate",
        key_digest=canonical_sha256({"day": td.isoformat()}),
        payload_digest=canonical_sha256({"day": td.isoformat()}),
        query_id=f"inv_mood_{pid}_g{gen}",
    )
    mood_ack = active_admission.issue(mood_attempt)
    mood_res = pending_actions.get(mood_attempt.attempt_id, {"day": td.strftime("%Y%m%d")})
    journal.append(replace(mood_ack, state=AttemptState.VERIFIED, verified_at=_to_bj(now_fn())))

    # 8) 全部完成时刻严格早于 16:15:00 才签发 CleanCertificate
    completed_bj = _to_bj(now_fn())
    if completed_bj >= manifest.clean_deadline:
        return CleanupReport(
            target_date=td, plan_id=pid, generation=gen, dry_run=False,
            deleted_tables=tuple(deleted_tables), mood_invalidated=True,
            certificate=None,
            stage_result=StageResult(
                target_date=td, plan_id=pid, generation=gen,
                outcome=StageState.DEADLINE_MISSED,
                reason_code="clean_completed_at_or_after_1615",
                rc=int(ReturnCode.DEADLINE_MISSED),
                source_attempted=True, source_completed=True,
                write_attempted=True, writes_known=True,
                accepted_keys=0, rejected_keys=0, exception_ledger_ref="",
                coverage_status="complete", certificate_ref=None,
                retry_safe=False, alarm_severity="critical",
                concurrent_events=("deadline_missed",),
                stage_entry_ref=stage_ref,
            ),
        )

    cert = CleanCertificate(
        target_date=td,
        plan_id=pid,
        generation=gen,
        approval_id=manifest.approval_id,
        issued_at=completed_bj,
        expires_at=completed_bj + certificate_ttl,
        issuer=issuer,
        evidence_refs=(
            f"cleanup_tables:{len(deleted_tables)}",
            f"mood_invalidated:{mood_res.get('day', td.strftime('%Y%m%d'))}",
        ),
        schema_digest=schema_digest,
        manifest_digest=digest_object(manifest),
        source_digest=digest_object(source_cert),
        backup_digest=digest_object(backup_cert),
        close_digest=digest_object(close_cert),
        mutation_evidence_digest=canonical_sha256(mutation_records),
        residual_digest=canonical_sha256({"residual": residual_report, "sentinels": post_sentinels}),
        completed_at=completed_bj,
    )
    sr = StageResult(
        target_date=td,
        plan_id=pid,
        generation=gen,
        outcome=StageState.CLEANED,
        reason_code="postclose_cleanup_certified",
        rc=int(ReturnCode.COMPLETED),
        source_attempted=True,
        source_completed=True,
        write_attempted=True,
        writes_known=True,
        accepted_keys=0,
        rejected_keys=0,
        exception_ledger_ref="",
        coverage_status="complete",
        certificate_ref=f"clean_cert:{digest_object(cert)}",
        retry_safe=False,
        alarm_severity="none",
        stage_entry_ref=stage_ref,
    )
    return CleanupReport(
        target_date=td,
        plan_id=pid,
        generation=gen,
        dry_run=False,
        deleted_tables=tuple(deleted_tables),
        mood_invalidated=True,
        certificate=cert,
        stage_result=sr,
    )
