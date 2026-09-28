from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import pytest

from utils.ocean_stage import (
    EXPECTED_HEADER,
    ClickHouseOceanStageSink,
    LocalOceanStageSink,
    MemoryOceanStageSink,
    OCEAN_SOURCE,
    OCEAN_SOURCE_CATEGORY,
    STANDARD_5M_MINUTES,
    OceanStageError,
    accepted_stock_policy_hash,
    ocean_day_audit_ddl,
    ocean_stage_ddl,
    stage_verified_archive,
)
from utils.ocean_audit import iter_local_manifests
from utils.verified_zip import VerifiedZipError


HEADER = ",".join(EXPECTED_HEADER)


def _rows(
    code: str = "sh600000",
    day: str = "2001-01-02",
    count: int = 48,
    slots: tuple[int, ...] | None = None,
) -> list[str]:
    result = []
    for index, slot in enumerate((slots or STANDARD_5M_MINUTES)[:count]):
        minute = slot
        hour, minute = divmod(minute, 60)
        timestamp = f"{day} {hour:02d}:{minute:02d}:00"
        result.append(f"{timestamp},{code},浦发银行,20.75,20.8,20.8,20.75,172,356920,0.24,0.24")
    return result


def _make_archive(
    tmp_path: Path,
    content: str,
    name: str = "2001_5min.zip",
    entry_name: str = "sh600000_2001.csv",
) -> Path:
    archive = tmp_path / name
    with ZipFile(archive, "w") as handle:
        handle.writestr(entry_name, content)
        handle.writestr("README.txt", "fixture")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    marker = archive.with_suffix(archive.suffix + ".verified.json")
    marker.write_text(
        json.dumps({
            "name": archive.name,
            "size": archive.stat().st_size,
            "sha256": digest,
            "remote": "/fixture/" + archive.name,
            "remote_stat": f"{archive.stat().st_size} fixture",
            "verified_at": "2026-09-20T00:00:00+08:00",
            "method": "fixture",
        }),
        encoding="utf-8",
    )
    return archive


def test_ocean_schema_is_independent_and_lineage_keyed() -> None:
    ddl = ocean_stage_ddl()
    audit_ddl = ocean_day_audit_ddl()
    assert "ocean_verified_purchased_history_minute_5_stage" in ddl
    assert "purchased_history_minute_5_stage" not in ddl.replace("ocean_verified_purchased_history_minute_5_stage", "")
    assert "ENGINE = MergeTree" in ddl
    assert "ORDER BY (batch_id, source_entry, code, datetime, row_hash)" in ddl
    assert "archive_sha256 String" in ddl
    assert "ORDER BY (batch_id, source_entry, trade_date)" in audit_ddl
    assert "kline_minute_5" not in ddl + audit_ddl


def test_verified_crc_daily_48_and_idempotent_sink(tmp_path: Path) -> None:
    content = HEADER + "\n" + "\n".join(_rows()) + "\n"
    archive = _make_archive(tmp_path, content)
    sink = MemoryOceanStageSink()
    first = stage_verified_archive(
        archive,
        accepted_stock_codes=["600000.SH"],
        sink=sink,
        ingested_at=datetime(2026, 9, 20, 1, 0),
    )
    second = stage_verified_archive(
        archive,
        accepted_stock_codes=["600000.SH"],
        sink=sink,
        ingested_at=datetime(2026, 9, 20, 1, 1),
    )
    assert first.csv_entries == 1
    assert first.non_csv_entries == 1
    assert first.crc_verified_entries == 2
    assert first.stage_rows == 48
    assert first.pass_days == 1
    assert first.review_days == 0
    assert first.structure_pass is True
    assert first.formal_authorized is False
    assert first.gate_scope == "structure_only"
    assert second.stage_rows == 48
    assert len(sink.stage_rows) == 48
    assert len(sink.day_audits) == 1
    row = next(iter(sink.stage_rows.values()))
    assert row[0] == OCEAN_SOURCE
    assert row[1] == OCEAN_SOURCE_CATEGORY
    assert row[4].startswith("ocean_vps_5m_2001_")
    assert row[5] == first.archive_sha256
    assert row[3] == "sh600000_2001.csv"


