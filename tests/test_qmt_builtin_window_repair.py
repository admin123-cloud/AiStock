from datetime import date, datetime, timezone
from types import SimpleNamespace
import hashlib
import pytest

import pandas as pd

from scripts.repair_qmt_builtin_window import (
    FIELDS, SLOTS, aggregate, epoch, same, validate_day, validate_daily,
    load_raw,
    verified_absence,
    repair_table,
    fetch_existing,
    validate_absence_source,
    CROSS_BORDER_SLOTS, normalize_native_daily_volume,
    validate_bse_total_quantity,
)
from scripts.qmtmini_daily_backfill_validate import reconcile_verified_pending_listing_metadata
from data_fetcher.sources.qmtmini import _aggregate_5m_history
from utils.qmt_daily_prices import daily_price_metrics
from scripts.qmtmini_daily_backfill_validate import rows_from_qmt


def sample():
    frame = pd.DataFrame([[10., 11., 9., 10.5, 100., 100000.] for _ in SLOTS],
                         columns=FIELDS, index=['20260930' + slot for slot in SLOTS])
    daily = pd.Series(dict(open=10., high=11., low=9., close=10.5,
                           volume=4800., amount=4800000., suspendFlag=0))
    return frame, daily


def test_native_units_reject_hundredfold_daily_volume():
    frame, daily = sample()
    assert validate_daily(daily, 'stock') is None
    assert validate_day('20260930', frame, daily, 'stock') is None
    daily.volume *= 100
    assert validate_daily(daily, 'stock') == 'native_daily_lots_yuan_contract_mismatch'
    assert validate_day('20260930', frame, daily, 'stock') == 'native_minute_daily_volume_mismatch'


def test_closed_slots_and_aggregation_do_not_cross_lunch():
    frame, daily = sample()
    assert validate_day('20260930', frame.iloc[1:], daily, 'stock') == 'minute_slots_not_closed'
    rows = aggregate(frame, 60)
    assert [row[0][-6:] for row in rows] == ['103000', '113000', '140000', '150000']
    assert len(rows) == 4 and sum(row[1][4] for row in rows) == daily.volume


def test_cross_border_slots_require_explicit_index_identity_and_close():
    frame, daily = sample()
    frame = pd.DataFrame([frame.iloc[0].tolist()] * 68, columns=FIELDS,
                         index=['20260930' + slot for slot in CROSS_BORDER_SLOTS])
    daily.volume = 6800
    daily.amount = 6800000
    assert validate_day('20260930', frame, daily, 'index', '980001.SZ') is None
    assert validate_day('20260930', frame, daily, 'stock', '980001.SZ') == 'minute_slots_not_closed'
    assert validate_day('20260930', frame, daily, 'index', '399001.SZ') == 'minute_slots_not_closed'
    assert validate_day('20260930', frame.iloc[:-1], daily, 'index', '980001.SZ') == 'minute_slots_not_closed'
    for period, count in ((15, 23), (30, 12), (60, 7)):
        rows = aggregate(frame, period)
        assert len(rows) == count
        assert sum(row[1][4] for row in rows) == daily.volume
        assert sum(row[1][5] for row in rows) == daily.amount
        assert any(row[0].endswith('120000') for row in rows)
        assert rows[-1][0].endswith('161000')
    rows = aggregate(frame, 60)
    assert rows[2][1][4] == 600 and rows[3][0].endswith('140000')
    with pytest.raises(ValueError, match='closed day'):
        aggregate(frame.iloc[:-1], 60)


def test_int32_wrap_needs_exact_complete_minute_proof_and_index_allowlist():
    frame, daily = sample()
    frame.volume = 50000000
    frame.amount = 50000000000
    daily.volume = 2400000000 - 2**32
    daily.amount = 2400000000000
    original = daily.copy()
    normalized, evidence = normalize_native_daily_volume('399379.SZ', '20260930', daily, frame, 'index')
    assert normalized.volume == 2400000000 and evidence['raw_volume'] == original.volume
    assert daily.equals(original)
    assert normalize_native_daily_volume('399001.SZ', '20260930', daily, frame, 'index')[1] is None
    assert normalize_native_daily_volume('399379.SZ', '20260930', daily, frame, 'stock')[1] is None
    assert normalize_native_daily_volume('399379.SZ', '20260930', daily, frame.iloc[:-1], 'index')[1] is None
    frame.iloc[0, 4] += 1
    assert normalize_native_daily_volume('399379.SZ', '20260930', daily, frame, 'index')[1] is None
    frame.iloc[0, 4] -= 1
    frame.iloc[0, 5] *= 2
    assert normalize_native_daily_volume('399379.SZ', '20260930', daily, frame, 'index')[1] is None


