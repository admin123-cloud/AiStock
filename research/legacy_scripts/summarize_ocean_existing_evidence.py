from __future__ import annotations

"""Stream existing Ocean per-entry evidence into a read-only segment summary."""

import argparse
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--segment-root', type=Path, required=True)
    p.add_argument('--output-root', type=Path, required=True)
    p.add_argument('--years', nargs='+', type=int, required=True)
    args = p.parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)
    result = {'schema': 'ocean-existing-evidence-summary-v1', 'read_only': True, 'formal_authorized': False, 'years': []}
    for year in args.years:
        root = args.segment_root / f'stage_{year}'
        counters = Counter()
        reason_counts = Counter()
        market_counts = Counter()
        source_rows = accepted_rows = invalid_rows = quantity_invalid_rows = 0
        duplicate_rows = 0
        missing_daily = 0
        price_review = volume_review = amount_review = 0
        bars_hist = Counter()
        audit_files = 0
        audit_rows = 0
        for path in root.rglob('day_audit.jsonl.gz'):
            audit_files += 1
            with gzip.open(path, 'rt', encoding='utf-8') as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    audit_rows += 1
                    status = str(row.get('status') or 'missing_status')
                    counters[status] += 1
                    reason = str(row.get('reason') or '')
                    if reason:
                        for token in reason.split('; '):
                            reason_counts[token] += 1
                    code = str(row.get('code') or '')
                    market_counts[code[-2:] if len(code) >= 2 else 'UNKNOWN'] += 1
                    source_rows += int(row.get('source_row_count') or 0)
                    accepted_rows += int(row.get('accepted_row_count') or 0)
                    invalid_rows += int(row.get('invalid_row_count') or 0)
                    quantity_invalid_rows += int(row.get('quantity_invalid_row_count') or 0)
                    duplicate_rows += int(row.get('duplicate_datetime_rows') or 0)
                    bars_hist[str(row.get('accepted_row_count'))] += 1
                    # Existing per-entry stage evidence is structure-only.  Do
                    # not reinterpret absent reconciliation fields as daily
                    # failures; mark those dimensions as not checked below.
                    if 'daily_present' in row and not row.get('daily_present', False):
                        missing_daily += 1
                    if 'price_relative_diff' in row and (row.get('price_relative_diff') is None or float(row.get('price_relative_diff') or 0) > 0.02):
                        price_review += 1
                    if 'volume_relative_diff' in row and (row.get('volume_relative_diff') is None or float(row.get('volume_relative_diff') or 0) > 0.03):
                        volume_review += 1
                    if 'amount_relative_diff' in row and (row.get('amount_relative_diff') is None or float(row.get('amount_relative_diff') or 0) > 0.03):
                        amount_review += 1
        result['years'].append({
            'year': year, 'audit_files': audit_files, 'audit_rows': audit_rows,
            'status_counts': dict(counters), 'reason_counts': dict(reason_counts.most_common()),
            'market_counts': dict(market_counts), 'accepted_bars_histogram': dict(bars_hist),
            'source_rows': source_rows, 'accepted_rows': accepted_rows,
            'invalid_rows': invalid_rows, 'quantity_invalid_rows': quantity_invalid_rows,
            'duplicate_datetime_rows': duplicate_rows, 'daily_missing_rows': missing_daily,
            'daily_reconciliation': {'status': 'not_checked' if not missing_daily and not price_review and not volume_review and not amount_review and audit_rows else 'checked', 'daily_missing_rows': missing_daily, 'price_review_rows': price_review, 'volume_review_rows': volume_review, 'amount_review_rows': amount_review},
            'formal_diff': {'status': 'not_checked', 'read_only': True, 'reason': 'no bounded formal diff was run in this evidence-only pass'},
        })
    (args.output_root / 'existing_evidence_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
