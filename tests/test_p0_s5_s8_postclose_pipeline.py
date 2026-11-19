# -*- coding: utf-8 -*-
"""P0 第二批（S5 / S6 / S7 / S8）盘后全链单元测试。

覆盖：
- S5 源认证、16:00 后下载证据、全拒绝账本不截断与 reconstruction_ready 分离；
- S6 分钟四表写前备份、NULL/多版本赢家与孤立恢复校验；
- S7 16:15 前精准清理、force 禁绕门、mood realtime 失效不伤 final、UNKNOWN_WRITES/RECOVERY_REQUIRED；
- S8 单进程全链编排、16:15 导入门、15/30/60 分桶不跨午休与 NULL 传播、build_tdx_minute_periods 旁路封堵。
"""

from __future__ import annotations

import gzip
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.market_write_admission import AttemptState, WriteAdmission  # noqa: E402
from services.postclose_backup import (  # noqa: E402
    MINUTE_BACKUP_TABLES,
    MINUTE_COLUMNS,
    export_and_verify_minute_backup,
    resolve_deterministic_winners,
)
from services.postclose_cleanup import run_postclose_cleanup  # noqa: E402
from services.postclose_contracts import (  # noqa: E402
    CLEANUP_OBJECTS,
    CloseCertificate,
    PlanManifest,
    ReturnCode,
    StageState,
    day_window,
    digest_object,
    validate_certificate,
    validate_manifest,
    validate_stage_result,
)
from services.postclose_journal import MemoryJournal  # noqa: E402
from services.postclose_orchestrator import (  # noqa: E402
    SESSION_5M_SLOTS,
    aggregate_derived_bars_from_5m,
    run_postclose_orchestration,
)
from services.postclose_source_prepare import (  # noqa: E402
    DEFAULT_FROZEN_INDEX_CODES,
    LocalFileSnapshot,
    SourcePreparationError,
    canonical_sha256,
    prepare_postclose_sources,
    validate_download_evidence,
    validate_minute_5m_candidate,
    verify_candidate_digest,
)

BJ = ZoneInfo("Asia/Shanghai")
TD = date(2026, 11, 18)
SCHEMA_DIGEST = canonical_sha256({"version": "postclose-v1"})


def _at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 18, h, m, s, tzinfo=BJ)


def _snaps(mtime: datetime | None = None, *, sha_after: str | None = None, size: int = 320, markets=("sh", "sz", "bj")) -> list[LocalFileSnapshot]:
    ts = mtime or _at(16, 2, 0)
    return [
        LocalFileSnapshot(
            market=mkt,
            path=f"D:/TDX/vipdoc/{mkt}/lday/sample_{mkt}.day",
            mtime_bj=ts,
            byte_size=size,
            has_target_date=True,
            sha256_before=f"sha_{mkt}_1",
            sha256_after=sha_after if sha_after is not None else f"sha_{mkt}_1",
        )
        for mkt in markets
    ]


def _daily_rows(extra_codes=("600000.SH", "000001.SZ", "920002.BJ")) -> list[dict]:
    rows = []
    seen = set()
    for code in list(DEFAULT_FROZEN_INDEX_CODES) + list(extra_codes):
        if code in seen:
            continue
        seen.add(code)
        rows.append(
            {
                "code": code,
                "trade_date": TD.isoformat(),
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 1000.0,
                "amount": 102000.0,
            }
        )
    return rows


def _bars_48(code: str, *, null_slot: str | None = None) -> list[dict]:
    out = []
    for idx, hhmm in enumerate(SESSION_5M_SLOTS):
        base = 10.0 + idx * 0.01
        out.append(
            {
                "code": code,
                "datetime": f"{TD.isoformat()} {hhmm}:00",
                "open": round(base, 2),
                "high": round(base + 0.05, 2),
                "low": round(base - 0.05, 2),
                "close": round(base + 0.02, 2),
                "volume": 100.0,
                "amount": None if hhmm == null_slot else 1000.0,
            }
        )
    return out


class _FakeLockContext:
    def __init__(self, *, fail_acquire: bool = False, fail_assert: bool = False):
        self.fail_acquire = fail_acquire
        self.fail_assert = fail_assert
        self.held = False
        self.acquired_count = 0
        self.released_count = 0
        self.heartbeats = 0

    def acquire(self):
        if self.fail_acquire:
            raise RuntimeError("lock already held by another process")
        self.held = True
        self.acquired_count += 1

    def assert_held(self, **_kwargs):
        if self.fail_assert or not self.held:
            raise RuntimeError("lease lost")

    def heartbeat(self):
        if self.fail_assert or not self.held:
            raise RuntimeError("heartbeat failed")
        self.heartbeats += 1

    @property
    def endpoint(self):
        return "db:8123"

    @property
    def database(self):
        return "stock"

    def release(self):
        self.held = False
        self.released_count += 1


