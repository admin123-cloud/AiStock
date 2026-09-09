from datetime import datetime,timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest
from scripts import repair_5m_manifest as repair
from scripts.qmt_xtquant_minute_backfill_validate import REGULAR_ASHARE_5M_TIMES


def day_rows():
    return [['000001.SZ',datetime.combine(datetime(2026,8,25),t),10.,11.,9.,10.5,100.,1000.]
            for t in sorted(REGULAR_ASHARE_5M_TIMES)]


def test_only_missing_keys_are_staged_and_hundredfold_units_require_overlap():
    source=day_rows();existing=[list(r) for r in source[2:]]
    for row in existing:row[6]*=100
    missing,proof=repair.prepare_day(source,existing)
    assert len(missing)==2 and proof['volume_factor']==100
    assert {repair.timestamp(r[1]) for r in missing}=={r[1] for r in source[:2]}
    assert all(r[6]==10000 for r in missing)
    assert source[0][6]==100


@pytest.mark.parametrize('mutation,reason',[
    (lambda rows:rows.pop(),'cache_day_incomplete'),
    (lambda rows:rows.append(rows[0]),'duplicate_time_keys'),
    (lambda rows:rows[0].__setitem__(1,rows[0][1]+timedelta(minutes=1)),'cache_day_incomplete'),
    (lambda rows:rows[0].__setitem__(3,float('nan')),'invalid_source_values'),
])
def test_invalid_cache_cannot_supply_missing_keys(mutation,reason):
    source=day_rows();existing=[list(r) for r in source[2:]];mutation(source)
    with pytest.raises(ValueError,match=reason):repair.prepare_day(source,existing)


@pytest.mark.parametrize('column,value,reason',[(5,20.,'price_conflict'),(7,5.,'amount_conflict'),(6,9000.,'volume_units')])
def test_conflicting_overlap_is_never_overwritten(column,value,reason):
    source=day_rows();existing=[list(r) for r in source[2:]];existing[0][column]=value
    with pytest.raises(ValueError,match=reason):repair.prepare_day(source,existing)


def test_empty_day_needs_separate_evidence():
    with pytest.raises(ValueError,match='insufficient_overlap'):repair.prepare_day(day_rows(),[])


def setup_apply(tmp_path,row,state='staged'):
    archive=tmp_path/'backup.zip';archive.write_bytes(b'unit-test-archive')
    proof=tmp_path/'protection.json';proof.write_text(json.dumps({'state':'archive_verified','tables':['stock.kline_minute_5'],
                  'archive':{'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}}))
    stage=tmp_path/'stage.json';stage.write_text(json.dumps({'state':state,'table':'stock_repair.five_minute_gap_'+'a'*16,
                  'rows':1,'sha256':repair.digest([row])}))
    return SimpleNamespace(backup_confirmation=proof,stage_report=stage)


def test_apply_conflict_stops_before_any_insert(tmp_path,monkeypatch):
    row=day_rows()[0];args=setup_apply(tmp_path,row);different=list(row);different[5]=12
    calls=[];client=SimpleNamespace(query=lambda *a,**k:SimpleNamespace(result_rows=[row]),command=lambda *a,**k:calls.append(a))
    monkeypatch.setattr(repair,'read_day',lambda *a:[different])
    with pytest.raises(RuntimeError,match='existing_key_conflict'):repair.apply(args,client)
    assert not calls


def test_partial_uncertain_outcome_cannot_be_replayed(tmp_path,monkeypatch):
    row=day_rows()[0];args=setup_apply(tmp_path,row,'outcome_uncertain');calls=[]
    client=SimpleNamespace(query=lambda *a,**k:SimpleNamespace(result_rows=[row]),command=lambda *a,**k:calls.append(a))
    monkeypatch.setattr(repair,'read_day',lambda *a:[])
    with pytest.raises(RuntimeError,match='prior_write_uncertain'):repair.apply(args,client)
    assert not calls
