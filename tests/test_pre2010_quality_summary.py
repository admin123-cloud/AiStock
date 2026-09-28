from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from scripts.summarize_pre2010_quality_gate import PERIOD_FACTORS, summarize_year


def test_period_derivation_requires_exact_bar_factors():
    # A complete 5m code-day is the only source for deterministic roll-ups;
    # each derived period must consume fixed contiguous 5m bars.
    assert PERIOD_FACTORS == {"5m": 1, "15m": 3, "30m": 6, "60m": 12}
    assert {"5m": 48, "15m": 16, "30m": 8, "60m": 4} == {
        period: 48 // factor for period, factor in PERIOD_FACTORS.items()
    }


def _write_evidence(root: Path, *, rows: list[dict], partition: str) -> dict:
    entry = root / "entry"
    entry.mkdir(parents=True, exist_ok=True)
    path = entry / f"{partition}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rows": len(rows)}


def _manifest(root: Path, pass_rows: list[dict], review_rows: list[dict]) -> None:
    entry = root / "entry"
    pass_info = _write_evidence(root, rows=pass_rows, partition="pass")
    review_info = _write_evidence(root, rows=review_rows, partition="review")
    payload = {
        "schema_version": "ocean-daily-reconciliation-v1",
        "status": "complete",
        "formal_authorized": False,
        "gate_scope": "structure_plus_daily_read_only",
        "pass_file": pass_info["file"],
        "pass_sha256": pass_info["sha256"],
        "review_file": review_info["file"],
        "review_sha256": review_info["sha256"],
    }
    (entry / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _pass_row(code: str, trade_date: str) -> dict:
    return {
        "code": code,
        "trade_date": trade_date,
        "source_entry": f"{code}_2001.csv",
        "batch_id": "batch",
        "source_archive": "archive",
        "source_manifest_sha256": "source-manifest",
        "minute_rows": 48,
        "unique_datetime_rows": 48,
        "daily_snapshot_rows": 1,
        "structure_pass": True,
        "daily_gate_pass": True,
        "overall_status": "pass",
        "reason": "",
        "units_contract": "volume_lots/amount_yuan; raw_unadjusted_price",
        "evidence_partition": "pass",
    }


def test_summary_writes_only_unique_accepted_candidates(tmp_path: Path):
    daily = tmp_path / "daily_reconciliation_2001"
    daily.mkdir()
    (daily / "state.json").write_text(
        json.dumps({"status": "complete", "expected_csv_entries": 1, "completed_entries": 1}),
        encoding="utf-8",
    )
    accepted = _pass_row("600000.SH", "2001-01-02")
    _manifest(daily, [accepted, dict(accepted)], [{"overall_status": "review", "reason": "daily_missing"}])

    result = summarize_year(2001, daily_roots=[daily], output_root=tmp_path / "out")

    assert result["status"] == "complete"
    assert result["accepted_code_days_before_dedup"] == 2
    assert result["duplicate_candidate_code_days"] == 1
    assert result["candidate_code_days"] == 0
    assert result["review_evidence_rows"] == 1
    assert result["formal_authorized"] is False


def test_summary_marks_missing_year_as_not_started(tmp_path: Path):
    result = summarize_year(2008, daily_roots=[], output_root=tmp_path / "out")
    assert result["status"] == "not_started"
    assert result["candidate_code_days"] == 0


def test_failed_daily_run_is_blocked_without_partial_candidates(tmp_path: Path):
    daily = tmp_path / "daily_reconciliation_2000"
    daily.mkdir()
    (daily / "state.json").write_text(
        json.dumps({"status": "failed_preserved", "error": "truncated evidence"}),
        encoding="utf-8",
    )
    accepted = _pass_row("600000.SH", "2000-01-04")
    _manifest(daily, [accepted], [])

    result = summarize_year(2000, daily_roots=[daily], output_root=tmp_path / "out")

    assert result["status"] == "blocked"
    assert result["candidate_code_days"] == 0
    assert result["accepted_code_days_before_dedup"] == 0
