"""Shared, exact-date status lookup for daily and minute anomaly review."""
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
import re
import struct


EXEMPT_STATUSES = {"suspended", "delisted", "not_trading"}
MINUTE_FINGERPRINT_COLUMNS = (
    'code,toUnixTimestamp(datetime) AS stored_epoch,open,high,low,close,volume,amount,'
    'toUnixTimestamp(created_at) AS created_epoch,id'
)


def minute_rows_fingerprint(rows):
    """Bind exact FINAL rows, including nulls, Float64 bits and version identity."""
    rows = list(rows)
    if any(len(row) != 10 for row in rows):
        raise ValueError('minute fingerprint requires all ten typed fields')
    values = []
    for row in sorted(rows, key=lambda r: (str(r[0]), r[1], r[9])):
        code, stamp, op, high, low, close, volume, amount, created, identity = row
        if volume is not None and int(volume) != volume:
            raise ValueError('minute volume fingerprint requires the Int64 schema')
        bits = lambda value: None if value is None else struct.pack('<d', float(value)).hex()
        values.append([str(code), int(stamp), bits(op), bits(high), bits(low), bits(close),
                       None if volume is None else int(volume), bits(amount),
                       None if created is None else int(created), int(identity)])
    body = json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return hashlib.sha256(body).hexdigest()


class StatusIndex:
    def __init__(self, rows):
        self.by_code = defaultdict(list)
        for row in rows:
            row = dict(row, start_date=str(row['start_date'])[:10], end_date=str(row['end_date'])[:10])
            self.by_code[str(row["code"]).upper()].append(row)

    def matches(self, code, day):
        day = str(day)[:10]
        return [r for r in self.by_code.get(str(code).upper(), [])
                if str(r["start_date"])[:10] <= day <= str(r["end_date"])[:10]]

    def decide(self, code, day, *, conflicting_trade=False):
        rows = self.matches(code, day)
        verified = any(r.get("review_state") == "verified" and r.get("evidence_url")
                       and r.get("status") in EXEMPT_STATUSES for r in rows)
        conflict = any(r.get("review_state") == "conflict" for r in rows)
        if conflict or (verified and conflicting_trade):
            return "conflict"
        return "exempt" if verified else "unregistered"

    def reviewed_clock_binding(self, code, day):
        """A reviewed source/date mapping permits only its frozen original rows."""
        day = str(day)[:10]
        candidates = []
        for row in self.matches(code, day):
            if (row.get('review_state') != 'verified' or row.get('status') != 'suspended'
                    or not row.get('evidence_url') or row['start_date'] != day or row['end_date'] != day):
                continue
            try:
                proof = json.loads(row.get('evidence_json') or '{}').get('adjudication', {})
                binding = proof.get('retained_minute_rows', {})
                if (proof.get('outcome') == 'confirmed_suspension'
                        and proof.get('reason_code') == 'legacy_timestamp_maps_to_next_session'
                        and proof.get('code') == str(code) and proof.get('trade_date') == day
                        and proof.get('notice_confirmed') is True and proof.get('minute_original_verified') is True
                        and binding.get('schema_version') == 1 and isinstance(binding.get('rows'), int)
                        and binding['rows'] > 0 and binding.get('source_date', '') > day
                        and re.fullmatch(r'[0-9a-f]{64}', binding.get('sha256', ''))):
                    candidates.append(binding)
            except (ValueError, TypeError, AttributeError):
                continue
        identities = {(r['sha256'], r['rows'], r['source_date']) for r in candidates}
        return candidates[0] if len(identities) == 1 else None


def load_status_index(client, start="1970-01-01", end="2099-12-31", codes=None):
    start, end = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
    if start > end:
        raise ValueError("invalid status date range")
    code_filter = ""
    if codes is not None:
        codes = sorted(set(map(str, codes)))
        if not codes:
            return StatusIndex([])
        if any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", c) for c in codes):
            raise ValueError("canonical codes required")
        code_filter = " AND code IN (" + ",".join("'" + c + "'" for c in codes) + ")"
    rows = client.query(
        "SELECT code,start_date,end_date,status,evidence_url,reason,review_state,evidence_json "
        "FROM stock.kline_daily_market_status_audit FINAL "
        f"WHERE start_date<=toDate('{end}') AND end_date>=toDate('{start}')"
        " AND ((review_state='verified' AND evidence_url!='' "
        "AND status IN ('suspended','delisted','not_trading')) OR review_state='conflict')"
        + code_filter,
        settings={"max_threads": 1, "max_memory_usage": 402653184},
    ).result_rows
    fields = ("code", "start_date", "end_date", "status", "evidence_url", "reason", "review_state", "evidence_json")
    return StatusIndex(dict(zip(fields, row)) for row in rows)


def partition_status_keys(keys, index, conflicting_keys=()):
    conflict = set(conflicting_keys)
    groups = {"exempt": set(), "conflict": set(), "unregistered": set()}
    for key in keys:
        groups[index.decide(*key, conflicting_trade=key in conflict)].add(key)
    groups["lookup"] = groups["unregistered"] | groups["conflict"]
    return groups


def find_status_trade_conflicts(client, keys, index):
    """Recheck only keys that would otherwise be exempt, in bounded months.

    Stored UTC date is only a conservative candidate bucket. A reviewed legacy
    row group may belong to a later session. Such a resolution is honored only
    while every current FINAL field matches the frozen row fingerprint.
    """
    keys = list(keys)
    months = defaultdict(set)
    for code, day in keys:
        if index.decide(code, day) == "exempt":
            months[str(day)[:7]].add(code)
    conflicts = set()
    wanted = {(str(code), str(day)[:10]): (code, day) for code, day in keys}
    for month, codes in sorted(months.items()):
        first = date.fromisoformat(month + "-01")
        end = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        for offset in range(0, len(codes), 256):
            code_sql = ",".join("'" + c + "'" for c in sorted(codes)[offset:offset + 256])
            for table, column in (("kline_daily", "trade_date"), ("kline_minute_5", "datetime")):
                rows = client.query(
                    f"SELECT DISTINCT code,toDate({column}) FROM stock.{table} FINAL "
                    f"WHERE code IN ({code_sql}) AND {column}>=toDate('{first}') AND {column}<toDate('{end}') "
                    "AND (volume>0 OR amount>0 OR isNull(volume) OR isNull(amount) "
                    "OR NOT isFinite(amount) OR volume<0 OR amount<0)",
                    settings={"max_threads": 1, "max_memory_usage": 402653184},
                ).result_rows
                for code, day in rows:
                    identity = (str(code), str(day)[:10])
                    if identity not in wanted:
                        continue
                    key = wanted[identity]
                    binding = index.reviewed_clock_binding(*identity) if table == 'kline_minute_5' else None
                    if binding and key not in conflicts:
                        first_day = date.fromisoformat(identity[1])
                        current = client.query(
                            f"SELECT {MINUTE_FINGERPRINT_COLUMNS} FROM stock.kline_minute_5 FINAL "
                            f"WHERE code='{identity[0]}' AND datetime>=toDate('{first_day}') "
                            f"AND datetime<toDate('{first_day + timedelta(days=1)}') ORDER BY datetime,id",
                            settings={"max_threads": 1, "max_memory_usage": 402653184},
                        ).result_rows
                        if len(current) == binding['rows'] and minute_rows_fingerprint(current) == binding['sha256']:
                            continue
                    conflicts.add(key)
    return conflicts
