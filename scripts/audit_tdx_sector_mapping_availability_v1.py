"""
Audit whether local artifacts can support a TDX-vs-Shenwan sector mapping A/B.

This script is intentionally read-only. It does not initialize TDX/TdxQuant or
write ClickHouse tables. The goal is to distinguish three things:

1. canonical QMT/Shenwan sector tables currently used by AiStock;
2. stock-level TDX alias used for K-line fallback;
3. TDX sector membership tables needed for a true sector mapping comparison.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


REPORT_DIR = report_path("tdx_sector_mapping_availability_v1")


def _query(sql: str) -> list[dict[str, Any]]:
    try:
        return clickhouse_query_df(sql).to_dict(orient="records")
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]


def _scalar(sql: str, default: Any = None) -> Any:
    rows = _query(sql)
    if not rows or "error" in rows[0]:
        return default
    return next(iter(rows[0].values()), default)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def collect() -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    table_rows = _query(
        """
        SELECT name, total_rows
        FROM system.tables
        WHERE database = currentDatabase()
          AND (
            lower(name) LIKE '%sector%'
            OR lower(name) LIKE '%alias%'
            OR lower(name) LIKE '%tdx%'
          )
        ORDER BY name
        """
    )
    table_names = {str(row.get("name")) for row in table_rows if row.get("name")}

    canonical_sector_counts = _query(
        """
        SELECT type, level, count() AS sectors, sum(stock_count) AS stock_count_sum
        FROM sectors
        GROUP BY type, level
        ORDER BY type, level
        """
    )
    canonical_mapping_counts = _query(
        """
        SELECT count() AS rows,
               uniqExact(stock_code) AS stocks,
               uniqExact(sector_code) AS sectors
        FROM sector_stocks
        """
    )
    alias_counts = _query(
        """
        SELECT source, alias_status, count() AS rows, uniqExact(canonical_code) AS canonical_codes
        FROM source_instrument_alias
        GROUP BY source, alias_status
        ORDER BY source, alias_status
        """
    )
    backup_prefix_counts = _query(
        """
        SELECT multiIf(
                   startsWith(code, 'qmt:'), 'qmt',
                   startsWith(code, 'tdx:'), 'tdx',
                   position(code, ':') > 0, splitByChar(':', code)[1],
                   'none'
               ) AS prefix,
               count() AS sectors
        FROM sectors_before_industry_restore_20260703_174111
        GROUP BY prefix
        ORDER BY sectors DESC
        """
    )

    legacy_script_refs = []
    patterns = [
        re.compile(r"get_sector_list"),
        re.compile(r"get_stock_list_in_sector"),
        re.compile(r"TdxQuant.*sector", re.IGNORECASE),
    ]
    for root in ["scripts", "api", "services", "data_fetcher"]:
        for path in (Path(project_root) / root).rglob("*.py"):
            text = _read_text(path)
            if any(p.search(text) for p in patterns):
                legacy_script_refs.append(str(path.relative_to(project_root)))

    has_stock_alias = any(row.get("source") == "tdx" and row.get("alias_status") == "active" for row in alias_counts)
    tdx_sector_tables = sorted(
        name
        for name in table_names
        if "tdx" in name.lower()
        and "sector" in name.lower()
        and name not in {"tdx_fallback_repair_audit"}
    )
    has_tdx_sector_membership_table = any(
        name for name in table_names if name.lower() in {"tdx_sector_stocks", "source_sector_stocks", "sector_source_alias"}
    )

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "verdict": "missing_tdx_sector_membership_mapping",
        "canonical_sector_basis": "QMT/Shenwan canonical sectors in sectors + sector_stocks",
        "stock_alias_status": "available_for_kline_fallback" if has_stock_alias else "missing_or_inactive",
        "tdx_sector_membership_status": "available" if has_tdx_sector_membership_table else "not_found",
        "tdx_sector_tables_detected": tdx_sector_tables,
        "tables_like_sector_alias_tdx": table_rows,
        "canonical_sector_counts": canonical_sector_counts,
        "canonical_mapping_counts": canonical_mapping_counts,
        "stock_alias_counts": alias_counts,
        "backup_sector_prefix_counts": backup_prefix_counts,
        "legacy_sector_script_refs": sorted(set(legacy_script_refs)),
        "current_sector_rows": _scalar("SELECT count() AS c FROM sectors", 0),
        "current_sector_stock_rows": _scalar("SELECT count() AS c FROM sector_stocks", 0),
        "why_strict_ab_blocked": (
            "There is stock-level TDX alias for K-line fallback, but no persisted TDX sector "
            "membership or sector alias table. Current backup sector tables are QMT market-universe "
            "snapshots, not old TDX industry membership. Therefore a strict old-TDX-vs-current-SW "
            "sector-only historical A/B cannot be completed from current tables."
        ),
        "recommended_next_step": (
            "Build a read-only TDX sector snapshot table and an audited sector alias map, then run a "
            "forward/practical dual-mapping experiment. Keep production canonical sectors as QMT/SW2 "
            "unless a fixed-candidate A/B proves the sector layer is the dominant failure."
        ),
    }
    return result


def write_report(result: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# TDX 板块映射可用性审计 v1",
        "",
        f"- 生成时间：`{result['generated_at']}`",
        f"- 结论：`{result['verdict']}`",
        f"- 当前 canonical 板块口径：{result['canonical_sector_basis']}",
        f"- 股票级 TDX alias：`{result['stock_alias_status']}`",
        f"- TDX 板块成分映射：`{result['tdx_sector_membership_status']}`",
        "",
        "## 当前表状态",
        "",
        f"- `sectors` 行数：`{result['current_sector_rows']}`",
        f"- `sector_stocks` 行数：`{result['current_sector_stock_rows']}`",
        f"- 检测到的 TDX 板块相关表：`{len(result['tdx_sector_tables_detected'])}`",
        "",
        "## 为什么现在还不能做严格板块 A/B",
        "",
        result["why_strict_ab_blocked"],
        "",
        "## 下一步",
        "",
        result["recommended_next_step"],
        "",
        "## 相关旧脚本",
        "",
    ]
    for ref in result["legacy_sector_script_refs"][:80]:
        lines.append(f"- `{ref}`")
    (REPORT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    result = collect()
    write_report(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"report_dir={REPORT_DIR}")


if __name__ == "__main__":
    main()
