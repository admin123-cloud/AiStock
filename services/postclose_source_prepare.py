# -*- coding: utf-8 -*-
"""S5：盘后源前置验证、主备分类与不可变候选准备（纯计算/可注入只读边界）。

契约要点：
1. 16:00:00 北京时间前拒绝真实盘后下载完成认证；客户端 UI 按钮启用/进度 0/关闭弹窗/超时
   一概判为失败或 uncertain，绝不凭 UI enabled 自证完成。
2. 三市场（sh/sz/bj）本地文件须全部通过 `mtime >= 16:00`、含目标日记录、32B 对齐与读取前
   后哈希不变认证，方可签发本地源就绪。
3. 严格拆解 `source_task_success`、`reconstruction_ready`、`publish_ready`：
   - 全拒绝无抓取错（`TERMINAL_REJECTED_GAPS`, `rc=0`）仅表示“来源子任务终态完成”，
     `reconstruction_ready` 保持 `False`，绝不授予 S7 清理旧数据权限；
   - 保留全量 `accepted_keys` 与 `rejected_keys` 账本，严禁截断（如 `[:50]`）；
   - 官网日线仅允许在本地日线 **零写入且前置失败** 时回退；若发生写锁/许可/写后重叠错，
     严禁切换官网源，且官网日线回退不赋予分钟源就绪。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from services.postclose_contracts import (
    ReturnCode,
    SourceCertificate,
    StageResult,
    StageState,
    digest_object,
)
from utils.tdx_lc5 import STANDARD_5M_BAR_ENDS

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
POSTCLOSE_DOWNLOAD_NOT_BEFORE = time(16, 0)
DEFAULT_MARKETS = ("sh", "sz", "bj")
DEFAULT_FROZEN_INDEX_CODES = (
    "000001.SH",
    "399001.SZ",
    "399006.SZ",
    "399106.SZ",
    "000680.SH",
    "000300.SH",
    "000016.SH",
    "000905.SH",
    "000852.SH",
    "000688.SH",
)


class SourcePreparationError(RuntimeError):
    """盘后源准备或不可变摘要核验失败（fail-closed）。"""


@dataclass(frozen=True)
class LocalFileSnapshot:
    market: str
    path: str
    mtime_bj: datetime
    byte_size: int
    has_target_date: bool
    sha256_before: str
    sha256_after: str


@dataclass(frozen=True)
class DownloadValidationResult:
    ok: bool
    reason: str
    markets_verified: tuple[str, ...]
    fresh_file_count: int


@dataclass(frozen=True)
class DailyCandidateResult:
    ok: bool
    reason: str
    source_mode: str
    fallback_used: bool
    accepted_rows: tuple[dict[str, Any], ...]
    rejected_rows: tuple[dict[str, Any], ...]
    missing_index_codes: tuple[str, ...]
    source_digest: str


@dataclass(frozen=True)
class MinuteCandidateResult:
    ok: bool
    rc: int
    outcome: StageState
    reason_code: str
    coverage_status: str
    source_task_success: bool
    reconstruction_ready: bool
    publish_ready: bool
    accepted_keys: tuple[str, ...]
    rejected_keys: tuple[dict[str, Any], ...]
    fetch_errors: dict[str, str]
    accepted_bars_by_code: dict[str, tuple[dict[str, Any], ...]]
    source_digest: str
    accepted_keys_digest: str
    rejected_keys_digest: str


@dataclass(frozen=True)
class PreparedSourceBundle:
    target_date: date
    plan_id: str
    generation: int
    approval_id: str
    source_mode: str
    fallback_used: bool
    daily: DailyCandidateResult
    minute: MinuteCandidateResult
    candidate_digest: str
    certificate: Optional[SourceCertificate]
    stage_result: StageResult


def _to_bj(dt: datetime) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(BEIJING_TZ)


def _valid_ohlc(bar: Mapping[str, Any]) -> bool:
    try:
        o = float(bar["open"])
        h = float(bar["high"])
        l = float(bar["low"])
        c = float(bar["close"])
        v = float(bar["volume"])
    except (KeyError, TypeError, ValueError):
        return False
    if o <= 0 or h <= 0 or l <= 0 or c <= 0 or v < 0:
        return False
    if h < max(o, c) or l > min(o, c) or l > h:
        return False
    amt = bar.get("amount")
    if amt is not None:
        try:
            if float(amt) < 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _freeze_for_digest(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple((str(k), _freeze_for_digest(value[k])) for k in sorted(value, key=lambda x: str(x)))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_for_digest(x) for x in value)
    if isinstance(value, float):
        return f"float:{value!r}"
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(_freeze_for_digest(x) for x in value))
    return value


def canonical_sha256(value: Any) -> str:
    """将任意嵌套映射/列表/浮点数规范化为不可变元组后复用 ``digest_object`` 算摘要。"""
    return digest_object(_freeze_for_digest(value))


def verify_candidate_digest(payload: Any, expected_digest: str) -> str:
    """不可变候选冻结复核：若内容与登记摘要不符立即 fail-closed 抛错。"""
    actual = canonical_sha256(payload)
    if actual != expected_digest:
        raise SourcePreparationError(
            f"candidate digest mismatch: expected={expected_digest}, actual={actual}"
        )
    return actual


def validate_download_evidence(
    target_date: date,
    download_report: Mapping[str, Any],
    file_snapshots: Sequence[LocalFileSnapshot],
    *,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    min_fresh_files: int = 1,
) -> DownloadValidationResult:
    """核验 TDX 客户端下载完成证据：严禁 <16:00 下载、严禁 UI enabled 自证完成。"""
    if not isinstance(download_report, Mapping):
        return DownloadValidationResult(False, "download_report_missing", (), 0)

    state = str(download_report.get("state") or "").strip().lower()
    if state != "completed":
        return DownloadValidationResult(False, f"client_download_{state or 'unknown'}", (), 0)

    if download_report.get("progress") is not None and float(download_report.get("progress", 0)) <= 0:
        return DownloadValidationResult(False, "client_download_zero_progress", (), 0)

    completed_at = download_report.get("completed_at")
    if completed_at is None:
        return DownloadValidationResult(False, "download_completed_at_missing", (), 0)
    completed_bj = _to_bj(completed_at)
    cutoff = datetime.combine(target_date, POSTCLOSE_DOWNLOAD_NOT_BEFORE, BEIJING_TZ)
    if completed_bj < cutoff:
        return DownloadValidationResult(False, "download_before_1600", (), 0)

    by_market: dict[str, int] = {}
    fresh_count = 0
    for snap in file_snapshots:
        mkt = snap.market.lower().strip()
        if snap.byte_size <= 0 or snap.byte_size % 32 != 0:
            return DownloadValidationResult(False, f"invalid_record_alignment:{snap.path}", (), 0)
        if snap.sha256_before != snap.sha256_after or not snap.sha256_before:
            return DownloadValidationResult(False, f"file_mutated_during_read:{snap.path}", (), 0)
        snap_bj = _to_bj(snap.mtime_bj)
        if snap_bj < cutoff or not snap.has_target_date:
            continue
        by_market[mkt] = by_market.get(mkt, 0) + 1
        fresh_count += 1

    missing_markets = [m for m in markets if by_market.get(m.lower(), 0) == 0]
    if missing_markets:
        return DownloadValidationResult(
            False, f"missing_fresh_markets:{','.join(missing_markets)}", tuple(sorted(by_market)), fresh_count
        )
    if fresh_count < min_fresh_files:
        return DownloadValidationResult(
            False, f"insufficient_fresh_files:{fresh_count}<{min_fresh_files}", tuple(sorted(by_market)), fresh_count
        )
    return DownloadValidationResult(True, "ok", tuple(sorted(by_market)), fresh_count)


def validate_daily_candidate(
    target_date: date,
    rows: Sequence[Mapping[str, Any]],
    *,
    source_mode: str = "local_lday",
    prior_write_error: bool = False,
    required_index_codes: Sequence[str] = DEFAULT_FROZEN_INDEX_CODES,
) -> DailyCandidateResult:
    """日线候选静态门禁（OHLC 包络、量额、重复键、冻结指数覆盖、禁写错切换）。"""
    if source_mode not in ("local_lday", "official_fallback"):
        raise ValueError(f"unsupported daily source_mode: {source_mode}")
    fallback_used = source_mode == "official_fallback"
    if fallback_used and prior_write_error:
        return DailyCandidateResult(
            ok=False,
            reason="fallback_forbidden_after_write_error",
            source_mode=source_mode,
            fallback_used=True,
            accepted_rows=(),
            rejected_rows=(),
            missing_index_codes=tuple(required_index_codes),
            source_digest=canonical_sha256([]),
        )

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    target_iso = target_date.isoformat()

    for item in rows:
        code = str(item.get("code") or "").strip().upper()
        row_date = item.get("trade_date")
        row_date_iso = row_date.isoformat() if isinstance(row_date, date) else str(row_date or "")[:10]
        if not code or row_date_iso != target_iso:
            rejected.append({"code": code, "reason": "date_or_code_mismatch"})
            continue
        if code in seen_codes:
            rejected.append({"code": code, "reason": "duplicate_key"})
            continue
        if not _valid_ohlc(item):
            rejected.append({"code": code, "reason": "invalid_ohlcv"})
            continue
        seen_codes.add(code)
        accepted.append(
            {
                "code": code,
                "trade_date": target_iso,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item["volume"]),
                "amount": None if item.get("amount") is None else float(item["amount"]),
            }
        )

    accepted.sort(key=lambda r: r["code"])
    missing_indices = tuple(c for c in required_index_codes if c not in seen_codes)
    if not accepted or rejected or missing_indices:
        reason = "daily_empty" if not accepted else ("daily_rows_rejected" if rejected else "missing_frozen_indices")
        return DailyCandidateResult(
            ok=False,
            reason=reason,
            source_mode=source_mode,
            fallback_used=fallback_used,
            accepted_rows=tuple(accepted),
            rejected_rows=tuple(rejected),
            missing_index_codes=missing_indices,
            source_digest=canonical_sha256(accepted),
        )

    return DailyCandidateResult(
        ok=True,
        reason="ok",
        source_mode=source_mode,
        fallback_used=fallback_used,
        accepted_rows=tuple(accepted),
        rejected_rows=(),
        missing_index_codes=(),
        source_digest=canonical_sha256(accepted),
    )


def validate_minute_5m_candidate(
    target_date: date,
    expected_keys: Sequence[str],
    raw_bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    fetch_errors: Optional[Mapping[str, str]] = None,
    accepted_exceptions: Iterable[str] = (),
    reject_alarm_rate: float = 0.05,
    expected_slot_count: int = len(STANDARD_5M_BAR_ENDS),
) -> MinuteCandidateResult:
    """5 分钟候选与主备分类门禁（全量拒绝账本不截断；source_task_success 与 reconstruction_ready 分离）。"""
    errors = dict(fetch_errors or {})
    exempt = {str(c).strip().upper() for c in accepted_exceptions}
    expected_norm = sorted({str(c).strip().upper() for c in expected_keys if str(c).strip()})
    target_iso = target_date.isoformat()

    accepted_bars: dict[str, tuple[dict[str, Any], ...]] = {}
    accepted_keys: list[str] = []
    rejected_keys: list[dict[str, Any]] = []

    for code in expected_norm:
        if code in errors:
            continue
        raw_bars = list(raw_bars_by_code.get(code) or ())
        if not raw_bars:
            if code in exempt:
                continue
            rejected_keys.append({"code": code, "date": target_iso, "reason": "no_source_coverage"})
            continue

        slots_seen: set[str] = set()
        normalized_bars: list[dict[str, Any]] = []
        bar_error: Optional[str] = None
        for bar in raw_bars:
            dt_val = str(bar.get("datetime") or "").strip()
            if not dt_val.startswith(target_iso) or len(dt_val) < 16:
                bar_error = "wrong_date_bar"
                break
            hhmm = dt_val[11:16]
            if hhmm in slots_seen:
                bar_error = "duplicate_slot"
                break
            if not _valid_ohlc(bar):
                bar_error = "invalid_ohlcv"
                break
            slots_seen.add(hhmm)
            amt = bar.get("amount")
            normalized_bars.append(
                {
                    "code": code,
                    "datetime": dt_val[:19] if len(dt_val) >= 19 else f"{dt_val[:16]}:00",
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": float(bar["volume"]),
                    # S4/R22b 契约：缺少真实成交额必须保持 None/NULL，绝不估额或转 0
                    "amount": None if amt is None else float(amt),
                }
            )
        if bar_error is not None:
            rejected_keys.append({"code": code, "date": target_iso, "reason": bar_error})
            continue
        if len(normalized_bars) != expected_slot_count:
            rejected_keys.append(
                {
                    "code": code,
                    "date": target_iso,
                    "reason": f"slot_count_mismatch:{len(normalized_bars)}!={expected_slot_count}",
                }
            )
            continue

        normalized_bars.sort(key=lambda b: b["datetime"])
        accepted_bars[code] = tuple(normalized_bars)
        accepted_keys.append(code)

    total = len(expected_norm)
    n_err = len(errors)
    n_rej = len(rejected_keys)
    accepted_keys.sort()

    source_digest = canonical_sha256(accepted_bars)
    accepted_digest = canonical_sha256(accepted_keys)
    rejected_digest = canonical_sha256(rejected_keys)

    if total > 0 and n_err >= total:
        return MinuteCandidateResult(
            ok=False,
            rc=int(ReturnCode.SOURCE_FAILED),
            outcome=StageState.SOURCE_FAILED,
            reason_code="minute_source_unreachable",
            coverage_status="blocked",
            source_task_success=False,
            reconstruction_ready=False,
            publish_ready=False,
            accepted_keys=(),
            rejected_keys=tuple(rejected_keys),
            fetch_errors=errors,
            accepted_bars_by_code={},
            source_digest=source_digest,
            accepted_keys_digest=accepted_digest,
            rejected_keys_digest=rejected_digest,
        )

    if n_err > 0:
        return MinuteCandidateResult(
            ok=False,
            rc=int(ReturnCode.DEGRADED),
            outcome=StageState.DEGRADED,
            reason_code="minute_partial_fetch_errors",
            coverage_status="blocked",
            source_task_success=False,
            reconstruction_ready=False,
            publish_ready=False,
            accepted_keys=tuple(accepted_keys),
            rejected_keys=tuple(rejected_keys),
            fetch_errors=errors,
            accepted_bars_by_code=accepted_bars,
            source_digest=source_digest,
            accepted_keys_digest=accepted_digest,
            rejected_keys_digest=rejected_digest,
        )

    if total > 0 and n_rej == total and len(accepted_keys) == 0:
        return MinuteCandidateResult(
            ok=False,
            rc=int(ReturnCode.COMPLETED),
            outcome=StageState.TERMINAL_REJECTED_GAPS,
            reason_code="all_missing_keys_rejected_by_arbitration",
            coverage_status="all_rejected",
            source_task_success=True,
            reconstruction_ready=False,
            publish_ready=False,
            accepted_keys=(),
            rejected_keys=tuple(rejected_keys),
            fetch_errors={},
            accepted_bars_by_code={},
            source_digest=source_digest,
            accepted_keys_digest=accepted_digest,
            rejected_keys_digest=rejected_digest,
        )

    if n_rej > 0:
        over_rate = bool(total > 0 and (n_rej / total) > reject_alarm_rate)
        return MinuteCandidateResult(
            ok=False,
            rc=int(ReturnCode.DEGRADED if over_rate else ReturnCode.BLOCKED),
            outcome=StageState.DEGRADED if over_rate else StageState.BLOCKED,
            reason_code="minute_unapproved_rejected_keys",
            coverage_status="blocked",
            source_task_success=not over_rate,
            reconstruction_ready=False,
            publish_ready=False,
            accepted_keys=tuple(accepted_keys),
            rejected_keys=tuple(rejected_keys),
            fetch_errors={},
            accepted_bars_by_code=accepted_bars,
            source_digest=source_digest,
            accepted_keys_digest=accepted_digest,
            rejected_keys_digest=rejected_digest,
        )

    return MinuteCandidateResult(
        ok=True,
        rc=int(ReturnCode.COMPLETED),
        outcome=StageState.PREPARED,
        reason_code="minute_complete",
        coverage_status="complete",
        source_task_success=True,
        reconstruction_ready=True,
        publish_ready=True,
        accepted_keys=tuple(accepted_keys),
        rejected_keys=(),
        fetch_errors={},
        accepted_bars_by_code=accepted_bars,
        source_digest=source_digest,
        accepted_keys_digest=accepted_digest,
        rejected_keys_digest=rejected_digest,
    )


def prepare_postclose_sources(
    *,
    target_date: date,
    plan_id: str,
    generation: int,
    approval_id: str,
    schema_digest: str,
    download_report: Optional[Mapping[str, Any]],
    file_snapshots: Sequence[LocalFileSnapshot],
    daily_rows: Sequence[Mapping[str, Any]],
    expected_minute_keys: Sequence[str],
    minute_bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    now: datetime,
    issuer: str = "postclose_source_prepare",
    daily_source_mode: str = "local_lday",
    prior_write_error: bool = False,
    minute_fetch_errors: Optional[Mapping[str, str]] = None,
    accepted_exceptions: Iterable[str] = (),
    required_index_codes: Sequence[str] = DEFAULT_FROZEN_INDEX_CODES,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    exception_ledger_ref: str = "ledger:in_memory",
    certificate_ttl: timedelta = timedelta(hours=6),
    expected_slot_count: int = len(STANDARD_5M_BAR_ENDS),
) -> PreparedSourceBundle:
    """盘后多源准备与认证汇总：零写正式表，先证源后清理。"""
    now_bj = _to_bj(now)
    if daily_source_mode == "local_lday":
        dl = validate_download_evidence(
            target_date, download_report or {}, file_snapshots, markets=markets
        )
        if not dl.ok:
            empty_daily = DailyCandidateResult(
                ok=False,
                reason=dl.reason,
                source_mode=daily_source_mode,
                fallback_used=False,
                accepted_rows=(),
                rejected_rows=(),
                missing_index_codes=tuple(required_index_codes),
                source_digest=canonical_sha256([]),
            )
            empty_min = MinuteCandidateResult(
                ok=False,
                rc=int(ReturnCode.SOURCE_FAILED),
                outcome=StageState.SOURCE_FAILED,
                reason_code=dl.reason,
                coverage_status="blocked",
                source_task_success=False,
                reconstruction_ready=False,
                publish_ready=False,
                accepted_keys=(),
                rejected_keys=(),
                fetch_errors={},
                accepted_bars_by_code={},
                source_digest=canonical_sha256({}),
                accepted_keys_digest=canonical_sha256([]),
                rejected_keys_digest=canonical_sha256([]),
            )
            sr = StageResult(
                target_date=target_date,
                plan_id=plan_id,
                generation=generation,
                outcome=StageState.SOURCE_FAILED,
                reason_code=dl.reason,
                rc=int(ReturnCode.SOURCE_FAILED),
                source_attempted=True,
                source_completed=False,
                write_attempted=False,
                writes_known=True,
                accepted_keys=0,
                rejected_keys=0,
                exception_ledger_ref="",
                coverage_status="blocked",
                certificate_ref=None,
                retry_safe=True,
                alarm_severity="critical",
            )
            return PreparedSourceBundle(
                target_date=target_date,
                plan_id=plan_id,
                generation=generation,
                approval_id=approval_id,
                source_mode=daily_source_mode,
                fallback_used=False,
                daily=empty_daily,
                minute=empty_min,
                candidate_digest=canonical_sha256({"daily": [], "minute": {}}),
                certificate=None,
                stage_result=sr,
            )

    daily = validate_daily_candidate(
        target_date,
        daily_rows,
        source_mode=daily_source_mode,
        prior_write_error=prior_write_error,
        required_index_codes=required_index_codes,
    )
    minute = validate_minute_5m_candidate(
        target_date,
        expected_minute_keys,
        minute_bars_by_code,
        fetch_errors=minute_fetch_errors,
        accepted_exceptions=accepted_exceptions,
        expected_slot_count=expected_slot_count,
    )

    candidate_payload = {
        "target_date": target_date.isoformat(),
        "daily": daily.accepted_rows,
        "minute": minute.accepted_bars_by_code,
    }
    candidate_digest = canonical_sha256(candidate_payload)

    # 官网日线回退只代表日线可用，绝不跳过或冒充分钟就绪
    overall_ready = daily.ok and minute.reconstruction_ready
    if not daily.ok:
        outcome = StageState.BLOCKED if prior_write_error and daily.fallback_used else StageState.SOURCE_FAILED
        rc = int(ReturnCode.BLOCKED if outcome is StageState.BLOCKED else ReturnCode.SOURCE_FAILED)
        reason_code = daily.reason
        coverage_status = "blocked"
        cert: Optional[SourceCertificate] = None
        cert_ref: Optional[str] = None
        retry_safe = outcome is not StageState.BLOCKED
        alarm_sev = "critical"
    elif not minute.reconstruction_ready:
        outcome = minute.outcome
        rc = minute.rc
        reason_code = minute.reason_code
        coverage_status = minute.coverage_status
        cert = None
        cert_ref = None
        retry_safe = False if outcome is StageState.TERMINAL_REJECTED_GAPS else True
        alarm_sev = "warning" if outcome is StageState.TERMINAL_REJECTED_GAPS else "critical"
    else:
        outcome = StageState.PREPARED
        rc = int(ReturnCode.COMPLETED)
        reason_code = "sources_prepared_with_fallback" if daily.fallback_used else "sources_prepared"
        coverage_status = "complete"
        cert = SourceCertificate(
            target_date=target_date,
            plan_id=plan_id,
            generation=generation,
            approval_id=approval_id,
            issued_at=now_bj,
            expires_at=now_bj + certificate_ttl,
            issuer=issuer,
            evidence_refs=(f"daily:{daily.source_mode}:{daily.source_digest[:12]}",
                           f"minute:{minute.source_digest[:12]}"),
            schema_digest=schema_digest,
            source_digest=candidate_digest,
            accepted_keys_digest=minute.accepted_keys_digest,
            rejected_keys_digest=minute.rejected_keys_digest,
            coverage_status="complete",
            source_task_success=True,
            reconstruction_ready=True,
            publish_ready=True,
        )
        cert_ref = f"source_cert:{digest_object(cert)}"
        retry_safe = True
        alarm_sev = "warning" if daily.fallback_used else "none"

    n_rejected = len(minute.rejected_keys)
    ledger = exception_ledger_ref if n_rejected else ""
    sr = StageResult(
        target_date=target_date,
        plan_id=plan_id,
        generation=generation,
        outcome=outcome,
        reason_code=reason_code,
        rc=rc,
        source_attempted=True,
        source_completed=bool(daily.ok and minute.source_task_success),
        write_attempted=False,
        writes_known=True,
        accepted_keys=len(minute.accepted_keys),
        rejected_keys=n_rejected,
        exception_ledger_ref=ledger,
        coverage_status=coverage_status,
        certificate_ref=cert_ref,
        retry_safe=retry_safe,
        alarm_severity=alarm_sev,
        concurrent_events=("source_failed",) if rc == int(ReturnCode.SOURCE_FAILED) else (),
    )
    return PreparedSourceBundle(
        target_date=target_date,
        plan_id=plan_id,
        generation=generation,
        approval_id=approval_id,
        source_mode=daily.source_mode,
        fallback_used=daily.fallback_used,
        daily=daily,
        minute=minute,
        candidate_digest=candidate_digest,
        certificate=cert,
        stage_result=sr,
    )