def test_runtime_history_keeps_cross_border_tail_without_merging_missing_slots():
    frame, _ = sample()
    frame = pd.DataFrame([frame.iloc[0].tolist()] * 68, columns=FIELDS)
    frame['datetime'] = pd.to_datetime(['20260930' + slot for slot in CROSS_BORDER_SLOTS], format='%Y%m%d%H%M%S')
    for period, count in (('15m', 23), ('30m', 12), ('60m', 7)):
        rows = _aggregate_5m_history(frame, period, '980001.SZ')
        assert len(rows) == count and rows.volume.sum() == 6800
        assert rows.datetime.iloc[-1].strftime('%H:%M') == '16:10'
    ordinary = _aggregate_5m_history(frame, '60m', '399001.SZ')
    assert len(ordinary) == 4 and ordinary.datetime.iloc[-1].strftime('%H:%M') == '15:00'
    missing = _aggregate_5m_history(frame.drop(index=1), '60m', '980001.SZ')
    assert len(missing) == 6 and missing.datetime.iloc[0].strftime('%H:%M') == '11:30'
    no_close = _aggregate_5m_history(frame.iloc[:-1], '15m', '980001.SZ')
    assert len(no_close) == 22 and no_close.datetime.iloc[-1].strftime('%H:%M') == '16:00'


def test_fractional_five_minute_lots_are_not_truncated():
    frame, daily = sample()
    frame.iloc[0, 4] = 100.5
    assert validate_day('20260930', frame, daily, 'stock') == 'native_minute_volume_not_integer_lots'


def test_aggregation_preserves_open_close_and_extremes():
    frame, _ = sample()
    frame.iloc[:3] = [[10,12,9,11,100,100000], [11,13,10,12,200,220000], [12,14,8,10,300,330000]]
    timestamp, values = aggregate(frame, 15)[0]
    assert timestamp == '20260930094500'
    assert values == [10,14,8,10,600,650000]


def test_epoch_is_independent_of_host_timezone():
    assert epoch('20260930150000') == int(datetime(2026, 9, 30, 7, tzinfo=timezone.utc).timestamp())


def test_comparison_detects_units_without_rewriting_float_noise():
    assert same([10, 11, 9, 10.5, 100, 100000], [10.0000002, 11, 9, 10.5, 100, 100000.001])
    assert not same([10, 11, 9, 10.5, 100, 100000], [10, 11, 9, 10.5, 10000, 10])


def test_source_suspension_is_not_a_filled_trading_day():
    _, daily = sample()
    daily.suspendFlag = 1
    assert validate_daily(daily, 'stock') == 'native_suspended'


def test_old_pending_ipo_evidence_preserves_later_confirmed_listing():
    commands=[]
    client=SimpleNamespace(query=lambda sql:SimpleNamespace(result_rows=[
        ('301686.SZ',date(2026,9,22)),('920201.BJ',date(2026,9,24))]),command=commands.append)
    assert reconcile_verified_pending_listing_metadata(client) == 0
    assert commands == []


def test_pending_ipo_update_keeps_date_guard_against_concurrent_refresh():
    commands=[]
    client=SimpleNamespace(query=lambda sql:SimpleNamespace(result_rows=[
        ('301686.SZ',date(2026,9,11)),('920201.BJ',date(2026,9,24))]),command=commands.append)
    assert reconcile_verified_pending_listing_metadata(client) == 1
    assert len(commands) == 1 and 'list_date <= toDate' in commands[0]


