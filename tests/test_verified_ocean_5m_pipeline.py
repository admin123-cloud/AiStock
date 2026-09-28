from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.consume_verified_ocean_5m import StateStore, format_sample_ok, run_once
from scripts.import_purchased_5m_stage import process_entry, source_code_from_entry
from utils.verified_zip import VerifiedZipError, inspect_zip, verify_ready


HEADER = "时间,代码,名称,开盘价,收盘价,最高价,最低价,成交量,成交额,涨幅,振幅"
ROW = "2001-01-02 09:35:00,sh600000,浦发银行,20.75,20.8,20.8,20.75,172,356920,0.24,0.24"


def make_archive(tmp_path: Path, name: str = "2001_5min.zip") -> tuple[Path, Path]:
    archive = tmp_path / name
    with ZipFile(archive, "w") as handle:
        handle.writestr("sh600000_2001.csv", HEADER + "\n" + ROW + "\n")
    import hashlib

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    marker = archive.with_suffix(archive.suffix + ".verified.json")
    marker.write_text(
        json.dumps(
            {
                "name": archive.name,
                "size": archive.stat().st_size,
                "sha256": digest,
                "remote": "/home/asky/AShare_5min_2000_2026/" + archive.name,
                "remote_stat": f"{archive.stat().st_size} 1",
                "verified_at": "2026-09-20T00:00:00+08:00",
                "method": "fixture",
            }
        ),
        encoding="utf-8",
    )
    return archive, marker


def test_verified_marker_hash_and_format_contract(tmp_path: Path):
    archive, marker = make_archive(tmp_path)
    verified = verify_ready(archive, marker)
    inspection = inspect_zip(archive)
    ok, format_info = format_sample_ok(inspection["sample"])
    assert verified.sha256
    assert inspection["csv_entries"] == 1
    assert ok is True
    assert format_info["timestamp_format"] == "%Y-%m-%d %H:%M:%S"
    assert "stock volume lots" in format_info["source_contract"]


def test_missing_verified_marker_is_not_consumable(tmp_path: Path):
    archive = tmp_path / "2001_5min.zip"
    archive.write_bytes(b"partial-not-a-zip")
    with pytest.raises(VerifiedZipError, match="verified_marker_missing"):
        verify_ready(archive)


def test_zip_slip_member_is_rejected(tmp_path: Path):
    archive = tmp_path / "2001_5min.zip"
    with ZipFile(archive, "w") as handle:
        handle.writestr("../outside.csv", HEADER + "\n" + ROW + "\n")
    with pytest.raises(VerifiedZipError, match="unsafe_zip_traversal_member"):
        inspect_zip(archive)


def test_annual_entry_name_and_new_datetime_are_parsed(tmp_path: Path):
    archive_path = tmp_path / "2001_5min.zip"
    with ZipFile(archive_path, "w") as handle:
        handle.writestr("sh600000_2001.csv", HEADER + "\n" + ROW + "\n")
    with ZipFile(archive_path) as archive:
        info = archive.infolist()[0]
        audit, rows = process_entry(
            archive,
            info,
            archive_path,
            "fixture_batch",
            "stock",
            "purchased_hs_5m",
            {},
            {},
            True,
        )
    assert source_code_from_entry("sh600000_2001.csv") == "sh600000"
    assert audit[4] == "sh600000"
    assert audit[5] == "600000.SH"
    assert audit[10] == 1
    assert len(rows) == 1
    assert rows[0][10].isoformat().startswith("2001-01-02T09:35:00")


def test_worker_waits_without_opening_unverified_archive(tmp_path: Path):
    archive = tmp_path / "2001_5min.zip"
    archive.write_bytes(b"partial-not-a-zip")
    args = Namespace(
        archive_dir=tmp_path,
        write_stage=False,
        write_candidates=False,
        promote_missing=False,
        poll_seconds=300,
        max_years_per_scan=1,
        batch_rows=10,
        min_free_bytes=1,
        years=[2001],
    )
    state = StateStore(tmp_path / "state", tmp_path, args)
    assert run_once(args, state) == 0
    saved = json.loads((tmp_path / "state" / "status.json").read_text(encoding="utf-8"))
    assert saved["years"]["2001"]["state"] == "waiting_verified"
