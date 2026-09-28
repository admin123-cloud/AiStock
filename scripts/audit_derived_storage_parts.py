"""Record physical ClickHouse corruption evidence without reading or changing data tables."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.operations.health import BUSINESS_TZ, write_snapshot
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


def part_rows(client, table):
    rows = client.query(
        "SELECT name, partition, rows, bytes_on_disk, modification_time, active "
        "FROM system.parts WHERE database = currentDatabase() "
        f"AND table = '{table}' ORDER BY active DESC, modification_time DESC",
        settings={'max_threads': 1, 'max_memory_usage': 200_000_000},
    ).result_rows
    return [dict(name=str(row[0]), partition=str(row[1]), rows=int(row[2]),
                 bytes_on_disk=int(row[3]), modification_time=str(row[4]), active=bool(row[5]))
            for row in rows]


def checksum_failures(client, table):
    rows = client.query(
        "SELECT event_time, query, exception "
        "FROM system.query_log WHERE type = 'ExceptionWhileProcessing' "
        f"AND has(tables, 'stock.{table}') AND exception_code = 40 "
        "ORDER BY event_time DESC LIMIT 50",
        settings={'max_threads': 1, 'max_memory_usage': 200_000_000},
    ).result_rows
    result = []
    for occurred_at, query, exception in rows:
        text = str(exception)
        if 'Checksum doesn' not in text:
            continue
        result.append({
            'occurred_at': str(occurred_at),
            'query': str(query)[:2000],
            'exception': text[:2000],
        })
    return result


def report(table, parts, failures):
    return {
        'generated_at': datetime.now(BUSINESS_TZ).isoformat(),
        'table': f'stock.{table}',
        'active_parts': parts,
        'checksum_failures': failures,
        'state': 'physical_corruption_evidence_recorded' if failures else 'no_checksum_failure_in_query_log',
        'scope_limit': (
            'system.parts does not expose row datetime bounds for this non-partitioned legacy table. '
            'The affected row range is therefore unknown; do not infer it from part names or repair by deleting rows.'
        ),
        'allowed_next_step': 'build and independently validate a replacement candidate; preserve the old table until promotion is explicitly authorized',
        'write_authority': 'read_only_audit',
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--period', type=int, choices=(15, 30, 60), default=60)
    parser.add_argument('--output', default='')
    args = parser.parse_args()
    table = f'kline_minute_{args.period}'
    output = Path(args.output) if args.output else runtime_path(
        'operations', 'derived_recovery', 'storage_audits',
        f'{table}_{datetime.now(BUSINESS_TZ):%Y%m%d_%H%M%S}.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    try:
        payload = report(table, part_rows(client, table), checksum_failures(client, table))
        write_snapshot(payload, output)
        print(json.dumps({'output': str(output), 'state': payload['state'],
                          'parts': len(payload['active_parts']),
                          'failures': len(payload['checksum_failures'])}, ensure_ascii=False))
    finally:
        client.close()


if __name__ == '__main__':
    main()
