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
