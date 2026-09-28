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


def test_purchased_review_key_is_visible_and_append_only(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setattr(ledger, "runtime_path", lambda *parts: root.joinpath(*parts))
    monkeypatch.setenv("AISTOCK_REVIEW_OPERATOR", "test-operator")
    item = {
        "review_key": "purchased_history|project_daily_missing_for_5m|5m_to_1d|600000.SH|2000-06-30",
        "issue_type": "project_daily_missing_for_5m",
        "code": "600000.SH",
        "date": "2000-06-30",
        "validity": "blocked",
        "category": "purchased_history:project_daily_missing_for_5m",
    }
    monkeypatch.setattr(ledger, "_purchased_history_snapshot", lambda: ([item], "purchased-source-fp"))

    _path, snapshot, source_fp = ledger.source_snapshot()
    assert snapshot["items"][0]["review_key"] == item["review_key"]
    assert source_fp
    assert "request_5m_repair" in ledger.allowed_actions_for_item(item)

    request = {
        "review_key": item["review_key"],
        "action": "keep_pending",
        "fingerprint": source_fp,
        "expected_item_version": ledger.source_item_version(item),
        "contract_version": "v1",
        "evidence_hash": _evidence_hash(),
        "idempotency_key": "purchased-review-1",
        "confirm_token": source_fp,
        "reason": "",
        "evidence_refs": [],
        "payload": {"source": "purchased_history"},
    }
    result = ledger.record_action(request)
    assert result["idempotent_replay"] is False
    assert ledger.queue()[0]["review_key"] == item["review_key"]


def test_incomplete_purchased_source_cannot_enqueue_repair():
    item = {"issue_type": "source_5m_incomplete_bars", "category": "purchased_history:source_5m_incomplete_bars"}
    assert ledger.allowed_actions_for_item(item) == {"keep_pending", "mark_reviewed"}


def test_purchased_repair_uses_canonical_code_and_date(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setattr(ledger, "runtime_path", lambda *parts: root.joinpath(*parts))
    monkeypatch.setenv("AISTOCK_REVIEW_OPERATOR", "test-operator")
    item = {
        "review_key": "purchased_history|project_daily_missing_for_5m|5m_to_1d|600000.SH|2000-06-30",
        "issue_type": "project_daily_missing_for_5m",
        "code": "600000.SH",
        "date": "2000-06-30",
        "validity": "blocked",
        "category": "purchased_history:project_daily_missing_for_5m",
    }
    monkeypatch.setattr(ledger, "_purchased_history_snapshot", lambda: ([item], "purchased-source-fp"))
    from services.operations import ingestion_backlog

    captured = {}
    monkeypatch.setattr(
        ingestion_backlog,
        "enqueue",
        lambda args, kind: captured.update(args=args, kind=kind) or "job-1",
    )
    _path, _snapshot, source_fp = ledger.source_snapshot()
    result = ledger.record_action({
        "review_key": item["review_key"],
        "action": "request_5m_repair",
        "fingerprint": source_fp,
        "expected_item_version": ledger.source_item_version(item),
        "contract_version": "v1",
        "evidence_hash": _evidence_hash("人工确认后申请复核"),
        "idempotency_key": "purchased-repair-1",
        "confirm_token": source_fp,
        "reason": "人工确认后申请复核",
        "evidence_refs": [],
        "payload": {},
    })
    assert result["pull_job_id"] == "job-1"
    assert captured["kind"] == "ingestion"
    assert captured["args"][captured["args"].index("--codes") + 1] == "600000.SH"
    assert captured["args"][captured["args"].index("--start-date") + 1] == "2000-06-30"
