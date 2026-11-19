# -*- coding: utf-8 -*-
"""P1（S9 / S10 / S11 / S12）发布代际只读门、认证情绪终算、脱敏告警与端到端集成测试。

全部离线合成，零真实 DB / SMTP / 实盘路径副作用。
"""

from __future__ import annotations

import ast
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.postclose_alerts import ALLOWED_ALERT_FIELDS, AlertOutbox, sanitize_alert_event  # noqa: E402
from services.postclose_contracts import PublishCertificate, StageState, digest_object  # noqa: E402
from services.postclose_journal import MemoryJournal  # noqa: E402
from services.postclose_orchestrator import run_postclose_orchestration  # noqa: E402
from services.postclose_publication import (  # noqa: E402
    PublicationStore,
    publication_cache_key,
    read_with_generation_guard,
)
from services.postclose_source_prepare import canonical_sha256  # noqa: E402
from scripts.postclose_deadline_watch import inspect_postclose_deadlines  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")
TD = date(2026, 11, 18)


def _at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 18, h, m, s, tzinfo=BJ)


def _sample_publish_cert(*, td: date = TD, generation: int = 1) -> PublishCertificate:
    pub_at = datetime(td.year, td.month, td.day, 16, 16, 0, tzinfo=BJ)
    return PublishCertificate(
        target_date=td,
        plan_id=f"plan-{td.strftime('%Y%m%d')}",
        generation=generation,
        approval_id="apr-001",
        issued_at=pub_at,
        expires_at=pub_at + timedelta(hours=6),
        issuer="postclose_orchestrator",
        evidence_refs=("daily:5500", "minute_5m:264000"),
        schema_digest=canonical_sha256({"v": 1}),
        manifest_digest=canonical_sha256({"m": generation}),
        clean_digest=canonical_sha256({"c": generation}),
        source_digest=canonical_sha256({"s": generation}),
        verification_digest=canonical_sha256({"ver": generation}),
        coverage_status="complete",
        published_at=pub_at,
    )


