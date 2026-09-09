from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from services.operations.ingestion_checkpoint import run_staged
from services.operations.ingestion_backlog import enqueue, process_one
from services.operations.intraday_coverage import evaluate_coverage
from services.operations.ingestion_lanes import IngestionLane
from scripts.run_ingestion_backlog import execution_budget
from scripts.qmt_fullpush_intraday_aggregator import parse_tick_time


def test_boundary_yield_resumes_without_resetting_validated_stage(tmp_path, monkeypatch):
    worker = tmp_path/'worker.py'; worker.write_text('# worker')
    command = ['python', str(worker), '--phase', 'all', '--report', str(tmp_path/'report.json')]
    seen = []
    def runner(cmd, timeout):
        phase = cmd[cmd.index('--phase')+1]; seen.append(phase)
        Path(cmd[cmd.index('--report')+1]).write_text('{"ok":true}')
        if phase == 'fetch':
            monkeypatch.setenv('AISTOCK_INGESTION_YIELD_AT', '1')
        return {'ok': True}
    first = run_staged(command, 100, tmp_path, runner, phases=('fetch', 'validate-stage', 'apply'))
    assert first['deferred'] and first['completed'] == ['fetch']
    monkeypatch.delenv('AISTOCK_INGESTION_YIELD_AT')
    assert run_staged(command, 100, tmp_path, runner, phases=('fetch', 'validate-stage', 'apply'))['ok']
    assert seen == ['fetch', 'validate-stage', 'apply']


def test_uncertain_write_does_not_replay_or_reset_stage(tmp_path):
    worker = tmp_path/'worker.py'; worker.write_text('# worker')
    command = ['python', str(worker), '--phase', 'apply', '--report', str(tmp_path/'report.json')]
    seen = []
    def runner(cmd, timeout):
        seen.append(cmd)
        return {'ok': False, 'uncertain': True, 'error': 'execution_deadline_uncertain'}
    assert not run_staged(command, 1, tmp_path, runner, phases=('apply',))['ok']
    assert run_staged(command, 1, tmp_path, runner, phases=('apply',))['uncertain']
    assert len(seen) == 1


def test_clean_yields_do_not_exhaust_retry_allowance(tmp_path):
    path = tmp_path/'queue.sqlite'; now = datetime(2026, 9, 9, 20)
    enqueue(['--mode', 'after-close-full-refresh'], path=path, now=now)
    for i in range(1, 6):
        result = process_one(lambda args: {'ok': False, 'deferred': True}, path=path, now=now+timedelta(minutes=i*10))
        assert result['state'] == 'queued' and result['attempts'] == 0
    assert process_one(lambda args: {'ok': True}, path=path, now=now+timedelta(minutes=65))['state'] == 'complete'


def test_replay_allows_a_declared_phase_longer_than_fifteen_minutes():
    assert execution_budget(['python', 'qmt_xtquant_data_source_task.py', '--minute-timeout-sec', '10800']) > 10800


def test_missing_security_is_not_exempt_from_name_or_zero_volume():
    now = datetime(2026, 9, 9, 10)
    ticks = {'a': {'time': '20260909100000', 'volume': 0}, 'b': {'time': '20260909094500'}}
    result = evaluate_coverage({'a', 'b', 'c'}, ticks, {'a', 'b'}, now=now, parse_time=parse_tick_time,
        metadata={'a': {'type': 'stock'}, 'b': {'type': 'stock'}, 'c': {'type': 'index', 'name': '退'}})
    assert result['groups']['stock']['persisted_fresh'] == 1
    assert result['groups']['index']['exempt'] == 0 and result['slo_300s'] == 'failed'
    assert {x['reason'] for x in result['unverified']} == {'no_response', 'stale_or_invalid_source_time'}


def test_exemption_requires_current_qmt_evidence():
    now = datetime(2026, 9, 9, 10)
    evidence = {'source': 'qmt', 'status': 'suspended', 'trade_date': '2026-09-09', 'evidence_ref': 'qmt-status-20260909'}
    assert evaluate_coverage({'a'}, {}, set(), now=now, parse_time=parse_tick_time,
                             exemptions={'a': evidence})['slo_300s'] == 'passed'
    evidence['trade_date'] = '2026-09-08'
    assert evaluate_coverage({'a'}, {}, set(), now=now, parse_time=parse_tick_time,
                             exemptions={'a': evidence})['slo_300s'] == 'failed'


