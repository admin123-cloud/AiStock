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


def test_legacy_hundredfold_units_are_not_extended_into_new_keys():
    source=day_rows();existing=[list(r) for r in source[2:]]
    for row in existing:row[6]*=100
    with pytest.raises(ValueError,match='legacy_volume_unit_conflict'):
        repair.prepare_day(source,existing)


def test_only_missing_keys_are_staged_in_canonical_units():
    source=day_rows();existing=[list(r) for r in source[2:]]
    missing,proof=repair.prepare_day(source,existing)
    assert len(missing)==2 and proof['volume_factor']==1
    assert {repair.timestamp(r[1]) for r in missing}=={r[1] for r in source[:2]}
    assert all(r[6]==100 for r in missing)
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


def test_legacy_boundary_and_rounding_differences_are_preserved_not_overwritten():
    source=day_rows();existing=[list(r) for r in source[:-2]]
    existing[0][6]=60;existing[0][7]=600  # Legacy first bar omitted auction.
    for row in existing:
        row[2]=10.5
    existing[1][7]=1100  # Legacy amount granularity.
    missing,proof=repair.prepare_day(source,existing)
    assert len(missing)==2 and proof['existing_ohlc_differences_retained']==46
    assert existing[0][6]==60 and missing[0][2]==10


def setup_apply(tmp_path,row,state='staged'):
    archive=tmp_path/'backup.zip';archive.write_bytes(b'unit-test-archive')
    proof=tmp_path/'protection.json';proof.write_text(json.dumps({'state':'archive_verified','tables':['stock.kline_minute_5'],
                  'archive':{'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}}))
    source=day_rows();existing=source[1:];_,calibration=repair.prepare_day(source,existing)
    evidence=tmp_path/'source-evidence.jsonl'
    evidence.write_text(json.dumps({'day':'2026-08-25','code':'000001.SZ',
                        'source':[repair.canonical(r) for r in source],
                        'existing':[repair.canonical(r) for r in existing],**calibration}))
    stage=tmp_path/'stage.json';stage.write_text(json.dumps({'state':state,'table':'stock_repair.five_minute_gap_'+'a'*16,
                  'rows':1,'sha256':repair.digest([row]),'volume_unit':'lots',
                  'evidence_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}))
    return SimpleNamespace(backup_confirmation=proof,stage_report=stage)


def test_apply_conflict_stops_before_any_insert(tmp_path,monkeypatch):
    row=day_rows()[0];args=setup_apply(tmp_path,row);different=list(row);different[5]=12
    calls=[];client=SimpleNamespace(query=lambda *a,**k:SimpleNamespace(result_rows=[row]),command=lambda *a,**k:calls.append(a))
    monkeypatch.setattr(repair,'read_day',lambda *a:day_rows()[1:]+[different])
    with pytest.raises(RuntimeError,match='existing_key_conflict'):repair.apply(args,client)
    assert not calls


def test_partial_uncertain_outcome_cannot_be_replayed(tmp_path,monkeypatch):
    row=day_rows()[0];args=setup_apply(tmp_path,row,'outcome_uncertain');calls=[]
    client=SimpleNamespace(query=lambda *a,**k:SimpleNamespace(result_rows=[row]),command=lambda *a,**k:calls.append(a))
    monkeypatch.setattr(repair,'read_day',lambda *a:day_rows()[1:])
    with pytest.raises(RuntimeError,match='prior_write_uncertain'):repair.apply(args,client)
    assert not calls


def test_changed_calibration_baseline_blocks_apply(tmp_path,monkeypatch):
    row=day_rows()[0];args=setup_apply(tmp_path,row);calls=[]
    client=SimpleNamespace(query=lambda *a,**k:SimpleNamespace(result_rows=[row]),command=lambda *a,**k:calls.append(a))
    old=day_rows()[1:];old[0][6]*=100
    monkeypatch.setattr(repair,'read_day',lambda *a:old)
    with pytest.raises(ValueError,match='baseline_changed'):repair.apply(args,client)
    assert not calls


def test_empty_evidence_cannot_authorize_nonempty_stage():
    with pytest.raises(ValueError,match='does_not_reproduce'):repair.validate_evidence([],day_rows()[:1])
