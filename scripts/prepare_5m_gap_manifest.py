"""Prepare a QMT repair manifest from the frozen 5m daily-count evidence."""
from __future__ import annotations
import argparse, gzip, json, math, sys
from collections import Counter
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client


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
    anomaly_dates = []
    for entry in baseline['months']:
        with gzip.open(Path(entry['daily_file']), 'rt', encoding='utf-8') as handle:
            anomaly_dates.extend(line.split('\t', 1)[0] for line in handle if int(line.rstrip('\n').split('\t')[2]) != 48)
    dates_sql = ','.join("'" + d.replace("'", "''") + "'" for d in sorted(set(anomaly_dates))) or "'1900-01-01'"
    daily = {(r[0], str(r[1])): r[2:] for r in client.query(
        f'SELECT code, trade_date, volume, amount, open, high, low, close FROM kline_daily FINAL WHERE trade_date IN ({dates_sql})'
    ).result_rows}
    statuses = client.query(
        "SELECT code, start_date, end_date, status FROM kline_daily_market_status_audit FINAL "
        "WHERE review_state = 'verified' AND status IN ('suspended', 'delisted', 'not_trading')"
    ).result_rows
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
                    for status_code, start, end, status in statuses:
                        if status_code == code and str(start) <= trade_day <= str(end):
                            reason = f'status:{status}'
                            break
                daily_row = daily.get((code, trade_day))
                if reason is None and daily_row:
                    volume, amount = daily_row[:2]
                    no_trade = lambda value: value is None or (isinstance(value, float) and (math.isnan(value) or not math.isfinite(value))) or float(value) <= 0
                    if no_trade(volume) and no_trade(amount):
                        reason = 'daily_no_trade_evidence'
                if reason is not None:
                    categories[reason] += 1
                    continue
                gaps.append({'code': code, 'trade_date': trade_day,
                             'missing_count': max(0, 48 - count), 'observed_count': count})
                categories['qmt_repair_candidate'] += 1
    payload = {
        'generated_from': str(args.baseline),
        'baseline_run_id': baseline['run_id'],
        'baseline_fingerprint': baseline['fingerprint'],
        'scope': 'partial_bucket_only; unresolved active stock code-days only',
        'gaps': gaps,
        'categories': dict(categories),
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
