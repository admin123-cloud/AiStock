from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.qmt_fullpush_intraday_aggregator import FullPushAggregator, minute_rows, validate_pending_unit_contract
from scripts.qmt_xtquant_minute_backfill_validate import rows_from_qmt
from utils.kline_units import MINUTE_UNIT_CONTRACT_ID, normalize_qmt_minute_values, qmt_tick_lots_yuan


@pytest.mark.parametrize('code,volume,pvolume,amount',[
    ('600000.SH',505325,50532458,467549900),
    ('000698.SZ',40565,4056530,15095700),
    ('920679.BJ',3426,3426,5544600),
    ('000001.SH',517504311,51750431100,873734897100),
    ('399001.SZ',603143763,60314376300,981871743200),
    ('899050.BJ',8791610,8791610,17639421200),
])
def test_observed_sh_sz_bj_stock_index_ticks_share_lots_contract(code,volume,pvolume,amount):
    tick={'time':'20260909093400','lastPrice':10,'open':10,'high':11,'low':9,
          'volume':volume,'pvolume':pvolume,'amount':amount}
    collector=FullPushAggregator(SimpleNamespace())
    collector.on_data({code:tick})
    assert qmt_tick_lots_yuan(tick)==(volume,amount)
    assert minute_rows(list(collector.current_bars.values()),'5m')[0][6:8]==(volume,amount)
    assert collector.daily_rows()[0][6:8]==(volume,amount)
    # Native historical fields are already standardized lots, including indices.
    fields={name:pd.DataFrame([[value]],index=[code],columns=['20260909093500'])
            for name,value in {'open':10,'high':11,'low':9,'close':10,'volume':volume,'amount':amount}.items()}
    rows,counts=rows_from_qmt(fields,'5m',[code],datetime(2026,9,9,10),'2026-09-09','2026-09-09')
    assert counts[code]==1 and rows[0][7:9]==(volume,amount)


def test_tick_delta_uses_lots_not_raw_exchange_dependent_pvolume():
    collector=FullPushAggregator(SimpleNamespace())
    collector.on_data({'600000.SH':{'time':'20260909094000','lastPrice':10,'volume':100,'pvolume':10000,'amount':100000}})
    collector.on_data({'600000.SH':{'time':'20260909094100','lastPrice':10,'volume':120,'pvolume':12000,'amount':120000}})
    assert collector.current_bars['600000.SH'].volume==20


def test_unverified_tick_field_is_not_guessed_or_persisted_as_zero():
    tick={'time':'20260909093400','lastPrice':10,'open':10,'high':10,'low':10,'pvolume':10000,'amount':100000}
    collector=FullPushAggregator(SimpleNamespace())
    collector.on_data({'000001.SH':tick})
    assert not collector.current_bars and not collector.daily_rows()
    assert collector.unit_errors['000001.SH']=='qmt_tick_volume_unit_unverified'
    with pytest.raises(ValueError):qmt_tick_lots_yuan(tick)


def test_explicit_shares_conversion_requires_evidence_and_keeps_fractional_lots():
    assert normalize_qmt_minute_values(150,1500,volume_unit='shares')==(1.5,1500)
    for unit in ('unknown','pvolume','old_database_ratio_100'):
        with pytest.raises(ValueError,match='unverified'):
            normalize_qmt_minute_values(150,1500,volume_unit=unit)


def test_missing_history_volume_cannot_become_zero_bar():
    fields={name:pd.DataFrame([[10]],index=['000001.SH'],columns=['20260909093500'])
            for name in ('open','high','low','close','amount')}
    with pytest.raises(ValueError,match='missing_or_invalid'):
        rows_from_qmt(fields,'5m',['000001.SH'],datetime(2026,9,9,10),'2026-09-09','2026-09-09')


def test_nonempty_legacy_journal_never_replays_under_new_units():
    with pytest.raises(RuntimeError,match='pending_minute_units_unverified'):
        validate_pending_unit_contract({'bars':[{'volume':10000}]})
    validate_pending_unit_contract({'bars':[]})
    validate_pending_unit_contract({'bars':[{'volume':100}], 'minute_unit_contract':MINUTE_UNIT_CONTRACT_ID})


@pytest.mark.parametrize('code,volume,amount',[
    ('000001.SH',517504311,873734897105),('399001.SZ',603143763,981871743156),
    ('899050.BJ',8791610,17639421178),
])
def test_sdk_daily_index_row_payload_preserves_standard_lots(code,volume,amount):
    from scripts.qmtmini_daily_backfill_validate import rows_from_qmt as daily_rows
    fields={name:pd.DataFrame([[value]],index=[code],columns=['20260909'])
            for name,value in {'open':10,'high':11,'low':9,'close':10,'volume':volume,'amount':amount}.items()}
    rows,counts=daily_rows(fields,[code],datetime(2026,9,10,18),'2026-09-09','2026-09-09',index_codes={code})
    assert counts[code]==1 and rows[0][6:8]==(volume,amount)
