"""Read-only audits over local Ocean evidence.

No ClickHouse client or formal-table writer is imported here.  The functions
operate on one local entry at a time so an annual archive is never loaded as a
single in-memory dataset.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

from utils.ocean_stage import (
    OCEAN_PARSE_CONTRACT_VERSION,
    OCEAN_STAGE_SCHEMA_VERSION,
    STANDARD_5M_MINUTES,
)
from utils.purchased_5m_reconciliation import (
    PRICE_RELATIVE_TOLERANCE,
    RELATIVE_TOLERANCE,
    max_price_abs_diff,
    optional_float,
    price_relative_diff,
)


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
UNEXPECTED_TIMEZONE_POLICY = "convert_to_Asia_Shanghai; naive_assume_Asia_Shanghai"


class OceanAuditError(RuntimeError):
    """Local evidence is incomplete, tampered, or outside the audit contract."""


def canonical_datetime_key(value: Any) -> str:
    """Canonicalize keys to second precision in Asia/Shanghai.

    Naive values are explicitly interpreted as Shanghai business time.  Aware
    values in another timezone are explicitly converted, never compared by
    their original string representation.
    """

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise OceanAuditError(f"invalid_datetime_key={value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BUSINESS_TZ)
    else:
        parsed = parsed.astimezone(BUSINESS_TZ)
    # Do not truncate sub-second input: the reconciliation gate rejects it,
    # while dryrun key comparison must not silently turn two different rows
    # into the same formal key.
    return parsed.isoformat()


@dataclass(frozen=True)
class LocalOceanManifest:
    path: Path
    payload: Mapping[str, Any]

    @property
    def entry_dir(self) -> Path:
        return self.path.parent

    @property
    def compression(self) -> str:
        return str(self.payload.get("compression") or "none")

    def _file(self, key: str) -> Path:
        name = str(self.payload.get(key) or "")
        if not name or Path(name).name != name:
            raise OceanAuditError(f"unsafe_manifest_filename={name!r}")
        result = self.entry_dir / name
        if not result.is_file():
            raise OceanAuditError(f"manifest_file_missing={result}")
        return result

    def stage_path(self) -> Path:
        return self._file("stage_file")

    def audit_path(self) -> Path:
        return self._file("audit_file")

    def _verify_file(self, path: Path, key: str) -> None:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != str(self.payload.get(key) or ""):
            raise OceanAuditError(f"manifest_file_hash_mismatch={path}")

    def iter_stage_rows(self) -> Iterator[dict[str, Any]]:
        path = self.stage_path()
        self._verify_file(path, "stage_sha256")
        yield from _iter_jsonl(path, self.compression)

    def iter_day_audits(self) -> Iterator[dict[str, Any]]:
        path = self.audit_path()
        self._verify_file(path, "audit_sha256")
        yield from _iter_jsonl(path, self.compression)


def _iter_jsonl(path: Path, compression: str) -> Iterator[dict[str, Any]]:
    opener = gzip.open if compression == "gzip" else open
    with opener(path, "rt", encoding="utf-8", newline="\n") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OceanAuditError(f"invalid_jsonl={path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise OceanAuditError(f"jsonl_row_not_object={path}:{line_number}")
            yield value


def iter_local_manifests(
    root: Path,
    *,
    archive_sha256: str | None = None,
) -> Iterator[LocalOceanManifest]:
    """Yield only complete, structure-only manifests under a local root."""

    root = Path(root)
    scan_root = root / str(archive_sha256)[:16] if archive_sha256 else root
    for path in sorted(scan_root.rglob("manifest.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise OceanAuditError(f"manifest_not_object={path}")
        # The independent runtime root also stores immutable whitelist and
        # reconciliation evidence.  They are intentionally not stage entries;
        # unknown schemas are still rejected below rather than ignored.
        if payload.get("schema_version") in {
            "ocean-stock-whitelist-v1",
            "ocean-daily-reconciliation-v1",
        }:
            continue
        required = (
            "archive_sha256", "batch_id", "source_entry", "accepted_stock_policy_hash",
            "parser_contract_version", "stage_schema_version", "stage_file", "audit_file",
            "stage_sha256", "audit_sha256",
        )
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise OceanAuditError(f"manifest_identity_missing={path}:{missing}")
        if payload.get("formal_authorized") is not False or payload.get("gate_scope") != "structure_only":
            raise OceanAuditError(f"manifest_not_structure_only={path}")
        if payload.get("parser_contract_version") != OCEAN_PARSE_CONTRACT_VERSION:
            raise OceanAuditError(f"unsupported_parser_contract={path}")
        if payload.get("stage_schema_version") != OCEAN_STAGE_SCHEMA_VERSION:
            raise OceanAuditError(f"unsupported_stage_schema={path}")
        entry_hash = hashlib.sha256(str(payload["source_entry"]).encode("utf-8")).hexdigest()[:16]
        if path.parent.name != entry_hash:
            raise OceanAuditError(f"manifest_entry_path_mismatch={path}")
        if path.parent.parent.name != str(payload["accepted_stock_policy_hash"])[:16]:
            raise OceanAuditError(f"manifest_policy_path_mismatch={path}")
        if path.parent.parent.parent.name != str(payload["archive_sha256"])[:16]:
            raise OceanAuditError(f"manifest_archive_path_mismatch={path}")
        manifest = LocalOceanManifest(path, payload)
        manifest.stage_path()
        manifest.audit_path()
        yield manifest


def select_manifest_scope(
    manifests: Iterable[LocalOceanManifest],
    *,
    batch_id: str | None = None,
    policy_hash: str | None = None,
) -> list[LocalOceanManifest]:
    """Select exactly one batch/policy scope; never merge policies implicitly."""

    all_manifests = list(manifests)
    if batch_id is not None:
        all_manifests = [item for item in all_manifests if item.payload.get("batch_id") == batch_id]
    if policy_hash is not None:
        all_manifests = [
            item for item in all_manifests
            if item.payload.get("accepted_stock_policy_hash") == policy_hash
        ]
    groups = {
        (str(item.payload.get("batch_id")), str(item.payload.get("accepted_stock_policy_hash")))
        for item in all_manifests
    }
    if not all_manifests:
        raise OceanAuditError("no_local_manifest_in_selected_scope")
    if len(groups) != 1:
        raise OceanAuditError(f"multiple_batch_or_policy_scope={sorted(groups)}")
    return sorted(all_manifests, key=lambda item: str(item.payload.get("source_entry")))


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return _as_datetime(value).date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if "T" in text or " " in text:
        return _as_datetime(text).date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise OceanAuditError(f"invalid_trade_date={value!r}") from exc


def _as_datetime(value: Any) -> datetime:
    return datetime.fromisoformat(canonical_datetime_key(value))


def _as_float(value: Any) -> float | None:
    return optional_float(value)


@dataclass(frozen=True)
class OceanDayReconciliation:
    batch_id: str
    source_entry: str
    code: str
    trade_date: date
    minute_rows: int
    unique_datetime_rows: int
    daily_snapshot_rows: int
    daily_quality_status: str
    minute_volume_lots: float | None
    daily_volume_lots: float | None
    minute_amount_yuan: float | None
    daily_amount_yuan: float | None
    price_relative_diff: float | None
    volume_relative_diff: float | None
    amount_relative_diff: float | None
    structure_pass: bool
    overall_status: str
    reason: str
    daily_gate_pass: bool = False
    units_contract: str = "volume_lots/amount_yuan; raw_unadjusted_price"


def _finite_float(value: Any) -> float | None:
    return _as_float(value)


def _valid_positive_ohlc(row: Mapping[str, Any], *, prefix: str) -> list[str]:
    values = [_finite_float(row.get(name)) for name in ("open", "high", "low", "close")]
    reasons: list[str] = []
    if any(value is None or value <= 0 for value in values):
        reasons.append(f"{prefix}_positive_ohlc_failed")
        return reasons
    open_, high, low, close = (float(value) for value in values)  # type: ignore[arg-type]
    if high < max(open_, close) or low > min(open_, close):
        reasons.append(f"{prefix}_ohlc_relation_failed")
    return reasons


def _valid_nonnegative_quantities(row: Mapping[str, Any], *, prefix: str) -> list[str]:
    reasons: list[str] = []
    for name in ("source_volume_lots", "source_amount_yuan"):
        value = _finite_float(row.get(name))
        if value is None or value < 0:
            reasons.append(f"{prefix}_{name}_finite_nonnegative_failed")
    return reasons


def _audit_int(audit: Mapping[str, Any], name: str) -> int | None:
    value = audit.get(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _validate_day_audit(
    audit: Mapping[str, Any] | None,
    *,
    stage_row_count: int,
) -> list[str]:
    if audit is None:
        return ["day_audit_missing"]
    reasons: list[str] = []
    status = str(audit.get("status") or "missing")
    if status != "pass":
        reasons.append(f"day_audit_status={status}")
    if str(audit.get("security_type") or "") != "stock":
        reasons.append("day_audit_security_type_not_stock")
    if str(audit.get("universe_decision") or "") != "accepted_stock":
        reasons.append("day_audit_universe_not_accepted_stock")

    expected = _audit_int(audit, "expected_bar_count")
    source = _audit_int(audit, "source_row_count")
    accepted = _audit_int(audit, "accepted_row_count")
    unique = _audit_int(audit, "unique_datetime_count")
    invalid = _audit_int(audit, "invalid_row_count")
    quantity_invalid = _audit_int(audit, "quantity_invalid_row_count")
    if expected != 48:
        reasons.append(f"day_audit_expected_bar_count={expected}; expected=48")
    if source is None:
        reasons.append("day_audit_source_row_count_invalid")
    if accepted is None:
        reasons.append("day_audit_accepted_row_count_invalid")
    if unique is None:
        reasons.append("day_audit_unique_datetime_count_invalid")
    if invalid is None:
        reasons.append("day_audit_invalid_row_count_invalid")
    elif invalid:
        reasons.append(f"day_audit_invalid_rows={invalid}")
    if quantity_invalid is None:
        reasons.append("day_audit_quantity_invalid_row_count_invalid")
    elif quantity_invalid:
        reasons.append(f"day_audit_quantity_invalid_rows={quantity_invalid}")
    if source is not None and accepted is not None and source != accepted:
        reasons.append(f"day_audit_source_accepted_mismatch={source}!={accepted}")
    if accepted is not None and accepted != stage_row_count:
        reasons.append(f"day_audit_stage_mismatch={accepted}!={stage_row_count}")
    if unique is not None and unique != 48:
        reasons.append(f"day_audit_unique_datetime={unique}; expected=48")
    return reasons


def _daily_key(value: tuple[Any, Any]) -> tuple[str, date]:
    return str(value[0]), _as_date(value[1])


def reconcile_stage_rows(
    rows: Iterable[Mapping[str, Any]],
    daily_snapshot: Mapping[tuple[str, date], Mapping[str, Any]],
    *,
    day_audits: Iterable[Mapping[str, Any]] | None = None,
    price_tolerance: float = PRICE_RELATIVE_TOLERANCE,
    relative_tolerance: float = RELATIVE_TOLERANCE,
    amount_tolerance: float | None = None,
) -> list[OceanDayReconciliation]:
    """Reconcile local stage rows plus original day-audit evidence.

    ``stage_rows`` are not treated as the source of truth for row counts:
    ``day_audits`` is merged in so a 48-row filtered stage cannot hide a
    49-row source day with one invalid record.  This remains a read-only,
    structure-and-snapshot audit; it never authorizes formal promotion.
    """

    if amount_tolerance is None:
        amount_tolerance = relative_tolerance
    if amount_tolerance < 0 or relative_tolerance < 0 or price_tolerance < 0:
        raise ValueError("tolerances_must_be_non_negative")

    grouped: dict[tuple[str, date], list[Mapping[str, Any]]] = {}
    for row in rows:
        code = str(row.get("code") or "")
        trade_date = _as_date(row.get("trade_date") or row.get("datetime"))
        grouped.setdefault((code, trade_date), []).append(row)

    audit_by_key: dict[tuple[str, date], Mapping[str, Any]] = {}
    for audit in day_audits or ():
        code = str(audit.get("code") or "")
        trade_date = _as_date(audit.get("trade_date"))
        key = (code, trade_date)
        if key in audit_by_key:
            raise OceanAuditError(f"duplicate_day_audit_key={key}")
        audit_by_key[key] = audit

    all_keys = sorted(set(grouped) | set(audit_by_key))

    results: list[OceanDayReconciliation] = []
    for (code, trade_date) in all_keys:
        values = grouped.get((code, trade_date), [])
        audit = audit_by_key.get((code, trade_date))
        ordered = sorted(values, key=lambda row: _as_datetime(row["datetime"]))
        datetimes: list[datetime] = []
        actual_slots = {item.hour * 60 + item.minute for item in datetimes}
        structure_reasons: list[str] = []
        if len(ordered) != 48:
            structure_reasons.append(f"minute_rows={len(ordered)}; expected=48")

        row_validation_reasons: list[str] = []
        for row in ordered:
            dt = _as_datetime(row.get("datetime"))
            datetimes.append(dt)
            if dt.date() != trade_date:
                row_validation_reasons.append("datetime_trade_date_mismatch")
            if dt.second != 0 or dt.microsecond != 0:
                row_validation_reasons.append("datetime_seconds_not_zero")
            if str(row.get("security_type") or "") != "stock":
                row_validation_reasons.append("stage_security_type_not_stock")
            if str(row.get("universe_decision") or "") != "accepted_stock":
                row_validation_reasons.append("stage_universe_not_accepted_stock")
            row_validation_reasons.extend(_valid_positive_ohlc(row, prefix="minute"))
            row_validation_reasons.extend(_valid_nonnegative_quantities(row, prefix="minute"))

        unique_datetime_rows = len(set(datetimes))
        actual_slots = {item.hour * 60 + item.minute for item in datetimes}
        if unique_datetime_rows != 48:
            structure_reasons.append(f"unique_datetime={unique_datetime_rows}; expected=48")
        if actual_slots != set(STANDARD_5M_MINUTES):
            missing = sorted(set(STANDARD_5M_MINUTES) - actual_slots)
            unexpected = sorted(actual_slots - set(STANDARD_5M_MINUTES))
            structure_reasons.append(
                f"standard_5m_slots_mismatch; missing={missing}; unexpected={unexpected}"
            )
        structure_reasons.extend(row_validation_reasons)
        structure_reasons.extend(_validate_day_audit(audit, stage_row_count=len(ordered)))
        structure_pass = not structure_reasons

        daily = daily_snapshot.get((code, trade_date))
        daily_rows = 1 if daily is not None else 0
        daily_quality = str(daily.get("quality_status") or "missing") if daily else "missing"
        minute_ohlc_valid = bool(ordered) and not any(
            _valid_positive_ohlc(row, prefix="minute") for row in ordered
        )
        minute_prices = (
            (_as_float(ordered[0].get("open")), max(float(_as_float(row.get("high"))) for row in ordered),
             min(float(_as_float(row.get("low"))) for row in ordered), _as_float(ordered[-1].get("close")))
            if minute_ohlc_valid else (None,) * 4
        )
        daily_prices = tuple(_as_float(daily.get(key)) for key in ("open", "high", "low", "close")) if daily else (None,) * 4
        price_diff = max_price_abs_diff(minute_prices, daily_prices)
        price_ratio = price_relative_diff(price_diff, daily_prices)
        minute_quantities_valid = bool(ordered) and not any(
            _valid_nonnegative_quantities(row, prefix="minute") for row in ordered
        )
        minute_volume = (
            sum(float(_as_float(row.get("source_volume_lots"))) for row in ordered)
            if minute_quantities_valid else None
        )
        minute_amount = (
            sum(float(_as_float(row.get("source_amount_yuan"))) for row in ordered)
            if minute_quantities_valid else None
        )
        daily_volume = _as_float(daily.get("volume_lots")) if daily else None
        daily_amount = _as_float(daily.get("amount_yuan")) if daily else None
        daily_reasons: list[str] = []
        if daily_rows != 1:
            daily_reasons.append("daily_snapshot=missing")
        elif daily_quality != "valid":
            daily_reasons.append(f"daily_snapshot={daily_quality}")
        if daily is not None:
            if str(daily.get("security_type") or "") != "stock":
                daily_reasons.append("daily_security_type_not_stock")
            daily_reasons.extend(_valid_positive_ohlc(daily, prefix="daily"))
            for name in ("volume_lots", "amount_yuan"):
                value = _finite_float(daily.get(name))
                if value is None or value < 0:
                    daily_reasons.append(f"daily_{name}_finite_nonnegative_failed")
        daily_gate_pass = not daily_reasons
        volume_ratio = (
            None if minute_volume is None or daily_volume is None
            else abs(minute_volume - daily_volume) / max(abs(daily_volume), 1.0)
        )
        amount_ratio = (
            None if minute_amount is None or daily_amount is None
            else abs(minute_amount - daily_amount) / max(abs(daily_amount), 1.0)
        )

        reasons = list(structure_reasons) + list(daily_reasons)
        if not daily_gate_pass:
            pass
        elif price_ratio is None or price_ratio > price_tolerance:
            reasons.append(f"price_relative_diff={price_ratio}")
        if daily_gate_pass and (volume_ratio is None or volume_ratio > relative_tolerance):
            reasons.append(f"volume_relative_diff={volume_ratio}")
        if daily_gate_pass and (amount_ratio is None or amount_ratio > amount_tolerance):
            reasons.append(f"amount_relative_diff={amount_ratio}")
        first = ordered[0] if ordered else audit or {}
        results.append(OceanDayReconciliation(
            batch_id=str(first.get("batch_id") or ""),
            source_entry=str(first.get("source_entry") or ""), code=code,
            trade_date=trade_date, minute_rows=len(ordered), unique_datetime_rows=unique_datetime_rows,
            daily_snapshot_rows=daily_rows, daily_quality_status=daily_quality,
            minute_volume_lots=minute_volume, daily_volume_lots=daily_volume,
            minute_amount_yuan=minute_amount, daily_amount_yuan=daily_amount,
            price_relative_diff=price_ratio, volume_relative_diff=volume_ratio,
            amount_relative_diff=amount_ratio, structure_pass=structure_pass,
            overall_status="pass" if not reasons else "review", reason="; ".join(reasons),
            daily_gate_pass=daily_gate_pass,
        ))
    return results


def find_code_date_conflicts(
    reconciliations: Iterable[OceanDayReconciliation],
) -> tuple[tuple[str, date], ...]:
    """Return code/date keys represented by more than one source entry."""

    owners: dict[tuple[str, date], tuple[str, str]] = {}
    conflicts: set[tuple[str, date]] = set()
    for item in reconciliations:
        key = (item.code, item.trade_date)
        owner = (item.batch_id, item.source_entry)
        previous = owners.setdefault(key, owner)
        if previous != owner:
            conflicts.add(key)
    return tuple(sorted(conflicts))


@dataclass(frozen=True)
class MissingKeyDryRun:
    candidate_rows: int
    candidate_unique_keys: int
    existing_formal_keys: int | None
    missing_keys: tuple[tuple[str, str], ...] | None
    duplicate_candidate_keys: tuple[tuple[str, str], ...]
    missing_keys_status: str


def dryrun_missing_formal_keys(
    candidate_rows: Iterable[Mapping[str, Any]],
    existing_formal_keys: Iterable[tuple[str, Any]] | None,
) -> MissingKeyDryRun:
    """Calculate missing formal keys only; never mutates or connects to a DB."""

    existing = (
        {(str(code), canonical_datetime_key(value)) for code, value in existing_formal_keys}
        if existing_formal_keys is not None else None
    )
    seen: set[tuple[str, str]] = set()
    missing: list[tuple[str, str]] = []
    duplicates: list[tuple[str, str]] = []
    candidate_count = 0
    for row in candidate_rows:
        candidate_count += 1
        key = (str(row.get("code") or ""), canonical_datetime_key(row.get("datetime")))
        if key in seen:
            duplicates.append(key)
            continue
        seen.add(key)
        if existing is not None and key not in existing:
            missing.append(key)
    return MissingKeyDryRun(
        candidate_rows=candidate_count, candidate_unique_keys=len(seen),
        existing_formal_keys=None if existing is None else len(existing),
        missing_keys=None if existing is None else tuple(missing),
        duplicate_candidate_keys=tuple(duplicates),
        missing_keys_status="not_checked" if existing is None else "checked",
    )


def build_daily_snapshot_lookup(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, date], dict[str, Any]]:
    """Build a daily lookup without allowing duplicate keys to overwrite evidence."""

    result: dict[tuple[str, date], dict[str, Any]] = {}
    counts: dict[tuple[str, date], int] = {}
    for row in rows:
        normalized = dict(row)
        normalized["trade_date"] = _as_date(normalized.get("trade_date"))
        key = (str(normalized.get("code") or ""), normalized["trade_date"])
        counts[key] = counts.get(key, 0) + 1
        if key in result:
            raise OceanAuditError(
                f"daily_snapshot_duplicate_key={key}; daily_snapshot_rows={counts[key]}"
            )
        result[key] = normalized
    return result


class DailySnapshotReader:
    """Read the smallest useful Parquet slice without retaining a full-year table.

    The reconciliation worker normally supplies one entry's code set.  When the
    snapshot is partitioned by ``trade_year=YYYY`` we open only that partition
    and issue one predicate scan for the entry/year; the returned table is then
    filtered in Python by day.  At most one dataset handle is retained and the
    materialised rows are bounded by the entry's code set rather than by the
    whole snapshot year.  The old ``load_day`` API remains available for small
    callers and uses the same year-local path when possible.
    """

    def __init__(self, root: Path, *, max_result_bytes: int = 64 * 1024 * 1024) -> None:
        import pyarrow.dataset as ds

        self.root = Path(root)
        self._ds_module = ds
        self.max_result_bytes = int(max_result_bytes)
        self._year: int | None = None
        self._year_ds: Any | None = None

    def _dataset_for_year(self, year: int) -> Any:
        if self._year == year and self._year_ds is not None:
            return self._year_ds
        year_root = self.root / f"trade_year={int(year)}"
        if year_root.is_dir():
            dataset = self._ds_module.dataset(year_root, format="parquet")
        else:
            # Compatibility fallback for snapshots without year directories.
            # The predicate below still constrains the scan to one year.
            if not self.root.exists():
                raise OceanAuditError(f"daily_snapshot_root_missing={self.root}")
            dataset = self._ds_module.dataset(self.root, format="parquet", partitioning="hive")
        self._year = int(year)
        self._year_ds = dataset
        return dataset

    @staticmethod
    def _columns() -> list[str]:
        return [
            "code", "security_type", "quality_status", "trade_date",
            "open", "high", "low", "close", "volume_lots", "amount_yuan",
        ]

    def load_code_year(
        self,
        year: int,
        *,
        codes: Iterable[str],
    ) -> dict[tuple[str, date], dict[str, Any]]:
        """Load one entry's code set for one year in one bounded Parquet scan."""

        ds = self._ds_module
        code_values = sorted({str(code) for code in codes if str(code)})
        if not code_values:
            return {}
        predicate = ds.field("security_type") == "stock"
        predicate = predicate & ds.field("code").isin(code_values)
        year_root = self.root / f"trade_year={int(year)}"
        if not year_root.is_dir():
            predicate = predicate & (ds.field("trade_date") >= date(int(year), 1, 1))
            predicate = predicate & (ds.field("trade_date") < date(int(year) + 1, 1, 1))
        scanner = self._dataset_for_year(int(year)).scanner(
            columns=self._columns(),
            filter=predicate,
            batch_size=4096,
        )
        rows: list[dict[str, Any]] = []
        result_bytes = 0
        for batch in scanner.to_batches():
            result_bytes += int(getattr(batch, "nbytes", 0))
            if result_bytes > self.max_result_bytes:
                raise OceanAuditError(
                    f"daily_snapshot_result_too_large={result_bytes}>{self.max_result_bytes};year={year}"
                )
            rows.extend(batch.to_pylist())
        return build_daily_snapshot_lookup(rows)

    def load_day(
        self,
        trade_date: date,
        *,
        codes: Iterable[str] | None = None,
    ) -> dict[tuple[str, date], dict[str, Any]]:
        code_values = sorted({str(code) for code in codes or () if str(code)})
        if code_values:
            year_rows = self.load_code_year(trade_date.year, codes=code_values)
            return {
                key: row for key, row in year_rows.items()
                if key[1] == trade_date
            }
        ds = self._ds_module
        predicate = (ds.field("trade_date") == trade_date) & (ds.field("security_type") == "stock")
        scanner = self._dataset_for_year(trade_date.year).scanner(
            columns=self._columns(),
            filter=predicate,
            batch_size=4096,
        )
        rows: list[dict[str, Any]] = []
        result_bytes = 0
        for batch in scanner.to_batches():
            result_bytes += int(getattr(batch, "nbytes", 0))
            if result_bytes > self.max_result_bytes:
                raise OceanAuditError(
                    f"daily_snapshot_result_too_large={result_bytes}>{self.max_result_bytes};date={trade_date}"
                )
            rows.extend(batch.to_pylist())
        return build_daily_snapshot_lookup(rows)


def load_daily_snapshot_day(root: Path, trade_date: date) -> dict[tuple[str, date], dict[str, Any]]:
    """Read one Parquet snapshot day; this is intentionally read-only and bounded."""

    return DailySnapshotReader(root).load_day(trade_date)
