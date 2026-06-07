"""
行业板块同步（ClickHouse 原子切换版）

- 同步一/二/三级行业板块到 sectors
- 可选同步板块成分到 sector_stocks
- 全程避免 UPDATE，统一使用临时表 + RENAME 原子切换
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Set

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from data_fetcher.sources.tdxquant_pool import tdxquant_pool
from utils.logger import get_logger
from utils.market_warehouse import clickhouse_client

logger = get_logger("SyncSectors")


class SectorSyncer:
    def __init__(self):
        self.stats = {
            "total_sectors": 0,
            "new_sectors": 0,
            "updated_sectors": 0,
            "total_mappings": 0,
            "new_mappings": 0,
            "failed": 0,
        }

    def sync_all_sectors(self, include_mappings: bool = True):
        logger.info("=" * 80)
        logger.info("Start syncing sectors (atomic swap mode)")
        logger.info("=" * 80)

        self._ensure_sector_tables()

        level1 = self._fetch_sectors_by_level(1)
        level2 = self._fetch_sectors_by_level(2)
        level3 = self._fetch_sectors_by_level(3)
        merged = level1 + level2 + level3

        self._atomic_replace_sectors(merged)

        if include_mappings:
            self._atomic_replace_sector_mappings(merged)
        else:
            logger.info("Skip sector-stock mapping sync for lightweight sector-list run")

        logger.info("板块同步完成，统计={}", self.stats)

    def _fetch_sectors_by_level(self, level: int) -> List[Dict[str, Any]]:
        market_type_map = {1: "16", 2: "17", 3: "18"}
        market_type = market_type_map.get(level)
        if not market_type:
            return []

        sector_list = tdxquant_pool.get_stock_list(market=market_type, list_type=1)
        if not sector_list:
            logger.warning("No sectors returned for level={}", level)
            return []

        out: List[Dict[str, Any]] = []
        for item in sector_list:
            code = item.get("Code")
            name = item.get("Name")
            if not code or not name:
                continue
            out.append({"code": str(code), "name": str(name), "level": int(level)})

        logger.info("Fetched sectors level={} count={}", level, len(out))
        return out

    def _ensure_sector_tables(self):
        ch = clickhouse_client()
        ch.command(
            """
            CREATE TABLE IF NOT EXISTS sectors (
                code String,
                name String,
                type String,
                parent_code Nullable(String),
                level Int32,
                stock_count Int32,
                created_at DateTime
            )
            ENGINE = ReplacingMergeTree(created_at)
            ORDER BY (code)
            """
        )
        ch.command(
            """
            CREATE TABLE IF NOT EXISTS sector_stocks (
                sector_code String,
                stock_code String,
                weight Float64,
                created_at DateTime
            )
            ENGINE = ReplacingMergeTree(created_at)
            ORDER BY (sector_code, stock_code)
            """
        )

    def _atomic_replace_sectors(self, sector_rows: List[Dict[str, Any]]):
        ch = clickhouse_client()
        now = datetime.now()

        dedup: Dict[str, Dict[str, Any]] = {}
        for row in sector_rows:
            dedup[row["code"]] = row
        items = list(dedup.values())
        self.stats["total_sectors"] = len(items)

        existing_rows = ch.query("SELECT code, name, type, level FROM sectors").result_rows
        existing_map = {
            str(r[0]): {"name": str(r[1] or ""), "type": str(r[2] or ""), "level": int(r[3] or 0)}
            for r in existing_rows
            if r and r[0]
        }

        self.stats["new_sectors"] = 0
        self.stats["updated_sectors"] = 0
        for row in items:
            old = existing_map.get(row["code"])
            if old is None:
                self.stats["new_sectors"] += 1
            elif old["name"] != row["name"] or old["type"] != "industry" or old["level"] != row["level"]:
                self.stats["updated_sectors"] += 1

        rows = []
        for row in items:
            rows.append([
                row["code"],
                row["name"],
                "industry",
                None,
                int(row["level"]),
                0,
                now,
            ])

        tmp_table = "sectors_sync_tmp"
        backup_table = "sectors_sync_backup"
        ch.command(f"DROP TABLE IF EXISTS {tmp_table}")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")
        ch.command(f"CREATE TABLE {tmp_table} AS sectors")

        if rows:
            ch.insert(
                tmp_table,
                rows,
                column_names=["code", "name", "type", "parent_code", "level", "stock_count", "created_at"],
            )

        ch.command(f"RENAME TABLE sectors TO {backup_table}, {tmp_table} TO sectors")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")

    def _atomic_replace_sector_mappings(self, sectors: List[Dict[str, Any]]):
        ch = clickhouse_client()
        now = datetime.now()

        sector_codes: Set[str] = {str(s["code"]) for s in sectors}
        rows = []

        for idx, sector_code in enumerate(sorted(sector_codes), start=1):
            try:
                stock_list = tdxquant_pool.get_stock_list_in_sector(sector_code) or []
            except Exception as exc:
                logger.error("获取板块 {} 成分股映射失败: {}", sector_code, exc)
                self.stats["failed"] += 1
                continue

            for stock in stock_list:
                stock_code = stock.get("Code")
                if not stock_code:
                    continue
                rows.append([str(sector_code), str(stock_code), 0.0, now])

            if idx % 10 == 0:
                logger.info("映射进度: {}/{} 个板块", idx, len(sector_codes))

        self.stats["total_mappings"] = len(rows)
        self.stats["new_mappings"] = len(rows)

        tmp_table = "sector_stocks_sync_tmp"
        backup_table = "sector_stocks_sync_backup"
        ch.command(f"DROP TABLE IF EXISTS {tmp_table}")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")
        ch.command(f"CREATE TABLE {tmp_table} AS sector_stocks")

        if rows:
            ch.insert(
                tmp_table,
                rows,
                column_names=["sector_code", "stock_code", "weight", "created_at"],
            )

        ch.command(f"RENAME TABLE sector_stocks TO {backup_table}, {tmp_table} TO sector_stocks")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")

        # Refresh stock_count in sectors via atomic rebuild
        tmp2 = "sectors_count_sync_tmp"
        bak2 = "sectors_count_sync_backup"
        ch.command(f"DROP TABLE IF EXISTS {tmp2}")
        ch.command(f"DROP TABLE IF EXISTS {bak2}")
        ch.command(f"CREATE TABLE {tmp2} AS sectors")
        ch.command(
            f"""
            INSERT INTO {tmp2} (code, name, type, parent_code, level, stock_count, created_at)
            SELECT s.code,
                   s.name,
                   s.type,
                   s.parent_code,
                   s.level,
                   toInt32(ifNull(m.cnt, 0)) AS stock_count,
                   s.created_at
            FROM sectors s
            LEFT JOIN (
                SELECT sector_code, count() AS cnt
                FROM sector_stocks
                GROUP BY sector_code
            ) m ON m.sector_code = s.code
            """
        )
        ch.command(f"RENAME TABLE sectors TO {bak2}, {tmp2} TO sectors")
        ch.command(f"DROP TABLE IF EXISTS {bak2}")


def main():
    logger.info("Start sector sync")
    syncer = SectorSyncer()
    syncer.sync_all_sectors(include_mappings=True)
    logger.info("Sector sync completed")


if __name__ == "__main__":
    main()
