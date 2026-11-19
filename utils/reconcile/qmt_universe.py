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
# These QMT abbreviations omit both keywords: 深信中高 / 深信中低.
# The publisher's methodology identifies both as credit-bond benchmarks.
BOND_INDEX_CODE_EXCLUSIONS: tuple[str, ...] = ("399298.SZ", "399299.SZ")
BOND_INDEX_EVIDENCE_URL = "https://www.cnindex.com.cn/docs/gz_399298.pdf"

# SZSE defines 395xxx as turnover-statistics records, without OHLC prices.
# https://www.szse.cn/disclosure/notice/general/t20041011_499305.html
MARKET_STATISTICS_EVIDENCE_URL = "https://www.szse.cn/disclosure/notice/general/t20041011_499305.html"

# Publisher-confirmed mainland/Hong Kong equity indices. Native 5m ends at
# 12:00 and 16:10, including the Hong Kong closing auction.
CROSS_BORDER_EQUITY_INDEX_CODES = frozenset({'980001.SZ', '980023.SZ', '980068.SZ', '988201.SZ'})


def is_a_share_stock_code(code: str) -> bool:
    base, _, market = str(code or '').upper().partition('.')
    if len(base) != 6 or not base.isdigit():
        return False
    return (market == 'SZ' and base.startswith(('000','001','002','003','300','301'))
            or market == 'SH' and base.startswith(('600','601','603','605','688','689'))
            or market == 'BJ' and base.startswith('92'))


def is_market_statistics_code(code: str) -> bool:
    text = str(code or "").strip().upper()
    base, separator, exchange = text.partition(".")
    return separator == "." and exchange == "SZ" and len(base) == 6 and base.isdigit() and base.startswith("395")


def is_bond_index(code: str, name: str = "") -> bool:
    return (str(code or "").strip().upper() in BOND_INDEX_CODE_EXCLUSIONS
            or any(keyword in str(name or "") for keyword in BOND_INDEX_NAME_KEYWORDS))


def bond_index_exclusion_sql(name_expr: str = "name", code_expr: str = "code") -> str:
    """Exclude debt indices by name and publisher-confirmed abbreviated codes."""
    checks = " OR ".join(
        f"positionUTF8(ifNull({name_expr}, ''), '{keyword}') > 0"
        for keyword in BOND_INDEX_NAME_KEYWORDS
    )
    codes = ", ".join(f"'{code}'" for code in BOND_INDEX_CODE_EXCLUSIONS)
    return f"NOT ({checks} OR {code_expr} IN ({codes}))"


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
        parts.append(f"({type_expr} = 'index' AND {bond_index_exclusion_sql(name_expr, code_expr)} "
                     f"AND NOT ({code_expr} LIKE '395___.SZ'))")
    if "etf" in kinds:
        parts.append(
            f"match({code_expr}, '^(159|510|511|512|513|515|516|517|518|588)[0-9]{{3}}\\\\.(SH|SZ)$')"
        )
    return "(" + " OR ".join(parts) + ")" if parts else "1"
