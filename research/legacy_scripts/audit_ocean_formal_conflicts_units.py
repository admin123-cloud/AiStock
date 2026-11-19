"""Reclassify existing read-only formal-diff evidence by unit uncertainty.

This is an offline audit.  It reads only completed formal-diff manifests and
their hashed gzip evidence, writes a new audit artifact atomically, and never
connects to ClickHouse or changes the original evidence.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.ocean_formal_units import classify_formal_conflict


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


class UnitAuditError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(BUSINESS_TZ).replace(microsecond=0).isoformat()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _atomic_gzip_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    count = 0
    try:
        with temp.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                for row in rows:
                    compressed.write((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                    count += 1
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temp, path)
        return count, _sha256_path(path)
    except Exception:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def _iter_gzip(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise UnitAuditError(f"conflict_row_not_object={path}:{line_number}")
            yield value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal-root", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--policy-hash", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-checked-at",
        help="Optional ISO-8601 cutoff for an immutable historical checkpoint scope.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = args.formal_root / args.archive_sha256[:16] / args.policy_hash[:16]
    if not source_root.is_dir():
        raise SystemExit(f"formal_root_missing={source_root}")
    entries = sorted(path.parent for path in source_root.glob("*/manifest.json"))
    if args.max_checked_at:
        cutoff = datetime.fromisoformat(args.max_checked_at)
        filtered: list[Path] = []
        for entry_dir in entries:
            manifest = json.loads((entry_dir / "manifest.json").read_text(encoding="utf-8"))
            checked_at = manifest.get("checked_at")
            if checked_at and datetime.fromisoformat(str(checked_at)) <= cutoff:
                filtered.append(entry_dir)
        entries = filtered
    classified_path = args.output.with_name(
        f"{args.output.stem}_conflicts_classified.jsonl.gz"
    )
    counts: Counter[str] = Counter()
    signals: Counter[str] = Counter()
    total = 0
    manifests = 0
    checked = 0

    def rows() -> Iterable[Mapping[str, Any]]:
        nonlocal total, manifests, checked
        for entry_dir in entries:
            manifest_path = entry_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise UnitAuditError(f"manifest_not_object={manifest_path}")
            manifests += 1
            if manifest.get("status") != "checked":
                continue
            checked += 1
            conflict_path = entry_dir / str(manifest.get("conflict_file") or "")
            expected_hash = str(manifest.get("conflict_sha256") or "")
            if not conflict_path.is_file() or _sha256_path(conflict_path) != expected_hash:
                raise UnitAuditError(f"conflict_evidence_hash_mismatch={conflict_path}")
            manifest_hash = _sha256_path(manifest_path)
            for conflict in _iter_gzip(conflict_path):
                audit = classify_formal_conflict(conflict)
                enriched = {
                    **conflict,
                    **audit,
                    "legacy_conflict_manifest_sha256": manifest_hash,
                    "legacy_conflict_file_sha256": expected_hash,
                }
                counts[str(audit["conflict_class"])] += 1
                for key in ("amount_scale_signal", "volume_scale_signal"):
                    signal = audit.get(key)
                    if signal:
                        signals[f"{key}:{signal}"] += 1
                total += 1
                yield enriched

    count, classified_sha = _atomic_gzip_jsonl(classified_path, rows())
    if count != total:
        raise UnitAuditError("classified_count_mismatch")
    summary = {
        "schema_version": "ocean-formal-unit-audit-v1",
        "created_at": _now(),
        "formal_root": str(args.formal_root.resolve()),
        "archive_sha256": args.archive_sha256,
        "accepted_stock_policy_hash": args.policy_hash,
        "scope_entries": len(entries),
        "manifests_read": manifests,
        "checked_entries": checked,
        "conflicts_read": total,
        "class_counts": dict(sorted(counts.items())),
        "unit_scale_signals": dict(sorted(signals.items())),
        "classified_file": classified_path.name,
        "classified_sha256": classified_sha,
        "formal_authorized": False,
    }
    _atomic_json(args.output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
