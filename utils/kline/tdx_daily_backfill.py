"""TDX candidate planning helpers for guarded daily-kline backfills.

This module is intentionally limited to immutable plan construction and row
formatting. The production apply script owns the explicit ClickHouse writes,
readbacks, and rollback journal.
"""

from __future__ import annotations

import csv
import hashlib
import ipaddress
import json
import math
import os
import re
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import blake2b, sha256
from numbers import Integral
from pathlib import Path
from socket import gethostname
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from utils.daily_date_universe import (
    BUSINESS_TZ,
    DateUniverseContractError,
    DateUniversePayload,
    build_date_universe_payload,
    date_set_sha256,
    normalize_code,
    normalize_trade_date,
)
from utils.security_market_policy import assert_no_neeq_codes

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
SOURCE_VOLUME_UNIT = "lots (TDX shares / 100)"
SOURCE_AMOUNT_UNIT = "yuan"
KLINE_DAILY_TDX_COLUMNS = (
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "change_pct",
    "change_amount",
    "turnover_rate",
    "created_at",
    "id",
)
MEMBER_COLUMNS = (
    "dataset_id",
    "code",
    "trade_date",
    "member_ordinal",
    "date_set_sha256",
    "published_at",
)
REGISTRY_COLUMNS = (
    "dataset_id",
    "code",
    "security_type",
    "identity_status",
    "source_kind",
    "source_ref",
    "source_sha256",
    "coverage_start",
    "coverage_end",
    "completeness_state",
    "date_set_sha256",
    "member_count",
    "published_at",
    "is_active",
)


def parse_windows_tcp_listener_addresses(output: str, port: int) -> tuple[str, ...]:
    """Extract local IPs for TCP LISTENING sockets on one port from netstat output."""
    try:
        target_port = int(port)
    except (TypeError, ValueError) as exc:
        raise DateUniverseContractError("ClickHouse listener port must be an integer") from exc
    if not 1 <= target_port <= 65535:
        raise DateUniverseContractError("ClickHouse listener port is outside the TCP port range")

    addresses = set()
    for line in str(output).splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0].upper() != "TCP" or fields[-2].upper() != "LISTENING":
            continue
        local_address, separator, port_text = fields[1].rpartition(":")
        if not separator or not port_text.isdigit() or int(port_text) != target_port:
            continue
        local_address = local_address.strip("[]")
        try:
            parsed_address = ipaddress.ip_address(local_address)
        except ValueError as exc:
            raise DateUniverseContractError("netstat returned an invalid ClickHouse listener address") from exc
        addresses.add(str(parsed_address))
    return tuple(sorted(addresses))


def validate_single_writer_deployment(
    *,
    clickhouse_url: str,
    default_cluster_rows: Sequence[Mapping[str, Any]],
    listener_addresses: Sequence[str],
) -> dict[str, Any]:
    """Accept only a locally bound, single-node ClickHouse deployment."""
    try:
        endpoint = urlsplit(str(clickhouse_url))
        host = endpoint.hostname
        port = endpoint.port
    except ValueError as exc:
        raise DateUniverseContractError("invalid ClickHouse endpoint URL") from exc
    if endpoint.scheme != "http" or not host or endpoint.username or endpoint.password:
        raise DateUniverseContractError("apply requires an unauthenticated loopback HTTP endpoint")
    if endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment:
        raise DateUniverseContractError("ClickHouse endpoint URL must not contain a path, query, or fragment")
    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise DateUniverseContractError("ClickHouse endpoint must use a numeric loopback IP") from exc
    if not host_ip.is_loopback:
        raise DateUniverseContractError("ClickHouse endpoint is not loopback-only")
    if port != 8123:
        raise DateUniverseContractError("single-writer deployment requires the verified HTTP port 8123")

    clusters = list(default_cluster_rows)
    if len(clusters) != 1:
        raise DateUniverseContractError("default ClickHouse cluster must contain exactly one local server")
    cluster = clusters[0]
    cluster_host = str(cluster.get("host_name") or "").strip().lower()
    if str(cluster.get("cluster") or "") != "default" or int(cluster.get("is_local") or 0) != 1:
        raise DateUniverseContractError("default ClickHouse cluster is not a single local server")
    if cluster_host == "localhost":
        pass
    else:
        try:
            if not ipaddress.ip_address(cluster_host).is_loopback:
                raise DateUniverseContractError("default ClickHouse server hostname is not loopback")
        except ValueError as exc:
            raise DateUniverseContractError("default ClickHouse server hostname is not loopback") from exc

    addresses = []
    for address in listener_addresses:
        try:
            parsed_address = ipaddress.ip_address(str(address).strip().strip("[]"))
        except ValueError as exc:
            raise DateUniverseContractError("ClickHouse listener address is not a numeric IP") from exc
        if not parsed_address.is_loopback:
            raise DateUniverseContractError("ClickHouse HTTP listener is reachable beyond loopback")
        addresses.append(str(parsed_address))
    if not addresses:
        raise DateUniverseContractError("no ClickHouse HTTP listener was verified")
    return {
        "mode": "single_writer_local",
        "endpoint_host": str(host_ip),
        "endpoint_port": port,
        "default_cluster_host": cluster_host,
        "listener_addresses": tuple(sorted(set(addresses))),
    }


