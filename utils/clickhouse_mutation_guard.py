"""Safety checks for ClickHouse mutations against minute K-line tables."""

from __future__ import annotations

import re
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UnsafeMinuteMutationError(RuntimeError):
    """Raised before a mutation can rewrite an unpartitioned minute table."""


def require_month_partitioned_minute_mutation(client: Any, table: str) -> None:
    """Allow minute DELETE only on a table partitioned by calendar month."""
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError(f"invalid ClickHouse table identifier: {table!r}")
    rows = client.query(
        "SELECT partition_key FROM system.tables "
        "WHERE database = currentDatabase() AND name = %(table)s",
        parameters={"table": table},
    ).result_rows
    partition_key = str(rows[0][0] or "") if rows else ""
    if "toYYYYMM(datetime)" not in partition_key:
        raise UnsafeMinuteMutationError(
            f"blocked DELETE on {table}: minute-table mutations require "
            "PARTITION BY toYYYYMM(datetime); rebuild derived data in a candidate table instead"
        )
