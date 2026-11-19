from __future__ import annotations

"""Promote the validated purchased 5m candidate into the formal table."""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client

CANDIDATE_TABLE = "stock.purchased_history_minute_5_formal_candidate_20260916"
FORMAL_TABLE = "stock.kline_minute_5"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-candidate-rows", type=int, required=True)
    parser.add_argument("--batch-id", action="append", dest="batch_ids")
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = clickhouse_client()
    where = ""
    parameters: dict[str, str] = {}
    if args.batch_ids:
        placeholders = []
        for index, batch_id in enumerate(args.batch_ids):
            key = f"batch{index}"
            placeholders.append(f"%({key})s")
            parameters[key] = batch_id
        where = f" WHERE batch_id IN ({', '.join(placeholders)})"
    candidate = client.query(
        f"SELECT count(), min(datetime), max(datetime) FROM {CANDIDATE_TABLE}{where}",
        parameters=parameters,
    ).result_rows[0]
    formal_before = client.query(
        f"SELECT count() FROM {FORMAL_TABLE} "
        "WHERE datetime >= %(min_datetime)s AND datetime <= %(max_datetime)s",
        parameters={"min_datetime": candidate[1], "max_datetime": candidate[2]},
    ).result_rows[0][0]
    print(f"formal_candidate_range_before={formal_before}")
    print(f"candidate_rows={candidate[0]} candidate_min={candidate[1]} candidate_max={candidate[2]}")
    if int(candidate[0]) != args.expected_candidate_rows:
        print("candidate_row_count_mismatch", file=sys.stderr)
        return 2
    if int(formal_before) != 0:
        print("formal_pre2006_not_empty; refusing promotion", file=sys.stderr)
        return 3
    if not args.write:
        print("write=False")
        return 0

    client.command(
        f"""
        INSERT INTO {FORMAL_TABLE}
            (code, datetime, open, high, low, close, volume, amount, created_at, id)
        SELECT code, datetime, open, high, low, close, volume, amount, created_at, id
        FROM {CANDIDATE_TABLE}{where}
        """
        , parameters=parameters,
    )
    formal_after = client.query(
        f"SELECT count() FROM {FORMAL_TABLE} "
        "WHERE datetime >= %(min_datetime)s AND datetime <= %(max_datetime)s",
        parameters={"min_datetime": candidate[1], "max_datetime": candidate[2]},
    ).result_rows[0][0]
    print(f"formal_pre2006_after={formal_after}")
    if int(formal_after) != int(args.expected_candidate_rows):
        print("formal_row_count_mismatch", file=sys.stderr)
        return 4
    print("write=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
