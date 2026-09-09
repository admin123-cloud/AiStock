"""
Sync QMT sectors and sector memberships into ClickHouse with atomic swaps.

QMT is the canonical source for stock and sector membership. TDX can only be
used later as a price fallback through alias mapping, never as the owner of the
sector taxonomy.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
from utils.logger import get_logger
from utils.market_warehouse import clickhouse_client

logger = get_logger("SyncSectors")

FALSE_VALUES = {"0", "false", "no", "off"}

QMT_UNIVERSE_SECTOR_NAMES = {
    "上证A股",
    "上证B股",
    "上证转债",
    "京市A股",
    "创业板",
    "沪市ETF",
    "沪市债券",
    "沪市基金",
    "沪市指数",
    "沪深A股",
    "沪深B股",
    "沪深ETF",
    "沪深京A股",
    "沪深债券",
    "沪深基金",
    "沪深指数",
    "沪深转债",
    "深市ETF",
    "深市债券",
    "深市基金",
    "深市指数",
    "深证A股",
    "深证B股",
    "深证转债",
    "科创板",
    "科创板CDR",
}

QMT_UNIVERSE_KEYWORDS = ("A股", "B股", "ETF", "基金", "债券", "转债", "指数", "CDR")
QMT_LOCAL_ROOT = Path(os.environ.get("AISTOCK_QMT_ROOT", r"D:\国金QMT\国金证券QMT交易端"))
QMT_LOCAL_SW_SECTOR_DIRS = (
    ("申万一级行业板块", 1, "SW1"),
    ("申万二级行业板块", 2, "SW2"),
    ("申万三级行业板块", 3, "SW3"),
)
QMT_LOCAL_SYSTEM_WEIGHT_FILES = (
    Path("datadir") / "Weight" / "sectorWeightData.txt",
    Path("userdata_mini") / "datadir" / "Weight" / "systemSectorWeightData.txt",
)
STOCK_CODE_PATTERN = re.compile(r"\b(?:[036]\d{5}\.(?:SH|SZ)|[48]\d{5}\.BJ)\b", re.IGNORECASE)


def _is_truthy_env(name: str, default: str = "1") -> bool:
    return str(os.environ.get(name, default)).strip().lower() not in FALSE_VALUES


def _is_qmt_universe_sector(name: str) -> bool:
    text = str(name or "").strip()
    if text in QMT_UNIVERSE_SECTOR_NAMES:
        return True
    return len(text) <= 12 and any(keyword in text for keyword in QMT_UNIVERSE_KEYWORDS)


def _classify_qmt_sector(name: str) -> Tuple[str, int]:
    if _is_qmt_universe_sector(name):
        return "qmt_universe", 0
    if str(name or "").startswith("SW1"):
        return "industry", 1
    if str(name or "").startswith("SW2"):
        return "industry", 2
    if str(name or "").startswith("SW3"):
        return "industry", 3
    # Existing strategy queries use type='industry' AND level=2. QMT does not
    # expose a stable category field through xtdata, so every non-universe QMT
    # board is made strategy-visible at level 2.
    return "industry", 2


class SectorSyncer:
    def __init__(self, registered_only: bool = False):
        self.registered_only = registered_only
        self.pure_qmt = _is_truthy_env("AISTOCK_QMT_SECTOR_PURE_MODE", "1")
        self.filter_to_universe = _is_truthy_env("AISTOCK_QMT_SECTOR_FILTER_TO_UNIVERSE", "1")
        self.include_all_qmt_sectors = _is_truthy_env("AISTOCK_QMT_SECTOR_INCLUDE_ALL", "1")
        self.include_universe_sectors = _is_truthy_env("AISTOCK_QMT_SECTOR_INCLUDE_UNIVERSE", "0")
        self.stats = {
            "total_sectors": 0,
            "universe_sectors": 0,
            "industry_like_sectors": 0,
            "new_sectors": 0,
            "updated_sectors": 0,
            "total_mappings": 0,
            "new_mappings": 0,
            "dropped_non_universe_mappings": 0,
            "failed": 0,
            "source_fresh": False,
        }

    def sync_all_sectors(self, include_mappings: bool = True):
        logger.info("=" * 80)
        logger.info("Start syncing QMT sectors (atomic swap mode)")
        logger.info(
            "QMT sector mode: pure_qmt={}, filter_to_universe={}, include_all_qmt_sectors={}",
            self.pure_qmt,
            self.filter_to_universe,
            self.include_all_qmt_sectors,
        )
        logger.info("QMT universe sector mappings enabled={}", self.include_universe_sectors)
        logger.info("=" * 80)

        self._ensure_sector_tables()
        sectors = self._fetch_qmt_sectors()
        if not sectors:
            raise RuntimeError("QMT returned no sectors; abort atomic swap to protect existing sector tables")
        if self.registered_only and include_mappings:
            # Fetch and validate every membership before replacing either live table.
            universe = self._official_universe_codes()
            if self.filter_to_universe and not universe:
                raise RuntimeError('Official security universe unavailable; preserve sector metadata')
            client = self._qmt_client()
            from services.operations.reference_universe import current_stock_universe, scoped_members
            active = current_stock_universe(client)
            self.stats['excluded_members'] = {}
            for sector in sectors:
                raw = client.get_stock_list_in_sector(sector['qmt_name']) or []
                codes, excluded = scoped_members(raw, active)
                if universe and not set(codes).issubset(universe):
                    raise RuntimeError(f"Empty or unknown QMT membership for {sector['code']}; preserve sector metadata")
                sector['members'] = codes
                if excluded:
                    self.stats['excluded_members'][sector['code']] = excluded
        self._atomic_replace_sectors(sectors)

        if include_mappings:
            self._atomic_replace_sector_mappings(sectors)
        else:
            logger.info("Skip sector-stock mapping sync for lightweight sector-list run")

        logger.info("Sector sync completed, stats={}", self.stats)

    def _qmt_client(self) -> QmtMiniMarketClient:
        client = QmtMiniMarketClient()
        client.connect()
        return client

    def _fetch_qmt_sectors(self) -> List[Dict[str, Any]]:
        client = self._qmt_client()
        try:
            xtdata = client._ensure_connected()
            if hasattr(xtdata, "download_sector_data"):
                xtdata.download_sector_data()
                self.stats['source_fresh'] = True
                logger.info("QMT download_sector_data completed before sector list fetch")
            elif self.registered_only:
                raise RuntimeError('QMT online sector refresh capability is unavailable')
        except Exception as exc:
            if self.registered_only:
                raise RuntimeError('QMT online sector refresh failed; preserving registered taxonomy') from exc
            logger.warning("QMT download_sector_data failed before sector list fetch: {}", exc)

        try:
            sector_names = sorted({str(item).strip() for item in client.get_sector_list() if str(item).strip()})
        except Exception as exc:
            if self.registered_only:
                raise RuntimeError('QMT online sector list unavailable') from exc
            logger.warning("QMT get_sector_list failed, try local QMT sector files: {}", exc)
            return self._fetch_local_qmt_sector_files()

        if self.registered_only:
            registered = {str(row[0])[4:] for row in clickhouse_client().query(
                "SELECT code FROM sectors WHERE startsWith(code,'qmt:') AND type='industry'").result_rows}
            if not registered or not registered.issubset(set(sector_names)):
                raise RuntimeError('Registered QMT sector catalog is empty or differs from the refreshed source')
            sector_names = sorted(registered)
        elif not self.include_all_qmt_sectors:
            sector_names = [name for name in sector_names if _is_qmt_universe_sector(name)]
        if not sector_names:
            logger.warning("No QMT sectors returned, try local QMT sector files")
            return self._fetch_local_qmt_sector_files()

        out: List[Dict[str, Any]] = []
        for name in sector_names:
            sector_type, level = _classify_qmt_sector(name)
            if sector_type == "qmt_universe" and not self.include_universe_sectors:
                continue
            out.append(
                {
                    "code": f"qmt:{name}",
                    "name": name,
                    "qmt_name": name,
                    "type": sector_type,
                    "level": level,
                }
            )

        # QMT's online sector list can omit a recently refreshed SW component
        # even though the local QMT sector files already contain it.  Merge the
        # local QMT-owned SW memberships so an online refresh cannot erase a
        # valid canonical mapping (for example, SW2电池 -> 002245.SZ).
        local_rows = [] if self.registered_only else self._fetch_local_qmt_sector_files()
        merged: Dict[str, Dict[str, Any]] = {str(row["code"]): row for row in out}
        for local in local_rows:
            code = str(local["code"])
            existing = merged.get(code)
            if existing is None:
                merged[code] = local
                continue
            existing["members"] = list(local.get("members") or [])
            existing["qmt_name"] = str(local.get("qmt_name") or existing.get("qmt_name") or "")
            existing["name"] = str(local.get("name") or existing.get("name") or "")
            existing["type"] = str(local.get("type") or existing.get("type") or "industry")
            existing["level"] = int(local.get("level") or existing.get("level") or 2)
        out = list(merged.values())

        self.stats["universe_sectors"] = sum(1 for row in out if row["type"] == "qmt_universe")
        self.stats["industry_like_sectors"] = sum(1 for row in out if row["type"] == "industry")
        logger.info(
            "Fetched QMT sectors count={}, universe={}, industry_like={}",
            len(out),
            self.stats["universe_sectors"],
            self.stats["industry_like_sectors"],
        )
        return out

    def _fetch_local_qmt_sector_files(self) -> List[Dict[str, Any]]:
        base = QMT_LOCAL_ROOT / "datadir" / "Sector"
        if not base.exists():
            logger.warning("QMT local sector directory does not exist: {}", base)
            return []

        rows: List[Dict[str, Any]] = []
        if self.include_universe_sectors:
            rows.extend(self._fetch_local_qmt_universe_files())
        for dirname, level, prefix in QMT_LOCAL_SW_SECTOR_DIRS:
            folder = base / dirname
            if not folder.exists():
                logger.warning("QMT local SW sector folder missing: {}", folder)
                continue
            for path in sorted(folder.iterdir()):
                if not path.is_file() or path.name.lower().endswith(".xml"):
                    continue
                raw_name = path.name.strip()
                if not raw_name.startswith(prefix):
                    continue
                name = raw_name[len(prefix) :].strip() or raw_name
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except Exception as exc:
                    logger.warning("Read QMT local sector file failed: path={}, error={}", path, exc)
                    continue
                members = sorted({m.upper() for m in STOCK_CODE_PATTERN.findall(text)})
                if not members:
                    continue
                rows.append(
                    {
                        "code": f"qmt:{raw_name}",
                        "name": name,
                        "qmt_name": raw_name,
                        "type": "industry",
                        "level": level,
                        "members": members,
                    }
                )

        dedup = {str(row["code"]): row for row in rows}
        rows = list(dedup.values())
        self.stats["universe_sectors"] = sum(1 for row in rows if row["type"] == "qmt_universe")
        self.stats["industry_like_sectors"] = sum(1 for row in rows if row["type"] == "industry")
        logger.info("Fetched local QMT SW sectors count={} from {}", len(rows), base)
        return rows

    def _fetch_local_qmt_universe_files(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for rel_path in QMT_LOCAL_SYSTEM_WEIGHT_FILES:
            path = QMT_LOCAL_ROOT / rel_path
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception as exc:
                logger.warning("Read QMT local universe file failed: path={}, error={}", path, exc)
                continue
            for line in lines:
                parts = [part.strip() for part in line.split(";") if part.strip()]
                if len(parts) < 2:
                    continue
                name = parts[0]
                if not _is_qmt_universe_sector(name):
                    continue
                members = sorted({m.upper() for item in parts[1:] for m in STOCK_CODE_PATTERN.findall(item)})
                if not members:
                    continue
                rows.append(
                    {
                        "code": f"qmt:{name}",
                        "name": name,
                        "qmt_name": name,
                        "type": "qmt_universe",
                        "level": 0,
                        "members": members,
                    }
                )
        return rows

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

    def _official_universe_codes(self) -> Set[str]:
        if not self.filter_to_universe:
            return set()
        ch = clickhouse_client()
        try:
            rows = ch.query("SELECT code FROM stocks WHERE type IN ('stock', 'index')").result_rows
        except Exception as exc:
            logger.warning("Load official stock universe failed, skip sector member filtering: {}", exc)
            return set()
        codes = {str(r[0]).strip().upper() for r in rows if r and str(r[0]).strip()}
        logger.info("Loaded official universe codes for QMT sector mapping: {}", len(codes))
        return codes

    def _atomic_replace_sectors(self, sector_rows: List[Dict[str, Any]]):
        ch = clickhouse_client()
        now = datetime.now()

        dedup: Dict[str, Dict[str, Any]] = {}
        for row in sector_rows:
            dedup[row["code"]] = row
        items = list(dedup.values())
        self.stats["total_sectors"] = len(items)

        existing_rows = ch.query("SELECT code, name, type, level FROM sectors WHERE startsWith(code, 'qmt:')").result_rows
        existing_map = {
            str(r[0]): {"name": str(r[1] or ""), "type": str(r[2] or ""), "level": int(r[3] or 0)}
            for r in existing_rows
            if r and r[0]
        }

        self.stats["new_sectors"] = 0
        self.stats["updated_sectors"] = 0
        for row in items:
            old = existing_map.get(row["code"])
            row_type = str(row.get("type") or "industry")
            if old is None:
                self.stats["new_sectors"] += 1
            elif old["name"] != row["name"] or old["type"] != row_type or old["level"] != row["level"]:
                self.stats["updated_sectors"] += 1

        rows = []
        for row in items:
            rows.append(
                [
                    row["code"],
                    row["name"],
                    str(row.get("type") or "industry"),
                    None,
                    int(row["level"]),
                    0,
                    now,
                ]
            )

        tmp_table = "sectors_sync_tmp"
        backup_table = "sectors_sync_backup"
        ch.command(f"DROP TABLE IF EXISTS {tmp_table}")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")
        ch.command(f"CREATE TABLE {tmp_table} AS sectors")
        if not self.pure_qmt:
            ch.command(f"INSERT INTO {tmp_table} SELECT * FROM sectors WHERE NOT startsWith(code, 'qmt:')")

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

        client = None
        sector_by_code: Dict[str, str] = {str(s["code"]): str(s.get("qmt_name") or s["name"]) for s in sectors}
        sector_members_by_code: Dict[str, List[str]] = {
            str(s["code"]): list(s.get("members") or [])
            for s in sectors
            if s.get("members")
        }
        sector_codes: Set[str] = set(sector_by_code.keys())
        universe_codes = self._official_universe_codes()
        rows = []
        dropped_non_universe = 0

        for idx, sector_code in enumerate(sorted(sector_codes), start=1):
            if sector_code in sector_members_by_code:
                stock_list = sector_members_by_code[sector_code]
            else:
                if client is None:
                    client = self._qmt_client()
                try:
                    stock_list = client.get_stock_list_in_sector(sector_by_code[sector_code]) or []
                except Exception as exc:
                    logger.error("Fetch QMT sector members failed: sector={}, error={}", sector_code, exc)
                    self.stats["failed"] += 1
                    continue

            for stock in stock_list:
                stock_code = stock.get("Code") if isinstance(stock, dict) else stock
                if not stock_code:
                    continue
                code_text = str(stock_code).strip().upper()
                if universe_codes and code_text not in universe_codes:
                    dropped_non_universe += 1
                    continue
                rows.append([str(sector_code), code_text, 0.0, now])

            if idx % 10 == 0:
                logger.info("Sector mapping progress: {}/{} sectors", idx, len(sector_codes))

        self.stats["total_mappings"] = len(rows)
        self.stats["new_mappings"] = len(rows)
        self.stats["dropped_non_universe_mappings"] = dropped_non_universe
        if self.registered_only and self.stats['failed']:
            raise RuntimeError('Some QMT memberships failed; preserve existing membership table')
        if not rows:
            raise RuntimeError("QMT returned no sector members; abort atomic swap to protect existing sector_stocks")

        tmp_table = "sector_stocks_sync_tmp"
        backup_table = "sector_stocks_sync_backup"
        ch.command(f"DROP TABLE IF EXISTS {tmp_table}")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")
        ch.command(f"CREATE TABLE {tmp_table} AS sector_stocks")
        if not self.pure_qmt:
            ch.command(f"INSERT INTO {tmp_table} SELECT * FROM sector_stocks WHERE NOT startsWith(sector_code, 'qmt:')")

        if rows:
            ch.insert(
                tmp_table,
                rows,
                column_names=["sector_code", "stock_code", "weight", "created_at"],
            )

        ch.command(f"RENAME TABLE sector_stocks TO {backup_table}, {tmp_table} TO sector_stocks")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")

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
            WHERE ({1 if not self.pure_qmt else 0}) = 1 OR (startsWith(s.code, 'qmt:') AND ifNull(m.cnt, 0) > 0)
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