def test_changed_source_file_blocks_loading_before_any_write(tmp_path):
    path=tmp_path/'native_raw'/'600000.SH_5m.parquet'
    path.parent.mkdir()
    sample()[0].to_parquet(path)
    manifest={'files':{'600000.SH_5m':{'file':str(path),'rows':48,
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}}
    path.write_bytes(path.read_bytes()+b'changed')
    with pytest.raises(RuntimeError,match='hash mismatch'):
        load_raw(tmp_path,'600000.SH','5m',manifest)


def test_source_manifest_cannot_reference_a_file_outside_root(tmp_path):
    root=tmp_path/'artifact';root.mkdir()
    path=tmp_path/'external.parquet'
    manifest={'files':{'600000.SH_5m':{'file':str(path),'rows':48,'sha256':'unused'}}}
    with pytest.raises(RuntimeError,match='outside frozen artifact root'):
        load_raw(root,'600000.SH','5m',manifest)


def test_retirement_trading_day_is_not_excluded_by_earlier_suspension():
    metadata={'verified_events':[{'code':'301139.SZ','start_date':'2026-08-31','end_date':'2026-09-29',
                'status':'suspended','evidence_url':'https://www.szse.cn/notice'}]}
    assert verified_absence(metadata,'301139.SZ','20260929') is not None
    assert verified_absence(metadata,'301139.SZ','20260930') is None


def test_notice_does_not_hide_minute_trading_when_daily_is_absent():
    minute, daily = sample()
    assert validate_absence_source(None, minute) == 'source_conflicts_with_verified_absence'
    daily.volume = 0
    daily.amount = 0
    daily.suspendFlag = 1
    assert validate_absence_source(daily, minute) == 'source_conflicts_with_verified_absence'


def test_notice_does_not_hide_amount_only_or_invalid_source():
    minute, daily = sample()
    empty = minute.iloc[:0]
    daily.volume = 0
    assert validate_absence_source(daily, empty) == 'source_conflicts_with_verified_absence'
    daily.amount = float('nan')
    assert validate_absence_source(daily, empty) == 'invalid_native_absence_source'
    daily.amount = -1
    assert validate_absence_source(daily, empty) == 'invalid_native_absence_source'


def test_absence_requires_empty_source_or_explicit_zero_suspension():
    minute, daily = sample()
    empty = minute.iloc[:0]
    assert validate_absence_source(None, empty) is None
    daily.volume = daily.amount = 0
    assert validate_absence_source(daily, empty) == 'native_zero_row_without_suspension'
    daily.suspendFlag = 1
    assert validate_absence_source(daily, empty) is None


def test_reconciliation_reports_canonical_only_keys_without_removing_them(tmp_path, monkeypatch):
    existing_key=('600000.SH',date(2026,9,29))
    monkeypatch.setattr('scripts.repair_qmt_builtin_window.fetch_existing',
                        lambda *a,**k: {existing_key: ()})
    client=SimpleNamespace(insert=lambda *a,**k: pytest.fail('No writes expected'))
    result=repair_table(client,'kline_daily',[],['600000.SH'],tmp_path,'probe',True,daily=True)
    assert result['canonical_only'] == 1 and result['native_target_rows'] == 0
    assert not result['applied']
    evidence=pd.read_parquet(result['canonical_only_file'])
    assert list(evidence.itertuples(index=False,name=None)) == [existing_key]


def test_duplicate_source_target_keys_stop_before_insert(tmp_path, monkeypatch):
    monkeypatch.setattr('scripts.repair_qmt_builtin_window.fetch_existing',lambda *a,**k: {})
    row=('600000.SH',date(2026,9,30),10.,11.,9.,10.5,100.,100000.,0.,0.,0.,0.,datetime.now(),1)
    with pytest.raises(RuntimeError,match='Duplicate native target'):
        repair_table(None,'kline_daily',[row,row],['600000.SH'],tmp_path,'probe',True,daily=True)


def test_empty_price_scope_does_not_query_preserved_excluded_rows():
    client = SimpleNamespace(query=lambda *a, **k: pytest.fail('Excluded scope must not be queried'))
    assert fetch_existing(client, 'kline_daily', [], daily=True) == {}
    assert fetch_existing(client, 'kline_minute_5', []) == {}


def test_daily_metrics_use_native_reference_instead_of_old_close():
    frame = pd.DataFrame({'close':[11.60,11.30], 'high':[11.70,11.40], 'low':[11.50,11.22],
                          'preClose':[11.55,11.35]}, index=['20260923','20260924'])
    got = daily_price_metrics(frame).loc['20260924']
    assert got.change_amount == pytest.approx(-.05)
    assert got.change_pct == pytest.approx(-.05/11.35*100)
    frame.loc['20260924','preClose'] = float('nan')
    assert daily_price_metrics(frame).loc['20260924'].change_amount == pytest.approx(-.30)


def test_auxiliary_only_daily_difference_is_corrected_and_turnover_preserved(tmp_path, monkeypatch):
    day=date(2026,9,24);now=datetime.now();key=('000001.SZ',day)
    old=(key[0],day,11.3,11.4,11.22,11.3,100.,113000.,1.,-2.5,-.3,7.2,now,1)
    target=(key[0],day,*old[2:8],1.585903,-.440529,-.05,0.,now,2)
    state={key:old}
    monkeypatch.setattr('scripts.repair_qmt_builtin_window.fetch_existing',lambda *a,**k:dict(state))
    client=SimpleNamespace(insert=lambda table,rows,**kw:state.update({(r[0],r[1]):r for r in rows}))
    report=repair_table(client,'kline_daily',[target],[key[0]],tmp_path,'aux',True,True)
    assert report['corrections']==1 and report['readback_verified']
    assert state[key][8:11]==target[8:11] and state[key][11]==old[11]


def test_bse_total_quantity_can_exceed_auction_price_band_but_not_unit_limits():
    frame,daily=sample();frame.low=9.5;frame.amount=91000.
    daily.low=9.5;daily.amount=frame.amount.sum();daily['preClose']=10.
    assert validate_daily(daily,'stock')=='native_daily_lots_yuan_contract_mismatch'
    proof=validate_bse_total_quantity('20260930',frame,daily,'920005.BJ','20200101')
    assert proof and proof['vwap']==9.1
    assert validate_bse_total_quantity('20260930',frame,daily,'600000.SH','20200101') is None
    assert validate_bse_total_quantity('20260930',frame,daily,'920005.BJ','20260930') is None
    assert validate_bse_total_quantity('20260930',frame.iloc[1:],daily,'920005.BJ','20200101') is None
    frame.volume*=100;daily.volume*=100
    assert validate_bse_total_quantity('20260930',frame,daily,'920005.BJ','20200101') is None


def test_zero_trade_previous_quote_does_not_change_stock_transaction_extremes():
    frame,daily=sample();daily.high=11.;daily.preClose=12.
    frame.iloc[0]=[12.,12.,12.,12.,0.,0.]
    daily.volume=frame.volume.sum();daily.amount=frame.amount.sum()
    assert validate_day('20260930',frame,daily,'stock','600000.SH') is None
    assert validate_day('20260930',frame,daily,'index','399001.SZ')=='minute_outside_native_daily_range'
    assert aggregate(frame,15,'stock')[0][1][:4]==[10.,11.,9.,10.5]
    frame.iloc[:3]=[12.,12.,12.,12.,0.,0.]
    assert aggregate(frame,15,'stock')[0][1]==[12.,12.,12.,12.,0.,0.]
    frame.iloc[0,1]=13.
    assert validate_day('20260930',frame,daily,'stock','600000.SH')=='native_zero_quantity_quote_outside_daily_range'


def test_native_zero_quantity_moving_quote_preserves_base_bar_and_checks_daily_contract():
    frame,daily=sample()
    frame.iloc[0]=[10.,10.8,9.8,10.6,0.,0.]
    daily.volume=frame.volume.sum();daily.amount=frame.amount.sum()
    assert validate_day('20260930',frame,daily,'stock','600000.SH') is None
    assert aggregate(frame,5,'stock')[0][1]==[10.,10.8,9.8,10.6,0.,0.]
    assert aggregate(frame,15,'stock')[0][1]==[10.,11.,9.,10.5,200.,200000.]
    daily.volume += 100
    assert validate_day('20260930',frame,daily,'stock','600000.SH')=='native_minute_daily_volume_mismatch'


def test_runtime_stock_aggregation_excludes_zero_trade_quote_but_index_keeps_it():
    frame,_=sample();frame.iloc[0]=[12.,12.,12.,12.,0.,0.]
    frame['datetime']=pd.to_datetime(frame.index,format='%Y%m%d%H%M%S')
    stock=_aggregate_5m_history(frame,'15m','000001.SZ').iloc[0]
    index=_aggregate_5m_history(frame,'15m','399001.SZ').iloc[0]
    assert stock.open==10. and stock.high==11. and stock.close==10.5
    assert index.open==12. and index.high==12.


def test_native_reference_survives_daily_field_reshape_and_optional_stage_column():
    code='000001.SZ';dates=['20260923','20260924']
    values=dict(open=[11.6,11.3],high=[11.7,11.4],low=[11.5,11.22],close=[11.6,11.3],
                volume=[100.,100.],amount=[116000.,113000.],preClose=[11.55,11.35])
    data={k:pd.DataFrame([v],index=[code],columns=dates) for k,v in values.items()}
    data['close'].attrs['qmt_market_transport']='builtin'
    rows,_=rows_from_qmt(data,[code],datetime(2026,10,7,18),'2026-09-23','2026-09-24',include_reference=True)
    assert len(rows[-1])==14 and rows[-1][-1]==11.35 and rows[-1][10]==pytest.approx(-.05)
    legacy={k:v.copy() for k,v in data.items()};legacy['close'].attrs.clear()
    rows,_=rows_from_qmt(legacy,[code],datetime(2026,10,7,18),'2026-09-23','2026-09-24',include_reference=True)
    assert rows[-1][-1] is None and rows[-1][10]==pytest.approx(-.3)
