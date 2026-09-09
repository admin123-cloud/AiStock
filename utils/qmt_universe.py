from __future__ import annotations

"""Shared QMT universe rules for scheduled market-data collection.

Bond index series are retained in ClickHouse for historical reference, but
they are outside AiStock's equity/index maintenance contract.  Keeping this
rule in one place prevents the after-close validator, full-push collector, and
System Config coverage panel from drifting apart.
"""


# The current QMT index master identifies debt benchmarks with either of these
# terms (for example: 国债指数, 上证转债, 5年信用).  This intentionally applies
# only to ``type = 'index'``; it never filters A-share stocks.
BOND_INDEX_NAME_KEYWORDS: tuple[str, ...] = ("债", "信用")


def bond_index_exclusion_sql(name_expr: str = "name") -> str:
    """Return a ClickHouse predicate that excludes debt-index names."""
    checks = " OR ".join(
        f"positionUTF8(ifNull({name_expr}, ''), '{keyword}') > 0"
        for keyword in BOND_INDEX_NAME_KEYWORDS
    )
    return f"NOT ({checks})"


def qmt_universe_filter_sql(
    universe: str,
    *,
    code_expr: str = "code",
    type_expr: str = "type",
    name_expr: str = "name",
) -> str:
    """Build the QMT scheduled-collection universe SQL predicate."""
    kinds = {item.strip().lower() for item in str(universe or "").split(",") if item.strip()}
    if not kinds or "all" in kinds:
        return "1"

    parts: list[str] = []
    if "stock" in kinds:
        parts.append(f"{type_expr} = 'stock'")
    if "index" in kinds:
        parts.append(f"({type_expr} = 'index' AND {bond_index_exclusion_sql(name_expr)})")
    if "etf" in kinds:
        parts.append(
            f"match({code_expr}, '^(159|510|511|512|513|515|516|517|518|588)[0-9]{{3}}\\\\.(SH|SZ)$')"
        )
    # Callers append AND lifecycle/date constraints; OR branches must stay grouped.
    return "(" + " OR ".join(parts) + ")" if parts else "1"