def test_degraded_lane_does_not_record_business_success():
    lane = IngestionLane('snapshot', lambda: {'errors': {'coverage': 'missing'}})
    assert lane.start() and lane.join(1)
    assert lane.state['status'] == 'degraded' and 'last_success' not in lane.state


def test_http_ack_loss_preserves_same_minute_identity(tmp_path, monkeypatch):
    from scripts import qmt_fullpush_intraday_aggregator as f
    bar = f.BarState('000001.SZ', datetime(2026, 9, 9, 9, 35), 1, 1, 1, 1, volume=100)
    todo = [bar]; writes = []
    a = SimpleNamespace(args=SimpleNamespace(periods='5m', dry_run=False), minute_journal=tmp_path/'pending.json')
    a.drain_closed_bars = lambda **kw: [todo.pop()] if todo else []
    def write(table, rows, *args):
        writes.extend(rows)
        if len(writes) == 1:
            raise TimeoutError('remote insert committed but acknowledgment lost')
        return len(rows)
    monkeypatch.setattr(f, 'insert_rows', write)
    with pytest.raises(TimeoutError):
        f.flush_minutes(a)
    assert json.loads(a.minute_journal.read_text())['bars']
    f.flush_minutes(a)
    assert writes[0][-1] == writes[1][-1]
    assert writes[0][:8] == writes[1][:8]


def test_completed_history_period_is_not_reexecuted_on_resume(tmp_path):
    from scripts.qmt_xtquant_data_source_task import run_isolated_history
    args = SimpleNamespace(minute_timeout_sec=100, minute_periods='5m,15m', start_date='2026-09-01',
        end_date='2026-09-02', minute_report_dir=str(tmp_path), report='', retry_after_close_source_empty=False)
    calls = []
    def runner(child):
        calls.append(child.minute_periods)
        return {'ok': child.minute_periods == '5m' or calls.count('15m') == 2,
                'minute': {'worker_summary': {'issue_count': 0}}}
    assert not run_isolated_history(args, runner)['ok']
    assert run_isolated_history(args, runner)['ok']
    assert calls == ['5m', '15m', '15m']


def test_index_universe_filters_before_limit_and_rejects_stock(monkeypatch):
    from scripts import qmt_xtquant_data_source_task as task
    seen = []
    class Client:
        def query(self, sql):
            seen.append(sql)
            return SimpleNamespace(result_rows=[('000001.SH',), ('399001.SZ',)])
    monkeypatch.setattr(task, 'load_codes', lambda *args: ['600000.SH', '000001.SH', '399001.SZ'])
    args = SimpleNamespace(universe='index', include_index=False, codes='', limit=1, end_date='2026-09-09')
    assert task.resolve_daily_codes(Client(), args) == ['000001.SH']
    assert "type = 'index'" in seen[0]
    args.codes = '600000.SH'
    with pytest.raises(ValueError, match='outside requested'):
        task.resolve_daily_codes(Client(), args)


def test_index_daily_fixed_entry_cannot_enable_minutes_or_stock():
    from scripts.repair_index_daily import canonical_arguments
    from scripts.run_ingestion_backlog import job_command
    args = SimpleNamespace(start_date='2026-09-08', end_date='2026-09-09', batch_size=999)
    argv = canonical_arguments(args)
    assert argv[argv.index('--universe')+1] == 'index'
    assert '--no-with-minutes' in argv
    assert argv[argv.index('--daily-batch-size')+1] == '80'
    assert Path(job_command({'kind': 'index_daily', 'arguments': argv})[1]).name == 'repair_index_daily.py'