class _FakeBackupReader:
    def __init__(self, rows_by_table: dict[str, list[dict]], *, cols=MINUTE_COLUMNS, drift_on_second_read: bool = False):
        self.rows_by_table = rows_by_table
        self.cols = cols
        self.drift_on_second_read = drift_on_second_read
        self._reads: dict[str, int] = {}

    def fetch_schema_columns(self, table: str):
        return tuple(self.cols)

    def fetch_raw_rows(self, table: str, target_date: date):
        cnt = self._reads.get(table, 0) + 1
        self._reads[table] = cnt
        rows = [dict(r) for r in self.rows_by_table.get(table, [])]
        if self.drift_on_second_read and cnt >= 2 and rows:
            rows[0]["close"] = 999.0
        return rows


class _FakeCleanupGateway:
    def __init__(
        self,
        *,
        is_done: bool = True,
        raise_on_table: str | None = None,
        residual_table: str | None = None,
        mutate_sentinel_after: bool = False,
        on_delete_tick=None,
    ):
        self.is_done = is_done
        self.raise_on_table = raise_on_table
        self.residual_table = residual_table
        self.mutate_sentinel_after = mutate_sentinel_after
        self.on_delete_tick = on_delete_tick
        self.deleted: list[tuple[str, datetime, datetime, str, str]] = []
        self._sentinel_calls = 0

    def snapshot_non_target_sentinels(self, tables, target_date: date) -> dict:
        self._sentinel_calls += 1
        if self.mutate_sentinel_after and self._sentinel_calls >= 2:
            # 条数相同但逻辑值被改写
            return {t: {"d_minus_1_count": 100, "d_minus_1_xor": 999999} for t in tables}
        return {t: {"d_minus_1_count": 100, "d_minus_1_xor": 123456} for t in tables}

    def delete_day_window(self, table: str, window_start: datetime, window_end: datetime, time_col: str, *, attempt_id: str) -> dict:
        if self.on_delete_tick is not None:
            self.on_delete_tick(table)
        if self.raise_on_table == table:
            raise TimeoutError(f"HTTP timeout deleting {table}")
        self.deleted.append((table, window_start, window_end, time_col, attempt_id))
        return {"mutation_id": f"mut_{len(self.deleted)}"}

    def wait_mutation_terminal(self, table: str, mutation_info) -> dict:
        return {"is_done": self.is_done, "error": "" if self.is_done else "still_running"}

    def verify_residual_zero(self, table: str, window_start: datetime, window_end: datetime, time_col: str) -> dict:
        if self.residual_table == table:
            return {"raw_count": 1, "final_count": 1}
        return {"raw_count": 0, "final_count": 0}


class _FakeImportGateway:
    def __init__(self, existing_daily: list[dict] | None = None, fail_table: str | None = None):
        self.existing_daily = list(existing_daily or [])
        self.fail_table = fail_table
        self.daily_rows: list[dict] = list(existing_daily or [])
        self.minute_rows: dict[str, list[dict]] = {}

    def fetch_existing_daily_rows(self, target_date: date) -> list[dict]:
        return [dict(r) for r in self.existing_daily]

    def insert_daily_rows(self, rows, *, attempt_id: str) -> int:
        if self.fail_table == "stock.kline_daily":
            raise TimeoutError("daily insert response lost")
        for r in rows:
            self.daily_rows.append(dict(r))
        return len(rows)

    def verify_daily_rows(self, target_date: date) -> list[dict]:
        return [dict(r) for r in self.daily_rows]

    def insert_minute_rows(self, table: str, rows, *, attempt_id: str) -> int:
        if self.fail_table == table:
            raise TimeoutError(f"insert timeout on {table}")
        bucket = self.minute_rows.setdefault(table, [])
        for r in rows:
            bucket.append(dict(r))
        return len(rows)

    def verify_minute_rows(self, table: str, target_date: date) -> list[dict]:
        return [dict(r) for r in self.minute_rows.get(table, [])]


def _make_close_cert() -> CloseCertificate:
    return CloseCertificate(
        target_date=TD,
        plan_id="plan-20261118",
        generation=1,
        approval_id="apr-001",
        issued_at=_at(15, 4, 58),
        expires_at=_at(21, 4, 58),
        issuer="close_certifier",
        evidence_refs=("close:attempts=0",),
        schema_digest=SCHEMA_DIGEST,
        attempts_digest=canonical_sha256([]),
        server_evidence_digest=canonical_sha256({"active": 0}),
        fencing_evidence_ref="fencing:rw_locked",
        unresolved_writes=0,
        closed_at=_at(15, 4, 50),
    )


