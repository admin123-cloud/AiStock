"""Restore native archives on a separate, resource-limited Docker instance."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import zipfile
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import clickhouse_connect
from services.operations.backup import backup, backup_key, identifier, read_manifest, sha256, verify_archive
from services.operations.health import BUSINESS_TZ, write_snapshot
from utils.paths import artifacts_root

DOCKER = r'C:\Program Files\Docker\Docker\resources\bin\docker.exe'


def docker(*args):
    return subprocess.run([DOCKER, *args], check=True, capture_output=True, text=True,
                          encoding='utf-8', timeout=120,
                          creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0).stdout.strip()


def start_server(name, root):
    docker('run', '-d', '--name', name, '--label', 'aistock.backup.drill=true',
           '--cpus', '0.5', '--memory', '1g', '--memory-swap', '1g',
           '-p', '127.0.0.1::8123', '-e', 'CLICKHOUSE_SKIP_USER_SETUP=1',
           '--mount', f'type=bind,source={root},target=/backups',
           '--mount', f'type=bind,source={ROOT / "config/clickhouse/config.d/aistock-backups.xml"},target=/etc/clickhouse-server/config.d/aistock-backups.xml,readonly',
           'clickhouse/clickhouse-server:24.8')
    binding = docker('port', name, '8123/tcp')
    port = int(binding.rsplit(':', 1)[1])
    for _ in range(30):
        try:
            return clickhouse_connect.get_client(host='127.0.0.1', port=port, database='default',
                                                connect_timeout=2, send_receive_timeout=630, query_retries=0)
        except Exception:
            time.sleep(1)
    raise RuntimeError('Isolated backup server did not become ready')


def remove_owned_server(name):
    label = docker('inspect', '--format', '{{ index .Config.Labels "aistock.backup.drill" }}', name)
    if not name.startswith('aistock-backup-drill-') or label != 'true':
        raise RuntimeError('Refusing cleanup of a container not owned by this drill')
    docker('rm', '-f', '-v', name)


def restore(root, key, client):
    backup_key(key)
    manifest_path = root / key / 'manifest.json'
    manifest = read_manifest(manifest_path)
    if manifest.get('state') != 'archive_verified' or manifest.get('id') != key:
        raise ValueError('A verified manifest is required')
    # Check the complete direct-base dependency before native restore reads anything.
    dependencies = [manifest]
    if manifest.get('base_id'):
        base_id = backup_key(manifest['base_id'])
        base = read_manifest(root / base_id / 'manifest.json')
        if base.get('id') != base_id or base.get('database') != manifest.get('database') or base.get('base_id'):
            raise ValueError('Base backup identity or direct-full dependency mismatch')
        dependencies.append(base)
    for item in dependencies:
        archive = root / item['id'] / 'backup.zip'
        if sha256(archive) != item['archive']['sha256']:
            raise ValueError('Backup or base archive hash mismatch')
    database = manifest['database']
    with zipfile.ZipFile(root / key / 'backup.zip') as archive:
        logical_files = [node.text or '' for node in ElementTree.fromstring(archive.read('.backup')).findall('./contents/file/name')]
        expected = {Path(n).stem for n in logical_files
                    if n.startswith(f'metadata/{database}/') and n.endswith('.sql')}
    # Source database is absent in this isolated instance. No production connection is accepted.
    if client.command(f'EXISTS DATABASE {identifier(database)}'):
        raise RuntimeError('Restore destination is not empty')
    operation = str(uuid.uuid4())
    client.command(f"RESTORE DATABASE {identifier(database)} FROM Disk('backups', '{key}/backup.zip') "
                   f"SETTINGS id='{operation}' ASYNC")
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        rows = client.query('SELECT status,error FROM system.backups WHERE id={id:String}',
                            parameters={'id': operation}).result_rows
        if rows and rows[0][0] == 'RESTORED':
            break
        if rows and rows[0][0] == 'RESTORE_FAILED':
            raise RuntimeError(rows[0][1])
        time.sleep(2)
    else:
        raise TimeoutError('Restore drill timed out; isolated container is retained')
    tables = client.query('SELECT name,engine FROM system.tables WHERE database={db:String}',
                          parameters={'db': database}).result_rows
    actual = {r[0] for r in tables}
    if expected != actual:
        raise RuntimeError(f'Restored table set mismatch: missing={expected-actual}, extra={actual-expected}')
    checks = []
    for table, engine in tables:
        if 'MergeTree' in engine or engine in ('Log', 'TinyLog', 'StripeLog'):
            result = client.query(f'CHECK TABLE {identifier(database)}.{identifier(table)}',
                                  settings={'max_execution_time': 600, 'max_threads': 1}).result_rows
            if not result or any(row[0] != 1 for row in result):
                raise RuntimeError(f'Restored table integrity check failed: {table}')
            count = client.command(f'SELECT count() FROM {identifier(database)}.{identifier(table)}')
            checks.append({'table': table, 'rows': count, 'check': 'passed'})
    manifest.update(restore_checks=checks)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=artifacts_root() / 'backup-drill/native')
    parser.add_argument('--backup-id')
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--publish-health', action='store_true')
    args = parser.parse_args()
    if not args.probe and not args.backup_id:
        parser.error('--probe or --backup-id required')
    args.root.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex[:10]
    names, clients = [], []
    succeeded = False
    try:
        if args.probe:
            name = 'aistock-backup-drill-source-' + suffix
            names.append(name)
            client = start_server(name, args.root.resolve())
            clients.append(client)
            client.command('CREATE DATABASE backup_probe')
            client.command('CREATE TABLE backup_probe.probe (id UInt64, value String) ENGINE=MergeTree ORDER BY id')
            client.command("INSERT INTO backup_probe.probe SELECT number,concat('value-',toString(number)) FROM numbers(1000)")
            result = backup(client, args.root, 'backup_probe', lambda value: None, full=True)
            client.command("INSERT INTO backup_probe.probe SELECT number+1000,concat('value-',toString(number+1000)) FROM numbers(500)")
            result = backup(client, args.root, 'backup_probe', lambda value: None)
            if not result['base_id']:
                raise RuntimeError('Incremental probe did not select the just-created full backup')
            args.backup_id = result['id']
            client.close()
            clients.remove(client)
            docker('stop', name)
        name = 'aistock-backup-drill-restore-' + suffix
        names.append(name)
        client = start_server(name, args.root.resolve())
        clients.append(client)
        result = restore(args.root, args.backup_id, client)
        if args.probe:
            bad = client.command("SELECT countIf(value != concat('value-',toString(id))) FROM backup_probe.probe")
            count = client.command('SELECT count() FROM backup_probe.probe')
            if bad or count != 1500:
                raise RuntimeError('Probe content differs from the original 1500 deterministic rows')
        result['restore_verified_at'] = datetime.now(BUSINESS_TZ).isoformat()
        write_snapshot(result, args.root / args.backup_id / 'manifest.json')
        if args.publish_health and not args.probe:
            from scripts.manage_clickhouse_backup import publish
            publish(result, root=args.root)
        succeeded = True
        print(json.dumps({'id': args.backup_id, 'restore_verified_at': result['restore_verified_at'],
                          'checks': result['restore_checks']}, ensure_ascii=False))
    finally:
        for client in clients:
            client.close()
        for name in names:
            if succeeded:
                remove_owned_server(name)
            else:
                print(f'Drill failed; retained isolated container for inspection: {name}', file=sys.stderr)


if __name__ == '__main__':
    main()
