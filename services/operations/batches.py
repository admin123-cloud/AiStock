"""Atomic self-contained G3 observation batches; legacy artifacts stay untouched."""
import hashlib
import io
import csv
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from strategies.contracts import formal_g3_score88_contract_metadata


def publish_mainwave_batch(root: Path, summary: dict, candidates_csv: str, *, tickets_csv=None, diagnostics_csv=None):
    # Embedded payloads make a batch a single atomic file: no inter-file read race.
    payload = {'summary': summary, 'candidates_csv': candidates_csv}
    if tickets_csv is not None and diagnostics_csv is not None:
        payload.update(tickets_csv=tickets_csv, diagnostics_csv=diagnostics_csv)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    batch = {'schema_version': 1, 'batch_id': uuid4().hex,
             'contract_hash': formal_g3_score88_contract_metadata()['sha256'],
             'sha256': hashlib.sha256(encoded.encode('utf-8')).hexdigest(), 'payload': encoded}
    target = root/'operations/mainwave_batch.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, delete=False) as handle:
            temp = Path(handle.name)
            json.dump(batch, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp and temp.exists():
            temp.unlink()
    return batch['batch_id']


def read_mainwave_batch(root: Path, *, include_runtime=False):
    try:
        batch = json.loads((root/'operations/mainwave_batch.json').read_text(encoding='utf-8'))
        if batch.get('schema_version') != 1 or not batch.get('batch_id'):
            raise ValueError('batch_schema_invalid')
        if batch.get('contract_hash') != formal_g3_score88_contract_metadata()['sha256']:
            raise ValueError('batch_contract_mismatch')
        if hashlib.sha256(batch['payload'].encode('utf-8')).hexdigest() != batch['sha256']:
            raise ValueError('batch_checksum_mismatch')
        payload = json.loads(batch['payload'])
        reader = csv.DictReader(io.StringIO(payload['candidates_csv'].lstrip('\ufeff')))
        if not {'route','code','entry_date','decision_date'}.issubset(reader.fieldnames or []):
            raise ValueError('batch_candidates_schema_invalid')
        summary, rows = payload['summary'], list(reader)
        if not summary.get('entry_date') or any(
                x.get('entry_date') != summary.get('entry_date') or x.get('decision_date') != summary.get('decision_date') for x in rows):
            raise ValueError('batch_business_date_mismatch')
        metadata = {'ok': True, 'batch_id': batch['batch_id'], 'contract_hash': batch['contract_hash']}
        if include_runtime:
            if 'tickets_csv' not in payload or 'diagnostics_csv' not in payload:
                raise ValueError('batch_runtime_payload_missing')
            # Preserve the existing runtime reader's numeric/boolean/null inference.
            import pandas as pd
            def records(value):
                try:
                    frame = pd.read_csv(io.StringIO(value), low_memory=False)
                except pd.errors.EmptyDataError:
                    return []
                frame = frame.astype(object).where(pd.notna(frame), None)
                return json.loads(frame.to_json(orient='records', force_ascii=False))
            metadata['tickets'] = records(payload['tickets_csv'])
            metadata['diagnostics'] = records(payload['diagnostics_csv'])
            if any(x.get('entry_date') != summary.get('entry_date') for x in metadata['tickets']):
                raise ValueError('batch_ticket_date_mismatch')
        return summary, rows, metadata
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return None, None, {'ok': False, 'reason': str(exc) if not isinstance(exc, OSError) else 'batch_missing_or_unreadable'}