def _build_pinned_manifest(
    *,
    source_cert_digest: str,
    backup_cert_digest: str,
    close_cert_digest: str,
    delete_start_latest: datetime | None = None,
) -> PlanManifest:
    win_start, win_end = day_window(TD)
    dl_deadline = _at(16, 15, 0)
    dsl = delete_start_latest or _at(16, 12, 0)
    target_fps = tuple((obj[0], canonical_sha256({"table": obj[0]})) for obj in CLEANUP_OBJECTS[:-1])
    return PlanManifest(
        target_date=TD,
        plan_id="plan-20261118",
        generation=1,
        approval_id="apr-001",
        policy_decision_ref="policy:20261118",
        operation_permit_refs=(
            ("delete", "perm:del:1"),
            ("insert", "perm:ins:1"),
            ("invalidate", "perm:inv:1"),
            ("recovery", "perm:rec:1"),
        ),
        target_schema_fingerprints=target_fps,
        predicate_digest=canonical_sha256({"start": win_start, "end": win_end}),
        observed_raw_keys_digest=canonical_sha256([]),
        observed_final_keys_digest=canonical_sha256([]),
        observed_payload_digest=canonical_sha256({}),
        identity_version="id-v1",
        expected_keys_digest=canonical_sha256(["600000.SH", "000001.SZ"]),
        exception_ledger_ref="ledger:empty",
        excluded_objects=("stock.kline_minute_1", "stock.kline_daily", "account_objects"),
        certificate_digests=(
            ("source", source_cert_digest),
            ("backup", backup_cert_digest),
            ("close", close_cert_digest),
        ),
        non_target_sentinel_digest=canonical_sha256({"sentinel": "baseline"}),
        delete_start_latest=dsl,
        clean_deadline=dl_deadline,
        import_not_before=dl_deadline,
        lease_budget=timedelta(minutes=25),
        recovery_strategy="restore_from_minute_archive",
        read_impact="not_ready_during_rebuild",
        alarm_owner="owner@aistock",
        window_start=win_start,
        window_end=win_end,
    )


