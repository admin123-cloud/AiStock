"""Independent, stage-only validation contract for Ocean annual 5m ZIPs.

This module intentionally has no ClickHouse client import and no formal-table
dependency.  The optional sink interface is injectable so tests can exercise
the idempotency contract without creating tables or writing a database.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Protocol, Sequence
from zipfile import ZipFile, ZipInfo
from zoneinfo import ZoneInfo

try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None

from utils.verified_zip import VerifiedArchive, inspect_zip, verify_ready, zip_member_name


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
OCEAN_SOURCE = "ocean_vps_5m"
OCEAN_SOURCE_CATEGORY = "ocean_hs_5m"
OCEAN_STAGE_TABLE = "stock.ocean_verified_purchased_history_minute_5_stage"
OCEAN_AUDIT_TABLE = "stock.ocean_verified_purchased_history_minute_5_day_audit"
OCEAN_STAGE_SCHEMA_VERSION = "ocean-stage-v2"
OCEAN_PARSE_CONTRACT_VERSION = "ocean-5m-structure-v3"
EXPECTED_HEADER = ("时间", "代码", "名称", "开盘价", "收盘价", "最高价", "最低价", "成交量", "成交额", "涨幅", "振幅")
EXPECTED_BAR_COUNT = 48
STANDARD_5M_MINUTES = tuple(
    list(range(9 * 60 + 35, 11 * 60 + 30 + 1, 5))
    + list(range(13 * 60 + 5, 15 * 60 + 1, 5))
)
YEAR_RE = re.compile(r"^(20(?:0[0-9]|1[0-9]|2[0-6]))_5min\.zip$", re.IGNORECASE)
ENTRY_YEAR_RE = re.compile(r"_(20(?:0[0-9]|1[0-9]|2[0-6]))$", re.IGNORECASE)

STAGE_COLUMNS = [
    "source", "source_category", "source_archive", "source_entry", "batch_id",
    "archive_sha256", "source_code", "code", "security_type", "universe_decision",
    "period", "trade_date", "datetime", "open", "high", "low", "close",
    "source_volume_lots", "source_amount_yuan", "price_adjustment", "row_hash",
    "ingested_at", "id",
]

DAY_AUDIT_COLUMNS = [
    "source", "source_category", "source_archive", "source_entry", "batch_id",
    "archive_sha256", "source_code", "code", "security_type", "universe_decision",
    "trade_date", "expected_bar_count", "source_row_count", "valid_ohlc_row_count",
    "accepted_row_count", "unique_datetime_count", "invalid_row_count",
    "quantity_invalid_row_count", "status", "reason", "min_datetime", "max_datetime",
    "audited_at",
]


class OceanStageError(RuntimeError):
    """The archive or a source entry cannot enter the independent Ocean stage."""


def ocean_stage_ddl(table: str = OCEAN_STAGE_TABLE) -> str:
    """Return the future independent stage DDL; this function never executes it."""

    return f"""
