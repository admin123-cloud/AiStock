"""Strict, read-only Ocean accepted-stock whitelist evidence.

The source of truth is the stocks master query supplied by the caller.  This
module never infers security type from a code prefix and never talks to a
database itself; the CLI owns the read-only query and passes its result here.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from utils.ocean_stage import accepted_stock_policy_hash


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
WHITELIST_SCHEMA_VERSION = "ocean-stock-whitelist-v1"
CANONICAL_STOCK_RE = r"^[0-9]{6}\.(SH|SZ|BJ)$"


class OceanWhitelistError(RuntimeError):
    """The stock master cannot produce a complete safe whitelist."""


@dataclass(frozen=True)
class StockWhitelistSnapshot:
    codes: tuple[str, ...]
    source_table: str
    source_query: str
    generated_at: str
    source_rows: int

    @property
    def policy_hash(self) -> str:
        return accepted_stock_policy_hash(self.codes)


def _row_values(row: Any) -> tuple[Any, Any]:
    if isinstance(row, Mapping):
        return row.get("code"), row.get("type")
    try:
        return row[0], row[1]
    except (IndexError, KeyError, TypeError) as exc:
        raise OceanWhitelistError(f"invalid_stocks_master_row={row!r}") from exc


def _canonical_master_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    import re

    if not re.fullmatch(CANONICAL_STOCK_RE, text):
        raise OceanWhitelistError(f"invalid_canonical_stock_master_code={value!r}")
    return text


def build_stock_whitelist_snapshot(
    rows: Iterable[Any],
    *,
    source_table: str = "stock.stocks",
    source_query: str = "SELECT code,type FROM stock.stocks FINAL WHERE type='stock' ORDER BY code",
    generated_at: str | None = None,
) -> StockWhitelistSnapshot:
    """Validate exactly the master rows returned by a type=stock query."""

    values = list(rows)
    if not values:
        raise OceanWhitelistError("empty_type_stock_master_result")
    codes: list[str] = []
    seen: set[str] = set()
    for row in values:
        raw_code, raw_type = _row_values(row)
        if str(raw_type or "").strip().lower() != "stock":
            raise OceanWhitelistError(f"master_row_type_not_stock={raw_type!r}")
        code = _canonical_master_code(raw_code)
        if code in seen:
            raise OceanWhitelistError(f"duplicate_canonical_stock_master_code={code}")
        seen.add(code)
        codes.append(code)
    return StockWhitelistSnapshot(
        codes=tuple(sorted(codes)),
        source_table=source_table,
        source_query=source_query,
        generated_at=generated_at or datetime.now(BUSINESS_TZ).replace(microsecond=0).isoformat(),
        source_rows=len(values),
    )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def write_stock_whitelist_snapshot(root: Path, snapshot: StockWhitelistSnapshot) -> dict[str, Any]:
    """Write one immutable timestamped evidence directory; never overwrite it."""

    root = Path(root)
    stamp = snapshot.generated_at.replace("-", "").replace(":", "").replace("+", "_")
    directory = root / "whitelist" / stamp
    if directory.exists():
        raise OceanWhitelistError(f"whitelist_snapshot_already_exists={directory}")
    directory.mkdir(parents=True, exist_ok=False)
    codes_path = directory / "accepted_stock_codes.txt"
    codes_bytes = ("".join(f"{code}\n" for code in snapshot.codes)).encode("utf-8")
    _atomic_write(codes_path, codes_bytes)
    manifest = {
        "schema_version": WHITELIST_SCHEMA_VERSION,
        "source_table": snapshot.source_table,
        "source_query": snapshot.source_query,
        "generated_at": snapshot.generated_at,
        "source_rows": snapshot.source_rows,
        "accepted_rows": len(snapshot.codes),
        "codes_file": codes_path.name,
        "codes_sha256": _sha256_path(codes_path),
        "accepted_stock_policy_hash": snapshot.policy_hash,
        "formal_authorized": False,
        "read_only_source": True,
    }
    _atomic_write(
        directory / "manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return {"directory": str(directory), **manifest}
