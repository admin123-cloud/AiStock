"""Prepare a QMT repair manifest from the frozen 5m daily-count evidence."""
from __future__ import annotations
import argparse, gzip, json, sys
from collections import Counter
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.market_status import load_status_index, partition_status_keys, find_status_trade_conflicts


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
    client = clickhouse_client()
    stocks = {r[0]: r[1:] for r in client.query(
        'SELECT code, market, type, listing_status, list_date, delist_date, quit FROM stocks FINAL'
    ).result_rows}
    anomaly_keys = set()
    for entry in baseline['months']:
        with gzip.open(Path(entry['daily_file']), 'rt', encoding='utf-8') as handle:
            for line in handle:
                day, code, count = line.rstrip('\n').split('\t')
                if int(count) != 48:
                    anomaly_keys.add((code, day))
    status_index = load_status_index(client)
    status_plan = partition_status_keys(anomaly_keys, status_index,
        find_status_trade_conflicts(client, anomaly_keys, status_index))
    gaps, categories = [], Counter()
    for entry in baseline['months']:
        path = Path(entry['daily_file'])
        with gzip.open(path, 'rt', encoding='utf-8') as handle:
            for line in handle:
                trade_day, code, count = line.rstrip('\n').split('\t')
                count = int(count)
                if count == 48:
                    continue
                stock = stocks.get(code)
                reason = None
                if not stock:
                    reason = 'stock_missing'
                else:
                    market, kind, listing, listed, delisted, quit_flag = stock
                    if kind != 'stock': reason = f'non_stock:{kind}'
                    elif listing != 'active': reason = f'listing:{listing}'
                    elif listed and str(listed) > trade_day: reason = 'not_listed'
                    elif delisted and str(delisted) < trade_day: reason = 'delisted'
                    elif quit_flag: reason = 'quit'
                if reason is None:
                    if (code, trade_day) in status_plan['exempt']:
                        reason = 'status:verified_business_absence'
                    elif (code, trade_day) in status_plan['conflict']:
                        categories['status_conflict_requires_notice'] += 1
                        continue
                if reason is not None:
                    categories[reason] += 1
                    continue
                gaps.append({'code': code, 'trade_date': trade_day,
                             'missing_count': max(0, 48 - count), 'observed_count': count,
                             'requires_notice_review': (code, trade_day) in status_plan['lookup']})
                categories['qmt_repair_candidate'] += 1
    payload = {
        'generated_from': str(args.baseline),
        'baseline_run_id': baseline['run_id'],
        'baseline_fingerprint': baseline['fingerprint'],
        'scope': 'partial_bucket_only; unresolved active stock code-days only',
        'gaps': gaps,
        'categories': dict(categories),
        'status_conflicts': [{'code': c, 'trade_date': d, 'requires_notice_review': True}
                             for c, d in sorted(status_plan['conflict'])],
        'code_days': len(gaps),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
    list_output = args.output.with_suffix('.tsv')
    list_output.write_text(
        'trade_date\\tcode\\tmissing_count\\tobserved_count\\n' +
        ''.join(f"{row['trade_date']}\\t{row['code']}\\t{row['missing_count']}\\t{row['observed_count']}\\n" for row in gaps),
        encoding='utf-8'
    )
    print(json.dumps({'output': str(args.output), 'list_output': str(list_output), 'code_days': len(gaps),
                      'categories': dict(categories)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
