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
    client.get_stock_list_in_sector = lambda name: []
    result = collect(client)
    assert not result['verified']
    assert result['errors']


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
