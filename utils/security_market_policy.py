"""Security-market exclusions enforced before AiStock data writes."""

from __future__ import annotations

import re
from collections.abc import Iterable


# AiStock's historical feed normalizes NEEQ securities to .SZ. The legacy
# 400/420/430 families and NEEQ 43/83/87 families are rejected independently
# of an optional suffix. 88xxxx.SH is intentionally exempt because 880xxx.SH
# is used by TDX index symbols; .BJ is exempt for BSE securities.
_NEEQ_CODE = re.compile(
    r"^(?:(?:400|420|430)\d{3}(?:\.(?:SZ|SH|BJ|NQ|NEEQ))?"
    r"|(?:43|83|87)\d{4}(?:\.(?:SZ|SH|NQ|NEEQ))?"
    r"|88\d{4}(?:\.(?:SZ|NQ|NEEQ))?)$",
    re.IGNORECASE,
)
_CLICKHOUSE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def clickhouse_normalized_code_expression(column: str = "code") -> str:
    """Return the DB-side case/space normalization used by NEEQ CHECKs."""
    if not isinstance(column, str) or not _CLICKHOUSE_IDENTIFIER.fullmatch(column):
        raise ValueError(f"invalid ClickHouse code column identifier: {column!r}")
    return f"upper(trimBoth(ifNull(toString({column}), '')))"


class NEEQSecurityBlockedError(ValueError):
    """Raised before a NEEQ security can enter an AiStock data store."""


def is_neeq_security_code(value: object) -> bool:
    """Return whether a normalized or bare legacy code belongs to NEEQ.

    BSE's current 920xxx.BJ namespace and TDX's 880xxx.SH index namespace
    are deliberately not classified as NEEQ.
    """
    if value is None:
        return False
    code = str(value).strip().upper()
    if code.endswith(".BJ") and not code.startswith(("400", "420", "430")):
        return False
    if code.endswith(".SH") and code.startswith("88"):
        return False
    return bool(_NEEQ_CODE.fullmatch(code))


def assert_no_neeq_codes(codes: Iterable[object], *, context: str) -> None:
    """Fail closed when a write batch contains any NEEQ security code."""
    blocked = sorted({str(code).strip().upper() for code in codes if is_neeq_security_code(code)})
    if blocked:
        preview = ", ".join(blocked[:20])
        suffix = "..." if len(blocked) > 20 else ""
        raise NEEQSecurityBlockedError(
            f"blocked {context}: AiStock no longer accepts NEEQ securities "
            f"({preview}{suffix})"
        )
