from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from scripts.publish_qmt_sector_universe import collect


class Client:
    def _ensure_connected(self):
        return self

    def download_sector_data(self):
        pass

    def get_sector_list(self):
        return ['沪深A股', 'SW2电池']

    def get_stock_list_in_sector(self, name):
        if name == '沪深京A股':
            return [f'{i:06d}.SZ' for i in range(1000)] + ['002245.SZ']
        return ['002245.SZ']


def test_online_members_have_current_effective_date():
    now = datetime(2026, 9, 9, 18, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
    result = collect(Client(), now=now)
    assert result['verified']
    assert result['codes'] == ['qmt:SW2电池']
    assert result['members'] == {'qmt:SW2电池': ['002245.SZ']}
    assert result['effective_from'] == '2026-09-09'


def test_failed_refresh_cannot_be_verified_from_cached_list():
    client = Client()
    client.download_sector_data = lambda: (_ for _ in ()).throw(RuntimeError('offline'))
    with pytest.raises(RuntimeError, match='offline'):
        collect(client)


def test_empty_members_block_verified_denominator():
    client = Client()
    client.get_stock_list_in_sector = lambda name: Client().get_stock_list_in_sector(name) if name == '沪深京A股' else []
    result = collect(client)
    assert not result['verified']
    assert result['errors']


def test_non_current_qmt_members_are_explained_without_guessing_delist_dates():
    client = Client()
    client.get_stock_list_in_sector = lambda name: Client().get_stock_list_in_sector(name) if name == '沪深京A股' else ['002245.SZ','999991.SZ']
    result = collect(client)
    assert result['verified'] and result['members'] == {'qmt:SW2电池':['002245.SZ']}
    assert result['excluded_members'] == {'qmt:SW2电池':['999991.SZ']}
    assert result['exemptions'] == []


def test_registered_taxonomy_is_not_expanded_by_online_catalog():
    client = Client()
    client.get_sector_list = lambda: ['SW2电池', 'SW2煤炭', '沪深A股']
    result = collect(client, registered_codes=['qmt:SW2电池'])
    assert result['verified'] and result['codes'] == ['qmt:SW2电池']
    with pytest.raises(RuntimeError):
        collect(client, registered_codes=['qmt:missing'])


def test_late_reference_trigger_keeps_due_date():
    from scripts.run_reference_maintenance import due_date
    tz = ZoneInfo('Asia/Shanghai')
    assert due_date(datetime(2026, 9, 9, 19, 30, tzinfo=tz)) == '2026-09-09'
    assert due_date(datetime(2026, 9, 10, 0, 5, tzinfo=tz)) == '2026-09-09'
    assert due_date(datetime(2026, 9, 14, 0, 5, tzinfo=tz)) == '2026-09-11'


def test_registered_membership_failure_precedes_both_live_table_swaps():
    from scripts.sync_sectors_and_mapping import SectorSyncer
    syncer = SectorSyncer.__new__(SectorSyncer)
    syncer.registered_only = syncer.pure_qmt = syncer.filter_to_universe = True
    syncer.include_all_qmt_sectors = syncer.include_universe_sectors = False
    syncer._ensure_sector_tables = lambda: None
    syncer._fetch_qmt_sectors = lambda: [{'code':'qmt:SW2电池','qmt_name':'SW2电池'}]
    syncer._official_universe_codes = lambda: {'002245.SZ'}
    syncer.stats = {}
    syncer._qmt_client = lambda: SimpleNamespace(get_stock_list_in_sector=lambda name: Client().get_stock_list_in_sector(name) if name == '沪深京A股' else [])
    called = []
    syncer._atomic_replace_sectors = lambda rows: called.append('sectors')
    syncer._atomic_replace_sector_mappings = lambda rows: called.append('members')
    with pytest.raises(RuntimeError, match='Empty current'):
        syncer.sync_all_sectors()
    assert called == []


def test_host_reference_owner_prevents_api_startup_refresh(monkeypatch):
    import ast
    import os
    from pathlib import Path
    tree = ast.parse((Path(__file__).parents[1]/'api/system_config.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='maybe_run_startup_reference_sync')
    called = []
    scope = {'os':os,'logger':SimpleNamespace(info=lambda *a:None),
             'get_startup_reference_sync_enabled':lambda:True,
             '_kickoff_core_maintenance_bootstrap_refresh':lambda:called.append(True)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<startup-owner>','exec'),scope)
    monkeypatch.setenv('AISTOCK_REFERENCE_DATA_OWNER','host')
    assert scope['maybe_run_startup_reference_sync']() is False
    assert called == []