class TestS5SourcePrepare:
    def test_rejects_early_download_uncertain_missing_market_and_mutated_file(self):
        early = validate_download_evidence(
            TD, {"state": "completed", "progress": 100, "completed_at": _at(15, 59, 59)}, _snaps()
        )
        assert early.ok is False and early.reason == "download_before_1600"

        uncertain = validate_download_evidence(
            TD, {"state": "uncertain", "progress": 0, "completed_at": _at(16, 2, 0)}, _snaps()
        )
        assert uncertain.ok is False and "uncertain" in uncertain.reason

        missing_bj = validate_download_evidence(
            TD, {"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            _snaps(markets=("sh", "sz")),
        )
        assert missing_bj.ok is False and "bj" in missing_bj.reason

        mutated = validate_download_evidence(
            TD, {"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            _snaps(sha_after="changed_hash"),
        )
        assert mutated.ok is False and "file_mutated_during_read" in mutated.reason

    def test_terminal_rejected_gaps_keeps_full_ledger_and_blocks_reconstruction(self):
        codes = [f"60{i:04d}.SH" for i in range(65)]
        res = validate_minute_5m_candidate(TD, codes, {})
        assert res.rc == int(ReturnCode.COMPLETED)
        assert res.outcome is StageState.TERMINAL_REJECTED_GAPS
        assert res.source_task_success is True
        assert res.reconstruction_ready is False
        assert res.publish_ready is False
        # 严禁截断 [:50]
        assert len(res.rejected_keys) == 65

    def test_fallback_forbidden_after_write_error_and_null_amount_preserved(self):
        bundle = prepare_postclose_sources(
            target_date=TD,
            plan_id="plan-20261118",
            generation=1,
            approval_id="apr-001",
            schema_digest=SCHEMA_DIGEST,
            download_report=None,
            file_snapshots=(),
            daily_rows=_daily_rows(),
            expected_minute_keys=["600000.SH"],
            minute_bars_by_code={"600000.SH": _bars_48("600000.SH", null_slot="10:00")},
            now=_at(16, 5, 0),
            daily_source_mode="official_fallback",
            prior_write_error=True,
        )
        assert bundle.certificate is None
        assert bundle.stage_result.outcome is StageState.BLOCKED
        assert bundle.daily.reason == "fallback_forbidden_after_write_error"

        # 正常官网回退（先于写前）+ 带 NULL 成交额分钟 → NULL 保持 None 且证书摘要不可篡改
        ok_bundle = prepare_postclose_sources(
            target_date=TD,
            plan_id="plan-20261118",
            generation=1,
            approval_id="apr-001",
            schema_digest=SCHEMA_DIGEST,
            download_report={"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            file_snapshots=_snaps(),
            daily_rows=_daily_rows(),
            expected_minute_keys=["600000.SH"],
            minute_bars_by_code={"600000.SH": _bars_48("600000.SH", null_slot="10:00")},
            now=_at(16, 5, 0),
            daily_source_mode="local_lday",
        )
        assert ok_bundle.certificate is not None
        assert validate_stage_result(ok_bundle.stage_result, target_date=TD, plan_id="plan-20261118", generation=1)
        amt_1000 = next(b["amount"] for b in ok_bundle.minute.accepted_bars_by_code["600000.SH"] if "10:00" in b["datetime"])
        assert amt_1000 is None
        with pytest.raises(SourcePreparationError):
            verify_candidate_digest({"daily": [], "minute": {}}, ok_bundle.candidate_digest)


class TestS6Backup:
    def test_ambiguous_final_winner_and_drift_and_corrupt_archive_rejected(self, tmp_path):
        dup_rows = [
            {
                "code": "600000.SH", "datetime": "2026-11-18 09:35:00",
                "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
                "volume": 100.0, "amount": None, "created_at": "2026-11-18 09:35:05", "id": 1,
            },
            {
                "code": "600000.SH", "datetime": "2026-11-18 09:35:00",
                "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.15,
                "volume": 100.0, "amount": None, "created_at": "2026-11-18 09:35:05", "id": 2,
            },
        ]
        with pytest.raises(Exception, match="ambiguous FINAL winner"):
            resolve_deterministic_winners(dup_rows)

        base_row = dict(dup_rows[0])
        reader = _FakeBackupReader({t: [dict(base_row)] for t in MINUTE_BACKUP_TABLES})
        ok = export_and_verify_minute_backup(
            reader=reader,
            backup_root=tmp_path,
            target_date=TD,
            plan_id="plan-20261118",
            generation=1,
            approval_id="apr-001",
            schema_digest=SCHEMA_DIGEST,
            now=_at(16, 6, 0),
        )
        assert ok.certificate is not None
        assert ok.certificate.recovery_verified is True
        assert len(ok.artifacts) == 4
        assert ok.stage_result.outcome is StageState.BACKED_UP

        # 基线漂移拒绝签发
        drifted = export_and_verify_minute_backup(
            reader=_FakeBackupReader({t: [dict(base_row)] for t in MINUTE_BACKUP_TABLES}, drift_on_second_read=True),
            backup_root=tmp_path / "drift",
            target_date=TD, plan_id="plan-20261118", generation=1,
            approval_id="apr-001", schema_digest=SCHEMA_DIGEST, now=_at(16, 6, 0),
        )
        assert drifted.certificate is None
        assert drifted.stage_result.outcome is StageState.BLOCKED

        # 保留期 <30 天 或 路径穿越拒绝
        with pytest.raises(Exception, match="retention_days"):
            export_and_verify_minute_backup(
                reader=reader, backup_root=tmp_path, target_date=TD,
                plan_id="plan-20261118", generation=1, approval_id="apr-001",
                schema_digest=SCHEMA_DIGEST, now=_at(16, 6, 0), retention_days=7,
            )


class TestS7CleanupAndMood:
    def _prereqs(self, tmp_path):
        close_cert = _make_close_cert()
        src = prepare_postclose_sources(
            target_date=TD, plan_id="plan-20261118", generation=1, approval_id="apr-001",
            schema_digest=SCHEMA_DIGEST,
            download_report={"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            file_snapshots=_snaps(), daily_rows=_daily_rows(),
            expected_minute_keys=["600000.SH"],
            minute_bars_by_code={"600000.SH": _bars_48("600000.SH")},
            now=_at(16, 5, 0),
        )
        bk_reader = _FakeBackupReader({t: [] for t in MINUTE_BACKUP_TABLES})
        bk = export_and_verify_minute_backup(
            reader=bk_reader, backup_root=tmp_path, target_date=TD,
            plan_id="plan-20261118", generation=1, approval_id="apr-001",
            schema_digest=SCHEMA_DIGEST, now=_at(16, 6, 0),
        )
        manifest = _build_pinned_manifest(
            source_cert_digest=digest_object(src.certificate),
            backup_cert_digest=digest_object(bk.certificate),
            close_cert_digest=digest_object(close_cert),
        )
        return manifest, src.certificate, bk.certificate, close_cert

    def test_force_cannot_bypass_missing_permit_or_deadline(self, tmp_path):
        manifest, src_cert, bk_cert, close_cert = self._prereqs(tmp_path)
        gw = _FakeCleanupGateway()
        lock = _FakeLockContext()
        lock.acquire()

        rep_perm = run_postclose_cleanup(
            manifest=manifest, source_cert=src_cert, backup_cert=bk_cert, close_cert=close_cert,
            trusted_issuers=("postclose_source_prepare", "postclose_backup", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "WRONG", "invalidate": "perm:inv:1"},
            gateway=gw, journal=MemoryJournal(), lock_ctx=lock,
            now_fn=lambda: _at(16, 10, 0), schema_digest=SCHEMA_DIGEST,
            dry_run=False, force=True,
        )
        assert rep_perm.stage_result.outcome is StageState.BLOCKED
        assert gw.deleted == []

        # 过 delete_start_latest → DEADLINE_MISSED 零删除
        rep_dl = run_postclose_cleanup(
            manifest=manifest, source_cert=src_cert, backup_cert=bk_cert, close_cert=close_cert,
            trusted_issuers=("postclose_source_prepare", "postclose_backup", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "perm:del:1", "invalidate": "perm:inv:1"},
            gateway=gw, journal=MemoryJournal(), lock_ctx=lock,
            now_fn=lambda: _at(16, 12, 0), schema_digest=SCHEMA_DIGEST,
            dry_run=False, force=True,
        )
        assert rep_dl.stage_result.outcome is StageState.DEADLINE_MISSED
        assert rep_dl.stage_result.rc == int(ReturnCode.DEADLINE_MISSED)
        assert gw.deleted == []

    def test_cleanup_preserves_mood_final_and_detects_unknown_writes_and_sentinel_tampering(self, tmp_path):
        import services.market_mood as mm

        manifest, src_cert, bk_cert, close_cert = self._prereqs(tmp_path)
        day_str = TD.strftime("%Y%m%d")
        mm.write_day_payload(day_str, realtime={"date": TD.isoformat(), "as_of": "2026-11-18 14:55", "limit_up_count": 21}, out_root=tmp_path)
        mm.write_day_payload(day_str, final={"mode": "final", "date": TD.isoformat(), "limit_up_count": 25}, out_root=tmp_path)

        gw = _FakeCleanupGateway()
        journal = MemoryJournal()
        lock = _FakeLockContext()
        lock.acquire()

        ok = run_postclose_cleanup(
            manifest=manifest, source_cert=src_cert, backup_cert=bk_cert, close_cert=close_cert,
            trusted_issuers=("postclose_source_prepare", "postclose_backup", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "perm:del:1", "invalidate": "perm:inv:1"},
            gateway=gw, journal=journal, lock_ctx=lock,
            now_fn=lambda: _at(16, 10, 0),
            mood_invalidator=lambda d, ts: mm.invalidate_day_realtime(d.strftime("%Y%m%d"), invalidated_at=ts, out_root=tmp_path),
            schema_digest=SCHEMA_DIGEST, dry_run=False,
        )
        assert ok.stage_result.outcome is StageState.CLEANED
        assert ok.certificate is not None
        assert validate_stage_result(ok.stage_result, target_date=TD, plan_id="plan-20261118", generation=1)
        assert [row[0] for row in gw.deleted] == [obj[0] for obj in CLEANUP_OBJECTS[:-1]]

        # final 完整保留、realtime 已移除、后续不得复活、GET 不回退成被清 realtime
        loaded = mm.load_day_payload(day_str, out_root=tmp_path)
        assert loaded["final"]["limit_up_count"] == 25
        assert "realtime" not in loaded
        with pytest.raises(RuntimeError, match="禁止盘后复活"):
            mm.write_day_payload(day_str, realtime={"limit_up_count": 99}, out_root=tmp_path)
        assert mm.limit_up_stats(now=_at(16, 12, 0), out_root=tmp_path)["mode"] == "final"

        # 坏 JSON 不以空字典覆写已有内容（fail-closed）
        corrupt_day = "20261119"
        p = mm.day_path(corrupt_day, out_root=tmp_path)
        p.write_text("{corrupt", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            mm.invalidate_day_realtime(corrupt_day, out_root=tmp_path)

        # 超时/is_done=False → UNKNOWN_WRITES (rc=6)
        gw_timeout = _FakeCleanupGateway(raise_on_table="stock.kline_minute_15")
        rep_unk = run_postclose_cleanup(
            manifest=manifest, source_cert=src_cert, backup_cert=bk_cert, close_cert=close_cert,
            trusted_issuers=("postclose_source_prepare", "postclose_backup", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "perm:del:1", "invalidate": "perm:inv:1"},
            gateway=gw_timeout, journal=MemoryJournal(), lock_ctx=lock,
            now_fn=lambda: _at(16, 10, 0), schema_digest=SCHEMA_DIGEST, dry_run=False,
        )
        assert rep_unk.stage_result.outcome is StageState.UNKNOWN_WRITES
        assert rep_unk.stage_result.rc == int(ReturnCode.UNKNOWN_WRITES)
        assert rep_unk.stage_result.writes_known is False

        # 同 count 非目标哨兵逻辑值变更 → RECOVERY_REQUIRED (rc=7)
        gw_sentinel = _FakeCleanupGateway(mutate_sentinel_after=True)
        rep_rec = run_postclose_cleanup(
            manifest=manifest, source_cert=src_cert, backup_cert=bk_cert, close_cert=close_cert,
            trusted_issuers=("postclose_source_prepare", "postclose_backup", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "perm:del:1", "invalidate": "perm:inv:1"},
            gateway=gw_sentinel, journal=MemoryJournal(), lock_ctx=lock,
            now_fn=lambda: _at(16, 10, 0), schema_digest=SCHEMA_DIGEST, dry_run=False,
        )
        assert rep_rec.stage_result.outcome is StageState.RECOVERY_REQUIRED
        assert rep_rec.stage_result.rc == int(ReturnCode.RECOVERY_REQUIRED)


class TestS8OrchestratorAndLegacyChoke:
    def _build_full_inputs(self, tmp_path):
        close_cert = _make_close_cert()
        bars = {"600000.SH": _bars_48("600000.SH", null_slot="10:00")}
        src = prepare_postclose_sources(
            target_date=TD, plan_id="plan-20261118", generation=1, approval_id="apr-001",
            schema_digest=SCHEMA_DIGEST,
            download_report={"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            file_snapshots=_snaps(), daily_rows=_daily_rows(),
            expected_minute_keys=["600000.SH"], minute_bars_by_code=bars,
            now=_at(16, 5, 0), issuer="postclose_orchestrator",
        )
        bk_reader = _FakeBackupReader({t: [] for t in MINUTE_BACKUP_TABLES})
        bk = export_and_verify_minute_backup(
            reader=bk_reader, backup_root=tmp_path / "backup_pin", target_date=TD,
            plan_id="plan-20261118", generation=1, approval_id="apr-001",
            schema_digest=SCHEMA_DIGEST, now=_at(16, 6, 0), issuer="postclose_orchestrator",
        )
        manifest = _build_pinned_manifest(
            source_cert_digest=digest_object(src.certificate),
            backup_cert_digest=digest_object(bk.certificate),
            close_cert_digest=digest_object(close_cert),
        )
        assert validate_manifest(manifest)
        return manifest, close_cert, bars

    def test_end_to_end_certified_pipeline_and_null_and_bucket_invariants(self, tmp_path):
        manifest, close_cert, bars = self._build_full_inputs(tmp_path)
        lock = _FakeLockContext()
        journal = MemoryJournal()
        cleanup_gw = _FakeCleanupGateway()
        import_gw = _FakeImportGateway()
        phase = {"calls": 0, "mood_done": False, "after_mood": 0}

        def _clock():
            phase["calls"] += 1
            if phase["calls"] == 1:
                return _at(16, 5, 0)   # S5
            if phase["calls"] <= 3:
                return _at(16, 6, 0)   # S6
            if not phase["mood_done"]:
                return _at(16, 10, 30)  # S7 清理全程 < 16:15
            phase["after_mood"] += 1
            if phase["after_mood"] <= 3:
                return _at(16, 11, 0)   # mood 终态登记 + CleanCertificate (< 16:15)
            return _at(16, 15, 5)       # CleanCertificate 已签，进入 16:15+ 导入窗口

        def _mood_inv(d, ts):
            phase["mood_done"] = True
            return {"day": d.strftime("%Y%m%d")}

        res = run_postclose_orchestration(
            manifest=manifest, close_cert=close_cert,
            trusted_issuers=("postclose_orchestrator", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "perm:del:1", "insert": "perm:ins:1", "invalidate": "perm:inv:1"},
            schema_digest=SCHEMA_DIGEST,
            download_report={"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            file_snapshots=_snaps(), daily_rows=_daily_rows(),
            expected_minute_keys=["600000.SH"], minute_bars_by_code=bars,
            backup_reader=_FakeBackupReader({t: [] for t in MINUTE_BACKUP_TABLES}),
            backup_root=tmp_path / "backup_pin",
            cleanup_gateway=cleanup_gw, import_gateway=import_gw,
            journal=journal, lock_ctx=lock,
            now_fn=_clock,
            mood_invalidator=_mood_inv,
            dry_run=False,
        )
        assert res.stage_result.outcome is StageState.CERTIFIED
        assert res.stage_result.rc == 0
        assert res.publish_certificate is not None
        assert validate_stage_result(res.stage_result, target_date=TD, plan_id="plan-20261118", generation=1)
        assert validate_certificate(
            res.publish_certificate,
            target_date=TD, plan_id="plan-20261118", generation=1, approval_id="apr-001",
            expected_digest=digest_object(res.publish_certificate),
            trusted_issuers=("postclose_orchestrator",), now=_at(16, 16, 0),
        )
        assert res.minute_5m_inserted == 48
        assert res.derived_inserted == {"15m": 16, "30m": 8, "60m": 4}
        assert lock.acquired_count == 1 and lock.released_count == 1

        # 检验 10:00 所在桶全部保持 amount=None（15m=10:00, 30m=10:00, 60m=10:30），且无午休跨桶
        b15 = aggregate_derived_bars_from_5m(TD, bars, "15m")
        b30 = aggregate_derived_bars_from_5m(TD, bars, "30m")
        b60 = aggregate_derived_bars_from_5m(TD, bars, "60m")
        assert [r["datetime"][11:16] for r in b60] == ["10:30", "11:30", "14:00", "15:00"]
        assert next(r["amount"] for r in b15 if "10:00" in r["datetime"]) is None
        assert next(r["amount"] for r in b30 if "10:00" in r["datetime"]) is None
        assert next(r["amount"] for r in b60 if "10:30" in r["datetime"]) is None

    def test_calendar_closed_noop_and_unknown_writes_keeps_lock(self, tmp_path):
        manifest, close_cert, bars = self._build_full_inputs(tmp_path)
        lock = _FakeLockContext()
        journal = MemoryJournal()
        closed_res = run_postclose_orchestration(
            manifest=manifest, close_cert=close_cert,
            trusted_issuers=("postclose_orchestrator", "close_certifier"),
            calendar_state="closed",
            operation_permits={"delete": "perm:del:1", "insert": "perm:ins:1", "invalidate": "perm:inv:1"},
            schema_digest=SCHEMA_DIGEST,
            download_report=None, file_snapshots=(), daily_rows=(),
            expected_minute_keys=(), minute_bars_by_code={},
            backup_reader=_FakeBackupReader({}), backup_root=tmp_path,
            cleanup_gateway=_FakeCleanupGateway(), import_gateway=_FakeImportGateway(),
            journal=journal, lock_ctx=lock,
            now_fn=lambda: _at(16, 15, 0), dry_run=False,
        )
        assert closed_res.stage_result.outcome is StageState.NO_OP_CLOSED
        assert lock.acquired_count == 0

        # 导入阶段 INSERT 超时 → UNKNOWN_WRITES 且严禁释放锁
        phase2 = {"calls": 0, "mood_done": False, "after_mood": 0}

        def _clock2():
            phase2["calls"] += 1
            if phase2["calls"] == 1:
                return _at(16, 5, 0)
            if phase2["calls"] <= 3:
                return _at(16, 6, 0)
            if not phase2["mood_done"]:
                return _at(16, 10, 30)
            phase2["after_mood"] += 1
            if phase2["after_mood"] <= 3:
                return _at(16, 11, 0)
            return _at(16, 15, 5)

        res_unk = run_postclose_orchestration(
            manifest=manifest, close_cert=close_cert,
            trusted_issuers=("postclose_orchestrator", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "perm:del:1", "insert": "perm:ins:1", "invalidate": "perm:inv:1"},
            schema_digest=SCHEMA_DIGEST,
            download_report={"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            file_snapshots=_snaps(), daily_rows=_daily_rows(),
            expected_minute_keys=["600000.SH"], minute_bars_by_code=bars,
            backup_reader=_FakeBackupReader({t: [] for t in MINUTE_BACKUP_TABLES}),
            backup_root=tmp_path / "backup_pin",
            cleanup_gateway=_FakeCleanupGateway(),
            import_gateway=_FakeImportGateway(fail_table="stock.kline_minute_5"),
            journal=MemoryJournal(), lock_ctx=lock,
            now_fn=_clock2,
            mood_invalidator=lambda d, ts: phase2.update({"mood_done": True}) or {"day": d.strftime("%Y%m%d")},
            dry_run=False,
        )
        assert res_unk.stage_result.outcome is StageState.UNKNOWN_WRITES
        assert lock.held is True
        assert lock.released_count == 0

    def test_build_tdx_minute_periods_and_cli_wrappers_block_formal_bypass(self):
        from scripts import build_tdx_minute_periods as btm
        from scripts import postclose_cleanup as cli_clean
        from scripts import postclose_orchestrator as cli_orch

        fake_client = SimpleNamespace(command=lambda *_a, **_k: None, query=lambda *_a, **_k: None, insert=lambda *_a, **_k: None)
        for tbl in ("kline_minute_5", "stock.kline_minute_15", "kline_minute_30", "kline_minute_60"):
            with pytest.raises(RuntimeError, match="R14/S8 退役门禁"):
                btm._create_table(fake_client, tbl)
            with pytest.raises(RuntimeError, match="R14/S8 退役门禁"):
                btm._delete_range(fake_client, tbl, _at(9, 35), _at(15, 0), {"600000.SH"}, dry_run=False)
            with pytest.raises(RuntimeError, match="R14/S8 退役门禁"):
                btm._flush(fake_client, tbl, [("row",)], dry_run=False)

        # dry_run=True 与非正式表（如 _qfq）保持允许
        assert btm._flush(fake_client, "kline_minute_15", [], dry_run=True) == 0
        assert btm._flush(fake_client, "kline_minute_15_qfq", [], dry_run=False) == 0

        assert cli_clean.main(["--plan-id", "p1", "--trade-date", "2026-11-18"]) == 0
        assert cli_clean.main(["--plan-id", "p1", "--trade-date", "2026-11-18", "--apply"]) == 2
        assert cli_orch.main(["--plan-id", "p1", "--trade-date", "2026-11-18"]) == 0
        assert cli_orch.main(["--plan-id", "p1", "--trade-date", "2026-11-18", "--apply"]) == 2

    def test_orchestrator_blocks_early_import_and_daily_overlap_conflict(self, tmp_path):
        manifest, close_cert, bars = self._build_full_inputs(tmp_path)
        lock = _FakeLockContext()
        # 1) CleanCertificate 签发后若时钟 < 16:15:00（未达 import_not_before），严禁提前导入
        phase_early = {"calls": 0}

        def _early_clock():
            phase_early["calls"] += 1
            if phase_early["calls"] == 1:
                return _at(16, 5, 0)
            if phase_early["calls"] <= 3:
                return _at(16, 6, 0)
            return _at(16, 11, 0)

        res_early = run_postclose_orchestration(
            manifest=manifest, close_cert=close_cert,
            trusted_issuers=("postclose_orchestrator", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "perm:del:1", "insert": "perm:ins:1", "invalidate": "perm:inv:1"},
            schema_digest=SCHEMA_DIGEST,
            download_report={"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            file_snapshots=_snaps(), daily_rows=_daily_rows(),
            expected_minute_keys=["600000.SH"], minute_bars_by_code=bars,
            backup_reader=_FakeBackupReader({t: [] for t in MINUTE_BACKUP_TABLES}),
            backup_root=tmp_path / "backup_pin",
            cleanup_gateway=_FakeCleanupGateway(),
            import_gateway=_FakeImportGateway(),
            journal=MemoryJournal(), lock_ctx=lock,
            now_fn=_early_clock,
            mood_invalidator=lambda d, ts: {"day": d.strftime("%Y%m%d")},
            dry_run=False,
        )
        assert res_early.stage_result.outcome is StageState.BLOCKED
        assert res_early.stage_result.reason_code == "import_attempted_before_1615"

        # 2) 日线重叠键数值冲突 → IMPORT_FAILED（rc=1），不得继续 5m/15m 导入
        phase_ov = {"calls": 0, "mood_done": False, "after_mood": 0}

        def _ov_clock():
            phase_ov["calls"] += 1
            if phase_ov["calls"] == 1:
                return _at(16, 5, 0)
            if phase_ov["calls"] <= 3:
                return _at(16, 6, 0)
            if not phase_ov["mood_done"]:
                return _at(16, 10, 30)
            phase_ov["after_mood"] += 1
            return _at(16, 11, 0) if phase_ov["after_mood"] <= 3 else _at(16, 15, 5)

        res_ov = run_postclose_orchestration(
            manifest=manifest, close_cert=close_cert,
            trusted_issuers=("postclose_orchestrator", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "perm:del:1", "insert": "perm:ins:1", "invalidate": "perm:inv:1"},
            schema_digest=SCHEMA_DIGEST,
            download_report={"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            file_snapshots=_snaps(), daily_rows=_daily_rows(),
            expected_minute_keys=["600000.SH"], minute_bars_by_code=bars,
            backup_reader=_FakeBackupReader({t: [] for t in MINUTE_BACKUP_TABLES}),
            backup_root=tmp_path / "backup_pin",
            cleanup_gateway=_FakeCleanupGateway(),
            import_gateway=_FakeImportGateway(existing_daily=[{"code": "600000.SH", "close": 50.0}]),
            journal=MemoryJournal(), lock_ctx=lock,
            now_fn=_ov_clock,
            mood_invalidator=lambda d, ts: phase_ov.update({"mood_done": True}) or {"day": d.strftime("%Y%m%d")},
            dry_run=False,
        )
        assert res_ov.stage_result.outcome is StageState.IMPORT_FAILED
        assert "daily_overlap_conflict" in res_ov.stage_result.reason_code