def test_full_marker_sha_is_checked_before_stage(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(_rows()) + "\n")
    data = bytearray(archive.read_bytes())
    data[-1] ^= 1
    archive.write_bytes(data)
    with pytest.raises(VerifiedZipError, match="local_sha256_mismatch"):
        stage_verified_archive(archive, accepted_stock_codes=["600000.SH"])


def test_member_crc_is_checked_when_csv_reaches_eof(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(_rows()) + "\n")
    with ZipFile(archive) as handle:
        info = handle.infolist()[0]
        payload_offset = info.header_offset + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
        payload_end = payload_offset + info.compress_size
    data = bytearray(archive.read_bytes())
    data[payload_end - 1] ^= 1
    archive.write_bytes(data)
    marker = archive.with_suffix(archive.suffix + ".verified.json")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["size"] = archive.stat().st_size
    payload["sha256"] = hashlib.sha256(data).hexdigest()
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BadZipFile):
        stage_verified_archive(archive, accepted_stock_codes=["600000.SH"])


def test_pending_security_isolated_from_stage(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(_rows()) + "\n")
    sink = MemoryOceanStageSink()
    report = stage_verified_archive(archive, accepted_stock_codes=[], sink=sink)
    assert report.stage_rows == 0
    assert report.pending_entries == 1
    assert report.isolated_days == 1
    assert not sink.stage_rows
    audit = next(iter(sink.day_audits.values()))
    assert audit[9] == "historical_stock_pending"
    assert audit[18] == "isolated"


def test_row_code_mismatch_isolated_even_when_ohlc_is_valid(tmp_path: Path) -> None:
    content = HEADER + "\n" + "\n".join(_rows(code="sz000001")) + "\n"
    archive = _make_archive(tmp_path, content)
    sink = MemoryOceanStageSink()
    report = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
    assert report.stage_rows == 0
    assert report.rejected_entries == 1
    assert not sink.stage_rows
    audit = next(iter(sink.day_audits.values()))
    assert audit[18] == "isolated"
    assert audit[19] == "csv_code_does_not_match_filename"


def test_invalid_quantity_and_ohlc_block_daily_pass(tmp_path: Path) -> None:
    rows = _rows()
    rows[0] = rows[0].replace(",172,356920,", ",172.5,356920,")
    rows[1] = rows[1].replace(",20.75,20.8,20.8,20.75,", ",0,20.8,20.8,20.75,")
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(rows) + "\n")
    sink = MemoryOceanStageSink()
    report = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
    assert report.stage_rows == 46
    assert report.pass_days == 0
    assert report.review_days == 1
    audit = next(iter(sink.day_audits.values()))
    assert audit[16] == 2
    assert audit[17] == 1
    assert "accepted_bars=46" in audit[19]


def test_wrong_48_slots_do_not_pass_structure_gate(tmp_path: Path) -> None:
    wrong_slots = tuple(range(9 * 60 + 40, 13 * 60 + 40, 5))[:48]
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(_rows(slots=wrong_slots)) + "\n")
    sink = MemoryOceanStageSink()
    report = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
    assert report.stage_rows == 48
    assert report.pass_days == 0
    assert report.review_days == 1
    assert report.structure_pass is False
    audit = next(iter(sink.day_audits.values()))
    assert "standard_5m_slots_mismatch" in audit[19]


def test_entry_year_mismatch_isolated_with_zero_stage_rows(tmp_path: Path) -> None:
    archive = _make_archive(
        tmp_path,
        HEADER + "\n" + "\n".join(_rows()) + "\n",
        entry_name="sh600000_2000.csv",
    )
    sink = MemoryOceanStageSink()
    report = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
    assert report.stage_rows == 0
    assert report.rejected_entries == 1
    assert not sink.stage_rows
    audit = next(iter(sink.day_audits.values()))
    assert audit[19] == "entry_year_mismatch"


def test_datetime_year_mismatch_isolated_with_zero_stage_rows(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(_rows(day="2000-01-03")) + "\n")
    sink = MemoryOceanStageSink()
    report = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
    assert report.stage_rows == 0
    assert report.rejected_entries == 1
    assert not sink.stage_rows
    audit = next(iter(sink.day_audits.values()))
    assert audit[19] == "datetime_year_mismatch"


