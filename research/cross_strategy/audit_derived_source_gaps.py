"""Write a read-only manifest for incomplete 5m-derived buckets and a post-cutoff tail."""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rebuild_derived_history import aggregate_range
from services.operations.health import BUSINESS_TZ, write_snapshot
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


def missing_buckets(client, start, end, period):
    sql = aggregate_range(period, start, end)
    rows = client.query(
        "SELECT code, toDate(datetime), formatDateTime(datetime, '%H:%i'), source_bars "
        f"FROM ({sql}) WHERE source_bars != {period // 5} ORDER BY 2, 1, 3",
        settings={'max_threads': 1, 'max_memory_usage': 2_000_000_000,
                  'max_bytes_before_external_group_by': 500_000_000}).result_rows
    return [{'code': str(r[0]), 'trade_date': str(r[1]), 'bucket_time': str(r[2]),
             'source_bars': int(r[3]), 'expected_bars': period // 5} for r in rows]


def tail_summary(client, cutoff):
    rows = []
    for period in (5, 15, 30, 60):
        table = f'kline_minute_{period}'
        r = client.query(
            f"SELECT count(), uniqExact(tuple(code, datetime)), min(datetime), max(datetime) "
            f"FROM {table} WHERE toDate(datetime) > toDate('{cutoff}')",
            settings={'max_threads': 1, 'max_memory_usage': 1_000_000_000}).result_rows[0]
        rows.append({'period': period, 'rows': int(r[0]), 'unique_keys': int(r[1]),
                     'min_datetime': str(r[2]) if r[2] else None,
                     'max_datetime': str(r[3]) if r[3] else None})
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start', required=True); p.add_argument('--end', required=True)
    p.add_argument('--cutoff', default='2026-09-08'); p.add_argument('--period', type=int, choices=(15, 30, 60), default=15)
    p.add_argument('--output', default='')
    a = p.parse_args(); start, end, cutoff = date.fromisoformat(a.start), date.fromisoformat(a.end), date.fromisoformat(a.cutoff)
    if start > end: raise SystemExit('--start must not be after --end')
    out = Path(a.output) if a.output else runtime_path('operations', 'derived_recovery', 'gap_audits',
                                                        f'{datetime.now():%Y%m%d_%H%M%S}.json')
    out.parent.mkdir(parents=True, exist_ok=True); c = clickhouse_client()
    try:
        gaps = missing_buckets(c, start, end, a.period)
        payload = {'generated_at': datetime.now(BUSINESS_TZ).isoformat(), 'range': [str(start), str(end)],
                   'period': a.period, 'gaps': gaps, 'gap_count': len(gaps),
                   'tail_after_cutoff': tail_summary(c, cutoff.isoformat()),
                   'scope': 'partial_bucket_only; does not enumerate wholly absent code-days or buckets',
                   'write_authority': 'read_only_manifest'}
        write_snapshot(payload, out)
        print(out); print(json.dumps({'gap_count': len(gaps), 'tail': payload['tail_after_cutoff']}))
    finally: c.close()

if __name__ == '__main__': main()
