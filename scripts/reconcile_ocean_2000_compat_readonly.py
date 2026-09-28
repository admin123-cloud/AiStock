from __future__ import annotations

"""Compatibility-only 2000 Ocean daily reconciliation.

Reads only valid legacy stage manifests below the archive hash directory, so
old formal-diff manifests outside that tree cannot poison the scan.  It writes
new evidence and compatibility manifests under a new output root; no source
stage file or formal table is modified.
"""

import argparse
import gzip
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.ocean_audit import (
    DailySnapshotReader,
    LocalOceanManifest,
    reconcile_stage_rows,
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--source-root', type=Path, required=True)
    p.add_argument('--archive-prefix', required=True)
    p.add_argument('--policy-prefix', required=True)
    p.add_argument('--daily-root', type=Path, required=True)
    p.add_argument('--output-root', type=Path, required=True)
    p.add_argument('--formal-conflict-audit', type=Path, required=True)
    p.add_argument('--relative-tolerance', type=float, default=0.001)
    p.add_argument('--amount-tolerance', type=float, default=0.01)
    p.add_argument(
        '--accept-amount-unit-yuan', action='store_true',
        help='Apply the user-approved pre-2010 yuan contract; unit-only review is not a blocker.',
    )
    args = p.parse_args()
    if args.relative_tolerance < 0 or args.amount_tolerance < 0:
        raise SystemExit('tolerances_must_be_non_negative')
    started = time.monotonic()
    entry_root = args.source_root / args.archive_prefix / args.policy_prefix
    manifests = sorted(path for path in entry_root.glob('*/manifest.json') if path.is_file())
    args.output_root.mkdir(parents=True, exist_ok=True)
    pass_path = args.output_root / 'pass.jsonl.gz'
    review_path = args.output_root / 'review.jsonl.gz'
    counts = Counter()
    unit_counts = Counter()
    errors = []
    snapshot = DailySnapshotReader(args.daily_root)
    with gzip.open(pass_path, 'wt', encoding='utf-8', newline='\n') as pass_out, gzip.open(review_path, 'wt', encoding='utf-8', newline='\n') as review_out:
        for idx, path in enumerate(manifests, 1):
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
                source_manifest = LocalOceanManifest(path, payload)
                stage_rows = list(source_manifest.iter_stage_rows())
                day_audits = list(source_manifest.iter_day_audits())
                codes = sorted({str(row.get('code') or '') for row in day_audits if row.get('code')})
                years = sorted({int(str(row.get('trade_date'))[:4]) for row in day_audits if row.get('trade_date')})
                daily = {}
                for year in years:
                    daily.update(snapshot.load_code_year(year, codes=codes))
                reconciled = reconcile_stage_rows(
                    stage_rows,
                    daily,
                    day_audits=day_audits,
                    relative_tolerance=args.relative_tolerance,
                    amount_tolerance=args.amount_tolerance,
                )
                entry_counts = Counter()
                for item in reconciled:
                    row = dict(item.__dict__)
                    row['trade_date'] = str(row['trade_date'])
                    row['amount_unit_status'] = (
                        'accepted_yuan_by_user_policy'
                        if args.accept_amount_unit_yuan
                        else 'unconfirmed_source_amount_yuan'
                    )
                    row['database_amount_unit_contract'] = 'yuan'
                    if not args.accept_amount_unit_yuan:
                        row['overall_status'] = 'review'
                        row['reason'] = ((row.get('reason') + '; ') if row.get('reason') else '') + 'amount_unit_unconfirmed_source_vs_yuan_contract'
                    entry_counts[row['overall_status']] += 1
                    counts[row['overall_status']] += 1
                    unit_counts[row['amount_unit_status']] += 1
                    target = pass_out if row['overall_status'] == 'pass' else review_out
                    target.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=str) + '\n')
                compat = {
                    'schema_version': 'ocean-2000-compat-manifest-v1',
                    'source_manifest': str(path),
                    'source_manifest_sha256': sha256(path),
                    'archive_sha256': payload.get('archive_sha256'),
                    'batch_id': payload.get('batch_id'),
                    'source_entry': payload.get('source_entry'),
                    'parser_contract_version': payload.get('parser_contract_version'),
                    'source_stage_file': str(source_manifest.stage_path()),
                    'source_audit_file': str(source_manifest.audit_path()),
                    'source_stage_sha256': payload.get('stage_sha256'),
                    'source_audit_sha256': payload.get('audit_sha256'),
                    'entry_status': 'pass' if entry_counts.get('review', 0) == 0 and entry_counts.get('pass', 0) > 0 else 'review',
                    'structure_pass': entry_counts.get('review', 0) == 0 and entry_counts.get('pass', 0) > 0,
                    'daily_pass_count': entry_counts.get('pass', 0),
                    'daily_review_count': entry_counts.get('review', 0),
                    'amount_unit_status': 'accepted_yuan_by_user_policy' if args.accept_amount_unit_yuan else 'unconfirmed_source_amount_yuan',
                    'amount_tolerance': args.amount_tolerance,
                    'relative_tolerance': args.relative_tolerance,
                    'database_amount_unit_contract': 'yuan',
                    'formal_authorized': False,
                    'formal_insert_rows': 0,
                    'formal_diff_status': 'not_run',
                }
                compat_dir = args.output_root / 'compat_manifests' / path.parent.name
                compat_dir.mkdir(parents=True, exist_ok=True)
                (compat_dir / 'manifest.json').write_text(json.dumps(compat, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            except Exception as exc:
                errors.append({'source_manifest': str(path), 'error': f'{type(exc).__name__}: {exc}'})
                counts['error'] += 1
            if idx % 50 == 0:
                (args.output_root / 'progress.json').write_text(json.dumps({'processed_entries': idx, 'total_entries': len(manifests), 'counts': dict(counts), 'errors': len(errors), 'formal_authorized': False}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    conflict_audit = json.loads(args.formal_conflict_audit.read_text(encoding='utf-8'))
    summary = {
        'schema_version': 'ocean-2000-compat-reconciliation-summary-v1',
        'run_id': f'ocean_2000_compat_{time.strftime("%Y%m%dT%H%M%S")}',
        'source_entries': len(manifests),
        'processed_status_counts': dict(counts),
        'unit_status_counts': dict(unit_counts),
        'errors': errors,
        'formal': {'formal_authorized': False, 'formal_insert_rows': 0, 'diff_status': 'not_run', 'existing_conflict_audit': conflict_audit},
        'pass_file': str(pass_path),
        'review_file': str(review_path),
        'pass_sha256': sha256(pass_path),
        'review_sha256': sha256(review_path),
        'elapsed_seconds': round(time.monotonic() - started, 3),
        'candidate_gate': (
            'blocked_formal_diff_not_run'
            if args.accept_amount_unit_yuan
            else 'blocked_amount_unit_unconfirmed_and_formal_diff_not_run'
        ),
        'policy': {
            'accept_amount_unit_yuan': bool(args.accept_amount_unit_yuan),
            'relative_tolerance': args.relative_tolerance,
            'amount_tolerance': args.amount_tolerance,
            'formal_authorized': False,
        },
    }
    (args.output_root / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    (args.output_root / 'progress.json').write_text(json.dumps({'status': 'complete', **summary}, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
