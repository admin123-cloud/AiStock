import gzip
import json
from pathlib import Path

from scripts.audit_daily_closure_snapshot import (
    _load_accepted_manifest,
    _manifest_reader,
    _parse_ths_source_rows,
)
from scripts.run_daily_closure_range_snapshot import _merge_coverage_gate, _merge_source_gates
from utils.daily_closure_snapshot import CLOSURE_BLOCKED, CLOSURE_HEALTHY


def test_manifest_reader_builds_exact_local_registry_member_permit(tmp_path: Path):
    manifest = tmp_path / "manifest.jsonl.gz"
    row = {
        "code": "000001.SZ",
        "date_set_sha256": "accepted-digest",
        "dates": ["2026-01-05", "2026-01-06"],
        "source_acceptance": {"sha256": "source-digest"},
    }
    with gzip.open(manifest, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    records = _load_accepted_manifest(manifest)
    permit = _manifest_reader(records).exact_key_permit(
        [("000001.SZ", "2026-01-05"), ("000001.SZ", "2026-01-06")],
        source="test-local-manifest",
    )
    assert len(permit.keys) == 2
    assert permit.dataset_ids


def test_merge_coverage_gate_counts_missing_and_outside():
    gate = _merge_coverage_gate(
        expected_key_count=10,
        formal_key_count=12,
        formal_present_count=8,
        expected_latest=__import__("datetime").date(2026, 1, 6),
        formal_latest=__import__("datetime").date(2026, 1, 7),
    )
    assert gate["status"] == CLOSURE_BLOCKED
    assert gate["missing_key_count"] == 2
    assert gate["outside_key_count"] == 4


def test_merge_source_gate_preserves_mismatch_block():
    gate = _merge_source_gates(
        [
            {"status": CLOSURE_HEALTHY, "formal_rows_compared": 4, "source_rows_compared": 4},
            {"status": CLOSURE_BLOCKED, "formal_rows_compared": 3, "source_rows_compared": 3, "mismatched_rows": 1},
        ]
    )
    assert gate["status"] == CLOSURE_BLOCKED
    assert gate["mismatched_rows"] == 1
