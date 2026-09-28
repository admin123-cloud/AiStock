"""Publish host task inventory and reconcile incidents. No repair/order side effects.

Use --notify only for the operational installation. Omit for isolated verification.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.operations.health import BUSINESS_TZ, read_snapshot, strategy_data_checks, write_snapshot
from services.operations.read_models import read_json, mainwave_daily, record_mainwave_tracking, task_board
from services.operations.incidents import reconcile, dispatch, send_digest, read_incidents, notification_configured, notification_transport_status, baseline_notifications
from services.operations.delivery import build_delivery_calendar, delivery_contract
from utils.paths import runtime_path


def host_inventory() -> list[dict]:
    if os.name != 'nt':
        raise RuntimeError('Host inventory must run on Windows')
    script = r"""[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); $ErrorActionPreference='Stop';
    @(Get-ScheduledTask | Where-Object {$_.TaskName -like '*AiStock*'} | ForEach-Object {
      $info=$_ | Get-ScheduledTaskInfo
      [pscustomobject]@{name=$_.TaskName;state=[string]$_.State;last_run=if($info.LastRunTime){$info.LastRunTime.ToString('o')}else{$null};next_run=if($info.NextRunTime){$info.NextRunTime.ToString('o')}else{$null};last_result=$info.LastTaskResult;description=$_.Description;window=(@($_.Triggers | ForEach-Object { $trigger=$_; "$($trigger.StartBoundary) [$($trigger.CimClass.CimClassName)] interval=$($trigger.Repetition.Interval) duration=$($trigger.Repetition.Duration)" }) -join '; ')}
    }) | ConvertTo-Json -Depth 3 -Compress"""
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                            capture_output=True, text=True, encoding='utf-8-sig', timeout=25,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Windows task inventory failed')
    value = json.loads(result.stdout or '[]')
    return value if isinstance(value, list) else [value]


def repair_incident_checks(result):
    reasons = {'budget_exhausted':'自动修复批次预算已用尽',
               'no_progress_requires_review':'连续复验没有进展，已停止自动补数',
               'requires_review':'执行结果不确定或任务失败，需人工核实'}
    checks = [{'name':'repair:'+item['key'], 'ok':False, 'reason':item['status'],
               'message':item['key']+'：'+reasons[item['status']], 'remediation_owner':'共享数据修复队列'}
              for item in result.get('held', []) if item.get('status') in reasons]
    checks.extend({'name':'repair:'+key, 'ok':True, 'message':key+'：独立数据复验通过'}
                  for key in result.get('verified', []))
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-root', type=Path, default=runtime_path())
    parser.add_argument('--notify', action='store_true')
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--baseline-notifications', action='store_true',
                        help='Adopt existing incidents without emitting historic failure or recovery mail.')
    args = parser.parse_args()
    if args.baseline_notifications:
        return baseline(args)
    from services.operations.lifecycle import InstanceLock
    lock = InstanceLock(args.runtime_root/'operations/publisher-owner.lock')
    lock.acquire()
    try:
        return publish(args)
    finally:
        lock.release()


def publish(args):
    now = datetime.now(BUSINESS_TZ)
    root = args.runtime_root
    health = read_snapshot(root/'health/latest.json', now=now)
    daily = mainwave_daily(root,now=now)
    repair_result = {'status':'verification_unavailable'}
    checks = [x for x in daily['checks'] if x['name'] != 'runtime_data_health']
    checks.append({'name':'runtime_health_publisher','ok':health.get('publisher_status')=='healthy',
                   'message':'健康发布者心跳检查；过期状态不能解释为正常'})
    board = task_board(root, {}, now=now)
    api_state = board['api_runtime']
    checks.append({'name':'api_scheduler_readiness', 'ok':api_state['ready'],
                   'message':'API任务启动与30秒心跳验收', 'remediation_owner':'API服务生命周期'})
    checks += [{**x, 'ok': x.get('status') in ('healthy','deferred')} for x in health.get('components', [])]
    try:
        tasks = host_inventory()
        write_snapshot({'generated_at': now.isoformat(timespec='seconds'), 'tasks': tasks},root/'operations/host_tasks.json')
        checks.append({'name':'host_task_inventory','ok':True})
        checks += [{'name': 'task:'+x['name'], 'ok': x['last_result'] in (0,267009,267011,None),
                    'message': f"最近运行结果 {x['last_result']}", 'remediation_owner':x['name']}
                   for x in tasks if x['state'] not in ('Disabled','Running')]
    except Exception as exc:
        checks.append({'name':'host_task_inventory','ok':False,'message':str(exc)})
    try:
        from utils.market_warehouse import clickhouse_client
        calendar = build_delivery_calendar(clickhouse_client(),days=30,now=now,sector_universe=read_json(root/'operations/sector_universe.json'))
        write_snapshot(calendar,root/'operations/delivery_calendar.json')
        from services.operations.remediation import request_repairs
        repair_result = request_repairs(calendar,root,enabled=getattr(args,'repair',False),now=now)
        checks.extend(repair_incident_checks(repair_result))
        checks.append({'name':'delivery_verifier','ok':True})
        for dataset in calendar['datasets']:
            bad = [cell for cell in dataset['cells'] if cell['status'] in ('partial','missing','unknown','unverified')]
            checks.append({'name':'delivery:'+dataset['id'], 'ok':not bad,
                           'message':f"{dataset['label']}：{len(bad)}个交易日存在缺口或无法验收",
                           'dates':[x['date'] for x in bad],
                           'cells':dataset['cells'],
                           'missing_keys':sum(x.get('missing') or 0 for x in bad),
                           'remediation_owner':'板块日线维护' if dataset['id']=='sector_daily' else 'Windows QMT 数据维护',
                           'repair_policy':'由既有唯一生产者修复；本发布器不并发启动第二个补数进程'})
    except Exception as exc:
        checks.append({'name':'delivery_verifier','ok':False,'message':f'覆盖验收无法完成：{type(exc).__name__}'})
    path = root/'operations/incidents.sqlite3'
    events = reconcile(path,checks,now=now,grace_minutes=delivery_contract()['repair_grace_minutes'])
    record_mainwave_tracking(root, daily)
    result = dispatch(path,send_digest,now=now) if args.notify else {'status':'not_requested'}
    transport = notification_transport_status(path,enabled=args.notify,configured=notification_configured(),now=now)
    write_snapshot({'generated_at': datetime.now(BUSINESS_TZ).isoformat(timespec='seconds'),
                    'notifications_enabled':args.notify, 'notification':result, 'repair':repair_result,
                    'notification_transport_ok':transport['ok'], 'notification_transport':transport,
                    'incidents':len(events)},root/'operations/latest.json')
    print(json.dumps({'incidents':len(events),'notification':result},ensure_ascii=False))
    return 0


def baseline(args):
    result = baseline_notifications(args.runtime_root/'operations/incidents.sqlite3')
    print(json.dumps({'baseline_notifications': result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
