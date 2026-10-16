"""Shared, exact-date status lookup for daily and minute anomaly review."""
from collections import defaultdict
from datetime import date, timedelta
import re


EXEMPT_STATUSES = {"suspended", "delisted", "not_trading"}


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
        "SELECT code,start_date,end_date,status,evidence_url,reason,review_state "
        "FROM stock.kline_daily_market_status_audit FINAL "
        f"WHERE start_date<=toDate('{end}') AND end_date>=toDate('{start}')"
        " AND ((review_state='verified' AND evidence_url!='' "
        "AND status IN ('suspended','delisted','not_trading')) OR review_state='conflict')"
        + code_filter,
        settings={"max_threads": 1, "max_memory_usage": 402653184},
    ).result_rows
    fields = ("code", "start_date", "end_date", "status", "evidence_url", "reason", "review_state")
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

    A-share session timestamps, including legacy late-hour stored bars, occupy
    their labeled UTC date. Checking the entire stored date is conservative:
    time anomalies must be reviewed, never used to hide positive trading bars.
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
                conflicts.update(wanted[(str(code), str(day)[:10])] for code, day in rows
                                 if (str(code), str(day)[:10]) in wanted)
    return conflicts
