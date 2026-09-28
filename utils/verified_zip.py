"""Readiness, hash, and member-safety gates for downloaded ZIP archives.

This module deliberately does not extract files to a destination directory.  A
consumer can stream-decompress one member at a time, which keeps disk usage
bounded and makes zip-slip impossible by construction.  Member names are
still checked because the same gate protects future optional extraction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZipFile, ZipInfo


class VerifiedZipError(RuntimeError):
    """The archive is not safe or is not covered by a valid ready marker."""


@dataclass(frozen=True)
class VerifiedArchive:
    archive: Path
    marker: Path
    name: str
    size: int
    sha256: str
    remote: str
    remote_stat: str
    verified_at: str
    method: str


def marker_path(archive: Path) -> Path:
    """Return the exact marker name emitted by the download queue."""
    return archive.with_suffix(archive.suffix + ".verified.json")


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise VerifiedZipError(f"verified marker field {key!r} is missing")
    return value.strip()


def verify_ready(
    archive: Path,
    marker: Path | None = None,
    known_signature: tuple[int, int, int, str] | None = None,
) -> VerifiedArchive:
    """Validate the atomic ready marker and then hash the unchanged ZIP.

    The archive is not opened until the marker fields, local size, and full
    SHA-256 agree.  Size and mtime are sampled again after hashing so a file
    still being replaced cannot be consumed even if its transient hash happens
    to match an old marker.
    """
    archive = archive.expanduser()
    marker = marker or marker_path(archive)
    if not archive.is_file():
        raise VerifiedZipError(f"archive_missing={archive}")
    if not marker.is_file():
        raise VerifiedZipError(f"verified_marker_missing={marker}")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerifiedZipError(f"verified_marker_unreadable={marker}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerifiedZipError(f"verified_marker_not_object={marker}")

    name = _required_text(payload, "name")
    if name != archive.name:
        raise VerifiedZipError(f"verified_name_mismatch={name!r}!={archive.name!r}")
    try:
        expected_size = int(payload.get("size"))
    except (TypeError, ValueError) as exc:
        raise VerifiedZipError("verified_size_invalid") from exc
    if expected_size < 0:
        raise VerifiedZipError("verified_size_negative")
    expected_sha = _required_text(payload, "sha256").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise VerifiedZipError("verified_sha256_invalid")
    remote = _required_text(payload, "remote")
    remote_stat = _required_text(payload, "remote_stat")
    verified_at = _required_text(payload, "verified_at")
    method = _required_text(payload, "method")

    marker_stat = marker.stat()
    before = archive.stat()
    if before.st_size != expected_size:
        raise VerifiedZipError(f"local_size_mismatch={before.st_size}!={expected_size}")
    signature = (before.st_size, before.st_mtime_ns, marker_stat.st_mtime_ns, expected_sha)
    actual_sha = expected_sha if known_signature == signature else sha256_file(archive)
    after = archive.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise VerifiedZipError("archive_changed_during_hash")
    if actual_sha != expected_sha:
        raise VerifiedZipError(f"local_sha256_mismatch={actual_sha}!={expected_sha}")
    return VerifiedArchive(
        archive=archive,
        marker=marker,
        name=name,
        size=expected_size,
        sha256=expected_sha,
        remote=remote,
        remote_stat=remote_stat,
        verified_at=verified_at,
        method=method,
    )


def _safe_member_name(info: ZipInfo) -> str:
    raw = str(info.filename or "")
    if not raw or "\x00" in raw:
        raise VerifiedZipError(f"unsafe_zip_member={raw!r}")
    name = raw.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:/", name):
        raise VerifiedZipError(f"unsafe_zip_absolute_member={raw!r}")
    parts = PurePosixPath(name).parts
    if any(part in ("..", "") for part in parts if part != "."):
        raise VerifiedZipError(f"unsafe_zip_traversal_member={raw!r}")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise VerifiedZipError(f"unsafe_zip_symlink_member={raw!r}")
    return name


def inspect_zip(path: Path, sample_lines: int = 2) -> dict[str, Any]:
    """Inspect central-directory safety and one bounded CSV sample.

    CRC is checked while the consumer reads every member to avoid a second
    full decompression pass.  This function only reads the central directory
    plus at most ``sample_lines`` from the first CSV.
    """
    names: list[str] = []
    csv_sample: dict[str, Any] | None = None
    with ZipFile(path, "r") as archive:
        for info in archive.infolist():
            name = _safe_member_name(info)
            if name in names:
                raise VerifiedZipError(f"duplicate_zip_member={name}")
            names.append(name)
            if csv_sample is None and not name.endswith("/") and name.lower().endswith(".csv"):
                with archive.open(info, "r") as stream:
                    lines = [
                        stream.readline().decode("utf-8-sig", errors="strict").rstrip("\r\n")
                        for _ in range(max(0, sample_lines))
                    ]
                csv_sample = {"entry": name, "lines": lines, "file_size": info.file_size}
    return {
        "entries": len(names),
        "csv_entries": sum(name.lower().endswith(".csv") for name in names),
        "member_names_sha256": hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest(),
        "sample": csv_sample,
    }


def zip_member_name(info: ZipInfo) -> str:
    """Expose the same normalized/safe name for streaming consumers."""
    return _safe_member_name(info)