def test_daily_batches_preserve_exact_codes_and_successful_batch_checkpoint(tmp_path):
    from scripts.qmt_xtquant_data_source_task import run_daily_batches
    worker = tmp_path/'worker.py'; worker.write_text('# worker')
    command = ['python', str(worker), '--phase', 'all', '--report', str(tmp_path/'report.json')]
    seen = []
    def runner(cmd, timeout):
        codes = cmd[cmd.index('--codes')+1]; phase = cmd[cmd.index('--phase')+1]
        seen.append((codes, phase))
        if codes == 'i3' and sum(c == 'i3' for c, p in seen) == 1:
            return {'ok': False}
        Path(cmd[cmd.index('--report')+1]).write_text('{"ok":true}')
        return {'ok': True}
    args = SimpleNamespace(daily_phase='all', daily_batch_size=2, daily_timeout_sec=100)
    assert not run_daily_batches(command, ['i1', 'i2', 'i3'], args, tmp_path, runner)['ok']
    assert run_daily_batches(command, ['i1', 'i2', 'i3'], args, tmp_path, runner)['ok']
    assert sum(c == 'i1,i2' for c, p in seen) == 4
    assert sum(c == 'i3' for c, p in seen) == 5


def test_sector_membership_cannot_be_projected_backwards_or_read_without_evidence():
    from scripts.repair_sector_daily import verified_members, TZ
    from datetime import date
    now = datetime(2026, 9, 9, 20, tzinfo=TZ)
    manifest = {'source': 'qmt', 'verified': True, 'generated_at': now.isoformat(),
                'effective_from': '2026-09-09', 'codes': ['sector'], 'members': {'sector': ['a', 'b']}}
    assert len(verified_members(manifest, date(2026, 9, 9), now)) == 2
    with pytest.raises(ValueError, match='older history'):
        verified_members(manifest, date(2026, 9, 8), now)
    manifest['members'] = {}
    with pytest.raises(ValueError, match='member snapshot'):
        verified_members(manifest, date(2026, 9, 9), now)


def test_sector_fill_requires_all_members_and_preserves_previous_price_base():
    import pandas as pd
    from datetime import date
    from scripts.repair_sector_daily import prepare_rows
    day = date(2026, 9, 9)
    members = pd.DataFrame([{'sector_code': 's', 'stock_code': c} for c in ('a', 'b')])
    daily = pd.DataFrame([{'code': c, 'trade_date': day, 'open_ret': .01, 'high_ret': .03,
                          'low_ret': -.01, 'close_ret': .02, 'change_pct': 2., 'amount': 100., 'volume': 10.}
                         for c in ('a', 'b')])
    with pytest.raises(ValueError, match='Incomplete'):
        prepare_rows(members, daily.iloc[:1], set(), {'s': 2500}, day)
    with pytest.raises(ValueError, match='Prior sector close'):
        prepare_rows(members, daily, set(), {}, day)
    rows = prepare_rows(members, daily, set(), {'s': 2500}, day)
    assert rows.iloc[0]['close'] == pytest.approx(2550)
    assert prepare_rows(members, daily, {'s'}, {'s': 2500}, day).empty


def test_owner_migration_keeps_original_triggers_and_principal():
    from scripts.plan_ingestion_owner_migration import transform, NS
    import xml.etree.ElementTree as ET
    xml = f'''<Task xmlns="{NS}"><Triggers>
      <CalendarTrigger><StartBoundary>2026-07-30T00:05:00</StartBoundary><ScheduleByWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger>
      <CalendarTrigger><StartBoundary>2026-07-30T16:10:00</StartBoundary><ScheduleByWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger>
      </Triggers><Principals><Principal id="original"><UserId>user</UserId></Principal></Principals>
      <Settings><Enabled>true</Enabled><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy></Settings>
      <Actions><Exec><Command>powershell.exe</Command><Arguments>-File "F:\\Stock\\AiStock-core\\scripts\\run_qmt_xtquant_collector.ps1" -Mode "daily-coverage-repair"</Arguments>
      <WorkingDirectory>F:\\Stock\\AiStock-core</WorkingDirectory></Exec></Actions></Task>'''
    planned, before, after = transform(xml, r'F:\Stock\AiStock-core', r'F:\Stock\AiStock-refactor',
                                       daily=True, now=datetime(2026, 9, 9, 20))
    root = ET.fromstring(planned)
    assert (before, after) == (2, 3)
    starts = [node.text for node in root.findall('.//{'+NS+'}StartBoundary')]
    assert starts == ['2026-07-30T00:05:00', '2026-07-30T16:10:00', '2026-09-10T18:10:00']
    assert root.find('.//{'+NS+'}Principal').attrib['id'] == 'original'
    assert root.find('.//{'+NS+'}Enabled').text == 'false'
    assert 'daily-maintenance' in planned and 'AiStock-core' not in planned


