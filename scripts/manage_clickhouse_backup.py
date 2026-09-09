"""Inspect or create a ClickHouse native backup in a host-mounted dedicated disk."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import subprocess
import os
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.operations.backup import backup, manifests, retention_plan, verify_archive, reconcile, read_manifest, prune_verified_groups
from services.operations.health import BUSINESS_TZ, write_snapshot
from services.operations.lifecycle import InstanceLock
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


def publish(value, *, root=None):
    path = runtime_path('operations', 'backups', 'latest.json')
    previous = read_manifest(path)
    if value['state'] == 'archive_verified' and root is not None:
        newer = [v for v in manifests(root) if v.get('database') == value['database']
                 and v.get('created_at', '') > value.get('created_at', '')]
        if newer:
            value = max(newer, key=lambda v: v['created_at'])
    complete = value['state'] == 'archive_verified'
    restored = value.get('restore_verified_at') or (previous.get('last_restore_verified_at') if not complete else None)
    if not restored and root is not None and value.get('base_id'):
        restored = read_manifest(root / value['base_id'] / 'manifest.json').get('restore_verified_at')
    try:
        restore_fresh = 0 <= (datetime.now(BUSINESS_TZ)-datetime.fromisoformat(restored)).total_seconds() <= 7*86400
    except (TypeError, ValueError):
        restore_fresh = False
    healthy = complete and restore_fresh
    write_snapshot({'generated_at': datetime.now(BUSINESS_TZ).isoformat(),
                    'status': 'healthy' if healthy else 'degraded' if complete else 'running' if value['state'] in ('running', 'submitting') else 'failed',
                    'backup_id': value['id'], 'scope': value['database'],
                    'last_backup_at': value.get('completed_at') or previous.get('last_backup_at'),
                    'last_restore_verified_at': restored,
                    'host_copy_verified': complete or bool(previous.get('host_copy_verified')), 'error': value.get('error'),
                    'reason': 'archive_and_weekly_restore_verified' if healthy else 'isolated_restore_drill_required' if complete else value['state']},
                   path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('inspect', 'backup', 'verify', 'retention-plan', 'prune', 'reconcile'), default='inspect')
    parser.add_argument('--root', type=Path, default=Path('F:/Stock/clickhouse_backup/native'))
    parser.add_argument('--database', default='stock')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--backup-id')
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--scheduled', action='store_true')
    args = parser.parse_args()
    if args.mode == 'inspect':
        print(json.dumps({'root': str(args.root), 'batches': len(manifests(args.root)),
                          'retention': 'two restored weekly full groups plus their increments; legacy files untouched'}))
        return 0
    if args.mode == 'retention-plan':
        print(json.dumps(retention_plan(args.root, database=args.database)))
        return 0
    if args.mode == 'verify':
        if not args.archive:
            parser.error('--archive required')
        print(json.dumps(verify_archive(args.archive)))
        return 0
    lock = InstanceLock(runtime_path('operations', 'backups', 'backup.lock'))
    lock.acquire()
    try:
        publisher = lambda value: publish(value, root=args.root)
        if args.mode == 'prune':
            print(json.dumps(prune_verified_groups(args.root, args.database)))
            return 0
        if args.mode == 'reconcile':
            if not args.backup_id:
                parser.error('--backup-id required')
            result = reconcile(clickhouse_client(), args.root, args.backup_id, publisher)
        else:
            now = datetime.now(BUSINESS_TZ)
            from services.operations.qmt_download_queue import protected_session
            if protected_session(now):
                raise RuntimeError('Bulk backup start is deferred during the protected realtime session')
            if args.scheduled and not (6.5 <= now.hour + now.minute/60 < 8):
                raise RuntimeError('Scheduled backup missed its 06:30-08:00 start window')
            existing = [v for v in manifests(args.root) if v.get('database') == args.database
                        and v.get('state') == 'archive_verified' and v.get('created_at', '').startswith(now.date().isoformat())]
            with ExitStack() as held:
                # Do not start against an active rebuild or SDK history download.
                for path in (runtime_path('operations','derived_recovery.lock'),
                             runtime_path('operations','qmt_downloads','download.lock')):
                    guard = InstanceLock(path)
                    guard.acquire()
                    held.callback(guard.release)
                result = max(existing, key=lambda v: v['created_at']) if args.scheduled and existing else backup(
                    clickhouse_client(), args.root, args.database, publisher, full=args.full)
                if args.scheduled and not result.get('base_id') and not result.get('restore_verified_at'):
                    subprocess.run([sys.executable, str(ROOT/'scripts/verify_clickhouse_backup.py'),
                                    '--root', str(args.root), '--backup-id', result['id'], '--publish-health'],
                                   check=True, timeout=9000,
                                   env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'},
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
                    result = read_manifest(args.root / result['id'] / 'manifest.json')
                    prune_verified_groups(args.root, args.database)
                publisher(result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        path = runtime_path('operations','backups','latest.json')
        previous = read_manifest(path)
        write_snapshot({**previous, 'generated_at':datetime.now(BUSINESS_TZ).isoformat(),
                        'status':'failed','scope':args.database,'error':str(exc)[:2000],
                        'reason':'backup_or_restore_failed'},path)
        raise
    finally:
        lock.release()


if __name__ == '__main__':
    raise SystemExit(main())
