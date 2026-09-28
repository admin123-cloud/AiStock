"""Native backups with durable manifests; legacy archives are never pruned."""
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
import zipfile

from services.operations.health import BUSINESS_TZ, write_snapshot


def identifier(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise ValueError('Invalid database identifier')
    return f'`{value}`'


def backup_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{8}_\d{6}_[a-f0-9]{8}', value):
        raise ValueError('Invalid backup ID')
    return value


def read_manifest(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        if '.backup' not in archive.namelist():
            raise ValueError('Native backup metadata missing')
        damaged = archive.testzip()
        if damaged:
            raise ValueError(f'Archive CRC failed: {damaged}')
    return {'sha256': sha256(path), 'bytes': path.stat().st_size}


def manifests(root):
    values = []
    for path in root.glob('*/manifest.json'):
        value = read_manifest(path)
        key = value.get('id', '')
        if isinstance(key, str) and re.fullmatch(r'\d{8}_\d{6}_[a-f0-9]{8}', key) and path.parent.name == key:
            values.append(value)
    return values


def reconcile(client, root, key, publish):
    backup_key(key)
    path = root / key / 'manifest.json'
    value = read_manifest(path)
    if value.get('id') != key:
        raise ValueError('Manifest identity mismatch')
    rows = client.query('SELECT status,error FROM system.backups WHERE id={id:String}',
                        parameters={'id': value['operation_id']}).result_rows
    if rows and rows[0][0] == 'BACKUP_CREATED':
        value['archive'] = verify_archive(root / key / 'backup.zip')
        value.update(state='archive_verified', completed_at=datetime.now(BUSINESS_TZ).isoformat(), error=None)
    elif rows and rows[0][0] == 'BACKUP_FAILED':
        value.update(state='failed', error=rows[0][1])
    else:
        raise RuntimeError('Server completion is unavailable; retain unresolved manifest and archive')
    write_snapshot(value, path)
    publish(value)
    return value


def choose_base(root, database, now):
    # Every daily increment depends directly on a verified weekly full, not a growing chain.
    candidates = []
    monday = (now - timedelta(days=now.weekday())).date()
    for value in manifests(root):
        try:
            created = datetime.fromisoformat(value['created_at'])
            archive = root / value['id'] / 'backup.zip'
            if (value['database'] == database and value.get('state') == 'archive_verified'
                    and not value.get('base_id') and created.date() >= monday
                    and archive.is_file()):
                candidates.append(value)
        except (KeyError, TypeError, ValueError):
            continue
    return max(candidates, key=lambda x: x['created_at']) if candidates else None


def retention_plan(root, keep_full=2, database='stock'):
    """Propose whole dependency groups only; never remove anything automatically."""
    if keep_full < 2:
        raise ValueError('Keep at least two independent full backup groups')
    items = {v['id']: v for v in manifests(root) if v.get('id') and v.get('database') == database}
    full = sorted((v for v in items.values() if not v.get('base_id') and
                   v.get('state') == 'archive_verified'), key=lambda v: v['created_at'], reverse=True)
    removable = []
    # A newer full must have passed an isolated restore drill before replacing older groups.
    if sum(bool(v.get('restore_verified_at')) for v in full[:keep_full]) < keep_full:
        return []
    for base in full[keep_full:]:
        group = [v for v in items.values() if v['id'] == base['id'] or v.get('base_id') == base['id']]
        if all(v.get('state') == 'archive_verified' for v in group):
            removable.append([v['id'] for v in group])
    return removable


def prune_verified_groups(root, database='stock'):
    root = Path(root).resolve(strict=True)
    groups = retention_plan(root, database=database)
    if not groups:
        return []
    removed_ids = {key for group in groups for key in group}
    retained = [v for v in manifests(root) if v.get('database') == database and v['id'] not in removed_ids
                and v.get('state') == 'archive_verified' and not v.get('base_id') and v.get('restore_verified_at')]
    if len(retained) < 2:
        raise RuntimeError('Two independently restored full backups must remain')
    for item in retained:
        if verify_archive(root / item['id'] / 'backup.zip')['sha256'] != item['archive']['sha256']:
            raise RuntimeError('Retained backup archive changed; pruning refused')
    paths = []
    for key in sorted(removed_ids):
        candidate = root / backup_key(key)
        if candidate.resolve(strict=True).parent != root:
            raise RuntimeError('Backup deletion target escaped managed root')
        # Reject junctions/symlinks anywhere in a managed group before recursive deletion.
        for item in [candidate, *candidate.rglob('*')]:
            if item.is_symlink() or getattr(os.lstat(item), 'st_file_attributes', 0) & 0x400:
                raise RuntimeError('Backup group contains a reparse point; pruning refused')
        paths.append(candidate)
    for path in paths:
        shutil.rmtree(path)
    write_snapshot({'generated_at':datetime.now(BUSINESS_TZ).isoformat(),
                    'database':database,'removed_groups':groups}, root/'retention_latest.json')
    return groups


def backup(client, root, database, publish, *, now=None, wait_seconds=3600, full=False):
    now = now or datetime.now(BUSINESS_TZ)
    identifier(database)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for value in manifests(root):
        if value.get('state') in ('submitting', 'running', 'outcome_uncertain'):
            raise RuntimeError('Prior backup has an unresolved server outcome; reconcile its operation ID first')
    disks = client.query("SELECT name,path FROM system.disks WHERE name='backups'").result_rows
    if disks != [('backups', '/backups/')]:
        raise RuntimeError('Dedicated backups disk /backups/ is not configured')
    byte_count = int(client.query(
        'SELECT sum(bytes_on_disk) FROM system.parts WHERE database={db:String} AND active',
        parameters={'db': database}).result_rows[0][0] or 0)
    if shutil.disk_usage(root).free < byte_count * 1.15 + 20 * 1024**3:
        raise RuntimeError('Insufficient host space for a full backup plus 20 GiB reserve')
    base = None if full else choose_base(root, database, now)
    if base and sha256(root / base['id'] / 'backup.zip') != base['archive']['sha256']:
        raise RuntimeError('Base archive changed; cannot build an incremental backup')
    key = now.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    directory = root / key
    directory.mkdir()
    manifest = dict(id=key, database=database, created_at=now.isoformat(), state='submitting',
                    operation_id=str(uuid.uuid4()), base_id=base['id'] if base else None,
                    scope='entire_database', tables_at_start=client.query(
                        'SELECT name FROM system.tables WHERE database={db:String} ORDER BY name',
                        parameters={'db': database}).result_rows,
                    source_bytes=byte_count, restore_verified_at=None)

    def save():
        write_snapshot(manifest, directory / 'manifest.json')
        publish(manifest)

    save()
    sql = (f"BACKUP DATABASE {identifier(database)} TO Disk('backups', '{key}/backup.zip') "
           f"SETTINGS id='{manifest['operation_id']}', compression_method='deflate', compression_level=1")
    if base:
        sql += f", base_backup=Disk('backups', '{base['id']}/backup.zip')"
    try:
        client.command(sql + ' ASYNC')
        manifest['state'] = 'running'
        save()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            rows = client.query('SELECT status,error FROM system.backups WHERE id={id:String}',
                                parameters={'id': manifest['operation_id']}).result_rows
            if rows and rows[0][0] == 'BACKUP_CREATED':
                manifest['archive'] = verify_archive(directory / 'backup.zip')
                manifest.update(state='archive_verified', completed_at=datetime.now(BUSINESS_TZ).isoformat())
                save()
                return manifest
            if rows and rows[0][0] == 'BACKUP_FAILED':
                manifest.update(state='failed', error=rows[0][1])
                save()
                raise RuntimeError(rows[0][1])
            time.sleep(2)
        raise TimeoutError('Backup still running after wait budget; inspect operation ID before retry')
    except BaseException as exc:
        if manifest['state'] not in ('failed', 'archive_verified'):
            manifest.update(state='outcome_uncertain', error=str(exc)[:2000])
            save()
        raise
