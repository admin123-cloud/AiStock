from datetime import datetime
from threading import Event
from types import SimpleNamespace
import json
from zoneinfo import ZoneInfo
import pandas as pd
import pytest
from services.operations.ingestion_lanes import IngestionLane
from services.operations.ingestion_checkpoint import run_staged
from scripts import qmt_fullpush_intraday_aggregator as f


def test_slow_minute_lane_does_not_block_snapshot_or_spawn_duplicates():
    gate=Event()
    slow=IngestionLane('minutes',lambda: gate.wait(2))
    fast=IngestionLane('snapshot',lambda: {'rows':6000})
    try:
        assert slow.start()
        assert not slow.start()
        assert fast.start()
        assert fast.join(1)
        assert fast.state['result']['rows']==6000
        assert slow.snapshot()['status']=='running'
    finally:
        gate.set();slow.join(2)


@pytest.mark.parametrize('time,expected', [('09:30:15',None),('09:35:15','09:35'),('12:30:15','11:30'),('15:10:15','15:00')])
def test_closed_boundary_respects_lunch_and_close(time,expected):
    x=f.latest_closed_boundary(datetime.fromisoformat('2026-09-09T'+time))
    assert (x.strftime('%H:%M') if x else None)==expected


def test_failed_minute_write_is_journaled_and_retried(tmp_path,monkeypatch):
    bar=f.BarState('000001.SZ',datetime(2026,9,9,9,35),1,1,1,1,volume=100,amount=100)
    todo=[bar]
    a=SimpleNamespace(args=SimpleNamespace(periods='5m',dry_run=False),minute_journal=tmp_path/'pending.json')
    a.drain_closed_bars=lambda **kw: [todo.pop()] if todo else []
    def fail(*args):raise RuntimeError('database down')
    monkeypatch.setattr(f,'insert_rows',fail)
    with pytest.raises(RuntimeError):f.flush_minutes(a)
    assert len(json.loads(a.minute_journal.read_text())['bars'])==1
    monkeypatch.setattr(f,'insert_rows',lambda table,rows,*args:len(rows))
    assert f.flush_minutes(a)['minute_rows']['5m']==1
    assert json.loads(a.minute_journal.read_text())['bars']==[]


def test_newer_push_wins_over_older_polled_tick():
    a=f.FullPushAggregator(SimpleNamespace())
    old={'time':'20260909100000','lastPrice':1,'open':1,'high':2,'low':1,'volume':10,'amount':1000}
    new={**old,'time':'20260909100100','lastPrice':2}
    a.latest_ticks={'000001.SZ':new};a.full_tick_ticks={'000001.SZ':old}
    assert a.daily_rows()[0][5]==2


def test_resume_validation_failure_does_not_download_again(tmp_path):
    worker=tmp_path/'worker.py';worker.write_text('# worker')
    command=['python',str(worker),'--phase','all','--report',str(tmp_path/'old.json'),'--reset-stage','--in-process']
    calls=[]
    def runner(cmd,timeout):
        phase=cmd[cmd.index('--phase')+1];calls.append(phase)
        assert '--in-process' not in cmd
        if phase=='validate-stage' and len(calls)==2:return {'ok':False,'stderr_tail':'temporary db timeout'}
        from pathlib import Path
        Path(cmd[cmd.index('--report')+1]).write_text(json.dumps({'ok':True}))
        return {'ok':True}
    phases=('fetch','validate-stage','apply')
    assert not run_staged(command,10,tmp_path,runner,phases=phases)['ok']
    assert run_staged(command,10,tmp_path,runner,phases=phases)['ok']
    assert calls==['fetch','validate-stage','validate-stage','apply']


def test_storage_corruption_stops_repeated_download(tmp_path):
    worker=tmp_path/'worker.py';worker.write_text('# worker')
    cmd=['python',str(worker),'--phase','all','--report',str(tmp_path/'old.json')]
    calls=[]
    def runner(*args,**kw):calls.append(1);return {'ok':False,'stderr_tail':'CHECKSUM_DOESNT_MATCH'}
    assert not run_staged(cmd,10,tmp_path,runner,phases=('fetch',))['ok']
    assert run_staged(cmd,10,tmp_path,runner,phases=('fetch',))['reason']=='storage_blocked_requires_recovery'
    assert len(calls)==1


