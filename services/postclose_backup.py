# -*- coding: utf-8 -*-
"""S6：分钟全域写前备份与孤立可恢复认证（无真实 DB 默认写客户端）。

契约要点：
1. 备份范围严格限定四张分钟表（``stock.kline_minute_{5,15,30,60}``），
   不向两张盘中快照表追加已豁免的备份要求。
2. 全列 ``raw`` 导出与确定性 FINAL 赢家核验：
   - 保留 Nullable ``amount=None`` 原样，绝不把 ``NULL`` 改写成 ``0``；
   - 若同 ``(code, datetime)`` 多版本行具有相同 ``created_at`` 且业务字段不一致
     （ReplacingMergeTree 赢家不确定），显式 fail-closed 拒绝签发认证；
3. 原子写入备份根目录 ``<backup_root>/<YYYYMMDD>/<plan_id>_g<generation>/``
   （校验目录边界防路径穿越、强制保留期 ``>= 30`` 天）；
4. 从磁盘重新读取归档核对 SHA-256、列完整性、条数及重建赢家摘要，并与当前锁内基线
   对比无漂移后，方签发 ``BackupCertificate(recovery_verified=True)``。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

from services.postclose_contracts import (
    BackupCertificate,
    CLEANUP_OBJECTS,
    ReturnCode,
    StageResult,
    StageState,
    digest_object,
)
from services.postclose_source_prepare import canonical_sha256

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

MINUTE_BACKUP_TABLES = (
    "stock.kline_minute_5",
    "stock.kline_minute_15",
    "stock.kline_minute_30",
    "stock.kline_minute_60",
)
# 验证与 CLEANUP_OBJECTS 前 4 项一致
assert MINUTE_BACKUP_TABLES == tuple(x[0] for x in CLEANUP_OBJECTS[:4])

MINUTE_COLUMNS = (
    "code",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "created_at",
    "id",
)

MIN_RETENTION_DAYS = 30
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class BackupError(RuntimeError):
    """备份导出、归档校验或孤立恢复认证失败。"""


class MinuteBackupReader(Protocol):
    """只读备份数据源接口（不提供任何写方法）。"""

    def fetch_raw_rows(self, table: str, target_date: date) -> list[dict[str, Any]]:
        ...

    def fetch_schema_columns(self, table: str) -> Sequence[str]:
        ...


@dataclass(frozen=True)
class BackupTableArtifact:
    table: str
    archive_path: str
    archive_sha256: str
    raw_count: int
    winner_count: int
    keys_digest: str
    winners_digest: str


@dataclass(frozen=True)
class BackupBundle:
    target_date: date
    plan_id: str
    generation: int
    approval_id: str
    retention_days: int
    backup_dir: str
    manifest_path: str
    artifacts: tuple[BackupTableArtifact, ...]
    backup_digest: str
    keys_digest: str
    baseline_digest: str
    certificate: Optional[BackupCertificate]
    stage_result: StageResult


def _to_bj(dt: datetime) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(BEIJING_TZ)


def _canonical_row(row: Mapping[str, Any], required_cols: Sequence[str] = MINUTE_COLUMNS) -> dict[str, Any]:
    missing = [col for col in required_cols if col not in row]
    if missing:
        raise BackupError(f"missing required columns: {missing}")

    amt = row["amount"]
    return {
        "code": str(row["code"]),
        "datetime": str(row["datetime"]),
        "open": None if row["open"] is None else float(row["open"]),
        "high": None if row["high"] is None else float(row["high"]),
        "low": None if row["low"] is None else float(row["low"]),
        "close": None if row["close"] is None else float(row["close"]),
        "volume": None if row["volume"] is None else float(row["volume"]),
        # 保留 NULL 语义，禁止转 0
        "amount": None if amt is None else float(amt),
        "created_at": "" if row["created_at"] is None else str(row["created_at"]),
        "id": int(row["id"]),
    }


def resolve_deterministic_winners(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 ReplacingMergeTree ``ORDER BY (code, datetime)`` 与 ``created_at`` 确定唯一赢家。

    若同键出现相同最大 ``created_at`` 且荷载不同，立即 fail-closed 抛 ``BackupError``。
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in rows:
        row = _canonical_row(item)
        key = (row["code"], row["datetime"])
        grouped.setdefault(key, []).append(row)

    winners: list[dict[str, Any]] = []
    for key in sorted(grouped):
        versions = grouped[key]
        versions.sort(key=lambda r: (r["created_at"], r["id"]))
        top_created = versions[-1]["created_at"]
        top_tied = [v for v in versions if v["created_at"] == top_created]
        if len(top_tied) > 1:
            payloads = {
                (v["open"], v["high"], v["low"], v["close"], v["volume"], v["amount"], v["id"])
                for v in top_tied
            }
            if len(payloads) > 1:
                raise BackupError(f"ambiguous FINAL winner for key={key} created_at={top_created!r}")
        winners.append(versions[-1])
    return winners


def _safe_backup_dir(backup_root: Path, target_date: date, plan_id: str, generation: int) -> Path:
    if not _SAFE_ID_RE.fullmatch(plan_id):
        raise BackupError(f"unsafe plan_id: {plan_id!r}")
    if type(generation) is not int or generation < 1:
        raise BackupError(f"invalid generation: {generation!r}")
    root_resolved = backup_root.resolve()
    candidate = (root_resolved / target_date.strftime("%Y%m%d") / f"{plan_id}_g{generation}").resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise BackupError(f"backup path escaped root: {candidate}") from exc
    return candidate


def _write_gzip_jsonl_atomic(path: Path, rows: Sequence[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    lines = [json.dumps(r, ensure_ascii=True, sort_keys=True, separators=(",", ":")) for r in rows]
    raw_bytes = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    compressed = gzip.compress(raw_bytes, compresslevel=6, mtime=0)
    tmp.write_bytes(compressed)
    os.replace(tmp, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_gzip_jsonl(path: Path, expected_sha256: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise BackupError(f"archive missing: {path}")
    blob = path.read_bytes()
    actual_sha = hashlib.sha256(blob).hexdigest()
    if actual_sha != expected_sha256:
        raise BackupError(f"archive sha256 mismatch for {path.name}")
    try:
        text = gzip.decompress(blob).decode("utf-8")
    except Exception as exc:
        raise BackupError(f"corrupt or truncated archive {path.name}: {exc}") from exc

    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception as exc:
            raise BackupError(f"corrupt JSON line {idx} in {path.name}: {exc}") from exc
        if not isinstance(obj, dict):
            raise BackupError(f"invalid JSON object at line {idx} in {path.name}")
        rows.append(_canonical_row(obj))
    return rows


def export_and_verify_minute_backup(
    *,
    reader: MinuteBackupReader,
    backup_root: Path,
    target_date: date,
    plan_id: str,
    generation: int,
    approval_id: str,
    schema_digest: str,
    now: datetime,
    retention_days: int = MIN_RETENTION_DAYS,
    issuer: str = "postclose_backup",
    expected_baseline_digests: Optional[Mapping[str, str]] = None,
    certificate_ttl: timedelta = timedelta(hours=6),
    cleanup_objects: tuple = CLEANUP_OBJECTS,
) -> BackupBundle:
    """导出 4 张分钟表 raw 行、重读归档进行孤立恢复比对并核对基线无漂移后签发 BackupCertificate。"""
    now_bj = _to_bj(now)
    if cleanup_objects != CLEANUP_OBJECTS:
        raise BackupError("unexpected cleanup_objects scope")
    if type(retention_days) is not int or retention_days < MIN_RETENTION_DAYS:
        raise BackupError(f"retention_days must be >= {MIN_RETENTION_DAYS}, got {retention_days!r}")

    backup_dir = _safe_backup_dir(Path(backup_root), target_date, plan_id, generation)
    backup_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[BackupTableArtifact] = []
    all_keys_digest_map: dict[str, str] = {}
    all_winners_digest_map: dict[str, str] = {}
    archive_digest_map: dict[str, str] = {}

    try:
        for table in MINUTE_BACKUP_TABLES:
            cols = tuple(reader.fetch_schema_columns(table))
            missing_cols = [c for c in MINUTE_COLUMNS if c not in cols]
            if missing_cols:
                raise BackupError(f"schema drift on {table}: missing columns {missing_cols}")

            raw_rows_in = reader.fetch_raw_rows(table, target_date)
            canonical_raw = [_canonical_row(r) for r in raw_rows_in]
            canonical_raw.sort(key=lambda r: (r["code"], r["datetime"], r["created_at"], r["id"]))
            expected_winners = resolve_deterministic_winners(canonical_raw)
            expected_winners_digest = canonical_sha256(expected_winners)
            expected_keys = sorted({(r["code"], r["datetime"]) for r in expected_winners})
            expected_keys_digest = canonical_sha256(expected_keys)

            if expected_baseline_digests is not None and table in expected_baseline_digests:
                if expected_baseline_digests[table] != expected_winners_digest:
                    raise BackupError(f"baseline drift detected on {table}")

            safe_tbl = table.replace(".", "_")
            archive_path = backup_dir / f"{safe_tbl}.jsonl.gz"
            archive_sha = _write_gzip_jsonl_atomic(archive_path, canonical_raw)

            # 孤立读回校验：重新读取磁盘归档 → 验证 sha256/行数/条列/重算 FINAL 赢家
            reloaded_raw = _read_gzip_jsonl(archive_path, archive_sha)
            if len(reloaded_raw) != len(canonical_raw):
                raise BackupError(f"archive row count mismatch after reload on {table}")
            recovered_winners = resolve_deterministic_winners(reloaded_raw)
            recovered_winners_digest = canonical_sha256(recovered_winners)
            if recovered_winners_digest != expected_winners_digest:
                raise BackupError(f"isolated recovery winner mismatch on {table}")

            # 再次查询基线（防御备份过程中目标发生漂移）
            post_read_raw = [_canonical_row(r) for r in reader.fetch_raw_rows(table, target_date)]
            post_winners_digest = canonical_sha256(resolve_deterministic_winners(post_read_raw))
            if post_winners_digest != expected_winners_digest:
                raise BackupError(f"target table mutated during backup export on {table}")

            artifact = BackupTableArtifact(
                table=table,
                archive_path=str(archive_path),
                archive_sha256=archive_sha,
                raw_count=len(canonical_raw),
                winner_count=len(recovered_winners),
                keys_digest=expected_keys_digest,
                winners_digest=recovered_winners_digest,
            )
            artifacts.append(artifact)
            all_keys_digest_map[table] = expected_keys_digest
            all_winners_digest_map[table] = recovered_winners_digest
            archive_digest_map[table] = archive_sha

        backup_digest = canonical_sha256(archive_digest_map)
        keys_digest = canonical_sha256(all_keys_digest_map)
        baseline_digest = canonical_sha256(all_winners_digest_map)

        manifest_path = backup_dir / "backup_manifest.json"
        manifest_payload = {
            "schema": "postclose_backup_manifest/1",
            "target_date": target_date.isoformat(),
            "plan_id": plan_id,
            "generation": generation,
            "approval_id": approval_id,
            "retention_days": retention_days,
            "created_at": now_bj.isoformat(),
            "backup_digest": backup_digest,
            "keys_digest": keys_digest,
            "baseline_digest": baseline_digest,
            "tables": [
                {
                    "table": a.table,
                    "archive_path": a.archive_path,
                    "archive_sha256": a.archive_sha256,
                    "raw_count": a.raw_count,
                    "winner_count": a.winner_count,
                    "keys_digest": a.keys_digest,
                    "winners_digest": a.winners_digest,
                }
                for a in artifacts
            ],
        }
        tmp_manifest = manifest_path.with_suffix(".json.tmp")
        tmp_manifest.write_text(
            json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_manifest, manifest_path)

        cert = BackupCertificate(
            target_date=target_date,
            plan_id=plan_id,
            generation=generation,
            approval_id=approval_id,
            issued_at=now_bj,
            expires_at=now_bj + certificate_ttl,
            issuer=issuer,
            evidence_refs=(f"backup_manifest:{manifest_path.name}",),
            schema_digest=schema_digest,
            backup_digest=backup_digest,
            keys_digest=keys_digest,
            baseline_digest=baseline_digest,
            recovery_evidence_ref=str(manifest_path),
            recovery_verified=True,
        )
        sr = StageResult(
            target_date=target_date,
            plan_id=plan_id,
            generation=generation,
            outcome=StageState.BACKED_UP,
            reason_code="backup_and_recovery_verified",
            rc=int(ReturnCode.COMPLETED),
            source_attempted=False,
            source_completed=False,
            write_attempted=False,
            writes_known=True,
            accepted_keys=0,
            rejected_keys=0,
            exception_ledger_ref="",
            coverage_status="unknown",
            certificate_ref=f"backup_cert:{digest_object(cert)}",
            retry_safe=True,
            alarm_severity="none",
        )
        return BackupBundle(
            target_date=target_date,
            plan_id=plan_id,
            generation=generation,
            approval_id=approval_id,
            retention_days=retention_days,
            backup_dir=str(backup_dir),
            manifest_path=str(manifest_path),
            artifacts=tuple(artifacts),
            backup_digest=backup_digest,
            keys_digest=keys_digest,
            baseline_digest=baseline_digest,
            certificate=cert,
            stage_result=sr,
        )
    except Exception as exc:
        empty_digest = canonical_sha256({})
        sr = StageResult(
            target_date=target_date,
            plan_id=plan_id,
            generation=generation,
            outcome=StageState.BLOCKED,
            reason_code=f"backup_failed:{type(exc).__name__}",
            rc=int(ReturnCode.BLOCKED),
            source_attempted=False,
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
            concurrent_events=("blocked",),
        )
        return BackupBundle(
            target_date=target_date,
            plan_id=plan_id,
            generation=generation,
            approval_id=approval_id,
            retention_days=retention_days,
            backup_dir=str(backup_dir),
            manifest_path="",
            artifacts=tuple(artifacts),
            backup_digest=empty_digest,
            keys_digest=empty_digest,
            baseline_digest=empty_digest,
            certificate=None,
            stage_result=sr,
        )