class TdxDailyBackfillApplyLock:
    """Nonblocking file lock serializing local backfill runner processes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise DateUniverseContractError("apply lock is already held by this instance")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(bytes(1))
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise DateUniverseContractError("daily backfill apply lock is already held") from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False


@dataclass(frozen=True)
class TdxDailyBackfillPlan:
    """Frozen exact-key/date-universe plan made from reviewed TDX candidates."""

    candidate_rows: tuple[dict[str, Any], ...]
    blocked_candidate_rows: tuple[dict[str, Any], ...]
    blocked_by_code: Mapping[str, str]
    payloads: tuple[DateUniversePayload, ...]
    retired_registry_rows: tuple[dict[str, Any], ...]
    candidate_sha256: str
    plan_sha256: str

    @property
    def keys(self) -> frozenset[tuple[str, date]]:
        return frozenset(
            (row["code"], normalize_trade_date(row["trade_date"]))
            for row in self.candidate_rows
        )

    @property
    def registry_rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(payload.registry.registry_row() for payload in self.payloads)

    @property
    def member_rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(row for payload in self.payloads for row in payload.member_rows)

    @property
    def dataset_ids(self) -> tuple[str, ...]:
        return tuple(payload.registry.dataset_id for payload in self.payloads)


def _finite_float(row: Mapping[str, Any], name: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    try:
        value = float(row[name])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DateUniverseContractError(f"candidate has invalid {name}") from exc
    if not math.isfinite(value):
        raise DateUniverseContractError(f"candidate has non-finite {name}")
    if positive and value <= 0:
        raise DateUniverseContractError(f"candidate {name} must be positive")
    if nonnegative and value < 0:
        raise DateUniverseContractError(f"candidate {name} must be nonnegative")
    return value


def _normalize_candidate_rows(
    candidate_rows: Iterable[Mapping[str, Any]],
    *,
    active_stock_codes: set[str],
    expected_trade_dates: set[date] | None,
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, date]] = set()
    for source in candidate_rows:
        row = dict(source)
        try:
            code = normalize_code(row.get("code"))
            trade_day = normalize_trade_date(row.get("trade_date"))
        except DateUniverseContractError as exc:
            raise DateUniverseContractError(f"invalid candidate key: {exc}") from exc
        key = (code, trade_day)
        if key in seen:
            raise DateUniverseContractError(f"duplicate candidate key: {code}@{trade_day}")
        seen.add(key)
        if code not in active_stock_codes:
            raise DateUniverseContractError(f"candidate code outside current active stock scope: {code}")
        assert_no_neeq_codes([code], context="TDX daily candidate")
        if expected_trade_dates is not None and trade_day not in expected_trade_dates:
            raise DateUniverseContractError(
                f"candidate date outside the reviewed target window: {code}@{trade_day}"
            )
        if trade_day < date(2000, 1, 1):
            raise DateUniverseContractError(f"candidate predates maintenance cutoff: {code}@{trade_day}")
        for name in ("open", "high", "low", "close"):
            _finite_float(row, name, positive=True)
        volume = _finite_float(row, "volume", positive=True)
        amount = _finite_float(row, "amount", positive=True)
        _finite_float(row, "amplitude", nonnegative=True)
        _finite_float(row, "change_pct")
        _finite_float(row, "change_amount")
        _finite_float(row, "turnover_rate", nonnegative=True)
        _finite_float(row, "prev_close_from_tdx", nonnegative=True)
        open_, high, low, close = (float(row[name]) for name in ("open", "high", "low", "close"))
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise DateUniverseContractError(f"candidate OHLC envelope invalid: {code}@{trade_day}")
        if str(row.get("source_volume_unit") or "").strip() != SOURCE_VOLUME_UNIT:
            raise DateUniverseContractError(f"candidate volume-unit contract mismatch: {code}@{trade_day}")
        if str(row.get("source_amount_unit") or "").strip() != SOURCE_AMOUNT_UNIT:
            raise DateUniverseContractError(f"candidate amount-unit contract mismatch: {code}@{trade_day}")
        try:
            overlap_rows = int(row.get("overlap_control_rows") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DateUniverseContractError(f"candidate overlap control count invalid: {code}@{trade_day}") from exc
        if overlap_rows < 1:
            raise DateUniverseContractError(f"candidate lacks formal overlap control: {code}@{trade_day}")
        file_ref = str(row.get("tdx_file") or "").strip()
        file_sha = str(row.get("tdx_file_sha256") or "").strip().lower()
        if not file_ref or not SHA256_PATTERN.fullmatch(file_sha):
            raise DateUniverseContractError(f"candidate source lineage is invalid: {code}@{trade_day}")
        row["code"] = code
        row["trade_date"] = trade_day.isoformat()
        row["tdx_file"] = file_ref
        row["tdx_file_sha256"] = file_sha
        row["overlap_control_rows"] = overlap_rows
        # Normalize every numeric field once; the writer will not round values.
        for name in (
            "open", "high", "low", "close", "volume", "amount", "amplitude",
            "change_pct", "change_amount", "turnover_rate", "prev_close_from_tdx",
        ):
            row[name] = float(row[name])
        normalized.append(row)
    if not normalized:
        raise DateUniverseContractError("TDX backfill candidate set is empty")
    normalized.sort(key=lambda item: (item["code"], item["trade_date"]))
    return tuple(normalized)


def _active_registry_rows_by_code(
    registry_rows: Iterable[Mapping[str, Any]], candidate_codes: set[str]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in registry_rows:
        row = dict(source)
        try:
            code = normalize_code(row.get("code"))
        except DateUniverseContractError:
            continue
        if code not in candidate_codes or int(row.get("is_active", 1) or 0) != 1:
            continue
        result[code].append(row)
    return result


def _existing_member_dates(
    registry: Mapping[str, Any], member_rows: Iterable[Mapping[str, Any]]
) -> tuple[tuple[date, ...] | None, str | None]:
    code = normalize_code(registry.get("code"))
    dataset_id = str(registry.get("dataset_id") or "").strip()
    expected_digest = str(registry.get("date_set_sha256") or "").strip()
    selected: list[dict[str, Any]] = []
    for source in member_rows:
        row = dict(source)
        if normalize_code(row.get("code")) == code and str(row.get("dataset_id") or "").strip() == dataset_id:
            selected.append(row)
    try:
        expected_count = int(registry.get("member_count", 0))
    except (TypeError, ValueError, OverflowError):
        return None, "active_registry_member_count_invalid"
    if not dataset_id or not expected_digest or expected_count <= 0:
        return None, "active_registry_identity_invalid"
    if len(selected) != expected_count:
        return None, "active_member_count_mismatch"
    dates: list[date] = []
    ordinals: list[int] = []
    for row in selected:
        try:
            trade_day = normalize_trade_date(row.get("trade_date"))
            ordinal = int(row.get("member_ordinal"))
        except (DateUniverseContractError, TypeError, ValueError, OverflowError):
            return None, "active_member_row_invalid"
        if str(row.get("date_set_sha256") or "").strip() != expected_digest:
            return None, "active_member_digest_field_mismatch"
        if str(row.get("code") or "").strip().upper() != code or str(row.get("dataset_id") or "").strip() != dataset_id:
            return None, "active_member_identity_mismatch"
        dates.append(trade_day)
        ordinals.append(ordinal)
    if len(set(dates)) != len(dates):
        return None, "active_member_duplicate_date"
    if sorted(ordinals) != list(range(1, expected_count + 1)):
        return None, "active_member_ordinal_mismatch"
    normalized_dates = tuple(sorted(dates))
    if date_set_sha256(code, normalized_dates) != expected_digest:
        return None, "active_member_digest_recompute_mismatch"
    return normalized_dates, None


def _source_digest(
    *,
    code: str,
    candidate_sha256: str,
    rows: Sequence[Mapping[str, Any]],
    old_registry: Mapping[str, Any] | None,
    target_dates: Sequence[date],
    plan_nonce: str,
) -> str:
    file_map = {
        str(row["tdx_file"]): str(row["tdx_file_sha256"]).lower()
        for row in rows
    }
    if len(set(file_map.values())) != 1 or len(file_map) != 1:
        raise DateUniverseContractError(f"candidate source file identity changed within {code}")
    source_record: dict[str, Any] = {
        "code": code,
        "candidate_csv_sha256": candidate_sha256,
        "tdx_files": file_map,
        "target_window_dates": [item.isoformat() for item in sorted(set(target_dates))],
        "plan_nonce": plan_nonce,
        "source_volume_unit": SOURCE_VOLUME_UNIT,
        "source_amount_unit": SOURCE_AMOUNT_UNIT,
        "parser_contract": "research.cross_strategy.import_local_tdx_daily_to_clickhouse.DAY_RECORD",
    }
    if old_registry is not None:
        source_record["previous_accepted_registry"] = {
            key: str(old_registry.get(key) or "")
            for key in (
                "dataset_id", "source_kind", "source_ref", "source_sha256",
                "date_set_sha256", "member_count", "coverage_start", "coverage_end",
            )
        }
    encoded = json.dumps(source_record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def build_tdx_daily_backfill_plan(
    candidate_rows: Iterable[Mapping[str, Any]],
    *,
    candidate_sha256: str,
    bundle_ref: str,
    plan_nonce: str = "",
    active_stock_codes: Iterable[object],
    registry_rows: Iterable[Mapping[str, Any]],
    member_rows: Iterable[Mapping[str, Any]],
    existing_formal_keys: Iterable[tuple[object, object]],
    expected_candidate_rows: int | None = None,
    expected_candidate_codes: int | None = None,
    expected_trade_dates: Iterable[object] | None = None,
    member_dates_by_code: Mapping[object, Iterable[object]] | None = None,
    source_blocked_codes: Mapping[object, str] | None = None,
    published_at: datetime | None = None,
) -> TdxDailyBackfillPlan:
    """Build an immutable source-bound plan and quarantine broken old registries.

    Existing active universes are copied forward only if their current member
    count, ordinals, per-row digest, and recomputed digest all close. A broken
    legacy universe blocks only that code; its previous registry remains active
    and untouched, while other validated codes can proceed.
    """
    candidate_sha = str(candidate_sha256 or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(candidate_sha):
        raise DateUniverseContractError("candidate CSV SHA-256 is invalid")
    if not str(bundle_ref or "").strip():
        raise DateUniverseContractError("candidate bundle reference is required")
    nonce = str(plan_nonce or "").strip()
    if nonce and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", nonce):
        raise DateUniverseContractError("plan nonce must use 1-64 letters, digits, underscores, or hyphens")
    active_codes = {normalize_code(code) for code in active_stock_codes}
    expected_dates = (
        {normalize_trade_date(value) for value in expected_trade_dates}
        if expected_trade_dates is not None else None
    )
    source_window_dates_by_code: dict[str, tuple[date, ...]] = {}
    for raw_code, raw_dates in (member_dates_by_code or {}).items():
        code = normalize_code(raw_code)
        dates = tuple(sorted({normalize_trade_date(value) for value in raw_dates}))
        if expected_dates is not None and not set(dates).issubset(expected_dates):
            raise DateUniverseContractError(f"source member date outside target window for {code}")
        source_window_dates_by_code[code] = dates
    original_rows = list(candidate_rows)
    if expected_candidate_rows is not None and len(original_rows) != int(expected_candidate_rows):
        raise DateUniverseContractError(
            f"candidate row count mismatch: expected {expected_candidate_rows}, got {len(original_rows)}"
        )
    normalized = _normalize_candidate_rows(
        original_rows,
        active_stock_codes=active_codes,
        expected_trade_dates=expected_dates,
    )
    original_codes = {row["code"] for row in normalized}
    if set(source_window_dates_by_code) - original_codes:
        raise DateUniverseContractError("source member dates include codes outside the candidate scope")
    if member_dates_by_code is not None:
        for row in normalized:
            code = row["code"]
            if normalize_trade_date(row["trade_date"]) not in set(source_window_dates_by_code.get(code, ())):
                raise DateUniverseContractError(
                    f"candidate key absent from source member dates: {code}@{row['trade_date']}"
                )
    if expected_candidate_codes is not None and len(original_codes) != int(expected_candidate_codes):
        raise DateUniverseContractError(
            f"candidate code count mismatch: expected {expected_candidate_codes}, got {len(original_codes)}"
        )
    candidate_keys = {
        (row["code"], normalize_trade_date(row["trade_date"]))
        for row in normalized
    }
    formal_keys = {
        (normalize_code(code), normalize_trade_date(day))
        for code, day in existing_formal_keys
    }
    already_formal = candidate_keys & formal_keys
    if already_formal:
        sample = ", ".join(f"{code}@{day}" for code, day in sorted(already_formal)[:10])
        raise DateUniverseContractError(
            f"candidate key already exists in formal snapshot ({len(already_formal)}): {sample}"
        )

    registry_by_code = _active_registry_rows_by_code(registry_rows, original_codes)
    all_member_rows = tuple(dict(row) for row in member_rows)
    old_dates_by_code: dict[str, tuple[date, ...]] = {}
    old_registry_by_code: dict[str, dict[str, Any]] = {}
    blocked_by_code = {
        normalize_code(code): str(reason or "").strip()
        for code, reason in (source_blocked_codes or {}).items()
    }
    unknown_blocked_codes = set(blocked_by_code) - original_codes
    if unknown_blocked_codes:
        raise DateUniverseContractError(
            f"source-blocked codes outside candidate scope: {sorted(unknown_blocked_codes)}"
        )
    if any(not reason for reason in blocked_by_code.values()):
        raise DateUniverseContractError("source-blocked code reason must not be empty")
    for code in sorted(original_codes):
        if code in blocked_by_code:
            continue
        rows = registry_by_code.get(code, [])
        if len(rows) > 1:
            blocked_by_code[code] = "multiple_active_registry_rows"
            continue
        if not rows:
            continue
        registry = rows[0]
        if str(registry.get("completeness_state") or "").strip().lower() != "complete":
            blocked_by_code[code] = "active_registry_not_complete"
            continue
        dates, error = _existing_member_dates(registry, all_member_rows)
        if error:
            blocked_by_code[code] = error
            continue
        old_dates_by_code[code] = dates or ()
        old_registry_by_code[code] = registry

    blocked_rows = tuple(
        {**row, "blocked_reason": blocked_by_code[row["code"]]}
        for row in normalized
        if row["code"] in blocked_by_code
    )
    accepted_rows = tuple(row for row in normalized if row["code"] not in blocked_by_code)
    if not accepted_rows:
        raise DateUniverseContractError("no candidate keys remain after active-registry integrity gates")

    rows_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in accepted_rows:
        rows_by_code[row["code"]].append(row)

    timestamp = published_at or datetime.now(BUSINESS_TZ)
    payloads: list[DateUniversePayload] = []
    retirements: list[dict[str, Any]] = []
    for code in sorted(rows_by_code):
        code_rows = rows_by_code[code]
        old_registry = old_registry_by_code.get(code)
        source_target_dates = set(source_window_dates_by_code.get(code, ()))
        candidate_target_dates = {normalize_trade_date(row["trade_date"]) for row in code_rows}
        if source_target_dates and not candidate_target_dates.issubset(source_target_dates):
            raise DateUniverseContractError(f"candidate dates outside source window for {code}")
        if not source_target_dates:
            source_target_dates = candidate_target_dates
        dates = set(old_dates_by_code.get(code, ()))
        dates.update(source_target_dates)
        target_dates = sorted(source_target_dates)
        source_kind = (
            "tdx_local_daily_window_extension"
            if old_registry is not None
            else "tdx_local_daily_window"
        )
        source_sha = _source_digest(
            code=code,
            candidate_sha256=candidate_sha,
            rows=code_rows,
            old_registry=old_registry,
            target_dates=target_dates,
            plan_nonce=nonce,
        )
        source_ref = (
            f"{str(bundle_ref).strip()}#code={code}"
            f"&window={min(target_dates).isoformat()}..{max(target_dates).isoformat()}"
            + (f"&run={nonce}" if nonce else "")
        )
        payload = build_date_universe_payload(
            code=code,
            security_type="stock",
            identity_status="verified_active_stock_master",
            source_kind=source_kind,
            source_ref=source_ref,
            source_sha256=source_sha,
            dates=dates,
            completeness_state="complete",
            published_at=timestamp,
        )
        payloads.append(payload)
        if old_registry is not None:
            retired = dict(old_registry)
            retired["is_active"] = 0
            retired["published_at"] = timestamp
            retirements.append(retired)

    planned_members = {
        (row["code"], row["trade_date"])
        for payload in payloads
        for row in payload.member_rows
    }
    missing_members = candidate_keys - planned_members
    # Blocked codes are intentionally absent from the new dataset and write set.
    eligible_keys = {
        (row["code"], normalize_trade_date(row["trade_date"]))
        for row in accepted_rows
    }
    if eligible_keys - planned_members:
        raise DateUniverseContractError("eligible candidate key missing from planned registry/member payload")

    plan_identity = {
        "candidate_sha256": candidate_sha,
        "plan_nonce": nonce,
        "keys": [f"{code}\t{day.isoformat()}" for code, day in sorted(eligible_keys)],
        "blocked_by_code": blocked_by_code,
        "registry_versions": [
            [payload.registry.code, payload.registry.dataset_id, payload.registry.date_set_sha256]
            for payload in payloads
        ],
        "missing_members_for_blocked_keys": len(missing_members),
    }
    plan_digest = sha256(
        json.dumps(plan_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return TdxDailyBackfillPlan(
        candidate_rows=accepted_rows,
        blocked_candidate_rows=blocked_rows,
        blocked_by_code=dict(sorted(blocked_by_code.items())),
        payloads=tuple(payloads),
        retired_registry_rows=tuple(retirements),
        candidate_sha256=candidate_sha,
        plan_sha256=plan_digest,
    )


def stable_tdx_daily_id(code: object, trade_day: object, *, attempt_nonce: str = "") -> int:
    normalized_code = normalize_code(code)
    normalized_day = normalize_trade_date(trade_day)
    namespace = str(attempt_nonce or "").strip()
    if namespace and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", namespace):
        raise DateUniverseContractError("row identity attempt nonce is invalid")
    suffix = f"|attempt:{namespace}" if namespace else ""
    key = f"{normalized_code}|{normalized_day:%Y-%m-%d}|local_tdx_day{suffix}".encode("utf-8")
    return int.from_bytes(blake2b(key, digest_size=8).digest(), "big", signed=False)


def build_daily_insert_frame(
    plan: TdxDailyBackfillPlan, *, created_at: datetime, attempt_nonce: str = ""
):
    """Create canonical rows, optionally isolated to one apply attempt."""
    import pandas as pd

    records = []
    for row in plan.candidate_rows:
        records.append(
            {
                "code": row["code"],
                "trade_date": normalize_trade_date(row["trade_date"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "amount": float(row["amount"]),
                "amplitude": float(row["amplitude"]),
                "change_pct": float(row["change_pct"]),
                "change_amount": float(row["change_amount"]),
                "turnover_rate": float(row["turnover_rate"]),
                "created_at": created_at,
                "id": stable_tdx_daily_id(row["code"], row["trade_date"], attempt_nonce=attempt_nonce),
            }
        )
    frame = pd.DataFrame.from_records(records, columns=KLINE_DAILY_TDX_COLUMNS)
    if frame["id"].duplicated().any():
        raise DateUniverseContractError("stable TDX ids are not unique within the candidate set")
    if frame[["code", "trade_date"]].duplicated().any():
        raise DateUniverseContractError("daily insert frame contains duplicate exact keys")
    return frame


def member_rows_by_month(plan: TdxDailyBackfillPlan) -> dict[str, tuple[dict[str, Any], ...]]:
    """Group complete new member versions by the live monthly partition key."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in plan.member_rows:
        trade_day = normalize_trade_date(row["trade_date"])
        grouped[trade_day.strftime("%Y%m")].append(dict(row))
    return {
        partition: tuple(
            sorted(rows, key=lambda row: (row["dataset_id"], row["code"], row["trade_date"]))
        )
        for partition, rows in sorted(grouped.items())
    }