class TestS9PublicationGate:
    def test_missing_corrupt_rebuilding_and_explicit_date_mismatch_fail_closed(self, tmp_path):
        # 1) 控制目录缺失 → not_ready
        missing_store = PublicationStore(tmp_path / "missing_dir")
        v0 = missing_store.read_view(requested_date=TD, now=_at(16, 20))
        assert v0.ready is False
        assert v0.status == "not_ready"

        # 2) 前日 CERTIFIED，当日 CLEANING → 保留 last_certified_date 但当日 not_ready 且绝不把昨日偷换成当日
        store = PublicationStore(tmp_path / "ctrl")
        prev_day = date(2026, 11, 17)
        store.publish_certified(_sample_publish_cert(td=prev_day, generation=1), now=datetime(2026, 11, 17, 16, 20, tzinfo=BJ))
        store.write_stage(
            target_date=TD, plan_id="plan-20261118", generation=2, approval_id="apr-001",
            status=StageState.CLEANING, reason="cleanup_in_progress", now=_at(16, 10),
        )
        v_clean = store.read_view(requested_date=TD, now=_at(16, 10))
        assert v_clean.ready is False
        assert v_clean.status == "not_ready"
        assert v_clean.data_date is None
        assert v_clean.last_certified_date == "2026-11-17"

        # 3) 显式请求未认证日 2026-11-19，绝不偷换成 11-18
        cert_today = _sample_publish_cert(td=TD, generation=2)
        store.publish_certified(cert_today, now=_at(16, 16))
        v_today = store.read_view(requested_date=TD, now=_at(16, 17))
        assert v_today.ready is True
        assert v_today.data_date == "2026-11-18"
        assert v_today.certificate_digest == digest_object(cert_today)
        assert publication_cache_key(v_today, "indices")[0:4] == ("2026-11-18", 2, "CERTIFIED", "indices")

        v_other = store.read_view(requested_date=date(2026, 11, 19), now=_at(16, 17))
        assert v_other.ready is False
        assert v_other.data_date is None
        assert v_other.last_certified_date == "2026-11-18"

        # 4) 篡改证书摘要或坏 JSON → fail-closed
        raw = json.loads(store.state_path.read_text(encoding="utf-8"))
        raw["certificate_digest"] = "0" * 64
        store.state_path.write_text(json.dumps(raw), encoding="utf-8")
        assert store.read_view(requested_date=TD, now=_at(16, 17)).ready is False

        store.state_path.write_text("{corrupt", encoding="utf-8")
        assert store.read_view(requested_date=TD, now=_at(16, 17)).ready is False
        assert store.is_rebuilding(TD) is True

    def test_generation_read_guard_discards_mid_query_generation_change_and_blocks_warm_cache(self, tmp_path, monkeypatch):
        store = PublicationStore(tmp_path / "ctrl")
        store.publish_certified(_sample_publish_cert(td=TD, generation=1), now=_at(16, 16))

        def _mutating_reader(view):
            # 模拟查询途中切入下一代 CLEANING
            store.write_stage(
                target_date=TD, plan_id="plan-20261118", generation=2, approval_id="apr-002",
                status=StageState.CLEANING, reason="rebuild_started", now=_at(16, 20),
            )
            return {"close": 10.5}

        after_view, data = read_with_generation_guard(
            store, _mutating_reader, requested_date=TD, now_fn=lambda: _at(16, 20)
        )
        assert data is None
        assert after_view.ready is False
        assert after_view.reason == "mixed_generation_discarded"

        # S9 门控必须先于暖缓存命中（market_turnover_forecast & intraday_market_snapshot）
        from services import intraday_market_snapshot as ims
        from services import market_turnover_forecast as mtf

        monkeypatch.setattr(mtf, "_POSTCLOSE_CONTROL_ROOT", tmp_path / "ctrl")
        monkeypatch.setattr(mtf, "_now_bj", lambda: _at(16, 20))
        monkeypatch.setattr(mtf, "_CACHE", {"payload": {"available": True, "stale": True}, "expires_at": time.monotonic() + 300})
        res = mtf.get_intraday_turnover_forecast()
        assert res["available"] is False
        assert "not_ready" in res["reason"]

        monkeypatch.setattr(ims, "_POSTCLOSE_CONTROL_ROOT", tmp_path / "ctrl")
        monkeypatch.setattr(ims, "_table_ready", lambda _t: (_ for _ in ()).throw(AssertionError("重建期不得查快照表")))
        assert ims.latest_snapshot_ts(now=_at(16, 20)) is None


class TestS10CertifiedMarketMood:
    def test_late_intraday_blocked_after_1505_and_final_requires_certified_event_idempotently(self, tmp_path, monkeypatch):
        from scripts import market_mood_update as mmu
        from services import market_mood as mm

        # 1) 15:05:00 后在途 realtime 计算不得落盘
        res_late = mmu.run_intraday(out_root=tmp_path, now=_at(15, 5, 0))
        assert res_late["status"] == "blocked"
        assert res_late["reason"] == "after_1505_cutoff"

        # 2) 未认证前，即使日线表有一行数据，run_certified_finalize 也严格拒绝落 final
        ctrl = tmp_path / "ctrl"
        store = PublicationStore(ctrl)
        store.write_stage(
            target_date=TD, plan_id="plan-20261118", generation=1, approval_id="apr-001",
            status=StageState.IMPORTING, reason="importing", now=_at(16, 15),
        )
        monkeypatch.setattr(mmu, "compute_final_mood", lambda d: {"mode": "final", "date": d, "limit_up_count": 33})
        blocked = mmu.run_certified_finalize(TD.isoformat(), control_root=ctrl, out_root=tmp_path, now=_at(16, 16))
        assert blocked["status"] == "blocked"
        assert mm.load_day_payload("20261118", out_root=tmp_path) is None

        # 3) 发布 CERTIFIED 证书后触发终算，并绑定 certificate_digest；重复触发幂等不重写
        cert = _sample_publish_cert(td=TD, generation=1)
        store.publish_certified(cert, now=_at(16, 16))
        first = mmu.run_certified_finalize(TD.isoformat(), control_root=ctrl, out_root=tmp_path, now=_at(16, 17))
        assert first["status"] == "success"
        assert first["write_status"] == "written"
        assert first["certificate_digest"] == digest_object(cert)

        second = mmu.run_certified_finalize(TD.isoformat(), control_root=ctrl, out_root=tmp_path, now=_at(16, 18))
        assert second["status"] == "success"
        assert second["write_status"] == "idempotent_noop"