def test_report_claiming_success_with_failed_batches_is_not_checkpointed(tmp_path):
    worker=tmp_path/'worker.py';worker.write_text('# worker')
    cmd=['python',str(worker),'--phase','all','--report',str(tmp_path/'old.json')]
    calls=[]
    def runner(command,timeout):
        from pathlib import Path
        calls.append(1)
        Path(command[command.index('--report')+1]).write_text(json.dumps({'ok':True,'summaries':[{'failed_batches':1}]}))
        return {'ok':True}
    for _ in range(2):
        assert not run_staged(cmd,10,tmp_path,runner,phases=('fetch',))['ok']
    assert len(calls)==2


def test_damaged_derived_period_does_not_block_other_periods(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,9,9,15,0,20,tzinfo=tz)
    monkeypatch.setattr(f,'datetime',Clock)
    a=SimpleNamespace(args=SimpleNamespace(periods='5m,15m,30m,60m',dry_run=False))
    a.drain_closed_bars=lambda **kw:[]
    def derive(period, day, **kwargs):
        if period=='30m':raise RuntimeError('damaged table')
        return [(period,)]
    monkeypatch.setattr(f,'derive_higher_rows_from_clickhouse',derive)
    monkeypatch.setattr(f,'ensure_live_derived_table',lambda _client, period:f'kline_minute_{period[:-1]}_live_derived')
    monkeypatch.setattr(f,'insert_rows',lambda table,rows,*args:len(rows))
    result=f.flush_minutes(a)
    assert result['minute_rows']['15m']==1
    assert result['minute_rows']['60m']==1
    assert '30m' in result['errors']


def test_dry_run_derived_flush_does_not_create_tables(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 9, 10, 0, 20, tzinfo=tz)

    monkeypatch.setattr(f, 'datetime', Clock)
    bars = [
        f.BarState('000001.SZ', Clock(2026, 9, 9, 9, minute), 1, 1, 1, 1, volume=100, amount=100)
        for minute in (50, 55)
    ]
    bars.append(f.BarState('000001.SZ', Clock(2026, 9, 9, 10, 0), 1, 1, 1, 1, volume=100, amount=100))
    a = SimpleNamespace(args=SimpleNamespace(periods='15m', dry_run=True))
    a.drain_closed_bars = lambda **kw: list(bars)
    monkeypatch.setattr(f, 'clickhouse_client', lambda: (_ for _ in ()).throw(AssertionError('must not create table')))
    writes = []
    monkeypatch.setattr(f, 'insert_rows', lambda table, rows, *args: writes.append((table, rows)) or len(rows))
    result = f.flush_minutes(a)
    assert result['minute_rows']['15m'] == 1
    assert writes[-1][0] == 'kline_minute_15_live_derived'


def test_live_derived_query_matches_aware_clickhouse_timestamp_to_wall_clock_boundary(monkeypatch):
    tz = ZoneInfo('Asia/Shanghai')
    source = pd.DataFrame([
        {'code':'000001.SZ','datetime':datetime(2026,9,9,9,minute,tzinfo=tz),
         'open':1.0,'high':2.0,'low':0.5,'close':1.5,'volume':100.0,'amount':1000.0,
         'created_at':datetime(2026,9,9,9,minute,tzinfo=tz)}
        for minute in (35,40,45)
    ])
    monkeypatch.setattr(f, 'clickhouse_query_df', lambda *args, **kwargs: source)
    rows = f.derive_higher_rows_from_clickhouse(
        '15m', datetime(2026,9,9).date(), target_boundary=datetime(2026,9,9,9,45))
    assert len(rows) == 1
    assert rows[0][0] == '000001.SZ'
    assert rows[0][1] == datetime(2026,9,9,9,45)
