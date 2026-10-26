"""Build a read-only daily closure snapshot.

``anomalies`` mode is a fast value-domain classification of the live formal
table.  ``range`` mode reads a bounded formal date range and combines it with
an expected key file plus registry/member exact-key readback.  Both modes only
issue SELECTs with ``readonly=1`` and write evidence to local files.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.daily_closure_snapshot import (
    build_daily_closure_snapshot,
    build_daily_closure_snapshot_from_reader,
    classify_daily_rows,
)
from utils.daily_date_universe import DailyDateUniverseReader, normalize_code, normalize_trade_date
from utils.market_warehouse import clickhouse_client


READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 180, "max_threads": 2}


def _open_text(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open("r", encoding="utf-8")


def _load_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    with _open_text(path) as handle:
        text = handle.read()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    def coerce(row: Any) -> dict[str, Any]:
        if isinstance(row, Mapping):
            return dict(row)
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            return {"code": row[0], "trade_date": row[1]}
        raise ValueError(f"unsupported evidence row in {path}: {row!r}")

    if isinstance(payload, list):
        return [coerce(row) for row in payload]
    if isinstance(payload, dict):
        if "code" in payload and "trade_date" in payload:
            return [dict(payload)]
        for key in ("rows", "keys", "records", "members"):
            value = payload.get(key)
            if isinstance(value, list):
                return [coerce(row) for row in value]
    raise ValueError(f"unsupported evidence payload: {path}")


def _load_keys(path: Path | None) -> list[tuple[str, Any]] | None:
    if path is None:
        return None
    rows = _load_rows(path)
    return [(normalize_code(row["code"]), normalize_trade_date(row["trade_date"])) for row in rows]


def _load_gzip_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL evidence file without treating it as a command source."""

    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _load_accepted_manifest(path: Path) -> list[dict[str, Any]]:
    """Load the previously accepted date-membership manifest.

    The manifest is deliberately kept as a local evidence input.  It is not
    silently promoted to the production registry; callers still need an
    online registry/member readback before a write permit can be considered
    healthy.
    """

    rows = _load_gzip_jsonl(path) if path.suffix == ".gz" else _load_rows(path)
    output: list[dict[str, Any]] = []
    for row in rows:
        code = normalize_code(row.get("code"))
        dates = [normalize_trade_date(value) for value in row.get("dates") or []]
        if not dates:
            continue
        output.append({**row, "code": code, "dates": dates})
    return output


def _manifest_reader(records: Iterable[Mapping[str, Any]]) -> DailyDateUniverseReader:
    """Create a read-only registry/member reader backed by accepted evidence."""

    by_code: dict[str, dict[str, Any]] = {}
    for record in records:
        code = normalize_code(record.get("code"))
        dates = tuple(normalize_trade_date(value) for value in record.get("dates") or [])
        date_digest = str(record.get("date_set_sha256") or "").strip()
        source_digest = str(
            (record.get("source_acceptance") or {}).get("sha256")
            or (record.get("source_acceptance") or {}).get("source_audit_sha256")
            or "accepted-manifest"
        ).strip()
        dataset_id = f"local-accepted:{code}:{date_digest[:32]}"
        by_code[code] = {
            "dataset_id": dataset_id,
            "code": code,
            "completeness_state": "complete",
            "date_set_sha256": date_digest or source_digest,
            "is_active": 1,
            "source_sha256": source_digest,
            "dates": frozenset(dates),
        }

    def registry_loader(codes: list[str] | tuple[str, ...]) -> Iterable[Mapping[str, Any]]:
        return [
            {key: value for key, value in row.items() if key != "dates"}
            for code in codes
            if (row := by_code.get(normalize_code(code))) is not None
        ]

    def member_loader(
        codes: list[str] | tuple[str, ...],
        keys: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    ) -> Iterable[Mapping[str, Any]]:
        allowed = {normalize_code(code) for code in codes}
        output: list[dict[str, Any]] = []
        for code, trade_date in keys:
            canonical_code = normalize_code(code)
            row = by_code.get(canonical_code)
            day = normalize_trade_date(trade_date)
            if row is None or canonical_code not in allowed or day not in row["dates"]:
                continue
            output.append(
                {
                    "dataset_id": row["dataset_id"],
                    "code": canonical_code,
                    "trade_date": day,
                    "date_set_sha256": row["date_set_sha256"],
                }
            )
        return output

    return DailyDateUniverseReader(
        registry_loader=registry_loader,
        member_loader=member_loader,
    )