class TestS11AlertsAndIndependentDeadlineWatcher:
    def test_sanitization_dedup_escalation_uncertain_timeout_and_restart_durability(self, tmp_path):
        cleaned = sanitize_alert_event(
            {
                "target_date": "2026-11-18",
                "plan_id": "plan-1",
                "generation": 1,
                "phase": "cleanup",
                "reason_code": "password=super_secret token=abc123 account_balance=99999",
                "severity": "warning",
                "episode": "ep1",
                "raw_traceback": "Traceback SECRET_KEY=xyz",
                "broker_account": "88881234",
            },
            now=_at(16, 10),
        )
        assert set(cleaned) == ALLOWED_ALERT_FIELDS
        assert "super_secret" not in json.dumps(cleaned)
        assert "abc123" not in json.dumps(cleaned)
        assert "99999" not in json.dumps(cleaned)
        assert "raw_traceback" not in cleaned
        assert "broker_account" not in cleaned

        outbox = AlertOutbox(tmp_path / "alerts")
        e1 = outbox.enqueue({"target_date": "2026-11-18", "plan_id": "p1", "phase": "clean", "reason_code": "slow", "severity": "warning", "episode": "e1"}, now=_at(16, 10))
        e2 = outbox.enqueue({"target_date": "2026-11-18", "plan_id": "p1", "phase": "clean", "reason_code": "slow", "severity": "warning", "episode": "e1"}, now=_at(16, 11))
        assert e1.deduplicated is False and e2.deduplicated is True

        # 严重度升级 warning -> critical 必须重新激活告警
        e3 = outbox.enqueue({"target_date": "2026-11-18", "plan_id": "p1", "phase": "clean", "reason_code": "slow", "severity": "critical", "episode": "e1"}, now=_at(16, 12))
        assert e3.deduplicated is False and e3.escalated is True and e3.delivery_status == "pending"

        # 模拟重启：重新实例化 AlertOutbox，pending 事件持久保留；SMTP 超时标 uncertain 不称送达
        restarted = AlertOutbox(tmp_path / "alerts")

        class _TimeoutMailer:
            def send(self, payload):
                raise TimeoutError("SMTP timeout")

        outcomes = restarted.dispatch_pending(_TimeoutMailer())
        assert outcomes[e3.event_id] == "uncertain"
        assert restarted.list_records()[e3.event_id]["delivery_status"] == "uncertain"

    def test_independent_watcher_has_no_writer_imports_and_detects_missed_deadlines(self, tmp_path):
        # 结构守卫：独立观察者绝不 import 采集/清理/编排写手模块
        watcher_src = (ROOT / "scripts" / "postclose_deadline_watch.py").read_text(encoding="utf-8")
        tree = ast.parse(watcher_src)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        for forbidden in ("postclose_cleanup", "postclose_orchestrator", "daily_kline_pipeline", "minute_collect_auto"):
            assert not any(forbidden in mod for mod in imported), f"观察者不得导入写手模块: {forbidden}"

        outbox = AlertOutbox(tmp_path / "alerts")
        # 1) 控制目录缺失 → 立即报 control_root_missing
        ev_missing = inspect_postclose_deadlines(control_root=tmp_path / "nope", outbox=outbox, now=_at(16, 20))
        assert any(e["reason_code"] == "control_root_missing" for e in ev_missing)

        # 2) 心跳过期 + 16:45 仍停留在 CLEANING → 同时检出心跳过期 + 16:15/16:40 违约
        ctrl = tmp_path / "ctrl"
        store = PublicationStore(ctrl)
        store.write_heartbeat(now=_at(16, 0))
        store.write_stage(
            target_date=TD, plan_id="p1", generation=1, approval_id="apr-1",
            status=StageState.CLEANING, reason="stuck", now=_at(16, 10),
        )
        ev_stuck = inspect_postclose_deadlines(
            control_root=ctrl, outbox=AlertOutbox(tmp_path / "alerts2"),
            now=_at(16, 45), max_heartbeat_age_sec=300.0,
        )
        reasons = {e["reason_code"] for e in ev_stuck}
        assert "runner_heartbeat_stale" in reasons
        assert "clean_deadline_1615_missed" in reasons
        assert "publish_deadline_1640_missed" in reasons