CREATE TABLE IF NOT EXISTS {table}
(
    source LowCardinality(String),
    source_category LowCardinality(String),
    source_archive String,
    source_entry String,
    batch_id String,
    archive_sha256 String,
    source_code String,
    code String,
    security_type LowCardinality(String),
    universe_decision LowCardinality(String),
    period LowCardinality(String),
    trade_date Date,
    datetime DateTime('Asia/Shanghai'),
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    source_volume_lots Nullable(Float64),
    source_amount_yuan Nullable(Float64),
    price_adjustment LowCardinality(String),
    row_hash String,
    ingested_at DateTime('Asia/Shanghai'),
    id UInt64
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(datetime)
ORDER BY (batch_id, source_entry, code, datetime, row_hash)
""".strip()


def ocean_day_audit_ddl(table: str = OCEAN_AUDIT_TABLE) -> str:
    """Return the future day-audit DDL; this function never executes it."""

    return f"""
CREATE TABLE IF NOT EXISTS {table}
(
    source LowCardinality(String),
    source_category LowCardinality(String),
    source_archive String,
    source_entry String,
    batch_id String,
    archive_sha256 String,
    source_code String,
    code String,
    security_type LowCardinality(String),
    universe_decision LowCardinality(String),
    trade_date Date,
    expected_bar_count UInt16,
    source_row_count UInt32,
    valid_ohlc_row_count UInt32,
    accepted_row_count UInt32,
    unique_datetime_count UInt32,
    invalid_row_count UInt32,
    quantity_invalid_row_count UInt32,
    status LowCardinality(String),
    reason String,
    min_datetime Nullable(DateTime('Asia/Shanghai')),
    max_datetime Nullable(DateTime('Asia/Shanghai')),
    audited_at DateTime('Asia/Shanghai')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(trade_date)
ORDER BY (batch_id, source_entry, trade_date)
""".strip()


def parse_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BUSINESS_TZ)
    except ValueError:
        return None


def canonical_stock(value: Any) -> str | None:
    match = re.fullmatch(r"(sh|sz|bj)(\d{6})", str(value or "").strip(), re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(2)}.{match.group(1).upper()}"


def source_code_from_entry(source_entry: str) -> str:
    stem = PurePosixPath(source_entry.replace("\\", "/")).name
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    return re.sub(r"_(?:19|20)\d{2}$", "", stem)


def _entry_year(source_entry: str) -> int | None:
    stem = PurePosixPath(source_entry.replace("\\", "/")).name.rsplit(".", 1)[0]
    match = ENTRY_YEAR_RE.search(stem)
    return int(match.group(1)) if match else None


def _archive_year(path: Path) -> int:
    match = YEAR_RE.fullmatch(path.name)
    if not match:
        raise OceanStageError(f"unsupported_ocean_archive_name={path.name}")
    return int(match.group(1))


def normalize_stock_whitelist(values: Iterable[str]) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text.startswith("#"):
            continue
        match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", text, re.IGNORECASE)
        canonical = canonical_stock(text) or (f"{match.group(1)}.{match.group(2).upper()}" if match else None)
        if canonical:
            result.add(canonical)
    return frozenset(result)


def accepted_stock_policy_hash(values: Iterable[str]) -> str:
    """Hash the canonical accepted-stock policy, independent of input order."""

    canonical = sorted(normalize_stock_whitelist(values))
    payload = "accepted-stock-policy-v1\n" + "\n".join(canonical)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class _DayStats:
    source_row_count: int = 0
    valid_ohlc_row_count: int = 0
    accepted_row_count: int = 0
    invalid_row_count: int = 0
    quantity_invalid_row_count: int = 0
    datetimes: list[datetime] = field(default_factory=list)


@dataclass(frozen=True)
class OceanDayAudit:
    source: str
    source_category: str
    source_archive: str
    source_entry: str
    batch_id: str
    archive_sha256: str
    source_code: str
    code: str
    security_type: str
    universe_decision: str
    trade_date: date
    source_row_count: int
    valid_ohlc_row_count: int
    accepted_row_count: int
    unique_datetime_count: int
    invalid_row_count: int
    quantity_invalid_row_count: int
    status: str
    reason: str
    min_datetime: datetime | None
    max_datetime: datetime | None
    audited_at: datetime

    def values(self) -> list[Any]:
        return [
            self.source, self.source_category, self.source_archive, self.source_entry,
            self.batch_id, self.archive_sha256, self.source_code, self.code,
            self.security_type, self.universe_decision, self.trade_date, EXPECTED_BAR_COUNT,
            self.source_row_count, self.valid_ohlc_row_count, self.accepted_row_count,
            self.unique_datetime_count, self.invalid_row_count, self.quantity_invalid_row_count,
            self.status, self.reason, self.min_datetime, self.max_datetime, self.audited_at,
        ]


@dataclass
class OceanEntryResult:
    batch_id: str
    archive_sha256: str
    source_archive: str
    accepted_stock_policy_hash: str
    parser_contract_version: str
    stage_schema_version: str
    source_entry: str
    source_code: str
    code: str
    security_type: str
    universe_decision: str
    entry_status: str
    reason: str
    stage_rows: list[list[Any]]
    day_audits: list[OceanDayAudit]
    crc_verified: bool = True

    @property
    def accepted_rows(self) -> int:
        return len(self.stage_rows)


@dataclass
class OceanArchiveReport:
    archive: str
    batch_id: str
    archive_sha256: str
    csv_entries: int = 0
    non_csv_entries: int = 0
    crc_verified_entries: int = 0
    stage_rows: int = 0
    audit_days: int = 0
    pass_days: int = 0
    isolated_days: int = 0
    review_days: int = 0
    pending_entries: int = 0
    rejected_entries: int = 0
    structure_pass: bool = False
    formal_authorized: bool = False
    gate_scope: str = "structure_only"
    accepted_stock_policy_hash: str = ""
    parser_contract_version: str = OCEAN_PARSE_CONTRACT_VERSION
    stage_schema_version: str = OCEAN_STAGE_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class OceanStageSink(Protocol):
    def write_entry(self, result: OceanEntryResult) -> None:
        """Persist only to the independent Ocean stage and day-audit tables."""


def _row_hash(archive_sha256: str, source_entry: str, ordinal: int, cells: Sequence[str]) -> str:
    payload = "\x1f".join((archive_sha256, source_entry, str(ordinal), *[str(cell) for cell in cells]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stage_row(
    *, archive_path: Path, source_entry: str, batch_id: str, archive_sha256: str,
    source_code: str, code: str, decision: str, dt: datetime,
    open_: float, high: float, low: float, close: float, volume: float, amount: float,
    row_hash: str, ingested_at: datetime,
) -> list[Any]:
    return [
        OCEAN_SOURCE, OCEAN_SOURCE_CATEGORY, str(archive_path), source_entry, batch_id,
        archive_sha256, source_code, code, "stock", decision, "5m", dt.date(), dt,
        open_, high, low, close, volume, amount, "none", row_hash, ingested_at,
        int.from_bytes(bytes.fromhex(row_hash[:16]), "big"),
    ]


def _drain_text(text: io.TextIOBase) -> None:
    for _ in text:
        pass


def _audit_status(
    *, decision: str, entry_reason: str, stats: _DayStats,
) -> tuple[str, str]:
    if entry_reason:
        return "isolated", entry_reason
    if decision == "historical_stock_pending":
        return "isolated", "pending_stock_not_in_accepted_whitelist"
    if decision != "accepted_stock":
        return "isolated", "source_code_not_classified_as_stock"
    reasons: list[str] = []
    if stats.accepted_row_count != EXPECTED_BAR_COUNT:
        reasons.append(f"accepted_bars={stats.accepted_row_count}; expected={EXPECTED_BAR_COUNT}")
    if len(set(stats.datetimes)) != EXPECTED_BAR_COUNT:
        reasons.append(f"unique_datetime={len(set(stats.datetimes))}; expected={EXPECTED_BAR_COUNT}")
    if stats.invalid_row_count:
        reasons.append(f"invalid_rows={stats.invalid_row_count}")
    if stats.quantity_invalid_row_count:
        reasons.append(f"quantity_invalid_rows={stats.quantity_invalid_row_count}")
    expected_slots = set(STANDARD_5M_MINUTES)
    actual_slots = {item.hour * 60 + item.minute for item in stats.datetimes}
    if actual_slots != expected_slots:
        missing = sorted(expected_slots - actual_slots)
        unexpected = sorted(actual_slots - expected_slots)
        reasons.append(f"standard_5m_slots_mismatch; missing={missing}; unexpected={unexpected}")
    return ("pass", "") if not reasons else ("review", "; ".join(reasons))


def parse_ocean_entry(
    archive: ZipFile,
    info: ZipInfo,
    *,
    archive_path: Path,
    archive_year: int,
    batch_id: str,
    archive_sha256: str,
    accepted_stock_codes: frozenset[str],
    accepted_stock_policy: str,
    parser_contract_version: str = OCEAN_PARSE_CONTRACT_VERSION,
    stage_schema_version: str = OCEAN_STAGE_SCHEMA_VERSION,
    ingested_at: datetime | None = None,
) -> OceanEntryResult:
    """Stream one CSV member, validating it without touching ClickHouse."""

    source_entry = zip_member_name(info)
    source_code = source_code_from_entry(source_entry)
    code = canonical_stock(source_code) or ""
    entry_year = _entry_year(source_entry)
    decision = "accepted_stock" if code and code in accepted_stock_codes else (
        "historical_stock_pending" if code else "rejected_unclassified"
    )
    security_type = "stock" if code else "unknown"
    entry_reason = "" if entry_year == archive_year else "entry_year_mismatch"
    if entry_year is None:
        entry_reason = "entry_name_missing_year"
    if ingested_at is None:
        ingested_at = datetime.now(BUSINESS_TZ).replace(microsecond=0)

    stage_rows: list[list[Any]] = []
    day_stats: dict[date, _DayStats] = {}
    row_code_mismatch = False
    row_shape_or_datetime_error = False
    datetime_year_mismatch = False
    format_reason = ""

    with archive.open(info, "r") as binary:
        text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
        try:
            reader = csv.reader(text)
            header = next(reader, [])
            if tuple(header) != EXPECTED_HEADER:
                format_reason = "unsupported_header"
                _drain_text(text)
            else:
                for ordinal, cells in enumerate(reader, start=1):
                    if len(cells) != len(EXPECTED_HEADER):
                        row_shape_or_datetime_error = True
                        continue
                    dt = parse_datetime(cells[0])
                    if dt is None:
                        row_shape_or_datetime_error = True
                        continue
                    stats = day_stats.setdefault(dt.date(), _DayStats())
                    stats.source_row_count += 1
                    if dt.year != archive_year:
                        datetime_year_mismatch = True
                        stats.invalid_row_count += 1
                        continue
                    if cells[1].strip().lower() != source_code.strip().lower():
                        row_code_mismatch = True
                        stats.invalid_row_count += 1
                        continue
                    open_, close, high, low = (parse_float(cells[index]) for index in (3, 4, 5, 6))
                    if (
                        any(value is None or value <= 0 for value in (open_, close, high, low))
                        or high < max(open_, close)
                        or low > min(open_, close)
                    ):
                        stats.invalid_row_count += 1
                        continue
                    stats.valid_ohlc_row_count += 1
                    stats.datetimes.append(dt)
                    volume = parse_float(cells[7])
                    amount = parse_float(cells[8])
                    quantity_invalid = (
                        volume is None or volume < 0 or not float(volume).is_integer()
                        or amount is None or amount < 0
                    )
                    if quantity_invalid:
                        stats.invalid_row_count += 1
                        stats.quantity_invalid_row_count += 1
                        continue
                    if decision != "accepted_stock":
                        continue
                    row_hash = _row_hash(archive_sha256, source_entry, ordinal, cells)
                    stage_rows.append(_stage_row(
                        archive_path=archive_path, source_entry=source_entry, batch_id=batch_id,
                        archive_sha256=archive_sha256, source_code=source_code, code=code,
                        decision=decision, dt=dt, open_=float(open_), high=float(high),
                        low=float(low), close=float(close), volume=float(volume),
                        amount=float(amount), row_hash=row_hash, ingested_at=ingested_at,
                    ))
                    stats.accepted_row_count += 1
        finally:
            # Iterating to EOF is what makes ZipExtFile verify the member CRC.
            text.detach()

    if row_code_mismatch:
        entry_reason = "csv_code_does_not_match_filename"
        stage_rows = []
    elif row_shape_or_datetime_error:
        entry_reason = "invalid_row_shape_or_timestamp"
        stage_rows = []
    elif datetime_year_mismatch:
        entry_reason = "datetime_year_mismatch"
        stage_rows = []
    if format_reason:
        entry_reason = format_reason
        stage_rows = []
    if not day_stats and not entry_reason:
        entry_reason = "empty_csv_no_data"
    if entry_reason:
        stage_rows = []

    audits: list[OceanDayAudit] = []
    for trade_date, stats in sorted(day_stats.items()):
        status, reason = _audit_status(decision=decision, entry_reason=entry_reason, stats=stats)
        audits.append(OceanDayAudit(
            source=OCEAN_SOURCE, source_category=OCEAN_SOURCE_CATEGORY,
            source_archive=str(archive_path), source_entry=source_entry, batch_id=batch_id,
            archive_sha256=archive_sha256, source_code=source_code, code=code,
            security_type=security_type, universe_decision=decision, trade_date=trade_date,
            source_row_count=stats.source_row_count, valid_ohlc_row_count=stats.valid_ohlc_row_count,
            accepted_row_count=stats.accepted_row_count, unique_datetime_count=len(set(stats.datetimes)),
            invalid_row_count=stats.invalid_row_count, quantity_invalid_row_count=stats.quantity_invalid_row_count,
            status=status, reason=reason, min_datetime=min(stats.datetimes) if stats.datetimes else None,
            max_datetime=max(stats.datetimes) if stats.datetimes else None, audited_at=ingested_at,
        ))

    if entry_reason:
        entry_status = "isolated"
    elif decision != "accepted_stock":
        entry_status = "isolated_pending"
    elif any(a.status == "review" for a in audits):
        entry_status = "review"
    else:
        entry_status = "accepted_stage"
    return OceanEntryResult(
        batch_id=batch_id, archive_sha256=archive_sha256, source_archive=str(archive_path),
        accepted_stock_policy_hash=accepted_stock_policy,
        parser_contract_version=parser_contract_version, stage_schema_version=stage_schema_version,
        source_entry=source_entry, source_code=source_code, code=code,
        security_type=security_type, universe_decision=decision, entry_status=entry_status,
        reason=entry_reason, stage_rows=stage_rows, day_audits=audits,
    )


def _drain_member(archive: ZipFile, info: ZipInfo, chunk_size: int = 1024 * 1024) -> None:
    with archive.open(info, "r") as stream:
        while stream.read(chunk_size):
            pass


def stage_verified_archive(
    archive_path: Path,
    *,
    accepted_stock_codes: Iterable[str],
    sink: OceanStageSink | None = None,
    ingested_at: datetime | None = None,
) -> OceanArchiveReport:
    """Verify and stage an archive; no database is touched when sink is None."""

    archive_path = archive_path.expanduser()
    verified: VerifiedArchive = verify_ready(archive_path)
    inspection = inspect_zip(archive_path)
    del inspection  # The full member safety scan is intentionally a gate only.
    archive_year = _archive_year(archive_path)
    batch_id = f"{OCEAN_SOURCE}_{archive_year}_{verified.sha256[:12]}"
    whitelist = normalize_stock_whitelist(accepted_stock_codes)
    policy_hash = accepted_stock_policy_hash(whitelist)
    report = OceanArchiveReport(
        str(archive_path), batch_id, verified.sha256,
        accepted_stock_policy_hash=policy_hash,
    )

    with ZipFile(archive_path, "r") as archive:
        for info in archive.infolist():
            name = zip_member_name(info)
            if name.endswith("/"):
                continue
            if name.lower().endswith(".csv"):
                result = parse_ocean_entry(
                    archive, info, archive_path=archive_path, archive_year=archive_year,
                    batch_id=batch_id, archive_sha256=verified.sha256,
                    accepted_stock_codes=whitelist, accepted_stock_policy=policy_hash,
                    ingested_at=ingested_at,
                )
                if sink is not None:
                    sink.write_entry(result)
                report.csv_entries += 1
                report.crc_verified_entries += 1
                report.stage_rows += result.accepted_rows
                report.audit_days += len(result.day_audits)
                report.pass_days += sum(a.status == "pass" for a in result.day_audits)
                report.isolated_days += sum(a.status == "isolated" for a in result.day_audits)
                report.review_days += sum(a.status == "review" for a in result.day_audits)
                report.pending_entries += result.entry_status == "isolated_pending"
                report.rejected_entries += result.entry_status == "isolated"
            else:
                _drain_member(archive, info)
                report.non_csv_entries += 1
                report.crc_verified_entries += 1
    report.structure_pass = bool(
        report.csv_entries
        and report.audit_days
        and report.stage_rows
        and report.pass_days == report.audit_days
        and report.review_days == 0
        and report.isolated_days == 0
        and report.pending_entries == 0
        and report.rejected_entries == 0
    )
    return report


class MemoryOceanStageSink:
    """Test sink implementing batch+entry+row_hash idempotency."""

    def __init__(self) -> None:
        self.stage_rows: dict[tuple[str, str, str], list[Any]] = {}
        self.day_audits: dict[tuple[str, str, date], list[Any]] = {}

    def write_entry(self, result: OceanEntryResult) -> None:
        for row in result.stage_rows:
            values = dict(zip(STAGE_COLUMNS, row))
            key = (str(values["batch_id"]), str(values["source_entry"]), str(values["row_hash"]))
            self.stage_rows.setdefault(key, row)
        for audit in result.day_audits:
            values = dict(zip(DAY_AUDIT_COLUMNS, audit.values()))
            key = (str(values["batch_id"]), str(values["source_entry"]), values["trade_date"])
            self.day_audits.setdefault(key, audit.values())


class ClickHouseOceanStageSink:
    """Fail-closed placeholder until DB execution is explicitly approved."""

    def __init__(self, client: Any) -> None:
        del client
        raise OceanStageError(
            "clickhouse_ocean_sink_disabled: no DB writes, DDL, memory settings, "
            "post-write verification, or single-writer lock are enabled"
        )


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_jsonl(
    path: Path,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    compressed: bool = False,
) -> int:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    count = 0
    try:
        if compressed:
            with temporary.open("wb") as raw_handle:
                with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as gzip_handle:
                    with io.TextIOWrapper(gzip_handle, encoding="utf-8", newline="\n") as handle:
                        for row in rows:
                            payload = {column: _json_value(value) for column, value in zip(columns, row)}
                            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                            count += 1
                        handle.flush()
                raw_handle.flush()
                os.fsync(raw_handle.fileno())
        else:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    payload = {column: _json_value(value) for column, value in zip(columns, row)}
                    handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                    count += 1
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, path)
        return count
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


class LocalOceanStageSink:
    """Atomic per-archive/per-entry evidence sink independent of ClickHouse.

    A complete manifest is the commit marker.  Existing complete evidence is
    never overwritten; an incomplete directory is retained and fails closed.
    Only one CSV entry is materialized at a time, so an annual archive is not
    accumulated in memory.
    """

    def __init__(self, root: Path, *, min_free_bytes: int = 2 * 1024**3, compression: str = "gzip") -> None:
        if compression not in {"gzip", "none"}:
            raise ValueError(f"unsupported_local_sink_compression={compression}")
        self.root = Path(root)
        self.min_free_bytes = int(min_free_bytes)
        self.compression = compression
        self.lock_path = self.root / ".local_ocean_stage.lock"
        self._lock_handle: Any | None = None

    def _acquire_lock(self) -> None:
        if self._lock_handle is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            elif _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            self._lock_handle = handle
        except (OSError, IOError) as exc:
            handle.close()
            raise OceanStageError(f"local_sink_lock_busy={self.lock_path}") from exc

    def close(self) -> None:
        handle, self._lock_handle = self._lock_handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "LocalOceanStageSink":
        self._acquire_lock()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _entry_dir(self, result: OceanEntryResult) -> Path:
        entry_key = hashlib.sha256(result.source_entry.encode("utf-8")).hexdigest()[:16]
        # Keep Windows paths bounded; complete hashes remain in the manifest and
        # any prefix collision is rejected by the full identity check.
        return self.root / result.archive_sha256[:16] / result.accepted_stock_policy_hash[:16] / entry_key

    @staticmethod
    def _estimated_jsonl_bytes(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
        total = 0
        for row in rows:
            payload = {column: _json_value(value) for column, value in zip(columns, row)}
            total += len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        return total

    def write_entry(self, result: OceanEntryResult) -> None:
        self._acquire_lock()
        entry_dir = self._entry_dir(result)
        suffix = ".jsonl.gz" if self.compression == "gzip" else ".jsonl"
        stage_path = entry_dir / f"stage{suffix}"
        audit_path = entry_dir / f"day_audit{suffix}"
        manifest_path = entry_dir / "manifest.json"
        identity = {
            "archive_sha256": result.archive_sha256,
            "batch_id": result.batch_id,
            "source_entry": result.source_entry,
            "accepted_stock_policy_hash": result.accepted_stock_policy_hash,
            "parser_contract_version": result.parser_contract_version,
            "stage_schema_version": result.stage_schema_version,
        }
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if any(existing.get(key) != value for key, value in identity.items()):
                raise OceanStageError(f"local_manifest_identity_mismatch={result.source_entry}")
            for path, manifest_key in ((stage_path, "stage_sha256"), (audit_path, "audit_sha256")):
                if not path.is_file():
                    raise OceanStageError(f"local_manifest_file_missing={path}")
                if _sha256_path(path) != str(existing.get(manifest_key) or ""):
                    raise OceanStageError(f"local_manifest_file_hash_mismatch={path}")
            return
        if stage_path.exists() or audit_path.exists():
            raise OceanStageError(f"local_entry_incomplete={entry_dir}")

        estimated_bytes = (
            self._estimated_jsonl_bytes(STAGE_COLUMNS, result.stage_rows)
            + self._estimated_jsonl_bytes(DAY_AUDIT_COLUMNS, (audit.values() for audit in result.day_audits))
            + 16 * 1024
        )
        free_bytes = shutil.disk_usage(self.root).free
        if free_bytes < self.min_free_bytes + estimated_bytes:
            raise OceanStageError(
                f"local_free_disk_guard: free={free_bytes}; required_reserve={self.min_free_bytes}; "
                f"estimated_uncompressed_entry={estimated_bytes}"
            )

        entry_dir.mkdir(parents=True, exist_ok=True)
        stage_count = _atomic_jsonl(stage_path, STAGE_COLUMNS, result.stage_rows, compressed=self.compression == "gzip")
        audit_count = _atomic_jsonl(
            audit_path, DAY_AUDIT_COLUMNS, (audit.values() for audit in result.day_audits),
            compressed=self.compression == "gzip",
        )
        manifest = {
            **identity,
            "source": OCEAN_SOURCE,
            "source_category": OCEAN_SOURCE_CATEGORY,
            "source_archive": result.source_archive,
            "entry_status": result.entry_status,
            "reason": result.reason,
            "structure_pass": bool(result.entry_status == "accepted_stage" and audit_count > 0),
            "formal_authorized": False,
            "gate_scope": "structure_only",
            "compression": self.compression,
            "estimated_uncompressed_bytes": estimated_bytes,
            "stage_rows": stage_count,
            "audit_days": audit_count,
            "stage_file": stage_path.name,
            "stage_sha256": _sha256_path(stage_path),
            "audit_file": audit_path.name,
            "audit_sha256": _sha256_path(audit_path),
        }
        temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(manifest, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, manifest_path)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
