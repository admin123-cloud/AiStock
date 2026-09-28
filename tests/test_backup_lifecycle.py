from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from services.operations import backup as module


def manifest(root, key, **extra):
    path = root / key / 'manifest.json'
    path.parent.mkdir(parents=True)
    value = {'id': key, 'database': 'stock', 'created_at': '2026-09-09T03:00:00+08:00',
             'state': 'archive_verified', 'base_id': None, **extra}
    path.write_text(json.dumps(value))
    return value


def test_archive_requires_native_metadata_and_crc(tmp_path):
    path = tmp_path / 'backup.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('metadata/stock/a.sql', 'CREATE TABLE a')
    with pytest.raises(ValueError, match='metadata missing'):
        module.verify_archive(path)
    with zipfile.ZipFile(path, 'a') as z:
        z.writestr('.backup', '<backup/>')
    result = module.verify_archive(path)
    assert result['sha256'] == module.sha256(path) and result['bytes'] > 0


def test_retention_requires_two_restored_full_groups(tmp_path):
    old = '20260801_030000_00000001'
    manifest(tmp_path, old, created_at='2026-08-01T03:00:00+08:00')
    manifest(tmp_path, '20260802_030000_00000002', base_id=old)
    first = manifest(tmp_path, '20260901_030000_00000003', restore_verified_at='2026-09-01')
    assert module.retention_plan(tmp_path) == []
    manifest(tmp_path, '20260908_030000_00000004', restore_verified_at='2026-09-09')
    assert module.retention_plan(tmp_path) == [[old, '20260802_030000_00000002']]
    assert (tmp_path / old / 'manifest.json').exists()


def test_unresolved_server_outcome_prevents_another_backup(tmp_path):
    manifest(tmp_path, '20260909_030000_00000001', state='outcome_uncertain')
    with pytest.raises(RuntimeError, match='unresolved server outcome'):
        module.backup(object(), tmp_path, 'stock', lambda value: None)


def test_reconcile_does_not_trust_archive_presence_without_server_completion(tmp_path):
    key = '20260909_030000_00000001'
    manifest(tmp_path, key, state='outcome_uncertain', operation_id='test')
    client = SimpleNamespace(query=lambda *a, **k: SimpleNamespace(result_rows=[]))
    with pytest.raises(RuntimeError, match='Server completion is unavailable'):
        module.reconcile(client, tmp_path, key, lambda value: None)
    assert module.read_manifest(tmp_path / key / 'manifest.json')['state'] == 'outcome_uncertain'


def test_manifest_path_and_sql_identity_are_validated(tmp_path):
    manifest(tmp_path, 'valid', id='../another')
    assert module.manifests(tmp_path) == []
    with pytest.raises(ValueError):
        module.identifier('stock; DROP DATABASE stock')


def test_restore_evidence_from_other_database_cannot_prune_stock(tmp_path):
    manifest(tmp_path, '20260801_030000_00000001', created_at='2026-08-01T03:00:00+08:00')
    manifest(tmp_path, '20260901_030000_00000002', database='probe', restore_verified_at='2026-09-01')
    manifest(tmp_path, '20260908_030000_00000003', database='probe', restore_verified_at='2026-09-08')
    assert module.retention_plan(tmp_path) == []


def test_running_backup_preserves_last_good_evidence(tmp_path, monkeypatch):
    from scripts import manage_clickhouse_backup as cli
    path = tmp_path / 'latest.json'
    path.write_text(json.dumps({'last_backup_at':'2026-09-08T07:30:00+08:00',
                               'host_copy_verified':True,'last_restore_verified_at':'2026-09-07T08:00:00+08:00'}))
    monkeypatch.setattr(cli, 'runtime_path', lambda *parts: path)
    cli.publish({'id':'20260909_070000_00000001','database':'stock','state':'running'})
    value = module.read_manifest(path)
    assert value['status'] == 'running'
    assert value['last_backup_at'] == '2026-09-08T07:30:00+08:00'
    assert value['host_copy_verified']


def test_pruning_keeps_legacy_and_two_restored_dependency_groups(tmp_path):
    legacy = tmp_path / 'legacy.tar.gz'
    legacy.write_bytes(b'old backup untouched')
    old = '20260801_030000_00000001'
    manifest(tmp_path, old, created_at='2026-08-01T03:00:00+08:00')
    for key in ('20260901_030000_00000002', '20260908_030000_00000003'):
        value = manifest(tmp_path, key, restore_verified_at='2026-09-09')
        archive = tmp_path / key / 'backup.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('.backup', '<backup/>')
        value['archive'] = module.verify_archive(archive)
        (archive.parent / 'manifest.json').write_text(json.dumps(value))
    assert module.prune_verified_groups(tmp_path) == [[old]]
    assert legacy.read_bytes() == b'old backup untouched'
    assert not (tmp_path / old).exists()
    assert len(module.manifests(tmp_path)) == 2
