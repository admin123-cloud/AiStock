"""Read-only acceptance of source coverage and collector persistence receipts."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.qmt_fullpush_intraday_aggregator import load_codes, parse_tick_time, SH_TZ
from services.operations.ingestion_store import clickhouse_query_df
from services.operations.intraday_coverage import evaluate_coverage
from services.operations.qmt_snapshot import read_full_snapshot
from utils.paths import runtime_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--read-source', action='store_true', help='Read full_tick only; no history download or market writes')
    parser.add_argument('--report', type=Path, default=runtime_path('operations', 'intraday_acceptance.json'))
    args = parser.parse_args()
    live = runtime_path('operations', 'intraday_ingestion.json')
    receipt = json.loads(live.read_text(encoding='utf-8')) if live.exists() else {}
    now = datetime.now(SH_TZ)
    payload = {'generated_at': now.isoformat(), 'collector_receipt': receipt, 'market_tables_written': False}
    generated = receipt.get('generated_at')
    try:
        fresh_receipt = 0 <= (now-datetime.fromisoformat(generated)).total_seconds() <= 30
    except (TypeError, ValueError):
        fresh_receipt = False
    lane = next((row for row in receipt.get('lanes', []) if row.get('name') == 'snapshot'), {})
    result = lane.get('result', {})
    payload['acceptance'] = 'passed' if (fresh_receipt and receipt.get('phase') == 'running'
        and lane.get('status') == 'complete' and result.get('coverage', {}).get('slo_300s') == 'passed'
        and result.get('interval_slo_300s') == 'passed' and result.get('end_to_end_seconds', 301) <= 300) else 'not_verified'
    if args.read_source:
        codes = load_codes('stock,index', '', 0, False, '')
        frame = clickhouse_query_df('SELECT code, type, name FROM stocks FINAL')
        metadata = {row['code']: row for row in frame.to_dict('records')}
        started = time.monotonic()
        source = read_full_snapshot(codes)
        source_now = datetime.now(SH_TZ).replace(tzinfo=None)
        payload['source_observation'] = {
            'seconds': round(time.monotonic()-started, 3), 'requested': len(codes),
            'returned': len(source['ticks']), 'failed_batches': source.get('failed_batches'),
            'coverage': evaluate_coverage(set(codes), source['ticks'], set(), now=source_now,
                                          parse_time=parse_tick_time, metadata=metadata),
            'missing': [{**metadata.get(code, {'code': code, 'type': 'unknown'}),
                         'classification': 'qmt_no_response_requires_evidence'}
                        for code in codes if code not in source['ticks']],
            'scope': 'read-only source observation; does not certify persistence or trading-peak SLO'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes(json.dumps(payload, ensure_ascii=False, default=str, indent=2).encode('utf-8'))
    print(json.dumps({'report': str(args.report), 'acceptance': payload['acceptance'],
                      'source_observation': {k: v for k, v in payload.get('source_observation', {}).items()
                                             if k in ('seconds', 'requested', 'returned', 'failed_batches')}}, ensure_ascii=False))
    return 0 if payload['acceptance'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