def _parse_ths_source_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse one accepted native THS export into formal stock units.

    THS exports report shares while ``kline_daily.volume`` is lots.  The
    explicit /100 conversion is local audit logic only; it does not alter the
    source file or the production table.
    """

    source_files = record.get("source_files") or {}
    raw_ref = source_files.get("raw_gbk") or {}
    raw_path = Path(str(raw_ref.get("path") or ""))
    if not raw_path.exists():
        raise FileNotFoundError(f"accepted THS source file missing: {raw_path}")

    def number(value: str) -> float | None:
        token = str(value or "").strip()
        if token in {"", "--", "-", "—"}:
            return None
        return float(token.replace(",", "").replace("%", ""))

    code = normalize_code(record.get("code"))
    rows: list[dict[str, Any]] = []
    for line in raw_path.read_bytes().decode("gbk").splitlines():
        values = re.split(r"\t+", line.strip())
        if len(values) != 11 or not values[0] or not values[0][:4].isdigit():
            continue
        trade_date = values[0].split(",", 1)[0]
        volume_shares = number(values[7])
        rows.append(
            {
                "code": code,
                "trade_date": trade_date,
                "open": number(values[1]),
                "high": number(values[2]),
                "low": number(values[3]),
                "close": number(values[4]),
                "volume": None if volume_shares is None else volume_shares / 100.0,
                "amount": number(values[8]),
            }
        )
    return rows


def _dict_rows(result: Any) -> list[dict[str, Any]]:
    names = list(getattr(result, "column_names", ()) or ())
    return [dict(zip(names, row)) for row in getattr(result, "result_rows", ())]


def _read_formal_rows(client: Any, *, mode: str, start_date: str | None, end_date: str | None) -> list[dict[str, Any]]:
    where = ""
    if mode == "range":
        if not start_date or not end_date:
            raise ValueError("range mode requires --start-date and --end-date")
        where = f"WHERE trade_date >= toDate('{start_date}') AND trade_date <= toDate('{end_date}')"
    elif mode != "anomalies":
        raise ValueError(f"unsupported mode: {mode}")
    if mode == "anomalies":
        where = """WHERE (high < greatest(open, close) OR low > least(open, close) OR high < low)
           OR (amount > 0 AND volume <= 0)
           OR (volume > 0 AND amount <= 0)"""
    result = client.query(
        f"""
        SELECT code,trade_date,open,high,low,close,volume,amount
        FROM stock.kline_daily FINAL
        {where}
        ORDER BY trade_date,code
        """,
        settings=READONLY_SETTINGS,
    )
    return _dict_rows(result)


def _read_status_rows(client: Any) -> list[dict[str, Any]]:
    result = client.query(
        """
        SELECT code,start_date,end_date,status,review_state,evidence_url,reason
        FROM stock.kline_daily_market_status_audit FINAL
        """,
        settings=READONLY_SETTINGS,
    )
    return _dict_rows(result)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")


def build_report(
    *,
    client: Any,
    mode: str,
    expected_keys_path: Path | None,
    source_rows_path: Path | None,
    start_date: str | None,
    end_date: str | None,
    source: str,
    accepted_manifest_path: Path | None = None,
    batch_size: int = 40,
    output_dir: Path | None = None,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    if mode == "range" and accepted_manifest_path is not None:
        # Import lazily to keep the small anomaly/range unit-test surface free
        # of a module cycle.  The range runner still uses this module's
        # read-only helpers and the same snapshot contract.
        from scripts.run_daily_closure_range_snapshot import _build_report

        report, classified = _build_report(
            client=client,
            manifest_path=accepted_manifest_path,
            start_date=normalize_trade_date(start_date),
            end_date=normalize_trade_date(end_date),
            batch_size=batch_size,
            output_dir=output_dir or Path("."),
        )
        return report, tuple(classified)
    expected_keys = _load_keys(expected_keys_path)
    formal_rows = _read_formal_rows(client, mode=mode, start_date=start_date, end_date=end_date)
    status_rows = _read_status_rows(client)
    source_rows = _load_rows(source_rows_path) if source_rows_path else None

    if expected_keys is not None:
        reader = DailyDateUniverseReader.from_clickhouse(client)
        snapshot = build_daily_closure_snapshot_from_reader(
            reader=reader,
            expected_keys=expected_keys,
            formal_rows=formal_rows,
            source_rows=source_rows,
            status_rows=status_rows,
            source=source,
        )
    else:
        snapshot = build_daily_closure_snapshot(
            permit=None,
            formal_rows=formal_rows,
            source_rows=source_rows,
            status_rows=status_rows,
        )
    classified = classify_daily_rows(formal_rows, status_rows=status_rows)
    report = snapshot.as_dict()
    report.update(
        {
            "mode": mode,
            "evidence_scope": "formal_date_range" if mode == "range" else "formal_anomaly_rows_only",
            "expected_keys_path": str(expected_keys_path) if expected_keys_path else None,
            "source_rows_path": str(source_rows_path) if source_rows_path else None,
            "formal_rows_read": len(formal_rows),
            "status_rows_read": len(status_rows),
        }
    )
    return report, classified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("anomalies", "range"), default="anomalies")
    parser.add_argument("--expected-keys", type=Path)
    parser.add_argument("--source-rows", type=Path)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--source", default="scripts.audit_daily_closure_snapshot")
    parser.add_argument("--accepted-manifest", type=Path)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--anomalies-output", type=Path)
    args = parser.parse_args()

    report, classified = build_report(
        client=clickhouse_client(),
        mode=args.mode,
        expected_keys_path=args.expected_keys,
        source_rows_path=args.source_rows,
        start_date=args.start_date,
        end_date=args.end_date,
        source=args.source,
        accepted_manifest_path=args.accepted_manifest,
        batch_size=args.batch_size,
        output_dir=args.output.parent,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if args.anomalies_output:
        _write_jsonl(args.anomalies_output, classified)
    print(json.dumps({
        "output": str(args.output),
        "status": report["status"],
        "mode": report["mode"],
        "formal_rows_read": report["formal_rows_read"],
        "anomaly_counts": report["anomaly_counts"],
        "anomalies_output": str(args.anomalies_output) if args.anomalies_output else None,
    }, ensure_ascii=False))
    return 0 if report["status"] == "healthy" else 2


if __name__ == "__main__":
    raise SystemExit(main())

