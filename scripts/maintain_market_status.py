"""Maintain the shared status audit from reviewed, exact-date notice evidence.

Default import is a dry run. Only --apply writes; it never replaces an existing
status key, changes a price table, or extends an interval into another date.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

TABLE = "stock.kline_daily_market_status_audit"
KEY = ("code", "start_date", "end_date", "status")
ALLOWED_HOSTS = {"static.cninfo.com.cn", "www.sse.com.cn", "www.szse.cn", "www.bse.cn"}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def code_value(value):
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(value)):
        raise ValueError("requires canonical code: " + str(value))
    return value


def is_exempt(row):
    return (row.get("review_state") == "verified"
            and row.get("status") in {"suspended", "delisted", "not_trading"}
            and bool(row.get("evidence_url")))


def prepare_exact(items, *, today=None):
    """Validate previously reviewed proofs; this does not review notice text."""
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    rows = {}
    for item in items:
        code = code_value(item["code"])
        day = date.fromisoformat(item["date"])
        if (day > today or item["status"] != "suspended"
                or item["review_state"] != "verified_for_this_exact_date"
                or item.get("kline_fabricated") is not False):
            raise ValueError("not reviewed exact-date suspension: " + code)
        if not (date.fromisoformat(item["supporting_interval_start"]) <= day
                <= date.fromisoformat(item["supporting_interval_end"])):
            raise ValueError("date outside reviewed notice interval")
        url = urllib.parse.urlsplit(item["evidence_url"])
        if url.scheme != "https" or url.hostname not in ALLOWED_HOSTS or url.username:
            raise ValueError("official notice URL required")
        for path_field, hash_field in (("source_path", "source_sha256"),
                                       ("full_text_path", "full_text_sha256")):
            if not re.fullmatch(r"[0-9a-f]{64}", item[hash_field]) or sha256(item[path_field]) != item[hash_field]:
                raise ValueError("notice evidence hash mismatch: " + item[path_field])
        if not item["reason"].strip():
            raise ValueError("reviewed reason required")
        proof = dict(item)
        proof["registry_scope"] = "exact_date_only"
        row = dict(code=code, start_date=day.isoformat(), end_date=day.isoformat(),
                   status="suspended", evidence_url=item["evidence_url"], reason=item["reason"],
                   expected_resume_date=None, source="verified_exact_notice",
                   review_state="verified", evidence_json=json.dumps(proof, ensure_ascii=False, sort_keys=True))
        key = tuple(row[k] for k in KEY)
        if key in rows and rows[key] != row:
            raise ValueError("conflicting duplicate proof: " + str(key))
        rows[key] = row
    return list(rows.values())


def plan_insert(rows, existing):
    by_key = {tuple(r[k] for k in KEY): r for r in existing}
    pending, skipped = [], []
    for row in rows:
        key = tuple(row[k] for k in KEY)
        old = by_key.get(key)
        if old is None:
            pending.append(row)
        elif is_exempt(old) and all(old.get(k) == v for k, v in row.items()):
            skipped.append(key)
        else:
            raise ValueError("existing key differs; review explicitly, never overwrite: " + str(key))
    return pending, skipped


class Warehouse:
    def __init__(self, endpoint):
        self.endpoint = endpoint.rstrip("/") + "/"

    def execute(self, sql, *, write=False):
        settings = dict(max_threads=1, max_memory_usage=402653184,
                        max_execution_time=40, wait_end_of_query=1,
                        output_format_json_quote_64bit_integers=0)
        if not write:
            settings["readonly"] = 2
        request = urllib.request.Request(self.endpoint + "?" + urllib.parse.urlencode(settings), data=sql.encode("utf-8"))
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode("utf-8")) from exc

    def query(self, sql):
        return [json.loads(x) for x in self.execute(sql + " FORMAT JSONEachRow").splitlines()]


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8123")
    sub = parser.add_subparsers(dest="operation", required=True)
    lookup = sub.add_parser("lookup")
    lookup.add_argument("--code", required=True, type=code_value)
    lookup.add_argument("--date", required=True, type=date.fromisoformat)
    load = sub.add_parser("import-exact")
    load.add_argument("--input", required=True, type=Path)
    load.add_argument("--receipt", required=True, type=Path)
    load.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = Warehouse(args.endpoint)
    if args.operation == "lookup":
        rows = db.query(f"SELECT * FROM {TABLE} FINAL WHERE code='{args.code}' AND start_date<=toDate('{args.date}') AND end_date>=toDate('{args.date}') ORDER BY observed_at DESC")
        print(json.dumps(dict(code=args.code, date=str(args.date), exempt=any(map(is_exempt, rows)), rows=rows), ensure_ascii=False, indent=2))
        return
    if args.receipt.exists() or args.receipt.with_suffix(".before.json").exists():
        raise ValueError("use a new receipt path; preserve previous write outcome")
    input_hash = sha256(args.input)
    rows = prepare_exact(json.loads(args.input.read_text(encoding="utf-8-sig")))
    existing = db.query(f"SELECT * FROM {TABLE} FINAL ORDER BY code,start_date,end_date,status")
    pending, skipped = plan_insert(rows, existing)
    receipt = dict(input=str(args.input), input_sha256=input_hash, table=TABLE,
                   candidate_count=len(rows), pending_count=len(pending), skipped_count=len(skipped),
                   apply=args.apply, state="planned", pending_keys=[[r[k] for k in KEY] for r in pending])
    save(args.receipt.with_suffix(".before.json"), existing)
    save(args.receipt, receipt)
    if args.apply and pending:
        # Persist intent before write. After an uncertain outcome rerun with a
        # fresh receipt: planning checks current keys before inserting anything.
        receipt["state"] = "writing"
        save(args.receipt, receipt)
        try:
            db.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS evidence_json String DEFAULT ''", write=True)
            stamp = int(datetime.now(timezone.utc).timestamp())
            payload = [dict(row, observed_at=stamp) for row in pending]
            columns = ",".join(payload[0])
            db.execute(f"INSERT INTO {TABLE} ({columns}) FORMAT JSONEachRow\n" + "\n".join(json.dumps(r, ensure_ascii=False) for r in payload), write=True)
        except Exception as exc:
            receipt.update(state="write_outcome_requires_readback", error=str(exc))
            save(args.receipt, receipt)
            raise
    if args.apply:
        after = db.query(f"SELECT * FROM {TABLE} FINAL ORDER BY code,start_date,end_date,status")
        remaining, _ = plan_insert(rows, after)
        if remaining:
            raise RuntimeError("readback missing keys; inspect receipt before retry")
        after_by_key = {tuple(r[k] for k in KEY): r for r in after}
        for old in existing:
            actual = after_by_key[tuple(old[k] for k in KEY)]
            if any(actual[k] != v for k, v in old.items()):
                raise RuntimeError("pre-existing status changed during import")
        if len(after) != len(existing) + len(pending):
            raise RuntimeError("unexpected concurrent registry change; inspect readback")
        save(args.receipt.with_suffix(".after.json"), after)
        receipt.update(state="verified", after_count=len(after), old_rows_unchanged=True)
    save(args.receipt, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