def test_empty_csv_isolated_and_not_accepted_stage(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, HEADER + "\n")
    sink = MemoryOceanStageSink()
    report = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
    assert report.stage_rows == 0
    assert report.rejected_entries == 1
    assert report.structure_pass is False
    assert not sink.stage_rows


def test_clickhouse_sink_is_fail_closed_without_touching_client() -> None:
    class NoTouchClient:
        def __getattr__(self, name: str):
            raise AssertionError(f"unexpected_client_access={name}")

    with pytest.raises(OceanStageError, match="clickhouse_ocean_sink_disabled"):
        ClickHouseOceanStageSink(NoTouchClient())


def test_local_sink_commits_atomic_entry_manifest_and_is_idempotent(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(_rows()) + "\n")
    root = tmp_path / "isolated"
    sink = LocalOceanStageSink(root, min_free_bytes=0)
    first = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
    second = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
    assert first.structure_pass is True
    assert second.structure_pass is True
    manifests = list(root.rglob("manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["archive_sha256"] == first.archive_sha256
    assert manifest["accepted_stock_policy_hash"] == accepted_stock_policy_hash(["600000.SH"])
    assert manifest["parser_contract_version"]
    assert manifest["stage_schema_version"]
    assert manifest["stage_rows"] == 48
    assert manifest["audit_days"] == 1
    assert manifest["formal_authorized"] is False
    assert manifest["gate_scope"] == "structure_only"
    assert manifest["compression"] == "gzip"
    assert not list(root.rglob("*.tmp"))
    local_manifest = next(iter_local_manifests(root))
    assert len(list(local_manifest.iter_stage_rows())) == 48


def test_policy_hash_change_never_reuses_pending_manifest(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(_rows()) + "\n")
    root = tmp_path / "isolated"
    pending_sink = LocalOceanStageSink(root, min_free_bytes=0)
    pending = stage_verified_archive(archive, accepted_stock_codes=[], sink=pending_sink)
    pending_sink.close()
    accepted_sink = LocalOceanStageSink(root, min_free_bytes=0)
    accepted = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=accepted_sink)
    accepted_sink.close()
    assert pending.stage_rows == 0
    assert accepted.stage_rows == 48
    manifests = list(root.rglob("manifest.json"))
    assert len(manifests) == 2
    assert {item.parent.parent.name for item in manifests} == {
        accepted_stock_policy_hash([])[:16], accepted_stock_policy_hash(["600000.SH"])[:16]
    }


def test_local_sink_single_writer_lock_is_fail_closed(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(_rows()) + "\n")
    root = tmp_path / "isolated"
    first = LocalOceanStageSink(root, min_free_bytes=0)
    second = LocalOceanStageSink(root, min_free_bytes=0)
    try:
        stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=first)
        with pytest.raises(OceanStageError, match="local_sink_lock_busy"):
            stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=second)
    finally:
        first.close()
        second.close()


def test_local_sink_free_disk_guard_fails_before_materialization(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(_rows()) + "\n")
    root = tmp_path / "isolated"
    free = shutil.disk_usage(tmp_path).free
    sink = LocalOceanStageSink(root, min_free_bytes=free)
    try:
        with pytest.raises(OceanStageError, match="local_free_disk_guard"):
            stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
        assert not list(root.rglob("manifest.json"))
    finally:
        sink.close()


def test_malformed_row_isolates_the_whole_entry(tmp_path: Path) -> None:
    rows = _rows()
    rows.append("malformed,row")
    archive = _make_archive(tmp_path, HEADER + "\n" + "\n".join(rows) + "\n")
    sink = MemoryOceanStageSink()
    report = stage_verified_archive(archive, accepted_stock_codes=["600000.SH"], sink=sink)
    assert report.stage_rows == 0
    assert report.rejected_entries == 1
    assert not sink.stage_rows
    audit = next(iter(sink.day_audits.values()))
    assert audit[19] == "invalid_row_shape_or_timestamp"
