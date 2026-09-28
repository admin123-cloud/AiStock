import hashlib
import json

from services.operations import data_review_ledger as ledger


def _evidence_hash(reason="", refs=None):
    return hashlib.sha256(json.dumps(
        {"reason": reason, "evidence_refs": refs or []},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def test_action_is_append_only_idempotent_and_projects_latest(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setattr(ledger, "runtime_path", lambda *parts: root.joinpath(*parts))
    monkeypatch.setenv("AISTOCK_REVIEW_OPERATOR", "test-operator")
    source_dir = root / "minute_migration_v2"
    source_dir.mkdir(parents=True)
    item = {"code": "600000.SH", "date": "2026-01-02", "count": 47, "category": "daily_no_trade_evidence", "validity": "blocked_evidence"}
    source = source_dir / "scoped_anomalies_astra6.json"
    source.write_text(json.dumps({"items": [item]}), encoding="utf-8")
    source_fp = hashlib.sha256(source.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps({"reason": "", "evidence_refs": []}, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    payload = {"review_key": "600000.SH|2026-01-02", "action": "no_trade",
               "fingerprint": source_fp, "expected_item_version": ledger.source_item_version(item), "contract_version": "v1", "evidence_hash": digest,
               "idempotency_key": "idem-1", "confirm_token": source_fp, "payload": {"code": "600000.SH"}}
    first = ledger.record_action(payload)
    replay = ledger.record_action(payload)
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    again = ledger.record_action({**payload, "idempotency_key": "idem-2"})
    assert again["idempotent_replay"] is False
    assert len(ledger.queue()) == 1
    assert ledger.queue()[0]["payload"]["payload"]["code"] == "600000.SH"


def test_rejects_unsupported_action_and_bad_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "runtime_path", lambda *parts: tmp_path.joinpath(*parts))
    base = {"review_key": "k", "fingerprint": "fp", "contract_version": "v1",
            "evidence_hash": "x" * 64, "idempotency_key": "i"}
    try:
        ledger.record_action({**base, "action": "drop_table"})
        assert False
    except ValueError as exc:
        assert "unsupported action" in str(exc)
    try:
        ledger.record_action({**base, "action": "no_trade", "evidence_hash": "bad"})
        assert False
    except ValueError as exc:
        assert "SHA-256" in str(exc)


def test_actions_include_keep_pending():
    assert "keep_pending" in ledger.ACTIONS


def test_mark_reviewed_keeps_item_visible_and_survives_later_action(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    source_dir = root / "minute_migration_v2"
    source_dir.mkdir(parents=True)
    monkeypatch.setattr(ledger, "runtime_path", lambda *parts: root.joinpath(*parts))
    monkeypatch.setenv("AISTOCK_REVIEW_OPERATOR", "test-operator")
    item = {
        "review_key": "purchased_history|source_5m_incomplete_bars|5m|000007.SH|2002-01-14",
        "issue_type": "source_5m_incomplete_bars",
        "code": "000007.SH",
        "date": "2002-01-14",
        "count": 0,
        "category": "purchased_history:source_5m_incomplete_bars",
        "validity": "blocked",
    }
    source = source_dir / "scoped_anomalies_reviewed.json"
    source.write_text(json.dumps({"items": [item]}), encoding="utf-8")
    source_fp = hashlib.sha256(source.read_bytes()).hexdigest()
    evidence_hash = _evidence_hash("保持缺失，不补造数据；本条记录已完成审核")
    request = {
        "review_key": item["review_key"],
        "action": "mark_reviewed",
        "fingerprint": source_fp,
        "expected_item_version": ledger.source_item_version(item),
        "contract_version": "v1",
        "evidence_hash": evidence_hash,
        "idempotency_key": "reviewed-1",
        "confirm_token": source_fp,
        "reason": "保持缺失，不补造数据；本条记录已完成审核",
        "evidence_refs": [],
        "payload": {"source": "manual_review"},
    }
    first = ledger.record_action(request)
    assert first["idempotent_replay"] is False
    assert ledger.queue()[0]["reviewed"] is True
    assert ledger.queue()[0]["review_status"] == "reviewed"

    later = {**request, "action": "keep_pending", "idempotency_key": "reviewed-2", "reason": "仍保留在审核队列"}
    later["evidence_hash"] = _evidence_hash(later["reason"])
    ledger.record_action(later)
    assert ledger.queue()[0]["action"] == "keep_pending"
    assert ledger.queue()[0]["reviewed"] is True


def test_no_source_snapshot_is_forbidden(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "runtime_path", lambda *parts: tmp_path.joinpath(*parts))
    monkeypatch.setenv("AISTOCK_REVIEW_OPERATOR", "test-operator")
    digest = hashlib.sha256(json.dumps({"reason": "", "evidence_refs": []}, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    try:
        ledger.record_action({"review_key": "k", "action": "keep_pending", "fingerprint": "fp", "evidence_hash": digest, "idempotency_key": "none-1", "confirm_token": "x"})
        assert False
    except ledger.ForbiddenReview as exc:
        assert "snapshot" in str(exc)


def test_first_source_item_submission_and_stale_source_conflict(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    source_dir = root / "minute_migration_v2"
    source_dir.mkdir(parents=True)
    monkeypatch.setattr(ledger, "runtime_path", lambda *parts: root.joinpath(*parts))
    monkeypatch.setenv("AISTOCK_REVIEW_OPERATOR", "test-operator")
    item = {"code": "600000.SH", "date": "2026-01-02", "count": 47, "category": "daily_no_trade_evidence", "validity": "blocked_evidence"}
    source = source_dir / "scoped_anomalies_astra6.json"
    source.write_text(json.dumps({"items": [item]}), encoding="utf-8")
    source_fp = hashlib.sha256(source.read_bytes()).hexdigest()
    item_version = ledger.source_item_version(item)
    evidence_hash = hashlib.sha256(json.dumps({"reason": "confirmed", "evidence_refs": []}, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    request = {"review_key": "600000.SH|2026-01-02", "action": "no_trade", "fingerprint": source_fp,
               "expected_item_version": item_version, "reason": "confirmed", "evidence_refs": [],
               "evidence_hash": evidence_hash, "idempotency_key": "source-1", "confirm_token": source_fp}
    result = ledger.record_action(request)
    assert result["idempotent_replay"] is False
    source.write_text(json.dumps({"items": [item, {"code": "1", "date": "2026-01-03"}]}), encoding="utf-8")
    new_fp = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        ledger.record_action({**request, "idempotency_key": "source-2", "confirm_token": new_fp})
        assert False
    except ledger.ConflictReview as exc:
        assert "fingerprint" in str(exc)