class TestS12EndToEndIntegration:
    def test_full_s5_to_s11_certified_e2e_and_r59_retired(self, tmp_path, monkeypatch):
        from scripts import market_mood_update as mmu
        from scripts import pipeline_scheduler as ps
        from tests.test_p0_s5_s8_postclose_pipeline import (
            MINUTE_BACKUP_TABLES,
            SCHEMA_DIGEST,
            TestS8OrchestratorAndLegacyChoke,
            _FakeBackupReader,
            _FakeCleanupGateway,
            _FakeImportGateway,
            _FakeLockContext,
            _daily_rows,
            _snaps,
        )

        # R59 永久退役门禁复验
        with pytest.raises(RuntimeError, match="R59"):
            ps._register_tdx_postclose_minutes(None)

        helper = TestS8OrchestratorAndLegacyChoke()
        manifest, close_cert, bars = helper._build_full_inputs(tmp_path)
        ctrl_root = tmp_path / "postclose_control"
        pub_store = PublicationStore(ctrl_root)
        pub_store.write_heartbeat(now=_at(16, 15, 0))
        outbox = AlertOutbox(tmp_path / "alerts")
        lock = _FakeLockContext()
        phase = {"calls": 0, "mood_done": False, "after_mood": 0}

        def _clock():
            phase["calls"] += 1
            if phase["calls"] == 1:
                return _at(16, 5, 0)
            if phase["calls"] <= 3:
                return _at(16, 6, 0)
            if not phase["mood_done"]:
                return _at(16, 10, 30)
            phase["after_mood"] += 1
            return _at(16, 11, 0) if phase["after_mood"] <= 3 else _at(16, 15, 5)

        res = run_postclose_orchestration(
            manifest=manifest,
            close_cert=close_cert,
            trusted_issuers=("postclose_orchestrator", "close_certifier"),
            calendar_state="trading",
            operation_permits={"delete": "perm:del:1", "insert": "perm:ins:1", "invalidate": "perm:inv:1"},
            schema_digest=SCHEMA_DIGEST,
            download_report={"state": "completed", "progress": 100, "completed_at": _at(16, 2, 0)},
            file_snapshots=_snaps(),
            daily_rows=_daily_rows(),
            expected_minute_keys=["600000.SH"],
            minute_bars_by_code=bars,
            backup_reader=_FakeBackupReader({t: [] for t in MINUTE_BACKUP_TABLES}),
            backup_root=tmp_path / "backup_pin",
            cleanup_gateway=_FakeCleanupGateway(),
            import_gateway=_FakeImportGateway(),
            journal=MemoryJournal(),
            lock_ctx=lock,
            now_fn=_clock,
            mood_invalidator=lambda d, ts: phase.update({"mood_done": True}) or {"day": d.strftime("%Y%m%d")},
            publication_store=pub_store,
            alert_outbox=outbox,
            dry_run=False,
        )
        assert res.stage_result.outcome is StageState.CERTIFIED

        # S9 读侧立即看到 CERTIFIED + ready=True
        view = pub_store.read_view(requested_date=TD, now=_at(16, 17, 0))
        assert view.ready is True
        assert view.data_date == "2026-11-18"
        assert view.certificate_digest == digest_object(res.publish_certificate)

        # S10 认证事件驱动情绪终算成功落盘且绑定证书摘要
        monkeypatch.setattr(mmu, "compute_final_mood", lambda d: {"mode": "final", "date": d, "limit_up_count": 42})
        mood_res = mmu.run_certified_finalize(TD.isoformat(), control_root=ctrl_root, out_root=tmp_path / "mood", now=_at(16, 17, 5))
        assert mood_res["status"] == "success"
        assert mood_res["certificate_digest"] == view.certificate_digest

        # S11 独立观察者在 16:45 巡检 CERTIFIED + 新鲜心跳 → 零告警
        pub_store.write_heartbeat(now=_at(16, 44, 0))
        emitted = inspect_postclose_deadlines(control_root=ctrl_root, outbox=outbox, now=_at(16, 45, 0))
        assert emitted == []
        assert outbox.list_records() == {}