def build_tdx_rollback_registry_rows(
    plan: TdxDailyBackfillPlan,
    *,
    active_new_dataset_ids: Iterable[object],
    rollback_at: datetime,
) -> dict[str, Any]:
    """Build versioned registry rows that deactivate new and restore old datasets."""
    if not isinstance(rollback_at, datetime):
        raise ValueError("rollback_at must be a datetime")
    if rollback_at.tzinfo is None:
        rollback_at = rollback_at.replace(tzinfo=BUSINESS_TZ)
    latest_plan_time = max(
        (payload.registry.published_at for payload in plan.payloads),
        default=rollback_at,
    )
    if latest_plan_time.tzinfo is None:
        latest_plan_time = latest_plan_time.replace(tzinfo=BUSINESS_TZ)
    if rollback_at <= latest_plan_time:
        rollback_at = latest_plan_time + timedelta(seconds=1)

    plan_rows_by_id = {
        str(row.get("dataset_id") or ""): dict(row)
        for row in plan.registry_rows
    }
    active_ids = {str(value).strip() for value in active_new_dataset_ids if str(value).strip()}
    unknown_ids = active_ids - set(plan_rows_by_id)
    if unknown_ids:
        raise DateUniverseContractError(
            "rollback active dataset IDs are outside the plan: " + ", ".join(sorted(unknown_ids)[:10])
        )
    deactivate = []
    for dataset_id in sorted(active_ids):
        row = dict(plan_rows_by_id[dataset_id])
        row["is_active"] = 0
        row["published_at"] = rollback_at
        deactivate.append(row)
    reactivate = []
    for source in plan.retired_registry_rows:
        row = dict(source)
        row["is_active"] = 1
        row["published_at"] = rollback_at
        reactivate.append(row)
    return {
        "rollback_at": rollback_at,
        "deactivate_new_rows": tuple(deactivate),
        "reactivate_old_rows": tuple(reactivate),
        "delete_member_dataset_ids": tuple(plan.dataset_ids),
    }


