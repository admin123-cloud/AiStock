"""Explicit per-security source and acknowledged-write coverage, no inferred exemptions."""
from datetime import datetime


def evaluate_coverage(expected, ticks, persisted, *, now, parse_time, metadata=None, exemptions=None):
    metadata, exemptions = metadata or {}, exemptions or {}
    groups, failures = {}, []
    for code in sorted(expected):
        kind = metadata.get(code, {}).get('type') or 'unknown'
        record = groups.setdefault(kind, {'expected': 0, 'fresh': 0, 'persisted_fresh': 0, 'exempt': 0})
        record['expected'] += 1
        evidence = exemptions.get(code, {})
        # Even a name containing "退" or a zero-volume tick is not proof of suspension.
        exempt = (evidence.get('source') == 'qmt' and bool(evidence.get('evidence_ref'))
                  and evidence.get('trade_date') == now.date().isoformat()
                  and evidence.get('status') in ('suspended', 'delisted', 'not_listed'))
        if exempt:
            record['exempt'] += 1
            continue
        tick = ticks.get(code)
        source_at = parse_time(tick.get('time'), datetime.min) if tick else datetime.min
        fresh = 0 <= (now-source_at).total_seconds() <= 300
        record['fresh'] += int(fresh)
        written = fresh and code in persisted
        record['persisted_fresh'] += int(written)
        if not written:
            failures.append({'code': code, 'type': kind, 'reason': 'no_response' if not tick else
                             'stale_or_invalid_source_time' if not fresh else 'not_persisted',
                             'source_at': source_at.isoformat() if tick else None})
    return {'groups': groups, 'unverified': failures,
            'slo_300s': 'passed' if expected and not failures else 'failed' if expected else 'unknown',
            'evidence_scope': 'source timestamp and acknowledged current batch writes; repeated-run interval separately measured'}
