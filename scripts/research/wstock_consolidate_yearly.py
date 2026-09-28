"""Merge WStock wsCSV exports into one unique CSV per code and calendar year.

Source roots are supplied in precedence order (first root wins on a conflicting
code+datetime). By default the script audits only. --write publishes a complete
new tree only when all input rows are parseable and no conflicting values exist.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


HEADER = b"Date,Open,Low,High,Close,Volume,Amount"
CODE_RE = re.compile(r"^(?:SH|SZ)\d{6}$")
DATETIME_RE = re.compile(rb"^(\d{4})-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
NUMBER_RE = re.compile(rb"^-?\d+(?:\.\d+)?$")


def same_values(left: bytes, right: bytes) -> bool:
    if left == right:
        return True
    a, b = left.split(b","), right.split(b",")
    if len(a) != 7 or len(b) != 7 or a[0] != b[0]:
        return False
    try:
        return all(Decimal(x.decode("ascii")) == Decimal(y.decode("ascii"))
                   for x, y in zip(a[1:], b[1:]))
    except (InvalidOperation, UnicodeDecodeError):
        return False


def nonpositive(value: bytes) -> bool:
    return value.startswith(b"-") or not any(49 <= char <= 57 for char in value)


def negative(value: bytes) -> bool:
    return value.startswith(b"-") and any(49 <= char <= 57 for char in value)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if path.exists() or tmp.exists():
        raise FileExistsError(f"Refusing to overwrite manifest: {path}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-root", type=Path, action="append", required=True,
                    help="input CSV tree; repeat in priority order, highest priority first")
    ap.add_argument("--output-root", type=Path, required=True,
                    help="canonical output root; files are written as YYYY/CODE.csv")
    ap.add_argument("--manifest", type=Path, required=True,
                    help="new JSON audit manifest path outside the output tree")
    ap.add_argument("--conflicts-csv", type=Path, required=True,
                    help="new CSV containing differing values for duplicate keys")
    ap.add_argument("--quality-exceptions", type=Path, default=None,
                    help="optional new CSV listing nonpositive OHLC and negative volume/amount rows")
    ap.add_argument("--write", action="store_true",
                    help="publish output after a clean full audit; otherwise audit only")
    args = ap.parse_args()

    output_root = args.output_root.resolve()
    manifest_path = args.manifest.resolve()
    conflicts_path = args.conflicts_csv.resolve()
    exceptions_path = args.quality_exceptions.resolve() if args.quality_exceptions else None
    if manifest_path.exists() or conflicts_path.exists() or (exceptions_path and exceptions_path.exists()):
        raise FileExistsError("Refusing to overwrite an existing manifest or conflict report")
    if (output_root in manifest_path.parents or output_root in conflicts_path.parents
            or (exceptions_path and output_root in exceptions_path.parents)):
        raise ValueError("Keep audit metadata outside the canonical CSV tree")

    roots: list[Path] = []
    for root in args.source_root:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {resolved}")
        if output_root in resolved.parents:
            raise ValueError(f"Output root must not be inside an input source: {resolved}")
        roots.append(resolved)
    if len(set(roots)) != len(roots):
        raise ValueError("Duplicate source roots were supplied")

    # Group by security code to keep only one stock's keys in memory at a time.
    files_by_code: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    source_summaries = []
    for rank, root in enumerate(roots):
        files = sorted(root.rglob("*.csv"))
        if not files:
            print(f"WARNING: source contains no CSV files: {root}", flush=True)
        source_summaries.append({"priority": rank, "root": str(root), "csv_files": len(files)})
        for path in files:
            code = path.stem
            if not CODE_RE.fullmatch(code):
                raise ValueError(f"Unexpected CSV filename (expected SH/SZ + 6 digits): {path}")
            files_by_code[code].append((rank, path))

    if not files_by_code:
        raise ValueError("No source CSV files found")

    summary = {
        "source_files": 0,
        "input_rows": 0,
        "unique_code_datetime_rows": 0,
        "exact_duplicate_rows": 0,
        "format_only_duplicate_rows": 0,
        "conflicting_duplicate_rows": 0,
        "nonpositive_ohlc_rows": 0,
        "negative_volume_amount_rows": 0,
        "code_year_files": 0,
    }
    per_year: dict[str, dict[str, int]] = defaultdict(lambda: {"codes": 0, "rows": 0})
    file_records: list[dict] = []
    conflict_stream = None
    conflict_writer = None
    exception_stream = None
    exception_writer = None
    partial = output_root.with_name(output_root.name + ".partial")
    if args.write and partial.exists():
        raise FileExistsError(f"Refusing to overwrite a partial output tree: {partial}")
    if args.write:
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.mkdir()
    output_stats: list[dict] = []
    output_bytes = 0
    output_files = 0
    output_rows = 0
    codes = sorted(files_by_code)

    for code_index, code in enumerate(codes, 1):
        # year -> datetime -> (raw row, source priority, source path)
        rows_by_year: dict[str, dict[bytes, tuple[bytes, int, str]]] = defaultdict(dict)
        for rank, path in sorted(files_by_code[code], key=lambda item: (item[0], str(item[1]))):
            digest = hashlib.sha256()
            file_rows = 0
            date_min: str | None = None
            date_max: str | None = None
            with path.open("rb") as stream:
                raw_header = stream.readline()
                header = raw_header.rstrip(b"\r\n").lstrip(b"\xef\xbb\xbf")
                if header != HEADER:
                    raise ValueError(f"Unexpected CSV header in {path}: {header!r}")
                digest.update(raw_header)
                for raw in stream:
                    digest.update(raw)
                    line = raw.rstrip(b"\r\n")
                    if not line:
                        raise ValueError(f"Blank CSV row in {path} at input row {file_rows + 2}")
                    fields = line.split(b",")
                    if len(fields) != 7:
                        raise ValueError(f"Expected 7 CSV fields in {path} row {file_rows + 2}")
                    match = DATETIME_RE.fullmatch(fields[0])
                    if not match:
                        raise ValueError(f"Invalid Date value in {path} row {file_rows + 2}: {fields[0]!r}")
                    if any(not NUMBER_RE.fullmatch(value) for value in fields[1:]):
                        raise ValueError(f"Invalid numeric field in {path} row {file_rows + 2}")
                    year = match.group(1).decode("ascii")
                    key = fields[0]
                    row_map = rows_by_year[year]
                    previous = row_map.get(key)
                    if previous is None:
                        row_map[key] = (line, rank, str(path))
                        bad_prices = any(nonpositive(fields[index]) for index in range(1, 5))
                        bad_volume_amount = any(negative(fields[index]) for index in (5, 6))
                        if bad_prices:
                            summary["nonpositive_ohlc_rows"] += 1
                        if bad_volume_amount:
                            summary["negative_volume_amount_rows"] += 1
                        if (bad_prices or bad_volume_amount) and exceptions_path is not None:
                            if exception_writer is None:
                                exceptions_path.parent.mkdir(parents=True, exist_ok=True)
                                exception_stream = exceptions_path.open("x", newline="", encoding="utf-8-sig")
                                exception_writer = csv.DictWriter(exception_stream, fieldnames=[
                                    "code", "datetime", "open", "low", "high", "close", "volume", "amount",
                                    "issue", "selected_source",
                                ])
                                exception_writer.writeheader()
                            issues = []
                            if bad_prices:
                                issues.append("nonpositive_OHLC")
                            if bad_volume_amount:
                                issues.append("negative_volume_or_amount")
                            exception_writer.writerow({
                                "code": code,
                                "datetime": key.decode("ascii"),
                                "open": fields[1].decode("ascii"),
                                "low": fields[2].decode("ascii"),
                                "high": fields[3].decode("ascii"),
                                "close": fields[4].decode("ascii"),
                                "volume": fields[5].decode("ascii"),
                                "amount": fields[6].decode("ascii"),
                                "issue": "|".join(issues),
                                "selected_source": str(path),
                            })
                            exception_stream.flush()
                    elif previous[0] == line:
                        summary["exact_duplicate_rows"] += 1
                    elif same_values(previous[0], line):
                        summary["format_only_duplicate_rows"] += 1
                    else:
                        summary["conflicting_duplicate_rows"] += 1
                        if conflict_writer is None:
                            conflicts_path.parent.mkdir(parents=True, exist_ok=True)
                            conflict_stream = conflicts_path.open("x", newline="", encoding="utf-8-sig")
                            conflict_writer = csv.DictWriter(conflict_stream, fieldnames=[
                                "code", "datetime", "kept_source", "kept_row", "other_source", "other_row",
                                "kept_priority", "other_priority",
                            ])
                            conflict_writer.writeheader()
                        conflict_writer.writerow({
                            "code": code,
                            "datetime": key.decode("ascii"),
                            "kept_source": previous[2],
                            "kept_row": previous[0].decode("utf-8", errors="replace"),
                            "other_source": str(path),
                            "other_row": line.decode("utf-8", errors="replace"),
                            "kept_priority": previous[1],
                            "other_priority": rank,
                        })
                        conflict_stream.flush()
                    file_rows += 1
                    stamp = fields[0].decode("ascii")
                    date_min = stamp if date_min is None or stamp < date_min else date_min
                    date_max = stamp if date_max is None or stamp > date_max else date_max
            summary["source_files"] += 1
            summary["input_rows"] += file_rows
            file_records.append({
                "priority": rank,
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
                "rows": file_rows,
                "date_min": date_min,
                "date_max": date_max,
            })

        summary["unique_code_datetime_rows"] += sum(len(row_map) for row_map in rows_by_year.values())
        for year, row_map in rows_by_year.items():
            per_year[year]["codes"] += 1
            per_year[year]["rows"] += len(row_map)
            if args.write and summary["conflicting_duplicate_rows"] == 0:
                relative = Path(year) / f"{code}.csv"
                target = partial / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(HEADER + b"\n")
                    for key in sorted(row_map):
                        stream.write(row_map[key][0] + b"\n")
                count = len(row_map)
                size = target.stat().st_size
                output_stats.append({"path": str(relative), "rows": count, "bytes": size})
                output_files += 1
                output_rows += count
                output_bytes += size
        if code_index % 100 == 0 or code_index == len(codes):
            print(f"audited_codes={code_index}/{len(codes)} input_rows={summary['input_rows']} "
                  f"unique_rows={summary['unique_code_datetime_rows']} "
                  f"conflicts={summary['conflicting_duplicate_rows']}", flush=True)

    summary["code_year_files"] = sum(v["codes"] for v in per_year.values())
    manifest = {
        "schema": "wstock-yearly-csv-consolidation-v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "output_contract": "YYYY/CODE.csv; one file per stock and calendar year; rows sorted by Date",
        "source_priority": source_summaries,
        "summary": summary,
        "per_year": dict(sorted(per_year.items())),
        "conflicts_csv": str(conflicts_path) if summary["conflicting_duplicate_rows"] else None,
        "quality_exceptions_csv": str(exceptions_path) if summary["nonpositive_ohlc_rows"] or summary["negative_volume_amount_rows"] else None,
        "output_root": str(output_root) if args.write and summary["conflicting_duplicate_rows"] == 0 else None,
        "files": file_records,
    }

    if conflict_stream is not None:
        conflict_stream.close()
    if exception_stream is not None:
        exception_stream.close()

    if summary["conflicting_duplicate_rows"]:
        if args.write:
            manifest["partial_output_root"] = str(partial)
            manifest["partial_output_files"] = output_files
            manifest["partial_output_rows"] = output_rows
            manifest["partial_output_bytes"] = output_bytes
        write_json(manifest_path, manifest)
        print(f"CONFLICT: {summary['conflicting_duplicate_rows']} row collisions; canonical output not published.", flush=True)
        print(f"conflicts_csv={conflicts_path}", flush=True)
        return 5
    if not args.write:
        write_json(manifest_path, manifest)
        print(f"AUDIT ONLY: output rows={summary['unique_code_datetime_rows']} code-year files={summary['code_year_files']}")
        print(f"manifest={manifest_path}")
        return 0

    if output_files != summary["code_year_files"] or output_rows != summary["unique_code_datetime_rows"]:
        raise RuntimeError("Written output counts do not match the audited source union")
    manifest["output_files"] = output_stats
    manifest["output_bytes"] = output_bytes

    backup = None
    if output_root.exists():
        history_dir = output_root.parent / "版本归档"
        history_dir.mkdir(parents=True, exist_ok=True)
        backup = history_dir / f"{output_root.name}_{time.strftime('%Y%m%d_%H%M%S')}"
        if backup.exists():
            raise FileExistsError(f"Refusing to overwrite prior canonical output: {backup}")
        output_root.rename(backup)
    try:
        partial.rename(output_root)
    except Exception:
        if backup is not None and backup.exists() and not output_root.exists():
            backup.rename(output_root)
        raise
    manifest["backup_root"] = str(backup) if backup else None
    write_json(manifest_path, manifest)
    print(f"PUBLISHED output_root={output_root} files={output_files} rows={output_rows} bytes={output_bytes}", flush=True)
    print(f"manifest={manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2)