def rollback_sql_for_ids(ids: Iterable[object]) -> str:
    """Return one exact, synchronous rollback mutation scoped to stable row IDs."""
    normalized: list[int] = []
    for value in ids:
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise ValueError("rollback ids must be integer UInt64 values")
        number = int(value)
        if number < 0 or number >= 2**64:
            raise ValueError("rollback ids must fit UInt64")
        normalized.append(number)
    if not normalized:
        raise ValueError("rollback id set must be non-empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError("rollback ids must be unique")
    values = ",".join(str(value) for value in sorted(normalized))
    return (
        "ALTER TABLE stock.kline_daily DELETE WHERE id IN ("
        f"{values}) SETTINGS mutations_sync = 2"
    )


def rollback_sql_for_ids_created_at(ids: Iterable[object], created_at: datetime) -> str:
    """Scope rollback to the batch IDs and its unique inserted DateTime marker."""
    if not isinstance(created_at, datetime) or created_at.tzinfo is not None:
        raise ValueError("rollback created_at must be a naive ClickHouse DateTime")
    timestamp = created_at.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    base = rollback_sql_for_ids(ids).split(" SETTINGS", 1)[0]
    return (
        f"{base} AND created_at = toDateTime('{timestamp}') "
        "SETTINGS mutations_sync = 2"
    )


def validate_owned_kline_rows_for_rollback(
    expected_rows: Iterable[Mapping[str, Any]],
    observed_rows: Iterable[Mapping[str, Any]],
    *,
    created_at: datetime,
) -> tuple[dict[str, Any], ...]:
    """Select only unique rows whose attempt ID, exact key, and timestamp match."""
    if not isinstance(created_at, datetime) or created_at.tzinfo is not None:
        raise DateUniverseContractError("rollback ownership timestamp must be a naive DateTime")
    marker = created_at.replace(microsecond=0)
    expected_by_id: dict[int, tuple[str, date]] = {}
    for raw in expected_rows:
        row = dict(raw)
        try:
            row_id = int(row["id"])
            key = (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
        except (KeyError, TypeError, ValueError, DateUniverseContractError) as exc:
            raise DateUniverseContractError(f"rollback ownership expectation is invalid: {exc}") from exc
        if row_id < 0 or row_id >= 2**64 or row_id in expected_by_id:
            raise DateUniverseContractError("rollback ownership IDs must be unique UInt64 values")
        expected_by_id[row_id] = key
    if not expected_by_id:
        raise DateUniverseContractError("rollback ownership expectation must be non-empty")

    owned: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for raw in observed_rows:
        row = dict(raw)
        try:
            row_id = int(row["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DateUniverseContractError(f"rollback ownership readback has invalid ID: {exc}") from exc
        if row_id not in expected_by_id:
            continue
        if row_id in seen_ids:
            raise DateUniverseContractError(f"rollback ownership duplicate planned ID: {row_id}")
        seen_ids.add(row_id)
        try:
            observed_key = (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
            observed_time = row["created_at"]
            if isinstance(observed_time, str):
                observed_time = datetime.fromisoformat(observed_time)
            if not isinstance(observed_time, datetime) or observed_time.tzinfo is not None:
                raise ValueError("created_at is not a naive DateTime")
            observed_time = observed_time.replace(microsecond=0)
        except (KeyError, TypeError, ValueError, DateUniverseContractError) as exc:
            raise DateUniverseContractError(f"rollback ownership readback is invalid: {exc}") from exc
        if observed_key != expected_by_id[row_id] or observed_time != marker:
            raise DateUniverseContractError(f"rollback ownership mismatch for planned ID {row_id}")
        owned.append(row)
    return tuple(owned)


def member_rollback_sql_for_dataset_ids(dataset_ids: Iterable[object]) -> str:
    normalized = sorted({str(value).strip() for value in dataset_ids if str(value).strip()})
    if not normalized:
        raise ValueError("member rollback dataset set must be non-empty")
    quoted = ",".join("'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'" for value in normalized)
    return (
        "ALTER TABLE stock.daily_date_universe_member DELETE WHERE dataset_id IN ("
        f"{quoted}) SETTINGS mutations_sync = 2"
    )


def validate_member_readback(
    plan: TdxDailyBackfillPlan, observed_member_rows: Iterable[Mapping[str, Any]]
) -> None:
    """Require the full new dataset/member payload to read back exactly once."""
    expected = {
        (
            str(row["dataset_id"]),
            str(row["code"]),
            normalize_trade_date(row["trade_date"]),
            int(row["member_ordinal"]),
            str(row["date_set_sha256"]),
        )
        for row in plan.member_rows
    }
    observed_list = [dict(row) for row in observed_member_rows]
    try:
        observed = {
            (
                str(row.get("dataset_id") or ""),
                normalize_code(row.get("code")),
                normalize_trade_date(row.get("trade_date")),
                int(row.get("member_ordinal") or 0),
                str(row.get("date_set_sha256") or ""),
            )
            for row in observed_list
        }
    except (DateUniverseContractError, TypeError, ValueError, OverflowError) as exc:
        raise DateUniverseContractError(f"member readback mismatch: invalid row: {exc}") from exc
    if len(observed_list) != len(expected) or observed != expected:
        missing = expected - observed
        extra = observed - expected
        raise DateUniverseContractError(
            "member readback mismatch: "
            f"expected_rows={len(expected)}, observed_rows={len(observed_list)}, "
            f"unique_observed={len(observed)}, missing={len(missing)}, extra={len(extra)}"
        )


def validate_registry_readback(
    plan: TdxDailyBackfillPlan,
    observed_registry_rows: Iterable[Mapping[str, Any]],
    *,
    initial_registry_rows: Iterable[Mapping[str, Any]],
) -> None:
    """Require the new versions active and preserve blocked codes' prior state."""
    payload_by_code = {payload.registry.code: payload for payload in plan.payloads}
    blocked_codes = set(plan.blocked_by_code)
    relevant_codes = set(payload_by_code) | blocked_codes
    observed = [dict(row) for row in observed_registry_rows]
    latest_by_id: dict[str, dict[str, Any]] = {}
    for row in observed:
        code = normalize_code(row.get("code"))
        if code not in relevant_codes:
            continue
        dataset_id = str(row.get("dataset_id") or "").strip()
        if not dataset_id:
            raise DateUniverseContractError(f"registry readback has empty dataset_id for {code}")
        if dataset_id in latest_by_id:
            raise DateUniverseContractError(f"registry readback has duplicate FINAL dataset_id: {dataset_id}")
        latest_by_id[dataset_id] = row

    initial_active_by_code: dict[str, set[str]] = defaultdict(set)
    for source in initial_registry_rows:
        row = dict(source)
        code = normalize_code(row.get("code"))
        if code in blocked_codes and int(row.get("is_active", 0) or 0) == 1:
            initial_active_by_code[code].add(str(row.get("dataset_id") or "").strip())

    observed_active_by_code: dict[str, set[str]] = defaultdict(set)
    for dataset_id, row in latest_by_id.items():
        if int(row.get("is_active", 0) or 0) == 1:
            observed_active_by_code[normalize_code(row.get("code"))].add(dataset_id)

    for code, payload in payload_by_code.items():
        expected = payload.registry.registry_row()
        active_ids = observed_active_by_code.get(code, set())
        if active_ids != {payload.registry.dataset_id}:
            raise DateUniverseContractError(
                f"registry readback active identity mismatch for {code}: "
                f"expected={payload.registry.dataset_id}, observed={sorted(active_ids)}"
            )
        observed_row = latest_by_id.get(payload.registry.dataset_id)
        if observed_row is None:
            raise DateUniverseContractError(f"registry readback missing new dataset for {code}")
        for field in (
            "date_set_sha256", "member_count", "completeness_state", "source_sha256",
            "source_ref", "source_kind", "security_type", "identity_status",
        ):
            if str(observed_row.get(field)) != str(expected.get(field)):
                raise DateUniverseContractError(
                    f"registry readback mismatch for {code} field={field}"
                )

    for code in blocked_codes:
        expected_ids = initial_active_by_code.get(code, set())
        observed_ids = observed_active_by_code.get(code, set())
        if observed_ids != expected_ids:
            raise DateUniverseContractError(
                f"blocked registry changed for {code}: "
                f"expected_active={sorted(expected_ids)}, observed_active={sorted(observed_ids)}"
            )

    for row in plan.retired_registry_rows:
        dataset_id = str(row.get("dataset_id") or "").strip()
        observed_row = latest_by_id.get(dataset_id)
        if observed_row is None or int(observed_row.get("is_active", 1) or 0) != 0:
            raise DateUniverseContractError(
                f"registry readback did not retire prior dataset {dataset_id}"
            )


def require_tdx_daily_distributed_lock() -> None:
    """Fail closed unless this runner currently holds the database-scoped lock.

    The lock state lives in ClickHouse itself (one CAS row per lock name), so
    every writer that can reach the database — including proxy/tunnel writers
    that host-local file locks cannot see — contends on the same row, and the
    server-serialized mutation queue arbitrates ownership.
    """
    lock = _HELD_DISTRIBUTED_LOCK
    if lock is None:
        raise DateUniverseContractError(
            "database-scoped distributed apply lock is not held by this runner"
        )
    lock.assert_held_and_heartbeat()


DISTRIBUTED_LOCK_TABLE = "stock.daily_backfill_distributed_lock"
DISTRIBUTED_LOCK_NAME = "tdx_daily_backfill"
DISTRIBUTED_LOCK_TTL_SECONDS = 30 * 60
DISTRIBUTED_LOCK_HEARTBEAT_INTERVAL_SECONDS = 60
DISTRIBUTED_LOCK_EPOCH = datetime(1970, 1, 1, tzinfo=BUSINESS_TZ)

_DISTRIBUTED_LOCK_DDL = f"""CREATE TABLE IF NOT EXISTS {DISTRIBUTED_LOCK_TABLE} (
    lock_name String,
    holder_id String,
    acquired_at DateTime64(6, 'Asia/Shanghai'),
    heartbeat_at DateTime64(6, 'Asia/Shanghai'),
    expires_at DateTime64(6, 'Asia/Shanghai'),
    note String
) ENGINE = ReplacingMergeTree(acquired_at) ORDER BY lock_name"""

_HELD_DISTRIBUTED_LOCK: "TdxDailyDistributedLock | None" = None


def _sql_quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dt64_literal(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=BUSINESS_TZ)
    value = value.astimezone(BUSINESS_TZ)
    text = value.strftime("%Y-%m-%d %H:%M:%S.%f")
    return f"toDateTime64('{text}', 6, 'Asia/Shanghai')"


def _as_business_tz(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip())
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=BUSINESS_TZ)
        return value.astimezone(BUSINESS_TZ)
    raise DateUniverseContractError(f"lock timestamp has unsupported type: {type(value)!r}")


def ensure_tdx_daily_distributed_lock_table(client) -> None:
    """Provision the single-row lock table; idempotent and duplicate-safe."""
    client.command(_DISTRIBUTED_LOCK_DDL, settings={"max_execution_time": 60})
    result = client.query(
        f"SELECT count() FROM {DISTRIBUTED_LOCK_TABLE} "
        f"WHERE lock_name={_sql_quote(DISTRIBUTED_LOCK_NAME)}",
        settings={"max_execution_time": 30},
    )
    if not int(result.result_rows[0][0]):
        epoch = _dt64_literal(DISTRIBUTED_LOCK_EPOCH)
        client.command(
            f"INSERT INTO {DISTRIBUTED_LOCK_TABLE} "
            f"(lock_name, holder_id, acquired_at, heartbeat_at, expires_at, note) VALUES "
            f"({_sql_quote(DISTRIBUTED_LOCK_NAME)}, '', {epoch}, {epoch}, {epoch}, 'bootstrap')",
            settings={"max_execution_time": 30},
        )


class TdxDailyDistributedLock:
    """Database-scoped CAS lock row; ownership is arbitrated by ClickHouse.

    Acquisition uses a synchronous ``ALTER TABLE ... UPDATE ... WHERE holder_id=''
    OR expires_at < now`` mutation: ClickHouse executes mutations on one table
    serially, so exactly one concurrent claimant observes the free/empty-holder
    predicate. Time-to-live expiry lets a crashed holder's claim self-release;
    a live holder extends the lease through heartbeats on every lock
    re-assertion (``require_tdx_daily_distributed_lock``).
    """

    def __init__(
        self,
        client,
        holder_id: str,
        *,
        lock_name: str = DISTRIBUTED_LOCK_NAME,
        ttl_seconds: int = DISTRIBUTED_LOCK_TTL_SECONDS,
        heartbeat_interval_seconds: int = DISTRIBUTED_LOCK_HEARTBEAT_INTERVAL_SECONDS,
        now_fn=None,
    ):
        if ttl_seconds <= 0 or heartbeat_interval_seconds < 0:
            raise DateUniverseContractError("distributed lock TTL/heartbeat must be non-negative")
        self._client = client
        self.holder_id = str(holder_id)
        self.lock_name = str(lock_name)
        self.ttl_seconds = int(ttl_seconds)
        self.heartbeat_interval_seconds = int(heartbeat_interval_seconds)
        self._now_fn = now_fn or (lambda: datetime.now(BUSINESS_TZ))
        self._last_heartbeat: datetime | None = None
        self._released = False

    # -- SQL plumbing ---------------------------------------------------
    def _mutation(self, assignments: str, predicate: str) -> None:
        sql = (
            f"ALTER TABLE {DISTRIBUTED_LOCK_TABLE} UPDATE {assignments} "
            f"WHERE {predicate} SETTINGS mutations_sync=1"
        )
        self._client.command(sql, settings={"max_execution_time": 120})

    def _read_row(self) -> dict[str, Any]:
        result = self._client.query(
            f"SELECT lock_name, holder_id, acquired_at, heartbeat_at, expires_at, note "
            f"FROM {DISTRIBUTED_LOCK_TABLE} FINAL "
            f"WHERE lock_name={_sql_quote(self.lock_name)}",
            settings={"max_execution_time": 30},
        )
        names = list(result.column_names)
        if not result.result_rows:
            raise DateUniverseContractError(
                "database-scoped distributed apply lock row is missing from ClickHouse"
            )
        return dict(zip(names, result.result_rows[0]))

    # -- lease maintenance ----------------------------------------------
    def _heartbeat(self, now: datetime) -> None:
        expires = _dt64_literal(now + timedelta(seconds=self.ttl_seconds))
        self._mutation(
            f"heartbeat_at={_dt64_literal(now)}, expires_at={expires}",
            f"lock_name={_sql_quote(self.lock_name)} AND holder_id={_sql_quote(self.holder_id)}",
        )
        self._last_heartbeat = now

    def assert_held_and_heartbeat(self) -> None:
        if self._released:
            raise DateUniverseContractError(
                "database-scoped distributed apply lock was already released by this runner"
            )
        now = _as_business_tz(self._now_fn())
        row = self._read_row()
        if str(row.get("holder_id") or "") != self.holder_id:
            raise DateUniverseContractError(
                "database-scoped distributed apply lock is no longer owned by this runner: "
                f"holder={row.get('holder_id')!r}"
            )
        if _as_business_tz(row["expires_at"]) <= now:
            raise DateUniverseContractError(
                "database-scoped distributed apply lock lease expired for this runner"
            )
        if (
            self._last_heartbeat is None
            or (now - self._last_heartbeat).total_seconds() >= self.heartbeat_interval_seconds
        ):
            self._heartbeat(now)

    def acquire(self, *, note: str = "") -> "TdxDailyDistributedLock":
        now = _as_business_tz(self._now_fn())
        expires = _dt64_literal(now + timedelta(seconds=self.ttl_seconds))
        # acquired_at is the ReplacingMergeTree version column and must stay
        # immutable; FINAL readback is unaffected because every mutation writes
        # identical values to every row carrying this lock_name.
        self._mutation(
            f"holder_id={_sql_quote(self.holder_id)}, "
            f"heartbeat_at={_dt64_literal(now)}, expires_at={expires}, "
            f"note={_sql_quote(note or 'acquire')}",
            f"lock_name={_sql_quote(self.lock_name)} AND "
            f"(holder_id='' OR expires_at < {_dt64_literal(now)})",
        )
        row = self._read_row()
        observed = str(row.get("holder_id") or "")
        if observed != self.holder_id:
            raise DateUniverseContractError(
                "database-scoped distributed apply lock is already held by another writer: "
                f"holder={observed!r}, note={row.get('note')!r}"
            )
        self._last_heartbeat = now
        return self

    def release(self) -> None:
        global _HELD_DISTRIBUTED_LOCK
        if self._released:
            return
        try:
            epoch = _dt64_literal(DISTRIBUTED_LOCK_EPOCH)
            self._mutation(
                f"holder_id='', heartbeat_at={epoch}, expires_at={epoch}, note='released'",
                f"lock_name={_sql_quote(self.lock_name)} AND holder_id={_sql_quote(self.holder_id)}",
            )
        finally:
            self._released = True
            if _HELD_DISTRIBUTED_LOCK is self:
                _HELD_DISTRIBUTED_LOCK = None


def acquire_tdx_daily_distributed_lock(
    client,
    *,
    note: str = "",
    lock_name: str = DISTRIBUTED_LOCK_NAME,
    ttl_seconds: int = DISTRIBUTED_LOCK_TTL_SECONDS,
    heartbeat_interval_seconds: int = DISTRIBUTED_LOCK_HEARTBEAT_INTERVAL_SECONDS,
    holder_id: str | None = None,
    now_fn=None,
) -> TdxDailyDistributedLock:
    """Provision the lock table and claim the database-scoped lock or fail."""
    global _HELD_DISTRIBUTED_LOCK
    if _HELD_DISTRIBUTED_LOCK is not None:
        raise DateUniverseContractError(
            "database-scoped distributed apply lock is already held by this runner"
        )
    ensure_tdx_daily_distributed_lock_table(client)
    holder = str(holder_id or f"{gethostname()}#{os.getpid()}#{uuid4().hex[:12]}")
    lock = TdxDailyDistributedLock(
        client,
        holder,
        lock_name=lock_name,
        ttl_seconds=ttl_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        now_fn=now_fn,
    ).acquire(note=note)
    _HELD_DISTRIBUTED_LOCK = lock
    return lock


def verify_tdx_daily_distributed_lock_provider(client, *, provision: bool = False) -> dict[str, Any]:
    """Report whether the database-scoped lock provider is usable right now.

    Never claims the lock: the probe mutation is an identity update guarded by
    ``holder_id=''``, so it cannot disturb a live holder and does nothing while
    the lock is held by another writer.
    """
    info: dict[str, Any] = {
        "table": DISTRIBUTED_LOCK_TABLE,
        "lock_name": DISTRIBUTED_LOCK_NAME,
        "available": False,
    }
    exists = client.query(
        "SELECT count() FROM system.tables WHERE database='stock' "
        "AND name='daily_backfill_distributed_lock'",
        settings={"max_execution_time": 30},
    )
    if not int(exists.result_rows[0][0]):
        if not provision:
            info["status"] = "not_provisioned"
            return info
        ensure_tdx_daily_distributed_lock_table(client)
    identity = client.query(
        "SELECT currentUser() AS user, getSetting('readonly') AS readonly",
        settings={"max_execution_time": 30},
    )
    names = list(identity.column_names)
    principal = dict(zip(names, identity.result_rows[0]))
    info["principal"] = {"user": str(principal["user"]), "readonly": int(principal["readonly"])}
    if int(principal["readonly"]) != 0:
        info["status"] = "principal_is_readonly"
        return info
    client.command(
        f"ALTER TABLE {DISTRIBUTED_LOCK_TABLE} UPDATE note=note "
        f"WHERE lock_name={_sql_quote(DISTRIBUTED_LOCK_NAME)} AND holder_id='' "
        f"SETTINGS mutations_sync=1",
        settings={"max_execution_time": 120},
    )
    row = client.query(
        f"SELECT holder_id, expires_at FROM {DISTRIBUTED_LOCK_TABLE} FINAL "
        f"WHERE lock_name={_sql_quote(DISTRIBUTED_LOCK_NAME)}",
        settings={"max_execution_time": 30},
    )
    holder = str(row.result_rows[0][0]) if row.result_rows else ""
    info["available"] = True
    info["status"] = "free" if not holder else "held"
    info["current_holder"] = holder
    return info


def publish_tdx_date_universe(
    plan: TdxDailyBackfillPlan,
    *,
    initial_registry_rows: Iterable[Mapping[str, Any]],
    member_partition_sink,
    member_loader,
    retire_registry_sink,
    registry_sink,
    registry_loader,
) -> dict[str, Any]:
    """Publish members first, verify them, then switch registry versions and verify again."""
    require_tdx_daily_distributed_lock()
    if not plan.payloads or not plan.member_rows:
        raise DateUniverseContractError("cannot publish an empty TDX date-universe plan")
    grouped = member_rows_by_month(plan)
    for partition, rows in grouped.items():
        require_tdx_daily_distributed_lock()
        member_partition_sink(partition, rows)

    dataset_ids = tuple(plan.dataset_ids)
    observed_members = tuple(member_loader(dataset_ids))
    validate_member_readback(plan, observed_members)

    if plan.retired_registry_rows:
        require_tdx_daily_distributed_lock()
        retire_registry_sink(plan.retired_registry_rows)
    require_tdx_daily_distributed_lock()
    registry_sink(plan.registry_rows)
    relevant_codes = sorted(
        {row["code"] for row in plan.candidate_rows}
        | {row["code"] for row in plan.blocked_candidate_rows}
    )
    observed_registry = tuple(registry_loader(relevant_codes))
    validate_registry_readback(
        plan,
        observed_registry,
        initial_registry_rows=initial_registry_rows,
    )
    return {
        "member_rows_verified": len(plan.member_rows),
        "member_partitions_published": len(grouped),
        "registry_rows_verified": len(plan.registry_rows),
        "retired_registry_rows_verified": len(plan.retired_registry_rows),
        "registry_codes_verified": len(relevant_codes),
    }


def execute_tdx_daily_apply(
    plan: TdxDailyBackfillPlan,
    *,
    preflight_check,
    publish_registry,
    permit_builder,
    append_formal_rows,
    verify_formal_rows,
    rollback,
    journal=None,
) -> dict[str, Any]:
    """Run one fail-closed publish/permit/append/readback transaction with rollback."""
    from utils.daily_date_universe import DailyExactKeyPermit

    require_tdx_daily_distributed_lock()
    if not plan.keys or not plan.candidate_rows:
        raise DateUniverseContractError("cannot apply an empty exact-key plan")
    publication_started = False
    formal_write_started = False

    def mark(stage: str, **details: Any) -> None:
        if journal is not None:
            journal(stage, details)

    try:
        mark("preflight_started")
        preflight_result = preflight_check()
        mark("preflight_passed")

        publication_started = True
        mark("date_universe_publication_started")
        require_tdx_daily_distributed_lock()
        publication_result = publish_registry()
        mark("date_universe_publication_verified")

        permit = permit_builder()
        if not isinstance(permit, DailyExactKeyPermit):
            raise DateUniverseContractError("official exact-key reader did not return a DailyExactKeyPermit")
        permit.assert_allows(plan.keys, operation="insert")
        if permit.keys != plan.keys:
            raise DateUniverseContractError("exact-key permit is not equal to the planned append key set")
        if permit.dataset_ids != frozenset(plan.dataset_ids):
            raise DateUniverseContractError("exact-key permit dataset IDs do not match published plan")
        expected_digests = {
            payload.registry.code: payload.registry.date_set_sha256
            for payload in plan.payloads
        }
        if dict(permit.date_set_digests) != expected_digests:
            raise DateUniverseContractError("exact-key permit digest map does not match published registry")
        mark("exact_key_permit_verified", permit_digest=permit.permit_digest)

        formal_write_started = True
        mark("formal_append_started", append_rows=len(plan.candidate_rows))
        require_tdx_daily_distributed_lock()
        inserted = append_formal_rows(permit)
        if int(inserted) != len(plan.candidate_rows):
            raise DateUniverseContractError(
                f"formal append row count mismatch: expected={len(plan.candidate_rows)}, inserted={inserted}"
            )
        mark("formal_append_returned", inserted_rows=int(inserted))

        verification = dict(verify_formal_rows())
        if int(verification.get("new_keys_verified", -1)) != len(plan.candidate_rows):
            raise DateUniverseContractError(
                "formal readback new-key count mismatch: "
                f"expected={len(plan.candidate_rows)}, observed={verification.get('new_keys_verified')}"
            )
        mark("formal_readback_verified", **verification)
        return {
            "verified": True,
            "plan_sha256": plan.plan_sha256,
            "permit_digest": permit.permit_digest,
            "inserted_rows": int(inserted),
            "preflight": preflight_result,
            "date_universe": publication_result,
            "formal_readback": verification,
        }
    except Exception as exc:
        if publication_started:
            try:
                mark(
                    "rollback_started",
                    publication_started=publication_started,
                    formal_write_started=formal_write_started,
                    error=str(exc),
                )
                require_tdx_daily_distributed_lock()
                rollback(
                    publication_started=publication_started,
                    formal_write_started=formal_write_started,
                    error=str(exc),
                )
                mark("rollback_verified")
            except Exception as rollback_exc:
                raise DateUniverseContractError(
                    f"TDX apply failed ({exc}); rollback also failed ({rollback_exc})"
                ) from rollback_exc
        raise


def validate_kline_daily_exact_readback(
    expected_new_rows: Iterable[Mapping[str, Any]],
    raw_rows: Iterable[Mapping[str, Any]],
    final_rows: Iterable[Mapping[str, Any]],
    preexisting_rows: Iterable[Mapping[str, Any]],
    *,
    existing_raw_counts: Mapping[tuple[object, object], int],
) -> dict[str, Any]:
    """Verify exact raw/FINAL append keys and prove all prior target rows unchanged."""
    expected_rows = [dict(row) for row in expected_new_rows]
    prior_rows = [dict(row) for row in preexisting_rows]
    raw = [dict(row) for row in raw_rows]
    final = [dict(row) for row in final_rows]
    if not expected_rows:
        raise DateUniverseContractError("exact-key readback has no expected appended rows")

    def key_of(row: Mapping[str, Any]) -> tuple[str, date]:
        return normalize_code(row.get("code")), normalize_trade_date(row.get("trade_date"))

    def unique_by_key(rows: Sequence[Mapping[str, Any]], label: str) -> dict[tuple[str, date], dict[str, Any]]:
        result: dict[tuple[str, date], dict[str, Any]] = {}
        for row in rows:
            key = key_of(row)
            if key in result:
                raise DateUniverseContractError(f"{label} duplicate exact key: {key[0]}@{key[1]}")
            result[key] = dict(row)
        return result

    def canonical_value(field: str, value: Any) -> Any:
        if value is None:
            return None
        if field in {"open", "high", "low", "close", "volume", "amount", "amplitude", "change_pct", "change_amount", "turnover_rate"}:
            return struct.pack(">d", float(value)).hex()
        if field == "trade_date":
            return normalize_trade_date(value).isoformat()
        if field == "created_at":
            if hasattr(value, "to_pydatetime"):
                value = value.to_pydatetime()
            if not isinstance(value, datetime):
                return str(value)
            return value.isoformat(timespec="seconds")
        if field == "id":
            return int(value)
        return str(value) if field == "code" else value

    def assert_same(expected: Mapping[str, Any], actual: Mapping[str, Any], *, label: str, key):
        for field in KLINE_DAILY_TDX_COLUMNS:
            left = canonical_value(field, expected.get(field))
            right = canonical_value(field, actual.get(field))
            if left != right:
                raise DateUniverseContractError(
                    f"{label} for {key[0]}@{key[1]} field={field}"
                )

    new_by_key = unique_by_key(expected_rows, "expected appended")
    old_by_key = unique_by_key(prior_rows, "preexisting snapshot")
    raw_by_key = unique_by_key(raw, "raw")
    final_by_key = unique_by_key(final, "FINAL")
    overlap = set(new_by_key) & set(old_by_key)
    if overlap:
        key = sorted(overlap)[0]
        raise DateUniverseContractError(f"new append key was already in preflight snapshot: {key}")

    prior_counts = {
        (normalize_code(code), normalize_trade_date(trade_day)): int(count)
        for (code, trade_day), count in existing_raw_counts.items()
    }
    if set(prior_counts) != set(old_by_key) or any(count != 1 for count in prior_counts.values()):
        raise DateUniverseContractError("preexisting raw snapshot is not a unique one-row-per-key baseline")
    expected_keys = set(new_by_key) | set(old_by_key)
    expected_counts = Counter(prior_counts)
    expected_counts.update({key: 1 for key in new_by_key})
    raw_counts = Counter(key_of(row) for row in raw)
    if raw_counts != expected_counts or set(raw_by_key) != expected_keys:
        raise DateUniverseContractError(
            "raw exact-key count mismatch: "
            f"expected_keys={len(expected_keys)}, observed_keys={len(raw_counts)}, "
            f"expected_rows={sum(expected_counts.values())}, observed_rows={len(raw)}"
        )
    if set(final_by_key) != expected_keys:
        raise DateUniverseContractError(
            "FINAL exact-key set mismatch: "
            f"expected_keys={len(expected_keys)}, observed_keys={len(final_by_key)}"
        )

    for key, row in new_by_key.items():
        assert_same(row, raw_by_key[key], label="raw appended value mismatch", key=key)
        assert_same(row, final_by_key[key], label="FINAL appended value mismatch", key=key)
    for key, row in old_by_key.items():
        assert_same(row, raw_by_key[key], label="preexisting formal row changed", key=key)
        assert_same(row, final_by_key[key], label="preexisting formal row changed", key=key)

    digest_rows = []
    for key in sorted(expected_keys):
        row = final_by_key[key]
        digest_rows.append(
            json.dumps(
                [canonical_value(field, row.get(field)) for field in KLINE_DAILY_TDX_COLUMNS],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return {
        "new_keys_verified": len(new_by_key),
        "existing_keys_preserved": len(old_by_key),
        "raw_rows_verified": len(raw),
        "final_rows_verified": len(final),
        "final_readback_sha256": sha256(("\n".join(digest_rows) + "\n").encode("utf-8")).hexdigest(),
    }


def validate_baostock_crosscheck(
    report: Mapping[str, Any], *, expected_rows: int,
    allowed_keys: Iterable[tuple[object, object]] | None = None,
    expected_keys: Iterable[tuple[object, object]] | None = None,
) -> None:
    """Validate every independent sample row against its declared TDX source keys."""
    try:
        checked = int(report.get("checked", 0))
        results = list(report.get("results") or ())
    except (TypeError, ValueError) as exc:
        raise DateUniverseContractError(f"Baostock cross-check report invalid: {exc}") from exc
    if int(expected_rows) <= 0 or not results:
        raise DateUniverseContractError("Baostock cross-check sample must be non-empty")
    if checked != int(expected_rows) or len(results) != int(expected_rows):
        raise DateUniverseContractError(
            "Baostock cross-check row count mismatch: "
            f"expected={expected_rows}, checked={checked}, results={len(results)}"
        )
    allowed = None
    if allowed_keys is not None:
        allowed = {
            (normalize_code(code), normalize_trade_date(trade_day))
            for code, trade_day in allowed_keys
        }
        if not allowed:
            raise DateUniverseContractError("Baostock cross-check source sample is empty")
    expected = None
    if expected_keys is not None:
        expected = {
            (normalize_code(code), normalize_trade_date(trade_day))
            for code, trade_day in expected_keys
        }
        if len(expected) != int(expected_rows):
            raise DateUniverseContractError("declared sample keys do not match the expected row count")
        if allowed is not None and not expected.issubset(allowed):
            raise DateUniverseContractError("declared sample keys are outside the TDX source sample")
    seen: set[tuple[str, date]] = set()
    for result in results:
        try:
            key = (normalize_code(result.get("code")), normalize_trade_date(result.get("trade_date")))
        except DateUniverseContractError as exc:
            raise DateUniverseContractError(f"Baostock cross-check key invalid: {exc}") from exc
        if key in seen:
            raise DateUniverseContractError(f"Baostock cross-check duplicate key: {key}")
        if allowed is not None and key not in allowed:
            raise DateUniverseContractError(f"Baostock cross-check key outside TDX source sample: {key}")
        seen.add(key)
        if (
            str(result.get("tradestatus") or "") != "1"
            or result.get("overlap_match") is not True
            or bool(result.get("diffs"))
        ):
            raise DateUniverseContractError(
                f"Baostock cross-check not accepted for {key[0]}@{key[1]}"
            )
    if expected is not None and seen != expected:
        raise DateUniverseContractError(
            f"Baostock cross-check keys do not equal declared sample keys: "
            f"missing={len(expected - seen)}, extra={len(seen - expected)}"
        )


def status_conflicts_for_candidates(
    candidate_rows: Iterable[Mapping[str, Any]], status_rows: Iterable[Mapping[str, Any]]
) -> list[tuple[str, date]]:
    """Return exact candidate keys conflicting with verified non-trading or review conflict."""
    from utils.market_status import StatusIndex

    index = StatusIndex(status_rows)
    conflicts: set[tuple[str, date]] = set()
    for row in candidate_rows:
        code = normalize_code(row.get("code"))
        trade_day = normalize_trade_date(row.get("trade_date"))
        if index.decide(code, trade_day, conflicting_trade=True) == "conflict":
            conflicts.add((code, trade_day))
    return sorted(conflicts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_reviewed_tdx_candidate_bundle(
    bundle_dir: str | Path,
    *,
    expected_candidate_sha256: str | None = None,
    expected_crosscheck_sha256: str | None = None,
    verify_source_files: bool = True,
    tdx_root: str | Path | None = None,
) -> dict[str, Any]:
    """Load only the reviewed bundle whose candidate, manifest, and source files agree."""
    bundle = Path(bundle_dir).resolve()
    manifest_path = bundle / "manifest.json"
    candidate_path = bundle / "tdx_daily_candidates_20261022_20261030.csv"
    crosscheck_path = bundle / "tdx_baostock_crosscheck_20261022_20261030.json"
    for required in (manifest_path, candidate_path, crosscheck_path):
        if not required.is_file():
            raise DateUniverseContractError(f"required TDX bundle artifact missing: {required.name}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        crosscheck = json.loads(crosscheck_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DateUniverseContractError(f"TDX bundle JSON invalid: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(crosscheck, dict):
        raise DateUniverseContractError("TDX bundle manifest or Baostock report has invalid root type")

    candidate_sha = _sha256_file(candidate_path)
    candidate_gate = manifest.get("candidate_gate") or {}
    candidate_validation = manifest.get("candidate_validation") or {}
    overlap_validation = manifest.get("overlap_validation") or {}
    crosscheck_summary = manifest.get("independent_baostock_crosscheck") or {}
    manifest_sha = str(candidate_validation.get("candidate_csv_sha256") or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(manifest_sha) or manifest_sha != candidate_sha:
        raise DateUniverseContractError(
            f"candidate CSV SHA-256 mismatch: manifest={manifest_sha}, actual={candidate_sha}"
        )
    if expected_candidate_sha256 is not None:
        expected_sha = str(expected_candidate_sha256).strip().lower()
        if not SHA256_PATTERN.fullmatch(expected_sha) or expected_sha != candidate_sha:
            raise DateUniverseContractError(
                f"candidate CSV SHA-256 mismatch: expected={expected_sha}, actual={candidate_sha}"
            )
    crosscheck_sha = _sha256_file(crosscheck_path)
    if expected_crosscheck_sha256 is not None:
        expected_crosscheck = str(expected_crosscheck_sha256).strip().lower()
        if not SHA256_PATTERN.fullmatch(expected_crosscheck) or expected_crosscheck != crosscheck_sha:
            raise DateUniverseContractError(
                "Baostock cross-check SHA-256 mismatch: "
                f"expected={expected_crosscheck}, actual={crosscheck_sha}"
            )
    required_gate_fields = (
        "append_only_missing_keys",
        "positive_volume_and_amount_required",
        "valid_ohlc_envelope_required",
        "at_least_one_formal_overlap_per_code",
        "all_overlap_rows_within_tolerance",
    )
    if any(candidate_gate.get(name) is not True for name in required_gate_fields):
        raise DateUniverseContractError("candidate manifest does not close all quality gates")
    if overlap_validation.get("mismatch_fields"):
        raise DateUniverseContractError("candidate manifest has TDX/formal overlap mismatches")
    if candidate_validation.get("invalid_rows"):
        raise DateUniverseContractError("candidate manifest contains invalid candidate rows")
    try:
        expected_rows = int(candidate_validation.get("rows"))
        expected_unique = int(candidate_validation.get("unique_code_date_keys"))
        expected_codes = int(candidate_gate.get("candidate_codes_after_gates"))
        gate_rows = int(candidate_gate.get("candidate_rows_after_gates"))
        sample_rows = int(crosscheck_summary.get("records_checked"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise DateUniverseContractError(f"candidate manifest counts are invalid: {exc}") from exc
    if expected_rows <= 0 or expected_unique != expected_rows or gate_rows != expected_rows:
        raise DateUniverseContractError("candidate manifest row/key counts do not reconcile")
    if int(candidate_validation.get("rows", -1)) != int(manifest.get("candidate_csv_rows", -2)):
        raise DateUniverseContractError("candidate CSV row count does not match bundle summary")
    if int(candidate_validation.get("unique_code_date_keys", -1)) != int(candidate_validation.get("rows", -2)):
        raise DateUniverseContractError("candidate keys are not unique in bundle manifest")
    if int(candidate_validation.get("unique_code_date_keys", -1)) != int(candidate_gate.get("candidate_rows_after_gates", -2)):
        raise DateUniverseContractError("candidate gate row count mismatch")
    if int(candidate_gate.get("candidate_codes_after_gates", -1)) != int(expected_codes):
        raise DateUniverseContractError("candidate gate code count mismatch")
    if int(crosscheck_summary.get("records_within_existing_tolerances", -1)) != sample_rows:
        raise DateUniverseContractError("Baostock cross-check aggregate count mismatch")
    if crosscheck_summary.get("all_tradestatus_trading") is not True:
        raise DateUniverseContractError("Baostock cross-check aggregate status is not all-trading")
    validate_baostock_crosscheck(crosscheck, expected_rows=sample_rows)

    target_dates = {normalize_trade_date(value) for value in manifest.get("target_trade_dates", ())}
    if not target_dates:
        raise DateUniverseContractError("candidate manifest has no target trade dates")
    rows: list[dict[str, Any]] = []
    keys: set[tuple[str, date]] = set()
    codes: set[str] = set()
    try:
        with candidate_path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not {"code", "trade_date", "tdx_file", "tdx_file_sha256"}.issubset(reader.fieldnames):
                raise DateUniverseContractError("candidate CSV is missing required identity/lineage columns")
            for raw in reader:
                row = dict(raw)
                code = normalize_code(row.get("code"))
                trade_day = normalize_trade_date(row.get("trade_date"))
                key = (code, trade_day)
                if key in keys:
                    raise DateUniverseContractError(f"duplicate candidate exact key: {code}@{trade_day}")
                if trade_day not in target_dates:
                    raise DateUniverseContractError(f"candidate date outside bundle target: {code}@{trade_day}")
                keys.add(key)
                codes.add(code)
                rows.append(row)
    except OSError as exc:
        raise DateUniverseContractError(f"cannot read TDX candidate CSV: {exc}") from exc
    if len(rows) != expected_rows or len(keys) != expected_unique or len(codes) != expected_codes:
        raise DateUniverseContractError(
            "candidate CSV content count mismatch: "
            f"rows={len(rows)}/{expected_rows}, keys={len(keys)}/{expected_unique}, "
            f"codes={len(codes)}/{expected_codes}"
        )

    source_root = Path(tdx_root or manifest.get("tdx_root") or "").resolve()
    if verify_source_files:
        if not str(tdx_root or manifest.get("tdx_root") or "").strip() or not source_root.is_dir():
            raise DateUniverseContractError(f"TDX source root is unavailable: {source_root}")
        expected_file_hashes: dict[Path, str] = {}
        for row in rows:
            source_path = Path(str(row.get("tdx_file") or "")).resolve()
            try:
                source_path.relative_to(source_root)
            except ValueError as exc:
                raise DateUniverseContractError(f"TDX source path escapes reviewed root: {source_path}") from exc
            expected_hash = str(row.get("tdx_file_sha256") or "").strip().lower()
            if not SHA256_PATTERN.fullmatch(expected_hash):
                raise DateUniverseContractError(f"TDX source SHA-256 invalid: {source_path}")
            previous_hash = expected_file_hashes.setdefault(source_path, expected_hash)
            if previous_hash != expected_hash:
                raise DateUniverseContractError(f"candidate rows disagree on TDX source hash: {source_path}")
        for source_path, expected_hash in expected_file_hashes.items():
            if not source_path.is_file():
                raise DateUniverseContractError(f"TDX source file is missing: {source_path}")
            actual_hash = _sha256_file(source_path)
            if actual_hash != expected_hash:
                raise DateUniverseContractError(
                    f"source file SHA-256 mismatch: {source_path}; expected={expected_hash}, actual={actual_hash}"
                )

    return {
        "bundle_dir": str(bundle),
        "manifest_path": str(manifest_path),
        "candidate_path": str(candidate_path),
        "crosscheck_path": str(crosscheck_path),
        "candidate_sha256": candidate_sha,
        "crosscheck_sha256": crosscheck_sha,
        "candidate_rows": tuple(rows),
        "candidate_codes": frozenset(codes),
        "target_trade_dates": frozenset(target_dates),
        "tdx_root": str(source_root),
        "manifest": manifest,
        "baostock_crosscheck": crosscheck,
        "source_files_checked": len({row.get("tdx_file") for row in rows}) if verify_source_files else 0,
    }


def validate_tdx_formal_overlap(
    candidate_rows: Iterable[Mapping[str, Any]],
    source_rows: Iterable[Mapping[str, Any]],
    formal_rows: Iterable[Mapping[str, Any]],
    *,
    price_relative_tolerance: float = 0.01,
    quantity_relative_tolerance: float = 0.03,
) -> dict[str, Any]:
    """Recompute candidate-code TDX/formal overlap controls from exact live rows."""
    candidates = tuple(dict(row) for row in candidate_rows)
    candidate_codes = {normalize_code(row.get("code")) for row in candidates}
    expected_overlap_counts: dict[str, int] = {}
    for row in candidates:
        code = normalize_code(row.get("code"))
        try:
            expected_count = int(row.get("overlap_control_rows") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DateUniverseContractError(f"candidate overlap control count invalid for {code}") from exc
        previous = expected_overlap_counts.setdefault(code, expected_count)
        if previous != expected_count or expected_count < 1:
            raise DateUniverseContractError(f"candidate overlap control count inconsistent for {code}")

    source_by_key: dict[tuple[str, date], dict[str, float]] = {}
    for row in source_rows:
        code = normalize_code(row.get("code"))
        if code not in candidate_codes:
            continue
        trade_day = normalize_trade_date(row.get("trade_date"))
        key = (code, trade_day)
        if key in source_by_key:
            raise DateUniverseContractError(f"duplicate TDX source overlap key: {code}@{trade_day}")
        source_by_key[key] = {
            field: _finite_float(row, field)
            for field in ("open", "high", "low", "close", "volume", "amount")
        }
    formal_by_key: dict[tuple[str, date], dict[str, float]] = {}
    for row in formal_rows:
        code = normalize_code(row.get("code"))
        if code not in candidate_codes:
            continue
        trade_day = normalize_trade_date(row.get("trade_date"))
        key = (code, trade_day)
        if key in formal_by_key:
            raise DateUniverseContractError(f"duplicate formal overlap key: {code}@{trade_day}")
        formal_by_key[key] = {
            field: _finite_float(row, field)
            for field in ("open", "high", "low", "close", "volume", "amount")
        }
    overlap_keys = sorted(source_by_key.keys() & formal_by_key.keys())
    counts_by_code: dict[str, int] = defaultdict(int)
    mismatches: dict[str, int] = defaultdict(int)
    digest_rows: list[str] = []
    for code, trade_day in overlap_keys:
        counts_by_code[code] += 1
        source = source_by_key[(code, trade_day)]
        formal = formal_by_key[(code, trade_day)]
        for field in ("open", "high", "low", "close", "volume", "amount"):
            tolerance = price_relative_tolerance if field in {"open", "high", "low", "close"} else quantity_relative_tolerance
            relative_diff = abs(source[field] - formal[field]) / max(abs(formal[field]), 1e-8)
            if relative_diff > tolerance:
                mismatches[field] += 1
                raise DateUniverseContractError(
                    "overlap mismatch for "
                    f"{code}@{trade_day} field={field} relative_diff={relative_diff:.8g} tolerance={tolerance}"
                )
        digest_rows.append(
            "\t".join(
                [code, trade_day.isoformat()]
                + [format(source[field], ".17g") for field in ("open", "high", "low", "close", "volume", "amount")]
                + [format(formal[field], ".17g") for field in ("open", "high", "low", "close", "volume", "amount")]
            )
        )
    for code in sorted(candidate_codes):
        actual_count = counts_by_code.get(code, 0)
        expected_count = expected_overlap_counts.get(code, 0)
        if actual_count != expected_count:
            raise DateUniverseContractError(
                f"overlap control row-count mismatch for {code}: expected={expected_count}, actual={actual_count}"
            )
    return {
        "candidate_codes": len(candidate_codes),
        "overlap_codes": len(counts_by_code),
        "overlap_rows": len(overlap_keys),
        "mismatch_fields": dict(sorted(mismatches.items())),
        "overlap_readback_sha256": sha256(("\n".join(digest_rows) + "\n").encode("utf-8")).hexdigest(),
        "price_relative_tolerance": price_relative_tolerance,
        "quantity_relative_tolerance": quantity_relative_tolerance,
    }


def validate_tdx_source_target_coverage(
    candidate_rows: Iterable[Mapping[str, Any]],
    source_rows: Iterable[Mapping[str, Any]],
    formal_rows: Iterable[Mapping[str, Any]],
    *,
    target_trade_dates: Iterable[object],
    expected_dates_by_code: Mapping[Any, Iterable[Any]] | None = None,
    status_rows: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Bind target keys to TDX/formal bars or verified date-specific exceptions."""
    candidates = tuple(dict(row) for row in candidate_rows)
    candidate_by_key: dict[tuple[str, date], dict[str, Any]] = {}
    candidate_codes: set[str] = set()
    for row in candidates:
        code = normalize_code(row.get("code"))
        trade_day = normalize_trade_date(row.get("trade_date"))
        key = (code, trade_day)
        if key in candidate_by_key:
            raise DateUniverseContractError(f"duplicate candidate exact key: {code}@{trade_day}")
        candidate_by_key[key] = row
        candidate_codes.add(code)
    target_dates = {normalize_trade_date(value) for value in target_trade_dates}
    if not target_dates:
        raise DateUniverseContractError("source target window is empty")
    expected_by_code: dict[str, tuple[date, ...]] = {}
    if expected_dates_by_code is not None:
        for raw_code, raw_dates in expected_dates_by_code.items():
            code = normalize_code(raw_code)
            dates = tuple(sorted({normalize_trade_date(value) for value in raw_dates}))
            if not set(dates).issubset(target_dates):
                raise DateUniverseContractError(f"expected target date outside source window for {code}")
            expected_by_code[code] = dates
        if set(expected_by_code) != candidate_codes:
            raise DateUniverseContractError("expected target-date code scope does not match candidate scope")

    source_by_key: dict[tuple[str, date], dict[str, Any]] = {}
    for source in source_rows:
        row = dict(source)
        code = normalize_code(row.get("code"))
        trade_day = normalize_trade_date(row.get("trade_date"))
        if code not in candidate_codes or trade_day not in target_dates:
            continue
        key = (code, trade_day)
        if key in source_by_key:
            raise DateUniverseContractError(f"duplicate TDX target source key: {code}@{trade_day}")
        source_by_key[key] = row
    formal_list = tuple(dict(row) for row in formal_rows)
    formal_keys = {
        (normalize_code(row.get("code")), normalize_trade_date(row.get("trade_date")))
        for row in formal_list
    }
    if len(formal_keys) != len(formal_list):
        raise DateUniverseContractError("duplicate formal exact key in source-window snapshot")
    candidate_keys = set(candidate_by_key)
    if candidate_keys & formal_keys:
        sample = sorted(candidate_keys & formal_keys)[0]
        raise DateUniverseContractError(f"candidate key already formal during source-window check: {sample}")
    missing_source = candidate_keys - set(source_by_key)
    if missing_source:
        sample = sorted(missing_source)[0]
        raise DateUniverseContractError(f"candidate key absent from current TDX file: {sample}")

    from utils.market_status import StatusIndex

    status_index = StatusIndex(status_rows)
    present_keys = {
        key for key in (set(source_by_key) | formal_keys)
        if key[0] in candidate_codes and key[1] in target_dates
    }
    status_conflicts = [
        key for key in present_keys
        if status_index.decide(key[0], key[1], conflicting_trade=True) == "conflict"
    ]
    if status_conflicts:
        raise DateUniverseContractError(
            f"source/formal target key conflicts with verified market status: {sorted(status_conflicts)[:10]}"
        )

    member_dates: dict[str, list[date]] = defaultdict(list)
    for code, trade_day in sorted(source_by_key):
        source = source_by_key[(code, trade_day)]
        member_dates[code].append(trade_day)
        candidate = candidate_by_key.get((code, trade_day))
        if candidate is None:
            if (code, trade_day) not in formal_keys:
                raise DateUniverseContractError(f"unaccounted TDX target key: {code}@{trade_day}")
            continue
        for field in ("open", "high", "low", "close", "volume", "amount"):
            source_value = _finite_float(source, field)
            candidate_value = _finite_float(candidate, field)
            if candidate_value != source_value:
                raise DateUniverseContractError(
                    f"candidate/source value mismatch for {code}@{trade_day} field={field}"
                )
        previous_close = _finite_float(
            source,
            "prev_close" if "prev_close" in source else "prev_close_from_tdx",
            nonnegative=True,
        )
        close = float(source["close"])
        high = float(source["high"])
        low = float(source["low"])
        expected_change_amount = close - previous_close if previous_close > 0 else 0.0
        expected_change_pct = expected_change_amount / previous_close * 100.0 if previous_close > 0 else 0.0
        expected_amplitude = (high - low) / previous_close * 100.0 if previous_close > 0 else 0.0
        for field, expected in (
            ("prev_close_from_tdx", previous_close),
            ("change_amount", expected_change_amount),
            ("change_pct", expected_change_pct),
            ("amplitude", expected_amplitude),
            ("turnover_rate", 0.0),
        ):
            if _finite_float(candidate, field, nonnegative=field in {"prev_close_from_tdx", "amplitude", "turnover_rate"}) != expected:
                raise DateUniverseContractError(
                    f"candidate/source derived-field mismatch for {code}@{trade_day} field={field}"
                )

    for code, trade_day in sorted(formal_keys):
        if code in candidate_codes and trade_day in target_dates:
            member_dates[code].append(trade_day)

    incomplete_codes: dict[str, tuple[date, ...]] = {}
    verified_nontrading_keys: list[tuple[str, date]] = []
    if expected_by_code:
        for code, trade_day in candidate_keys:
            if trade_day not in expected_by_code.get(code, ()):
                raise DateUniverseContractError(f"candidate key outside expected lifecycle dates: {code}@{trade_day}")
        for code, expected_days in sorted(expected_by_code.items()):
            missing_days = []
            for trade_day in expected_days:
                key = (code, trade_day)
                if key in present_keys:
                    continue
                decision = status_index.decide(code, trade_day)
                if decision == "exempt":
                    verified_nontrading_keys.append(key)
                else:
                    missing_days.append(trade_day)
            if missing_days:
                incomplete_codes[code] = tuple(missing_days)

    for code in sorted(candidate_codes):
        if not member_dates.get(code):
            raise DateUniverseContractError(f"no TDX/formal source target dates for candidate code {code}")
    source_window_sha = sha256(
        "\n".join(f"{code}\t{day.isoformat()}" for code, day in sorted(present_keys)).encode("utf-8")
    ).hexdigest()
    return {
        "candidate_rows": len(candidate_by_key),
        "candidate_codes": len(candidate_codes),
        "source_target_rows": len(present_keys),
        "source_target_codes": len(member_dates),
        "tdx_source_rows": len(source_by_key),
        "existing_formal_source_rows": len(set(source_by_key) & formal_keys),
        "formal_only_target_rows": len(formal_keys - set(source_by_key)),
        "unaccounted_target_keys": tuple(
            (code, day) for code, days in sorted(incomplete_codes.items()) for day in days
        ),
        "incomplete_codes": dict(sorted(incomplete_codes.items())),
        "verified_nontrading_keys": tuple(sorted(verified_nontrading_keys)),
        "member_dates_by_code": {
            code: tuple(sorted(set(days))) for code, days in sorted(member_dates.items())
        },
        "source_window_keys_sha256": source_window_sha,
    }
