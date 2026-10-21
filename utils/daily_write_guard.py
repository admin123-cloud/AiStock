"""Fail-closed guard for formal daily K-line writes.

The reviewed per-security date-universe writer is not in this checkout yet.
Until that writer supplies an accepted date set and an exact-key permit, all
formal ``kline_daily`` INSERT/DELETE paths must stop before touching ClickHouse.
"""

from __future__ import annotations


class DailyWriteBlockedError(RuntimeError):
    """Raised when a formal daily write lacks a reviewed exact-key permit."""


def block_unreviewed_daily_write(*, operation: str, source: str) -> None:
    """Stop an unreviewed formal daily mutation before any database call."""
    raise DailyWriteBlockedError(
        "blocked formal kline_daily "
        f"{operation} from {source}: reviewed per-security date-universe "
        "and exact-key write permit are not registered in this checkout"
    )