def test_polling_does_not_hide_a_dead_subscription():
    from scripts.qmt_fullpush_intraday_aggregator import FullPushAggregator
    collector = FullPushAggregator(SimpleNamespace())
    tick = {'a': {'time': '20260909100000', 'lastPrice': 1}}
    collector.on_data(tick, update_bars=False)
    assert collector.last_callback_at is None
    collector.on_data(tick, update_bars=True)
    assert collector.last_callback_at is not None


def test_termination_failure_still_blocks_retry_and_child_uses_utf8(monkeypatch, tmp_path):
    from services.operations import ingestion_budget as budget
    import subprocess
    def popen(command, **kwargs):
        assert kwargs['env']['PYTHONIOENCODING'] == 'utf-8'
        assert kwargs['env']['PYTHONUTF8'] == '1'
        def communicate(**kw):
            raise subprocess.TimeoutExpired(command, 1)
        return SimpleNamespace(pid=99999, communicate=communicate)
    def fail(*args, **kwargs):
        raise OSError('termination tool unavailable')
    monkeypatch.setattr(budget.subprocess, 'Popen', popen)
    if budget.os.name == 'nt':
        monkeypatch.setattr(budget.subprocess, 'run', fail)
    else:
        monkeypatch.setattr(budget.os, 'killpg', fail)
    result = budget.run_owned(['python', 'worker.py'], 1, cwd=tmp_path)
    assert result['uncertain'] and result['termination_error'] == 'termination tool unavailable'


def test_holding_owner_keeps_two_triggers_and_updates_inner_source_root():
    from scripts.plan_ingestion_owner_migration import transform, NS
    import xml.etree.ElementTree as ET
    xml = f'''<Task xmlns="{NS}"><Triggers><CalendarTrigger><StartBoundary>2026-07-01T09:30:00</StartBoundary></CalendarTrigger>
      <CalendarTrigger><StartBoundary>2026-07-01T16:00:00</StartBoundary></CalendarTrigger></Triggers>
      <Settings><Enabled>true</Enabled></Settings><Actions><Exec><Command>pythonw.exe</Command>
      <Arguments>"F:\\Stock\\AiStock-refactor\\scripts\\run_holding_t_service.py" --source-root "F:\\Stock\\AiStock-core"</Arguments>
      <WorkingDirectory>F:\\Stock\\AiStock-refactor</WorkingDirectory></Exec></Actions></Task>'''
    planned, before, after = transform(xml, r'F:\Stock\AiStock-core', r'F:\Stock\AiStock-release-final', holding=True)
    assert r'F:\Stock\AiStock-refactor' not in planned
    assert r'F:\Stock\AiStock-release-final\scripts\run_holding_t_service.py' in planned
    assert (before, after) == (2, 2) and 'AiStock-core' not in planned
    assert '--source-root' in planned and 'run_holding_t_service.py' in planned


def test_dify_expired_once_triggers_become_daily_without_losing_windows():
    from scripts.plan_dify_launcher import planned_xml, NS
    import xml.etree.ElementTree as ET
    xml = f'''<Task xmlns="{NS}"><Triggers><TimeTrigger><StartBoundary>2026-09-07T09:30:00+08:00</StartBoundary>
      <Repetition><Interval>PT30M</Interval><Duration>PT2H30M</Duration></Repetition></TimeTrigger>
      <CalendarTrigger><StartBoundary>2026-09-07T15:30:00+08:00</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
      <Settings><Enabled>true</Enabled></Settings><Actions><Exec><Command>pwsh.exe</Command>
      <Arguments>-NoProfile -File "original.ps1"</Arguments></Exec></Actions></Task>'''
    planned, count, converted = planned_xml(xml, 'system-powershell.exe', 'original.ps1', 'compatible.ps1')
    root = ET.fromstring(planned)
    assert (count, converted) == (2, 1)
    assert not root.findall('.//{'+NS+'}TimeTrigger')
    assert root.find('.//{'+NS+'}Interval').text == 'PT30M'
    assert root.find('.//{'+NS+'}Duration').text == 'PT2H30M'
    assert root.find('.//{'+NS+'}StartBoundary').text == '2026-09-07T09:30:00+08:00'
    assert root.find('.//{'+NS+'}Enabled').text == 'false'
