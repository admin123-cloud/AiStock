import copy
import csv
import hashlib
import json

import pytest

from scripts.maintain_market_status import prepare_reviewed, plan_insert


def archived_fixture(tmp_path):
    record = dict(code='600000.SH', trade_date='2010-02-26', external_status='confirmed_suspended_no_trade',
                  tradestatus='0', volume_shares='0.0', amount_yuan='0.0', any_minute_positive='False',
                  period_checks='zero_minute_aggregate_on_confirmed_suspension')
    path = tmp_path / 'reviewed.csv'
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, list(record))
        writer.writeheader()
        writer.writerow(record)
    proof = dict(registry_scope='exact_date_only', current_daily_5m_conflict_check='passed',
                 evidence_kind='archived_explicit_trade_status', reviewed_record=record, csv_line=2,
                 bindings=[dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())])
    row = dict(code='600000.SH', start_date='2010-02-26', end_date='2010-02-26', status='suspended',
               review_state='verified', evidence_url='archive://sha256/fixture?line=2', reason='reviewed explicit status',
               source='archived_verified_trade_status', expected_resume_date=None, evidence_json=json.dumps(proof))
    return row, proof, path


def test_archive_import_requires_explicit_status_and_exact_bound_source_row(tmp_path):
    row, proof, _ = archived_fixture(tmp_path)
    assert prepare_reviewed([row]) == [row]
    for change in [dict(tradestatus=''), dict(external_status='no_exact_daily_row'), dict(any_minute_positive='True')]:
        bad = copy.deepcopy(proof)
        bad['reviewed_record'].update(change)
        with pytest.raises(ValueError):
            prepare_reviewed([dict(row, evidence_json=json.dumps(bad))])
    bad = copy.deepcopy(proof)
    bad['reviewed_record']['extra'] = 'not in original source'
    with pytest.raises(ValueError, match='source row differs'):
        prepare_reviewed([dict(row, evidence_json=json.dumps(bad))])


def test_source_tamper_and_current_conflict_cannot_be_promoted(tmp_path):
    row, proof, path = archived_fixture(tmp_path)
    bad = dict(proof, current_daily_5m_conflict_check='conflict')
    with pytest.raises(ValueError, match='conflict check'):
        prepare_reviewed([dict(row, evidence_json=json.dumps(bad))])
    path.write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='SHA mismatch'):
        prepare_reviewed([row])


def test_conflict_is_durable_and_idempotent_without_becoming_verified(tmp_path):
    row, proof, _ = archived_fixture(tmp_path)
    proof['current_daily_5m_conflict_check'] = 'conflict'
    held = dict(row, status='suspended_candidate', review_state='conflict',
                source='historical_review_conflict', evidence_json=json.dumps(proof))
    assert prepare_reviewed([held]) == [held]
    assert plan_insert([held], [dict(held, observed_at='2026-10-16 00:00:00')])[0] == []
    with pytest.raises(ValueError, match='never overwrite'):
        plan_insert([held], [dict(held, reason='different review')])
