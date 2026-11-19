# -*- coding: utf-8 -*-
"""S9：发布代际控制面与只读端门控（PublicationView / PublicationStore / GenerationReadGuard）。

契约要点：
1. 唯一接口 ``PublicationView`` 包含：
   ``target_date, data_date, generation, status, ready, datasets, as_of,
   last_certified_date, coverage, exceptions_ref, reason, certificate_digest``。
2. 仅当控制面存在合法 ``CERTIFIED`` 状态且所载 ``PublishCertificate`` 经
   ``validate_certificate`` 与固定 ``certificate_digest`` 校验通过时，才返回 ``ready=True``；
   ``CLEANING / IMPORTING / VERIFYING / UNKNOWN_WRITES / RECOVERY_REQUIRED /
   BLOCKED`` 及目录缺失/坏 JSON/过期或错代证书一律 fail-closed 返回 ``ready=False``。
3. 显式查询目标日 ``D`` 未认证时，绝不静默偷换成 ``D-1``；仅通过 ``last_certified_date``
   明示上一认证日。
4. 缓存键绑定 ``(target_date, generation, status, dataset, as_of)``；
   ``read_with_generation_guard`` 在读前与读后各复核一次发布视图，
   若查询期间发生代际/状态切换则丢弃混代结果。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from services.postclose_contracts import (
    PublishCertificate,
    StageState,
    digest_object,
    validate_certificate,
)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_PUBLISHED_DATASETS: tuple[str, ...] = (
    "kline_daily",
    "kline_minute_5",
    "kline_minute_15",
    "kline_minute_30",
    "kline_minute_60",
)

DEFAULT_TRUSTED_ISSUERS: tuple[str, ...] = (
    "postclose_orchestrator",
)

REBUILDING_STATUSES: frozenset[str] = frozenset({
    StageState.CLEANING.value,
    StageState.CLEANED.value,
    StageState.IMPORTING.value,
    StageState.VERIFYING.value,
    StageState.UNKNOWN_WRITES.value,
    StageState.RECOVERY_REQUIRED.value,
})


@dataclass(frozen=True)
class PublicationView:
    target_date: str
    data_date: Optional[str]
    generation: int
    status: str
    ready: bool
    datasets: tuple[str, ...]
    as_of: Optional[str]
    last_certified_date: Optional[str]
    coverage: str
    exceptions_ref: str
    reason: str
    certificate_digest: Optional[str]


def publication_cache_key(view: PublicationView, dataset: str) -> tuple[str, int, str, str, str]:
    """S9 缓存代际键：``(D, generation, status, dataset, as_of)``。"""
    return (
        view.target_date,
        int(view.generation),
        view.status,
        str(dataset),
        view.as_of or "",
    )


def _default_control_root() -> Path:
    from utils.paths import runtime_path

    return Path(runtime_path("postclose_control"))


def _to_bj(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return datetime.now(BEIJING_TZ)
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)


def _serialize_publish_cert(cert: PublishCertificate) -> dict[str, Any]:
    return {
        "target_date": cert.target_date.isoformat(),
        "plan_id": cert.plan_id,
        "generation": cert.generation,
        "approval_id": cert.approval_id,
        "issued_at": cert.issued_at.isoformat(),
        "expires_at": cert.expires_at.isoformat(),
        "issuer": cert.issuer,
        "evidence_refs": list(cert.evidence_refs),
        "schema_digest": cert.schema_digest,
        "manifest_digest": cert.manifest_digest,
        "clean_digest": cert.clean_digest,
        "source_digest": cert.source_digest,
        "verification_digest": cert.verification_digest,
        "coverage_status": cert.coverage_status,
        "published_at": cert.published_at.isoformat(),
    }


def _deserialize_publish_cert(raw: Mapping[str, Any]) -> Optional[PublishCertificate]:
    try:
        return PublishCertificate(
            target_date=date.fromisoformat(str(raw["target_date"])),
            plan_id=str(raw["plan_id"]),
            generation=int(raw["generation"]),
            approval_id=str(raw["approval_id"]),
            issued_at=datetime.fromisoformat(str(raw["issued_at"])),
            expires_at=datetime.fromisoformat(str(raw["expires_at"])),
            issuer=str(raw["issuer"]),
            evidence_refs=tuple(str(x) for x in raw["evidence_refs"]),
            schema_digest=str(raw["schema_digest"]),
            manifest_digest=str(raw["manifest_digest"]),
            clean_digest=str(raw["clean_digest"]),
            source_digest=str(raw["source_digest"]),
            verification_digest=str(raw["verification_digest"]),
            coverage_status=str(raw["coverage_status"]),
            published_at=datetime.fromisoformat(str(raw["published_at"])),
        )
    except Exception:
        return None


class PublicationStore:
    """盘后发布控制面存储（原子写 ``state.json``，读侧只读 fail-closed）。"""

    def __init__(self, control_root: Optional[Path] = None):
        self.control_root = Path(control_root) if control_root is not None else _default_control_root()

    @property
    def state_path(self) -> Path:
        return self.control_root / "state.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.control_root / "heartbeat.json"

    def _read_raw_state(self) -> Optional[dict[str, Any]]:
        if not self.control_root.is_dir() or not self.state_path.is_file():
            return None
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state.json must be a JSON object")
        return data

    def _atomic_write(self, path: Path, payload: Mapping[str, Any]) -> None:
        self.control_root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def write_heartbeat(self, *, now: Optional[datetime] = None, runner_id: str = "pipeline-runner") -> None:
        stamp = _to_bj(now)
        self._atomic_write(
            self.heartbeat_path,
            {
                "schema": "postclose_heartbeat/1",
                "runner_id": runner_id,
                "heartbeat_at": stamp.isoformat(),
            },
        )

    def write_stage(
        self,
        *,
        target_date: date,
        plan_id: str,
        generation: int,
        approval_id: str,
        status: StageState | str,
        reason: str,
        coverage: str = "unknown",
        exceptions_ref: str = "",
        as_of: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """写入非 CERTIFIED 中间/阻断阶段状态，同时保留上一 ``last_certified_date``。"""
        status_str = status.value if isinstance(status, StageState) else str(status)
        if status_str == StageState.CERTIFIED.value:
            raise ValueError("CERTIFIED must be written via publish_certified(PublishCertificate)")

        prev_certified: Optional[str] = None
        try:
            prev = self._read_raw_state()
            if prev:
                prev_certified = prev.get("last_certified_date")
        except Exception:
            prev_certified = None

        payload = {
            "schema": "postclose_publication_state/1",
            "target_date": target_date.isoformat(),
            "plan_id": plan_id,
            "generation": int(generation),
            "approval_id": approval_id,
            "status": status_str,
            "datasets": [],
            "as_of": as_of,
            "last_certified_date": prev_certified,
            "coverage": coverage,
            "exceptions_ref": exceptions_ref,
            "reason": reason,
            "certificate_digest": None,
            "certificate": None,
            "updated_at": _to_bj(now).isoformat(),
        }
        self._atomic_write(self.state_path, payload)
        return payload

    def publish_certified(
        self,
        cert: PublishCertificate,
        *,
        datasets: Sequence[str] = DEFAULT_PUBLISHED_DATASETS,
        as_of: Optional[str] = None,
        exceptions_ref: str = "",
        reason: str = "certified",
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """仅接受结构完整 ``PublishCertificate`` 原子发布 ``CERTIFIED`` 代际。"""
        if type(cert) is not PublishCertificate:
            raise TypeError("publish_certified requires a PublishCertificate instance")
        cert_digest = digest_object(cert)
        stamp = _to_bj(now)
        payload = {
            "schema": "postclose_publication_state/1",
            "target_date": cert.target_date.isoformat(),
            "plan_id": cert.plan_id,
            "generation": int(cert.generation),
            "approval_id": cert.approval_id,
            "status": StageState.CERTIFIED.value,
            "datasets": list(datasets),
            "as_of": as_of or cert.published_at.isoformat(),
            "last_certified_date": cert.target_date.isoformat(),
            "coverage": cert.coverage_status,
            "exceptions_ref": exceptions_ref,
            "reason": reason,
            "certificate_digest": cert_digest,
            "certificate": _serialize_publish_cert(cert),
            "updated_at": stamp.isoformat(),
        }
        self._atomic_write(self.state_path, payload)
        return payload

    def is_rebuilding(self, target_date: Optional[date] = None) -> bool:
        """快速读侧检查：控制面文件不存在时返回 False；损坏或处于重建态时 fail-closed 返回 True。"""
        if not self.state_path.exists():
            return False
        try:
            raw = self._read_raw_state()
        except Exception:
            # 控制面文件存在但损坏 → fail-closed 视作不可读/重建中
            return True
        if not raw:
            return False
        status = str(raw.get("status") or "")
        if status not in REBUILDING_STATUSES:
            return False
        if target_date is not None and str(raw.get("target_date") or "") != target_date.isoformat():
            return False
        return True

    def read_view(
        self,
        *,
        requested_date: Optional[date] = None,
        trusted_issuers: tuple[str, ...] = DEFAULT_TRUSTED_ISSUERS,
        now: Optional[datetime] = None,
    ) -> PublicationView:
        """只读读取当前发布视图；任何缺失、损坏、重建中、证书失效或日期错配均 fail-closed 返回 ``ready=False``。"""
        now_bj = _to_bj(now)
        req_iso = requested_date.isoformat() if requested_date is not None else ""

        if not self.control_root.is_dir() or not self.state_path.is_file():
            return PublicationView(
                target_date=req_iso,
                data_date=None,
                generation=0,
                status="not_ready",
                ready=False,
                datasets=(),
                as_of=None,
                last_certified_date=None,
                coverage="unknown",
                exceptions_ref="",
                reason="control_state_missing",
                certificate_digest=None,
            )

        try:
            raw = self._read_raw_state() or {}
        except Exception as exc:
            return PublicationView(
                target_date=req_iso,
                data_date=None,
                generation=0,
                status="not_ready",
                ready=False,
                datasets=(),
                as_of=None,
                last_certified_date=None,
                coverage="unknown",
                exceptions_ref="",
                reason=f"control_state_corrupt:{type(exc).__name__}",
                certificate_digest=None,
            )

        state_target = str(raw.get("target_date") or "")
        generation = int(raw.get("generation") or 0)
        status = str(raw.get("status") or "unknown")
        last_certified = raw.get("last_certified_date")
        coverage = str(raw.get("coverage") or "unknown")
        exceptions_ref = str(raw.get("exceptions_ref") or "")
        as_of = raw.get("as_of")
        pinned_digest = raw.get("certificate_digest")

        # 显式请求日 D 与当前状态日不符时，严禁把昨日偷换成 D
        if requested_date is not None and state_target != req_iso:
            return PublicationView(
                target_date=req_iso,
                data_date=None,
                generation=generation,
                status="not_ready",
                ready=False,
                datasets=(),
                as_of=None,
                last_certified_date=last_certified,
                coverage="unknown",
                exceptions_ref="",
                reason="requested_date_not_certified",
                certificate_digest=None,
            )

        if status == StageState.INTRADAY_CLOSED.value:
            return PublicationView(
                target_date=state_target,
                data_date=state_target,
                generation=generation,
                status=status,
                ready=False,
                datasets=(),
                as_of=as_of,
                last_certified_date=last_certified,
                coverage=coverage,
                exceptions_ref=exceptions_ref,
                reason="intraday_provisional_only",
                certificate_digest=None,
            )

        if status != StageState.CERTIFIED.value:
            return PublicationView(
                target_date=state_target,
                data_date=None,
                generation=generation,
                status="not_ready" if status in REBUILDING_STATUSES else status,
                ready=False,
                datasets=(),
                as_of=as_of,
                last_certified_date=last_certified,
                coverage=coverage,
                exceptions_ref=exceptions_ref,
                reason=str(raw.get("reason") or f"stage_{status.lower()}"),
                certificate_digest=None,
            )

        # status == CERTIFIED → 必须通过 PublishCertificate 完整性/时效/签发者/摘要核验
        cert = _deserialize_publish_cert(raw.get("certificate") or {})
        if cert is None or not isinstance(pinned_digest, str) or not pinned_digest:
            return PublicationView(
                target_date=state_target,
                data_date=None,
                generation=generation,
                status="not_ready",
                ready=False,
                datasets=(),
                as_of=None,
                last_certified_date=None,
                coverage="blocked",
                exceptions_ref=exceptions_ref,
                reason="certificate_missing_or_corrupt",
                certificate_digest=None,
            )

        try:
            target_d = date.fromisoformat(state_target)
        except ValueError:
            target_d = date.min

        valid = validate_certificate(
            cert,
            target_date=target_d,
            plan_id=str(raw.get("plan_id") or ""),
            generation=generation,
            approval_id=str(raw.get("approval_id") or ""),
            expected_digest=pinned_digest,
            trusted_issuers=trusted_issuers,
            now=now_bj,
        )
        if not valid:
            return PublicationView(
                target_date=state_target,
                data_date=None,
                generation=generation,
                status="not_ready",
                ready=False,
                datasets=(),
                as_of=None,
                last_certified_date=None,
                coverage="blocked",
                exceptions_ref=exceptions_ref,
                reason="certificate_validation_failed",
                certificate_digest=None,
            )

        return PublicationView(
            target_date=state_target,
            data_date=state_target,
            generation=generation,
            status=StageState.CERTIFIED.value,
            ready=True,
            datasets=tuple(str(x) for x in (raw.get("datasets") or DEFAULT_PUBLISHED_DATASETS)),
            as_of=as_of,
            last_certified_date=state_target,
            coverage=cert.coverage_status,
            exceptions_ref=exceptions_ref,
            reason=str(raw.get("reason") or "certified"),
            certificate_digest=pinned_digest,
        )


def read_with_generation_guard(
    store: PublicationStore,
    reader_fn: Callable[[PublicationView], Any],
    *,
    requested_date: Optional[date] = None,
    dataset: str = "all",
    trusted_issuers: tuple[str, ...] = DEFAULT_TRUSTED_ISSUERS,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> Tuple[PublicationView, Any]:
    """查询前后复核发布代际：门控在暖缓存/查库之前，查中换代即丢弃防混代。"""
    del dataset
    now_call = now_fn or (lambda: datetime.now(BEIJING_TZ))
    before = store.read_view(
        requested_date=requested_date,
        trusted_issuers=trusted_issuers,
        now=now_call(),
    )
    if not before.ready:
        return before, None

    data = reader_fn(before)

    after = store.read_view(
        requested_date=requested_date,
        trusted_issuers=trusted_issuers,
        now=now_call(),
    )
    if (
        not after.ready
        or after.target_date != before.target_date
        or after.generation != before.generation
        or after.status != before.status
        or after.certificate_digest != before.certificate_digest
    ):
        discarded_view = PublicationView(
            target_date=after.target_date or before.target_date,
            data_date=None,
            generation=after.generation,
            status="not_ready",
            ready=False,
            datasets=(),
            as_of=None,
            last_certified_date=after.last_certified_date,
            coverage="unknown",
            exceptions_ref=after.exceptions_ref,
            reason="mixed_generation_discarded",
            certificate_digest=None,
        )
        return discarded_view, None

    return after, data
