"""Publish a current QMT online sector/member snapshot without changing market tables."""
from datetime import datetime
import argparse
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from services.operations.health import write_snapshot
from utils.paths import runtime_path
from services.operations.reference_universe import current_stock_universe, scoped_members, QMT_CURRENT_STOCKS


def collect(client, *, now=None, progress=None, registered_codes=None):
    from scripts.sync_sectors_and_mapping import _classify_qmt_sector
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    progress = progress or (lambda phase, **detail: None)
    sdk = client._ensure_connected()
    # Local-file fallback is intentionally not evidence of a successful current online refresh.
    progress('refresh_sector_catalog')
    sdk.download_sector_data()
    progress('read_sector_catalog')
    names = sorted({str(n).strip() for n in client.get_sector_list() if str(n).strip()})
    names = [n for n in names if _classify_qmt_sector(n)[0] == 'industry']
    if registered_codes is not None:
        expected = {c[4:] for c in registered_codes if c.startswith('qmt:')}
        if not expected or not expected.issubset(set(names)):
            raise RuntimeError('Registered QMT catalog is absent from the refreshed online catalog')
        names = sorted(expected)
    if not names:
        raise RuntimeError('QMT refreshed sector list is empty')
    active = current_stock_universe(client)
    members, errors, excluded_members = {}, [], {}
    for index, name in enumerate(names):
        progress('read_members', completed=index, total=len(names), sector=name)
        codes = client.get_stock_list_in_sector(name)
        try:
            clean, excluded = scoped_members(codes, active)
            members[f'qmt:{name}'] = clean
            if excluded:
                excluded_members[f'qmt:{name}'] = excluded
        except RuntimeError as exc:
            errors.append(f'qmt:{name}: {exc}')
            members[f'qmt:{name}'] = []
    return {'source': 'qmt', 'verified': not errors, 'generated_at': now.isoformat(),
            'effective_from': now.date().isoformat(), 'codes': sorted(members), 'members': members,
            'exemptions': [], 'errors': errors,
            'excluded_members':excluded_members,
            'membership_scope':{'source':'qmt:'+QMT_CURRENT_STOCKS,'count':len(active),
                                'excluded_reason':'not_in_current_qmt_stock_universe'},
            'scope': 'registered_qmt_industry_sectors' if registered_codes is not None else 'current_qmt_online_industry_sectors',
            'history_policy': 'Current membership cannot be used to backfill earlier dates'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=runtime_path('operations', 'sector_universe.json'))
    args = parser.parse_args()
    def progress(phase, **detail):
        write_snapshot({'generated_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                        'phase':phase, **detail}, args.output.with_suffix('.progress.json'))
    try:
        from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
        client = QmtMiniMarketClient()
        progress('connect')
        client.connect()
        from utils.market_warehouse import clickhouse_client
        registered = [str(row[0]) for row in clickhouse_client().query(
            "SELECT code FROM sectors WHERE startsWith(code,'qmt:') AND type='industry'").result_rows]
        snapshot = collect(client, progress=progress, registered_codes=registered)
    except Exception as exc:
        snapshot = {'source': 'qmt', 'verified': False, 'generated_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                    'error': f'{type(exc).__name__}: {exc}', 'codes': [], 'members': {}}
    write_snapshot(snapshot, args.output)
    progress('complete' if snapshot['verified'] else 'unverified')
    return 0 if snapshot['verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
